from PySide6.QtWidgets import QCheckBox

class StyledCheckBox(QCheckBox):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setStyleSheet("""
            QCheckBox {
                spacing: 8px;
                color: #e0e0e0;
                background: transparent;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 4px;
                border: 2px solid #555;
                background-color: #333;
            }
            QCheckBox::indicator:hover {
                border: 2px solid #05B8CC;
            }
            QCheckBox::indicator:checked {
                background-color: #05B8CC;
                border: 2px solid #05B8CC;
                image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='white' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'><polyline points='20 6 9 17 4 12'/></svg>");
            }
        """)