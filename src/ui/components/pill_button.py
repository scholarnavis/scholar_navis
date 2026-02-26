from PySide6.QtWidgets import QFrame, QLabel, QHBoxLayout, QSizePolicy
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor

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
        self.color = color

        self.setCursor(QCursor(Qt.PointingHandCursor))
        # 允许控件水平拉伸，并根据内容计算垂直高度
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        self.lbl = QLabel(f"{icon} <b>{tag}</b> | {self.text_content}")
        self.lbl.setWordWrap(True)
        self.lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.lbl.setStyleSheet(
            f"color: {color}; border: none; background: transparent; font-size: 13px; font-family: 'Segoe UI'; line-height: 1.4;"
        )

        layout.addWidget(self.lbl)

        self._default_style = """
            QFrame { background-color: #252526; border: 1px solid #333333; border-radius: 12px; }
        """
        self._hover_style = f"""
            QFrame {{ background-color: #2d2d30; border: 1px solid {self.color}; border-radius: 12px; }}
        """
        self.setStyleSheet(self._default_style)
        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

    def _apply_theme(self):
        tm = ThemeManager()
        self._default_style = f"QFrame {{ background-color: {tm.color('bg_card')}; border: 1px solid {tm.color('border')}; border-radius: 12px; }}"
        self._hover_style = f"QFrame {{ background-color: {tm.color('bg_input')}; border: 1px solid {self.color}; border-radius: 12px; }}"
        self.setStyleSheet(self._default_style)

    def enterEvent(self, event):
        self.setStyleSheet(self._hover_style)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setStyleSheet(self._default_style)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.sig_clicked.emit(self.text_content)
        elif event.button() == Qt.RightButton:
            self.sig_right_clicked.emit(self.text_content)
        super().mouseReleaseEvent(event)