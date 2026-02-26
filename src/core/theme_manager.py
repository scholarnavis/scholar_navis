import os
from PySide6.QtGui import QColor, QPixmap, QPainter, QIcon, Qt
from PySide6.QtCore import QObject, Signal
from PySide6.QtSvg import QSvgRenderer

from src.core.config_manager import ConfigManager


def get_themed_icon(icon_name: str, color_hex: str) -> QIcon:
    """加载 SVG 并动态渲染成指定的主题颜色"""
    path = os.path.join(os.getcwd(), "assets", "icons", f"{icon_name}.svg")
    if not os.path.exists(path):
        return QIcon()  # Fallback

    renderer = QSvgRenderer(path)
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
    painter.fillRect(pixmap.rect(), QColor(color_hex))
    painter.end()

    return QIcon(pixmap)

class ThemeManager(QObject):
    theme_changed = Signal()
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(ThemeManager, cls).__new__(cls, *args, **kwargs)
            cls._instance._is_initialized = False
        return cls._instance

    def __init__(self):
        if self._is_initialized:
            return
        super().__init__()
        self._is_initialized = True
        self._init_themes()

    def _init_themes(self):
        try:
            saved_theme = ConfigManager().user_settings.get("theme", "dark").lower()
        except Exception:
            saved_theme = "dark"

        self.themes = {
            "dark": {
                "bg_main": "#1e1e1e",
                "bg_card": "#252526",
                "bg_input": "#333333",
                "text_main": "#e0e0e0",
                "text_muted": "#888888",
                "accent": "#58a6ff",
                "accent_hover": "#79b8ff",
                "title_blue": "#6BA4E7",
                "border": "#444444",
                "danger": "#ff6b6b",
                "success": "#4caf50",
                "warning": "#ffb86c",
                "btn_bg": "#3e3e42",
                "btn_hover": "#4e4e52"
            },
            "light": {
                "bg_main": "#f3f3f3",
                "bg_card": "#ffffff",
                "bg_input": "#ffffff",
                "text_main": "#222222",
                "text_muted": "#666666",
                "accent": "#005a9e",
                "accent_hover": "#004578",
                "title_blue": "#1A365D",
                "border": "#cccccc",
                "danger": "#d32f2f",
                "success": "#2e7d32",
                "warning": "#ed6c02",
                "btn_bg": "#e0e0e0",
                "btn_hover": "#d5d5d5"
            }
        }

        if saved_theme in self.themes:
            self.current_theme = saved_theme
        else:
            self.current_theme = "dark"

    def set_theme(self, theme_name: str):
        theme_name = theme_name.lower()
        if theme_name in self.themes and self.current_theme != theme_name:
            self.current_theme = theme_name
            self.theme_changed.emit()

    def color(self, role: str) -> str:
        return self.themes[self.current_theme].get(role, "#ff00ff")

    def icon(self, icon_name: str, color_role: str = "text_main") -> QIcon:
        path = os.path.join(os.getcwd(), "Assets", "icons", f"{icon_name}.svg")
        if not os.path.exists(path): return QIcon()

        pixmap = QPixmap(path)
        if pixmap.isNull(): return QIcon()

        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor(self.color(color_role)))
        painter.end()

        return QIcon(pixmap)

    def get_custom_qss(self):
        return f"""
        QGroupBox {{ margin-top: 15px; }}
        QGroupBox::title {{
            color: {self.color('title_blue')} !important;
            font-weight: bold !important; font-size: 14px;
            subcontrol-origin: margin; left: 5px; 
        }}

        QLineEdit, QPlainTextEdit, QComboBox {{
            background-color: {self.color('bg_input')}; color: {self.color('text_main')};
            border: 1px solid {self.color('border')}; border-radius: 4px; padding: 5px;
        }}

        QComboBox QAbstractItemView {{
            background-color: {self.color('bg_card')};
            color: {self.color('text_main')};
            border: 2px solid {self.color('accent')}; /* 用醒目的主色框起来与外部隔离 */
            selection-background-color: {self.color('btn_hover')};
            outline: none;
        }}

        QScrollBar:vertical {{
            background: {self.color('bg_main')};
            width: 8px;
            border-left: 1px solid {self.color('border')};
            margin: 0px;
        }}
        QScrollBar::handle:vertical {{
            background: {self.color('text_muted')};
            min-height: 20px;
            border-radius: 4px;
        }}
        QScrollBar::handle:vertical:hover {{
            background: {self.color('accent')};
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0px; 
        }}

        QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled, 
        QLineEdit:read-only, QPlainTextEdit:read-only {{
            background-color: {self.color('bg_main')} !important;
            color: {self.color('text_muted')} !important;
            border: 1px dashed {self.color('border')} !important;
        }}

        QLabel[cssClass="hint"] {{ color: {self.color('text_muted')}; font-size: 11px; }}
        QLabel[cssClass="warning"] {{ color: {self.color('warning')}; font-weight: bold; }}
        QLabel[cssClass="status-success"] {{ color: {self.color('success')}; font-weight: bold; }}
        QLabel[cssClass="status-error"] {{ color: {self.color('danger')}; font-weight: bold; }}
        QLabel[cssClass="status-pending"] {{ color: {self.color('warning')}; }}

        QPushButton[cssClass="icon-btn"] {{ background: transparent; border: none; }}
        QPushButton[cssClass="link-btn"] {{
            background: transparent; color: {self.color('accent')};
            text-align: left; border: none; font-weight: bold;
        }}
        """



    def apply_class(self, widget, class_name):
        widget.setProperty("cssClass", class_name)
        widget.style().unpolish(widget)
        widget.style().polish(widget)