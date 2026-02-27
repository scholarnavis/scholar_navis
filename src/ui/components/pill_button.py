import re

from PySide6.QtWidgets import QFrame, QLabel, QHBoxLayout, QSizePolicy, QWidget, QVBoxLayout, QGridLayout
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor

from src.core.theme_manager import ThemeManager


class FollowUpGroupWidget(QWidget):
    """封装胶囊按钮的整体组件，解决空隙和高度问题"""

    def __init__(self, questions, trigger_fn, edit_fn, parent=None):
        super().__init__(parent)
        self.setObjectName("FollowUpGroup")
        # 核心：垂直方向绝不扩展，只占刚好能放下按钮的高度
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 5, 0, 5)  # 紧凑的上边距
        layout.setSpacing(10)

        tm = ThemeManager()
        lbl = QLabel("💡 <b>Suggestions:</b>")
        lbl.setStyleSheet(f"color: {tm.color('accent')}; font-size: 12px; border: none; background: transparent;")
        layout.addWidget(lbl)

        # 使用网格布局实现双列展示，减少垂直空间占用
        grid = QGridLayout()
        grid.setSpacing(8)
        grid.setContentsMargins(0, 0, 0, 0)

        # 对应你 pill_button.py 中的配置
        color_map = {
            "Deep Dive": ("warning", "search"),
            "Critical": ("danger", "warning"),
            "Broader": ("success", "explore"),
            "Brainstorm": ("accent", "lightbulb"),
            "Similar": ("accent_hover", "link"),
            "Application": ("title_blue", "rocket"),
            "General": ("text_muted", "help")
        }

        for i, q_obj in enumerate(questions):
            tag = q_obj.get("tag", "General") if isinstance(q_obj, dict) else "General"
            raw_text = q_obj.get("text", q_obj) if isinstance(q_obj, dict) else q_obj
            clean_text = re.sub(r'\[\s*\d+\s*(?:,\s*\d+\s*)*\]', '', raw_text).replace('**', '').strip()

            color_key, icon_name = color_map.get(tag, ("text_muted", "help"))

            # 实例化你的 FollowUpPillButton
            btn = FollowUpPillButton(tag, clean_text, color_key, icon_name)

            # 连接信号
            btn.sig_clicked.connect(trigger_fn)
            btn.sig_right_clicked.connect(edit_fn)

            # 双列排放：i // 2 为行，i % 2 为列
            grid.addWidget(btn, i // 2, i % 2)

        layout.addLayout(grid)


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