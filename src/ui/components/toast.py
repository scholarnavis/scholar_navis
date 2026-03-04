import logging

from PySide6.QtWidgets import QWidget, QLabel, QHBoxLayout, QGraphicsOpacityEffect
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, QPoint, Signal

logger = logging.getLogger("UI.Toast")

class ToastManager:
    """全局单例，用于管理 Toast 显示（支持多消息向上堆叠排队）"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ToastManager, cls).__new__(cls)
            cls._instance.parent_widget = None
            cls._instance.active_toasts = []
        return cls._instance

    def set_parent(self, widget):
        self.parent_widget = widget

    def show(self, message, level="info"):
        if not self.parent_widget:
            logger.debug("未设置 parent_widget")
            return

        if level == "error":
            logger.error(f"{message}")
        elif level == "warning":
            logger.warning(f"{message}")
        elif level == "success":
            logger.info(f"{message}")
        else:
            logger.info(f"{message}")

        toast = ToastWidget(self.parent_widget, message, level)
        self.active_toasts.append(toast)

        toast.sig_closed.connect(self._remove_toast)
        self._update_positions()
        toast.show_animation()

    def _remove_toast(self, toast_obj):
        if toast_obj in self.active_toasts:
            self.active_toasts.remove(toast_obj)
        self._update_positions()

    def _update_positions(self):
        if not self.parent_widget: return

        try:
            parent_rect = self.parent_widget.rect()
        except RuntimeError:
            self.active_toasts.clear()
            self.parent_widget = None
            return

        base_y = parent_rect.height() - 100
        spacing = 15

        valid_toasts = []
        for toast in self.active_toasts:
            try:
                toast.width()  # 探针检测
                valid_toasts.append(toast)
            except RuntimeError:
                pass
        self.active_toasts = valid_toasts

        for toast in reversed(self.active_toasts):
            x = (parent_rect.width() - toast.width()) // 2
            target_pos = QPoint(x, base_y)

            if not toast.isVisible():
                toast.move(x, base_y + 20)

            # 触发平滑位移动画
            toast.slide_to(target_pos)

            # 更新下一个 Toast 的 Y 坐标
            base_y -= (toast.height() + spacing)


class ToastWidget(QWidget):
    # 自定义信号：在动画彻底结束、组件销毁前通知 Manager
    sig_closed = Signal(object)

    def __init__(self, parent, text, level="info"):
        super().__init__(parent)

        # SubWindow 保证它相对于主窗口定位，且不溢出窗口边界
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.SubWindow)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)  # 鼠标穿透

        colors = {
            "info": "#333333",
            "success": "#2e7d32",
            "warning": "#f57c00",
            "error": "#c62828"
        }
        bg_color = colors.get(level, "#333333")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)  # 消除默认边距

        self.lbl = QLabel(text)
        self.lbl.setStyleSheet(f"""
            QLabel {{
                color: white; 
                font-weight: bold; 
                padding: 10px 20px;
                background-color: {bg_color};
                border-radius: 20px;
                font-family: 'Segoe UI';
            }}
        """)
        layout.addWidget(self.lbl)
        self.adjustSize()

        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.opacity_effect)
        self.opacity_effect.setOpacity(0.0)

        # 保持对动画对象的类级别引用，防止被 Python 的垃圾回收机制 (GC) 吞掉导致动画卡死
        self.anim_in = None
        self.anim_out = None
        self.anim_pos = None

    def slide_to(self, target_pos):
        """控制 Toast 平滑移动到指定坐标"""
        if self.pos() == target_pos: return

        self.anim_pos = QPropertyAnimation(self, b"pos")
        self.anim_pos.setDuration(300)
        self.anim_pos.setStartValue(self.pos())
        self.anim_pos.setEndValue(target_pos)
        self.anim_pos.setEasingCurve(QEasingCurve.OutCubic)
        self.anim_pos.start()

    def show_animation(self):
        self.show()
        self.raise_()

        self.anim_in = QPropertyAnimation(self.opacity_effect, b"opacity")
        self.anim_in.setDuration(300)
        self.anim_in.setStartValue(0.0)
        self.anim_in.setEndValue(1.0)
        self.anim_in.setEasingCurve(QEasingCurve.OutCubic)
        self.anim_in.start()

        # 停留 3 秒后自动执行淡出 (稍作延长，以免多条连续消息来不及看)
        QTimer.singleShot(3000, self.hide_animation)

    def hide_animation(self):

        if self.anim_out and self.anim_out.state() == QPropertyAnimation.Running:
            return

        self.anim_out = QPropertyAnimation(self.opacity_effect, b"opacity")
        self.anim_out.setDuration(300)
        self.anim_out.setStartValue(1.0)
        self.anim_out.setEndValue(0.0)
        self.anim_out.finished.connect(self._on_hide_finished)
        self.anim_out.start()

    def _on_hide_finished(self):
        self.sig_closed.emit(self)
        self.close()