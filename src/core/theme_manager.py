import logging
import os
import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QColor, QPixmap, QPainter, QIcon, Qt
from PySide6.QtCore import QObject, Signal
from PySide6.QtSvg import QSvgRenderer

from src.core import BASE_DIR
from src.core.config_manager import ConfigManager

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Windows 原生标题栏深浅色适配
# --------------------------------------------------------------------------- #

#: 深色标题栏开关属性号（Windows 10 build 18985+ 与 Windows 11）
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
#: 同上，早期 Windows 10 使用的属性号
_DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY = 19
#: Windows 11 起可显式指定标题栏底色 / 标题文字色（值为 COLORREF 0x00BBGGRR）
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36

#: "属性不被支持"的告警只打一次，避免每次主题切换刷屏
_win_titlebar_warned = False


def _dwm_set_attribute(hwnd: int, attribute: int, value: int) -> int:
    """调用 ``DwmSetWindowAttribute``，返回 HRESULT（负值表示失败）。

    **必须显式声明 argtypes**：HWND 在 64 位 Windows 上是指针宽度，若让
    ctypes 按默认的 32 位 int 传参，句柄值超过 2^31 时会抛 OverflowError，
    被上层 ``except`` 吞掉 —— 外部表现就是"主题已切换但标题栏颜色不变"。
    """
    import ctypes

    dwmapi = ctypes.WinDLL("dwmapi")
    dwmapi.DwmSetWindowAttribute.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    dwmapi.DwmSetWindowAttribute.restype = ctypes.c_int32
    buf = ctypes.c_uint(value)
    return dwmapi.DwmSetWindowAttribute(
        ctypes.c_void_p(hwnd), attribute, ctypes.byref(buf), ctypes.sizeof(buf))


def _windows_build() -> int:
    """当前 Windows 内部版本号（非 Windows 或解析失败返回 0）。"""
    try:
        return int(sys.getwindowsversion().build)
    except Exception:
        try:
            import platform
            return int(platform.version().split(".")[2])
        except Exception:
            return 0


def _to_colorref(hex_color: str) -> int:
    """``#RRGGBB`` → COLORREF(0x00BBGGRR)。非法输入回退 0。"""
    try:
        value = str(hex_color).lstrip("#")
        r, g, b = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
        return (b << 16) | (g << 8) | r
    except (ValueError, IndexError):
        return 0


def apply_native_titlebar_theme(window, is_dark: bool) -> bool:
    """把 Windows 原生标题栏切换为深色 / 浅色，返回是否应用成功。

    :param window: QWidget / QWindow（取其 ``winId()``）或直接的 HWND 整数。

    实现要点（均为踩坑后的加固）：

    * 句柄按指针宽度传递（见 :func:`_dwm_set_attribute`），避免大句柄静默失败；
    * 属性号随系统版本回退：优先 20（Win10 20H1+ / Win11），失败再试 19；
    * Windows 11 额外显式指定标题栏底色与文字色：部分系统配置下仅设 20 仍
      会保留系统浅色标题栏（本函数存在的直接原因）；
    * 属性更新后强制刷新非客户区，否则 DWM 可能沿用旧缓存直到窗口重绘。

    非 Windows 平台直接返回 False，调用方无需自行判断平台。
    """
    if sys.platform != "win32":
        return False

    global _win_titlebar_warned

    try:
        hwnd = int(window.winId()) if hasattr(window, "winId") else int(window)
    except (TypeError, ValueError):
        logger.debug("Invalid window handle for titlebar theming: %r", window)
        return False
    if not hwnd:
        return False

    try:
        import ctypes

        value = 1 if is_dark else 0
        hr = _dwm_set_attribute(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, value)
        if hr < 0:
            hr = _dwm_set_attribute(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY, value)

        if _windows_build() >= 22000:
            theme_key = "dark" if is_dark else "light"
            tm = ThemeManager()
            _dwm_set_attribute(hwnd, _DWMWA_CAPTION_COLOR,
                               _to_colorref(tm.color("bg_main", theme_key)))
            _dwm_set_attribute(hwnd, _DWMWA_TEXT_COLOR,
                               _to_colorref(tm.color("text_main", theme_key)))

        if hr < 0 and not _win_titlebar_warned:
            _win_titlebar_warned = True
            logger.warning(
                "DWM rejected immersive dark titlebar (hr=0x%08X); "
                "caption colors are used as the fallback.", hr & 0xFFFFFFFF)

        # 非客户区（标题栏）刷新：SWP_FRAMECHANGED 触发框架重绘，
        # WM_NCACTIVATE 切换一次强制标题栏按新状态重画。
        # 各接口同样显式声明签名（HWND/WPARAM/LPARAM 均为指针宽度）。
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SetWindowPos.restype = ctypes.c_int
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                        ctypes.c_size_t, ctypes.c_ssize_t]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.RedrawWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_void_p, ctypes.c_uint]
        user32.RedrawWindow.restype = ctypes.c_int

        user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 0, 0, 0, 0, 0x0037)
        user32.SendMessageW(ctypes.c_void_p(hwnd), 0x0086, 0, 0)
        user32.SendMessageW(ctypes.c_void_p(hwnd), 0x0086, 1, 0)
        user32.RedrawWindow(ctypes.c_void_p(hwnd), None, None,
                            0x0400 | 0x0100 | 0x0001)
        logger.debug("Native titlebar themed: hwnd=%s dark=%s hr=%s",
                     hwnd, is_dark, hr)
        return hr >= 0
    except Exception as e:
        # 标题栏适配属外观增强，失败不影响功能
        logger.warning("Failed to theme native titlebar (hwnd=%s): %s", hwnd, e)
        return False


def installed_font_families(qfont_database) -> list:
    """列出系统已安装字体族。

    Qt 6.10 起 ``QFontDatabase`` 已改为纯静态类，实例化会触发 DeprecationWarning
    且未来版本会移除；旧版本则只提供实例方法。两种调用方式都兼容，因此全应用
    统一通过本函数访问，避免各处重复写兼容分支。
    """
    try:
        return qfont_database.families()
    except TypeError:
        return qfont_database().families()


def hex_to_rgba(hex_color: str, alpha: float) -> str:
    """``#RGB`` / ``#RRGGBB`` → ``rgba(r, g, b, a)``，非 hex 值原样返回。

    主题色常需要"低透明度叠加"生成层次底色（悬浮层、提示条、危险态按钮
    悬停背景等）；本函数作为全应用唯一实现放在核心层，避免各 UI 模块
    各自复制一份（也避免 UI 模块之间为借用该工具而产生横向依赖）。
    """
    color = str(hex_color).strip().lstrip('#')
    if len(color) == 3:
        color = ''.join(c * 2 for c in color)
    if len(color) != 6:
        return str(hex_color)
    try:
        r = int(color[0:2], 16)
        g = int(color[2:4], 16)
        b = int(color[4:6], 16)
    except ValueError:
        return str(hex_color)
    return f"rgba({r}, {g}, {b}, {alpha})"


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

    # 西文（拉丁）字体族候选，仅在**系统字体无法解析**时作为兜底使用。
    #
    # 设计原则（与 KDE/GNOME 等桌面应用一致）：字体族一律跟随系统设置
    # （``QApplication.font()``，用户可在系统里配置字体与字号），应用不自作主
    # 张替换字体族，这样界面才能与桌面其它应用保持一致、并自动适配用户设置。
    # 因此该列表只在拿不到系统字体时（Qt 平台主题缺失等）才被采用。
    _FALLBACK_LATIN_FAMILIES = (
        "Segoe UI",         # Windows 默认 UI 族
        "Helvetica Neue",   # macOS
        "Liberation Sans",
        "Arial",
        "Noto Sans",
        "DejaVu Sans",
        "Roboto",
        "Cantarell",
        "Ubuntu",
    )

    def font_families(self) -> list:
        """应用生效字体栈（顺序即渲染优先级），全应用唯一字体源。

        **字体族跟随系统设置**（与 KDE/GNOME 等桌面应用一致）：

        1. 栈首始终是系统默认 UI 字体（``QApplication.font()`` 经 ``QFontInfo``
           解析）。用户在系统里配置的字体（含其内置的中英文与其它文字）就是
           应用字体，界面因此与桌面其它应用一致，也不会出现"应用字体与系统
           不符"的观感问题。
        2. 仅当系统字体**自身不含中文字形**时，才在栈尾补一个已安装的 CJK 族
           兜底，避免中文落到系统默认回退（Windows 上常命中宋体）。
        3. 系统字体完全取不到时（Qt 平台主题缺失等）才退回到内置候选列表。

        解析结果进程内缓存。
        """
        if ThemeManager._font_families_cache is not None:
            return list(ThemeManager._font_families_cache)

        system_default = None
        try:
            from PySide6.QtGui import QFontInfo
            app = QApplication.instance()
            if app is not None:
                system_default = QFontInfo(app.font()).family() or None
        except Exception as e:
            self.logger.warning(f"Failed to read system default font: {e}")

        installed = set()
        try:
            from PySide6.QtGui import QFontDatabase
            installed = set(installed_font_families(QFontDatabase))
        except Exception as e:
            self.logger.warning(f"QFontDatabase unavailable: {e}")

        families = []
        if system_default:
            families.append(system_default)

        # 系统字体不含中文时才补 CJK 兜底族
        if not self._font_renders_cjk(system_default):
            cjk = next((c for c in self._CJK_FAMILY_CANDIDATES if c in installed), None)
            if cjk:
                families.append(cjk)

        if not families:
            # 拿不到系统字体（无平台主题/无 QApplication）：用内置候选兜底
            fallback = next((c for c in self._FALLBACK_LATIN_FAMILIES if c in installed), None)
            if fallback:
                families.append(fallback)
            cjk = next((c for c in self._CJK_FAMILY_CANDIDATES if c in installed), None)
            if cjk and cjk not in families:
                families.append(cjk)

        if not families:
            families.append("Arial")

        self.logger.info(
            f"Resolved app font families: {families} (system default={system_default!r})")
        ThemeManager._font_families_cache = families
        return list(families)

    @staticmethod
    def _font_renders_cjk(family) -> bool:
        """该字体族**自身**是否带中文字形（不依赖系统回退链）。

        用于判断"系统默认字体是否已覆盖中文"：已覆盖时把西文族提前，让它专门
        承担拉丁字形（CJK 字体的西文子集字重偏重且常缺真 Bold）；未覆盖时保持
        用户设置的族在首位，仅在栈尾补 CJK 族兜底。

        实现要点：**不能用** ``QFontMetrics.inFont("中")``——fontconfig 会为缺失
        字形挂上回退字体，导致任何西文族都被误判为"支持中文"（DejaVu Sans 实测
        就会误判）。``QRawFont`` 直接加载目标字体文件、不参与回退，是准确判据。
        """
        if not family:
            return False

        try:
            from PySide6.QtGui import QFont, QRawFont

            raw = QRawFont.fromFont(QFont(str(family)))
            if raw.isValid():
                return bool(raw.supportsCharacter("中"))
        except Exception as e:
            logger.debug(f"QRawFont probe failed for {family!r}: {e}")

        # QRawFont 不可用时的兜底：按族名判断（覆盖主流 CJK UI 字体名）
        low = str(family).lower()
        return any(name.lower() in low for name in ThemeManager._CJK_FAMILY_CANDIDATES)

    def font_family(self) -> str:
        """字体栈（带引号、逗号分隔），供 QSS 与富文本 HTML 直接注入。

        Qt 6 的 QSS 与富文本引擎都接受逗号字体栈（按字形回退），因此这里返回
        完整栈而不是单一族名：英文命中栈首的西文族（字重正常），中文回退到
        栈内的 CJK 族。需要"单一族名"的 API（``QFont(...)`` 等）请用
        :meth:`font_family_name`。
        """
        stack = ", ".join(f"'{name}'" for name in self.font_families())
        return stack or "'Arial'"

    def qfont(self, point_size: int = 10, weight=None):
        """构造带完整字体栈的 ``QFont``（西文族在前、CJK 族随后）。

        ``QFont("Arial")`` 这类单族构造不参与跨族回退：中文会落到系统默认回退
        （Windows 上常为宋体），英文则失去栈首西文族的字重优势。凡需要用 QFont
        绘制文本的地方统一走这里（QPainter 自绘、委托等）。
        """
        from PySide6.QtGui import QFont

        font = QFont()
        if point_size:
            font.setPointSize(point_size)
        if weight is not None:
            font.setWeight(weight)

        families = self.font_families()
        if hasattr(font, "setFamilies") and families:
            font.setFamilies(families)
        else:
            font.setFamily(self.font_family_name())
        return font

    def font_family_name(self) -> str:
        """统一字体的**裸族名**（不带引号），供 QFont 等需要纯族名的 API 使用。

        硬编码 "Segoe UI" 在 Linux 上根本不存在，Qt 会静默回退到默认族，
        与 QSS 注入的族不一致（同一界面出现两种字体）。所有 QFont(...)
        构造点统一走这里。
        """
        from src.ui.components.text_formatter import pick_cjk_font_family
        return pick_cjk_font_family(self.font_families())

    def mono_font_family(self) -> str:
        """等宽字体裸族名（不带引号），供 QFont 构造使用。"""
        from src.ui.components.text_formatter import mono_font_family_css
        return mono_font_family_css().strip("'")

    def _get_system_theme(self) -> str:
        """判断桌面深浅色偏好；无 GUI 应用实例（API 模式）时回落 dark。

        优先使用 Qt 6.5+ 的 ``QStyleHints.colorScheme()``（由平台主题/portal
        提供，Linux 桌面下同样准确）；不可用时再回退到调色板亮度判断。

        注意：调色板判断必须在 qdarktheme 覆盖 QApplication 调色板**之前**
        执行，否则读到的是库下发的默认浅色调色板，会把暗色桌面误判为浅色。
        """
        app = QApplication.instance()
        if not isinstance(app, QApplication):
            return "dark"

        try:
            from PySide6.QtGui import QGuiApplication

            scheme = QGuiApplication.styleHints().colorScheme()
            if scheme == Qt.ColorScheme.Dark:
                return "dark"
            if scheme == Qt.ColorScheme.Light:
                return "light"
        except Exception as e:
            self.logger.debug(f"colorScheme() unavailable, falling back to palette: {e}")

        palette = app.palette()
        bg_color = palette.color(palette.ColorRole.Window)
        return "light" if bg_color.lightness() > 128 else "dark"


    #: 目录内容索引缓存：dir -> {小写名: 真实名}。资源目录运行期不变，
    #: 缓存后大小写回退查找退化为一次字典命中（零 syscall）。
    _dir_index_cache: dict = {}

    @staticmethod
    def _dir_index(path: str) -> dict:
        index = ThemeManager._dir_index_cache.get(path)
        if index is None:
            try:
                index = {entry.lower(): entry for entry in os.listdir(path)}
            except OSError:
                index = {}
            ThemeManager._dir_index_cache[path] = index
        return index

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

        target = os.path.join(base_dir, *paths)
        if not paths or os.path.exists(target):
            return target

        # 历史调用点混用了 "Assets/Icons" 与 "assets/icons" 两种拼写：Windows /
        # macOS 的文件系统不区分大小写，二者都能命中；Linux 区分大小写，按错误
        # 拼写查找会直接失败（症状是整套图标、Mermaid 脚本静默消失）。这里在
        # 精确路径不存在时按组件逐级做大小写不敏感回退，一次性覆盖所有调用点。
        return ThemeManager._resolve_case_insensitive(base_dir, paths) or target

    @staticmethod
    def _resolve_case_insensitive(base_dir: str, paths) -> str:
        """按大小写不敏感方式逐级解析路径；任一级缺失返回空串。"""
        current = base_dir
        for part in paths:
            real = ThemeManager._dir_index(current).get(str(part).lower())
            if real is None:
                return ""
            current = os.path.join(current, real)
        return current

    def set_theme(self, theme_name: str):
        theme_name = theme_name.lower()

        if theme_name == "auto":
            theme_name = self._get_system_theme()

        if theme_name in self.themes and self.current_theme != theme_name:
            self.current_theme = theme_name
            self.theme_changed.emit()

        # 调色板必须与样式表同源：qdarktheme 只下发样式表（其调色板刻意保留为
        # 系统默认），凡样式表未覆盖的部件都会按调色板取色。
        self.apply_palette()

    def apply_palette(self) -> None:
        """把当前主题色写入 QApplication 调色板。

        背景：``qdarktheme.setup_theme()`` 内部调用
        ``load_palette(..., for_stylesheet=True)``，该分支**返回系统默认（浅色）
        调色板**——库的设计意图是"颜色全部由样式表负责"。结果是：样式表覆盖到
        的部件是深色，而窗口留白、滚动区视口、工具提示、原生文件对话框、未显式
        配色的容器等按调色板取色，仍然保持浅色，在 Linux（Breeze/Adwaita 等
        平台样式参与绘制）上表现为"深色模式里到处冒出浅色块"。

        这里按 ThemeManager 的主题色显式下发调色板，使未被样式表覆盖的部分也
        与主题一致；浅色/深色切换时同步刷新。
        """
        from PySide6.QtGui import QColor, QPalette

        app = QApplication.instance()
        # API 模式（--api-server）只有 QCoreApplication：没有 setPalette/setStyle，
        # 此处直接跳过，避免无谓的 AttributeError 日志。
        if not isinstance(app, QApplication):
            return

        role = QPalette.ColorRole
        palette = QPalette()
        palette.setColor(role.Window, QColor(self.color('bg_main')))
        palette.setColor(role.WindowText, QColor(self.color('text_main')))
        palette.setColor(role.Base, QColor(self.color('bg_input')))
        palette.setColor(role.AlternateBase, QColor(self.color('bg_card')))
        palette.setColor(role.ToolTipBase, QColor(self.color('bg_card')))
        palette.setColor(role.ToolTipText, QColor(self.color('text_main')))
        palette.setColor(role.Text, QColor(self.color('text_main')))
        palette.setColor(role.PlaceholderText, QColor(self.color('text_muted')))
        palette.setColor(role.Button, QColor(self.color('btn_bg')))
        palette.setColor(role.ButtonText, QColor(self.color('text_main')))
        palette.setColor(role.BrightText, QColor(self.color('danger')))
        palette.setColor(role.Link, QColor(self.color('accent')))
        palette.setColor(role.LinkVisited, QColor(self.color('accent_hover')))
        palette.setColor(role.Highlight, QColor(self.color('accent')))
        palette.setColor(role.HighlightedText, QColor(self.color('selection_fg')))
        # Fusion 风格会用 Light/Mid/Dark 画立体边框；一律收敛到主题边框色，
        # 否则深色主题下会出现发亮的 3D 边线。
        for flat_role, color_key in (
            (role.Light, 'btn_hover'), (role.Midlight, 'btn_bg'),
            (role.Mid, 'border'), (role.Dark, 'border'), (role.Shadow, 'bg_main'),
        ):
            palette.setColor(flat_role, QColor(self.color(color_key)))

        disabled = QPalette.ColorGroup.Disabled
        for disabled_role, color_key in (
            (role.WindowText, 'text_muted'), (role.Text, 'text_muted'),
            (role.ButtonText, 'text_muted'), (role.Button, 'bg_main'),
            (role.Base, 'bg_main'), (role.PlaceholderText, 'text_muted'),
        ):
            palette.setColor(disabled, disabled_role, QColor(self.color(color_key)))

        app.setPalette(palette)

    def apply_application_theme(self, theme_name: str) -> None:
        """把主题应用到整个 QApplication（样式 + 调色板），全局唯一入口。

        调用方只需给出 ``dark`` / ``light`` / ``auto``：

        1. 先把基础样式固定为 Fusion。qdarktheme 的样式表与度量按 Fusion 设计，
           而 Linux 桌面常注入 Breeze/Adwaita 等平台样式，二者叠加会出现尺寸与
           配色错位（例如深色下部分控件仍按平台调色板绘制）。
        2. 应用 qdarktheme 样式表（仅首次会包裹代理样式）。
        3. 切换 ThemeManager 主题并下发同源调色板。
        """
        app = QApplication.instance()
        if not isinstance(app, QApplication):
            # 未创建 QApplication（如 API 模式/测试）时不适用 GUI 主题。
            self.logger.debug("apply_application_theme skipped: no QApplication instance.")
            return

        # "auto" 必须在 qdarktheme 覆盖调色板之前解析成具体主题，否则
        # _get_system_theme 读到的是库下发的默认浅色调色板，暗色桌面会误判。
        resolved = str(theme_name or "dark").lower()
        if resolved == "auto":
            resolved = self._get_system_theme()

        # qdarktheme 会设置该属性表示样式已由它接管；此时不再覆盖基础样式，
        # 否则会把它的代理样式替换掉。
        if not app.property("_qdarktheme_use_setup_style"):
            try:
                app.setStyle("Fusion")
            except Exception as e:
                self.logger.warning(f"Could not switch to Fusion style: {e}")

        try:
            import qdarktheme

            qdarktheme.setup_theme(resolved)
        except Exception as e:
            self.logger.error(f"Failed to apply qdarktheme stylesheet: {e}")

        self.set_theme(resolved)
        # 主题名未变化时 set_theme 不会重复下发调色板，这里兜底一次，保证
        # 首次启动/样式切换后调色板一定是当前主题的。
        self.apply_palette()

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
            font-weight: {strong_weight_css()} !important; font-size: 14px;
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

        QScrollBar:horizontal {{
            background: {self.color('bg_main')};
            height: 8px;
            border-top: 1px solid {self.color('border')};
            margin: 0px;
        }}
        QScrollBar::handle:horizontal {{
            background: {self.color('text_muted')};
            min-width: 20px;
            border-radius: 4px;
        }}
        QScrollBar::handle:horizontal:hover {{
            background: {self.color('accent')};
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            width: 0px;
        }}

        QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled, 
        QLineEdit:read-only, QPlainTextEdit:read-only {{
            background-color: {self.color('bg_main')} !important;
            color: {self.color('text_muted')} !important;
            border: 1px dashed {self.color('border')} !important;
        }}

        QLabel[cssClass="hint"] {{ color: {self.color('text_muted')}; font-size: 11px; }}
        QLabel[cssClass="warning"] {{ color: {self.color('warning')}; font-weight: {strong_weight_css()}; }}
        QLabel[cssClass="status-success"] {{ color: {self.color('success')}; font-weight: {strong_weight_css()}; }}
        QLabel[cssClass="status-error"] {{ color: {self.color('danger')}; font-weight: {strong_weight_css()}; }}
        QLabel[cssClass="status-pending"] {{ color: {self.color('warning')}; }}

        QPushButton[cssClass="icon-btn"] {{ background: transparent; border: none; }}
        QPushButton[cssClass="link-btn"] {{
            background: transparent; color: {self.color('accent')};
            text-align: left; border: none; font-weight: {strong_weight_css()};
        }}
        """

    def apply_class(self, widget, class_name):
        widget.setProperty("cssClass", class_name)
        widget.style().unpolish(widget)
        widget.style().polish(widget)


# --------------------------------------------------------------------------- #
#  字重档位（QSS / 富文本共用，避免各处写死 font-weight: bold）
# --------------------------------------------------------------------------- #

def strong_weight_css() -> str:
    """强调字重（比正文重一档）的 ``font-weight`` 值，可直接拼进样式表。

    界面里大量 `font-weight: bold` 声明把控件文案（导航项、表头、状态标签、按钮）
    渲染成标题级的 700，正文尺寸下笔画成倍加粗、整屏发黑。统一改用本函数后，字重
    取自字体**实际具备**的中间字面：有中间字面的字体栈（如 Noto Sans CJK 可变字体）
    取 500 档，只有 Regular/Bold 的字体栈自动退回 ``bold``——层级不会丢，只是不再
    无差别地用标题字重。

    解析细节与跨平台落点见
    :func:`src.ui.components.text_formatter.emphasis_font_weight`；结果已缓存，
    可放心在每次构建样式表时调用。
    """
    from src.ui.components.text_formatter import emphasis_font_weight

    return emphasis_font_weight() or "bold"


def title_weight_css() -> str:
    """标题字重的 ``font-weight`` 值（比强调档再重一档，通常 600）。

    用于页面/区块标题这类需要与正文拉开层级的场景：字号本身已提供主要层级，字重
    只需再重一档即可，用 900/700 会显得笨重。字体栈不支持时自动退回强调档或原生
    ``bold``。解析细节见
    :func:`src.ui.components.text_formatter.title_font_weight`。
    """
    from src.ui.components.text_formatter import title_font_weight

    return title_font_weight() or "bold"


def overlay_scrollbar_qss(thickness: int = 8, handle_color: str = None) -> str:
    """Overlay 风格滚动条：不操作时不可见，鼠标移到滚动条上才显形。

    常显滚动条会在图文流里留下一条贯穿整屏的竖线（对话区、代码块、长表格尤其
    明显）。这里把滑块常态设为**完全透明**，鼠标进入滚动条区域或按住拖动时才上色，
    轨道与箭头始终不占视觉空间——与 macOS / 现代 Web 的 overlay 滚动条一致。

    :param thickness: 滚动条粗细（px），横竖一致。
    :param handle_color: 滑块颜色，缺省取当前主题的 ``text_muted``。
    """
    tm = ThemeManager()
    color = handle_color or tm.color('text_muted')
    return f"""
        QScrollBar:vertical, QScrollBar:horizontal {{
            background: transparent; border: none; margin: 0px;
        }}
        QScrollBar:vertical {{ width: {thickness}px; }}
        QScrollBar:horizontal {{ height: {thickness}px; }}
        QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
            background: transparent; border: none; border-radius: {thickness // 2}px;
        }}
        QScrollBar::handle:vertical {{ min-height: 28px; }}
        QScrollBar::handle:horizontal {{ min-width: 28px; }}
        /* 鼠标进入滚动条区域 → 滑块显形（扩大命中范围；个别平台若不识别该组合，
           仍有下面的标准 :hover 规则兜底） */
        QScrollBar:hover::handle:vertical, QScrollBar:hover::handle:horizontal {{
            background: {hex_to_rgba(color, 0.35)};
        }}
        QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{
            background: {hex_to_rgba(color, 0.5)};
        }}
        QScrollBar::handle:vertical:pressed, QScrollBar::handle:horizontal:pressed {{
            background: {hex_to_rgba(color, 0.7)};
        }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; width: 0px; border: none; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    """