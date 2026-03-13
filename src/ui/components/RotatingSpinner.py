from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QPainter, QPen, QColor
from src.core.theme_manager import ThemeManager


class ModernSpinner(QWidget):
    def __init__(self, parent=None, size=32):
        super().__init__(parent)
        self.setFixedSize(size, size)

        self.start_angle = 0
        self.span_angle = 10
        self.rotation_offset = 0
        self.is_growing = True

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_anim)

    def _update_anim(self):
        self.rotation_offset = (self.rotation_offset + 4) % 360

        if self.is_growing:
            self.span_angle += 6
            if self.span_angle >= 280:
                self.is_growing = False
        else:
            self.span_angle -= 6
            self.start_angle = (self.start_angle + 12) % 360
            if self.span_angle <= 15:
                self.is_growing = True

        self.update()  # 触发重绘

    def start(self):
        self.show()
        self.timer.start(16)

    def stop(self):
        self.timer.stop()
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)  # 开启抗锯齿

        tm = ThemeManager()
        color_hex = tm.color('accent')

        pen = QPen(QColor(color_hex))
        pen.setWidth(3)  # 圆环粗细
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)

        margin = 4
        rect = QRectF(margin, margin, self.width() - margin * 2, self.height() - margin * 2)

        actual_start = -(self.start_angle + self.rotation_offset) * 16
        actual_span = -self.span_angle * 16

        painter.drawArc(rect, actual_start, actual_span)
        painter.end()