from PySide6.QtWidgets import QLineEdit

class HoverRevealLineEdit(QLineEdit):
    """
    支持鼠标悬停和获取焦点时显示原文，离开时隐藏的输入框，用于允许复制 API Keys。
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setEchoMode(QLineEdit.Password)

    def enterEvent(self, event):
        # 鼠标进入时，显示明文，允许复制
        self.setEchoMode(QLineEdit.Normal)
        super().enterEvent(event)

    def leaveEvent(self, event):
        # 鼠标离开时，如果当前没有处于输入/选中状态，则恢复为密码模式
        if not self.hasFocus():
            self.setEchoMode(QLineEdit.Password)
        super().leaveEvent(event)

    def focusOutEvent(self, event):
        # 失去焦点时，恢复为密码模式
        self.setEchoMode(QLineEdit.Password)
        super().focusOutEvent(event)