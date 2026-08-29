"""Settings mixin: configuration bundle export / import.

拆分自 src/tools/settings_tool.py。负责配置束（含加密 Skill 文件）的
导出与导入流程，均通过后台 TaskManager 执行。
"""

import base64
import json
import os
import zlib

from PySide6.QtWidgets import QFileDialog

from src.core.core_task import TaskManager, TaskMode
from src.core.encryption_service import SystemEncryptionService
from src.task.config_task import ExportConfigTask, ImportConfigTask
from src.ui.components.dialog import (ExportPasswordDialog, ImportPasswordDialog,
                                      ProgressDialog, StandardDialog)


class ConfigTransferMixin:
    """配置导入 / 导出流程。"""

    # ---------- Export ----------
    def on_export_clicked(self):
        if not self.check_unsaved_changes(proceed_callback=self._execute_export):
            return
        self._execute_export()

    def _execute_export(self):
        # 1. 获取加密密码
        from src.ui.components.dialog import ExportPasswordDialog, ProgressDialog
        pwd_dlg = ExportPasswordDialog(self.widget)
        pwd_dlg.exec()
        if pwd_dlg.is_cancelled:
            return
        password = pwd_dlg.password

        # 2. 选择保存路径
        from PySide6.QtWidgets import QFileDialog
        import os
        path, _ = QFileDialog.getSaveFileName(
            self.widget, "Save Config", "scholar_navis_config.json", "JSON (*.json)"
        )
        if not path:
            return

        # 3. 准备数据束
        from src.core.encryption_service import SystemEncryptionService
        import zlib
        import base64

        enc_service = SystemEncryptionService()
        exported_skills_data = {}

        for name, cfg in self.config.mcp_servers.get("external_skills", {}).items():
            if name in ["built-in", "Academic Tool", "builtin"]: continue
            skill_path = cfg.get("command", "")
            if skill_path and os.path.exists(skill_path):
                try:
                    with open(skill_path, 'rb') as f:
                        decrypted_data = enc_service.decrypt(f.read())

                        if isinstance(decrypted_data, str):
                            decrypted_data = decrypted_data.encode('utf-8')

                        compressed_bytes = zlib.compress(decrypted_data, level=9)
                        b64_str = base64.b64encode(compressed_bytes).decode('utf-8')

                        exported_skills_data[name] = b64_str
                except Exception as e:
                    self.logger.warning(f"Failed to decrypt and compress skill {name} for export: {e}")

        bundle = {
            "settings": self.config.user_settings,
            "mcp_servers": self.config.mcp_servers,
            "llm_configs": self.llm_configs,
            "skill_files_zlib_b64": exported_skills_data
        }

        self.btn_export.setEnabled(False)
        pd = ProgressDialog(self.widget, "Security Export", "Performing compression, encryption & serialization...")
        pd.show()

        # 4. 启动异步导出任务，统一使用 export_task_mgr
        self.export_task_mgr = TaskManager()
        self.export_task_mgr.sig_progress.connect(pd.update_progress)
        self.export_task_mgr.sig_result.connect(lambda res: self._finalize_export(res, path, pd))
        self.export_task_mgr.start_task(
            ExportConfigTask,
            task_id="export_cfg",
            path=path,
            password=password,
            bundle=bundle
        )

    def _finalize_export(self, result, path, pd):
        """处理导出任务结果并持久化至磁盘"""
        self.btn_export.setEnabled(True)

        if result.get("success"):
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(result, f, indent=4, ensure_ascii=False)
                pd.show_finish_state(True, "Export Successful", f"Configuration bundle has been securely saved to:\n{path}")
            except Exception as e:
                pd.show_finish_state(False, "Write Error", f"Failed to write file to disk: {e}")
        else:
            pd.show_finish_state(False, "Export Failed", result.get("msg", "An analytical error occurred during encryption."))

    # ---------- Import ----------
    def on_import_clicked(self, auto_path=None):
        if not self.check_unsaved_changes(proceed_callback=lambda: self._execute_import(auto_path)):
            return
        self._execute_import(auto_path)

    def _execute_import(self, auto_path=None):
        # Support retry flow without opening file dialog twice
        path = auto_path
        if not path:
            path, _ = QFileDialog.getOpenFileName(self.widget, "Import Config Bundle", "", "JSON (*.json)")
        if not path: return

        # Quick Format Check
        try:
            with open(path, 'r', encoding='utf-8') as f:
                preview_bundle = json.load(f)
            is_encrypted = "payload" in preview_bundle and "salt" in preview_bundle
        except Exception as e:
            StandardDialog(self.widget, "Import Error", f"Invalid JSON file: {e}").exec()
            return

        password = None
        if is_encrypted:
            pwd_dlg = ImportPasswordDialog(self.widget)
            pwd_dlg.exec()
            if pwd_dlg.is_cancelled:
                return  # User cancelled the import process
            password = pwd_dlg.password

        self.btn_import.setEnabled(False)
        pd = ProgressDialog(self.widget, "Importing", "Reading and decrypting...")
        pd.show()

        self.import_task_mgr = TaskManager()
        self.import_task_mgr.sig_progress.connect(pd.update_progress)
        self.import_task_mgr.sig_result.connect(lambda res: self._finalize_import(res, pd, path))
        self.import_task_mgr.start_task(ImportConfigTask, "import_cfg", mode=TaskMode.THREAD, path=path,
                                        password=password)

    def _finalize_import(self, result, pd, path):
        self.btn_import.setEnabled(True)

        if not result.get("success"):
            if "Decryption failed" in result.get("msg", ""):
                pd.close_safe()
                from src.ui.components.dialog import StandardDialog
                dlg = StandardDialog(
                    self.widget,
                    "Decryption Failed",
                    "Incorrect password or corrupted file.\nWould you like to try entering the password again?",
                    show_cancel=True
                )
                if dlg.exec():
                    self.on_import_clicked(auto_path=path)
            else:
                pd.show_finish_state(False, "Import Failed", result.get("msg", "Unknown error"))
            return

        try:
            final_data = result.get("data", {})

            if "skill_files_zlib_b64" in final_data:
                from src.core.encryption_service import SystemEncryptionService
                import zlib
                import base64

                enc_service = SystemEncryptionService()

                for s_name, b64_str in final_data["skill_files_zlib_b64"].items():
                    try:
                        compressed_bytes = base64.b64decode(b64_str)
                        plain_code_bytes = zlib.decompress(compressed_bytes)

                        plain_code_str = plain_code_bytes.decode('utf-8')
                        encrypted_data = enc_service.encrypt(plain_code_str)

                        if isinstance(encrypted_data, str):
                            encrypted_bytes = encrypted_data.encode('utf-8')
                        else:
                            encrypted_bytes = encrypted_data

                        if "mcp_servers" in final_data and "external_skills" in final_data["mcp_servers"]:
                            if s_name in final_data["mcp_servers"]["external_skills"]:
                                final_data["mcp_servers"]["external_skills"][s_name]["_pending_bytes"] = encrypted_bytes
                                final_data["mcp_servers"]["external_skills"][s_name]["_is_new"] = True
                                final_data["mcp_servers"]["external_skills"][s_name][
                                    "command"] = f"<Pending Edit: {s_name}>"

                    except Exception as e:
                        self.logger.error(f"💥 CRITICAL: Failed to decompress and stage skill '{s_name}': {e}")
                        from src.ui.components.toast import ToastManager
                        ToastManager().show(f"Failed to restore skill {s_name}: {e}", "error")

            if "settings" in final_data:
                imported_settings = final_data.get("settings", {}).copy()

                imported_device = imported_settings.get("inference_device", "auto")
                if self.combo_device.findData(imported_device) < 0:
                    fallback_dev = "cpu" if self.combo_device.findData("cpu") >= 0 else "auto"
                    imported_settings["inference_device"] = fallback_dev
                    from src.ui.components.toast import ToastManager
                    ToastManager().show(
                        f"Imported device '{imported_device}' is unavailable. Defaulting to {fallback_dev.upper()}.",
                        "warning"
                    )

                self.config.user_settings = imported_settings

            if "mcp_servers" in final_data:
                self.config.mcp_servers = final_data.get("mcp_servers", {}).copy()
            if "llm_configs" in final_data:
                self.llm_configs = final_data.get("llm_configs", []).copy()

            self._load_current_settings(reload_from_disk=False)
            self._mark_unsaved()

            mode = result.get("mode", "unknown")
            pd.show_finish_state(
                True,
                "Import Successful",
                f"Configuration bundle ({mode}) has been loaded into the interface.\n\n"
                "Please click 'Save Settings' at the bottom to apply these changes permanently."
            )
            from src.ui.components.toast import ToastManager
            ToastManager().show(f"Configuration imported to UI ({mode}). Please save to apply.", "success")
        except Exception as e:
            self.logger.error(f"Import application failed: {e}")
            from src.ui.components.dialog import StandardDialog
            StandardDialog(self.widget, "Import Error", f"Failed to apply settings to UI:\n{e}").exec()
