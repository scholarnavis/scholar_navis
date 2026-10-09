"""聊天气泡排版设置面板（独立小面板）。

职责：

* 5 个排版参数 × 2 个作用域（LLM 气泡 / 用户气泡）的编辑；
* **生产链路**的实时预览：不另写一套渲染，而是把聊天页那套原样搬进来——
  同一个 :class:`ChatBubbleWidget` 组件、同一个渲染入口
  （``TextFormatter.format_response`` + ``set_content``）、同一个"回源重渲染"
  契约（气泡把重排委托给宿主，见本面板的 ``rerender_bubble_from_source``），
  且**不隐藏任何控件**。预览所见即聊天页所见；
* 确认 / 取消 / 恢复默认；
* 修改标记（标题 ``*`` + 状态标签），未保存就关闭时二次确认。

保存语义：本面板**自管保存**（直接写 ``ConfigManager.user_settings`` 并落盘），
不依赖"全局设置"页的 Save 按钮——它是模态面板，Apply 即生效；保存后广播
:attr:`GlobalSignals.chat_typography_changed`，已存在的气泡就地重排。
"""
import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QDialog, QDoubleSpinBox, QFrame, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QScrollArea,
                               QSlider, QVBoxLayout, QWidget)

from src.core.chat_typography import (PARAMS, SCOPE_LABELS, SCOPES,
                                      TypographyParam, config_key, defaults,
                                      read, serialize)
from src.core.config_manager import ConfigManager
from src.core.signals import GlobalSignals
from src.core.theme_manager import ThemeManager, strong_weight_css
from src.ui.components.chat_bubble import ChatBubbleWidget
from src.ui.components.dialogs.base import BaseDialog

logger = logging.getLogger(__name__)

#: 面板标题（窗口标题带 ``*`` 表示有未保存改动）
_TITLE = "Chat Typography"

#: 面板宽度（px）。参数行本身就要：标签 110 + 两列 × (滑块 150 + 间距 8 +
#: 数值框 88) + 列间距 16×3 = 650，再加组框描边与内容边距 24×2 → 约 702。
#: 声明宽度必须 ≥ 这个值：否则 Qt 会陷入"布局最小宽度 > setFixedWidth"的冲突——
#: 参数行被挤压、固定宽度的滑块与数值框溢出各自单元格（看起来就是"控件伸到了
#: 它该在的范围之外"），或者面板被内容撑到超过声明宽度。开发者模式的排版自检
#: 会核对这条不变式，改动控件尺寸时不会悄悄失效（见 _test_chat_typography）。
_PANEL_WIDTH = 720

#: 预览样例：第一段刻意长到会折行（才看得出**行距**），并放两个层级标题
#: （才看得出**标题层级的自动偏移**：H2 > H3，且标题段距大于正文段距）。
#: 刻意保持精简，避免面板被预览撑高——超高时预览内部滚动。
_PREVIEW_AI_TEXT = (
    "## Reading comfort\n\n"
    "Line spacing applies to every wrapped line inside a paragraph, so a long "
    "sentence can breathe instead of turning into a dense wall of text.\n\n"
    "### Automatic heading offsets\n\n"
    "Headings derive their own size and spacing offsets from these values."
)
_PREVIEW_USER_TEXT = (
    "Could you make the answer easier to read? This sentence wraps as well, so "
    "the line spacing is visible on your own bubble too."
)

#: 预览气泡的"原始文本"（键 = 气泡 index）。与 ChatTool 一样，气泡在重渲染时
#: 向宿主索要原始文本，而不是复用已渲染的 HTML——这样标题层级偏移量才会重算。
_PREVIEW_SOURCES = {0: _PREVIEW_AI_TEXT, 1: _PREVIEW_USER_TEXT}

#: 预览滚动区的高度区间：字号调到上限时气泡会变高，超出部分在预览内滚动，
#: 而不是把整个面板撑到屏幕之外。
_PREVIEW_MIN_HEIGHT = 200
_PREVIEW_MAX_HEIGHT = 300


class _ParamEditor(QWidget):
    """滑块 + 数值框：同一参数的两个输入方式，双向同步并即时回调。

    滑块只能用整数刻度，因此以 ``param.step`` 为单位做映射
    （``value = tick * step``）；数值框统一用 ``QDoubleSpinBox``（整数参数
    ``decimals=0``），避免整数/小数两套控件分支。
    """

    valueChanged = Signal(float)

    def __init__(self, param: TypographyParam, value: float, parent=None):
        super().__init__(parent)
        self.param = param
        self._syncing = False

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(int(round(param.minimum / param.step)),
                             int(round(param.maximum / param.step)))
        self.slider.setSingleStep(1)
        self.slider.setPageStep(max(1, int(round((param.maximum - param.minimum)
                                                 / param.step / 10))))
        self.slider.setFixedWidth(150)

        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(param.decimals)
        self.spin.setSingleStep(param.step)
        self.spin.setRange(param.minimum, param.maximum)
        self.spin.setSuffix(f" {param.unit}")
        self.spin.setFixedWidth(88)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.slider)
        layout.addWidget(self.spin)

        self.slider.valueChanged.connect(self._on_slider)
        self.spin.valueChanged.connect(self._on_spin)
        self.set_value(value, emit=False)

    def value(self) -> float:
        return float(self.spin.value())

    def set_value(self, value: float, emit: bool = True):
        """设置数值（``emit=False`` 用于初始化，避免构建期触发回调）。"""
        clamped = min(max(float(value), self.param.minimum), self.param.maximum)
        changed = abs(clamped - self.value()) > 1e-9
        self._syncing = True
        try:
            self.spin.setValue(clamped)
            self.slider.setValue(int(round(clamped / self.param.step)))
        finally:
            self._syncing = False
        if emit and changed:
            self.valueChanged.emit(self.value())

    def _on_slider(self, tick: int):
        if self._syncing:
            return
        self._syncing = True
        try:
            self.spin.setValue(tick * self.param.step)
        finally:
            self._syncing = False
        self.valueChanged.emit(self.value())

    def _on_spin(self, value: float):
        if self._syncing:
            return
        self._syncing = True
        try:
            self.slider.setValue(int(round(float(value) / self.param.step)))
        finally:
            self._syncing = False
        self.valueChanged.emit(self.value())


class ChatTypographyDialog(BaseDialog):
    """聊天气泡排版设置面板。"""

    #: 预览刷新去抖：拖动滑块会高频触发，避免每个像素都重排气泡
    PREVIEW_DEBOUNCE_MS = 120

    def __init__(self, parent=None):
        super().__init__(parent, title=_TITLE, width=_PANEL_WIDTH)
        self.config = ConfigManager()

        #: 面板打开时的基线（"是否已修改"的判定依据；Cancel 不写盘，天然回滚）
        self._baseline = read(self.config.user_settings)
        self._draft = dict(self._baseline)
        self._editors: dict = {}
        #: 参数名标签 / 列标题：由 _apply_theme 统一上色，避免用 findChildren 猜
        self._plain_labels: list = []
        self._scope_heads: list = []
        #: 关闭确认只弹一次（closeEvent 与 reject 可能先后触发）
        self._close_confirmed = False

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(self.PREVIEW_DEBOUNCE_MS)
        self._preview_timer.timeout.connect(self._refresh_preview)

        self._build_ui()
        self._sync_dirty_state()
        self._apply_theme()
        self._refresh_preview()

        # 预览气泡的高度是异步收敛的（排版完成后才量出真实高度），首帧 sizeHint
        # 可能偏小；延迟再结算一次，避免面板一打开预览就被裁掉一截。
        QTimer.singleShot(200, self, self._adjust_and_anchor)

    # ------------------------------------------------------------- UI ---
    def _build_ui(self):
        self._title_lbl = QLabel("Chat Typography")
        self.lbl_modified = QLabel("● Unsaved changes")
        self.lbl_modified.setVisible(False)

        title_row = QHBoxLayout()
        title_row.addWidget(self._title_lbl)
        title_row.addStretch()
        title_row.addWidget(self.lbl_modified)
        self.content_layout.addLayout(title_row)

        self._hint_lbl = QLabel(
            "Control how LLM answers and your own messages are rendered. Changes "
            "also apply to messages already on screen.\n"
            "Line spacing is a percentage of the font size (100% = single spacing). "
            "Headings (H1–H6) have no controls of their own: their size and spacing "
            "offsets are derived automatically from these values.")
        self._hint_lbl.setWordWrap(True)
        self.content_layout.addWidget(self._hint_lbl)

        self.content_layout.addWidget(self._build_param_group())
        self.content_layout.addWidget(self._build_preview_group(), 1)

        self.btn_reset = self.add_button(" Restore Defaults", self._on_restore_defaults)
        self.btn_reset.setFixedWidth(140)
        self.add_button(" Cancel", self.reject)
        self.add_button(" Apply", self._on_apply, is_primary=True)

    def _build_param_group(self) -> QGroupBox:
        group = QGroupBox("Parameters")
        self._param_grid = QGridLayout(group)
        grid = self._param_grid
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)

        for column, scope in enumerate(SCOPES, start=1):
            head = QLabel(SCOPE_LABELS[scope])
            head.setAlignment(Qt.AlignCenter)
            self._scope_heads.append(head)
            grid.addWidget(head, 0, column)

        for row, param in enumerate(PARAMS, start=1):
            label = QLabel(param.label)
            label.setMinimumWidth(110)
            self._plain_labels.append(label)
            grid.addWidget(label, row, 0)
            for column, scope in enumerate(SCOPES, start=1):
                key = config_key(param.name, scope)
                editor = _ParamEditor(param, self._draft[key])
                editor.valueChanged.connect(
                    lambda value, k=key: self._on_param_changed(k, value))
                grid.addWidget(editor, row, column)
                self._editors[key] = editor

        # 末列吸收多余宽度，避免各列被均匀撑开
        grid.setColumnStretch(len(SCOPES) + 1, 1)
        return group

    def _build_preview_group(self) -> QGroupBox:
        """构建预览区：**完整复用生产链路**，不做裁剪、不隐藏任何控件。

        与聊天页逐条对应：

        * AI 气泡——同流式回答：先建空气泡，再 ``set_content(格式化结果)``
          （见 ``ChatTool.add_bubble`` / ``_throttled_render``）；
        * 用户气泡——同 ``add_bubble(display_text, is_user=True)``：构造时即带上
          文本，由气泡内部走 ``set_content``；
        * 两者都挂上 ``_owner_tool``：主题 / 排版变更时的"回源重渲染"由本面板按与
          ``ChatTool`` 相同的契约处理（见 :meth:`rerender_bubble_from_source`）。

        按钮行、气泡样式、内外边距全部保持生产样貌——预览即聊天页。
        """
        group = QGroupBox("Live preview")
        outer = QVBoxLayout(group)
        outer.setContentsMargins(8, 6, 8, 8)

        self.preview_ai = ChatBubbleWidget("", is_user=False, index=0)
        self.preview_user = ChatBubbleWidget(_PREVIEW_USER_TEXT, is_user=True, index=1)
        for bubble in (self.preview_ai, self.preview_user):
            bubble._owner_tool = self
        self.preview_ai.set_content(
            self._format_preview(_PREVIEW_SOURCES[0], self.preview_ai.index))

        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setSpacing(4)
        holder_layout.addWidget(self.preview_ai)
        holder_layout.addWidget(self.preview_user)
        holder_layout.addStretch()

        self.preview_scroll = QScrollArea()
        self.preview_scroll.setWidgetResizable(True)
        self.preview_scroll.setFrameShape(QFrame.NoFrame)
        self.preview_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.preview_scroll.setMinimumHeight(_PREVIEW_MIN_HEIGHT)
        self.preview_scroll.setMaximumHeight(_PREVIEW_MAX_HEIGHT)
        self.preview_scroll.setWidget(holder)

        outer.addWidget(self.preview_scroll)
        return group

    # ------------------------------------------------ 预览的渲染入口 ---
    @staticmethod
    def _format_preview(raw: str, index: int) -> str:
        """预览的渲染入口：与 ``ChatTool._format_response`` 逐字一致。

        走 :meth:`TextFormatter.format_response` 而不是直接 ``set_content(raw)``，
        这样 Think 面板 / Mermaid 卡片 / 行内引用等上游区块与聊天页完全同源。
        """
        from src.ui.components.text_formatter import TextFormatter
        return TextFormatter.format_response(raw, index, set(), set(), {})

    def rerender_bubble_from_source(self, bubble) -> bool:
        """与 ``ChatTool.rerender_bubble_from_source`` 同契约的回源重渲染。

        主题切换与排版变更都由气泡委托给宿主（``bubble._owner_tool``）。预览气泡的
        宿主是本面板而非 ChatTool，但取源与重渲染方式相同：拿**原始文本**重跑
        ``format_response`` + ``set_content``——标题层级偏移量因此会被重算，
        不会残留上一组参数写进 HTML 的内联样式。
        """
        index = getattr(bubble, 'index', -1)
        raw = _PREVIEW_SOURCES.get(index)
        if not raw:
            return False
        bubble.set_content(self._format_preview(raw, index))
        return True

    # ---------------------------------------------------------- 主题 ---
    def _apply_theme(self):
        super()._apply_theme()
        tm = ThemeManager()

        self._title_lbl.setStyleSheet(
            f"color: {tm.color('text_main')}; font-size: 16px; "
            f"font-weight: {strong_weight_css()}; font-family: {tm.font_family()}; "
            f"background: transparent; border: none;")
        self._hint_lbl.setStyleSheet(
            f"color: {tm.color('text_muted')}; font-size: 12px; "
            f"background: transparent; border: none;")
        self.lbl_modified.setStyleSheet(
            f"color: {tm.color('warning')}; font-size: 12px; "
            f"font-weight: {strong_weight_css()}; background: transparent; border: none;")

        for label in self._plain_labels:
            label.setStyleSheet(
                f"color: {tm.color('text_main')}; background: transparent; border: none;")
        for head in self._scope_heads:
            head.setStyleSheet(
                f"color: {tm.color('accent')}; font-size: 12px; "
                f"font-weight: {strong_weight_css()}; background: transparent; border: none;")

        for group in self.findChildren(QGroupBox):
            group.setStyleSheet(f"""
                QGroupBox {{
                    color: {tm.color('text_muted')};
                    border: 1px solid {tm.color('border')};
                    border-radius: 6px;
                    margin-top: 10px;
                    padding-top: 8px;
                }}
                QGroupBox::title {{
                    subcontrol-origin: margin;
                    left: 10px;
                    padding: 0 4px;
                    color: {tm.color('accent')};
                    font-weight: {strong_weight_css()};
                }}
            """)

        self.btn_reset.setIcon(tm.icon("undo", "text_main"))

    # --------------------------------------------------- 修改状态跟踪 ---
    def _is_modified(self) -> bool:
        return any(abs(self._draft[key] - self._baseline.get(key, 0.0)) > 1e-9
                   for key in self._draft)

    def _sync_dirty_state(self):
        modified = self._is_modified()
        self.lbl_modified.setVisible(modified)
        self.setWindowTitle(f"{_TITLE} *" if modified else _TITLE)

    def _on_param_changed(self, key: str, value: float):
        self._draft[key] = float(value)
        self._sync_dirty_state()
        self._preview_timer.start()

    # ----------------------------------------------------------- 预览 ---
    def _refresh_preview(self):
        """把草稿值喂给预览气泡（与最终落地共用同一条排版路径）。"""
        for bubble in (self.preview_ai, self.preview_user):
            bubble.refresh_typography(self._draft)

    # ----------------------------------------------------------- 动作 ---
    def _on_restore_defaults(self):
        """两个作用域全部恢复默认值（仍属"未保存改动"，需 Apply 才落盘）。"""
        for key, value in defaults().items():
            editor = self._editors.get(key)
            if editor is not None:
                editor.set_value(value)   # 触发回调 → 草稿与修改标记同步更新
        logger.info("Chat typography reset to defaults in the panel (not saved yet).")

    def _on_apply(self):
        settings = self.config.user_settings
        for param in PARAMS:
            for scope in SCOPES:
                key = config_key(param.name, scope)
                settings[key] = serialize(param, self._draft[key])
        self.config.save_settings()

        self._baseline = dict(self._draft)
        self._sync_dirty_state()
        # 广播给所有已存在的气泡（含本面板预览）：就地重排，无需重建消息
        GlobalSignals().chat_typography_changed.emit()
        logger.info("Chat typography applied: %s",
                    {k: settings[k] for k in sorted(settings)
                     if k.startswith("chat_bubble_")})
        self.accept()

    # ------------------------------------------------------- 关闭确认 ---
    def _confirm_discard(self) -> bool:
        """有未保存改动时二次确认；返回 True 表示允许关闭。"""
        if self._close_confirmed or not self._is_modified():
            return True

        prompt = BaseDialog(self, title="Unsaved Changes", width=440)
        label = QLabel("You have unsaved chat typography changes.\n"
                       "Close this panel and discard them?")
        label.setWordWrap(True)
        prompt.content_layout.addWidget(label)
        prompt.add_button(" Keep Editing", prompt.reject)
        prompt.add_button(" Discard Changes", prompt.accept, is_danger=True)
        prompt._apply_theme()

        if prompt.exec() != QDialog.DialogCode.Accepted:
            return False
        self._close_confirmed = True
        return True

    def reject(self):
        """Esc / Cancel 按钮：先过未保存确认。"""
        if not self._confirm_discard():
            return
        super().reject()

    def closeEvent(self, event):
        """标题栏关闭按钮：与 :meth:`reject` 共用 ``_close_confirmed`` 防重复弹窗。"""
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()
