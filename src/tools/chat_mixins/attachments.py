"""Attachment mixin: file attach, KB file pick, chat history export & import.

拆分自 src/tools/chat_tool.py：负责外部附件管理与聊天记录导入 / 导出任务。
"""
import base64
import logging
import os
import re
from urllib.parse import quote

from PySide6.QtGui import QCursor
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QMenu

from src.core.core_task import TaskManager, TaskMode
from src.core.file_types import (ATTACHABLE_EXTS, DOCUMENT_EXTS, IMAGE_EXTS,
                                 TEXT_VIEWER_EXTS)
from src.core.i18n import tr
from src.core.theme_manager import ThemeManager
from src.ui.components.file_dialogs import (open_file_name, open_file_names,
                                          save_file_name)
from src.ui.components.text_formatter import mono_font_family_css
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)


def _glob_patterns(exts) -> str:
    """把扩展名集合转成 QFileDialog 过滤串（排序保证各平台顺序稳定）。"""
    return " ".join(f"*.{ext}" for ext in sorted(exts))


class ChatAttachmentsMixin:
    """附件管理与导出。"""

    #: 单条消息允许携带的最大图片数
    MAX_IMAGES_PER_MESSAGE = 8

    def attach_from_local(self):
        """按钮点击触发的文件选择器。

        过滤清单全部由 :mod:`src.core.file_types` 派生，新增格式只改那一处即可，
        不会出现"查看器能打开但选不到 / 拖不进来"的脱节。
        """
        filter_str = ";;".join([
            f"Supported Files ({_glob_patterns(ATTACHABLE_EXTS)})",
            f"Documents & Text ({_glob_patterns(DOCUMENT_EXTS | TEXT_VIEWER_EXTS)})",
            f"Images ({_glob_patterns(IMAGE_EXTS)})",
        ])
        paths, _ = open_file_names(
            self.widget, "Select File(s)", "", filter_str)
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

    def attach_from_clipboard(self):
        """从剪贴板取图并走统一附件校验链路（Ctrl+V 与 Attach 菜单共用）。

        优先级：本地图片文件（资源管理器复制）> 位图（截图工具 / 浏览器
        "复制图片"）。位图落盘到临时缓存目录后按普通文件处理（大小校验、
        数量上限、缩略图预览与多模态发送链路全部复用）；剪贴板无图或
        落盘失败时给出明确提示。
        """
        from PySide6.QtGui import QGuiApplication

        clipboard = QGuiApplication.clipboard()
        mime = clipboard.mimeData()

        if mime.hasUrls():
            from src.core.image_utils import IMAGE_EXTENSIONS
            img_paths = [
                url.toLocalFile() for url in mime.urls()
                if url.isLocalFile()
                and url.toLocalFile().lower().endswith(IMAGE_EXTENSIONS)
            ]
            if img_paths:
                self.process_attached_files(img_paths)
                return

        image = clipboard.image()
        if image.isNull():
            ToastManager().show(tr("No image found in clipboard."), "warning")
            return

        path = self._save_clipboard_image(image)
        if not path:
            ToastManager().show(tr("Failed to save clipboard image."), "error")
            return

        self.process_attached_files([path])

    @staticmethod
    def _save_clipboard_image(image):
        """把剪贴板位图落盘为 PNG（内容哈希命名，重复粘贴自动去重）。

        :param image: 非空的 QImage（来自剪贴板）。
        :return: 临时 PNG 路径；保存失败返回 None。
        """
        import hashlib
        import tempfile

        try:
            digest = hashlib.md5()
            # 位图内容哈希：同一次截图重复粘贴命中同一文件，不产生冗余副本
            bits = image.constBits()
            digest.update(bytes(bits) if bits is not None else b"")
            digest.update(f"{image.width()}x{image.height()}".encode())

            cache_dir = os.path.join(tempfile.gettempdir(), "scholar_navis_cache")
            os.makedirs(cache_dir, exist_ok=True)
            path = os.path.join(cache_dir, f"clipboard_{digest.hexdigest()[:12]}.png")

            if os.path.exists(path):
                return path
            if not image.save(path, "PNG"):
                return None
            return path
        except Exception as e:
            logger.warning(f"Clipboard image save failed: {e}")
            return None

    def show_attachment_menu(self):
        tm = ThemeManager()
        menu = QMenu(self.widget)
        menu.setStyleSheet(f"""
            QMenu {{ background-color: {tm.color('bg_card')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; }}
            QMenu::item {{ padding: 6px 20px; }}
            QMenu::item:selected {{ background-color: {tm.color('accent')}; color: #fff; }}
        """)

        act_kb = menu.addAction(tm.icon("folder", "text_main"), tr("Select from Knowledge Base"))
        act_local = menu.addAction(tm.icon("upload", "text_main"), tr("Upload Local File"))
        act_clip = menu.addAction(tm.icon("copy", "text_main"), tr("Paste Image from Clipboard"))

        act_kb.triggered.connect(self.attach_from_kb)
        act_local.triggered.connect(self.attach_from_local)
        act_clip.triggered.connect(self.attach_from_clipboard)

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
            ToastManager().show(tr("Legacy .doc format detected. It may not be fully parsed. Please convert to .docx"),
                                "warning")

        if rejected:
            ToastManager().show(tr("Some files were skipped: {names}").format(names='; '.join(rejected)),
                                "warning")

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
            self.external_context_html += (
                f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' "
                f"style='color:{ThemeManager().color('accent')}; text-decoration:none;'>"
                f"📄 {f_name}</a></div>")

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
                display_text = (tr("{first}, {second} and {rest} more").format(
                    first=names[0], second=names[1], rest=len(names) - 2)
                    if len(names) > 2 else ", ".join(names))
            elif image_files:
                display_text = tr("{n} image(s)").format(n=len(image_files))

            self._schedule_attachment_preview(display_text)
            ToastManager().show(
                tr("Attached {n} file(s).").format(n=len(names) + len(image_files)), "success")
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
        """按当前 ``external_files`` 重建输入区预览（文本横幅 + 图片/文档芯片）。"""
        names = []
        image_files = []
        doc_files = []
        for c in getattr(self, 'external_files', []):
            if c.get('type') == 'image':
                image_files.append(c)
                continue
            doc_files.append(c)
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
        if hasattr(self.input_container, 'set_file_chips'):
            self.input_container.set_file_chips(doc_files)

    def _prepare_image_entry(self, path, name):
        """校验并构建图片附件条目。

        - 非 SVG：``image_path`` 直接指向原文件；
        - SVG：先栅格化为 PNG（缓存于临时目录），``image_path`` 指向该
          PNG，供后台任务（无 Qt 环境）直接编码发送给视觉模型。
        - 返回 None 表示该文件被拒绝（不存在 / 超限 / 无法解析）。
        """
        from src.core.image_utils import is_svg_file, MAX_IMAGE_BYTES

        if not os.path.exists(path):
            ToastManager().show(tr("Image not found: {name}").format(name=name), "error")
            return None

        try:
            if os.path.getsize(path) > MAX_IMAGE_BYTES:
                ToastManager().show(
                    tr("Image '{name}' exceeds {limit} MB limit.")
                    .format(name=name, limit=MAX_IMAGE_BYTES // (1024 * 1024)), "error")
                return None
        except OSError as e:
            ToastManager().show(tr("Cannot read image '{name}': {err}").format(name=name, err=e), "error")
            return None

        entry = {"type": "image", "path": path, "name": name}

        if is_svg_file(path):
            png_path = self._rasterize_svg(path)
            if not png_path:
                ToastManager().show(
                    tr("Failed to rasterize SVG '{name}'. The file cannot be sent to models.").format(name=name),
                    "error")
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
            ToastManager().show(
                tr("Image file not found: {name}").format(name=os.path.basename(str(image_path))), "error")

    def open_attachment_file(self, path, name=""):
        """打开待发送的文档附件（输入区芯片单击）。

        复用已发送消息中 ``cite://`` 链接的统一路由：PDF / 文本走内部查看器，
        其余扩展名（如 DOCX）交由系统默认程序，保证"发送前预览"与"发送后点击
        链接"行为完全一致（同一套实现，不在此处重复分支）。
        """
        if not path or not os.path.exists(path):
            ToastManager().show(
                tr("File not found: {name}").format(name=name or os.path.basename(str(path))), "error")
            return
        display_name = name or os.path.basename(path)
        link = f"cite://view?path={quote(path)}&page=1&name={quote(display_name)}"
        logger.debug("Open attached file from input chip: %s", path)
        self.handle_link_click(link)

    def remove_attached_file(self, info):
        """从待发送附件中移除指定文档（输入区芯片上的 x 按钮）。"""
        self.external_files = [f for f in getattr(self, 'external_files', []) if f is not info]
        self._refresh_attachment_preview()

    def clear_attached_context(self):
        self.external_files = []
        self.external_context_html = ""
        # hide_context_preview 会一并清空图片 / 文档芯片
        self.input_container.hide_context_preview()

    def export_chat_history(self):
        if not self.history:
            ToastManager().show(tr("There are currently no chat records to export."), "warning")
            self.logger.warning("Attempted to export empty chat history.")
            return

        tm = ThemeManager()
        menu = QMenu(self.widget)
        menu.setStyleSheet(f"""
            QMenu {{ background-color: {tm.color('bg_card')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; border-radius: 6px; padding: 4px;}}
            QMenu::item {{ padding: 6px 20px; border-radius: 4px;}}
            QMenu::item:selected {{ background-color: {tm.color('accent')}; color: #fff; }}
        """)

        act_pdf = menu.addAction(tm.icon("article", "text_main"), tr("Export as PDF"))
        act_md = menu.addAction(tm.icon("markdown", "text_main"), tr("Export as MD"))
        act_txt = menu.addAction(tm.icon("file-text", "text_main"), tr("Export as TXT"))
        menu.addSeparator()
        act_json = menu.addAction(tm.icon("archive", "text_main"),
                                  tr("Export as JSON (Lossless, for re-import)"))

        # 在鼠标位置弹出菜单
        action = menu.exec(QCursor.pos())
        if not action:
            return

        if action == act_pdf:
            filter_str, default_ext = tr("PDF Document (*.pdf)"), ".pdf"
        elif action == act_md:
            filter_str, default_ext = tr("Markdown File (*.md)"), ".md"
        elif action == act_json:
            filter_str, default_ext = tr("Scholar Navis History (*.schat *.json)"), ".schat"
        else:
            filter_str, default_ext = tr("Text File (*.txt)"), ".txt"

        # 弹出系统保存对话框
        path, _ = save_file_name(
            self.widget, tr("Export Log"), f"Scholar_Navis_Log{default_ext}", filter_str
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
        self.export_pd = ProgressDialog(self.widget, tr("Exporting Chat"),
                                        tr("Processing file in background..."))
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
            mono_font_family=mono_font_family_css(),
            user_icon=user_icon_b64,
            ai_icon=ai_icon_b64
        )

    def _on_export_state_changed(self, state, msg):
        from src.core.core_task import TaskState
        if state == TaskState.FAILED.value:
            self.export_pd.show_finish_state(False, tr("Export Failed"), str(msg))

    def _on_export_result(self, result):
        if result and result.get("success"):
            self.export_pd.show_finish_state(
                True, tr("Export Complete"),
                tr("Saved to {name}").format(name=os.path.basename(result.get('path', ''))))
            ToastManager().show(tr("Document successfully exported."), "success")
            self.logger.info(f"Chat history successfully exported to: {result.get('path')}")
        else:
            self.export_pd.show_finish_state(
                False, tr("Export Failed"),
                result.get("msg", tr("Unknown error")) if result else tr("Unknown error"))
            self.logger.error(f"Failed to export document: {result.get('msg') if result else 'None'}")

    # ---------- Import ----------
    def import_chat_history(self):
        """选择并导入一份聊天记录（.schat/.json 无损 或 .md/.txt 尽力还原）。"""
        from src.task.chat_tasks import ImportChatHistoryTask
        from src.ui.components.dialog import ProgressDialog

        # 空历史也允许导入（直接填充），因此不做前置判空
        path, _ = open_file_name(
            self.widget, tr("Import Chat History"), "",
            tr("Chat History (*.schat *.json *.md *.txt *.csv);;"
               "Scholar Navis Lossless (*.schat *.json);;"
               "Markdown (*.md);;Text (*.txt);;CSV (*.csv)")
        )
        if not path:
            return

        pd = ProgressDialog(self.widget, tr("Importing Chat"),
                            tr("Reading and parsing chat history..."))
        pd.show()

        self.import_task_mgr = TaskManager()
        self.import_task_mgr.sig_result.connect(
            lambda res: self._on_import_history_result(res, path, pd))
        self.import_task_mgr.start_task(
            ImportChatHistoryTask,
            task_id="import_chat_history",
            mode=TaskMode.THREAD,
            path=path,
        )

    def _on_import_history_result(self, result, path, pd):
        if not result or not result.get("success"):
            msg = result.get("msg", "Unknown error") if result else "Unknown error"
            pd.show_finish_state(False, tr("Import Failed"), msg)
            self.logger.error("Chat history import failed: %s", msg)
            return

        messages = result.get("messages", [])
        lossless = result.get("lossless", False)
        if not messages:
            pd.show_finish_state(False, tr("Import Failed"),
                                 tr("No chat messages were found in the file."))
            return

        # 确认是否用导入内容替换当前对话上下文
        from src.ui.components.dialog import StandardDialog
        fmt_note = tr("Lossless (full fidelity).") if lossless else \
            tr("Best-effort text import (rich content such as citations/images may be reduced to plain text).")
        dlg = StandardDialog(
            self.widget,
            tr("Import Chat History"),
            tr("Found {n} message(s).\n{note}\n\n"
               "This will replace the current conversation. Continue?").format(
                n=len(messages), note=fmt_note),
            show_cancel=True,
        )
        if not dlg.exec():
            pd.close_safe()
            return

        try:
            self._apply_imported_history(messages)
            pd.show_finish_state(
                True, tr("Import Complete"),
                tr("Imported {n} message(s) from:\n{name}").format(
                    n=len(messages), name=os.path.basename(path)))
            self.logger.info("Imported %d chat message(s) from %s (lossless=%s)",
                             len(messages), path, lossless)
        except Exception as e:
            self.logger.exception("Failed to render imported history.")
            pd.show_finish_state(False, tr("Import Error"),
                                 tr("Failed to render chat history:\n{err}").format(err=e))

    def _apply_imported_history(self, messages):
        """将导入的消息替换进对话：重建气泡并写回 self.history。

        复用编辑重发（edit-resend）同款重放逻辑，保证渲染行为与现有对话一致，
        导入后的记录可继续作为上下文追问。
        """
        self._ensure_chat_ui()

        # 终止可能在进行的生成，清空旧会话
        if hasattr(self, 'cancel_generation'):
            self.cancel_generation()
        self.current_ai_bubble = None
        self.is_locked = False
        self.clear_layout(self.chat_layout)
        self.remove_old_follow_ups()

        # 编号空间随会话重建而重置：先清空引用缓存，再按新气泡编号逐条回灌，
        # 避免旧会话残留条目串到导入结果的正文 [n] 上。
        from src.ui.components.citation_popup import CitationPopupController
        CitationPopupController.instance().clear_store()

        # 逐条重放并同时写入 self.history：add_bubble 依赖 len(history) 生成自增
        # 气泡索引，故必须与历史写入交替进行（与 edit-resend 重放逻辑一致）。
        self.history = []
        for msg in messages:
            role = msg.get("role", "assistant")
            is_user = role == "user"
            content = msg.get("content", "") or ""
            display_text = msg.get("display_text", content) if is_user else content
            ctx_html = msg.get("context_html")
            msg_images = [c for c in msg.get("external_files", []) if c.get("type") == "image"] \
                if msg.get("external_files") else []
            bubble = self.add_bubble(display_text, is_user=is_user, context_html=ctx_html,
                                     image_files=msg_images)
            # 无损导入的引用条目按新气泡编号回灌，恢复 [n] 的悬停著录与支撑原文。
            self._restore_message_references(bubble, msg)
            if not is_user and bubble is not None:
                # AI 消息必须走与正常生成流一致的渲染管线（_format_response）：
                # 无损导出的 content 是原始 markdown，含 <think>/<mcp_process>
                # 思考块、[FINAL_ANSWER] 标记与 mermaid 代码块。若直接
                # set_content（仅 markdown_to_html），未闭合标签会被透传成
                # 一坨原始文本，导致导入后排版混乱。此处理会生成 think 折叠
                # 面板 / mermaid 卡片，并缓存 mermaid 源码供点击查看。
                try:
                    final_html = self._format_response(content, getattr(bubble, "index", -1))
                    if final_html:
                        bubble.set_content(final_html)
                except Exception as e:
                    logger.warning("Failed to render imported AI message with format_response: %s", e)
            self.history.append(msg)

        # 恢复输入控件状态
        if hasattr(self, 'input_container'):
            self.input_container.unlock_input()
            self.input_container.clear_text()
        self.clear_attached_context()
        self.scroll_to_bottom(smooth=True)
        ToastManager().show(tr("Imported {n} message(s).").format(n=len(messages)), "success")
