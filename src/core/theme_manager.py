import logging
import os
import sys

from PySide6.QtGui import QColor, QPixmap, QPainter, QIcon, Qt
from PySide6.QtCore import QObject, Signal
from PySide6.QtSvg import QSvgRenderer

from src.core.config_manager import ConfigManager



class ThemeManager(QObject):
    theme_changed = Signal()
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._is_initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, '_is_initialized', False):
            return
        super().__init__()
        self._is_initialized = True
        self.logger = logging.getLogger("ThemeManager")
        self._init_themes()

    def _init_themes(self):
        self.themes = {
            "dark": {
                "bg_main": "#1e1e1e",
                "bg_card": "#252526",
                "bg_input": "#333333",
                "text_main": "#e0e0e0",
                "text_muted": "#888888",
                "accent": "#58a6ff",
                "accent_hover": "#79b8ff",
                "academic_blue": "#007acc",
                "academic_blue_hover": "#005a9e" ,
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
                "academic_blue": "#007acc",
                "academic_blue_hover": "#005a9e",
                "title_blue": "#1A365D",
                "border": "#cccccc",
                "danger": "#d32f2f",
                "success": "#2e7d32",
                "warning": "#ed6c02",
                "btn_bg": "#e0e0e0",
                "btn_hover": "#d5d5d5"
            }
        }
        self.current_theme = "dark"

        try:
            saved_theme = ConfigManager().user_settings.get("theme", "dark").lower()
            if saved_theme in self.themes:
                self.current_theme = saved_theme
        except Exception:
            pass

    def font_family(self) -> str:
        return "system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', Roboto, sans-serif"

    @staticmethod
    def get_resource_path(*paths):
        if getattr(sys, 'frozen', False) or '__compiled__' in globals():
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        return os.path.join(base_dir, *paths)


    def set_theme(self, theme_name: str):
        theme_name = theme_name.lower()
        if theme_name in self.themes and self.current_theme != theme_name:
            self.current_theme = theme_name
            self.theme_changed.emit()

    def color(self, role: str) -> str:
        return self.themes[self.current_theme].get(role, "#ff00ff")

    def icon(self, icon_name: str, color_key: str) -> QIcon:
        path = self.get_resource_path("assets", "icons", f"{icon_name}.svg")

        if not os.path.exists(path):
            self.logger.warning(f"Missing icon SVG file: '{icon_name}.svg' at {path}")
            return QIcon()

        color_hex = self.color(color_key)

        renderer = QSvgRenderer(path)
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor(color_hex))
        painter.end()

        return QIcon(pixmap)

    def get_custom_qss(self):
        return f"""
        
        QWidget {{
            font-family: {self.font_family()};
        }}
        
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
            border: 2px solid {self.color('accent')};
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