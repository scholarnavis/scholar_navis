"""Attachment mixin: file attach, KB file pick, chat history export.

拆分自 src/tools/chat_tool.py：负责外部附件管理与聊天记录导出任务。
"""
import base64
import logging
import os
import re
from urllib.parse import quote

from PySide6.QtGui import QCursor
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFileDialog, QMenu

from src.core.core_task import TaskManager, TaskMode
from src.core.theme_manager import ThemeManager
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)


class ChatAttachmentsMixin:
    """附件管理与导出。"""

    #: 单条消息允许携带的最大图片数
    MAX_IMAGES_PER_MESSAGE = 8

    def attach_from_local(self):
        """按钮点击触发的文件选择器"""
        paths, _ = QFileDialog.getOpenFileNames(
            self.widget, "Select File(s)", "",
            "Supported Files (*.pdf *.md *.txt *.docx *.png *.jpg *.jpeg *.webp *.gif *.bmp *.svg);;"
            "Documents (*.pdf *.md *.txt *.docx);;"
            "Images (*.png *.jpg *.jpeg *.webp *.gif *.bmp *.svg)"
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

        from src.core.image_utils import is_image_file

        file_infos = []
        has_legacy_doc = False
        rejected = []

        # 已附加的图片数（用于限制单条消息图片上限）
        current_image_count = sum(1 for f in self.external_files if f.get("type") == "image")

        for item in items:
            if isinstance(item, str):
                path, name = item, os.path.basename(item)
            elif isinstance(item, dict):
                path, name = item.get("path", ""), item.get("name", os.path.basename(item.get("path", "")))
            else:
                continue

            if is_image_file(path) or is_image_file(name):
                # 图片附件：校验 + 构建结构化条目（SVG 在此栅格化）
                if current_image_count >= self.MAX_IMAGES_PER_MESSAGE:
                    rejected.append(f"{name} (image limit {self.MAX_IMAGES_PER_MESSAGE} reached)")
                    continue
                entry = self._prepare_image_entry(path, name)
                if entry:
                    file_infos.append(entry)
                    current_image_count += 1
                continue

            if isinstance(item, str):
                file_infos.append({"path": path, "name": name})
                if path.lower().endswith('.doc'):
                    has_legacy_doc = True
            else:
                file_infos.append(item)
                if name.lower().endswith('.doc'):
                    has_legacy_doc = True

        if has_legacy_doc:
            ToastManager().show("Legacy .doc format detected. It may not be fully parsed. Please convert to .docx",
                                "warning")

        if rejected:
            ToastManager().show(f"Some files were skipped: {'; '.join(rejected)}", "warning")

        # 直接将文件路径保存，交由 Chat 进程去处理
        self.external_files.extend(file_infos)

        for info in file_infos:
            # 图片附件不生成文本链接（由气泡缩略图与输入区预览条呈现）
            if info.get("type") == "image":
                continue
            path = info['path']
            f_name = info['name']
            safe_path = quote(path)
            safe_name = quote(f_name)
            link = f"cite://view?path={safe_path}&page=1&name={safe_name}"
            self.external_context_html += f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#05B8CC; text-decoration:none;'>📄 {f_name}</a></div>"

        if self.external_files:
            names = []
            image_files = []
            for c in self.external_files:
                if c.get('type') == 'image':
                    image_files.append(c)
                    continue
                if c['name'] not in names:
                    names.append(c['name'])

            if names:
                display_text = (f"{names[0]}, {names[1]} and {len(names) - 2} more"
                                if len(names) > 2 else ", ".join(names))
            elif image_files:
                display_text = f"{len(image_files)} image(s)"

            self._schedule_attachment_preview(display_text)
            ToastManager().show(f"Attached {len(names) + len(image_files)} file(s).", "success")
        else:
            self._refresh_attachment_preview()

    def _schedule_attachment_preview(self, display_text: str):
        """延迟刷新附件预览（单发定时器，可被发送流程取消）。

        同步链路（如开发者测试在同一调用栈内 attach -> send）中，
        ``process_send`` 会先停掉该定时器，避免发送完成后预览残留。
        """
        timer = getattr(self, '_attach_preview_timer', None)
        if timer is None:
            timer = QTimer(self.widget)
            timer.setSingleShot(True)
            self._attach_preview_timer = timer
        timer.stop()
        try:
            timer.timeout.disconnect()
        except (RuntimeError, TypeError):
            pass
        timer.timeout.connect(lambda: self._refresh_attachment_preview())
        timer.start(100)

    def _refresh_attachment_preview(self):
        """按当前 ``external_files`` 重建输入区预览（文本横幅 + 图片芯片）。"""
        names = []
        image_files = []
        for c in getattr(self, 'external_files', []):
            if c.get('type') == 'image':
                image_files.append(c)
                continue
            if c['name'] not in names:
                names.append(c['name'])

        if names:
            display_text = (f"{names[0]}, {names[1]} and {len(names) - 2} more"
                            if len(names) > 2 else ", ".join(names))
            self.input_container.show_context_preview(display_text)
        elif image_files:
            self.input_container.show_context_preview(f"{len(image_files)} image(s)")
        else:
            self.input_container.hide_context_preview()

        if hasattr(self.input_container, 'set_image_thumbs'):
            self.input_container.set_image_thumbs(image_files)

    def _prepare_image_entry(self, path, name):
        """校验并构建图片附件条目。

        - 非 SVG：``image_path`` 直接指向原文件；
        - SVG：先栅格化为 PNG（缓存于临时目录），``image_path`` 指向该
          PNG，供后台任务（无 Qt 环境）直接编码发送给视觉模型。
        - 返回 None 表示该文件被拒绝（不存在 / 超限 / 无法解析）。
        """
        from src.core.image_utils import is_svg_file, MAX_IMAGE_BYTES

        if not os.path.exists(path):
            ToastManager().show(f"Image not found: {name}", "error")
            return None

        try:
            if os.path.getsize(path) > MAX_IMAGE_BYTES:
                ToastManager().show(
                    f"Image '{name}' exceeds {MAX_IMAGE_BYTES // (1024 * 1024)} MB limit.", "error")
                return None
        except OSError as e:
            ToastManager().show(f"Cannot read image '{name}': {e}", "error")
            return None

        entry = {"type": "image", "path": path, "name": name}

        if is_svg_file(path):
            png_path = self._rasterize_svg(path)
            if not png_path:
                ToastManager().show(f"Failed to rasterize SVG '{name}'. The file cannot be sent to models.", "error")
                return None
            entry["image_path"] = png_path
        else:
            entry["image_path"] = path

        return entry

    @staticmethod
    def _rasterize_svg(svg_path):
        """将 SVG 渲染为 PNG 并缓存（UI 进程内完成，供子进程直接消费）。

        返回 PNG 路径；渲染失败返回 None。
        """
        import hashlib
        import tempfile

        try:
            from PySide6.QtSvg import QSvgRenderer
            from PySide6.QtGui import QImage, QPainter

            stat = os.stat(svg_path)
            hash_key = hashlib.md5(f"{svg_path}_{stat.st_mtime_ns}_{stat.st_size}".encode()).hexdigest()
            cache_dir = os.path.join(tempfile.gettempdir(), "scholar_navis_cache")
            os.makedirs(cache_dir, exist_ok=True)
            png_path = os.path.join(cache_dir, f"{hash_key}.png")

            if os.path.exists(png_path):
                return png_path

            renderer = QSvgRenderer(svg_path)
            if not renderer.isValid():
                return None

            size = renderer.defaultSize()
            if not size.isValid() or size.isEmpty():
                from PySide6.QtCore import QSize
                size = QSize(1024, 768)

            img = QImage(size.width(), size.height(), QImage.Format_ARGB32)
            img.fill(Qt.transparent)
            painter = QPainter(img)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            renderer.render(painter)
            painter.end()

            if not img.save(png_path, "PNG"):
                return None
            return png_path
        except Exception as e:
            logger.warning(f"SVG rasterization failed for {svg_path}: {e}")
            return None

    def remove_attached_image(self, info):
        """从待发送附件中移除指定图片（输入区缩略图上的 x 按钮）。"""
        self.external_files = [f for f in getattr(self, 'external_files', []) if f is not info]
        self._refresh_attachment_preview()

    def open_attachment_image(self, image_path):
        """预览待发送的图片附件（输入区缩略图单击）。"""
        from src.ui.components.image_viewer import open_image_viewer
        if image_path and os.path.exists(image_path):
            open_image_viewer(image_path, parent=getattr(self, 'widget', None))
        else:
            ToastManager().show(f"Image file not found: {os.path.basename(str(image_path))}", "error")

    def clear_attached_context(self):
        self.external_files = []
        self.external_context_html = ""
        self.input_container.hide_context_preview()
        if hasattr(self.input_container, 'set_image_thumbs'):
            self.input_container.set_image_thumbs([])

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
