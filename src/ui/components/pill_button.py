from PySide6.QtWidgets import QFrame, QLabel, QHBoxLayout, QSizePolicy
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor

from src.core.theme_manager import ThemeManager


class FollowUpPillButton(QFrame):
    """
    Auto-wrapping, width-adaptive interactive pill button component.
    Supports left-click (send) and right-click (edit) signals.
    """
    sig_clicked = Signal(str)
    sig_right_clicked = Signal(str)

    def __init__(self, tag: str, text: str, color_key: str, icon_name: str, parent=None):
        super().__init__(parent)
        self.setObjectName("FollowUpPill")
        self.text_content = text
        self.color_key = color_key
        self.icon_name = icon_name

        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        self.icon_lbl = QLabel()
        self.icon_lbl.setFixedSize(16, 16)
        self.icon_lbl.setStyleSheet("background: transparent; border: none;")

        self.lbl = QLabel(f"<b>{tag}</b> | {self.text_content}")
        self.lbl.setWordWrap(True)
        self.lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout.addWidget(self.icon_lbl)
        layout.addWidget(self.lbl)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

    def _apply_theme(self):
        tm = ThemeManager()

        icon = tm.icon(self.icon_name, self.color_key)
        if not icon.isNull():
            self.icon_lbl.setPixmap(icon.pixmap(16, 16))

        actual_color = tm.color(self.color_key)

        self.lbl.setStyleSheet(
            f"color: {actual_color}; background: transparent; border: none; font-size: 13px; font-family: 'Segoe UI'; line-height: 1.4;"
        )

        self._default_style = f"QFrame#FollowUpPill {{ background-color: {tm.color('bg_card')}; border: 1px solid {tm.color('border')}; border-radius: 12px; }}"
        self._hover_style = f"QFrame#FollowUpPill {{ background-color: {tm.color('bg_input')}; border: 1px solid {actual_color}; border-radius: 12px; }}"

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