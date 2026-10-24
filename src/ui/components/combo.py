import logging

from PySide6.QtWidgets import QComboBox, QListView
from PySide6.QtCore import Qt, QPoint
from src.core.theme_manager import ThemeManager

logger = logging.getLogger(__name__)


class BaseComboBox(QComboBox):
    """统一样式的下拉框：可选输入过滤、可限制弹层高度。

    弹层高度**只走** ``setMaxVisibleItems``：它同时决定弹层容器与列表视图的高度，
    两者保持一致。反过来单独压 ``view().setMaximumHeight()`` 会让容器按项数撑高、
    列表被压扁，中间空出一整块**没有背景**的区域（底层窗口的文字直接透出来）；
    容器过高还会在窗口边缘放不下时被翻转，盖住控件自身。

    弹层宽度也在这里对齐控件：Qt 默认按列表内容的 sizeHint 取宽度，比控件窄时
    左边缘会明显错位（模型选择器实测窄了 280px）。
    """

    def __init__(self, parent=None, min_height=None, min_width=None, max_width=None,
                 searchable: bool = False, max_popup_height: int = 0):
        super().__init__(parent)

        self._max_popup_height = int(max_popup_height or 0)
        self.setView(QListView(self))
        if searchable:
            from PySide6.QtWidgets import QCompleter
            self.setEditable(True)
            self.setInsertPolicy(QComboBox.NoInsert)
            self.lineEdit().setPlaceholderText(self.lineEdit().placeholderText() or "Search…")
            completer = self.completer()
            if completer is not None:
                completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
                completer.setFilterMode(Qt.MatchContains)
                completer.setCaseSensitivity(Qt.CaseInsensitive)
        # 参数化控制尺寸
        if min_height is not None:
            self.setMinimumHeight(min_height)

        if min_width is not None:
            self.setMinimumWidth(min_width)

        if max_width is not None:
            self.setMaximumWidth(max_width)

        self.setFocusPolicy(Qt.StrongFocus)
        self.currentTextChanged.connect(self._update_tooltip)
        # 挂载 ThemeManager 监听主题切换
        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

    def _apply_theme(self):
        tm = ThemeManager()

        self.setStyleSheet(f"""
            QComboBox {{
                background-color: {tm.color('bg_input')};
                color: {tm.color('text_main')};
                border: 1px solid {tm.color('border')};
                border-radius: 4px;
                padding: 4px 8px;
            }}
            QComboBox:hover {{
                border: 1px solid {tm.color('accent')};
            }}
            QToolTip {{
                background-color: {tm.color('bg_card')};
                color: {tm.color('text_main')};
                border: 1px solid {tm.color('border')};
                padding: 4px 8px;
                border-radius: 4px;
                font-family: {tm.font_family()};
                font-size: 13px;
            }}
        """)

        self.view().setStyleSheet(f"""
            QListView {{
                background-color: {tm.color('bg_card')};
                color: {tm.color('text_main')};
                border: 1px solid {tm.color('border')};
                outline: none;
            }}
            QListView::item {{
                padding: 4px 6px;
            }}
            QListView::item:selected {{
                background-color: {tm.color('btn_hover')};
                color: {tm.color('text_main')};
            }}
        """)

    def _update_tooltip(self, text):
        self.setToolTip(text)

    def showPopup(self):
        """弹出前折算可见行数，弹出后把容器尺寸拉齐到控件。

        "容器 ↔ 视图"的尺寸分叉都集中在弹出这一刻修掉，调用方只给
        ``max_popup_height`` 即可，不必再碰任何尺寸。
        """
        self._apply_popup_height()
        super().showPopup()
        view = self.view()
        container = view.parentWidget() if view is not None else None
        if container is None:
            logger.debug("Popup container missing; skip alignment.")
            return

        combo_pos = self.mapToGlobal(QPoint(0, 0))
        popup_pos = container.mapToGlobal(QPoint(0, 0))
        narrowed = container.width() < self.width()
        if narrowed:
            # 只加宽不缩窄：内容更宽的弹层保持原样，避免截断较长的项文本。
            container.setFixedWidth(self.width())
        if popup_pos.x() != combo_pos.x():
            # 宽度窄于控件时 Qt 会把弹层摆偏；y 交给 Qt 的翻转逻辑，不动。
            container.move(combo_pos.x(), popup_pos.y())
        logger.debug(
            "Popup aligned: container=%dx%d view=%dx%d max_rows=%d width_fixed=%s dx=%d",
            container.width(), container.height(), view.width(), view.height(),
            self.maxVisibleItems(), narrowed, combo_pos.x() - popup_pos.x())

    def _apply_popup_height(self):
        """把 ``max_popup_height`` 折算成可见行数，交给 ``setMaxVisibleItems``。

        行高随图标与字体变化，所以按当前第一行的实际高度换算，而不是写死行数。
        """
        if self._max_popup_height <= 0:
            return
        view = self.view()
        row = view.sizeHintForRow(0) if self.count() else 0
        if row <= 0:
            row = max(1, self.fontMetrics().height() + 8)
        rows = max(3, self._max_popup_height // row)
        self.setMaxVisibleItems(rows)
        logger.debug("Popup height capped: %dpx / %dpx per row = %d rows.",
                     self._max_popup_height, row, rows)

    def wheelEvent(self, e):
        if not self.hasFocus():
            e.ignore()
        else:
            super().wheelEvent(e)