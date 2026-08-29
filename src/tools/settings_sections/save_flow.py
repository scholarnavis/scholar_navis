"""Settings mixin: save flow (validation, persistence and post-save bootstrap).

拆分自 src/tools/settings_tool.py。负责保存按钮触发后的完整流程：
Email 校验 -> 配置落盘 -> 环境变量同步 -> 模型校验任务 -> MCP 重载。
"""

import logging
import os

import qdarktheme
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from src.core import BASE_DIR
from src.core.core_task import TaskManager, TaskMode, TaskState
from src.core.mcp_manager import MCPManager
from src.core.network_worker import setup_global_network_env
from src.core.signals import GlobalSignals
from src.core.skill_manager import SkillManager
from src.core.theme_manager import ThemeManager
from src.task.settings_tasks import EmailVerifyTask
from src.task.common_task import VerifyModelsTask
from src.ui.components.dialog import (ProgressDialog, StandardDialog,
                                      UnsavedChangesDialog)


class SaveFlowMixin:
    """保存设置流程。"""

    # ---------- Navigation guard ----------
    def check_unsaved_changes(self, proceed_callback=None) -> bool:
        """
        Navigation Guard: Evaluates the current component state for the main router.
        When unsaved changes exist, it intercepts the navigation with a custom modal.
        @return: True (allow routing), False (block routing and stay on the current component)
        """
        if getattr(self, '_is_loading', False) or not self.btn_undo.isEnabled():
            return True

        dlg = UnsavedChangesDialog(self.widget)
        dlg.exec()

        if dlg.user_choice == "save":
            self.on_save_clicked(on_success=proceed_callback)
            return False
        elif dlg.user_choice == "revert":
            self.on_undo_clicked()
            return True
        else:
            return False

    # ---------- Save ----------
    def on_save_clicked(self, on_success=None):
        self._pending_route_callback = on_success
        self.widget.setFocus()

        new_email = self.input_ncbi_email.text().strip()
        old_email = self.config.user_settings.get("ncbi_email", "").strip()

        self.save_pd = ProgressDialog(
            self.widget, "Applying Settings",
            "Validating settings and email address...",
            telemetry_config={"cpu": False, "ram": False, "gpu": False, "net": False, "io": False}
        )
        self.save_pd.pbar.setRange(0, 0)
        self.save_pd.show()
        QApplication.processEvents()

        # 如果email没有发生修改或为空，跳过耗时的email验证
        if new_email == old_email or not new_email:
            QTimer.singleShot(50, lambda: self._on_email_verified_result({"success": True, "msg": ""}))
            return

        self.email_task_mgr = TaskManager()
        self.email_task_mgr.sig_result.connect(self._on_email_verified_result)
        self.save_pd.sig_canceled.connect(self.email_task_mgr.cancel_task)

        self.email_task_mgr.start_task(
            EmailVerifyTask,
            task_id="email_verify",
            mode=TaskMode.THREAD,
            email=new_email
        )

    def _on_email_verified_result(self, result):
        success = result.get("success", False)
        msg = result.get("msg", "")

        # Email validation failure fallback
        if not success:
            if hasattr(self, 'save_pd'):
                self.save_pd.close_safe()
            StandardDialog(
                self.widget,
                "Validation Error",
                msg
            ).exec()
            return

        self.save_pd.update_progress(-1, "Saving configurations...")
        QApplication.processEvents()

        new_email = self.input_ncbi_email.text().strip()

        if hasattr(self, '_sync_llm_data'):
            self._sync_llm_data_execute()
        self._save_llm_config()

        new_mcp_servers = {}
        new_external_skills = {}
        if hasattr(self, 'table_mcp'):
            active_skill_names = set()
            for row in range(self.table_mcp.rowCount()):
                name_item = self.table_mcp.item(row, 1)
                if not name_item: continue

                name = name_item.text()

                if name == "Academic Tool":
                    continue

                cfg = name_item.data(Qt.UserRole).copy()

                chk_widget = self.table_mcp.cellWidget(row, 0)
                if chk_widget:
                    chk = chk_widget.layout().itemAt(0).widget()
                    cfg["enabled"] = chk.isChecked()

                if cfg.get("type") == "SKILL":
                    active_skill_names.add(name)
                    if "_pending_bytes" in cfg:
                        workspace_dir = os.path.join(BASE_DIR, 'tools', 'skill')
                        os.makedirs(workspace_dir, exist_ok=True)
                        target_path = os.path.join(workspace_dir, f"{name}.enc")
                        try:
                            with open(target_path, 'wb') as f:
                                f.write(cfg["_pending_bytes"])
                            cfg["command"] = target_path
                        except Exception as e:
                            self.logger.error(f"Failed to write skill '{name}' to disk: {e}")

                        for temp_key in ["_pending_bytes", "_is_new", "_is_edited"]:
                            cfg.pop(temp_key, None)

                    new_external_skills[name] = cfg
                else:
                    new_mcp_servers[name] = cfg

            old_skills = self.config.mcp_servers.get("external_skills", {})
            import shutil
            for old_name, old_cfg in old_skills.items():
                if old_name not in active_skill_names:
                    old_path = old_cfg.get("command", "")
                    if old_path and "tools" in old_path and "skill" in old_path:
                        try:
                            if os.path.exists(old_path):
                                os.remove(old_path)
                            skill_workspace = os.path.join(self.config.BASE_DIR, 'tools', 'skill',
                                                           f"{old_name}_workspace")
                            if os.path.exists(skill_workspace) and os.path.isdir(skill_workspace):
                                shutil.rmtree(skill_workspace)
                        except Exception as e:
                            self.logger.warning(f"Failed to properly clean up deleted skill for '{old_name}': {e}")

            self.config.mcp_servers["mcpServers"] = new_mcp_servers
            self.config.mcp_servers["external_skills"] = new_external_skills
            self.config.save_mcp_servers()

        new_key = self.input_ncbi_api_key.text().strip()
        new_openalex_key = self.input_openalex_api_key.text().strip()
        new_s2_key = self.input_s2_api_key.text().strip()
        s2_rate_text = self.input_s2_rate_limit.text().strip()
        try:
            val = float(s2_rate_text.replace(',', '.'))
            if val <= 0: val = 1.0
            s2_rate_text = str(val)
        except ValueError:
            s2_rate_text = "1.0"

        new_github_token = self.input_github_token.text().strip()

        mode_idx = self.combo_proxy_mode.currentIndex()
        new_proxy_mode = ["off", "custom"][mode_idx]
        new_proxy_url = self.input_proxy.text().strip()

        new_theme = self.combo_theme.currentText().lower()

        qdarktheme.setup_theme(new_theme)
        ThemeManager().set_theme(new_theme)

        try:
            api_port = int(self.input_api_port.text().strip())
        except ValueError:
            api_port = 8000

        new_theme = self.combo_theme.currentText()

        self.config.user_settings.update({
            "proxy_mode": new_proxy_mode,
            "proxy_url": new_proxy_url,
            "hf_mirror": self.input_mirror.text().strip(),
            "inference_device": self.combo_device.currentData(),
            "current_model_id": self.combo_embed.currentData(),
            "rerank_model_id": self.combo_rerank.currentData(),
            "active_llm_id": self._get_active_llm_id(),
            "theme": new_theme,
            "log_level": self.combo_log.currentText(),
            "ncbi_email": new_email,
            "ncbi_api_key": new_key,
            "openalex_api_key": new_openalex_key,
            "s2_api_key": new_s2_key,
            "s2_rate_limit": s2_rate_text,
            "github_token": new_github_token,
            "api_server_host": self.input_api_host.text().strip(),
            "api_server_port": api_port,
            "api_server_key": self.input_api_key.text().strip(),
        })

        for old_key in ["external_mcp_enabled", "network_mcps", "network_mcp_enabled", "custom_network_models"]:
            self.config.user_settings.pop(old_key, None)

        self.config.save_settings()

        new_theme_lower = new_theme.lower()
        qdarktheme.setup_theme(new_theme_lower)
        ThemeManager().set_theme(new_theme_lower)

        if new_s2_key:
            os.environ["S2_API_KEY"] = new_s2_key
        else:
            os.environ.pop("S2_API_KEY", None)

        if self.input_s2_rate_limit.text().strip():
            os.environ["S2_RATE_LIMIT"] = self.input_s2_rate_limit.text().strip()
        else:
            os.environ.pop("S2_RATE_LIMIT", None)

        if new_key:
            os.environ["NCBI_API_KEY"] = new_key
        else:
            os.environ.pop("NCBI_API_KEY", None)

        if new_openalex_key:
            os.environ["OPENALEX_API_KEY"] = new_openalex_key
        else:
            os.environ.pop("OPENALEX_API_KEY", None)

        if new_email:
            os.environ["NCBI_API_EMAIL"] = new_email
        else:
            os.environ.pop("NCBI_API_EMAIL", None)

        if new_github_token:
            os.environ["GITHUB_TOKEN"] = new_github_token
        else:
            os.environ.pop("GITHUB_TOKEN", None)

        if new_proxy_mode == "custom" and new_proxy_url:
            os.environ["HTTP_PROXY"] = new_proxy_url
            os.environ["HTTPS_PROXY"] = new_proxy_url
            os.environ["http_proxy"] = new_proxy_url
            os.environ["https_proxy"] = new_proxy_url
        else:
            for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
                os.environ.pop(k, None)

        setup_global_network_env()

        try:
            from src.task.s2_task import s2_manager
            s2_manager.reload_config()
            logging.info("S2 Task Manager configuration reloaded successfully.")
        except Exception as e:
            logging.error(f"Failed to reload S2 Task Manager: {e}")

        if hasattr(GlobalSignals(), 'llm_config_changed'):
            GlobalSignals().llm_config_changed.emit()

        logging.getLogger().setLevel(getattr(logging, self.combo_log.currentText()))

        self.save_pd.update_progress(-1, "Initializing background tasks...")

        if hasattr(self, 'save_task_mgr') and self.save_task_mgr:
            self.save_task_mgr.cancel_task()

        self.save_task_mgr = TaskManager()
        self.save_task_mgr.sig_progress.connect(self.save_pd.update_progress)
        self.save_task_mgr.sig_state_changed.connect(self._on_save_task_state_changed)
        self.save_task_mgr.sig_result.connect(self._on_save_task_result)
        self.save_pd.sig_canceled.connect(self.save_task_mgr.cancel_task)

        embed_id = self.combo_embed.currentData()
        rerank_id = self.combo_rerank.currentData()

        self.save_task_mgr.start_task(
            VerifyModelsTask,
            task_id="verify_settings",
            mode=TaskMode.THREAD,
            embed_id=embed_id,
            rerank_id=rerank_id,
            mcp_config=getattr(self.config, 'mcp_servers', {})
        )

    def _on_save_task_state_changed(self, state, msg):
        if state in [TaskState.FAILED.value, TaskState.TERMINATED.value]:
            if hasattr(self, 'save_pd'):
                self.save_pd.show_finish_state(False, "Process Halted", f"Save process ended: {msg}")

    def _on_save_task_result(self, result_dict):
        self._clear_unsaved()

        def _bootstrap_mcp_async():
            try:
                from src.core.mcp_manager import MCPManager
                mcp_mgr = MCPManager.get_instance()
                mcp_mgr.bootstrap_servers()
                skill_mgr = SkillManager.get_instance()
                if hasattr(skill_mgr, 'reload_external_skills'):
                    skill_mgr.reload_external_skills()

                if hasattr(self, '_refresh_mcp_status'):
                    self._refresh_mcp_status()
            except Exception as e:
                self.logger.error(f"MCP Update Failed: {e}")

        QTimer.singleShot(100, _bootstrap_mcp_async)

        msg = "Settings saved successfully."
        if result_dict and result_dict.get("to_download"):
            msg += "\n\nNote: Some selected models are missing locally. Please click 'Download' next to the models to fetch and convert them."

        if hasattr(self, 'save_pd'):
            self.save_pd.show_finish_state(True, "Settings Saved", msg)

        self.check_models_status()

        if hasattr(self, '_pending_route_callback') and self._pending_route_callback:
            cb = self._pending_route_callback
            self._pending_route_callback = None
            cb()
