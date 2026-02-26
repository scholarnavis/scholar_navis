from PySide6.QtWidgets import QFrame, QLabel, QHBoxLayout, QSizePolicy
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor, QColor

from src.core.theme_manager import ThemeManager


class FollowUpPillButton(QFrame):
    """
    自适应换行、宽度自适应 UI 的交互式胶囊按钮组件。
    支持左键点击(发送)和右键点击(编辑)双重信号。
    """
    sig_clicked = Signal(str)
    sig_right_clicked = Signal(str)

    def __init__(self, tag: str, text: str, color: str, icon: str, parent=None):
        super().__init__(parent)
        self.text_content = text
        self.original_color = color

        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        self.lbl = QLabel(f"{icon} <b>{tag}</b> | {self.text_content}")
        self.lbl.setWordWrap(True)
        self.lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout.addWidget(self.lbl)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

    def _get_adapted_color(self) -> str:
        """根据当前主题背景的亮度，动态调整传入的颜色以保证可读性"""
        tm = ThemeManager()
        bg_color = QColor(tm.color('bg_main'))

        luminance = (0.299 * bg_color.red() + 0.587 * bg_color.green() + 0.114 * bg_color.blue())

        pill_color = QColor(self.original_color)

        if luminance > 128:
            h, s, l, a = pill_color.getHsl()

            adapted_l = min(l, 90)
            adapted_s = min(s + 30, 255)

            pill_color.setHsl(h, adapted_s, adapted_l, a)

        return pill_color.name()

    def _apply_theme(self):
        tm = ThemeManager()
        adapted_color = self._get_adapted_color()

        self.lbl.setStyleSheet(
            f"color: {adapted_color}; background: transparent; font-size: 13px; font-family: 'Segoe UI'; line-height: 1.4;"
        )

        self._default_style = f"QFrame {{ background-color: {tm.color('bg_card')}; border: 1px solid {tm.color('border')}; border-radius: 12px; }}"
        self._hover_style = f"QFrame {{ background-color: {tm.color('bg_input')}; border: 1px solid {adapted_color}; border-radius: 12px; }}"

        if self.underMouse():
            self.setStyleSheet(self._hover_style)
        else:
            self.setStyleSheet(self._default_style)

    def enterEvent(self, event):
        self.setStyleSheet(self._hover_style)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setStyleSheet(self._default_style)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.sig_clicked.emit(self.text_content)
        elif event.button() == Qt.RightButton:
            self.sig_right_clicked.emit(self.text_content)
        super().mousePressEvent(event)