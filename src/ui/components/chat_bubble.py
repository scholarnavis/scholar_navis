import hashlib
import logging
import os
import re
import sys
import tempfile
import time

logger = logging.getLogger(__name__)

from PySide6.QtCore import Qt, Signal, QEvent, QTimer, QSize
from PySide6.QtGui import (QGuiApplication, QPixmap, QTextBlockFormat,
                           QTextCursor, QFont)
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QTextEdit, QPushButton, QFrame, QSizePolicy, QMenu, QScrollArea, QTextBrowser)

from src.core.core_task import TaskManager, TaskMode
# hex_to_rgba 由核心层统一实现（全应用唯一来源，避免各 UI 模块各自复制）
from src.core.theme_manager import (ThemeManager, hex_to_rgba, overlay_scrollbar_qss,
                                    strong_weight_css)
from src.task.chat_tasks import DownloadImageTask
from src.ui.components.text_formatter import (TextFormatter, qt_font_family_css,
                                              resolve_qt_font_families,
                                              naturalize_table_html)
from src.ui.components.toast import ToastManager
from src.ui.components.image_viewer import open_image_viewer


#: 行距（按字体高度的百分比，100 为单倍行高）。
_LINE_HEIGHT_PERCENT = 150
#: 段落间距（设备逻辑像素，用于拉开段落/列表项间距）。
_BLOCK_BOTTOM_MARGIN = 8.0
_BLOCK_TOP_MARGIN = 4.0

#: 独立滚动块的高度上限（逻辑像素）。取值兼顾「一次能看到足够内容」与
#: 「不让单个块吃掉整屏」：思考链约 20 行、代码块约 24 行、引用约 18 行。
_THINK_MAX_HEIGHT = 340
_CODE_MAX_HEIGHT = 420
_QUOTE_MAX_HEIGHT = 300

#: 正文浏览器高度相对文档高度的额外余量（px）。旧实现固定 +15 并叠加"预测会出现
#: 横向滚动条"的预留，气泡会比内容高出一行以上且不回落。
_BROWSER_HEIGHT_SLACK = 6
#: 滚动条与内容之间的呼吸间距（避免滚动条紧贴表格边框/代码底色）。
_BLOCK_SCROLL_GAP = 8

#: 等待回复（尚无正文）时的动态提示词。任务下发到首个 token 之间可能间隔数秒
#: 至数十秒（模型加载、翻译、知识库检索、连接 provider 等），一直显示静态文案
#: 会让用户误以为卡死。这里轮换展示并叠加跳动圆点，持续表达"仍在推进"。
_LOADING_PHRASES = (
    "Thinking",
    "Preparing context",
    "Consulting sources",
    "Analyzing the question",
    "Working through it",
    "Almost there",
)
#: 单个提示词的展示时长（单位：动画 tick；tick = 500ms）。
_LOADING_PHRASE_TICKS = 6


#: Qt 族名解析已统一由 text_formatter.resolve_qt_font_families 提供（含缓存），
#: 本模块直接复用，保证 HTML 内联样式与文档默认字体来源一致。


class ImageAwareTextBrowser(QTextBrowser):
    """支持双击激活内联图片的 QTextBrowser。

    QTextBrowser 默认把图片当作不可交互的富文本元素；本子类在双击
    时探测光标下是否为图片，并发出 ``sig_image_activated``（携带本地
    路径），供外层打开内部查看器。
    """
    sig_image_activated = Signal(str)

    def mouseDoubleClickEvent(self, event):
        cursor = self.cursorForPosition(event.pos())
        fmt = cursor.charFormat()
        if fmt.isImageFormat():
            img_fmt = fmt.toImageFormat()
            src = img_fmt.name() or ""
            local_path = self._resolve_local_path(src)
            if local_path and os.path.exists(local_path):
                self.sig_image_activated.emit(local_path)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    @staticmethod
    def _resolve_local_path(src: str) -> str:
        """把文档内图片的 src（file:// URI 或绝对路径）解析为本地路径。"""
        if not src:
            return ""
        if src.startswith("file://"):
            from urllib.parse import urlparse, unquote
            try:
                path = unquote(urlparse(src).path)
            except ValueError:
                return ""
            if sys.platform == "win32" and path.startswith("/"):
                path = path.lstrip("/")
            return path
        if src.startswith("data:image"):
            # data URL 在渲染阶段已被替换为本地临时文件路径，这里兜底还原
            try:
                import base64
                import hashlib
                import tempfile
                header, encoded = src.split(",", 1)
                ext = header.split(";")[0].split("/")[1] if "/" in header else "png"
                img_data = base64.b64decode(encoded)
                local_path = os.path.join(
                    tempfile.gettempdir(), f"navis_base64_{hashlib.md5(img_data).hexdigest()[:12]}.{ext}")
                if not os.path.exists(local_path):
                    with open(local_path, "wb") as f:
                        f.write(img_data)
                return local_path
            except (ValueError, OSError):
                return ""
        return src if os.path.isabs(src) else ""


class OverflowBlock(QScrollArea):
    """为「超宽 / 超高」的富文本块提供独立滚动条。

    Qt 富文本引擎无法在同一个文档内为单个元素（表格、引用、代码块、思考
    链）单独加滚动条，因此 :meth:`ChatBubbleWidget._render_blocks` 会先把
    这些块从正文文档中拆出来，再各自放进本容器：

    * ``wrap=False``（表格 / 代码块）：按内容自然宽度布局（不换行），内容
      宽于容器时出现横向滚动条；
    * ``wrap=True``（引用 / 思考链）：先按容器宽度换行排版，若仍有内容无
      法收纳（超长不可断行片段、内嵌宽表）则退化为自然宽度并横向滚动。

    垂直方向由 ``max_height`` 限制（``None`` 表示不限制），超出时出现纵向
    滚动条；``viewportMargins`` 预留了滚动条与内容之间的呼吸间距，符合常见
    软件 / 网页的视觉习惯。
    """

    def __init__(self, html="", *, block_kind="text", horizontal=True,
                 vertical=True, max_height=None, wrap=False, parent=None):
        super().__init__(parent)
        self._block_kind = block_kind
        self._max_height = max_height
        self._wrap = wrap
        self._horizontal = horizontal
        self._vertical = vertical

        self._inner_html = ""
        self._html_natural = ""
        self._html_fill = ""
        self._applied_html = None
        self._natural_width = None
        self._last_html = None
        self._last_outer_h = -1
        self._syncing = False

        self.setFrameShape(QFrame.NoFrame)
        self.setWidgetResizable(False)
        self.setFocusPolicy(Qt.NoFocus)
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if horizontal else Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if vertical else Qt.ScrollBarAlwaysOff)
        # 右侧留出滚动条间距（横向滚动条在底部，由高度计算单独预留）
        self.setViewportMargins(0, 0, _BLOCK_SCROLL_GAP if horizontal else 0, 0)

        self.browser = ImageAwareTextBrowser()
        self.browser.setFrameShape(QFrame.NoFrame)
        self.browser.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.browser.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.browser.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        doc = self.browser.document()
        doc.setDocumentMargin(0)
        self.setWidget(self.browser)

        if html:
            self.set_content(html)

    # --- 内容与尺寸同步 ---
    def set_content(self, html):
        """更新块内容；内容未变化时直接返回（流式渲染下的高频调用保护）。"""
        if html == self._last_html:
            return
        self._last_html = html
        self._inner_html = html
        if self._block_kind == 'table':
            self._html_natural = naturalize_table_html(html)
            self._html_fill = html
        else:
            self._html_natural = html
            self._html_fill = html
        self._applied_html = None
        self._natural_width = None
        self._resync(force=True)

    def sync_layout(self, force: bool = False):
        """外部布局变化（窗口/气泡宽度改变）后重新测量并收敛高度。

        ``force=True`` 时忽略"高度未变就不写回"的幂等判断，用于流式结束后把高度
        强制收敛到最终内容（中途态可能已把高度写成偏大值）。
        """
        self._resync(force=force)

    def _apply_html(self, html):
        if html != self._applied_html:
            self._applied_html = html
            self.browser.setHtml(html)

    def _set_wrap_mode(self, wrap):
        """切换浏览器换行模式。

        ``NoWrap`` 是获得横向滚动的前提：QTextBrowser 默认按视口宽度换行
        并同步文档宽度，会彻底吃掉"内容比容器宽"的信息。
        """
        mode = QTextEdit.WidgetWidth if wrap else QTextEdit.NoWrap
        if self.browser.lineWrapMode() != mode:
            self.browser.setLineWrapMode(mode)

    def _resync(self, force=False):
        if self._syncing or not self._inner_html:
            return
        self._syncing = True
        try:
            vw = self.viewport().width()
            if vw < 20:  # 尚未完成首次布局，避免按错误宽度排版
                return
            doc = self.browser.document()

            if self._block_kind == 'table':
                # 表格：先量一次自然宽度，能放下就撑满容器（width:100% 换行
                # 排版），放不下则按自然宽度布局并交给横向滚动条。
                if self._natural_width is None:
                    self._set_wrap_mode(False)
                    self._apply_html(self._html_natural)
                    doc.setTextWidth(-1)
                    self._natural_width = max(1, int(doc.size().width()))
                if self._natural_width > vw + 1:
                    self._set_wrap_mode(False)
                    self._apply_html(self._html_natural)
                    doc.setTextWidth(-1)
                    can_fit_width = False
                else:
                    self._set_wrap_mode(True)
                    self._apply_html(self._html_fill)
                    doc.setTextWidth(vw)
                    can_fit_width = True
            elif self._wrap:
                self._set_wrap_mode(True)
                self._apply_html(self._inner_html)
                doc.setTextWidth(vw)
                can_fit_width = True
            else:
                self._set_wrap_mode(False)
                self._apply_html(self._inner_html)
                doc.setTextWidth(-1)
                can_fit_width = False

            size = doc.size()
            content_w = max(1, int(size.width()))
            content_h = max(1, int(size.height()) + 1)
            if not can_fit_width:
                content_w = max(content_w, vw)

            # 1px 容差：QTextDocument 的浮点宽度取整后可能与视口差 1px，
            # 若按严格比较会误判为溢出并弹出多余的横向滚动条。
            needs_h_scroll = self._horizontal and content_w > vw + 1
            self.browser.setFixedSize(content_w, content_h)

            outer_h = content_h
            if self._max_height is not None:
                outer_h = min(content_h, self._max_height)
            if needs_h_scroll:
                outer_h += self.horizontalScrollBar().sizeHint().height()

            if force or outer_h != self._last_outer_h:
                self._last_outer_h = outer_h
                self.setFixedHeight(outer_h)
        finally:
            self._syncing = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resync(force=False)


class _ImageThumb(QLabel):
    """用户消息中的图片缩略图（点击/双击打开查看器，右键菜单支持保存）。"""
    sig_activated = Signal(str)

    def __init__(self, info, parent=None):
        super().__init__(parent)
        self.info = info
        self.image_path = info.get("path", "")
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click or double-click to view the image\nRight-click: Save / Copy")
        self._load_pixmap()
        self._style()

    def _load_pixmap(self):
        path = self.image_path
        pix = None
        if path and path.lower().endswith('.svg'):
            try:
                from PySide6.QtSvg import QSvgRenderer
                from PySide6.QtGui import QPainter, QImage
                renderer = QSvgRenderer(path)
                if renderer.isValid():
                    size = renderer.defaultSize()
                    if not size.isValid() or size.isEmpty():
                        size = QSize(400, 300)
                    img = QImage(size.width(), size.height(), QImage.Format_ARGB32)
                    img.fill(Qt.transparent)
                    painter = QPainter(img)
                    renderer.render(painter)
                    painter.end()
                    pix = QPixmap.fromImage(img)
            except Exception as e:
                logger.warning(f"SVG thumbnail rendering failed: {e}")
        if pix is None and path and os.path.exists(path):
            pix = QPixmap(path)

        if pix is None or pix.isNull():
            self.setText("Image unavailable")
            self.setFixedSize(120, 90)
            self.setAlignment(Qt.AlignCenter)
            return

        scaled = pix.scaledToHeight(96, Qt.SmoothTransformation)
        self.setPixmap(scaled)
        self.setFixedSize(scaled.size() + QSize(2, 2))

    def _style(self):
        self.setStyleSheet(
            "QLabel { border: 1px solid rgba(128, 128, 128, 0.4); border-radius: 6px; background: rgba(128, 128, 128, 0.08); }")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and os.path.exists(self.image_path):
            self.sig_activated.emit(self.image_path)
        elif event.button() == Qt.LeftButton:
            ToastManager().show(f"Image file not found: {os.path.basename(self.image_path)}", "error")
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        # 双击同样打开查看器（单击已触发，双击时保持一致行为）
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        tm = ThemeManager()
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{ background-color: {tm.color('bg_card')}; color: {tm.color('text_main')};
                     border: 1px solid {tm.color('border')}; border-radius: 6px; padding: 4px; }}
            QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
            QMenu::item:selected {{ background-color: {tm.color('btn_hover')}; }}
        """)
        act_open = menu.addAction(tm.icon("open", "text_main"), "Open Viewer")
        act_save = menu.addAction(tm.icon("download", "text_main"), "Save As...")
        act_copy = menu.addAction(tm.icon("copy", "text_main"), "Copy Image")

        chosen = menu.exec(event.globalPos())
        if chosen == act_open:
            if os.path.exists(self.image_path):
                self.sig_activated.emit(self.image_path)
        elif chosen == act_save:
            self._save_image()
        elif chosen == act_copy:
            self._copy_image()

    def _save_image(self):
        if not os.path.exists(self.image_path):
            ToastManager().show(f"Image file not found: {os.path.basename(self.image_path)}", "error")
            return
        from src.ui.components.file_dialogs import save_file_name
        target, _ = save_file_name(
            self, "Save Image As", os.path.basename(self.image_path),
            "Image (*.png *.jpg *.jpeg *.webp *.gif *.bmp *.svg)")
        if not target:
            return
        try:
            import shutil
            shutil.copy2(self.image_path, target)
            ToastManager().show(f"Image saved: {os.path.basename(target)}", "success")
        except OSError as e:
            logger.error(f"Failed to save image: {e}")
            ToastManager().show(f"Failed to save image: {e}", "error")

    def _copy_image(self):
        pix = self.pixmap()
        if pix:
            QGuiApplication.clipboard().setPixmap(pix)
            ToastManager().show("Image copied to clipboard.", "success")


class ChatBubbleWidget(QWidget):
    # --- 1. 新增消息类型常量 (请放在类属性最顶端) ---
    MSG_USER = 1
    MSG_AI = 2
    MSG_ERROR = 3

    sig_edit_confirmed = Signal(int, str)
    sig_link_clicked = Signal(str)
    sig_retry_clicked = Signal(int)
    sig_plot_plan_confirm = Signal(str)
    sig_ask_user_submit = Signal(str)
    sig_deep_plan_confirm = Signal(str)
    sig_deep_plan_skip = Signal(str)

    # 由 init_ui/add_translation_widget 创建，仅作静态检查声明
    main_layout: QHBoxLayout
    spacer: QWidget
    content_container: QWidget
    content_layout: QVBoxLayout
    ctx_frame: QFrame
    ctx_header: QLabel
    ctx_content: QLabel
    lbl_text: QTextBrowser
    blocks_host: QWidget
    blocks_layout: QVBoxLayout
    edit_input: QTextEdit
    btn_widget: QWidget
    btn_layout: QHBoxLayout
    btn_copy: QPushButton
    btn_copy_md: QPushButton
    btn_bubble_retry: QPushButton
    btn_edit: QPushButton
    edit_btn_widget: QWidget
    edit_btn_layout: QHBoxLayout
    btn_cancel: QPushButton
    btn_confirm: QPushButton
    trans_container: QWidget
    btn_toggle_trans: QPushButton
    lbl_trans: QLabel

    # --- 2. 完整的 __init__ 方法 ---
    def __init__(self, text, is_user, index, context_html=None, parent=None, msg_type=None):
        super().__init__(parent)
        self.original_text = text
        self.is_user = is_user
        self.index = index
        self.context_html = context_html

        if msg_type is not None:
            self.msg_type = msg_type
        else:
            self.msg_type = self.MSG_USER if is_user else self.MSG_AI

        self.is_editing = False
        self._can_edit = True
        self.is_interrupted = False
        # Translator model config injected by the chat tool; used by plot-plan cards
        # to translate non-English user edits back to English before confirming.
        self.translator_config = None

        self.loading_timer = QTimer(self)
        self.loading_timer.timeout.connect(self._animate_loading)
        self.loading_dots = 0
        #: 动态加载提示的轮换状态：当前提示词下标、已累计 tick 数、外部阶段
        #: 文案覆盖（由 response_flow 通过 set_loading_caption 注入）。
        self._loading_phase = 0
        self._loading_ticks = 0
        self._loading_caption = None
        self.is_loading = False

        self.downloaded_images = {}
        self.downloading_urls = set()
        self.download_failed_urls = {}
        self.download_timeouts = {}
        self.image_task_mgrs = {}

        self.image_loading_timer = QTimer(self)
        self.image_loading_timer.timeout.connect(self._animate_image_loading)
        self.image_loading_dots = 0

        # 正文块渲染状态：lbl_text 承载首个文本块，后续的表格/引用/代码块/
        # 思考链由 _extra_blocks 中的独立滚动控件承载（见 _render_blocks）。
        self._extra_blocks = []
        self._lbl_last_height = -1
        self._height_sync_pending = False

        self.init_ui()
        ThemeManager().theme_changed.connect(self._apply_theme)
        # 主题切换后富文本必须重渲染：代码块底色、行内代码、链接色等主题色
        # 以 HTML 内联样式固化在文档里，仅刷 QSS 无法更新（详见 _rerender_on_theme）。
        ThemeManager().theme_changed.connect(self._rerender_on_theme)
        self._apply_theme()
        # "Thinking" 等纯文本 setText 不经过 set_content，提前固化文档默认字体
        self._ensure_document_font()

    # --- 3. 完整的 init_ui 方法 ---
    def init_ui(self):
        tm = ThemeManager()

        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(10, 5, 10, 5)
        self.main_layout.setSpacing(10)

        self.spacer = QWidget()
        self.spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        self.content_container = QWidget()
        self.content_container.setObjectName("BubbleWrapper")
        self.content_container.setAttribute(Qt.WA_StyledBackground, True)
        self.content_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        self.content_layout = QVBoxLayout(self.content_container)
        self.content_layout.setContentsMargins(12, 12, 12, 12)
        self.content_layout.setSpacing(6)
        self.content_layout.setAlignment(Qt.AlignTop)

        if self.context_html:
            self.ctx_frame = QFrame()
            self.ctx_frame.setObjectName("ContextFrame")
            ctx_layout = QVBoxLayout(self.ctx_frame)
            ctx_layout.setContentsMargins(10, 8, 10, 8)
            ctx_layout.setSpacing(4)

            self.ctx_header = QLabel("📎 Attached Context (Click to View)")

            self.ctx_content = QLabel()
            self.ctx_content.setTextFormat(Qt.RichText)
            self.ctx_content.setTextInteractionFlags(Qt.TextBrowserInteraction)
            self.ctx_content.setOpenExternalLinks(False)
            self.ctx_content.linkActivated.connect(self.sig_link_clicked.emit)
            self.ctx_content.setText(self.context_html)
            self.ctx_content.setWordWrap(True)

            self.ctx_frame.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
            self.ctx_content.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

            ctx_layout.addWidget(self.ctx_header)
            ctx_layout.addWidget(self.ctx_content)
            self.content_layout.addWidget(self.ctx_frame)

        # 正文块容器：lbl_text 承载第一段常规文本，表格/引用/代码块/思考链
        # 作为独立滚动控件追加在同一竖直布局里，从而在文档内部获得"按块滚动"
        # 的能力（Qt 富文本自身无法为单个元素加滚动条）。
        self.blocks_host = QWidget()
        self.blocks_host.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.blocks_layout = QVBoxLayout(self.blocks_host)
        self.blocks_layout.setContentsMargins(0, 0, 0, 0)
        self.blocks_layout.setSpacing(6)

        self.lbl_text = ImageAwareTextBrowser()
        self.lbl_text.setOpenExternalLinks(False)
        self.lbl_text.setOpenLinks(False)
        self.lbl_text.setFrameShape(QFrame.NoFrame)
        self.lbl_text.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lbl_text.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.lbl_text.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.lbl_text.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lbl_text.customContextMenuRequested.connect(self.show_context_menu)
        self.lbl_text.anchorClicked.connect(lambda url: self.sig_link_clicked.emit(url.toString()))
        # 双击内联图片（AI 生成 / 工具产图）时打开内部查看器
        self.lbl_text.sig_image_activated.connect(self.open_image_viewer)
        self.blocks_layout.addWidget(self.lbl_text)

        # 文档尺寸变化只做"合并调度"：流式期间 documentSizeChanged 会高频触发，
        # 逐次同步高度会引发"改高度 → 重排 → 再改高度"的正反馈抖动（闪烁）。
        self.lbl_text.document().documentLayout().documentSizeChanged.connect(
            lambda *_: self._schedule_height_sync())

        # 布局逻辑：MSG_ERROR 靠左（类似 AI 气泡）。
        # 气泡与弹簧按 17:3 的 stretch 比例分配宽度（气泡约 85%）：此前
        # 两者同为 Expanding 策略会被平分为各约 50%，气泡永远只占半窗宽，
        # 文本过早换行；stretch 比例使气泡无论内容长短都稳定填充 85%。
        if self.msg_type == self.MSG_ERROR:
            self.main_layout.addWidget(self.content_container, 17)
            self.main_layout.addWidget(self.spacer, 3)
            btn_alignment = Qt.AlignLeft
        elif self.is_user:
            self.main_layout.addWidget(self.spacer, 3)
            self.main_layout.addWidget(self.content_container, 17)
            btn_alignment = Qt.AlignRight
        else:
            self.main_layout.addWidget(self.content_container, 17)
            self.main_layout.addWidget(self.spacer, 3)
            btn_alignment = Qt.AlignLeft

        self.set_content(self.original_text, msg_type=self.msg_type)

        self.edit_input = QTextEdit()
        self.edit_input.setVisible(False)
        self.edit_input.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.edit_input.installEventFilter(self)
        self.edit_input.textChanged.connect(self.adjust_edit_height)

        self.content_layout.addWidget(self.blocks_host)
        self.content_layout.addWidget(self.edit_input)

        self.btn_widget = QWidget()
        self.btn_layout = QHBoxLayout(self.btn_widget)
        self.btn_layout.setContentsMargins(0, 0, 5, 0)
        self.btn_layout.setSpacing(10)
        self.btn_layout.setAlignment(btn_alignment)

        self.btn_copy = QPushButton(" Copy")
        self.btn_copy.setIcon(tm.icon("copy", "text_muted"))
        self.btn_copy.setCursor(Qt.PointingHandCursor)
        self.btn_copy.clicked.connect(self.copy_plain_text)
        self.btn_layout.addWidget(self.btn_copy)

        self.btn_copy_md = QPushButton(" Copy MD")
        self.btn_copy_md.setIcon(tm.icon("markdown_copy", "text_muted"))
        self.btn_copy_md.setCursor(Qt.PointingHandCursor)
        self.btn_copy_md.clicked.connect(self.copy_markdown)
        self.btn_layout.addWidget(self.btn_copy_md)

        if not self.is_user and self.msg_type != self.MSG_ERROR:
            self.btn_bubble_retry = QPushButton(" Retry")
            self.btn_bubble_retry.setIcon(tm.icon("refresh", "warning"))
            self.btn_bubble_retry.setCursor(Qt.PointingHandCursor)
            # 配色在 _apply_theme 中按主题注入（原硬编码橙/白在浅色下对比不足）
            self.btn_bubble_retry.clicked.connect(lambda: self.sig_retry_clicked.emit(self.index))
            self.btn_bubble_retry.setVisible(False)
            self.btn_layout.addWidget(self.btn_bubble_retry)

        if self.is_user:
            self.btn_edit = QPushButton("Edit")
            self.btn_edit.setIcon(tm.icon("edit", "text_muted"))
            self.btn_edit.setCursor(Qt.PointingHandCursor)
            self.btn_edit.clicked.connect(self.toggle_edit)
            self.btn_layout.addWidget(self.btn_edit)

        self.content_layout.addWidget(self.btn_widget)

        if self.is_user:
            self.edit_btn_widget = QWidget()
            self.edit_btn_layout = QHBoxLayout(self.edit_btn_widget)
            self.edit_btn_layout.setContentsMargins(0, 0, 0, 0)
            self.edit_btn_layout.setSpacing(6)
            self.edit_btn_layout.setAlignment(btn_alignment)

            self.btn_cancel = QPushButton(" Cancel")
            self.btn_cancel.setIcon(tm.icon("close", "text_muted"))
            self.btn_cancel.clicked.connect(self.cancel_edit)

            self.btn_confirm = QPushButton(" Confirm")
            self.btn_confirm.setIcon(tm.icon("check-circle", "bg_main"))
            # 配色在 _apply_theme 中按主题注入（academic_blue 为跨主题固定蓝）
            self.btn_confirm.clicked.connect(self.save_edit)

            self.edit_btn_layout.addWidget(self.btn_cancel)
            self.edit_btn_layout.addWidget(self.btn_confirm)

            self.edit_btn_widget.setVisible(False)
            self.content_layout.addWidget(self.edit_btn_widget)

    # --- 3.1 用户消息图片缩略图条 ---
    def set_image_files(self, image_infos):
        """在气泡中渲染上传图片的缩略图条（用户消息）。

        :param image_infos: 附件信息 dict 列表（``type == "image"``），
                            需含 ``path`` 字段。
        """
        if not image_infos:
            return
        if getattr(self, '_image_strip', None) is not None:
            return  # 幂等：重建气泡时不会重复插入

        strip = QWidget()
        strip_layout = QHBoxLayout(strip)
        strip_layout.setContentsMargins(0, 2, 0, 2)
        strip_layout.setSpacing(8)
        strip_layout.setAlignment(Qt.AlignLeft)

        for info in image_infos:
            thumb = _ImageThumb(info)
            thumb.sig_activated.connect(self.open_image_viewer)
            strip_layout.addWidget(thumb)

        self._image_strip = strip
        # 插到文本上方：有附件上下文框时紧跟其后，否则置顶
        insert_idx = 1 if getattr(self, 'ctx_frame', None) is not None else 0
        self.content_layout.insertWidget(min(insert_idx, self.content_layout.count()), strip)

    def open_image_viewer(self, image_path):
        """用内部查看器打开本地图片（含 SVG）。"""
        if image_path and os.path.exists(image_path):
            open_image_viewer(image_path, parent=self)
        else:
            ToastManager().show(f"Image file not found: {os.path.basename(str(image_path))}", "error")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        parent = self.parentWidget()
        if parent:
            # 文本区上限为父宽 85%：气泡主体已由布局 stretch 固定为约 85%
            # 父宽，此上限仅作兜底（≥ 气泡内容区实际宽度，正常不触发约束），
            # 防止极端情况下文档 idealWidth 撑破容器
            max_w = int(parent.width() * 0.85)

            if self.blocks_host.maximumWidth() != max_w:
                self.blocks_host.setMaximumWidth(max_w)
                if hasattr(self, 'edit_input'):
                    self.edit_input.setMaximumWidth(max_w)
                # 不再调用 updateGeometry()：宽度约束变化本身会让布局失效，
                # 额外触发一次会在窗口拖动时放大重排抖动。

        self._schedule_height_sync()

    def adjust_edit_height(self):
        doc_h = int(self.edit_input.document().size().height())
        new_h = doc_h + 14
        if new_h > 350:
            self.edit_input.setFixedHeight(350)
            self.edit_input.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        else:
            self.edit_input.setFixedHeight(max(40, new_h))
            self.edit_input.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def _animate_image_loading(self):
        if not self.downloading_urls:
            self.image_loading_timer.stop()
            return

        self.image_loading_dots = (self.image_loading_dots + 1) % 4

        current_time = time.time()
        timed_out_urls = []
        for url in list(self.downloading_urls):
            if current_time > self.download_timeouts.get(url, current_time + 30):
                timed_out_urls.append(url)

        if timed_out_urls:
            for url in timed_out_urls:
                self.downloading_urls.remove(url)
                if getattr(self, 'download_failed_urls', None) is None:
                    self.download_failed_urls = {}
                self.download_failed_urls[url] = "网络下载超时 (Timeout)"

        self.set_content(self.original_text)

    def show_context_menu(self, pos):
        tm = ThemeManager()
        menu = QMenu(self)
        menu.setStyleSheet(f"""
                    QMenu {{ background-color: {tm.color('bg_card')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 4px; }} 
                    QMenu::item {{ padding: 6px 20px; border-radius: 2px; }}
                    QMenu::item:selected {{ background-color: {tm.color('btn_hover')}; }}
                """)

        act_copy = menu.addAction(tm.icon("copy", "text_main"), "Copy Plain Text")
        act_copy.triggered.connect(self.copy_plain_text)

        act_copy_md = menu.addAction(tm.icon("file-text", "text_main"), "Copy Markdown")
        act_copy_md.triggered.connect(self.copy_markdown)


        if self.is_user and self._can_edit:
            act_edit = menu.addAction(tm.icon("edit", "text_main"), "编辑 (Edit)")
            act_edit.triggered.connect(self.toggle_edit)

        source = self.sender()
        if not isinstance(source, QWidget):
            source = self.lbl_text
        menu.exec(source.mapToGlobal(pos))

    def disable_edit(self):
        self._can_edit = False
        if hasattr(self, 'btn_edit'):
            self.btn_edit.setVisible(False)
        if self.is_editing:
            self.cancel_edit()

    # --- 3.2 富文本浏览器 / 滚动块的统一样式（随主题刷新） ---
    def _browser_qss(self) -> str:
        """正文浏览器 / 块内浏览器的 QSS：透明底 + 主题文字色 + 全局字体。"""
        tm = ThemeManager()
        # 字体栈与 HTML 内联样式同源（西文族优先、CJK 族回退），见
        # text_formatter.qt_font_family_css 的说明。
        css_family = qt_font_family_css()
        return f"""
            QTextBrowser {{
                background-color: transparent; color: {tm.color('text_main')};
                border: none; padding: 0px;
                font-size: 14px; font-family: {css_family};
            }}
        """

    def _scrollbar_qss(self) -> str:
        """气泡内滚动块（表格/引用/代码/思考链）的滚动条样式。

        与聊天滚动区共用一套 overlay 样式：滑块常态全透明，鼠标移到滚动条上或
        拖动时才显形——正文与长表格里不再出现常显竖线。
        """
        return overlay_scrollbar_qss(thickness=8)

    def _style_block(self, block: "OverflowBlock"):
        """给独立滚动块套上当前主题的滚动条与浏览器样式。"""
        block.setStyleSheet(self._scrollbar_qss())
        block.browser.setStyleSheet(self._browser_qss())

    def _apply_theme(self):
        tm = ThemeManager()
        # 注入"西文族优先 + CJK 族回退"的字体栈：栈内所有族都经过存在性校验，
        # 不含 system-ui 之类通用关键字，避免 Qt 走 last-resort 回退。
        css_family = qt_font_family_css()

        # MSG_ERROR 气泡：错误框线（danger 左侧竖条 + 浅色底）由
        # ErrorPanelWidget 统一承载，容器使用与 AI 气泡一致的卡片样式，
        # 避免双重描边，保证全应用报错美术样式一致。
        if self.is_user:
            bg_color = hex_to_rgba(tm.color('success'), 0.15) if tm.current_theme == 'dark' else hex_to_rgba(
                tm.color('success'), 0.1)
            border_color = tm.color('success')
            self.content_container.setStyleSheet(f"""
                QWidget#BubbleWrapper {{
                    background-color: {bg_color};
                    border: 1px solid {border_color};
                    border-radius: 8px;
                }}
            """)
        else:
            bg_color = tm.color('bg_card')
            border_color = tm.color('border')
            self.content_container.setStyleSheet(f"""
                QWidget#BubbleWrapper {{
                    background-color: {bg_color};
                    border: 1px solid {border_color};
                    border-radius: 8px;
                }}
            """)

        self.lbl_text.setStyleSheet(self._browser_qss() + self._scrollbar_qss())

        # 拆分出的滚动块（表格 / 引用 / 代码块 / 思考链）同步刷新配色
        for block in getattr(self, '_extra_blocks', ()):
            self._style_block(block)

        self.edit_input.setStyleSheet(f"""
            QTextEdit {{ 
                background-color: {tm.color('bg_input')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('accent')}; 
                border-radius: 6px; padding: 6px 10px; font-family: {css_family}; font-size: 14px;
            }}
        """)

        btn_style = f"QPushButton {{ background-color: transparent; border: none; color: {tm.color('text_muted')}; font-size: 12px; padding: 2px 4px; border-radius: 4px; }} QPushButton:hover {{ color: {tm.color('text_main')}; background-color: {tm.color('btn_hover')}; }}"
        self.btn_copy.setStyleSheet(btn_style)
        if hasattr(self, 'btn_copy_md'): self.btn_copy_md.setStyleSheet(btn_style)
        if hasattr(self, 'btn_edit'): self.btn_edit.setStyleSheet(btn_style)
        if hasattr(self, 'btn_cancel'): self.btn_cancel.setStyleSheet(btn_style)

        # Retry / Confirm：原为硬编码橙色与固定蓝，改为主题取色后随深浅模式
        # 自动刷新（浅色主题下 warning 用更深的橙，背景填充时文字取 bg_main
        # 保证对比度）。
        if hasattr(self, 'btn_bubble_retry'):
            self.btn_bubble_retry.setIcon(tm.icon("refresh", "warning"))
            self.btn_bubble_retry.setStyleSheet(f"""
                QPushButton {{ background-color: transparent; border: none;
                               color: {tm.color('warning')}; font-size: 12px;
                               padding: 2px 4px; border-radius: 4px; font-weight: {strong_weight_css()}; }}
                QPushButton:hover {{ color: {tm.color('bg_main')}; background-color: {tm.color('warning')}; }}
            """)
        if hasattr(self, 'btn_confirm'):
            self.btn_confirm.setIcon(tm.icon("check-circle", "bg_main"))
            self.btn_confirm.setStyleSheet(f"""
                QPushButton {{ background-color: {tm.color('academic_blue')}; border: none;
                               color: #ffffff; font-size: 12px; padding: 5px 12px;
                               border-radius: 4px; font-weight: {strong_weight_css()}; }}
                QPushButton:hover {{ background-color: {tm.color('academic_blue_hover')}; }}
            """)

        if hasattr(self, 'ctx_frame'):
            self.ctx_frame.setStyleSheet(f"""
                QFrame#ContextFrame {{ background-color: {hex_to_rgba(tm.color('bg_input'), 0.5)}; border-left: 3px solid {tm.color('accent')}; border-radius: 4px; }}
            """)
            self.ctx_header.setStyleSheet(
                f"color: {tm.color('accent')}; font-size: 11px; font-weight: {strong_weight_css()}; border: none; background: transparent; font-family: {css_family};")
            self.ctx_content.setStyleSheet(
                f"color: {tm.color('text_muted')}; font-size: 12px; border: none; background: transparent; font-family: {css_family}; margin: 0px; padding: 0px;")

        # QSS 重新应用（polish）会改变控件字体并可能同步覆盖文档默认字体，
        # 必须在其后重新固化无衬线默认字体，保证正文渲染命中预定字体。
        self._ensure_document_font()
        self._apply_token_stats_style()

    def _rerender_on_theme(self):
        """主题切换后重渲染富文本内容。

        ``TextFormatter`` 把标题色、代码块/行内代码底色、表格表头底色等
        以 HTML 内联样式写入文档（内联样式优先级高于 QSS），此外
        ``format_chat_text`` 注入的 think 折叠面板与 Mermaid 卡片也自带
        主题色固化的内联样式。因此主题切换时仅刷 QSS 不够，必须重渲染：

        1. 首选让所属 ChatTool 用**原始文本**重跑完整渲染管线
           （``rerender_bubble_from_source``）——只有这样才能刷新 think 面板
           与 Mermaid 卡片这类由上游生成的区块；
        2. 拿不到原始文本时回退为对 ``original_text`` 重渲染：渲染管线本身
           已做幂等处理（先清除上次注入的内联样式再按当前主题重建），因此
           标题/表格/代码块等仍能正确切换主题。

        图片下载缓存与卡片去重集保证重渲染不产生副作用（不重复下载、
        不重复插卡）。
        """
        tool = getattr(self, '_owner_tool', None)
        if tool is not None and hasattr(tool, 'rerender_bubble_from_source'):
            try:
                if tool.rerender_bubble_from_source(self):
                    return
            except Exception as e:
                logger.warning(f"Source-based re-render failed, falling back: {e}")
        try:
            self.set_content(self.original_text)
        except Exception as e:
            logger.warning(f"Failed to re-render bubble content on theme change: {e}")

    def set_loading(self, loading: bool):
        self.is_loading = loading
        if loading:
            self.loading_dots = 0
            self._loading_phase = 0
            self._loading_ticks = 0
            self._loading_caption = None
            self.btn_widget.hide()

            if not self.original_text.strip():
                self.lbl_text.setText(self._loading_label())
                self.loading_timer.start(500)
            else:
                self.set_content(self.original_text)
        else:
            self.loading_timer.stop()
            self._loading_caption = None
            self.btn_widget.show()
            self.set_content(self.original_text)

    def set_loading_caption(self, caption: str):
        """设置等待阶段的阶段名（如 "Contacting the model"），叠加在动态加载
        指示器上；传空串则恢复提示词自动轮换。

        相比直接把阶段文本写进正文，这样做有两个好处：一是等待期间始终保留
        跳动圆点，不会出现"卡住的静态文字"；二是阶段切换只改一行提示，不污染
        消息正文。若气泡当前已有正文，则本调用只更新状态，不打断正文渲染。
        """
        self._loading_caption = (caption or "").strip() or None
        if self.is_loading and not self.original_text.strip():
            self.lbl_text.setText(self._loading_label())
            if not self.loading_timer.isActive():
                self.loading_timer.start(500)

    def _loading_label(self) -> str:
        """构造当前加载提示文案（阶段名优先，否则按 tick 轮换提示词）。"""
        caption = self._loading_caption
        if not caption:
            phase = self._loading_phase % len(_LOADING_PHRASES)
            caption = _LOADING_PHRASES[phase]
        return f"{caption}{'.' * self.loading_dots}"

    def _animate_loading(self):
        if self.original_text.strip():
            self.loading_timer.stop()
            return

        self.loading_dots = (self.loading_dots + 1) % 4
        # 无外部阶段文案时，按固定 tick 轮换提示词，避免长时间停在同一句话。
        if not self._loading_caption:
            self._loading_ticks += 1
            if self._loading_ticks % _LOADING_PHRASE_TICKS == 0:
                self._loading_phase = (self._loading_phase + 1) % len(_LOADING_PHRASES)
        self.lbl_text.setText(self._loading_label())

    # --- 4.5 R 绘图卡片提取（流式标记 → 固定 QWidget） ---
    def _extract_rplot_cards(self, text: str) -> str:
        """检测 ``<rplot_card data="...">`` 标记并转换为 RPlotCardWidget。

        - 标记内为 base64 编码的 JSON（见 runtime._handle_plot_result）。
        - ``set_content`` 会被流式调用多次，因此用 ``_rplot_rendered``
          记录已创建卡片的 key，保证每个图只插入一次；标记本身从文本中
          剥离，不再进入 markdown 渲染。
        """
        if "<rplot_card" not in text:
            return text
        if not hasattr(self, "_rplot_rendered"):
            self._rplot_rendered = set()

        import base64
        import json

        def _replace(match):
            try:
                data = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
            except ValueError:
                logger.warning("Malformed <rplot_card> marker dropped")
                return ""
            key = data.get("png_path") or data.get("svg_path") or data.get("chart_title", "")
            if key and key not in self._rplot_rendered:
                self._rplot_rendered.add(key)
                try:
                    from src.ui.components.r_plot_card import RPlotCardWidget

                    card = RPlotCardWidget(data)
                    # btn_widget 在 __init__ 末尾才创建；若尚未创建则直接追加
                    idx = self.content_layout.indexOf(getattr(self, "btn_widget", None))
                    if idx >= 0:
                        self.content_layout.insertWidget(idx, card)
                    else:
                        self.content_layout.addWidget(card)
                except Exception as e:
                    logger.error(f"Failed to create R plot card: {e}")
            return ""

        text = re.sub(r'<rplot_card data="([^"]*)"></rplot_card>', _replace, text)
        # 清理流式中可能残留的不完整标记
        text = re.sub(r"<rplot_card[^>]*>", "", text)
        return text

    # --- 4.6 Plot 方案确认卡片提取（流式标记 → 固定 QWidget） ---
    def _extract_plot_plan_cards(self, text: str) -> str:
        """检测 ``<plot_plan data="...">`` 标记并转换为 PlotPlanCardWidget。

        - 标记内为 base64 编码的 JSON（见 runtime._handle_propose_plot_plan）。
        - 卡片允许用户查看 / 编辑 / 翻译 / 确认绘图方案；确认后通过
          ``sig_plot_plan_confirm`` 将最终需求（英文）交给 ChatTool 重新发送。
        """
        if "<plot_plan" not in text:
            return text
        if not hasattr(self, "_plot_plan_rendered"):
            self._plot_plan_rendered = set()

        import base64
        import json

        def _replace(match):
            try:
                data = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
            except ValueError:
                logger.warning("Malformed <plot_plan> marker dropped")
                return ""
            key = data.get("plan_text") or data.get("request") or "plan"
            if key in self._plot_plan_rendered:
                return ""
            self._plot_plan_rendered.add(key)
            try:
                from src.ui.components.plot_plan_card import PlotPlanCardWidget

                card = PlotPlanCardWidget(data, translator_config=self.translator_config)
                card.sig_confirm.connect(self.sig_plot_plan_confirm)
                idx = self.content_layout.indexOf(getattr(self, "btn_widget", None))
                if idx >= 0:
                    self.content_layout.insertWidget(idx, card)
                else:
                    self.content_layout.addWidget(card)
            except Exception as e:
                logger.error(f"Failed to create plot plan card: {e}")
            return ""

        text = re.sub(r'<plot_plan data="([^"]*)"></plot_plan>', _replace, text)
        # 清理流式中可能残留的不完整标记
        text = re.sub(r"<plot_plan[^>]*>", "", text)
        return text

    # --- 4.7 Ask-user 澄清卡（文本标记提取 + 结构化事件双通道） ---
    def _create_ask_user_card(self, data) -> bool:
        """创建 ask_user 卡并插入气泡；按问题文本去重（返回是否创建）。

        卡片可能从两条通道到达：文本标记（历史持久化后备）与结构化
        事件（runtime 暂存载荷，任务层经状态事件送达）。两路竞速，
        以去重集保证只渲染一张。
        """
        if not hasattr(self, "_ask_user_rendered"):
            self._ask_user_rendered = set()
        key = (data or {}).get("question", "") or "question"
        if key in self._ask_user_rendered:
            return False
        from src.ui.components.ask_user_card import AskUserCardWidget

        card = AskUserCardWidget(data)
        card.sig_submit.connect(self.sig_ask_user_submit)
        self._ask_user_rendered.add(key)
        self._insert_card(card)
        return True

    def attach_ask_user_card(self, data):
        """结构化事件入口：由 ChatTool._on_chat_result 调用（UI 线程）。"""
        try:
            self._create_ask_user_card(data)
        except Exception as e:
            logger.error(f"Failed to attach ask-user card: {e}")

    def _extract_ask_user_cards(self, text: str) -> str:
        """检测 ``<ask_user data="...">`` 标记并转换为 AskUserCardWidget。

        标记内为 base64 编码的 JSON（见 runtime._handle_ask_user）；用户在
        卡内点选/输入答案后通过 ``sig_ask_user_submit`` 交回发送管线。
        主通路为结构化事件（attach_ask_user_card），本提取为历史重渲染
        提供后备。
        """
        if "<ask_user" not in text:
            return text
        import base64
        import json

        def _replace(match):
            try:
                data = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                logger.warning("Malformed <ask_user> marker dropped")
                return ""
            try:
                self._create_ask_user_card(data)
            except Exception as e:
                logger.error(f"Failed to create ask-user card: {e}")
            return ""

        text = re.sub(r'<ask_user data="([^"]*)"\s*></ask_user>', _replace, text)
        # 清理流式中可能残留的不完整标记，以及上游被 HTML 转义的标记残迹
        text = re.sub(r"<ask_user[^>]*/?>", "", text)
        text = re.sub(r'&lt;/?ask_user[^&]*&gt;', "", text)
        return text

    # --- 4.8 Deep-plan 计划确认卡提取（流式标记 → DeepPlanCardWidget） ---
    def _extract_deep_plan_cards(self, text: str) -> str:
        """检测 ``<deep_plan data="...">`` 标记并转换为 DeepPlanCardWidget。

        标记内为 base64 编码的 JSON（见 chat_tasks._propose_deep_plan）；
        用户确认/跳过后分别触发 ``sig_deep_plan_confirm`` / ``sig_deep_plan_skip``。
        """
        if "<deep_plan" not in text:
            return text
        if not hasattr(self, "_deep_plan_rendered"):
            self._deep_plan_rendered = set()

        import base64
        import json

        def _replace(match):
            try:
                data = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                logger.warning("Malformed <deep_plan> marker dropped")
                return ""
            key = data.get("query", "") or "plan"
            if key in self._deep_plan_rendered:
                return ""
            self._deep_plan_rendered.add(key)
            try:
                from src.ui.components.deep_plan_card import DeepPlanCardWidget

                card = DeepPlanCardWidget(data)
                card.sig_confirm.connect(self.sig_deep_plan_confirm)
                card.sig_skip.connect(self.sig_deep_plan_skip)
                self._insert_card(card)
            except Exception as e:
                logger.error(f"Failed to create deep plan card: {e}")
            return ""

        text = re.sub(r'<deep_plan data="([^"]*)"\s*></deep_plan>', _replace, text)
        # 清理流式中可能残留的不完整标记
        text = re.sub(r"<deep_plan[^>]*/?>", "", text)
        return text

    def _insert_card(self, card):
        """把交互卡片插到气泡尾部按钮区之前（多种卡片共用的插入逻辑）。"""
        anchor = getattr(self, "btn_widget", None)
        idx = self.content_layout.indexOf(anchor) if anchor is not None else -1
        if idx >= 0:
            self.content_layout.insertWidget(idx, card)
        else:
            self.content_layout.addWidget(card)

    # --- 4.9 统一错误面板提取（流式标记 → 固定 QWidget） ---
    def _extract_error_panels(self, text: str) -> str:
        """检测 ``<error_panel data="...">`` 标记并转换为 ErrorPanelWidget。

        与 ``<rplot_card>`` / ``<plot_plan>`` 相同的协议：core/task 层产出
        base64 标记（见 src/core/llm_errors.py），UI 层解码渲染为统一
        错误面板（含可折叠的 Technical Details 技术详情栏）。
        """
        if "<error_panel" not in text:
            return text
        if not hasattr(self, "_error_panel_rendered"):
            self._error_panel_rendered = set()

        import base64
        import json

        def _replace(match):
            try:
                data = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                logger.warning("Malformed <error_panel> marker dropped")
                return ""
            key = f"{data.get('title', '')}|{str(data.get('details', ''))[:128]}"
            if key not in self._error_panel_rendered:
                self._error_panel_rendered.add(key)
                try:
                    from src.ui.components.error_panel import ErrorPanelWidget

                    panel = ErrorPanelWidget(data)
                    anchor = getattr(self, "btn_widget", None)
                    idx = self.content_layout.indexOf(anchor) if anchor is not None else -1
                    if idx >= 0:
                        self.content_layout.insertWidget(idx, panel)
                    else:
                        self.content_layout.addWidget(panel)
                except Exception as e:
                    logger.error(f"Failed to create error panel: {e}")
            return ""

        text = re.sub(r'<error_panel data="([^"]*)"\s*>\s*</error_panel>', _replace, text)
        # 清理流式中可能残留的不完整标记
        text = re.sub(r"<error_panel[^>]*/?>", "", text)
        return text

    # --- 4.8 MSG_ERROR 气泡统一渲染 ---
    def _render_error_payload(self, text: str):
        """MSG_ERROR 气泡统一渲染：解析 JSON payload -> ErrorPanelWidget。

        兼容非 JSON 纯文本（如开发提示），回退为 "Generation Terminated"
        通用标题；多次调用（如 set_loading 复原）时幂等更新同一面板。
        """
        import json
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("payload is not a dict")
        except ValueError:
            data = {"title": "Generation Terminated", "body": text, "details": ""}

        if getattr(self, '_error_panel', None) is None:
            from src.ui.components.error_panel import ErrorPanelWidget
            self._error_panel = ErrorPanelWidget(data)
            idx = self.content_layout.indexOf(self.blocks_host)
            if idx >= 0:
                self.content_layout.insertWidget(idx, self._error_panel)
            else:
                self.content_layout.addWidget(self._error_panel)
        else:
            self._error_panel.update_payload(data)

        self.blocks_host.setVisible(False)

    # --- Token 用量展示（AI 气泡） ---
    def set_token_stats(self, prompt_tokens, completion_tokens, estimated=False, elapsed_ms=None):
        """在气泡按钮行显示本次生成任务的 token 用量与本轮耗时。

        数据来源：provider 返回的真实 usage（runtime 逐步累计）；provider
        未返回 usage 时为估算值（estimated=True，显示带 ~ 前缀）。用户
        气泡或数据无效时不显示。``elapsed_ms`` 为本轮墙钟耗时（毫秒），
        缺省则不显示耗时段。
        """
        if self.is_user:
            return
        try:
            p_txt = f"{int(prompt_tokens):,}"
            c_txt = f"{int(completion_tokens):,}"
        except (TypeError, ValueError):
            return
        if getattr(self, "lbl_token_stats", None) is None:
            from PySide6.QtWidgets import QLabel
            self.lbl_token_stats = QLabel()
            self.lbl_token_stats.setVisible(False)
            # 插到按钮组最前（Copy 之前），不干扰既有按钮布局
            self.btn_layout.insertWidget(0, self.lbl_token_stats)
        prefix = "~" if estimated else ""
        text = f"{prefix}Tokens: {p_txt} in / {c_txt} out"
        elapsed_txt = self._format_elapsed(elapsed_ms)
        if elapsed_txt:
            text += f"  ·  {elapsed_txt}"
        self.lbl_token_stats.setText(text)
        if elapsed_txt:
            self.lbl_token_stats.setToolTip(f"This turn took {elapsed_txt} (wall clock).")
        self.lbl_token_stats.setVisible(True)
        self._apply_token_stats_style()

    @staticmethod
    def _format_elapsed(elapsed_ms) -> str:
        """把毫秒格式化为可读耗时；无效或非正值返回空串。

        ``< 60s`` 用秒（保留一位小数，便于比较不同模型的响应速度），
        更长时用 ``分:秒``，避免出现 1234.5s 这种难读的数字。
        """
        try:
            ms = float(elapsed_ms)
        except (TypeError, ValueError):
            return ""
        if ms <= 0:
            return ""
        seconds = ms / 1000.0
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes, sec = divmod(int(round(seconds)), 60)
        return f"{minutes}m{sec:02d}s"

    def _apply_token_stats_style(self):
        """同步用量标签的 QSS 样式（由 _apply_theme 一并刷新）。"""
        lbl = getattr(self, "lbl_token_stats", None)
        if lbl is None:
            return
        tm = ThemeManager()
        lbl.setStyleSheet(
            f"color: {tm.color('text_muted')}; font-size: 11px; "
            "background: transparent; border: none; padding: 0px;")

    # --- 4.8 排版优化：富文本文档字体 / 行距 / 段距 ---
    def _ensure_document_font(self, browser=None):
        """把 QTextDocument 默认字体设为全局无衬线栈（必须在 setText 之前调用）。

        Qt 在 ``setText`` 解析 HTML 时即用当时的默认字体固化未显式指定
        font-family 的文本；且渲染时未显式指定字体的文本块会**动态**取
        文档默认字体。因此除 ``setText`` 前调用外，还需在 QSS 重新应用
        （``_apply_theme``）后调用，防止 QSS polish 触发的 FontChange 把
        默认字体覆盖为 Qt 未知族名的 last-resort 回退（衬线体）。

        同一方法服务 ``lbl_text`` 与拆分出的滚动块浏览器（``browser`` 参数），
        保证两者排版完全同源。
        """
        browser = browser if browser is not None else getattr(self, 'lbl_text', None)
        if browser is None:
            return
        try:
            doc = browser.document()
            if doc is None:
                return

            # 沿用控件字号（QSS font-size），仅把字体族换成全局字体栈。
            f = QFont(browser.font())
            families = resolve_qt_font_families()
            if families:
                # 直接用 ThemeManager 的栈序（西文族在前、CJK 族随后）：
                # setFamilies 按字形逐个回退，英文取西文族的原生字重，
                # 中文取 CJK 字形。与 HTML 内联 / QSS 注入族完全同源。
                ordered = list(families)
                if hasattr(f, "setFamilies"):
                    try:
                        f.setFamilies(ordered)
                    except TypeError:  # 老版本无 setFamilies，退化为单族
                        f.setFamily(ordered[0])
                else:
                    f.setFamily(ordered[0])
            # 流式渲染会高频调用本方法；字体未变化时跳过写回，避免触发
            # 文档字体变更信号与随之而来的整篇重排。
            if doc.defaultFont() != f:
                doc.setDefaultFont(f)
            if browser.font() != f:
                # 控件字体与文档默认字体保持同源：QSS polish / FontChange 触发的
                # widget→document 字体同步只会回写同一个正确值，而非衬线回退。
                browser.setFont(f)

            # 诊断日志：每个气泡仅记录一次，便于核对实际命中的字体族。
            if browser is getattr(self, 'lbl_text', None) and not getattr(self, "_font_diag_logged", False):
                from PySide6.QtGui import QFontInfo
                try:
                    resolved = QFontInfo(f).family()
                except Exception:
                    resolved = "<unavailable>"
                logger.info("Bubble font applied: families=%s, resolved=%s",
                            families, resolved)
                self._font_diag_logged = True
        except Exception as e:
            logger.debug(f"Failed to set document default font: {e}")

    def _apply_typography(self, browser=None):
        """逐块拉大行距与段落/列表项间距（在 setText 之后调用）。

        字体族由 ``_ensure_document_font`` 在 setText 前设置；本方法只负责
        排版密度，改善长时间阅读的舒适度。
        """
        browser = browser if browser is not None else getattr(self, 'lbl_text', None)
        if browser is None:
            return
        try:
            doc = browser.document()
            if doc is None:
                return

            block = doc.begin()
            while block.isValid():
                fmt = block.blockFormat()
                fmt.setLineHeight(_LINE_HEIGHT_PERCENT, QTextBlockFormat.ProportionalHeight)
                if fmt.topMargin() < _BLOCK_TOP_MARGIN:
                    fmt.setTopMargin(_BLOCK_TOP_MARGIN)
                if fmt.bottomMargin() < _BLOCK_BOTTOM_MARGIN:
                    fmt.setBottomMargin(_BLOCK_BOTTOM_MARGIN)
                cur = QTextCursor(block)
                cur.setBlockFormat(fmt)
                block = block.next()

            # QTextBrowser 会根据视口宽度自动决定换行，无需手动 setTextWidth，
            # 避免固定过宽导致横向溢出。
        except Exception as e:
            logger.debug(f"Failed to apply bubble typography: {e}")

    # --- 5. 完整的 set_content 方法 ---
    def set_content(self, text, msg_type=None):
        text = self._extract_error_panels(text)
        text = self._extract_rplot_cards(text)
        text = self._extract_plot_plan_cards(text)
        text = self._extract_ask_user_cards(text)
        text = self._extract_deep_plan_cards(text)
        self.original_text = text
        if msg_type is not None:
            self.msg_type = msg_type

        if self.msg_type == self.MSG_ERROR:
            self._render_error_payload(text)
            return

        if self.is_loading:
            if not text.strip():
                if not self.loading_timer.isActive():
                    self.loading_dots = 0
                    self.lbl_text.setText(self._loading_label())
                    self.loading_timer.start(500)
                return
            else:
                if self.loading_timer.isActive():
                    self.loading_timer.stop()

        try:
            # 表格/表头/单元格的主题化样式已统一由 TextFormatter.markdown_to_html
            # 注入（theme_key 决定配色），此处不再二次替换：消费方（气泡、PDF
            # 导出等）拿到的是同一套样式，避免"某一方漏主题化"。
            html = TextFormatter.markdown_to_html(text)
            tm = ThemeManager()
            _accent = tm.color('accent')
            _danger = tm.color('danger')

            def _convert_svg_to_png(svg_path: str) -> str:
                """Convert an SVG file to PNG (cached) for inline display.

                ``QTextBrowser`` cannot natively render SVG, so we rasterize it
                with ``QSvgRenderer`` and cache the PNG next to the source file.
                Returns the PNG path, or the original path on failure.
                """
                try:
                    from PySide6.QtSvg import QSvgRenderer
                    from PySide6.QtGui import QPixmap, QPainter
                    png_path = os.path.splitext(svg_path)[0] + ".png"
                    if os.path.exists(png_path):
                        return png_path
                    renderer = QSvgRenderer(svg_path)
                    if not renderer.isValid():
                        return svg_path
                    # Use the SVG's intrinsic size; fall back to 800x600 if unknown.
                    size = renderer.defaultSize()
                    w = max(1, int(size.width())) if size.isValid() and size.width() > 0 else 800
                    h = max(1, int(size.height())) if size.isValid() and size.height() > 0 else 600
                    pixmap = QPixmap(w, h)
                    pixmap.fill(Qt.transparent)
                    painter = QPainter(pixmap)
                    painter.setRenderHint(QPainter.Antialiasing)
                    painter.setRenderHint(QPainter.SmoothPixmapTransform)
                    renderer.render(painter)
                    painter.end()
                    if pixmap.save(png_path, "PNG"):
                        return png_path
                    return svg_path
                except Exception as e:
                    print(f"SVG to PNG conversion failed: {e}")
                    return svg_path

            def _local_uri(path: str) -> str:
                p = path.replace("\\", "/")
                return f"file:///{p}" if not p.startswith("/") else f"file://{p}"

            def repl_img(match):
                raw_src_url = match.group(1)
                src_url = raw_src_url.replace("&amp;", "&")

                if src_url.startswith("data:image"):
                    try:
                        header, encoded = src_url.split(",", 1)
                        ext = header.split(";")[0].split("/")[1] if "/" in header else "png"
                        import base64
                        import hashlib
                        import tempfile

                        img_data = base64.b64decode(encoded)

                        file_name = f"navis_base64_{hashlib.md5(img_data).hexdigest()[:12]}.{ext}"
                        local_path = os.path.join(tempfile.gettempdir(), file_name)

                        if not os.path.exists(local_path):
                            with open(local_path, "wb") as f:
                                f.write(img_data)

                        self.downloaded_images[src_url] = local_path
                        local_uri = f"file:///{local_path.replace(os.sep, '/')}"
                        new_img_tag = f'<img width="420" style="max-width: 100%; border-radius: 8px; margin-top: 5px;" src="{local_uri}" title="Click to view full image" />'
                        return f'<a href="{local_uri}">{new_img_tag}</a>'
                    except (ValueError, OSError) as e:
                        logger.warning(f"Base64 image decode failed: {e}")
                        return f'<img width="420" style="max-width: 100%;" src="{src_url}" />'

                if src_url.startswith("file://"):
                    # Resolve the local file path.
                    try:
                        from urllib.parse import urlparse, unquote
                        parsed = urlparse(src_url)
                        local_path = unquote(parsed.path)
                        if sys.platform == "win32" and local_path.startswith("/"):
                            local_path = local_path.lstrip("/")
                        lower = local_path.lower()
                    except (ValueError, ImportError):
                        return match.group(0)

                    # PDF cannot render inline; route via cite://view to the internal viewer.
                    if lower.endswith(".pdf"):
                        import urllib.parse as _up
                        file_name = os.path.basename(local_path)
                        cite_url = (
                            "cite://view?path=" + _up.quote(local_path)
                            + "&name=" + _up.quote(file_name) + "&text=&page=1"
                        )
                        return (
                            f'<a href="{cite_url}" style="text-decoration:none;">'
                            f'<div style="display:inline-block; padding:14px 18px; border:2px dashed '
                            f'{_accent}; border-radius:8px; margin-top:5px; color:{_accent}; '
                            f'font-weight:{strong_weight_css()};">📄 View PDF (open in internal viewer)</div></a>'
                        )

                    # Convert SVG to PNG before inline display.
                    if lower.endswith(".svg"):
                        local_path = _convert_svg_to_png(local_path)
                        src_url = _local_uri(local_path)

                    new_img_tag = f'<img width="420" style="max-width: 100%; border-radius: 8px; margin-top: 5px;" src="{src_url}" title="Click to view full image" />'
                    return f'<a href="{src_url}">{new_img_tag}</a>'

                if src_url.startswith("http"):
                    if src_url in self.downloaded_images:
                        local_path = self.downloaded_images[src_url].replace('\\', '/')
                        if not local_path.startswith('/'):
                            local_uri = f"file:///{local_path}"
                        else:
                            local_uri = f"file://{local_path}"

                        new_img_tag = f'<img width="420" style="max-width: 100%; border-radius: 8px; margin-top: 5px;" src="{local_uri}" title="Click to view full image" />'
                        return f'<a href="{local_uri}">{new_img_tag}</a>'

                    elif src_url in getattr(self, 'download_failed_urls', {}):
                        error_msg = self.download_failed_urls[src_url]
                        return f'<div style="color:{_danger}; padding: 15px; border: 2px dashed {_danger}; border-radius: 8px; width: 400px; margin-top: 5px;">❌ <b>Image download failed.</b><br><span style="font-size: 12px;">{error_msg}</span></div>'

                    else:
                        import time
                        if src_url not in self.downloading_urls:
                            self.downloading_urls.add(src_url)
                            self.download_timeouts[src_url] = time.time() + 30
                            self._start_image_download(src_url)

                            if not self.image_loading_timer.isActive():
                                self.image_loading_timer.start(500)

                        dots = "." * getattr(self, 'image_loading_dots', 0)
                        return f'<div style="color:{_accent}; padding: 20px; border: 2px dashed {_accent}; border-radius: 8px; width: 400px; margin-top: 5px;">⏳ <span style="vertical-align: middle;">Downloading image to local cache, please wait. {dots}</span></div>'

                return match.group(0)

            html = re.sub(r'<img[^>]+src="([^">]+)"[^>]*>', repl_img, html)

            self._ensure_document_font()
            self._render_blocks(html)

        except Exception as e:
            logger.warning(f"Failed to render bubble content: {e}")
            self._set_browser_text(self.lbl_text, text)

    # --- 5.1 分块渲染：正文 / 表格 / 引用 / 代码块 / 思考链 ---
    def _render_blocks(self, html):
        """把完整富文本按块拆分，逐块交给合适的控件渲染。

        * 第一段 ``text`` 块复用 ``lbl_text``（保持既有的字体、样式、链接与
          图片交互连接），其余块（含后续 text 块）作为独立滚动控件承载；
        * 最终按块在原文中的顺序重排 ``blocks_layout``（思考链通常排在正文
          之前，不能简单把 lbl_text 固定在首位）；
        * 复用同类型的既有控件，流式渲染下只在尾部增长时零重建；
        * 整个过程批量关闭重绘（``setUpdatesEnabled``），结束后统一恢复，
          避免流式中高频调用时中间态被反复绘制造成闪烁。
        """
        blocks = TextFormatter.split_overflow_blocks(html)
        text_slot = next((i for i, (kind, _) in enumerate(blocks) if kind == 'text'), -1)

        self.setUpdatesEnabled(False)
        try:
            # 1) 除 lbl_text 之外的全部块（顺序即其在文档中的先后顺序）
            extras_target = [bf for i, bf in enumerate(blocks) if i != text_slot]
            extra_widgets = self._acquire_extra_blocks(extras_target)

            # 2) lbl_text：承载第一段文本；没有文本块时隐藏（不占位）
            if text_slot >= 0:
                if not self.lbl_text.isVisible():
                    self.lbl_text.setVisible(True)
                self._set_browser_text(self.lbl_text, blocks[text_slot][1])
            else:
                self.lbl_text.setText("")
                self.lbl_text.setVisible(False)

            # 3) 按块顺序重排并写入内容
            order = []
            ei = 0
            for i, (kind, fragment) in enumerate(blocks):
                if i == text_slot:
                    order.append((self.lbl_text, None))
                else:
                    order.append((extra_widgets[ei], fragment))
                    ei += 1

            for position, (widget, fragment) in enumerate(order):
                if self.blocks_layout.indexOf(widget) != position:
                    self.blocks_layout.removeWidget(widget)
                    self.blocks_layout.insertWidget(position, widget)
                if fragment is not None:
                    self._fill_block(widget, fragment)
        finally:
            self.setUpdatesEnabled(True)
            self.update()

        self._schedule_height_sync()

    def _acquire_extra_blocks(self, extras_target):
        """按目标块序列复用 / 新建滚动块控件，返回与目标一一对应的控件列表。

        流式渲染时块序列基本只在尾部增长，按下标比对类型即可命中绝大多数
        复用分支；类型变化（例如表格从「文本」变为「表格」）才从该下标起
        重建（一次性代价）。
        """
        existing = self._extra_blocks
        widgets = []
        for i, (kind, _fragment) in enumerate(extras_target):
            if i < len(existing) and existing[i]._block_kind == kind:
                widget = existing[i]
                if not widget.isVisible():
                    widget.setVisible(True)
            else:
                if i < len(existing):
                    self._discard_blocks(existing, i)
                widget = self._create_block(kind)
                self.blocks_layout.addWidget(widget)
                existing.append(widget)
            widgets.append(widget)

        if len(existing) > len(extras_target):
            self._discard_blocks(existing, len(extras_target))
        return widgets

    def _set_browser_text(self, browser, html):
        """写入 HTML 并固化字体/排版（对文本块与滚动块一致）。"""
        self._ensure_document_font(browser)
        browser.setText(html)
        self._apply_typography(browser)

    def _fill_block(self, block, html):
        """写入块内容：字体必须在写 HTML 前固化，排版在写 HTML 后应用。"""
        self._ensure_document_font(block.browser)
        block.set_content(html)
        self._apply_typography(block.browser)

    def _discard_blocks(self, existing, start):
        """移除并回收下标 ``start`` 起的滚动块控件。"""
        for widget in existing[start:]:
            self.blocks_layout.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()
        del existing[start:]

    def _create_block(self, kind):
        """按块类型创建带独立滚动条的控件（内容随后经 ``_fill_block`` 写入）。"""
        if kind == 'text':
            # 后续文本块（正文被表格/代码块打断后的延续段）：与 lbl_text 一致，
            # 高度自适应、不出现滚动条。
            block = OverflowBlock(block_kind=kind, horizontal=False,
                                  vertical=False, max_height=None, wrap=True)
        elif kind == 'think':
            # 思考链：限制最大高度，超出后内部纵向滚动；横向仅在内嵌宽内容
            # 时出现（换行排版优先）。
            block = OverflowBlock(block_kind=kind, horizontal=True,
                                  vertical=True, max_height=_THINK_MAX_HEIGHT, wrap=True)
        elif kind == 'table':
            # 表格：只关心左右太宽；高度按内容自适应（不压缩列宽）。
            block = OverflowBlock(block_kind=kind, horizontal=True,
                                  vertical=False, wrap=False)
        elif kind == 'code':
            # 代码块：上下太长与左右太宽都要可滚动。
            block = OverflowBlock(block_kind=kind, horizontal=True,
                                  vertical=True, max_height=_CODE_MAX_HEIGHT, wrap=False)
        else:  # quote
            # 引用块：上下太长与左右太宽都要可滚动。
            block = OverflowBlock(block_kind=kind, horizontal=True,
                                  vertical=True, max_height=_QUOTE_MAX_HEIGHT, wrap=True)

        browser = block.browser
        browser.setOpenExternalLinks(False)
        browser.setOpenLinks(False)
        browser.setContextMenuPolicy(Qt.CustomContextMenu)
        browser.customContextMenuRequested.connect(self.show_context_menu)
        browser.anchorClicked.connect(lambda url: self.sig_link_clicked.emit(url.toString()))
        browser.sig_image_activated.connect(self.open_image_viewer)

        self._style_block(block)
        return block

    # --- 5.2 高度同步（合并调度，杜绝布局正反馈抖动） ---
    def _schedule_height_sync(self):
        """请求一次延迟的高度收敛；同一事件循环内多次请求只执行一次。"""
        if self._height_sync_pending:
            return
        self._height_sync_pending = True
        QTimer.singleShot(0, self._sync_heights)

    def _sync_heights(self):
        self._height_sync_pending = False
        if self.lbl_text.isVisible():
            self._adjust_browser_height()
        for block in getattr(self, '_extra_blocks', ()):
            block.sync_layout()

    def clean_up_images(self):
        for path in self.downloaded_images.values():
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        self.downloaded_images.clear()

    def add_translation_widget(self, translated_text):
        if not self.is_user:
            return

        tm = ThemeManager()

        self.trans_container = QWidget()
        trans_layout = QVBoxLayout(self.trans_container)
        trans_layout.setContentsMargins(0, 5, 0, 0)
        trans_layout.setSpacing(4)

        self.btn_toggle_trans = QPushButton(" Show Translated Query")
        self.btn_toggle_trans.setIcon(tm.icon("language", "text_muted"))
        self.btn_toggle_trans.setCursor(Qt.PointingHandCursor)

        self.lbl_trans = QLabel(translated_text)
        self.lbl_trans.setWordWrap(True)
        self.lbl_trans.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_trans.setVisible(False)

        def toggle_trans():
            is_vis = self.lbl_trans.isVisible()
            self.lbl_trans.setVisible(not is_vis)
            self.btn_toggle_trans.setText(" Hide Translated Query" if not is_vis else " Show Translated Query")
            self._apply_trans_theme()

        self.btn_toggle_trans.clicked.connect(toggle_trans)

        trans_layout.addWidget(self.btn_toggle_trans)
        trans_layout.addWidget(self.lbl_trans)

        if hasattr(self, 'btn_widget'):
            idx = self.content_layout.indexOf(self.btn_widget)
            self.content_layout.insertWidget(max(0, idx), self.trans_container)
        else:
            self.content_layout.addWidget(self.trans_container)

        ThemeManager().theme_changed.connect(self._apply_trans_theme)

        self._apply_trans_theme()

    def _apply_trans_theme(self):
        if not hasattr(self, 'btn_toggle_trans') or not hasattr(self, 'lbl_trans'):
            return

        tm = ThemeManager()
        # 与 _apply_theme 一致：QSS 仅接受真实族名，且须含中文字形，
        # 避免未知族或西文族的衬线/宋体回退。
        _families = resolve_qt_font_families()
        css_family = f"'{pick_cjk_font_family(_families)}'"

        self.btn_toggle_trans.setIcon(tm.icon("language", "text_muted"))
        self.btn_toggle_trans.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                text-align: left;
                color: {tm.color('text_muted')};
                font-size: 11px;
                font-weight: {strong_weight_css()};
                font-family: {css_family};
                padding: 2px 0px;
            }}
            QPushButton:hover {{
                color: {tm.color('accent')};
            }}
        """)

        if tm.current_theme == 'dark':
            bg = hex_to_rgba(tm.color('accent'), 0.08)
            border = hex_to_rgba(tm.color('accent'), 0.35)
            text_color = tm.color('text_muted')
        else:
            bg = hex_to_rgba(tm.color('academic_blue'), 0.06)
            border = hex_to_rgba(tm.color('academic_blue'), 0.3)
            text_color = tm.color('text_muted')

        self.lbl_trans.setStyleSheet(f"""
                   QLabel {{
                       color: {text_color};
                       background-color: {bg};
                       border: 1px solid {border};
                       border-left: 3px solid {tm.color('accent')};
                       border-radius: 6px;
                       padding: 8px 10px;
                       font-size: 12px;
                       font-family: {css_family};
                   }}
               """)

    def _adjust_browser_height(self):
        """把正文浏览器高度收敛到文档内容高度（幂等，变化时才写回）。

        仅在目标高度真正变化时调用 ``setFixedHeight``：流式期间文档尺寸会高频变化，
        反复写入相同高度会持续触发父布局失效，形成「改高度 → 重排 → 再改高度」的
        抖动（视觉上表现为闪烁）。

        横向滚动条只在**实际可见**时计入高度。旧实现在它不可见时用 ``idealWidth()``
        预测"将来会出现滚动条"并额外预留一行，再叠加固定 +15px：一旦文档短暂超过
        视口宽度（固定宽度图片、超宽表格行），气泡就会长期高出一行且不回落——表现
        即"气泡高度与内容量不匹配"。去掉预测后，滚动条真正出现时会触发一次
        resize/重排，本函数会再算一次，形成闭环修正。
        """
        if not self.lbl_text.isVisible():
            return
        doc = self.lbl_text.document()
        doc_height = int(doc.size().height())
        sb = self.lbl_text.horizontalScrollBar()
        sb_height = sb.height() if sb.isVisible() else 0
        target = doc_height + sb_height + _BROWSER_HEIGHT_SLACK
        if target != self._lbl_last_height:
            self._lbl_last_height = target
            self.lbl_text.setFixedHeight(target)

    def force_resync_height(self):
        """忽略幂等缓存，强制把气泡高度收敛到最终内容。

        流式输出的中间态（图片未加载完、宽表格未换行、横向滚动条闪现）会把高度写成
        偏大值；幂等缓存命中同一目标值时不再写回，于是气泡"长高不缩回"。回答结束
        （含报错、取消）时调用一次即可收敛，并连带重排拆分出的滚动块。
        """
        self._lbl_last_height = -1
        self._adjust_browser_height()
        for block in getattr(self, '_extra_blocks', ()):
            block.sync_layout(force=True)


    def _start_image_download(self, url):
        ext = url.split("?")[0].split(".")[-1]
        if ext.lower() not in ['png', 'jpg', 'jpeg', 'gif', 'webp','svg']:
            ext = 'png'
        file_name = f"navis_img_{hashlib.md5(url.encode()).hexdigest()}.{ext}"
        save_path = os.path.join(tempfile.gettempdir(), file_name)

        if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
            self._on_image_downloaded({"success": True, "url": url, "path": save_path})
            return

        task_mgr = TaskManager()
        task_mgr.sig_result.connect(self._on_image_downloaded)
        self.image_task_mgrs[url] = task_mgr

        task_mgr.start_task(
            DownloadImageTask,
            task_id=f"dl_img_{hashlib.md5(url.encode()).hexdigest()[:8]}",
            mode=TaskMode.THREAD,
            url=url,
            save_path=save_path
        )

    def _on_image_downloaded(self, result):
        success = result.get("success", False)
        url = result.get("url")
        result_path = result.get("path")

        if url in self.downloading_urls:
            self.downloading_urls.remove(url)

        # 清理已完成的任务管理器实例
        if url in self.image_task_mgrs:
            del self.image_task_mgrs[url]

        if success:
            self.downloaded_images[url] = result_path
        else:
            if not hasattr(self, 'download_failed_urls'):
                self.download_failed_urls = {}
            self.download_failed_urls[url] = result.get("msg", "Network transmission error")
            print(f"Failed to fetch image: {result_path}")

        if not self.downloading_urls and hasattr(self, 'image_loading_timer'):
            self.image_loading_timer.stop()

        self.set_content(self.original_text)

    def _extract_content_for_copy(self, is_markdown=False):
        """核心提取逻辑：全局跨组件打捞 Mermaid 源码，精准清洗 Cite 链接"""
        text = self.original_text

        # 0. 错误内容：错误气泡导出为可读文本（标题/建议/真实详情），
        #    AI 气泡剥离错误面板标记，避免 base64 噪声进入剪贴板。
        if self.msg_type == self.MSG_ERROR:
            try:
                import json as _json
                data = _json.loads(text)
                if isinstance(data, dict):
                    parts = [str(data.get(k, "") or "").strip()
                             for k in ("title", "body", "details")]
                    return "\n".join(p for p in parts if p)
            except ValueError:
                pass
        else:
            from src.core.llm_errors import strip_markers
            text = strip_markers(text)

        # 1. 预处理：移除后台思考过程和系统标识
        text = re.sub(r'<(think|mcp_process)>.*?(?:</\1>|$)', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'Initializing\.\.\.|Reasoning & Tool Execution|\[FINAL_ANSWER\]', '', text, flags=re.IGNORECASE)
        if self.is_loading:
            text = re.sub(r'^Thinking\.{0,3}', '', text)

        # 2. Mermaid 源码强行回填：全局搜索哈希缓存池
        def restore_mermaid_full(match):
            code_hash = match.group(1)
            code = None

            # 第一层级：顺着父组件树往上爬寻找
            curr = self
            while curr:
                cache = getattr(curr, 'mermaid_cache', getattr(curr, 'mermaid_codes', None))
                if cache is not None and code_hash in cache:
                    code = cache[code_hash]
                    break
                curr = curr.parentWidget()

            # 第二层级：若父树断裂，直接在全局应用程序内搜索所有的顶层组件及其子组件
            if not code:
                from PySide6.QtWidgets import QApplication, QWidget
                for top_widget in QApplication.topLevelWidgets():
                    cache = getattr(top_widget, 'mermaid_cache', getattr(top_widget, 'mermaid_codes', None))
                    if cache is not None and code_hash in cache:
                        code = cache[code_hash]
                        break
                    for child in top_widget.findChildren(QWidget):
                        cache = getattr(child, 'mermaid_cache', getattr(child, 'mermaid_codes', None))
                        if cache is not None and code_hash in cache:
                            code = cache[code_hash]
                            break
                    if code: break

            if code:
                return f"\n```mermaid\n{code}\n```\n"
            return ""

        # 彻底匹配 TextFormatter 注入的整块 UI HTML（提取哈希并替换为真源码）
        pattern_ui = r"<br>\s*<div[^>]*>\s*<div[^>]*>\s*<b>Mermaid Diagram Generated</b>\s*</div>\s*<a href=['\"]mermaid://view\?hash=([a-f0-9]+)['\"][^>]*>.*?</a>\s*</div>\s*<br>"
        text = re.sub(pattern_ui, restore_mermaid_full, text, flags=re.DOTALL | re.IGNORECASE)
        # 兼容匹配遗漏的 Markdown/纯文本形式链接
        text = re.sub(r'\[[^\]]*\]\(mermaid://view\?hash=([a-f0-9]+)\)', restore_mermaid_full, text,
                      flags=re.IGNORECASE)
        text = re.sub(r"mermaid://view\?hash=([a-f0-9]+)", restore_mermaid_full, text, flags=re.IGNORECASE)

        # 扫尾：清除没匹配到的残留 UI 提示文字
        text = re.sub(r"Mermaid Diagram Generated.*?Click here to view / edit interactive diagram", "", text,
                      flags=re.DOTALL | re.IGNORECASE)

        # 3. 内部协议链接统一还原为纯文本：cite:// 保留 [n] 编号，mermaid:// / think://
        #    一并清理，避免把只在应用内可点击的路由链接复制进 .md/.txt。
        text = TextFormatter.strip_internal_links(text)

        # 4. HTTP 与格式化处理
        # 提前用占位符保护好已经捞回来的 Mermaid 代码，防止下一步剥离 HTML 标签时误伤箭头符号
        mermaids = []

        def protect_mermaid(match):
            mermaids.append(match.group(0))
            return f"__MERMAID_{len(mermaids) - 1}__"

        text = re.sub(r'(```mermaid\s*\n.*?\n```)', protect_mermaid, text, flags=re.DOTALL | re.IGNORECASE)

        if not is_markdown:
            # 纯文本模式：将 HTTP 转换为“文字 (URL)”格式
            text = re.sub(r'<a[^>]+href=[\'"](https?://[^\'"]+)[\'"][^>]*>(.*?)</a>', r'\2 (\1)', text,
                          flags=re.IGNORECASE)
            text = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'\1 (\2)', text, flags=re.IGNORECASE)
            # 剥离所有 HTML 标签
            text = re.sub(r'<[^>]+>', '', text)
        else:
            # Markdown模式：HTTP 保持原状，但截断底部的引言区 UI
            if "<b>📚 Cited Sources:</b>" in text or "Reference:" in text:
                text = re.split(r"<br><hr[^>]*>|📚 Reference:", text)[0]
            # 残留 HTML 转成 Markdown（表格 / 图片 / 加粗 / 代码 / 卡片…）：复制出来的
            # .md 应当尽量是 Markdown，只有 Markdown 表达不了的结构才保留精简 HTML。
            text = TextFormatter.html_to_markdown(text)

        # 释放被保护的 Mermaid 代码
        for i, m in enumerate(mermaids):
            text = text.replace(f"__MERMAID_{i}__", m)

        # 收尾：清除过多换行
        lines = [line.rstrip() for line in text.splitlines()]
        cleaned = '\n'.join(lines)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

        return cleaned

    def copy_plain_text(self):
        if getattr(self, 'is_interrupted', False): return
        clipboard = QGuiApplication.clipboard()
        cleaned = self._extract_content_for_copy(is_markdown=False)
        clipboard.setText(cleaned)
        ToastManager().show("Plain text successfully copied to clipboard.", "success")

    def copy_markdown(self):
        if getattr(self, 'is_interrupted', False): return
        clipboard = QGuiApplication.clipboard()
        cleaned = self._extract_content_for_copy(is_markdown=True)
        clipboard.setText(cleaned)
        ToastManager().show("Markdown successfully copied to clipboard.", "success")


    def toggle_edit(self):
        if not self.is_editing:
            self.is_editing = True

            scroll_area = self.window().findChild(QScrollArea)
            current_scroll = scroll_area.verticalScrollBar().value() if scroll_area else 0

            self.blocks_host.setVisible(False)
            self.btn_widget.setVisible(False)

            self.content_layout.setSpacing(4)
            self.content_layout.setContentsMargins(12, 12, 12, 8)

            self.edit_input.setVisible(True)
            self.edit_btn_widget.setVisible(True)

            self.edit_input.setText(self.original_text)
            self.edit_input.setFocus()

            if scroll_area:
                scroll_area.verticalScrollBar().setValue(current_scroll)
        else:
            self.cancel_edit()

    def cancel_edit(self):
        self.is_editing = False
        self.edit_input.setVisible(False)
        if hasattr(self, 'edit_btn_widget'):
            self.edit_btn_widget.setVisible(False)

        self.content_layout.setSpacing(6)
        self.content_layout.setContentsMargins(12, 12, 12, 12)

        self.blocks_host.setVisible(True)
        self.btn_widget.setVisible(True)

    def eventFilter(self, obj, event):
        if obj == self.edit_input and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Return:
                if event.modifiers() & Qt.ShiftModifier:
                    return False
                else:
                    self.save_edit()
                    return True
        return super().eventFilter(obj, event)

    def save_edit(self):
        new_text = self.edit_input.toPlainText().strip()
        self.cancel_edit()

        if new_text and new_text != self.original_text:
            self.sig_edit_confirmed.emit(self.index, new_text)



def closeEvent(self, event):

    # 1. 停止图片加载动画定时器
    if hasattr(self, 'image_loading_timer') and self.image_loading_timer.isActive():
        self.image_loading_timer.stop()
        self.logger.debug("Image loading timer stopped.")

    # 2. 遍历所有任务管理器，取消正在进行的下载任务
    if hasattr(self, 'image_task_mgrs') and self.image_task_mgrs:

        for url in list(self.image_task_mgrs.keys()):
            task_mgr = self.image_task_mgrs[url]
            task_mgr.cancel_task()

            del self.image_task_mgrs[url]

            self.logger.debug(f"Cancelled image download task for URL: {url[:30]}...")

    if hasattr(self, 'downloading_urls'):
        self.downloading_urls.clear()

    super().closeEvent(event)