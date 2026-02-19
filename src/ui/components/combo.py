from PySide6.QtWidgets import QComboBox, QListView
from PySide6.QtCore import Qt


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

    def wheelEvent(self, e):
        if not self.hasFocus():
            e.ignore()
        else:
            super().wheelEvent(e)