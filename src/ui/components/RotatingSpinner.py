"""加载态视觉的唯一实现处：旋转指示器 :class:`ModernSpinner` 与内容区遮罩
:class:`LoadingOverlay`（详见各自的类文档）。
"""
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget
from PySide6.QtCore import Qt, QEvent, QTimer, QRectF
from PySide6.QtGui import QPainter, QPen, QColor
from src.core.theme_manager import ThemeManager, hex_to_rgba


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


class LoadingOverlay(QWidget):
    """内容区加载遮罩：半透明底色 + 旋转指示 + 一行文案。

    为什么需要遮罩，而不是只让指示器转起来：等待反馈只有在"重活真的在跑"时才有
    意义。历史回灌这类操作过去整段同步跑在 UI 线程，事件循环不转，任何动画都停
    在第一帧。遮罩负责把"正在忙"讲清楚；真正让它动起来的是调用方把重活切成分片、
    在分片之间让出事件循环（见
    ``ChatAttachmentsMixin._apply_imported_history`` 的分片推进）。

    宿主跟随：以 ``host`` 为父控件并自装事件过滤器，宿主 Resize / Show 时铺满宿主
    （Qt 子控件不会自动跟随父控件尺寸），因此调用方无需参与定位。

    延迟亮相：重建很快时（小会话一两帧就结束）不闪现遮罩——否则用户看到的是
    "闪一下"而不是"在加载"。等待超过 :attr:`REVEAL_DELAY_MS` 才真正显示并启动
    动画；期间若已 :meth:`stop`，这次等待就等于没有发生过。
    """

    #: 延迟亮相阈值（毫秒）：小于它的等待不值得打断用户视线。
    REVEAL_DELAY_MS = 120

    def __init__(self, host: QWidget, text: str = "", spinner_size: int = 36):
        super().__init__(host)
        self._host = host
        self.setObjectName("LoadingOverlay")
        # QSS 的 background-color 只在开启 styled background 的 QWidget 上生效
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(12)

        self.spinner = ModernSpinner(size=spinner_size)
        self.label = QLabel(text)
        self.label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.spinner, 0, Qt.AlignHCenter)
        layout.addWidget(self.label, 0, Qt.AlignHCenter)

        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.timeout.connect(self._reveal)

        self.hide()
        host.installEventFilter(self)
        self._apply_theme()

    # ---------------- 外观 ----------------
    def _apply_theme(self):
        """按当前主题刷新底色与文案色。

        每次 :meth:`start` 都重取一次，因此不订阅 ``theme_changed``——遮罩是短命
        浮层，为它挂一条常驻连接（还要处理析构）不划算。
        """
        tm = ThemeManager()
        self.setStyleSheet(
            f"QWidget#LoadingOverlay {{ background-color: "
            f"{hex_to_rgba(tm.color('bg_main'), 0.82)}; }}")
        self.label.setStyleSheet(
            f"color: {tm.color('text_muted')}; font-size: 13px; background: transparent;")

    # ---------------- 生命周期 ----------------
    def start(self, text: str = None):
        """进入加载态：更新文案，超过 :attr:`REVEAL_DELAY_MS` 后亮相并开始旋转。

        重复调用只换文案、不重启延迟计时：否则连续的进度更新会把亮相时刻一直往后
        推，遮罩永远不出现。
        """
        if text is not None:
            self.label.setText(text)
        self._apply_theme()
        if self.isVisible() or self._reveal_timer.isActive():
            return
        self._reveal_timer.start(self.REVEAL_DELAY_MS)

    def stop(self):
        """退出加载态：取消未到期的亮相，停止动画并隐藏。可重复调用。"""
        self._reveal_timer.stop()
        self.spinner.stop()
        self.hide()

    # ---------------- 内部 ----------------
    def _reveal(self):
        self._sync_geometry()
        self.raise_()       # 宿主里可能后加了别的浮层子控件
        self.show()
        self.spinner.start()

    def _sync_geometry(self):
        self.setGeometry(self._host.rect())

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() in (QEvent.Resize, QEvent.Show):
            self._sync_geometry()
        return False
