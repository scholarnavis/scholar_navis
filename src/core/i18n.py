"""界面多语言（i18n）的**唯一实现**。

设计约定
--------
* **英文即源语言**：源码里的英文原串既是默认显示文本，也是查表键；
  因此界面文案的"唯一来源"始终是调用点，译文只是叠加的可选覆盖。
* **译文集中存放**：所有语言的 ``TRANSLATIONS`` 字典放在
  :mod:`src.core.locales` 里（一个静态模块）。
  刻意**不用** JSON 数据文件，也**不用**按语言拆子模块 + 动态 ``import_module``：
  前者需要三平台各维护一份数据文件路径（PyInstaller ``--add-data``），
  后者会遇到 PyInstaller 静态分析不到动态导入、译文在冻结包里丢失的经典问题。
* **查不到就回退原文**：:func:`tr` 未命中译文时原样返回英文，
  保证漏翻只表现为"显示英文"，绝不抛错、不产生空串。
* **语言取值**：``auto``（跟随系统）/ ``en`` / ``zh_CN``；
  系统语言既非简体中文、也非英语时，``auto`` 一律回退英文。
* **切换需重启**：主界面在启动时一次性构建，本模块**不**提供运行期热重载；
  改变语言后由设置页提示用户重启。
"""

import logging
import os

from src.core.locales import TRANSLATIONS

logger = logging.getLogger("i18n")

#: 语言设置的"跟随系统"哨兵值（也是默认值）。
AUTO = "auto"
#: 源语言：源码中的原串即该语言的文本，无独立译表。
SOURCE_LANGUAGE = "en"
#: 当前受支持的语言代码（``en`` 为源语言，不需要译表）。
SUPPORTED_LANGUAGES = ("en", "zh_CN")
#: 语言代码 -> 界面上的原生名称（语言名按惯例用其自身文字展示，不参与翻译）。
LANGUAGE_NATIVE_NAMES = {"en": "English", "zh_CN": "简体中文"}

#: 繁体中文标识片段：本程序只支持简体中文，繁体系统回退英文。
_TRADITIONAL_MARKERS = ("hant", "zh_tw", "zh_hk", "zh_mo", "zh_cht")


def detect_system_language() -> str:
    """探测操作系统语言标识；全部探测手段都失败时返回空串。

    优先 Qt（与 GUI 栈同源，三平台行为一致），再退回环境变量与 :mod:`locale`。
    """
    candidates = []

    try:
        from PySide6.QtCore import QLocale

        candidates.append(QLocale.system().name())
    except Exception as e:  # Qt 不可用（如 API 模式早期）不应影响语言解析
        logger.debug("QLocale probe unavailable: %s", e)

    for env_key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(env_key)
        if value:
            # 形如 "zh_CN.UTF-8"：去掉编码后缀与 POSIX 的 "C"/"C.UTF-8"
            candidates.append(value.split(".", 1)[0])

    try:
        import locale as _locale

        candidates.append(_locale.getlocale()[0] or "")
    except Exception as e:
        logger.debug("locale probe unavailable: %s", e)

    for candidate in candidates:
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return ""


def normalize_language(raw: str) -> str:
    """把任意语言/区域标识归一为受支持的语言代码。

    只认简体中文（``zh`` 系，且非繁体）与英语（``en`` 系）；其余一律 ``en``。
    """
    if not raw:
        return SOURCE_LANGUAGE

    code = str(raw).strip().replace("-", "_").lower()
    if code.startswith("zh"):
        if any(marker in code for marker in _TRADITIONAL_MARKERS):
            return SOURCE_LANGUAGE
        return "zh_CN"
    if code.startswith("en"):
        return "en"
    return SOURCE_LANGUAGE


def resolve_language(setting: str) -> str:
    """把用户设置解析为**有效语言代码**。

    ``auto``（或缺省）时跟随系统语言；显式设置则归一后使用。
    """
    if not setting or str(setting).strip().lower() == AUTO:
        return normalize_language(detect_system_language())
    return normalize_language(setting)


def _load_table(language: str) -> dict:
    """取某语言的译表；源语言或缺失语言返回空表（即全部回退英文）。"""
    if language == SOURCE_LANGUAGE:
        return {}
    table = TRANSLATIONS.get(language)
    if not isinstance(table, dict):
        logger.warning("No translation table for language '%s'; falling back to English.",
                       language)
        return {}
    return {str(k): str(v) for k, v in table.items()}


class LanguageManager:
    """进程级单例：持有当前有效语言与译表，负责 :func:`tr` 的查表。

    懒初始化：首次 :meth:`translate` 时才读取配置并加载译表，
    因此可以在任意模块顶部安全地 ``from src.core.i18n import tr``。
    """

    _instance = None

    def __init__(self):
        self._setting = AUTO
        self._language = SOURCE_LANGUAGE
        self._table = {}
        self._loaded = False
        #: 已告警过的缺失键：同一键每个进程只告警一次，避免启动期刷屏。
        self._missing_keys = set()

    @classmethod
    def instance(cls) -> "LanguageManager":
        if cls._instance is None:
            cls._instance = LanguageManager()
        return cls._instance

    # ---- 初始化 ----

    def _read_setting(self) -> str:
        """从用户配置读取语言设置；配置不可用时按 ``auto`` 处理。"""
        try:
            from src.core.config_manager import ConfigManager

            return str(ConfigManager().user_settings.get("language", AUTO) or AUTO)
        except Exception as e:
            logger.warning("Failed to read language setting, defaulting to auto: %s", e)
            return AUTO

    def initialize(self) -> None:
        """按当前配置完成一次解析（幂等；重复调用不会重载）。"""
        if self._loaded:
            return
        self.apply(self._read_setting())

    def apply(self, setting: str) -> None:
        """显式设置语言并重载译表（供测试或未来热切换预留）。"""
        self._setting = str(setting or AUTO)
        self._language = resolve_language(self._setting)
        self._table = _load_table(self._language)
        self._missing_keys = set()
        self._loaded = True
        logger.info(
            "Interface language resolved: setting=%s effective=%s translations=%d",
            self._setting, self._language, len(self._table))

    # ---- 查询 ----

    @property
    def setting(self) -> str:
        """原始设置值（``auto`` / ``en`` / ``zh_CN``）。"""
        return self._setting

    @property
    def language(self) -> str:
        """解析后的有效语言代码。"""
        return self._language

    def translate(self, text: str) -> str:
        """翻译单条界面文案；未命中时记录一次警告并返回原文。

        仅在**非源语言**（即确实需要译表）时告警：源语言 ``en`` 下所有
        ``tr()`` 都命中不到，逐条告警没有信息量。同一缺失键每个进程只告警一次。
        """
        if not text:
            return text
        if not self._loaded:
            self.initialize()
        if self._language == SOURCE_LANGUAGE:
            return text

        translated = self._table.get(text)
        if not translated:  # 未登记或译文为空串，均视为未命中
            self._warn_missing(text)
            return text
        return translated

    def _warn_missing(self, text: str) -> None:
        """对未命中的键告警（按唯一键去重）。"""
        if text in self._missing_keys:
            return
        self._missing_keys.add(text)
        logger.warning(
            "Missing %s translation for UI string: %r "
            "(register it in src/core/locales.py)", self._language, text)


def tr(text: str) -> str:
    """界面文案翻译入口（等价于 :meth:`LanguageManager.translate`）。"""
    return LanguageManager.instance().translate(text)


def current_language() -> str:
    """当前有效语言代码的便捷访问。"""
    return LanguageManager.instance().language
