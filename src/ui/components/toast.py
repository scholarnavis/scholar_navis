from PySide6.QtWidgets import QWidget, QLabel, QHBoxLayout, QVBoxLayout, QGraphicsOpacityEffect
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, QPoint


class ToastManager:
    """全局单例，用于管理 Toast 显示"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ToastManager, cls).__new__(cls)
            cls._instance.parent_widget = None
        return cls._instance

    def set_parent(self, widget):
        self.parent_widget = widget

    def show(self, message, level="info"):
        if not self.parent_widget: return
        toast = ToastWidget(self.parent_widget, message, level)
        toast.show_animation()


class ToastWidget(QWidget):
    def __init__(self, parent, text, level="info"):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.SubWindow)  # SubWindow 保证在父窗口内部
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)

        # 样式配置
        colors = {
            "info": "#333333",
            "success": "#2e7d32",
            "warning": "#f57c00",
            "error": "#c62828"
        }
        bg_color = colors.get(level, "#333333")

        # 布局
        layout = QHBoxLayout(self)
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

        # 调整大小并定位（底部居中）
        self.adjustSize()
        parent_rect = parent.geometry()
        x = (parent_rect.width() - self.width()) // 2
        y = parent_rect.height() - 100  # 距离底部 100px
        self.move(x, y)

        # 透明度效果
        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.opacity_effect)
        self.opacity_effect.setOpacity(0.0)

    def show_animation(self):
        # 1. 淡入
        self.anim_in = QPropertyAnimation(self.opacity_effect, b"opacity")
        self.anim_in.setDuration(300)
        self.anim_in.setStartValue(0.0)
        self.anim_in.setEndValue(1.0)
        self.anim_in.setEasingCurve(QEasingCurve.OutCubic)
        self.anim_in.start()

        # 2. 停留 2秒 后淡出
        QTimer.singleShot(2500, self.hide_animation)

    def hide_animation(self):
        self.anim_out = QPropertyAnimation(self.opacity_effect, b"opacity")
        self.anim_out.setDuration(300)
        self.anim_out.setStartValue(1.0)
        self.anim_out.setEndValue(0.0)
        self.anim_out.finished.connect(self.close)  # 动画结束后销毁
        self.anim_out.start()