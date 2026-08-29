"""Base dialog frame: themed container with anchored sizing and footer buttons."""
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QWidget

from src.core.theme_manager import ThemeManager

__all__ = ["BaseDialog", "HAS_NVML"]


try:
    import pynvml

    pynvml.nvmlInit()
    HAS_NVML = True
except Exception:
    HAS_NVML = False


class BaseDialog(QDialog):
    def __init__(self, parent=None, title="Dialog", width=450):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Dialog |
            Qt.CustomizeWindowHint |
            Qt.WindowTitleHint |
            Qt.WindowCloseButtonHint
        )
        self.setWindowTitle(title)

        self._target_width = width
        self.setFixedWidth(width)

        self._is_closing = False
        self.tm = ThemeManager()
        self._tracked_buttons = []

        self.v_layout = QVBoxLayout(self)
        self.v_layout.setContentsMargins(0, 0, 0, 0)
        self.v_layout.setSpacing(0)

        # --- 内容区 ---
        self.content_widget = QWidget()
        self.content_widget.setObjectName("ContentWidget")
        self.content_widget.setAttribute(Qt.WA_StyledBackground, True)

        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(24, 24, 24, 24)
        self.content_layout.setSpacing(16)

        self.v_layout.addWidget(self.content_widget, 1)

        # --- 底部按钮区 ---
        self.footer_widget = QWidget()
        self.footer_widget.setAttribute(Qt.WA_StyledBackground, True)
        self.footer_widget.setFixedHeight(55)

        self.footer_layout = QHBoxLayout(self.footer_widget)
        self.footer_layout.setContentsMargins(15, 0, 15, 0)
        self.footer_layout.addStretch()
        self.v_layout.addWidget(self.footer_widget)

        self.tm.theme_changed.connect(self._apply_theme)

        self._parent_ref = parent
        QTimer.singleShot(0, self._adjust_and_anchor)

    def _adjust_and_anchor(self):
        """动态尺寸结算修复：去除套娃滚动条，利用原生 sizeHint 进行精准测量"""

        self.content_widget.setFixedWidth(self._target_width)
        self.layout().update()

        # 获取 Qt 引擎根据所有子组件真实排版后算出的“理想高度”
        ideal_height = self.layout().sizeHint().height()

        min_allowed = self.minimumHeight()
        ideal_height = max(ideal_height, min_allowed)

        screen_geo = QGuiApplication.primaryScreen().availableGeometry()
        max_allowed_height = int(screen_geo.height() * 0.85)

        final_height = min(ideal_height, max_allowed_height)

        self.setFixedSize(self._target_width, final_height)

        self._anchor_to_center(self._parent_ref)

    def _anchor_to_center(self, parent):
        frame_geo = self.frameGeometry()

        if parent and parent.window():
            parent_geo = parent.window().geometry()
            target_x = parent_geo.center().x() - (frame_geo.width() // 2)
            target_y = parent_geo.center().y() - (frame_geo.height() // 2)
        else:
            screen_geo = QGuiApplication.primaryScreen().geometry()
            target_x = screen_geo.center().x() - (frame_geo.width() // 2)
            target_y = screen_geo.center().y() - (frame_geo.height() // 2)

        self.move(target_x, target_y)

    def _apply_theme(self):
        from src.core.theme_manager import ThemeManager
        tm = ThemeManager()

        if sys.platform == "win32":
            try:
                import ctypes
                from PySide6.QtGui import QColor

                # 提取当前背景色，通过亮度判断是否处于深色模式
                is_dark = QColor(tm.color('bg_main')).lightness() < 128

                hwnd = int(self.winId())
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                value = ctypes.c_int(1 if is_dark else 0)

                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    DWMWA_USE_IMMERSIVE_DARK_MODE,
                    ctypes.byref(value),
                    ctypes.sizeof(value)
                )
            except Exception:
                pass

        self.setStyleSheet(f"""
            QDialog, QWidget#ContentWidget {{
                background-color: {tm.color('bg_main')};
                color: {tm.color('text_main')};
            }}

            QLineEdit, QTextEdit, QComboBox, QSpinBox {{
                background-color: {tm.color('bg_input')};
                color: {tm.color('text_main')};
                border: 1px solid {tm.color('border')};
                border-radius: 4px;
                padding: 6px;
                selection-background-color: {tm.color('accent')};
                selection-color: {tm.color('selection_fg')};
            }}

            QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {{
                border: 1px solid {tm.color('accent')};
            }}

            QComboBox QAbstractItemView {{
                background-color: {tm.color('bg_input')};
                color: {tm.color('text_main')};
                border: 1px solid {tm.color('border')};
                selection-background-color: {tm.color('btn_hover')};
                selection-color: {tm.color('text_main')};
                outline: none;
            }}

            QScrollArea {{
                background-color: transparent;
                border: none;
            }}
            QScrollBar:vertical, QScrollBar:horizontal {{
                background-color: transparent;
                border: none;
                width: 12px;
                height: 12px;
                margin: 0px;
            }}
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
                background-color: {tm.color('text_muted')};
                border-radius: 4px;
                min-height: 30px;
                min-width: 30px;
                margin: 2px;
            }}
            QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{
                background-color: {tm.color('text_main')};
            }}
            QScrollBar::add-line, QScrollBar::sub-line,
            QScrollBar::add-page, QScrollBar::sub-page {{
                background: none; border: none; height: 0px; width: 0px;
            }}

            QTableWidget {{
                background-color: {tm.color('bg_card')};
                color: {tm.color('text_main')};
                border: 1px solid {tm.color('border')};
                gridline-color: {tm.color('bg_main')};
                outline: none;
            }}
            QHeaderView::section {{
                background-color: {tm.color('bg_input')};
                color: {tm.color('text_muted')};
                border: none;
                border-bottom: 1px solid {tm.color('border')};
                border-right: 1px solid {tm.color('border')};
                padding: 8px;
                font-weight: bold;
            }}
            QTableWidget::item:selected {{
                background-color: {tm.color('btn_hover')};
                color: {tm.color('text_main')};
            }}
            QTableCornerButton::section {{
                background-color: {tm.color('bg_input')};
                border: none;
            }}

            QListWidget {{
                background-color: {tm.color('bg_card')};
                color: {tm.color('text_main')};
                border: 1px solid {tm.color('border')};
                border-radius: 6px;
                outline: none;
            }}
            QListWidget::item {{
                border-bottom: 1px solid {tm.color('bg_main')};
                padding: 6px;
            }}
            QListWidget::item:hover {{
                background-color: {tm.color('btn_hover')};
            }}
            QListWidget::item:selected {{
                background-color: {tm.color('accent')};
                color: {tm.color('selection_fg')};
            }}
        """)

        self.footer_widget.setStyleSheet(f"""
            background-color: {tm.color('bg_card')};
            border-top: 1px solid {tm.color('border')};
        """)

        for btn, b_type in self._tracked_buttons:
            self._update_button_style(btn, b_type)

    def _update_button_style(self, btn, b_type):
        tm = self.tm

        if b_type == "primary":
            style = f"""
                QPushButton {{
                    border-radius: 4px; font-family: 'Segoe UI'; font-size: 13px; font-weight: 500;
                    background-color: {tm.color('accent')};
                    color: {tm.color('bg_main')};
                    border: 1px solid {tm.color('accent')};
                }}
                QPushButton:hover {{ background-color: {tm.color('accent_hover')}; }}
            """
        elif b_type == "danger":
            style = f"""
                QPushButton {{
                    border-radius: 4px; font-family: 'Segoe UI'; font-size: 13px; font-weight: 500;
                    background-color: transparent;
                    color: {tm.color('danger')};
                    border: 1px solid {tm.color('danger')};
                }}
                QPushButton:hover {{ background-color: {tm.color('danger')}; color: {tm.color('bg_main')}; }}
            """
        else:
            style = f"""
                QPushButton {{
                    border-radius: 4px; font-family: 'Segoe UI'; font-size: 13px; font-weight: 500;
                    background-color: {tm.color('btn_bg')};
                    color: {tm.color('text_main')};
                    border: 1px solid {tm.color('border')};
                }}
                QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}
            """

        btn.setStyleSheet(style)


    def add_button(self, text, callback, is_primary=False, is_danger=False):
        btn = QPushButton(text)
        btn.setFixedSize(90, 32)
        btn.setCursor(Qt.PointingHandCursor)

        if callback:
            btn.clicked.connect(lambda *args, cb=callback: cb())

        b_type = "primary" if is_primary else ("danger" if is_danger else "default")
        self._tracked_buttons.append((btn, b_type))
        self._update_button_style(btn, b_type)

        self.footer_layout.addWidget(btn)
        return btn
