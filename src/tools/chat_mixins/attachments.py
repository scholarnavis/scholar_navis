"""Attachment mixin: file attach, KB file pick, conversation archive export & import.

拆分自 src/tools/chat_tool.py：负责外部附件管理、对话归档导入 / 导出，以及
"哪些文件被内嵌、哪些只剩文件名"的界面说明。
"""
import logging
import os
import time
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
        """导出对话：当前对话（单归档）或全部对话（多归档）。

        导出格式只有 ``.schat`` 一种（SQLite 容器）：正文、思考链、参考文献、
        书签、标签、组件状态与小体积附件都在同一个文件里，导入后可继续对话。
        PDF/MD/TXT 已移除。
        """
        has_current = bool(self.history)
        tm = ThemeManager()
        menu = QMenu(self.widget)
        menu.setStyleSheet(f"""
            QMenu {{ background-color: {tm.color('bg_card')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; border-radius: 6px; padding: 4px;}}
            QMenu::item {{ padding: 6px 20px; border-radius: 4px;}}
            QMenu::item:selected {{ background-color: {tm.color('accent')}; color: #fff; }}
        """)

        act_current = menu.addAction(tm.icon("archive", "text_main"),
                                     tr("Export Current Conversation"))
        act_current.setEnabled(has_current)
        act_all = menu.addAction(tm.icon("archive", "text_main"),
                                 tr("Export All Conversations"))

        action = menu.exec(QCursor.pos())
        if not action:
            return
        if action == act_all:
            self.export_all_conversations()
            return
        if not has_current:
            ToastManager().show(tr("There are currently no chat records to export."), "warning")
            return

        path, _ = save_file_name(
            self.widget, tr("Export Current Conversation"),
            "Scholar_Navis_Conversation.schat",
            tr("Scholar Navis Conversation (*.schat)"))
        if not path:
            return
        # 归档是单一容器文件，扩展名统一为 .schat（用户可能手输 .json / .db）
        if not path.lower().endswith(".schat"):
            path = os.path.splitext(path)[0] + ".schat"

        self._start_export(path, tabs=[{
            "name": self._current_conversation_name(),
            "messages": self.history,
        }])

    def export_all_conversations(self):
        """导出全部对话到一个归档文件（内容与"逐个导出"等价、可再导入）。

        消息来源由容器给出（见 :meth:`ChatTool.collect_tabs_payload`）：已打开过的
        对话取内存里的历史，未打开过的直接读磁盘快照，因此"导出全部"不需要先把
        所有对话都渲染一遍。
        """
        container = getattr(self, 'tabs_container', None)
        tabs = container.collect_tabs_payload() if container is not None else []
        if not any(tab.get("messages") for tab in tabs):
            ToastManager().show(tr("There are currently no chat records to export."), "warning")
            self.logger.warning("Attempted to export all conversations while every tab is empty.")
            return

        path, _ = save_file_name(
            self.widget, tr("Export All Conversations"),
            "Scholar_Navis_All_Conversations.schat",
            tr("Scholar Navis Conversation (*.schat)"))
        if not path:
            return
        if not path.lower().endswith(".schat"):
            path = os.path.splitext(path)[0] + ".schat"

        self._start_export(path, tabs=tabs)

    def _current_conversation_name(self):
        """当前标签页的显示名（用于归档里的对话名）。"""
        container = getattr(self, "tabs_container", None)
        getter = getattr(container, "tab_name", None)
        if callable(getter):
            return getter(self._tab_id())
        return None

    def _start_export(self, path, tabs=None):
        """启动后台导出任务并挂上进度弹窗（当前对话 / 全部对话共用同一条链路）。

        :param tabs: 传入时按"多对话归档"导出；``None`` 表示导出当前对话的
            ``self.history``。
        """
        # 导出前把气泡里已缓存的远程图片同步回历史，否则归档会漏掉这些图片
        sync = getattr(self, "_sync_all_bubble_remote_images", None)
        if callable(sync):
            sync()

        from src.ui.components.dialog import ProgressDialog
        self.export_pd = ProgressDialog(self.widget, tr("Exporting Conversation"),
                                        tr("Packing conversation and embedded files..."))
        self.export_pd.show()

        self.export_task_mgr = TaskManager()
        self.export_task_mgr.sig_progress.connect(self.export_pd.update_progress)
        self.export_task_mgr.sig_state_changed.connect(self._on_export_state_changed)
        self.export_task_mgr.sig_result.connect(self._on_export_result)

        from src.task.chat_tasks import ExportChatTask
        from src.core.chat_archive import normalize_tabs
        export_tabs = tabs if tabs is not None else [{
            "name": self._current_conversation_name(),
            "messages": self.history,
        }]
        # 在 UI 线程先做一次 JSON 化快照：后台任务不再直接读正在被编辑/流式追加
        # 的 history 字典，避免跨线程读写同一对象。
        export_tabs = normalize_tabs(export_tabs)
        self.export_task_mgr.start_task(
            ExportChatTask,
            task_id="export_chat",
            mode=TaskMode.THREAD,
            path=path,
            tabs=export_tabs,
        )

    def _on_export_state_changed(self, state, msg):
        from src.core.core_task import TaskState
        if state == TaskState.FAILED.value:
            self.export_pd.show_finish_state(False, tr("Export Failed"), str(msg))

    def _on_export_result(self, result):
        if result and result.get("success"):
            detail = tr("Saved to {name}").format(name=os.path.basename(result.get('path', '')))
            stats = result.get("stats") or {}
            embedded = int(stats.get("attachments") or 0)
            placeholders = int(stats.get("placeholders") or 0)
            if embedded:
                detail += "\n" + tr("{n} file(s) embedded into the archive.").format(n=embedded)
            if placeholders:
                detail += "\n" + tr("{n} file(s) kept as name placeholders (too large or unavailable).").format(
                    n=placeholders)
            self.export_pd.show_finish_state(True, tr("Export Complete"), detail)
            ToastManager().show(tr("Conversation exported."), "success")
            self.logger.info("Conversation archive written: %s (assets=%s, placeholders=%s)",
                             result.get("path"), embedded, placeholders)
        else:
            self.export_pd.show_finish_state(
                False, tr("Export Failed"),
                result.get("msg", tr("Unknown error")) if result else tr("Unknown error"))
            self.logger.error(f"Failed to export conversation: {result.get('msg') if result else 'None'}")

    # ---------- Import ----------
    def import_chat_history(self):
        """选择并导入对话归档（``.schat`` / ``.sqlite3`` / ``.db`` / ``.json``）。

        归档可含一个或多个对话：单对话归档替换当前对话，多对话归档则新建对应
        数量的标签页（见 :meth:`ChatTool.import_tabs`）。旧版 ZIP / 纯 JSON 归档
        仍可导入。PDF/MD/TXT 已移除。
        """
        from src.task.chat_tasks import ImportChatHistoryTask
        from src.ui.components.dialog import ProgressDialog

        # 空历史也允许导入（直接填充），因此不做前置判空
        path, _ = open_file_name(
            self.widget, tr("Import Conversations"), "",
            tr("Scholar Navis Conversation (*.schat *.sqlite3 *.db *.json)"))
        if not path:
            return

        pd = ProgressDialog(self.widget, tr("Importing Conversations"),
                            tr("Reading conversation archive and extracting files..."))
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
            self.logger.error("Conversation import failed: %s", msg)
            return

        # 兼容只返回 messages 的旧口径：统一收敛成"对话列表"再往下走。
        tabs = result.get("tabs")
        if not tabs:
            tabs = [{"name": None, "messages": result.get("messages", [])}]
        tabs = [t for t in tabs if isinstance(t, dict) and t.get("messages")]
        if not tabs:
            pd.show_finish_state(False, tr("Import Failed"),
                                 tr("No chat messages were found in the file."))
            return

        placeholders = int(result.get("placeholders") or 0)
        note = tr("Lossless (full fidelity).")
        if placeholders:
            note += "\n" + tr("{n} file(s) were not embedded (too large or unavailable); "
                              "their file names are kept.").format(n=placeholders)

        from src.ui.components.dialog import StandardDialog
        if len(tabs) > 1:
            self._import_into_new_tabs(tabs, path, pd, note, placeholders)
            return

        messages = tabs[0]["messages"]
        # 确认是否用导入内容替换当前对话上下文
        dlg = StandardDialog(
            self.widget,
            tr("Import Conversations"),
            tr("Found {n} message(s).\n{note}\n\n"
               "This will replace the current conversation. Continue?").format(
                n=len(messages), note=note),
            show_cancel=True,
        )
        if not dlg.exec():
            pd.close_safe()
            return

        # 归档里带了有意义的对话名时，一并恢复到当前标签页；默认名不覆盖
        # 用户现有的命名（否则一次导入会把自定义名字冲掉）。
        from src.core.chat_archive import DEFAULT_TAB_NAME
        imported_name = str(tabs[0].get("name") or "")
        container = getattr(self, "tabs_container", None)
        rename = getattr(container, "rename_tab", None)
        if imported_name and imported_name != DEFAULT_TAB_NAME and callable(rename):
            rename(self._tab_id(), imported_name)
        # 标签也随归档恢复：导入进来的是一段对话，它的标签同样属于这段内容。
        imported_tags = [t for t in (tabs[0].get("tags") or []) if isinstance(t, str)]
        if imported_tags:
            from src.core import chat_session
            chat_session.save_tab_tags(self._tab_id(), imported_tags)

        # 重建阶段（逐条渲染气泡）比解析慢得多，且全程在 UI 线程：这里改用异步
        # 分片推进（见 _apply_imported_history），进度对话框在这期间才能继续滚动
        # 动画、主窗口才能重绘。收尾提示因此挪到完成回调里。
        pd.update_progress(-1, tr("Rendering {n} message(s)...").format(n=len(messages)))

        def _on_rebuilt(success):
            if success:
                detail = tr("Imported {n} message(s) from:\n{name}").format(
                    n=len(messages), name=os.path.basename(path))
                if placeholders:
                    detail += "\n" + tr("{n} file(s) were not embedded; their names are kept.").format(
                        n=placeholders)
                pd.show_finish_state(True, tr("Import Complete"), detail)
                self.logger.info("Imported %d message(s) from %s (placeholders=%d)",
                                 len(messages), path, placeholders)
            else:
                pd.show_finish_state(False, tr("Import Error"),
                                     tr("Some messages could not be rendered."))
                self.logger.error("Imported %d message(s) from %s, "
                                  "but some messages failed to render.", len(messages), path)

        try:
            self._apply_imported_history(messages, on_done=_on_rebuilt)
        except Exception as e:
            self.logger.exception("Failed to render imported history.")
            pd.show_finish_state(False, tr("Import Error"),
                                 tr("Failed to render chat history:\n{err}").format(err=e))

    def _import_into_new_tabs(self, tabs, path, pd, note, placeholders=0):
        """多对话归档：确认后交给容器新建标签页（不覆盖任何现有对话）。"""
        from src.ui.components.dialog import StandardDialog

        total = sum(len(t["messages"]) for t in tabs)
        dlg = StandardDialog(
            self.widget,
            tr("Import Conversations"),
            tr("Found {tabs} conversation(s) with {n} message(s).\n{note}\n\n"
               "They will be opened as new conversations. Continue?").format(
                tabs=len(tabs), n=total, note=note),
            show_cancel=True,
        )
        if not dlg.exec():
            pd.close_safe()
            return

        container = getattr(self, 'tabs_container', None)
        if container is None:
            pd.show_finish_state(False, tr("Import Error"),
                                 tr("No chat tab is available to receive the imported conversations."))
            self.logger.error("Multi-conversation import failed: chat tab container is unavailable.")
            return

        pd.update_progress(-1, tr("Opening {tabs} conversation(s)...").format(tabs=len(tabs)))
        try:
            created = container.import_tabs(tabs)
        except Exception as e:
            self.logger.exception("Failed to open imported conversations as tabs.")
            pd.show_finish_state(False, tr("Import Error"),
                                 tr("Failed to render chat history:\n{err}").format(err=e))
            return

        if not created:
            pd.show_finish_state(False, tr("Import Failed"),
                                 tr("No chat messages were found in the file."))
            return
        detail = tr("Imported {tabs} conversation(s), {n} message(s) from:\n{name}").format(
            tabs=created, n=total, name=os.path.basename(path))
        if placeholders:
            detail += "\n" + tr("{n} file(s) were not embedded; their names are kept.").format(
                n=placeholders)
        pd.show_finish_state(True, tr("Import Complete"), detail)
        self.logger.info("Imported %d conversation(s), %d message(s) from %s.",
                         created, total, path)

    def _restore_message_remote_images(self, bubble, msg):
        """把历史里记录的"远程图片 -> 本地缓存"映射注入气泡，供正文渲染使用。"""
        remote = msg.get("remote_images")
        if isinstance(remote, dict):
            for url, path in remote.items():
                if (isinstance(url, str) and isinstance(path, str)
                        and os.path.isfile(path)):
                    bubble.downloaded_images[url] = path
        bubble.set_remote_image_placeholders(msg.get("remote_image_placeholders") or {})

    def _sync_all_bubble_remote_images(self) -> int:
        """把气泡里已下载的远程图片映射回写历史，供快照与归档内嵌。

        调用点是每次落盘前的"对账"：图片可能在本轮回答已经写入历史之后才下载
        完成，只看历史会漏掉它们。只同步 http(s) 且文件确实存在的条目。
        """
        layout = getattr(self, "chat_layout", None)
        history = getattr(self, "history", None) or []
        if layout is None:
            return 0
        changed = 0
        for i in range(layout.count()):
            item = layout.itemAt(i)
            bubble = item.widget() if item is not None else None
            idx = getattr(bubble, "index", -1)
            if not (0 <= idx < len(history)):
                continue
            remote = getattr(bubble, "downloaded_images", None)
            if not isinstance(remote, dict) or not remote:
                continue
            entry = history[idx]
            current = entry.get("remote_images")
            if not isinstance(current, dict):
                current = {}
                entry["remote_images"] = current
            for url, path in remote.items():
                if not (isinstance(url, str)
                        and url.startswith(("http://", "https://"))
                        and isinstance(path, str)
                        and os.path.isfile(path)):
                    continue
                if current.get(url) != path:
                    current[url] = path
                    changed += 1
        if changed:
            logger.debug("Synced %d remote image cache path(s) into history.", changed)
        return changed

    #: 历史回灌的分片时间预算（毫秒）：每片最多占用 UI 线程这么久，随后让出事件
    #: 循环一拍。取值低于一帧（约 16ms），保证加载动画持续有节拍可动、窗口不会被
    #: 桌面环境判定为"无响应"；又不至于小到让分片调度本身成为瓶颈。
    _HISTORY_REBUILD_SLICE_MS = 8

    def _apply_imported_history(self, messages, announce=True, on_done=None, progress_text=None):
        """将导入的消息替换进对话：重建气泡并写回 self.history。

        复用编辑重发（edit-resend）同款重放逻辑，保证渲染行为与现有对话一致，
        导入后的记录可继续作为上下文追问。

        **分片推进**：单条气泡的重建（``ChatBubbleWidget`` 构造 +
        ``_format_response`` + ``set_content``）耗时随正文长度线性增长且无法预估，
        整段同步跑在 UI 线程会让窗口在这期间完全冻结（大历史可达数秒，桌面环境会
        给出"无响应"）。因此本方法只做同步前置（终止生成、清空旧会话、复位编号
        空间），逐条重建交给 :meth:`_rebuild_history_step` 按
        :attr:`_HISTORY_REBUILD_SLICE_MS` 的时间预算推进，片间让出事件循环：加载
        动画能动、窗口可重绘。

        代价是**本方法返回时重建尚未结束**：需要收尾动作的调用方用 ``on_done``
        传回调，不要依赖返回后的状态。

        分片的粒度是**小步**（见 :meth:`_rebuild_history_step`）：预算在一条消息内部的
        各个慢步骤之间同样生效，而不是只在消息与消息之间。

        :param announce: 完成后是否弹"已导入 N 条"提示。启动时恢复上次对话走的是同一
            套重放逻辑，但那不是用户发起的导入动作，故传 ``False`` 静默回灌。
        :param on_done: 重建结束后的回调，签名 ``(success: bool) -> None``。单条消息
            渲染失败不会中断整轮重建，只会让 ``success`` 为 ``False``。
        :param progress_text: 非 ``None`` 时在对话区盖一层加载遮罩并显示该文案
            （见 :meth:`ChatTool._show_chat_loading`）；``None`` 表示不加遮罩，供已有
            进度反馈的调用方（如导入流程的 ``ProgressDialog``）避免重复提示。
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
        self.history = []

        # 重放期间抑制气泡自带的滚动定位（add_bubble 挂的 50ms 单次定时器）：分片
        # 推进跨多个事件循环轮次，那些定时器会在重建中途触发，把视图拽来拽去。视图
        # 定位统一留到重建结束后做一次，与 edit-resend 重放同一口径（见 send_flow）。
        self._is_editing = True

        state = {"messages": list(messages), "cursor": 0, "job": None,
                 "announce": announce, "on_done": on_done, "failed": False,
                 "progress_text": progress_text, "timings": {}, "max_step": 0.0,
                 "started": time.perf_counter()}
        # 上一轮未走完的分片链靠"状态对象是否仍是当前值"自行终止（见 _rebuild_history_step）
        self._history_rebuild_state = state

        # 遮罩只是装饰性反馈：创建/显示失败不能拖住重建，因此单独兜一层。
        try:
            if progress_text is not None:
                self._show_chat_loading(progress_text)
        except Exception as e:
            logger.warning("Failed to show chat loading overlay: %s", e)

        QTimer.singleShot(0, lambda s=state: self._rebuild_history_step(s))

    def _rebuild_history_step(self, state):
        """分片重建一步：在时间预算内推进尽可能多的小步，然后让出事件循环。

        粒度是**小步**而不是整条消息。单条消息内部各步耗时差着几个数量级——构造气泡、
        渲染正文（markdown → HTML）、把正文交给 QTextBrowser 重排，任何一步都可能独占
        UI 线程数百毫秒。预算只在消息之间检查的话，遇到那样一条长回答时加载动画照样
        停住（实测 44 条历史的回灌里就是这种分布）；按小步检查，一次让出前的占用被压
        到"最慢的单个 Qt 调用"。

        顺带记录主线程的真实占用（总忙时 + 最长单步）：这是"动画停住"唯一能定量的
        证据，收尾时汇总成一行日志（见 :meth:`_finish_history_rebuild`）。
        """
        if self._history_rebuild_state is not state:
            # 已被更新的回灌接管（或被取消）：本分片链自然终止，不再触碰共享状态
            logger.debug("History rebuild superseded; stopping this shard chain.")
            return

        messages = state["messages"]
        deadline = time.perf_counter() + self._HISTORY_REBUILD_SLICE_MS / 1000.0
        while time.perf_counter() < deadline:
            job = state["job"]
            if job is None:
                if state["cursor"] >= len(messages):
                    self._finish_history_rebuild(state)
                    return
                state["cursor"] += 1
                state["timings"][state["cursor"] - 1] = 0.0
                state["job"] = self._rebuild_history_message(messages[state["cursor"] - 1])
                job = state["job"]
                self._report_rebuild_progress(state)

            started = time.perf_counter()
            try:
                job.send(None)
            except StopIteration as done:
                # 该条消息的小步已跑完：生成器的返回值就是"是否完整渲染"
                if not done.value:
                    state["failed"] = True
                state["job"] = None
            except Exception:
                state["failed"] = True
                state["job"] = None
                logger.exception("Failed to rebuild chat message #%d while restoring history.",
                                 state["cursor"] - 1)

            step = time.perf_counter() - started
            index = state["cursor"] - 1
            state["timings"][index] = state["timings"].get(index, 0.0) + step
            if step > state["max_step"]:
                state["max_step"] = step

        QTimer.singleShot(0, lambda s=state: self._rebuild_history_step(s))

    def _rebuild_history_message(self, msg):
        """重建单条历史消息；**生成器**，每个 ``yield`` 是一个让出点。

        让出点供 :meth:`_rebuild_history_step` 在步与步之间检查时间预算，因此这里刻意
        把三处"可能很慢"的活儿分开：构造气泡 / 回灌引用与卡片 / 渲染并写入正文。

        AI 气泡先建**空壳**再写入最终 HTML：``add_bubble`` 会把传入文本先渲染一遍
        （``ChatBubbleWidget.__init__`` → ``set_content``），而紧接着这里还要用
        ``_format_response`` 的结果再写一次——对一条长回答等于白渲染一遍，是回灌耗时的
        主要来源之一。流式生成走的正是"空壳 + 后续 set_content"，与线上完全一致。

        索引与历史必须交替写入：``add_bubble`` 用 ``len(self.history)`` 生成自增气泡
        编号，两者必须同步前进，否则后续气泡编号整体错位（与 edit-resend 重放一致）。

        :return: 该条是否**完整**渲染。AI 正文渲染失败时回落到"直接写原始文本"（宁可
            排版差，也不要一个空气泡），并把 ``False`` 交回调用方，汇总成"恢复/导入
            不完整"的提示——静默降级会让用户以为对话本身就只有这些内容。
        """
        role = msg.get("role", "assistant")
        is_user = role == "user"
        content = msg.get("content", "") or ""
        display_text = msg.get("display_text", content) if is_user else ""
        ctx_html = msg.get("context_html")
        msg_images = [c for c in msg.get("external_files", []) if c.get("type") == "image"] \
            if msg.get("external_files") else []
        # 非图片附件若未能内嵌（过大 / 源文件缺失 / 旧版本），在气泡里列出
        # "文件名 + 原因"；图片由缩略图自己的占位块承担，避免重复。
        placeholder_files = [
            c for c in (msg.get("external_files") or [])
            if isinstance(c, dict) and c.get("type") != "image"
            and isinstance(c.get("asset_placeholder"), dict)
        ]
        bubble = self.add_bubble(display_text, is_user=is_user, context_html=ctx_html,
                                 image_files=msg_images,
                                 placeholder_files=placeholder_files)
        if bubble is not None:
            # 远程图片的本地缓存映射必须在正文渲染前注入，否则 set_content 仍会
            # 把正文里的 URL 当成"未下载"并重新联网。图片文件缺失时这里不会注入，
            # 正文会按占位说明展示或尝试重新下载。
            self._restore_message_remote_images(bubble, msg)

        rendered = True
        try:
            yield

            # 无损导入的引用条目按新气泡编号回灌，恢复 [n] 的悬停著录与支撑原文。
            # （引用与组件不同：它只与"气泡编号"相关，不依赖正文是否渲染。）
            self._restore_message_references(bubble, msg)
            # token 用量是本轮回答的属性，随条目落盘；重建时一并还原，否则
            # 重启 / 导入之后计数器会整片消失。
            self._restore_message_usage(bubble, msg)
            yield

            if not is_user and bubble is not None:
                # AI 消息必须走与正常生成流一致的渲染管线（_format_response）：
                # 无损导出的 content 是原始 markdown，含 <think>/<mcp_process> 思考块、
                # [FINAL_ANSWER] 标记与 mermaid 代码块。若直接 set_content（仅
                # markdown_to_html），未闭合标签会被透传成一坨原始文本，导致导入后排版
                # 混乱。此处理会生成 think 折叠面板 / mermaid 卡片，并缓存 mermaid
                # 源码供点击查看。
                final_html = ""
                try:
                    # 只有带引用列表的历史消息才显示 [n]：旧记录里可能残留编号却
                    # 没有对应条目（早期导出丢过引用），那种编号点开只会是空卡。
                    final_html = self._format_response(
                        content, getattr(bubble, "index", -1),
                        hide_citations=self._citations_hidden_for(msg))
                except Exception as e:
                    rendered = False
                    logger.warning("Failed to render imported AI message with "
                                   "format_response: %s", e)
                yield
                # 正文写入即"组件由标记重建完毕"的时刻（见 ChatBubbleWidget.set_content）。
                bubble.set_content(final_html or content)
                yield

            # 书签锚点依赖正文已经写成最终 HTML（用户消息走 add_bubble 时也已渲染完毕），
            # 因此放在这里回灌；它不依赖卡片，先于卡片恢复即可。
            self._restore_message_bookmarks(bubble, msg)
            yield

            # 组件的用户交互结果必须在**卡片已经从正文标记重建出来之后**再回灌：
            # restore_card_states 是"按 (kind, key) 匹配气泡里已有的卡片"，先于建卡
            # 调用只会空转，随后落盘对账还会拿默认状态覆盖掉用户当初的真实作答。
            # 编辑重发（send_flow）走的正是"先渲染、后回灌"的顺序，此处与之对齐。
            self._restore_message_cards(bubble, msg)
        except Exception as e:
            rendered = False
            logger.warning("Failed to rebuild imported message #%d: %s",
                           getattr(bubble, "index", -1), e)

        # 写在 try 之外、刻意不用 finally：气泡一旦建出来就必须与 history 同步前进，
        # 而 finally 在生成器被中途丢弃时会推迟到对象回收时刻才执行——那已是无法预期的
        # 时机，可能落到重建收尾之后。
        self.history.append(msg)
        return rendered

    def _report_rebuild_progress(self, state):
        """把"已重建 N/M 条"写进加载遮罩。

        动画有节拍还不够：整轮要几秒时，用户需要看到"它在往前走"。进度数字每开始重建
        一条就变一次，即便某一小步偶尔偏慢，也不会被读成"卡死"。
        """
        base = state.get("progress_text")
        if not base:
            return
        try:
            self._show_chat_loading(f"{base} {state['cursor']}/{len(state['messages'])}")
        except Exception as e:
            logger.debug("Progress overlay update skipped: %s", e)

    def _finish_history_rebuild(self, state):
        """重建收尾：复位交互状态、收起遮罩、恢复输入与视图，最后回调调用方。

        顺序有意如此：先把 ``_history_rebuild_state`` / ``_is_editing`` 复位，再收起
        遮罩，最后才执行 ``on_done``——回调可能弹阻塞式结果对话框（导入流程即如此），
        此刻内部状态必须已经一致，不能让对话框活在一次"重建未完成"的状态里。
        """
        timings = state.get("timings") or {}
        slowest = sorted(timings.items(), key=lambda kv: -kv[1])[:3]
        # 一行定量的回灌账本：整轮墙钟耗时 vs 主线程真实忙时，以及最长的一次独占。
        # "加载动画卡住"= 主线程被某一步独占太久，只看总耗时分不清是"步多"还是"步慢"，
        # 因此这两项必须一起记（慢的几条也列出来，便于按内容定位）。
        logger.info(
            "History rebuild: %d message(s) in %.2fs (main-thread busy %.2fs, "
            "longest single step %.0fms); slowest: %s",
            len(state.get("messages") or []), time.perf_counter() - state["started"],
            sum(timings.values()), state["max_step"] * 1000.0,
            ", ".join(f"#{i} {d * 1000.0:.0f}ms" for i, d in slowest) or "n/a")

        self._history_rebuild_state = None
        self._is_editing = False
        self._hide_chat_loading()
        # 会话恢复/导入完成：整段历史都是"已结束"的轮次，最后一条问题该给出重新发起入口
        self._sync_regenerate_available()

        # 恢复输入控件状态
        if hasattr(self, 'input_container'):
            self.input_container.unlock_input()
            self.input_container.clear_text()
        self.clear_attached_context()
        self.scroll_to_bottom(smooth=True)

        if state["announce"]:
            ToastManager().show(tr("Imported {n} message(s).").format(n=len(state["messages"])), "success")

        callback = state["on_done"]
        if callback is not None:
            callback(not state["failed"])
