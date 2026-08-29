"""Attachment mixin: file attach, KB file pick, chat history export.

拆分自 src/tools/chat_tool.py：负责外部附件管理与聊天记录导出任务。
"""
import base64
import logging
import os
import re
from urllib.parse import quote

from PySide6.QtGui import QCursor
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog, QMenu

from src.core.core_task import TaskManager, TaskMode
from src.core.theme_manager import ThemeManager
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)


class ChatAttachmentsMixin:
    """附件管理与导出。"""

    def attach_from_local(self):
        """按钮点击触发的文件选择器"""
        paths, _ = QFileDialog.getOpenFileNames(
            self.widget, "Select Document(s)", "",
            "Supported Files (*.pdf *.md *.txt *.docx);;"
            "Documents (*.pdf *.md *.txt *.docx)"
        )
        if not paths:
            return
        self.process_attached_files(paths)

    def attach_from_kb(self):
        from src.ui.components.dialog import SelectKBFileDialog
        dlg = SelectKBFileDialog(self.widget)

        if dlg.exec():
            file_infos = dlg.get_selected_file_infos()
            if file_infos:
                self.process_attached_files(file_infos)

    def show_attachment_menu(self):
        tm = ThemeManager()
        menu = QMenu(self.widget)
        menu.setStyleSheet(f"""
            QMenu {{ background-color: {tm.color('bg_card')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; }}
            QMenu::item {{ padding: 6px 20px; }}
            QMenu::item:selected {{ background-color: {tm.color('accent')}; color: #fff; }}
        """)

        act_kb = menu.addAction(tm.icon("folder", "text_main"), "Select from Knowledge Base")
        act_local = menu.addAction(tm.icon("upload", "text_main"), "Upload Local File")

        act_kb.triggered.connect(self.attach_from_kb)
        act_local.triggered.connect(self.attach_from_local)

        menu.exec(QCursor.pos())

    def process_attached_files(self, items):
        if not hasattr(self, 'external_files'):
            self.external_files = []
        if not hasattr(self, 'external_context_html'):
            self.external_context_html = ""

        file_infos = []
        has_legacy_doc = False

        for item in items:
            if isinstance(item, str):
                file_infos.append({"path": item, "name": os.path.basename(item)})
                if item.lower().endswith('.doc'):
                    has_legacy_doc = True
            elif isinstance(item, dict):
                file_infos.append(item)
                if item.get("name", "").lower().endswith('.doc'):
                    has_legacy_doc = True

        if has_legacy_doc:
            ToastManager().show("Legacy .doc format detected. It may not be fully parsed. Please convert to .docx",
                                "warning")

        # 直接将文件路径保存，交由 Chat 进程去处理
        self.external_files.extend(file_infos)

        for info in file_infos:
            path = info['path']
            f_name = info['name']
            safe_path = quote(path)
            safe_name = quote(f_name)
            link = f"cite://view?path={safe_path}&page=1&name={safe_name}"
            self.external_context_html += f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#05B8CC; text-decoration:none;'>📄 {f_name}</a></div>"

        if self.external_files:
            names = []
            for c in self.external_files:
                if c['name'] not in names:
                    names.append(c['name'])

            display_text = f"{names[0]}, {names[1]} and {len(names) - 2} more" if len(names) > 2 else ", ".join(names)
            QTimer.singleShot(100, lambda: self.input_container.show_context_preview(display_text))
            ToastManager().show(f"Attached {len(names)} file(s).", "success")
        else:
            self.input_container.hide_context_preview()

    def clear_attached_context(self):
        self.external_files = []
        self.external_context_html = ""
        self.input_container.hide_context_preview()

    def export_chat_history(self):
        if not self.history:
            ToastManager().show("There are currently no chat records to export.", "warning")
            self.logger.warning("Attempted to export empty chat history.")
            return

        tm = ThemeManager()
        menu = QMenu(self.widget)
        menu.setStyleSheet(f"""
            QMenu {{ background-color: {tm.color('bg_card')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; border-radius: 6px; padding: 4px;}}
            QMenu::item {{ padding: 6px 20px; border-radius: 4px;}}
            QMenu::item:selected {{ background-color: {tm.color('accent')}; color: #fff; }}
        """)

        act_pdf = menu.addAction(tm.icon("article", "text_main"), "Export as PDF")
        act_md = menu.addAction(tm.icon("markdown", "text_main"), "Export as MD")
        act_txt = menu.addAction(tm.icon("file-text", "text_main"), "Export as TXT")

        # 在鼠标位置弹出菜单
        action = menu.exec(QCursor.pos())
        if not action:
            return

        if action == act_pdf:
            filter_str, default_ext = "PDF Document (*.pdf)", ".pdf"
        elif action == act_md:
            filter_str, default_ext = "Markdown File (*.md)", ".md"
        else:
            filter_str, default_ext = "Text File (*.txt)", ".txt"

        # 弹出系统保存对话框
        path, _ = QFileDialog.getSaveFileName(
            self.widget, "Export Log", f"Scholar_Navis_Log{default_ext}", filter_str
        )

        if not path:
            return

        if not path.endswith(default_ext):
            path += default_ext

        def _get_colored_svg_base64(icon_name, color_hex):
            svg_path = tm.get_resource_path("assets", "icons", f"{icon_name}.svg")
            try:
                with open(svg_path, "r", encoding="utf-8") as f:
                    svg_content = f.read()
                if "<svg" in svg_content:
                    svg_content = re.sub(r'<svg', f'<svg fill="{color_hex}"', svg_content, count=1)
                encoded = base64.b64encode(svg_content.encode('utf-8')).decode('utf-8')
                return f"data:image/svg+xml;base64,{encoded}"
            except Exception:
                return ""

        colors = {
            'title_blue': tm.color('title_blue'),
            'academic_blue': tm.color('academic_blue'),
            'success': tm.color('success')
        }

        user_icon_b64 = _get_colored_svg_base64("user", tm.color('academic_blue'))
        ai_icon_b64 = _get_colored_svg_base64("ai_model", tm.color('success'))

        # 初始化后台导出任务并连接弹窗
        from src.ui.components.dialog import ProgressDialog
        self.export_pd = ProgressDialog(self.widget, "Exporting Chat", "Processing file in background...")
        self.export_pd.show()

        self.export_task_mgr = TaskManager()
        self.export_task_mgr.sig_progress.connect(self.export_pd.update_progress)
        self.export_task_mgr.sig_state_changed.connect(self._on_export_state_changed)
        self.export_task_mgr.sig_result.connect(self._on_export_result)

        from src.task.chat_tasks import ExportChatTask
        self.export_task_mgr.start_task(
            ExportChatTask,
            task_id="export_chat",
            mode=TaskMode.THREAD,
            history=self.history,
            path=path,
            export_fmt=default_ext,
            colors=colors,
            font_family=tm.font_family(),
            user_icon=user_icon_b64,
            ai_icon=ai_icon_b64
        )

    def _on_export_state_changed(self, state, msg):
        from src.core.core_task import TaskState
        if state == TaskState.FAILED.value:
            self.export_pd.show_finish_state(False, "Export Failed", str(msg))

    def _on_export_result(self, result):
        if result and result.get("success"):
            self.export_pd.show_finish_state(True, "Export Complete",
                                             f"Saved to {os.path.basename(result.get('path', ''))}")
            ToastManager().show(f"Document successfully exported.", "success")
            self.logger.info(f"Chat history successfully exported to: {result.get('path')}")
        else:
            self.export_pd.show_finish_state(False, "Export Failed",
                                             result.get("msg", "Unknown error") if result else "Unknown error")
            self.logger.error(f"Failed to export document: {result.get('msg') if result else 'None'}")
