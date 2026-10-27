"""Settings mixin: configuration bundle export / import.

拆分自 src/tools/settings_tool.py。负责配置束（含加密 Skill 文件）的
导出与导入流程，均通过后台 TaskManager 执行。
"""

import base64
import json
import os
import zlib

from src.core import safe_io
from src.core.core_task import TaskManager, TaskMode
from src.core.i18n import tr
from src.core.skill_store import read_skill_source
from src.task.config_task import ExportConfigTask, ImportConfigTask
from src.ui.components.dialog import (ExportPasswordDialog, ImportPasswordDialog,
                                      ProgressDialog, StandardDialog)
from src.ui.components.file_dialogs import open_file_name, save_file_name

#: 单个技能源码解压后的字节上限。正常技能脚本远小于此值，上限只用来挡住压缩炸弹。
_SKILL_CODE_MAX_BYTES = 8 * 1024 * 1024


def _decompress_limited(data: bytes, max_bytes: int = _SKILL_CODE_MAX_BYTES) -> bytes:
    """有上限的 zlib 解压。

    导入的配置束是不可信输入：几 KB 的 base64 可以膨胀到 GB 级，而
    ``zlib.decompress`` 没有长度参数，会直接把内存吃满。这里用
    ``decompressobj`` 限定输出，并检查是否还有未消耗的输入。
    """
    obj = zlib.decompressobj()
    out = obj.decompress(data, max_bytes + 1)
    if len(out) > max_bytes or obj.unconsumed_tail:
        raise ValueError(f"技能源码解压后超过 {max_bytes} 字节上限")
    tail = obj.flush()
    if len(out) + len(tail) > max_bytes:
        raise ValueError(f"技能源码解压后超过 {max_bytes} 字节上限")
    return out + tail


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
        path, _ = save_file_name(
            self.widget, tr("Save Config"), "scholar_navis_config.json", "JSON (*.json)"
        )
        if not path:
            return

        # 3. 准备数据束
        exported_skills_data = {}

        for name, cfg in self.config.mcp_servers.get("external_skills", {}).items():
            if name in ["built-in", "Academic Tool", "builtin"]: continue
            skill_path = cfg.get("command", "")
            if skill_path and os.path.exists(skill_path):
                try:
                    # .py 明文与历史 .enc 密文共用同一读取入口（src/core/skill_store.py）
                    code = read_skill_source(skill_path).encode('utf-8')
                    compressed_bytes = zlib.compress(code, level=9)
                    b64_str = base64.b64encode(compressed_bytes).decode('utf-8')

                    exported_skills_data[name] = b64_str
                except Exception as e:
                    self.logger.warning(f"Failed to read and compress skill {name} for export: {e}")

        bundle = {
            "settings": self.config.user_settings,
            "mcp_servers": self.config.mcp_servers,
            "llm_configs": self.llm_configs,
            "skill_files_zlib_b64": exported_skills_data
        }

        self.btn_export.setEnabled(False)
        pd = ProgressDialog(self.widget, tr("Security Export"),
                            tr("Performing compression, encryption & serialization..."))
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
                # 原子写：直接 open('w') 会先截断用户目标路径上的同名文件，
                # 中途失败就只剩半截 JSON，原有备份也一并被毁。
                safe_io.atomic_write_json(path, result)
                pd.show_finish_state(
                    True, tr("Export Successful"),
                    tr("Configuration bundle has been securely saved to:\n{path}").format(path=path))
            except (OSError, TypeError, ValueError) as e:
                pd.show_finish_state(False, tr("Write Error"),
                                     tr("Failed to write file to disk: {err}").format(err=e))
        else:
            pd.show_finish_state(
                False, tr("Export Failed"),
                result.get("msg", tr("An analytical error occurred during encryption.")))

    # ---------- Import ----------
    def on_import_clicked(self, auto_path=None):
        if not self.check_unsaved_changes(proceed_callback=lambda: self._execute_import(auto_path)):
            return
        self._execute_import(auto_path)

    def _execute_import(self, auto_path=None):
        # Support retry flow without opening file dialog twice
        path = auto_path
        if not path:
            path, _ = open_file_name(self.widget, tr("Import Config Bundle"), "", "JSON (*.json)")
        if not path: return

        # Quick Format Check
        try:
            with open(path, 'r', encoding='utf-8') as f:
                preview_bundle = json.load(f)
            is_encrypted = "payload" in preview_bundle and "salt" in preview_bundle
        except Exception as e:
            StandardDialog(self.widget, tr("Import Error"),
                           tr("Invalid JSON file: {err}").format(err=e)).exec()
            return

        password = None
        if is_encrypted:
            pwd_dlg = ImportPasswordDialog(self.widget)
            pwd_dlg.exec()
            if pwd_dlg.is_cancelled:
                return  # User cancelled the import process
            password = pwd_dlg.password

        self.btn_import.setEnabled(False)
        pd = ProgressDialog(self.widget, tr("Importing"), tr("Reading and decrypting..."))
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
                    tr("Decryption Failed"),
                    tr("Incorrect password or corrupted file.\n"
                       "Would you like to try entering the password again?"),
                    show_cancel=True
                )
                if dlg.exec():
                    self.on_import_clicked(auto_path=path)
            else:
                pd.show_finish_state(False, tr("Import Failed"),
                                     result.get("msg", tr("Unknown error")))
            return

        try:
            final_data = result.get("data", {})

            if "skill_files_zlib_b64" in final_data:
                for s_name, b64_str in final_data["skill_files_zlib_b64"].items():
                    try:
                        compressed_bytes = base64.b64decode(b64_str)
                        plain_code_str = _decompress_limited(compressed_bytes).decode('utf-8')

                        if "mcp_servers" in final_data and "external_skills" in final_data["mcp_servers"]:
                            if s_name in final_data["mcp_servers"]["external_skills"]:
                                # 源码明文暂存；落盘由 save_flow 写成 0o600 的 .py
                                final_data["mcp_servers"]["external_skills"][s_name]["_pending_code"] = plain_code_str
                                final_data["mcp_servers"]["external_skills"][s_name]["_is_new"] = True
                                final_data["mcp_servers"]["external_skills"][s_name][
                                    "command"] = f"<Pending Edit: {s_name}>"

                    except Exception as e:
                        self.logger.error(
                            f"Failed to decompress and stage skill '{s_name}': {e}")
                        from src.ui.components.toast import ToastManager
                        ToastManager().show(
                            tr("Failed to restore skill {name}: {err}").format(name=s_name, err=e),
                            "error")

            if "settings" in final_data:
                imported_settings = final_data.get("settings", {}).copy()

                imported_device = imported_settings.get("inference_device", "auto")
                imported_index = self.combo_device.findData(imported_device)
                imported_item = (self.combo_device.model().item(imported_index)
                                 if imported_index >= 0 else None)
                # 被禁用（不可用）的条目等同于不存在：导入的配置可能来自另一台机器
                if imported_item is None or not imported_item.isEnabled():
                    fallback_dev = "cpu" if self.combo_device.findData("cpu") >= 0 else "auto"
                    imported_settings["inference_device"] = fallback_dev
                    from src.ui.components.toast import ToastManager
                    ToastManager().show(
                        tr("Imported device '{device}' is unavailable. Defaulting to {fallback}.")
                        .format(device=imported_device, fallback=fallback_dev.upper()),
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
                tr("Import Successful"),
                tr("Configuration bundle ({mode}) has been loaded into the interface.\n\n"
                   "Please click 'Save Settings' at the bottom to apply these changes permanently.")
                .format(mode=mode)
            )
            from src.ui.components.toast import ToastManager
            ToastManager().show(
                tr("Configuration imported to UI ({mode}). Please save to apply.").format(mode=mode),
                "success")
        except Exception as e:
            self.logger.error(f"Import application failed: {e}")
            from src.ui.components.dialog import StandardDialog
            StandardDialog(self.widget, tr("Import Error"),
                           tr("Failed to apply settings to UI:\n{err}").format(err=e)).exec()
