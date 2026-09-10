import logging
import os
import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QColor, QPixmap, QPainter, QIcon, Qt
from PySide6.QtCore import QObject, Signal
from PySide6.QtSvg import QSvgRenderer

from src.core import BASE_DIR
from src.core.config_manager import ConfigManager


class ThemeManager(QObject):
    theme_changed = Signal()
    _instance = None
    current_theme: str

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
                "academic_blue_hover": "#005a9e",
                "title_blue": "#6BA4E7",
                "border": "#444444",
                "danger": "#ff6b6b",
                "success": "#4caf50",
                "warning": "#ffb86c",
                "btn_bg": "#3e3e42",
                "btn_hover": "#4e4e52",
                "selection_fg": "#1e1e1e",
                # Markdown 富文本代码配色：块级代码底色比气泡卡底（#252526）
                # 更深形成层次；行内代码用中灰底与正文区分。文字色需与底色
                # 成对设计并显式注入 HTML，杜绝"深底配深字"的失效对比。
                "code_bg": "#1a1a1a",
                "code_fg": "#e8eaed",
                "code_border": "#3d3d3d",
                "inline_code_bg": "#333333",
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
                "btn_hover": "#d5d5d5",
                "selection_fg": "#ffffff",
                # 浅色代码配色（GitHub 风格）：白色气泡上用浅灰底保证可区分，
                # 避免此前直接复用 bg_input（#ffffff）导致代码块完全隐形。
                "code_bg": "#f6f7f8",
                "code_fg": "#24292f",
                "code_border": "#e1e4e8",
                "inline_code_bg": "#eff1f3",
            }
        }
        self.current_theme = "dark"

        try:
            saved_theme = ConfigManager().user_settings.get("theme", "dark").lower()
            if saved_theme == "auto":
                self.current_theme = self._get_system_theme()
            elif saved_theme in self.themes:
                self.current_theme = saved_theme
        except Exception as e:
            self.logger.error(f"Failed to load theme from ConfigManager: {str(e)}")

    # 系统字体解析缓存：QFontInfo/QFontDatabase 查询有开销，且系统字体
    # 运行期不变，进程内解析一次即可。None 表示尚未解析。
    _font_families_cache = None

    # 含中日韩字形的系统族候选（按平台常见度排序，取第一个已安装者）
    _CJK_FAMILY_CANDIDATES = (
        "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
        "Hiragino Sans GB", "Noto Sans CJK SC", "Source Han Sans SC",
        "WenQuanYi Micro Hei",
    )

    def font_families(self) -> list:
        """应用生效字体族列表（按回退优先序），全应用统一字体源。

        首选"系统/用户正在使用的默认 UI 字体"：取 QApplication.font()
        经 QFontInfo 解析出的真实命中族——它由 Qt 依据平台与系统区域
        设置得出（中文 Windows 为雅黑系，macOS 为苹方系），用户自定义
        系统字体时自动跟随。该族不含中文字形时（如西文环境的
        Segoe UI），追加系统已安装的中文字形族作按序回退，保证中文
        渲染不落入衬线宋体。解析结果缓存，失败时兜底 Arial。
        """
        if ThemeManager._font_families_cache is not None:
            return list(ThemeManager._font_families_cache)

        families = []
        # 1. 系统默认 UI 字体的真实命中族
        try:
            from PySide6.QtGui import QFontInfo
            app = QApplication.instance()
            if app is not None:
                real = QFontInfo(app.font()).family()
                if real:
                    families.append(real)
        except Exception as e:
            self.logger.warning(f"Failed to read system default font: {e}")

        # 2. 系统已安装字体中挑中文字形族（默认族缺中文时提供按序回退；
        #    默认族本身已是 CJK 族则不会重复追加）
        try:
            from PySide6.QtGui import QFontDatabase
            db = QFontDatabase()
            installed = set(db.families())
            cjk = next((c for c in self._CJK_FAMILY_CANDIDATES if c in installed), None)
            if cjk and cjk not in families:
                families.append(cjk)
            if not families:
                # QApplication 未就绪等极端情况的静态兜底
                fallback = next(
                    (c for c in ("Segoe UI", "Helvetica Neue", "Roboto", "Arial")
                     if c in installed), None)
                if fallback:
                    families.append(fallback)
                if cjk and cjk not in families:
                    families.append(cjk)
        except Exception as e:
            self.logger.warning(f"QFontDatabase unavailable: {e}")

        families = [f for f in families if f] or ["Arial"]
        self.logger.info(f"Resolved app font families: {families}")
        ThemeManager._font_families_cache = families
        return list(families)

    def font_family(self) -> str:
        """应用统一字体的单族名（带引号），供 QSS / 富文本 HTML 直接注入。

        富文本引擎不支持字体栈，返回多族栈会导致声明整体失效并触发
        衬线回退（Windows 中文显示宋体），因此统一返回经 CJK 优先挑选
        的单族；20+ 处调用点（QSS、HTML div、代码查看器等）自动统一。
        """
        from src.ui.components.text_formatter import pick_cjk_font_family
        return f"'{pick_cjk_font_family(self.font_families())}'"

    def _get_system_theme(self) -> str:
        palette = QApplication.instance().palette()
        bg_color = palette.color(palette.ColorRole.Window)
        return "light" if bg_color.lightness() > 128 else "dark"


    @staticmethod
    def get_resource_path(*paths):
        if '__compiled__' in globals():
            base_dir = BASE_DIR

            if sys.platform == "darwin" and ".app/Contents/MacOS" in base_dir:
                base_dir = os.path.abspath(os.path.join(base_dir, "..", "Resources"))

        elif getattr(sys, 'frozen', False):
            base_dir = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        else:
            base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

        return os.path.join(base_dir, *paths)

    def set_theme(self, theme_name: str):
        theme_name = theme_name.lower()

        if theme_name == "auto":
            theme_name = self._get_system_theme()

        if theme_name in self.themes and self.current_theme != theme_name:
            self.current_theme = theme_name
            self.theme_changed.emit()

    def color(self, role: str, theme: str = None) -> str:
        """按角色取色。``theme`` 缺省跟随当前主题；显式传入（如 PDF 导出
        固定 "light"）时按指定主题取色，未知主题名回落当前主题。
        """
        if theme and theme in self.themes:
            return self.themes[theme].get(role, "#ff00ff")
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

    def get_app_icon(self) -> QIcon:
        if sys.platform == "win32":
            ico_path = self.get_resource_path("Assets", "icon.ico")
            if os.path.exists(ico_path):
                return QIcon(ico_path)

        png_path = self.get_resource_path("Assets", "icon.png")
        if os.path.exists(png_path):
            return QIcon(png_path)

        path = self.get_resource_path("Assets", "ico.svg")
        if not os.path.exists(path):
            path = self.get_resource_path("assets", "ico.svg")

        if not os.path.exists(path):
            self.logger.warning("App icon not found at startup!")
            return QIcon()

        renderer = QSvgRenderer(path)
        pixmap = QPixmap(128, 128)
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        renderer.render(painter)
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
            selection-background-color: {self.color('accent')};
            selection-color: {self.color('selection_fg')};
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