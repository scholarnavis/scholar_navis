from PySide6.QtWidgets import QComboBox, QStyledItemDelegate, QListView, QStyle
from PySide6.QtCore import Qt, QRect, QSize
from PySide6.QtGui import QColor, QPen, QFont, QBrush

from src.core.theme_manager import ThemeManager
from src.ui.components.combo import BaseComboBox


class KBDelegate(QStyledItemDelegate):
    """自定义绘制下拉框的每一项"""

    def paint(self, painter, option, index):
        if not index.isValid(): return

        # 获取数据
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

        # 布局参数
        rect = option.rect
        margin = 10

        # 1. 绘制名称 (大字，白色)
        title_font = QFont("Segoe UI", 11, QFont.Bold)
        painter.setFont(title_font)
        painter.setPen(QColor(tm.color('text_main')))
        title_rect = QRect(rect.left() + margin, rect.top() + 5, rect.width() - 100, 20)
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter, name)

        # 2. 绘制描述 (小字，灰色)
        desc_font = QFont("Segoe UI", 9)
        painter.setFont(desc_font)
        painter.setPen(QColor(tm.color('text_muted')))
        desc_rect = QRect(rect.left() + margin, rect.top() + 25, rect.width() - 100, 15)
        # 文本截断
        metrics = painter.fontMetrics()
        elided_desc = metrics.elidedText(desc, Qt.ElideRight, desc_rect.width())
        painter.drawText(desc_rect, Qt.AlignLeft | Qt.AlignVCenter, elided_desc)

        # 3. 绘制模型标签 (右上角，蓝色胶囊背景)
        model_font = QFont("Consolas", 8)
        painter.setFont(model_font)
        model_width = painter.fontMetrics().horizontalAdvance(model) + 10
        model_rect = QRect(rect.right() - model_width - margin, rect.top() + 8, model_width, 16)

        # 画圆角矩形背景
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(tm.color('accent'))))
        painter.drawRoundedRect(model_rect, 4, 4)

        # 画字
        painter.setPen(QColor("#ffffff"))
        painter.drawText(model_rect, Qt.AlignCenter, model)

        # 4. 绘制文档数量 (右下角，灰色)
        count_rect = QRect(rect.right() - 100 - margin, rect.top() + 28, 100, 12)
        painter.setPen(QColor("#aaaaaa"))
        painter.drawText(count_rect, Qt.AlignRight, count)

        painter.restore()

    def sizeHint(self, option, index):
        """每行的高度"""
        return QSize(200, 50)


class KBComboBox(BaseComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setView(QListView())
        self.view().setItemDelegate(KBDelegate(self))
        self.setStyleSheet("""
            QComboBox {
                background-color: #252526;
                color: white;
                border: 1px solid #444;
                padding: 5px;
                border-radius: 4px;
                font-size: 14px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #252526;
                selection-background-color: #37373d;
                outline: none;
            }
        """)