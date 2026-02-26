# src/ui/components/kb_combo.py
from PySide6.QtWidgets import QComboBox, QStyledItemDelegate, QListView, QStyle
from PySide6.QtCore import Qt, QRect, QSize
from PySide6.QtGui import QColor, QPen, QFont, QBrush

from src.core.theme_manager import ThemeManager
from src.ui.components.combo import BaseComboBox


class KBDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        if not index.isValid(): return

        data = index.data(Qt.UserRole)
        if not data:
            super().paint(painter, option, index)
            return

        name = data.get('name', 'Untitled')
        desc = data.get('description', 'No description')
        model = data.get('model_ui_name', 'Unknown Model')
        count = str(data.get('doc_count', 0)) + " Docs"

        tm = ThemeManager()

        if option.state & QStyle.State.State_Selected:
            painter.fillRect(option.rect, QColor(tm.color('btn_hover')))
        else:
            painter.fillRect(option.rect, QColor(tm.color('bg_card')))

        rect = option.rect
        margin = 10

        title_font = QFont("Segoe UI", 11, QFont.Bold)
        painter.setFont(title_font)
        painter.setPen(QColor(tm.color('text_main')))
        title_rect = QRect(rect.left() + margin, rect.top() + 5, rect.width() - 100, 20)
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter, name)

        desc_font = QFont("Segoe UI", 9)
        painter.setFont(desc_font)
        painter.setPen(QColor(tm.color('text_muted')))
        desc_rect = QRect(rect.left() + margin, rect.top() + 25, rect.width() - 100, 15)
        metrics = painter.fontMetrics()
        elided_desc = metrics.elidedText(desc, Qt.ElideRight, desc_rect.width())
        painter.drawText(desc_rect, Qt.AlignLeft | Qt.AlignVCenter, elided_desc)

        model_font = QFont("Consolas", 8)
        painter.setFont(model_font)
        model_width = painter.fontMetrics().horizontalAdvance(model) + 10
        model_rect = QRect(rect.right() - model_width - margin, rect.top() + 8, model_width, 16)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(tm.color('accent'))))
        painter.drawRoundedRect(model_rect, 4, 4)

        painter.setPen(QColor(tm.color('bg_main')))
        painter.drawText(model_rect, Qt.AlignCenter, model)

        count_rect = QRect(rect.right() - 100 - margin, rect.top() + 28, 100, 12)
        painter.setPen(QColor(tm.color('text_muted')))  # 修复：使用主题的次要文本色
        painter.drawText(count_rect, Qt.AlignRight, count)

        painter.restore()

    def sizeHint(self, option, index):
        return QSize(200, 50)


class KBComboBox(BaseComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setView(QListView())
        self.view().setItemDelegate(KBDelegate(self))
        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

    def _apply_theme(self):
        tm = ThemeManager()
        self.setStyleSheet(f"""
            QComboBox {{
                background-color: {tm.color('bg_input')};
                color: {tm.color('text_main')};
                border: 1px solid {tm.color('border')};
                padding: 5px;
                border-radius: 4px;
                font-size: 14px;
            }}
            QComboBox::drop-down {{ border: none; }}
            QComboBox QAbstractItemView {{
                background-color: {tm.color('bg_card')};
                selection-background-color: {tm.color('btn_hover')};
                border: 1px solid {tm.color('border')};
                outline: none;
            }}
        """)