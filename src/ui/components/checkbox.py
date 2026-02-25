from PySide6.QtWidgets import QCheckBox
from PySide6.QtCore import Qt

class StyledCheckBox(QCheckBox):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet("""
            QCheckBox {
                spacing: 0px;
                color: #cccccc;
                background-color: #2a2d2e;
                padding: 6px 14px;
                border-radius: 14px;  /* 圆角胶囊样式 */
                border: 1px solid #444444;
                font-family: 'Segoe UI', sans-serif;
                font-size: 13px;
            }
            QCheckBox:hover {
                background-color: #37373d;
                color: #ffffff;
                border: 1px solid #05B8CC;
            }
            QCheckBox:checked {
                background-color: rgba(5, 184, 204, 0.15);
                color: #05B8CC;
                border: 1px solid #05B8CC;
                font-weight: bold;
            }
            QCheckBox::indicator {
                width: 0px;
                height: 0px;
            }
        """)