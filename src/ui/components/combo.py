from PySide6.QtWidgets import QComboBox, QListView, QSizePolicy
from PySide6.QtCore import Qt
from src.core.theme_manager import ThemeManager


class BaseComboBox(QComboBox):

    def __init__(self, parent=None, min_height=None, min_width=None, max_width=None):
        super().__init__(parent)

        self.setView(QListView(self))
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


    def wheelEvent(self, e):
        if not self.hasFocus():
            e.ignore()
        else:
            super().wheelEvent(e)