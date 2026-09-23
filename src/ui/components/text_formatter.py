import re
import logging
import markdown
import os
import sys
import tempfile
import shutil
import hashlib
import json as _json
from base64 import b64decode as _b64decode
from functools import lru_cache
from html.parser import HTMLParser
from urllib.parse import urlparse, parse_qs
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from src.core.theme_manager import ThemeManager, installed_font_families
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)

def _rgba(hex_color: str, alpha: float) -> str:
    """``#RRGGBB`` → ``rgba(r, g, b, a)``；非 hex 值原样返回。

    Qt 富文本支持 ``rgba()``，用于生成"低透明度叠加"的层次底色（表头、
    引用块等）。非法输入直接回退原值，避免样式整体失效。
    """
    color = str(hex_color).strip()
    if not color.startswith("#") or len(color) != 7:
        return color
    try:
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
    except ValueError:
        return color
    return f"rgba({r}, {g}, {b}, {max(0.0, min(1.0, float(alpha)))})"


def _icon_uri(icon_name: str) -> str:
    """把资源图标解析为绝对 ``file://`` URI，供富文本 ``<img src=...>`` 使用。

    相对路径（``assets/icons/x.svg``）由 QTextBrowser 相对"文档基址"解析，而
    基址默认是进程工作目录：换一个目录启动、或在大小写敏感的文件系统上，
    图标就会静默丢失。这里统一解析为绝对 URI，与工作目录、平台大小写无关。
    """
    path = ThemeManager.get_resource_path("Assets", "Icons", f"{icon_name}.svg")
    if not os.path.exists(path):
        logger.debug(f"Icon not found for rich text: {icon_name}.svg")
        return ""
    return QUrl.fromLocalFile(path).toString()


def resolve_qt_font_families() -> list:
    """返回应用生效字体族列表（代理 ``ThemeManager.font_families``）。

    字体源已统一到 ThemeManager：首选系统默认 UI 字体的真实命中族
    （QFontInfo 解析），附系统已装 CJK 族作按序回退。本函数仅为保持
    既有调用点（chat_bubble 等）兼容的薄代理。
    """
    try:
        return ThemeManager().font_families()
    except Exception as e:
        logger.warning(f"Failed to resolve app font families, fallback to Arial: {e}")
        return ["Arial"]


def qt_font_family_css() -> str:
    """返回可内嵌进 Qt 富文本 style 属性的字体栈（带引号、逗号分隔）。

    Qt 6 的富文本引擎支持 CSS 字体栈并按字形逐个回退（``QTextCharFormat``
    会把整条 ``font-family`` 列表交给 QFont 的字体族列表），因此这里直接下发
    ``'西文族', '中文族'``：英文命中栈首西文族（字重正常、有原生粗体），中文
    回退到 CJK 族。旧实现注入单一 CJK 族，英文由 CJK 字体的拉丁子集渲染，
    小字号下合成加粗、笔画粘连，是"英文又粗又难分辨"的根因。
    """
    return ThemeManager().font_family()


#: 已知含中日韩字形的族名片段（小写）。均为操作系统自带字体，
#: 用于在解析出的族名列表中挑选可直接渲染中文的族。
_CJK_FAMILY_HINTS = (
    "microsoft yahei", "pingfang sc", "hiragino sans gb", "noto sans cjk",
    "source han sans", "wenquanyi", "dengxian", "simhei",
)


def pick_cjk_font_family(families: list) -> str:
    """从解析出的族名列表中挑第一个含中日韩字形的族。

    背景：Qt 富文本对"显式西文单族（如 Segoe UI）+ 中文内容"的渲染
    走系统字体回退，Windows 上常命中衬线宋体（SimSun），导致气泡
    中英混排时中文显示为宋体。把所有单族注入点（HTML 内联样式、QSS、
    文档默认字体主族）统一改为中文字形族（微软雅黑/苹方等，自身含
    西文字形），中英文均由该族直接渲染，系统回退链不再参与。

    找不到已知 CJK 族时退回首族（栈被用户自定义且无 CJK 字体的环境）。
    """
    for name in families:
        low = name.lower()
        if any(hint in low for hint in _CJK_FAMILY_HINTS):
            return name
    return families[0] if families else "Arial"


#: 等宽字体候选（按平台常见度排序：Windows → macOS → Linux）。
#: 均为操作系统自带或发行版常装字体，取第一个已安装者。
_MONO_FAMILY_CANDIDATES = (
    "Consolas", "Cascadia Mono", "Menlo", "DejaVu Sans Mono",
    "Liberation Mono", "Noto Sans Mono", "Courier New",
)

#: 等宽族解析缓存（None = 未解析）。QFontDatabase 查询有开销且系统字体
#: 运行期不变，进程内解析一次即可；流式渲染高频调用依赖此缓存。
_mono_family_cache = None


def mono_font_family_css() -> str:
    """返回等宽单族名（带引号），供代码块 / 行内代码 HTML 直接注入。

    Qt 富文本不支持字体栈（同 CJK 族解析的限制），多族名会导致声明
    整体失效并回退正文字体，使代码失去等宽形态。这里按平台常见度在
    已安装字体中挑第一个等宽候选单族注入；结果进程内缓存，极端环境
    兜底 Courier New（各平台最低限度可用的等宽族）。
    """
    global _mono_family_cache
    if _mono_family_cache is None:
        resolved = "Courier New"
        try:
            from PySide6.QtGui import QFontDatabase
            installed = set(installed_font_families(QFontDatabase))
            resolved = next(
                (c for c in _MONO_FAMILY_CANDIDATES if c in installed), resolved)
        except Exception as e:
            logger.warning(f"Failed to resolve monospace font, fallback to Courier New: {e}")
        logger.info(f"Resolved monospace font family: {resolved}")
        _mono_family_cache = resolved
    return f"'{_mono_family_cache}'"


#: 强调字重候选：(CSS 数值, QFont.Weight 成员名, 中间字重的独立族名后缀)。
#: 按"由轻到重"试探，目标是让强调"只比正文重一档"，而不是直接跳到标题级的 Bold。
#: 后缀用于把非 RIBBI 字重登记成独立族名的平台（Qt 在 Windows 上把 Semibold 记作
#: "<族名> Semibold"，此时光改 font-weight 取不到该字面，族名也得一起换）。
_EMPHASIS_WEIGHT_CANDIDATES = (
    (500, "Medium", (" Medium",)),
    (600, "DemiBold", (" SemiBold", " Semibold", " DemiBold")),
)

#: 字重实测样张：中英混排，覆盖主族（西文）与兜底族（CJK）的真实字形。
#: 判据是墨迹密度（前景像素占比），字重越大笔画越粗、墨迹越多，与字体设计无关。
_WEIGHT_PROBE_SAMPLE = "Hamburgefonstiv 强调"

#: 探针渲染字号（像素）：贴近界面正文字号，避免"大字号才显现的字重差"被误判。
_WEIGHT_PROBE_PIXEL_SIZE = 16

#: 已安装字体族名缓存（None = 未查询）。
_installed_families_cache = None

#: 强调样式解析缓存：``(css_weight, css_family)``；两者皆空表示退回 Qt 原生粗体。
_emphasis_style_cache = None


@lru_cache(maxsize=128)
def _family_ink(family: str, weight_attr=None) -> float:
    """该族在指定字重下的**墨迹密度**（前景像素占比）；``<0`` 表示测不出来。

    为什么判据是实测墨迹密度，而不是推进宽度或字重元数据：

    1. 字重元数据不可信（``QFontInfo.weight`` / ``QRawFont.weight``）：静态字体
       收到 Medium 请求时，部分平台会把请求值原样报回，实际匹配到的却仍是
       Regular——"元数据说支持"与"渲染结果真的不同"是两件事；
    2. 推进宽度同样不可靠，而且方向不固定：CJK 优先字体（Noto Sans CJK、思源
       黑体等）的西文宽度随字重**反向**变化（实测 Noto Sans CJK SC，16px：
       Thin 120.92 > Light 120.88 > Regular 120.83 > Medium 120.73 >
       DemiBold/Bold 120.66 > Black 120.61）。旧实现以"强调必须比正文更宽"
       判定，在这类字体上恒不成立，于是把实际可用的 Medium 误判为不可用，整体
       退回 Qt 原生粗体(700)——用户看到的现象就是"强调文字依然很粗"；
    3. 墨迹密度与字重单调正相关（实测同一字体 300/400/500/600/700/900 →
       0.069/0.088/0.101/0.118/0.148/0.158），且不依赖字体设计意图。

    ``weight_attr=None`` 表示该族的默认（正文）字重。测量退化（密度非正，如族名
    不存在时落到替身字体或渲染失败）返回 -1，由调用方判定为不可用。

    结果按 (族, 字重) 缓存：多族 × 多候选 × 三档基准合计只测十余次，单次为
    毫秒级（灰度图越小越快）。
    """
    try:
        from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter

        font = QFont(str(family))
        if weight_attr:
            font.setWeight(getattr(QFont.Weight, weight_attr))
        font.setPixelSize(_WEIGHT_PROBE_PIXEL_SIZE)

        metrics = QFontMetricsF(font)
        width = int(metrics.horizontalAdvance(_WEIGHT_PROBE_SAMPLE)) + 4
        height = int(metrics.height()) + 4
        if width <= 4 or height <= 4:
            return -1.0

        image = QImage(width, height, QImage.Format_Grayscale8)
        image.fill(0xFF)
        painter = QPainter(image)
        try:
            painter.setFont(font)
            painter.setPen(QColor("black"))
            painter.drawText(2, int(metrics.ascent()) + 2, _WEIGHT_PROBE_SAMPLE)
        finally:
            painter.end()

        # 灰度 8 位：白 255、黑 0。阈值 200 忽略抗锯齿边缘，只统计笔画主体。
        # 行末对齐填充为白，计入分母不影响单调性。
        data = bytes(image.constBits())
        if not data:
            return -1.0
        ink = sum(1 for value in data if value < 200) / len(data)
        return ink if ink > 0 else -1.0
    except Exception as e:
        logger.debug("Weight probe failed (family=%r weight=%r): %s", family, weight_attr, e)
        return -1.0


def _installed_families() -> set:
    """已安装字体族名集合（进程内缓存；查询失败返回空集合）。"""
    global _installed_families_cache
    if _installed_families_cache is None:
        try:
            from PySide6.QtGui import QFontDatabase
            _installed_families_cache = set(installed_font_families(QFontDatabase))
        except Exception as e:
            logger.debug("Font family list unavailable: %s", e)
            _installed_families_cache = set()
    return _installed_families_cache


@lru_cache(maxsize=64)
def _suffixed_family(family: str, suffix: str) -> str:
    """取"独立族名版"的中间字重族；查不到则原样返回 ``family``。

    平台差异：fontconfig（Linux）/ CoreText（macOS）把字重当作族内属性，中间字面
    只能靠 ``font-weight`` 取；Qt 的 Windows 字体库则把非 RIBBI 字重登记成独立
    族名（``Segoe UI`` 的 Semibold 字面即族名 ``Segoe UI Semibold``），这类平台
    必须连族名一起换。查不到该族名时是恒等映射，不会引入新字体。
    """
    if not suffix:
        return family
    candidate = f"{family}{suffix}"
    return candidate if candidate in _installed_families() else family


def _stack_accepts_weight(families: list, weight_attr: str, emphasis_families=None) -> bool:
    """强调配置是否可用（跨平台安全边界）。

    ``families`` 是正文字体栈；``emphasis_families`` 是准备落到强调文本上的族名栈
    （等长；None 表示沿用原族名、只改字重）。判定基准（Regular/Bold）始终量
    ``families``，被测对象量 ``emphasis_families``——独立族名版自身往往只有一个
    字面，拿它自己比 Regular/Bold 会得出"三档全等"的假象。

    判据是**墨迹密度**（见 :func:`_family_ink`；不用推进宽度，CJK 字体的宽度随
    字重反向变化，会把可用的中间字重全部误杀）。规则（顺序即优先级）：

    1. **主族必须真的变重一档**：强调密度严格落在正文主族的 Regular 与 Bold
       密度之间。主族（``families[0]``）渲染绝大部分正文字形，它没变重这次调整
       就没有收益；若平台把它匹配成比 Regular 更轻的字面（如 Light/Semilight，
       Windows 上请求 500 时确实可能发生），强调会比正文还淡，直接否决。

    2. **兜底族（栈尾 CJK 族）必须仍被加重**：强调密度要大于正文密度。
       Windows 微软雅黑只有 Regular/Bold，请求 500/600 会落到哪一档取决于匹配
       规则：落到 Bold（中文仍是粗体）可以接受，落到 Regular 就会"中文强调凭空
       消失、只剩西文加粗"，中英观感不一致，必须否决——此时整体退回 Qt 原生
       粗体，宁可都粗，也不要一半有一半没有。

    任一密度测不出来（``<0``）时按"不可用"处理：主族不可用即否决；兜底族不可用
    则跳过该族（不因测不到而误判可用性）。
    """
    if not families:
        return False
    emphasis = list(emphasis_families) if emphasis_families else list(families)
    if len(emphasis) != len(families):
        return False

    regular = _family_ink(families[0])
    bold = _family_ink(families[0], "Bold")
    actual = _family_ink(emphasis[0], weight_attr)
    details = [f"{families[0]}->{emphasis[0]}={regular:.4f}/{actual:.4f}/{bold:.4f}"]
    if not (regular > 0 and actual > 0 and bold > 0):
        logger.debug("Emphasis probe skipped: primary %r->%r unmeasurable for %s (%.4f/%.4f/%.4f)",
                     families[0], emphasis[0], weight_attr, regular, actual, bold)
        return False
    accepted = regular < actual < bold

    for body_family, emph_family in zip(families[1:], emphasis[1:]):
        base = _family_ink(body_family)
        cur = _family_ink(emph_family, weight_attr)
        details.append(f"{body_family}->{emph_family}={base:.4f}>{cur:.4f}")
        if base > 0 and 0 < cur <= base:
            logger.debug("Emphasis probe rejected: fallback %r->%r is no heavier than body "
                         "at %s (%.4f -> %.4f)", body_family, emph_family, weight_attr, base, cur)
            accepted = False

    logger.debug("Emphasis ink probe %s on %s -> %s (regular/actual/bold)",
                 weight_attr, ", ".join(details), "accept" if accepted else "reject")
    return accepted


def _resolve_emphasis_style():
    """解析强调样式，返回 ``(css_weight, css_family)``；都不可用时各为 ""。

    每个候选字重内先试策略 1、再试策略 2：

    1. 只改 ``font-weight``：Linux/fontconfig、macOS/CoreText 以及 Windows 的可变
       字体（Segoe UI Variable）都能按字重取到中间字面；
    2. 族名 + 字重一起换：平台把中间字重登记成独立族名时（Qt on Windows 的
       ``Segoe UI Semibold``），只改字重只会落到最近的 Bold。

    两种策略都要通过 :func:`_stack_accepts_weight` 的实测墨迹密度校验，因此"平台
    到底怎么解析字重"不影响正确性：判定不过就退回 Qt 原生粗体（强调不消失、也不
    比正文更淡）。各平台的实际落点见 :func:`emphasis_font_weight` 的说明；每次
    解析还会打一条 INFO 日志（候选命中情况 + 最终字重/族名），便于在 Windows
    机器上直接核对。
    """
    global _emphasis_style_cache
    if _emphasis_style_cache is None:
        resolved = ("", "")
        try:
            families = ThemeManager().font_families()
            probes = []
            for value, attr, suffixes in _EMPHASIS_WEIGHT_CANDIDATES:
                if _stack_accepts_weight(families, attr):
                    resolved = (str(value), "")
                    probes.append(f"{attr}:weight")
                    break
                for suffix in suffixes:
                    variants = [_suffixed_family(f, suffix) for f in families]
                    if variants != families and _stack_accepts_weight(families, attr, variants):
                        resolved = (str(value), ", ".join(f"'{f}'" for f in variants))
                        probes.append(f"{attr}:family{suffix.strip()}")
                        break
                if resolved[0]:
                    break
                probes.append(f"{attr}:miss")
            logger.info("Emphasis style resolved to weight=%r family=%r (families=%s, probes=%s)",
                        resolved[0] or "native-bold(700)", resolved[1] or "inherit",
                        families, ",".join(probes))
        except Exception as e:
            logger.warning("Emphasis style probe failed, fall back to native bold: %s", e)
        _emphasis_style_cache = resolved
    return _emphasis_style_cache


def emphasis_font_weight() -> str:
    """强调文本的 CSS ``font-weight`` 数值；"" = 退回 Qt 原生粗体。

    背景：Qt 富文本把 ``<b>`` / ``<strong>`` / 标题 / 表头一律按 QFont::Bold(700)
    渲染，而高质量字体的 Bold 是为标题级强调设计的：正文 14px 下笔画成倍加粗、
    与正文对比突兀（中英文同理，因为西文也由同一字体的 Bold 承担）。

    典型落点：Linux（Noto Sans CJK / 思源黑体，可变字体）→ 500 Medium；Windows
    （Segoe UI 系 + 微软雅黑）→ 600，西文 Semibold、中文仍是雅黑 Bold（雅黑没有
    中间字面，若被压回 Regular 会整体否决）；只有 Regular+Bold 的字体栈 → 返回
    空串，调用方用原生粗体。

    结果进程内缓存（系统字体运行期不变，流式渲染高频调用依赖此缓存）。
    """
    return _resolve_emphasis_style()[0]


def emphasis_font_family() -> str:
    """强调文本需要额外指定的 CSS ``font-family`` 栈；"" = 沿用正文字体栈。

    只有平台把中间字重登记成独立族名时（Qt on Windows 的 ``Segoe UI Semibold``
    等）才非空；非空时强调文本必须同时带上该族名栈，否则取不到中间字面。栈序与
    正文字体栈一一对应（同族名的中间字重版，缺失的族保持原样），因此中文仍落到
    原来的 CJK 族。
    """
    return _resolve_emphasis_style()[1]


def _strong_css() -> str:
    """内部用：强调档的 ``font-weight`` 值，取不到中间字面时退回 ``bold``。

    与 :func:`src.core.theme_manager.strong_weight_css` 同义（后者是给 QSS 调用方
    的公开入口，本函数供本模块内部的样式模板使用，避免自我导入）。
    """
    return emphasis_font_weight() or "bold"


@lru_cache(maxsize=2)
def title_font_weight() -> str:
    """标题字重的 CSS ``font-weight`` 数值；"" = 无可用中间字面（调用方用原生粗体）。

    界面里大量"加粗"其实是控件文案（导航项、表头、状态标签、按钮），旧实现一律写
    死 ``font-weight: bold``(700)。700 是标题级字重，正文尺寸下笔画成倍加粗、整屏
    观感发黑；这里把标题档收敛到 600（比强调档 500 再重一档），层级仍靠字号 +
    字重共同体现。

    候选不可用（字体栈只有 Regular/Bold，如 Windows 微软雅黑）时退回强调档
    :func:`emphasis_font_weight`；强调档也不可用则返回 ""，调用方沿用 ``bold``——
    与 :func:`emphasis_font_weight` 相同的"宁可粗、不要乱"的安全边界。
    """
    try:
        families = ThemeManager().font_families()
        for value, attr, suffixes in _EMPHASIS_WEIGHT_CANDIDATES:
            if value < 600:
                continue
            if _stack_accepts_weight(families, attr):
                return str(value)
            for suffix in suffixes:
                variants = [_suffixed_family(f, suffix) for f in families]
                if variants != families and _stack_accepts_weight(families, attr, variants):
                    return str(value)
    except Exception as e:
        logger.warning("Title weight probe failed, fall back to emphasis weight: %s", e)
    return emphasis_font_weight()


#: HTML 标签分词（属性值可含引号包裹的 ``>``，需按引号整体吞掉）。
_TAG_TOKEN_RE = re.compile(
    r'<(/?)([a-zA-Z][a-zA-Z0-9]*)((?:"[^"]*"|\'[^\']*\'|[^>"\'])*)>')

#: 表格标签匹配：用于把 ``width:100%`` / ``table-layout:fixed`` 换成自然宽度，
#: 使过宽表格能触发独立横向滚动条而不是被压窄换行。
_TABLE_TAG_RE = re.compile(r'<table\b[^>]*>', re.IGNORECASE)


def naturalize_table_html(html: str) -> str:
    """去掉表格的 ``width:100%`` 与 ``table-layout:fixed``，改按内容自然宽度布局。

    Qt 富文本默认让表格撑满可用宽度并压缩列宽；科研数据表列多、
    单元格窄，被压窄后换行严重影响可读性。去掉宽度约束后表格按内容定宽，
    超出容器的部分由外层横向滚动条承担。
    """
    def _repl(match):
        tag = match.group(0)
        tag = re.sub(r'width\s*:\s*100%\s*;?', '', tag, flags=re.IGNORECASE)
        tag = re.sub(r'table-layout\s*:\s*fixed\s*;?', '', tag, flags=re.IGNORECASE)
        return tag
    return _TABLE_TAG_RE.sub(_repl, html)


#: 渲染管线注入的 Mermaid 交互提示块（HTML 形态），导出/复制 Markdown 时应整块移除
_MERMAID_UI_HTML_RE = re.compile(
    r"<br\s*/?>\s*<div[^>]*>\s*<div[^>]*>\s*<b>\s*Mermaid Diagram Generated\s*</b>\s*</div>"
    r"\s*<a\b[^>]*>.*?</a>\s*</div>(?:\s*<br\s*/?>)?",
    re.DOTALL | re.IGNORECASE)

#: 同上，但文本已被转义或换行打散时的兜底匹配
_MERMAID_UI_TEXT_RE = re.compile(
    r"Mermaid Diagram Generated\s*(?:<br\s*/?>)?\s*(?:<a\b[^>]*>\s*)?"
    r"Click here to view\s*/\s*edit interactive diagram\s*(?:</a>)?",
    re.DOTALL | re.IGNORECASE)

#: 非正文标签的整棵子树（用于纯文本导出时彻底丢弃样式/脚本内容）
_DROP_SUBTREE_RE = re.compile(
    r"<(script|style|noscript)\b[^>]*>.*?</\1\s*>", re.DOTALL | re.IGNORECASE)


class _HtmlToMarkdown(HTMLParser):
    """HTML → Markdown 提取器（导出/复制 Markdown 共用）。

    取舍原则：**能用 Markdown 表达的一律用 Markdown**（表格、图片、加粗、斜体、
    代码、列表、链接、标题、引用、分隔线）；Markdown 表达不了的（带显示尺寸的
    图片、自定义卡片）保留精简 HTML 或转成等价可读文本；纯样式容器
    （div/span/font）只丢属性、内容照旧。多余空行由 :func:`_tidy_markdown` 收敛。
    """

    #: 内联标记标签 → Markdown 包裹符
    _MARKERS = {"b": "**", "strong": "**", "i": "*", "em": "*",
                "s": "~~", "strike": "~~", "del": "~~", "ins": "__"}
    #: 只保留内容、丢弃容器的标签
    _TRANSPARENT = {"div", "span", "font", "u", "small", "label", "center",
                    "section", "article", "header", "footer", "main", "tbody",
                    "thead", "tfoot", "abbr", "mark", "sub", "sup", "time"}
    #: 内部协议卡片标记（payload 为 base64 JSON）
    _CARDS = {"rplot_card", "plot_plan", "ask_user", "deep_plan"}
    _HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
    #: 内容不属于正文、整棵子树丢弃的标签（否则样式/脚本会被当成正文写进 .md）
    _DROP_SUBTREES = {"script", "style", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []
        self._pre = 0          # <pre> 嵌套深度：内部一律原样保留
        self._skip = 0         # _DROP_SUBTREES 嵌套深度：内部一律丢弃
        self._table = None     # 表格缓冲 {"rows": [], "row": ..., "cell": ...}
        self._lists = []       # 列表栈 [(ordered, counter)]
        self._links = []       # <a href> 栈
        self._markers = []     # 已开启的内联标记栈

    # ---------------- 缓冲 ----------------
    def _emit(self, text):
        if not text:
            return
        cell = self._table.get("cell") if self._table else None
        if cell is not None:
            cell.append(text)
        else:
            self._parts.append(text)

    def _tail_char(self):
        buf = (self._table.get("cell") if self._table else None) or self._parts
        for part in reversed(buf):
            if part:
                return part[-1]
        return ""

    def result(self):
        return "".join(self._parts)

    # ---------------- 事件 ----------------
    def handle_data(self, data):
        if not data:
            return
        if self._skip:
            return
        if self._pre:
            self._emit(data)
            return
        if data.strip():
            self._emit(data)
            return
        # 纯空白：标签换行/缩进不进入正文；行内空白压缩为单个空格
        if "\n" in data:
            self._emit("\n")
        elif self._tail_char() not in ("", " ", "\n"):
            self._emit(" ")

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        data = dict(attrs)
        # <script>/<style> 的内容是代码而非正文，整棵子树丢弃（含嵌套）
        if tag in self._DROP_SUBTREES:
            self._skip += 1
            return
        if self._skip:
            return
        if self._pre:
            if tag == "br":
                self._emit("\n")
            elif tag == "code":
                self._capture_code_language(data)
            else:
                self._emit(f"<{tag}>")
            return
        if tag == "br":
            self._emit("\n")
        elif tag == "hr":
            self._emit("\n\n---\n\n")
        elif tag == "img":
            self._emit_image(data)
        elif tag == "a":
            self._links.append((data.get("href") or "").strip())
            self._emit("[")
        elif tag == "code":
            self._emit("`")
        elif tag == "pre":
            self._pre += 1
            self._emit("\n\n```\n")
        elif tag in self._MARKERS:
            self._markers.append(tag)
            self._emit(self._MARKERS[tag])
        elif tag in ("ul", "ol"):
            self._lists.append([tag == "ol", 0])
            self._emit("\n")
        elif tag == "li":
            ordered, idx = self._lists[-1] if self._lists else (False, 0)
            if ordered:
                self._lists[-1][1] = idx + 1
                self._emit(f"{idx + 1}. ")
            else:
                self._emit("- ")
        elif tag in self._HEADINGS:
            self._emit("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "blockquote":
            self._emit("\n\n> ")
        elif tag == "table":
            self._table = {"rows": [], "row": None, "cell": None}
        elif tag == "tr":
            if self._table:
                self._table["row"] = []
        elif tag in ("td", "th"):
            if self._table:
                self._table["cell"] = []
        elif tag in self._CARDS:
            self._emit_card(tag, data)
        elif tag in self._TRANSPARENT:
            pass                                    # 纯样式容器透明化
        elif tag in ("p", "title", "body", "html", "head"):
            self._emit("\n\n")
        else:
            self._emit(f"<{tag}>")                  # 未知标签保守保留

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag == "br":
            self._emit("\n")
        elif tag == "hr":
            self._emit("\n\n---\n\n")
        elif tag == "img":
            self._emit_image(dict(attrs))
        else:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._DROP_SUBTREES:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "pre":
            self._pre = max(0, self._pre - 1)
            self._emit("\n```\n\n")
            return
        if self._pre:
            return
        if tag == "code":
            self._emit("`")
        elif tag == "a":
            href = self._links.pop() if self._links else ""
            self._emit(f"]({href})" if href else "]")
        elif tag in self._MARKERS:
            if self._markers and self._markers[-1] == tag:
                self._markers.pop()
                self._emit(self._MARKERS[tag])
        elif tag in ("ul", "ol"):
            if self._lists:
                self._lists.pop()
            self._emit("\n")
        elif tag == "li":
            self._emit("\n")
        elif tag in self._HEADINGS or tag == "blockquote":
            self._emit("\n\n")
        elif tag == "table":
            self._finish_table()
        elif tag == "tr":
            if self._table and self._table["row"] is not None:
                self._table["rows"].append(self._table["row"])
                self._table["row"] = None
        elif tag in ("td", "th"):
            self._finish_cell()
        elif tag in ("p", "title", "body", "html", "head"):
            self._emit("\n\n")
        elif tag in self._CARDS or tag in self._TRANSPARENT:
            pass
        else:
            self._emit(f"</{tag}>")

    # ---------------- 元素渲染 ----------------
    def _capture_code_language(self, data):
        """从 ``<pre><code class="language-python">`` 中提取围栏语言。

        渲染管线会给代码块标注语言；Markdown 围栏带上语言才能被高亮，因此把
        已经发出的空围栏（三反引号加换行）就地补成带语言的围栏。
        """
        match = re.search(r"(?:language|lang)-([\w#+.-]+)", data.get("class") or "")
        if not match or not self._parts:
            return
        tail = self._parts[-1]
        if tail.endswith("```\n"):
            self._parts[-1] = tail[:-4] + f"```{match.group(1)}\n"

    def _finish_cell(self):
        if not self._table or self._table["cell"] is None:
            return
        # 单元格内不能出现换行与竖线（会破坏表格结构）
        text = "".join(self._table["cell"]).strip()
        text = text.replace("|", "\\|").replace("\n", " ")
        if self._table["row"] is None:
            self._table["row"] = []
        self._table["row"].append(text)
        self._table["cell"] = None

    def _finish_table(self):
        table, self._table = self._table, None
        rows = [r for r in table["rows"] if r]
        if not rows:
            return
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |",
                 "| " + " | ".join(["---"] * width) + " |"]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        self._parts.append("\n\n" + "\n".join(lines) + "\n\n")

    def _emit_image(self, data):
        """图片：Markdown 语法优先；带显示尺寸时改用精简 HTML（Markdown 表达不了）。"""
        src = (data.get("src") or "").strip()
        if not src:
            return
        alt = (data.get("alt") or "").strip()
        width = (data.get("width") or "").strip()
        height = (data.get("height") or "").strip()
        if width or height:
            size = (f' width="{width}"' if width else "") + (f' height="{height}"' if height else "")
            self._emit(f'\n\n<img src="{src}" alt="{alt}"{size}>\n\n')
        else:
            self._emit(f"\n\n![{alt}]({src})\n\n")

    def _emit_card(self, tag, data):
        """内部卡片标记 → 可读 Markdown（图形转图片链接，方案/提问转引用块）。"""
        payload = None
        raw = data.get("data") or ""
        if raw:
            try:
                payload = _json.loads(_b64decode(raw).decode("utf-8"))
            except Exception:
                payload = None
        if not isinstance(payload, dict):
            return
        if tag == "rplot_card":
            img = payload.get("png_path") or payload.get("svg_path") or ""
            title = payload.get("chart_title") or payload.get("plot_label") or "chart"
            self._emit(f"\n\n![{title}]({img})\n\n" if img else f"\n\n*{title}*\n\n")
        elif tag in ("plot_plan", "deep_plan"):
            text = str(payload.get("plan_text") or payload.get("plan")
                       or payload.get("request") or "").strip()
            if text:
                self._emit("\n\n" + "\n".join("> " + ln for ln in text.splitlines()) + "\n\n")
        elif tag == "ask_user":
            question = str(payload.get("question") or "").strip()
            if question:
                options = [str(o).strip() for o in (payload.get("options") or []) if str(o).strip()]
                lines = ["> " + question] + [f"> - {o}" for o in options]
                self._emit("\n\n" + "\n".join(lines) + "\n\n")


def _tidy_markdown(text: str) -> str:
    """收敛转换副作用：行尾空白、连续空行、首尾空行。

    刻意**不**压缩行内空格——``<pre>`` 内的缩进属于代码内容，压缩会破坏代码块。
    """
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


# 不同 provider 的"内联思考"包裹写法各异，但语义一致（都应折叠进 Reasoning
# 面板）。只识别 ＜think＞ 一种写法时，其余变体会残留在正文——这正是"思考链
# 泄漏进正文"的根因之一。以下正则把这些写法统一折叠为 <think> / </think>。
# 覆盖：
#   1) XML 变体：＜think＞ ＜thinking＞ ＜reasoning＞ ＜reasoning_content＞
#   2) 管道变体：＜|thinking|＞ ＜|reasoning|＞（GPT-OSS、部分本地推理网关）
#   3) 符号包裹：◁think▷（Kimi K1.5 系列）
#   4) Harmony 频道：＜|channel|＞analysis＜|message|＞（思考信道起点）
# 这些字面量在正常学术正文中几乎不会出现；即便误判，代价也只是内容被移入
# 可折叠面板而非丢失，因此可安全用于兜底。
_THINK_OPEN_RE = re.compile(
    r"(?:"
    r"<\s*\??\s*(?:think|thinking|reasoning|reasoning_content)\s*>"
    r"|<\|\s*(?:think|thinking|reasoning)\s*\|>"
    r"|<\|\s*channel\s*\|>\s*analysis\s*<\|message\|>"
    r"|◁\s*think\s*▷"
    r")",
    re.IGNORECASE,
)

_THINK_CLOSE_RE = re.compile(
    r"(?:"
    r"<\s*[/?]+\s*(?:think|thinking|reasoning|reasoning_content)\s*>"
    r"|<\|\s*/\s*(?:think|thinking|reasoning)\s*\|>"
    r"|<\|\s*channel\s*\|>\s*final\s*<\|message\|>"
    r"|◁\s*/\s*think\s*▷"
    r")",
    re.IGNORECASE,
)

#: 推理/Harmony 协议的孤立标记：自身不承载正文，正文抽取后一并清除。
_THINK_NOISE_RE = re.compile(
    r"<\|\s*(?:end|start|message|constrain)\s*\|>"
    r"|<\|\s*channel\s*\|>\s*(?:analysis|final)?"
    r"|<\s*[/?]*\s*(?:think|thinking|reasoning|reasoning_content)\s*>"
    r"|◁\s*/?\s*think\s*▷",
    re.IGNORECASE,
)


class TextFormatter:

    #: 本管线"自有样式"的标签：每次渲染都会重新注入这些标签的样式，因此渲染
    #: 前必须先把上一次注入的 style 清掉。否则当入参已是"渲染结果"时（多处
    #: 调用点把 ``_format_response(...)`` 的输出再送进 ``set_content``），主题
    #: 切换后的重渲染只会保留固化在旧 HTML 里的主题色——典型症状是浅色主题
    #: 下标题仍是深色主题的浅灰、代码块仍是深色底、表格仍是深色边框。
    _STYLE_OWNED_TAGS = ('h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'th', 'td',
                         'ul', 'ol', 'li', 'blockquote', 'hr', 'pre', 'code')

    #: 上一次渲染注入的外层样式 div 的签名（font-family + color 组合，双引号）。
    #: 命中后降级为无样式 div，避免其 color 继续影响正文，同时保留配对的
    #: ``</div>`` 结构（Qt 富文本对多余闭合标签是容错的）。
    _WRAPPER_SIGN_RE = re.compile(
        r'<div style="font-family: [^"]*?;\s*color: [^"]*?;">', re.IGNORECASE)

    #: 自有样式标签的匹配：属性段允许出现引号包裹的值（如 ``style="a: b;"``）。
    #: ``\b`` 用于卡死标签名边界，避免 ``th`` 吃掉 ``<thead>``、``li`` 吃掉
    #: ``<link ...>`` 之类的同前缀标签。
    _OWNED_TAG_RE = re.compile(
        r'<(' + '|'.join(_STYLE_OWNED_TAGS) + r')\b((?:"[^"]*"|[^>"])*)>',
        re.IGNORECASE)

    #: ``<b>`` / ``<strong>`` 开标签（属性段允许引号包裹的值，如 ``style="a: b;"``）。
    _BOLD_OPEN_RE = re.compile(r'<(b|strong)((?:"[^"]*"|[^>"])*)>', re.IGNORECASE)
    #: 对应的闭标签（``</b >`` 之类带空格的写法一并容忍）。
    _BOLD_CLOSE_RE = re.compile(r'</(b|strong)\s*>', re.IGNORECASE)

    @staticmethod
    def apply_emphasis_weight(html: str) -> str:
        """把 ``<b>`` / ``<strong>`` 改写成带显式强调样式的 ``<span>``。

        Qt 的 ``<b>``/``<strong>`` 由富文本解析器直接钉死为 QFont::Bold，内联
        ``font-weight`` 能否覆盖它属实现细节；改写成 ``<span>`` 后样式只由内联
        声明决定，行为确定。字重取自 :func:`emphasis_font_weight`（无可用中间
        字重时为空串，此时原样返回、退回 Qt 原生粗体）；平台把中间字重登记成
        独立族名时（Qt on Windows），再带上 :func:`emphasis_font_family` 的族名栈。

        幂等：改写结果里已无 ``<b>``/``<strong>``，重复渲染不会叠加样式；标签
        属性（含 AI 自带的内联样式）原样保留，只把样式声明合并进去。
        """
        weight = emphasis_font_weight()
        if not weight or '<' not in html:
            return html

        declarations = f"font-weight:{weight};"
        family = emphasis_font_family()
        if family:
            declarations += f" font-family:{family};"

        def _open(match):
            attrs = match.group(2) or ""
            existing = re.search(r'style="([^"]*)"', attrs, re.IGNORECASE)
            if existing:
                kept = existing.group(1).strip().rstrip(';')
                merged = f"{kept}; {declarations}" if kept else declarations
                attrs = f'{attrs[:existing.start()]}style="{merged}"{attrs[existing.end():]}'
            else:
                attrs = f' style="{declarations}"{attrs}'
            return f"<span{attrs}>"

        html = TextFormatter._BOLD_OPEN_RE.sub(_open, html)
        return TextFormatter._BOLD_CLOSE_RE.sub('</span>', html)

    @classmethod
    def _reset_injected_styles(cls, text: str) -> str:
        """清除上一次渲染注入的内联样式，使本管线渲染幂等。

        只处理 :attr:`_STYLE_OWNED_TAGS` 中的标签（这些标签的样式完全由本
        管线决定），并保留 ``text-align``（python-markdown 表格列对齐语义）。
        其它标签（``<a>``/``<img>``/``<span>`` 等）与 AI 自带的内联 HTML
        不受影响。
        """
        if '<' not in text:
            return text

        text = cls._WRAPPER_SIGN_RE.sub('<div>', text)

        # 行内引用锚点（ref://）由本管线按主题注入强调色/底色：先还原为裸 ``[n]``，
        # 后续链接化规则会用当前主题重新建链，避免旧主题色被固化在文档里。
        text = re.sub(r"<a\b[^>]*href=['\"]ref://[^'\"]*['\"][^>]*>(.*?)</a>",
                      r"\1", text, flags=re.DOTALL | re.IGNORECASE)

        def _clean(match):
            tag, attrs = match.group(1), match.group(2)
            align = re.search(r'text-align\s*:\s*[^;"\']+', attrs, re.IGNORECASE)
            # 表格的历史表现型属性一并清掉，保证 <table> 能重新按主题重建
            attrs = re.sub(r'\s*(?:style|border|cellspacing|cellpadding)="[^"]*"',
                           '', attrs, flags=re.IGNORECASE)
            if align:
                attrs += f' style="{align.group(0).strip()}"'
            return f'<{tag}{attrs}>'

        return cls._OWNED_TAG_RE.sub(_clean, text)

    #: 常见 LaTeX 命令 → Unicode 符号映射（键不含反斜杠）。
    #: 仅用于行内简单公式的降级渲染（无 LaTeX 引擎场景），映射均为
    #: 数学/物理标准符号，保证公式语义不变。
    _LATEX_SYMBOLS = {
        # 运算与关系
        "times": "×", "div": "÷", "pm": "±", "mp": "∓", "cdot": "·",
        "approx": "≈", "simeq": "≃", "cong": "≅", "equiv": "≡",
        "neq": "≠", "ne": "≠", "leq": "≤", "geq": "≥",
        "ll": "≪", "gg": "≫", "sim": "∼", "propto": "∝",
        # 希腊字母（小写）
        "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
        "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
        "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
        "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π",
        "rho": "ρ", "sigma": "σ", "varsigma": "ς", "tau": "τ",
        "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ",
        "psi": "ψ", "omega": "ω",
        # 希腊字母（大写）
        "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
        "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ",
        "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
        # 微积分 / 算子
        "sum": "∑", "prod": "∏", "int": "∫", "iint": "∬", "iiint": "∭",
        "oint": "∮", "partial": "∂", "nabla": "∇", "infty": "∞",
        # 集合与逻辑
        "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆",
        "supset": "⊃", "supseteq": "⊇", "cup": "∪", "cap": "∩",
        "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃",
        "neg": "¬", "land": "∧", "lor": "∨",
        # 几何与其他
        "perp": "⊥", "parallel": "∥", "angle": "∠", "degree": "°",
        "hbar": "ℏ", "ell": "ℓ", "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ",
        "langle": "⟨", "rangle": "⟩", "prime": "′",
        # 箭头
        "to": "→", "rightarrow": "→", "leftarrow": "←",
        "Rightarrow": "⇒", "Leftarrow": "⇐",
        "leftrightarrow": "↔", "Leftrightarrow": "⇔",
        "mapsto": "↦", "uparrow": "↑", "downarrow": "↓",
        "longrightarrow": "⟶",
        # 省略号
        "cdots": "⋯", "ldots": "…", "dots": "…", "vdots": "⋮", "ddots": "⋱",
    }

    @staticmethod
    def _latex_expand_frac_sqrt(formula):
        """降级展开带参数命令：\\frac{a}{b} 与 \\sqrt{a} / \\sqrt[n]{a}。

        堆叠分数转斜杠形式：参数长度 >1 时加括号保持运算优先级不变
        （如 ``\\frac{\\Delta PE}{kT}`` → ``(ΔPE)/(kT)``），保证语义等价；
        n 次根号转分数指数：``\\sqrt[3]{x}`` → ``x^(1/3)``。
        迭代展开以支持嵌套（最多 4 层，防死循环）。
        """
        # \sqrt[n]{a} → a^(1/n)（先处理更具体的形式）
        formula = re.sub(r'\\sqrt\[([^\[\]]*)\]\{([^{}]*)\}',
                         lambda m: f"{m.group(2)}^(1/{m.group(1)})" if m.group(1)
                         else f"√({m.group(2)})",
                         formula)
        # \frac{a}{b} 迭代展开（内层先展开，逐层向外）
        for _ in range(4):
            new = re.sub(
                r'\\frac\{([^{}]*)\}\{([^{}]*)\}',
                lambda m: f"{m.group(1)}/{m.group(2)}"
                if (len(m.group(1)) == 1 and len(m.group(2)) == 1)
                else f"({m.group(1)})/({m.group(2)})",
                formula)
            if new == formula:
                break
            formula = new
        # \sqrt{a} → √(a)
        for _ in range(4):
            new = re.sub(r'\\sqrt\{([^{}]*)\}', r'√(\1)', formula)
            if new == formula:
                break
            formula = new
        return formula

    @staticmethod
    def _render_simple_latex(text):
        """把行内/独立 LaTeX 公式降级为 Qt 富文本可显示的 HTML。

        QTextBrowser 无 LaTeX 引擎，按顺序做四类转换：
        1. ``\\text{...}`` 还原为纯文本，``\\left``/``\\right`` 定界符剥离；
        2. 带参数命令展开（``\\frac`` / ``\\sqrt``，见 _latex_expand_frac_sqrt）；
        3. 符号命令映射为 Unicode（``\\approx``→≈、``\\Delta``→Δ 等）；
        4. ``^{}``/``_{}`` 上下标转 HTML sup/sub。
        转换后若仍残留未知命令，记录 debug 日志便于补充映射表。
        """

        def replacer(match):
            formula = match.group(1)
            formula = re.sub(r'\\text\{([^}]+)\}', r'\1', formula)
            formula = re.sub(r'\\mathrm\{([^}]+)\}', r'\1', formula)
            formula = re.sub(r'\\mathit\{([^}]+)\}', r'\1', formula)
            # \left( \right) 等自适应定界符：剥离前缀，保留定界符本身
            formula = re.sub(r'\\left\s*', '', formula)
            formula = re.sub(r'\\right\s*', '', formula)
            # 间距命令降级：薄空格/空隙 → 普通空格
            formula = re.sub(r'\\[,;:]', ' ', formula)
            formula = re.sub(r'\\quad|\\qquad', ' ', formula)
            formula = re.sub(r'\\ ', ' ', formula)
            # ^\circ / _\circ → °（温度/角度常见写法，需在符号映射前处理）
            formula = re.sub(r'([\^_])\s*\\circ(?![a-zA-Z])', '°', formula)

            formula = TextFormatter._latex_expand_frac_sqrt(formula)

            # 符号命令映射：按命令名长度降序替换，防止前缀误吃
            # （如 \simeq 必须先于 \sim、\neq 先于 \ne）。
            for name in sorted(TextFormatter._LATEX_SYMBOLS, key=len, reverse=True):
                formula = re.sub(r'\\' + name + r'(?![a-zA-Z])',
                                 TextFormatter._LATEX_SYMBOLS[name], formula)

            # 上下标（花括号形式优先，再处理单字符形式）
            formula = re.sub(r'\^\{([^}]+)\}', r'<sup>\1</sup>', formula)
            formula = re.sub(r'\^([a-zA-Z0-9])', r'<sup>\1</sup>', formula)
            formula = re.sub(r'_\{([^}]+)\}', r'<sub>\1</sub>', formula)
            formula = re.sub(r'_([a-zA-Z0-9])', r'<sub>\1</sub>', formula)
            formula = formula.replace('{}', '')

            residual = sorted(set(re.findall(r'\\[a-zA-Z]+', formula)))
            if residual:
                logger.debug("Unresolved LaTeX commands in inline formula: %s", residual)

            return f"<i>{formula}</i>"

        text = re.sub(r'\$\$(.*?)\$\$', replacer, text, flags=re.DOTALL)
        text = re.sub(r'\$(.*?)\$', replacer, text)
        return text

    @staticmethod
    def _render_chemistry(text):
        """识别并格式化化学分子式"""

        def formula_replacer(match):
            prefix = match.group(1)
            formula = match.group(2)
            subscripted = re.sub(r'(?<=[A-Za-z\)\]])(\d+)', r'<sub>\1</sub>', formula)
            return f"{prefix}{subscripted}"

        text = re.sub(
            r'(?i)((?:Molecular|Chemical|Empirical)[\s\*_]*formula[\s\*_:]*)([A-Za-z0-9\(\)\[\]]+)',
            formula_replacer,
            text
        )

        return text

    @staticmethod
    def markdown_to_plain_text(text):
        """将 Markdown 转换为适合纯文本阅读的格式（例如去除加粗、格式化表格制表符）"""
        # 去除 LaTeX 包装符
        text = re.sub(r'\$\$(.*?)\$\$', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\$(.*?)\$', r'\1', text)
        # 去除标题符
        text = re.sub(r'^#{1,6}\s*(.*)', r'\1', text, flags=re.MULTILINE)
        # 去除加粗和斜体
        text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
        text = re.sub(r'\*(.*?)\*', r'\1', text)
        # 去除链接，保留文本
        text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)

        # 格式化表格
        lines = text.split('\n')
        clean_lines = []
        for line in lines:
            if re.match(r'^\s*\|?.*\|.*\|?\s*$', line):
                if re.match(r'^\s*\|?[\s\-\:\|]+\|?\s*$', line):  # 跳过 Markdown 表格分隔线
                    continue
                # 按列拆分，并使用制表符对齐
                row = [cell.strip() for cell in line.strip('| \t').split('|') if cell.strip() or cell == '']
                clean_lines.append('\t'.join(row))
            else:
                clean_lines.append(line)
        return '\n'.join(clean_lines)


    @staticmethod
    def format_chat_text(text, index, expanded_indices, user_toggled_thinks):
        tm = ThemeManager()
        accent_color = tm.color('accent')
        bg_color = tm.color('bg_input')
        border_color = tm.color('border')
        text_muted = tm.color('text_muted')

        think_contents = []
        mcp_contents = []
        is_closed = True

        # 统一规范化标签
        text = re.sub(r'<\s*think\s*>', '<think>', text, flags=re.IGNORECASE)
        text = re.sub(r'<\s*/\s*think\s*>', '</think>', text, flags=re.IGNORECASE)
        text = re.sub(r'<\s*mcp_process\s*>', '<mcp_process>', text, flags=re.IGNORECASE)
        text = re.sub(r'<\s*/\s*mcp_process\s*>', '</mcp_process>', text, flags=re.IGNORECASE)

        # 兜底规范化：把其余 provider 的内联思考写法（thinking / reasoning /
        # 管道变体 / Harmony 频道 / Kimi 符号）也折叠为统一标签。只识别单一
        # 写法会让这些模型把思考链直接写进正文。
        # 注意：替换结果必须是带尖括号的 <think>，后续正文抽取是按
        # r'<think>(.*?)</think>' 匹配的；替换成裸文字（如 ' thinking'）会让
        # 这些思考块匹配不到，从而原样漏进正文——这正是"思考链与正文掺和
        # 在一起"的根因。
        text = _THINK_OPEN_RE.sub('<think>', text)
        # 关闭写法必须折叠为 </think> 而非删除：删除会让 <think> 块失去配对的
        # 结束标记，随后的正文会被当成"未闭合思考链"一并吞进 Reasoning 面板，
        # 导致正文区变空（比原来的泄漏更严重）。
        text = _THINK_CLOSE_RE.sub('</think>', text)

        # [FINAL_ANSWER] 的位置必须在规范化之后重算：规范化会改变其前缀长度
        # （如角括号写法会多出字符），沿用规范化前的偏移切片会让标记前后的
        # 内容错位，从而把思考链残留在正文。
        final_answer_match = re.search(r'\[FINAL_ANSWER\]\s*', text, flags=re.IGNORECASE)

        if final_answer_match:
            raw_hidden = text[:final_answer_match.start()]
            main_text = text[final_answer_match.end():].strip()
            is_closed = True

            # 提取已闭合的所有标签内容
            for t_match in re.finditer(r'<think>(.*?)</think>', raw_hidden, flags=re.DOTALL | re.IGNORECASE):
                think_contents.append(t_match.group(1).strip())
            for m_match in re.finditer(r'<mcp_process>(.*?)</mcp_process>', raw_hidden,
                                       flags=re.DOTALL | re.IGNORECASE):
                mcp_contents.append(m_match.group(1).strip())
        else:
            def think_repl(match):
                think_contents.append(match.group(1).strip())
                return ""

            main_text = re.sub(r'<think>(.*?)</think>', think_repl, text, flags=re.DOTALL | re.IGNORECASE)

            def mcp_repl(match):
                mcp_contents.append(match.group(1).strip())
                return ""

            main_text = re.sub(r'<mcp_process>(.*?)</mcp_process>', mcp_repl, main_text,
                               flags=re.DOTALL | re.IGNORECASE)

            unclosed_think = re.search(r'<think>(.*)$', main_text, flags=re.DOTALL | re.IGNORECASE)
            if unclosed_think:
                think_contents.append(unclosed_think.group(1).strip())
                main_text = main_text[:unclosed_think.start()].strip()
                is_closed = False

            unclosed_mcp = re.search(r'<mcp_process>(.*)$', main_text, flags=re.DOTALL | re.IGNORECASE)
            if unclosed_mcp:
                mcp_contents.append(unclosed_mcp.group(1).strip())
                main_text = main_text[:unclosed_mcp.start()].strip()
                is_closed = False

        hidden_blocks = []
        if think_contents:
            hidden_blocks.append("<b>🧠 AI Reasoning:</b><br>" + "<br><br>".join(filter(None, think_contents)))
        if mcp_contents:
            hidden_blocks.append("<b>🛠️ MCP Tool Execution:</b><br>" + "<br><br>".join(filter(None, mcp_contents)))

        hidden_content = "<br><br>".join(hidden_blocks)
        final_html = ""

        if hidden_content or not is_closed:
            if index in user_toggled_thinks:
                is_expanded = index in expanded_indices
            else:
                # 默认展开：思考链作为独立的折叠区域呈现，但默认态为"展开"，
                # 内容随流式实时可见。高度由 chat_bubble 的独立滚动块限制
                # （_THINK_MAX_HEIGHT），超出后由该块自身的纵向滚动条承担，
                # 因此不需要靠默认收起来控制篇幅。用户手动收起后（index 进入
                # user_toggled_thinks）才沿用其选择。
                is_expanded = True
            logger.debug("Think panel state: index=%s expanded=%s closed=%s user_toggled=%s",
                         index, is_expanded, is_closed, index in user_toggled_thinks)

            action = "collapse" if is_expanded else "expand"
            icon_name = "chevron-down" if is_expanded else "chevron-right"
            icon_uri = _icon_uri(icon_name)
            icon_html = (f"<img src='{icon_uri}' width='14' height='14' "
                         f"style='vertical-align: middle;' />") if icon_uri else ""

            # 根据内容智能显示折叠面板标题
            if mcp_contents and not think_contents:
                status_title = "Tool Execution" if is_closed else "Executing Tools..."
            elif mcp_contents and think_contents:
                status_title = "Reasoning & Tool Execution" if is_closed else "AI is Analyzing & Working..."
            else:
                status_title = "AI Reasoning" if is_closed else "AI is thinking..."

            link = f"<a href='think://{action}?index={index}' style='color:{accent_color}; text-decoration:none;'><nobr>{icon_html} <b>{status_title}</b></nobr></a>"

            # data-navis-think 标记：仅供 UI 层拆分块时识别思考链面板
            # （见 TextFormatter.split_overflow_blocks），Qt 渲染时忽略未知属性。
            if not is_expanded:
                final_html += (f"<div data-navis-think='1' style='background:{bg_color}; border-left: 3px solid {border_color}; "
                               f"padding: 8px 12px; margin: 10px 0; border-radius: 4px; font-size: 13px;'>{link}</div>")
            else:
                safe_content = hidden_content.replace('\n', '<br>')
                safe_content = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', safe_content)
                safe_content = re.sub(r'\*(.*?)\*', r'<i>\1</i>', safe_content)
                suffix = "" if is_closed else f" <span style='color:{accent_color};'><i>...</i></span>"
                final_html += (
                    f"<div data-navis-think='1' style='background:{bg_color}; border-left: 3px solid {accent_color}; padding: 8px 12px; "
                    f"margin: 10px 0; border-radius: 4px; font-size: 13px; color: {text_muted};'>"
                    f"{link}<br><br><div style='color:{text_muted};'>{safe_content}{suffix}</div></div>")

        if main_text:
            main_text = re.sub(r'\[FINAL_ANSWER\]\s*', '', main_text, flags=re.IGNORECASE)
            main_text = re.sub(r'\[\s*FOLLOW[_-]?\s*UPS?\s*\]\s*', '', main_text, flags=re.IGNORECASE)
            # 兜底清除孤立的推理/Harmony 协议标记：它们不承载正文，若上游
            # 传入了不配对的开闭标签，会以裸标记形式残留在正文里。
            main_text = _THINK_NOISE_RE.sub('', main_text)

            main_text = re.sub(r'<br\s*/?>', '\n', main_text, flags=re.IGNORECASE)

            rendered_main_html = TextFormatter.markdown_to_html(main_text)
            final_html += f"\n\n{rendered_main_html}"

        # 折叠面板标题（🧠 Reasoning / 🛠️ Tool Execution）等旁路 HTML 不经过
        # markdown_to_html，统一在此补一次强调字重；正文已处理过，是幂等的 no-op。
        return TextFormatter.apply_emphasis_weight(final_html)

    # ------------------------------------------------------------------
    # 残缺表格还原：分隔行漏写竖线 / 多行被压成一行
    # ------------------------------------------------------------------
    #: 表格分隔行：整行仅由竖线 / 连字符 / 冒号 / 空格组成，且至少含一个连字符。
    #: 允许模型漏写首尾竖线（如 ``---``、``--- | :---``），与 python-markdown
    #: tables 扩展对分隔行的字符集校验（``set ⊆ '|:- '``）保持一致的宽松度。
    _TABLE_DELIM_LINE_RE = re.compile(r'^[\s|:-]*-[\s|:-]*$')

    @staticmethod
    def _split_table_cells(line: str) -> list:
        """按未转义竖线切分一行表格，返回去掉首尾边框竖线后的单元格列表。

        取值方式对齐 python-markdown 的 ``TableProcessor._split_row``（忽略
        ``\\|`` 转义）；代码跨度内的竖线属罕见边界情况，这里不做处理。
        """
        text = line.strip()
        if text.startswith('|'):
            text = text[1:]
        if text.endswith('|') and not text.endswith(r'\|'):
            text = text[:-1]
        if not text:
            return []
        return [cell.strip() for cell in re.split(r'(?<!\\)\|', text)]

    @classmethod
    def _is_table_delimiter(cls, line: str) -> bool:
        """该行是否是 Markdown 表格分隔行（允许漏写首尾竖线）。

        纯 ``---`` 也命中，因此调用方必须限定在"上一行确为表头"的上下文里用。
        """
        stripped = line.strip()
        return bool(stripped) and bool(cls._TABLE_DELIM_LINE_RE.match(stripped))

    @staticmethod
    def _build_table_delimiter(columns: int) -> str:
        """按列数生成标准分隔行 ``| --- | --- |``（列数至少为 1）。"""
        return '|' + '|'.join([' --- '] * max(1, int(columns))) + '|'

    @classmethod
    def _repair_broken_tables(cls, text: str) -> str:
        """把模型输出的残缺表格还原为 python-markdown 可解析的标准表格。

        处理两类常见残缺（都会让 tables 扩展整体放弃解析、表格退化成"竖线原样
        显示"的纯文本）：

        1. **分隔行漏写竖线**：``|属性|内容|`` 之后跟 ``---``。``---`` 会被当成
           setext 下划线，把表头变成 ``<h2>``；这里按表头列数重建分隔行。
        2. **多行被压成一行**：``|A1|B1|A2|B2|``，单元格总数是表头列数的整数倍。
           扩展只认表头列数，会把后半段数据静默丢弃；这里按列数重新切分成多行。

        只在"上一行确为多列表头（至少 2 个竖线）、下一行确为分隔行"时触发，
        其余情况一律原样返回，绝不把普通含竖线的文本误判成表格。
        """
        lines = text.split('\n')
        result = []
        i, total = 0, len(lines)
        while i < total:
            header = lines[i]
            result.append(header)
            # 表头须"有边框"（首或尾为竖线）且至少 2 个竖线：把"普通含竖线的
            # 句子 + 一行 ---"这类误判挡在外面（无边框的规范表格本就解析正常，
            # 无需在此修复）。
            stripped_header = header.strip()
            bordered = (stripped_header.startswith('|')
                        or stripped_header.endswith('|'))
            columns = (len(cls._split_table_cells(header))
                       if bordered and header.count('|') >= 2 else 0)
            if (columns >= 2 and i + 1 < total
                    and cls._is_table_delimiter(lines[i + 1])):
                result.append(cls._build_table_delimiter(columns))
                i += 2
                while (i < total and lines[i].strip() and '|' in lines[i]
                       and not cls._is_table_delimiter(lines[i])):
                    cells = cls._split_table_cells(lines[i])
                    if len(cells) > columns and len(cells) % columns == 0:
                        for start in range(0, len(cells), columns):
                            chunk = cells[start:start + columns]
                            result.append('| ' + ' | '.join(chunk) + ' |')
                    else:
                        result.append(lines[i])
                    i += 1
                continue
            i += 1
        return '\n'.join(result)

    @staticmethod
    def _repair_glued_horizontal_rules(text: str) -> str:
        """修复被压成一行的水平分割线（``正文 --- 正文`` → 独立成块）。

        逐行处理并跳过含竖线的行：表格分隔行 ``| --- | --- |`` 同样满足
        "空白 + 连字符 + 空白"，若一并拆开会把整张表退化成纯文本。
        """
        lines = text.split('\n')
        for idx, line in enumerate(lines):
            if '|' in line:
                continue
            lines[idx] = re.sub(r'(?<=\S)\s+(--+)\s+(?=\S)', r'\n\n\1\n\n', line)
        return '\n'.join(lines)

    @staticmethod
    def markdown_to_html(text, theme_key=None):
        """Markdown → Qt 富文本 HTML。

        :param theme_key: 显式指定取色主题（如 PDF 导出固定 "light"）；
                          None 时跟随 ThemeManager 当前主题。主题色以
                          HTML 内联样式固化进文档，调用方（气泡层）需在
                          theme_changed 时重渲染以刷新内联主题色。
        """
        # 幂等前提：先清掉上一次渲染注入的主题样式，再重新注入当前主题的样式。
        # 入参既可能是原始 Markdown，也可能是已渲染过的 HTML（气泡重渲染路径）。
        processed_text = TextFormatter._reset_injected_styles(text)

        # ================= 救砖：修复丢失换行符的极度压缩 Markdown =================
        # 0. 先把残缺表格还原成标准 Markdown 表格。表格一旦残缺（分隔行漏写竖线、
        #    多行被压成一行），python-markdown 的 tables 扩展会整体放弃解析，表格
        #    退化成"竖线原样显示"的纯文本；这一步必须先于其它救砖规则执行。
        processed_text = TextFormatter._repair_broken_tables(processed_text)
        # 1. 修复连成一行的水平分割线。逐行处理并跳过含竖线的行：表格分隔行
        #    "| --- | --- |" 同样命中"空白 + 连字符 + 空白"，旧实现会把它拆成
        #    "|"、"<hr>"，从而破坏整张表——这正是表格渲染异常的根因。
        processed_text = TextFormatter._repair_glued_horizontal_rules(processed_text)
        # 2. 修复紧贴文本的标题，以及跟在表格后面的标题
        processed_text = re.sub(r'(\|\s*)(#{1,6}\s+)', r'\1\n\n\2', processed_text)
        processed_text = re.sub(r'(?<=\S)\s+(#{1,6}\s+)', r'\n\n\1', processed_text)
        processed_text = re.sub(r'([^\n])\n(#{1,6}\s+)', r'\1\n\n\2', processed_text)
        # 3. 修复标题和表格完全粘在一行的情况
        processed_text = re.sub(r'(#{1,6}\s+[^|\n]+?)\s+(\|)', r'\1\n\n\2', processed_text)
        # 4. 修复表格缺空行导致的解析失败 (紧贴文本的表格前加强制换行)
        processed_text = re.sub(r'([^\n])\n(\s*\|.*\|)\s*\n(\s*\|[-:| ]+\|)', r'\1\n\n\2\n\3', processed_text)
        # 5. 修复表格内部被压扁成单行的情况
        processed_text = processed_text.replace('| |-', '|\n|-')
        processed_text = re.sub(r'(\|\s*\[\d+\]\s*\|)\s*(?=\|)', r'\1\n', processed_text)
        # =========================================================================

        processed_text = TextFormatter._render_simple_latex(processed_text)
        processed_text = TextFormatter._render_chemistry(processed_text)

        html = markdown.markdown(processed_text, extensions=['extra', 'nl2br', 'sane_lists', 'tables'])

        # 强调字重：把 <b>/<strong> 换成显式强调样式的 <span>，避免 Qt 一律用
        # Bold(700) 造成的"中英文粗体都过重"（见 emphasis_font_weight）。
        _emph_weight = emphasis_font_weight()
        _emph_family = emphasis_font_family() if _emph_weight else ""
        # 无中间字重时退回 Qt 原生 bold，保持"强调仍是强调"。
        _emph_css = f"font-weight:{_emph_weight}; " if _emph_weight else ""
        # 平台把中间字重登记为独立族名时（Qt on Windows），族名也要一起下发，
        # 否则标题/表头/链接仍会落回最近的原生 Bold。
        _emph_family_decl = f"font-family:{_emph_family}; " if _emph_family else ""
        _emph_css += _emph_family_decl
        _emph_value = _emph_weight or 'bold'
        html = TextFormatter.apply_emphasis_weight(html)

        # ================= 代码视觉区分（对齐主流商业聊天软件惯例） =================
        # QTextBrowser 无原生代码样式：为块级 <pre><code> 与行内 <code> 注入
        # 主题化底色/边框/等宽字体（深浅色主题各自适配），使代码与正文明显
        # 区分。所有颜色（含文字色）按主题显式注入——HTML 内联样式会随文档
        # 固化，主题切换后由气泡层重渲染整体重建，避免"深底配深字"失效对比。
        # Qt 富文本不支持 border-radius 与行内 padding，无效属性会被静默忽略。
        _tm = ThemeManager()
        _mono_css = mono_font_family_css()
        _code_bg = _tm.color('code_bg', theme_key)
        _code_fg = _tm.color('code_fg', theme_key)
        _code_border = _tm.color('code_border', theme_key)
        _inline_bg = _tm.color('inline_code_bg', theme_key)
        # 块级代码：底色/边框挂在 <pre>，内部 <code> 置透明，避免双层底色叠加
        _pre_style = (f"background-color:{_code_bg}; border:1px solid {_code_border}; "
                      f"padding:8px 12px; margin:8px 0; border-radius:4px; "
                      f"font-family:{_mono_css}; color:{_code_fg};")
        _block_code_style = (f"font-family:{_mono_css}; color:{_code_fg}; "
                             f"background-color:transparent;")
        # 行内代码：主题灰底 + 等宽 + 显式文字色
        _code_style = (f"background-color:{_inline_bg}; font-family:{_mono_css}; "
                       f"color:{_code_fg};")

        # 1) 围栏代码块：<pre><code ...>（含无语言标注的裸 <code>）。
        #    块内 code 一并注入 style，行内规则凭 style= 前瞻跳过，避免重复命中。
        html = re.sub(r'<pre><code([^>]*)>',
                      lambda m: (f'<pre style="{_pre_style}">'
                                 f'<code{m.group(1)} style="{_block_code_style}">'),
                      html)
        # 2) 行内代码：剩余不带 style 的 <code>（至此仅剩正文行内形式）
        html = re.sub(r'<code(?![^>]*style=)', f'<code style="{_code_style}"', html)

        # 3) 标题层级：显式主题正文色 + 递减字号；h1/h2 加下边框增强分区感。
        #    不依赖文档默认色渲染，保证深浅主题与任意容器底色下对比稳定。
        #    font-weight 亦显式下发：标题的粗体默认来自 Qt 内置样式表，内联声明
        #    优先级更高（与上面的 font-size 同理），可压掉过重的 Bold(700)。
        _text_main = _tm.color('text_main', theme_key)
        _border = _tm.color('border', theme_key)
        for _lvl, _size in ((1, 21), (2, 18), (3, 16), (4, 15), (5, 14), (6, 13)):
            _h_style = (f"color:{_text_main}; font-size:{_size}px; {_emph_css}"
                        f"margin-top:12px; margin-bottom:4px;")
            if _lvl <= 2:
                _h_style += f" border-bottom:1px solid {_border}; padding-bottom:4px;"
            html = html.replace(f'<h{_lvl}>', f'<h{_lvl} style="{_h_style}">')

        # 4) 引用块：主题色左边条 + 弱化文字色（Qt 忽略不支持的属性，无害）
        html = html.replace(
            '<blockquote>',
            f'<blockquote style="border-left:3px solid {_border}; '
            f'padding-left:10px; margin:8px 0; '
            f'color:{_tm.color("text_muted", theme_key)};">')

        # 5) 水平分割线：显式主题边框色（默认黑线在深色主题下几乎不可见）
        _hr_html = f'<hr style="border:none; border-top:1px solid {_border}; margin:10px 0;" />'
        html = re.sub(r'<hr\s*/?>', lambda m: _hr_html, html)

        # 6) 表格：边框 / 表头底纹 / 单元格文字色全部显式注入。此前表头底纹与
        #    边框在气泡层（chat_bubble.set_content）二次替换，导致其它消费者
        #    （如 PDF 导出）拿不到主题化表格，且单元格文字色依赖容器调色板——
        #    深浅模式与固定浅色导出混用时会出现"文字与底色同色"。统一在此处理，
        #    theme_key 决定配色，任何调用方的结果一致。
        #    python-markdown 的 tables 扩展会为对齐生成 <th style="text-align:...">，
        #    直接再挂一个 style= 会产生重复属性（Qt 取首个，后者失效），故用
        #    正则与既有 style 合并；无 style 的标签直接新建。
        _is_dark_theme = (theme_key if theme_key in _tm.themes
                          else _tm.current_theme) == 'dark'
        _th_bg = (_rgba(_tm.color('bg_input', theme_key), 0.5) if _is_dark_theme
                  else _rgba(_border, 0.12))
        _cell_border = f"1px solid {_border}"

        def _merge_style(tag: str, extra: str):
            def _repl(match):
                existing = (match.group(1) or "").strip().rstrip(";")
                merged = f"{existing}; {extra}" if existing else extra
                return f'<{tag} style="{merged}">'
            return _repl

        html = re.sub(r'<th(?:\s+style="([^"]*)")?\s*>',
                      _merge_style('th', f"color:{_text_main}; background-color:{_th_bg}; "
                                         f"font-weight:{_emph_value}; {_emph_family_decl}"
                                         f"border:{_cell_border}; "
                                         f"padding:6px 10px; vertical-align:top;"), html)
        html = re.sub(r'<td(?:\s+style="([^"]*)")?\s*>',
                      _merge_style('td', f"color:{_text_main}; border:{_cell_border}; "
                                         f"padding:6px 10px; vertical-align:top;"), html)
        html = html.replace(
            '<table>',
            f'<table border="1" cellspacing="0" cellpadding="8" style="'
            f'border-collapse:collapse; border-color:{_border}; margin:10px 0; '
            f'width:100%; table-layout:fixed;">')

        # 7) 列表：显式主题文字色 + 缩进/间距（Qt 对 ul/ol 的默认缩进偏小，
        #    嵌套列表与正文几乎无区分度）。保留既有属性（如 <ol start="3">），
        #    仅追加样式——渲染前已由 _reset_injected_styles 清掉旧 style，
        #    不会产生重复属性。
        html = re.sub(
            r'<(ul|ol)([^>]*)>',
            lambda m: (f'<{m.group(1)}{m.group(2)} style="-qt-list-indent:1; '
                       f'color:{_text_main}; margin-top:4px; margin-bottom:8px;">'),
            html)
        html = re.sub(r'<li(?:\s+style="([^"]*)")?\s*>',
                      _merge_style('li', f"color:{_text_main}; margin-bottom:2px;"), html)
        # =========================================================================

        # skip 保护：a/pre/code/img 维持"整元素"保护（元素内文本不再重复建链）；
        # 末位追加"任意单个标签"兜底 <[^>]+>——保护所有标签的属性值（如本函数
        # 注入的 style="color:#e0e0e0"），否则科学 ID 链接器会在 (?si) 不区分
        # 大小写下把形如 UniProt 登录号（[A-NR-Z][0-9][A-Z][A-Z0-9]{2}[0-9]）
        # 的十六进制色值 e0e0e0/f6f7f8/e1e4e8 误判为蛋白 ID，向属性内注入
        # <a> 导致标签被截断、样式内容漏成正文。alternation 按序匹配，前四
        # 个整元素规则优先命中，文本节点（标签之间）仍正常扫描建链。
        skip_pattern = (r'(?si)(<a\b[^>]*>.*?</a>|<pre\b[^>]*>.*?</pre>|<code\b[^>]*>.*?</code>'
                        r'|<img\b[^>]*>|<[^>]+>)')

        url_pattern = skip_pattern + r'|(?<![="\'/])\b((?:https?|ftp|file)://[^\s<>\)\]"\'，。？！；：“”‘’\n]+(?<![.,?!;:：]))'

        def url_repl(match):
            if match.group(1): return match.group(1)  # 返回原样 (因被 Skip 保护)
            return f'<a href="{match.group(2)}">{match.group(2)}</a>'

        html = re.sub(url_pattern, url_repl, html)

        # 3. 匹配常见科研数据库/论文 ID 及其它标识，自动挂载官方解析链接
        #    行内引用标记 [n] 优先建链（置于首位）：链接化为内部 ref:// 协议，
        #    由气泡层渲染为"可悬停预览、可点开溯源"的参考文献入口（悬浮卡 /
        #    详情面板）。外观按超链接处理（主题强调色 + 下划线 + 手型光标），
        #    让用户一眼看出可点；写成带 style 的 <a>，通用链接色规则会跳过它，
        #    且后续标识符规则因 <a>...</a> 被 skip_pattern 保护而不会误伤编号。
        _cite_accent = _tm.color('accent', theme_key)
        _cite_style = (f"color:{_cite_accent}; background-color:{_rgba(_cite_accent, 0.12)}; "
                       f"text-decoration:underline; font-weight:{_emph_value}; {_emph_family_decl}")
        replacements = [
            # 正文行内引用标记：[1] / [12] / [101]（模板中的 \1 即 group(2)）
            (r'\[(\d{1,3})\]', '<a href="ref://cite?n=\\1" style="' + _cite_style + '">[\\1]</a>'),
            # 棉花基因 ID（CottonGen feature 页；置于首位优先建链获得 skip 保护）。
            # 覆盖多套命名体系（[AD]=亚基因组，\d{2}=染色体号）：
            #   Ghir_[AD]xxGxxxx / Gxxxxx（4-5 位，可带 .x 版本号，TM-1 参考基因组）
            #   GH_[AD]xxGxxxx / GhChr[AD]xxGxxxx / Gh_[AD]xxGxxxx（4 位）
            #   Gh_[AD]xxGxxxxxx（6 位）/ Ghi_[AD]xxGxxxx / Gohir.[AD]xxGxxxxxx（6 位）
            # 注意：skip_pattern 前缀的 (?si) 对合并后正则全局生效，本条与
            # 其余标识符一致为不区分大小写匹配（链接文本保留原始大小写）。
            # 捕获组为整体 ID：skip 机制占用 group(1)，模板中的 \1 即 group(2)。
            (r'\b((?:Ghir_[AD]\d{2}G\d{4,5}|Gohir\.[AD]\d{2}G\d{6}'
             r'|GhChr[AD]\d{2}G\d{4}|GH_[AD]\d{2}G\d{4}'
             r'|Gh_[AD]\d{2}G\d{6}|Ghi?_[AD]\d{2}G\d{4})(?:\.\d+)?)\b',
             r'<a href="https://www.cottongen.org/feature/\1">\1</a>'),
            # DOI
            (r'\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)(?<![.,?!;:：])', r'<a href="https://doi.org/\1">\1</a>'),
            # TaxID
            (r'\b(?:taxid|taxonomy\s*id)\s*:?\s*(\d+)\b',
             r'<a href="https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id=\1">TaxID: \1</a>'),
            # BioProject
            (r'\b(PRJN[A-Z]\d+)\b', r'<a href="https://www.ncbi.nlm.nih.gov/bioproject/\1">\1</a>'),
            # NCBI Assembly (GCF / GCA)
            (r'\b(GC[FA]_\d{9}(?:\.\d+)?)\b', r'<a href="https://www.ncbi.nlm.nih.gov/datasets/genome/\1/">\1</a>'),
            # NCBI RefSeq / GenBank / Accessions
            (r'\b((?:NM|NP|NR|NC|NG|XM|XP|XR|WP|YP|AP)_\d{4,10}(?:\.\d+)?)\b',
             r'<a href="https://www.ncbi.nlm.nih.gov/search/all/?term=\1">\1</a>'),

            # AlphaFold 原始模型文件下载提取
            (r'\b(AF-[A-Z0-9]{6,10}-F\d+-model_v\d+\.(?:pdb|cif))\b',
             r'<a href="https://alphafold.ebi.ac.uk/files/\1" style="color: #10b981;">📥 \1</a>'),
            # AlphaFold 结构标识符
            (r'\b(AF-[A-Z0-9]{6,10}-F\d+)\b',
             r'<a href="https://alphafold.ebi.ac.uk/entry/\1">AlphaFold \1</a>'),

            # UniProt
            (r'\b([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})\b',
             r'<a href="https://www.uniprot.org/uniprotkb/\1/entry">\1</a>'),
            # Ensembl
            (r'\b(ENS[GTPER]\d{11})\b', r'<a href="https://www.ensembl.org/id/\1">\1</a>'),
            # E.C. 酶编号
            (r'\b(?:EC\s+|E\.C\.\s*)(\d+\.\d+\.\d+\.(?:\d+|-))\b',
             r'<a href="https://enzyme.expasy.org/EC/\1">EC \1</a>'),
            # PubChem CID
            (r'\b(?:CID|PubChem\s*CID)\s*:?\s*(\d+)\b',
             r'<a href="https://pubchem.ncbi.nlm.nih.gov/compound/\1">CID \1</a>'),
            # PDB 晶体结构
            (r'\bPDB\s*(?:ID\s*)?:?\s*([1-9][A-Z0-9]{3})\b', r'<a href="https://www.rcsb.org/structure/\1">PDB \1</a>'),
            # STRING DB 蛋白互作网络
            (r'\b(\d+\.ENSP\d{11})\b', r'<a href="https://string-db.org/network/\1">\1</a>'),

            # PubMed PMID (新增)
            (r'\b(?:PMID|PubMed\s*ID)\s*:?\s*(\d+)\b',
             r'<a href="https://pubmed.ncbi.nlm.nih.gov/\1/">PMID \1</a>'),

            # GBIF Taxon Key (分类单元唯一标识符)
            (r'\b(?:GBIF\s*Taxon\s*Key|GBIF\s*ID|TaxonKey)\s*:?\s*(\d+)\b',
             r'<a href="https://www.gbif.org/species/\1">GBIF Taxon \1</a>'),
            # GBIF 发生记录主页映射
            (r'(Global\s*Biodiversity\s*Information\s*Facility\s*\(GBIF\)\s*-\s*Occurrence\s*(?:Download|Records?))',
             r'<a href="https://www.gbif.org/occurrence/search">\1</a>'),

            # Gene Ontology (GO) 映射
            (r'\b(GO:\d{7})\b', r'<a href="https://www.ebi.ac.uk/QuickGO/term/\1">\1</a>'),

            (r'\b(K\d{5})\b', r'<a href="https://www.kegg.jp/entry/\1">KEGG \1</a>'),
            # KEGG Pathway Identifier mapping
            (r'\b([a-z]{2,4}\d{5})\b', r'<a href="https://www.kegg.jp/pathway/\1">KEGG Pathway \1</a>'),

            # InterPro (IPR) - 新增
            (r'\b(IPR\d{6})\b',
             r'<a href="https://www.ebi.ac.uk/interpro/entry/InterPro/\1/">\1</a>'),

            # ChEMBL Target ID 映射
            (r'\b(CHEMBL\d+)\b', r'<a href="https://www.ebi.ac.uk/chembl/target_report_card/\1">\1</a>'),

            # ChEBI ID 映射
            (r'\b(CHEBI:\d+)\b', r'<a href="https://www.ebi.ac.uk/chebi/searchId.do?chebiId=\1">\1</a>'),

            # 拟南芥 AGI 基因号（TAIR 官方检索）
            (r'\b(AT[1-5CM]G\d{5})\b',
             r'<a href="https://www.arabidopsis.org/results?mainType=general&amp;searchText=\1&amp;category=genes">\1</a>'),

            # JASPAR Motif ID (例如: MA0001.1)
            (r'\b(MA\d{4}\.\d+)\b', r'<a href="https://jaspar.elixir.no/matrix/\1/">JASPAR \1</a>'),

            # SNP / Variation ID (例如: rs1234567)
            (r'\b(rs\d+)\b', r'<a href="https://www.ensembl.org/Variation/Explore?v=\1">SNP \1</a>'),



        ]

        for pat, template in replacements:
            combined_pat = re.compile(skip_pattern + r'|' + pat)

            def get_replacer(tmpl):
                def replacer_func(match):
                    if match.group(1): return match.group(1)
                    return tmpl.replace(r'\1', match.group(2))

                return replacer_func

            html = combined_pat.sub(get_replacer(template), html)

        # 链接主题化：跟随主题 accent 色（原硬编码 #4daafc 在浅色主题下
        # 对比度不足）。负向前瞻跳过已自带 style 的专用链接（如 AlphaFold
        # 下载绿色、think/mermaid 面板），避免产生重复 style 属性导致
        # 专用样式被通用链接色覆盖。字重用强调字重：链接密度高（引用编号、
        # 数据库 ID），Bold(700) 会让行内链接比正文"跳"得过头。
        _accent = _tm.color('accent', theme_key)
        html = re.sub(
            r'<a(?![^>]*style=)(\s+href=)',
            lambda m: (f'<a style="color:{_accent}; text-decoration:none; '
                       f'font-weight:{_emph_value}; {_emph_family_decl}"{m.group(1)}'),
            html)
        parts = re.split(r'(<[^>]+>)', html)
        for i in range(0, len(parts), 2):
            if parts[i]:
                parts[i] = re.sub(
                    r'[^\s&;]{40,}',
                    lambda m: '\u200b'.join(list(m.group(0))),
                    parts[i]
                )
        html = ''.join(parts)

        # 注入全局字体栈（西文族优先、CJK 族回退）与正文文字色：
        # style 属性统一用双引号，避免族名内单引号截断属性导致声明失效；
        # 显式 color 让所有无独立样式的节点跟随 theme_key，避免正文与主题色
        # 节点不同源时出现反色。
        final_html = (f"<div style=\"font-family: {qt_font_family_css()}; "
                      f"color: {_text_main};\">{html}</div>")

        return final_html

    # ------------------------------------------------------------------
    # 块拆分：为表格 / 引用 / 代码块 / 思考链提供独立滚动容器
    # ------------------------------------------------------------------
    @classmethod
    def _matching_close_end(cls, html: str, start: int, tag: str) -> int:
        """返回 ``tag`` 元素配对的闭合标签之后的位置；找不到返回 -1。

        采用同名标签计数法：只统计同名的开/闭标签，忽略其它标签与自闭合
        标签。渲染管线的 HTML 由 python-markdown 生成，结构良好（代码块
        内的 ``<`` 已转义为实体），该计数法足够稳健。
        """
        depth = 1
        pos = start
        while True:
            m = _TAG_TOKEN_RE.search(html, pos)
            if not m:
                return -1
            pos = m.end()
            if m.group(2).lower() != tag:
                continue
            if m.group(1):  # 闭合标签
                depth -= 1
                if depth == 0:
                    return m.end()
            elif not m.group(0).endswith('/>'):
                depth += 1

    @classmethod
    def _strip_outer_wrapper(cls, html: str):
        """剥掉最外层样式 wrapper，返回 ``(inner_html, wrapper_open_tag)``。

        wrapper 由 :meth:`markdown_to_html` 注入（``<div style="font-family:
        ...; color: ...">``），它包住整篇文档；拆分块时必须先剥掉，否则
        首/末个文本片段会各自带着未配对的 ``<div>``。历史调用点会把「上一
        轮渲染结果」再次送进管线，此时旧 wrapper 已被
        :meth:`_reset_injected_styles` 降级为裸 ``<div>``，一并剥离。
        """
        wrapper = ""
        m = cls._WRAPPER_SIGN_RE.match(html)
        if m:
            close_start = html.rfind('</div>')
            if close_start > m.end():
                wrapper = m.group(0)
                html = html[m.end():close_start]

        stripped = True
        while stripped:
            stripped = False
            m2 = re.match(r'<div\s*>', html)
            if m2:
                end = cls._matching_close_end(html, m2.end(), 'div')
                if end > 0 and not html[end:].strip():
                    html = html[m2.end():end - len('</div>')]
                    stripped = True
        return html, wrapper

    @classmethod
    def split_overflow_blocks(cls, html: str):
        """把渲染后的正文 HTML 拆成若干可独立承载滚动条的块。

        返回 ``[(kind, html), ...]``，``kind`` 取值：

        * ``text``  —— 常规富文本（段落 / 标题 / 列表 / 行内代码 / 图片…）；
        * ``think`` —— 思考链折叠面板（带 ``data-navis-think`` 的顶层 div）；
        * ``table`` —— Markdown 表格；
        * ``quote`` —— Markdown 引用（``>``，**不含**文末参考文献区）；
        * ``code``  —— 围栏代码块。

        只拆``顶层``容器：嵌套在引用内的代码块仍归外层引用块管辖，避免
        同一段内容被套两层滚动条。未命中任何目标块时原样返回单个 text 块，
        保证「普通回答」与旧的单文档渲染路径完全一致。
        """
        if not html:
            return []

        inner, wrapper = cls._strip_outer_wrapper(html)

        def _wrap(fragment: str) -> str:
            return f"{wrapper}{fragment}</div>" if wrapper else fragment

        blocks = []
        buf_start = 0
        pos = 0
        hit = False
        while True:
            m = _TAG_TOKEN_RE.search(inner, pos)
            if not m:
                break
            pos = m.end()
            if m.group(1):  # 闭合标签，跳过
                continue
            name = m.group(2).lower()
            tag_text = m.group(0)
            if name == 'table':
                kind = 'table'
            elif name == 'blockquote':
                kind = 'quote'
            elif name == 'pre':
                kind = 'code'
            elif name == 'div' and 'data-navis-think' in tag_text:
                kind = 'think'
            else:
                continue
            if tag_text.endswith('/>'):
                continue
            end = cls._matching_close_end(inner, m.end(), name)
            if end < 0:
                continue
            if m.start() > buf_start:
                lead = inner[buf_start:m.start()]
                if lead.strip():
                    blocks.append(('text', _wrap(lead)))
            blocks.append((kind, inner[m.start():end]))
            buf_start = end
            pos = end
            hit = True

        if not hit:
            return [('text', html)]
        tail = inner[buf_start:]
        if tail.strip():
            blocks.append(('text', _wrap(tail)))
        return blocks

    @staticmethod
    def _strip_tool_json(text: str) -> str:
        """从导出文本剥离"工具调用"形态的 JSON/JSONL，避免中间产物混入。

        agent 流式（reasoning 模型无原生 function-calling 时）可能把 fallback 的
        工具调用 JSON（``{"name": ..., "arguments": ...}``）当作正文 token 输出，
        造成"正文中断后跟着一段工具调用 JSONL"。这里只删明确是工具调用的对象
        （含 ``name`` 且 ``arguments``/``parameters``/``input`` 键），保留正文里
        合法的 JSON 示例/表格数据。
        """
        import json as _json

        def _is_tool_call(obj):
            return (isinstance(obj, dict)
                    and "name" in obj
                    and any(k in obj for k in ("arguments", "parameters", "input")))

        # 1) ```json ... ``` 围栏块：整块是工具调用则删除
        def _fence_repl(m):
            block = m.group(1)
            try:
                data = _json.loads(block)
                return "" if _is_tool_call(data) else m.group(0)
            except Exception:
                return m.group(0)

        text = re.sub(r"```json[ \t]*\r?\n(.*?)```",
                      _fence_repl, text, flags=re.DOTALL | re.IGNORECASE)

        # 2) 独立的裸 JSON 工具调用（含 JSONL，每行一个对象）：括号平衡扫描
        out = []
        i, n = 0, len(text)
        while i < n:
            if text[i] == "{":
                depth = 0
                j = i
                while j < n:
                    if text[j] == "{":
                        depth += 1
                    elif text[j] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                if depth == 0:
                    candidate = text[i:j + 1]
                    try:
                        data = _json.loads(candidate)
                        if _is_tool_call(data):
                            i = j + 1
                            continue
                    except Exception:
                        pass
            out.append(text[i])
            i += 1
        return "".join(out).strip()

    @staticmethod
    def html_to_markdown(html: str) -> str:
        """把 HTML 片段转成 Markdown：能转的转，转不了的保留精简 HTML。

        导出/复制 Markdown 时会混入渲染管线注入或模型自己输出的 HTML。旧实现两条路
        都不对：复制 MD 原样返回（``.md`` 里塞满 ``<div style=...>``）；导出 MD 用
        ``re.sub(r'<[^>]+>', '')`` 剥标签（表格塌成一行行文字、图片直接丢失）。这里
        统一按"Markdown 优先"转换，规则见 :class:`_HtmlToMarkdown`。
        """
        if not html or "<" not in html:
            return html or ""
        parser = _HtmlToMarkdown()
        try:
            parser.feed(html)
            parser.close()
        except Exception as e:
            logger.debug("html_to_markdown failed, keeping raw HTML: %s", e)
            return html
        return _tidy_markdown(parser.result())

    @staticmethod
    def strip_internal_links(text):
        """把内部协议链接还原为纯文本，供导出/复制 Markdown 使用。

        渲染管线会注入只在应用内可点击的路由链接：``cite://``（文献与 PDF 跳转）、
        ``mermaid://``（图表查看/编辑）、``think://``（思考折叠）。它们写进 .md/.txt
        毫无意义，这里统一还原为可见文字（``cite://`` 保留 ``[1]`` 编号），并清掉
        裸露的协议 URL。Markdown 与 HTML 两种形态都处理，便于在转 Markdown 前后调用。
        """
        if not text:
            return text
        # Markdown 形态的内部链接 → 保留锚文本（锚文本本身可能含 []，如 [[1]](cite://…)）
        text = re.sub(r"\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\((?:cite|mermaid|think|ref)://[^)]*\)",
                      r"\1", text, flags=re.IGNORECASE)
        # HTML 形态的内部链接 → 保留锚文本（含行内引用 ref://，还原为 [n]）
        text = re.sub(r"<a\b[^>]*?href=['\"](?:cite|mermaid|think|ref)://[^'\"]*['\"][^>]*>(.*?)</a>",
                      r"\1", text, flags=re.DOTALL | re.IGNORECASE)
        # 裸露的内部协议 URL
        text = re.sub(r"(?:cite|mermaid|think|ref)://[^\s)\"'<>]+", "", text, flags=re.IGNORECASE)
        return text

    @staticmethod
    def _strip_internal_markup(text):
        """移除仅在应用内有效的交互提示块（导出/复制 Markdown 前调用）。"""
        if not text:
            return text
        text = _MERMAID_UI_HTML_RE.sub("", text)
        text = _MERMAID_UI_TEXT_RE.sub("", text)
        text = _DROP_SUBTREE_RE.sub("", text)
        return TextFormatter.strip_internal_links(text)

    @staticmethod
    def clean_text_for_export(text, include_citations=True, markdown_mode=False):
        """清理待导出文本（剥离运行标识、可选保留引用区）。

        ``markdown_mode=True`` 时残留 HTML 交给 :meth:`html_to_markdown` 转成 Markdown
        语法（表格/图片/加粗/代码…）；``False`` 保持旧的"剥掉所有标签"行为，供 TXT
        纯文本导出使用。
        """
        def _strip(value):
            # 顺序要紧：先摘掉只在应用内有效的交互块/路由链接与样式脚本，再决定
            # 转 Markdown 还是纯文本，否则 cite://、mermaid:// 会被写进导出文件。
            cleaned = TextFormatter._strip_internal_markup(value)
            if markdown_mode:
                return TextFormatter.html_to_markdown(cleaned)
            return re.sub(r"<[^>]+>", "", cleaned.replace("<br>", "\n")).strip()

        final_match = re.search(r'\[FINAL_ANSWER\]\s*', text, flags=re.IGNORECASE)
        if final_match:
            text = text[final_match.end():]
        else:
            text = re.sub(r'<(think|mcp_process)>.*?(?:</\1>|$)', '', text, flags=re.DOTALL | re.IGNORECASE)

        # 全局清理不需要的运行标识与文字
        text = re.sub(r"\[CLEAR_SEARCH\]|\[START_LLM_NETWORK\]|\[\s*FOLLOW[_-]?\s*UPS?\s*\]", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\[AI is reasoning in the background\.\.\.\]", "", text, flags=re.IGNORECASE)
        text = re.sub(r"Initializing\.\.\.", "", text, flags=re.IGNORECASE)
        text = re.sub(r"Reasoning & Tool Execution", "", text, flags=re.IGNORECASE)

        if include_citations and "<b>📚 Cited Sources:</b>" in text:
            parts = text.split("<b>📚 Cited Sources:</b><br>")
            main_text = _strip(parts[0])
            citations_text = "\n\n📚 Reference:\n"
            if len(parts) > 1:
                raw_cites = parts[1]
                matches = re.findall(r"<b>\[(\d+)\]</b>\s*(.*?)\s*\(Page (\d+)\)", raw_cites)
                for m in matches:
                    idx, name, page = m
                    citations_text += f"[{idx}] {name.strip()} (第 {page} 页)\n"
            text = main_text + citations_text
        else:
            text = _strip(text)

        # 兜底：剥离混入正文的工具调用 JSON/JSONL（reasoning fallback 常见泄漏），
        # 避免导出里出现"正文中断后跟一段工具 JSON"。
        return TextFormatter._strip_tool_json(text).strip()

    @staticmethod
    def hide_think_tags(text, for_display=False):
        final_answer_match = re.search(r'\[FINAL_ANSWER\]\s*', text, flags=re.IGNORECASE)
        if final_answer_match:
            cleaned = text[final_answer_match.end():]
            return re.sub(r'</?(think|mcp_process)\s*>', '', cleaned, flags=re.IGNORECASE).strip()

        # 修改为匹配两种标签
        cleaned = re.sub(r'<(think|mcp_process)>.*?(?:</\1>|$)', '', text, flags=re.DOTALL | re.IGNORECASE)

        if ('<think>' in text or '<mcp_process>' in text) and not cleaned.strip():
            if for_display:
                from src.core.theme_manager import ThemeManager
                tm = ThemeManager()
                return f"<span style='color:{tm.color('text_muted')}; font-style:italic;'>[AI is working in the background...]</span>"
            return ""
        return cleaned.lstrip()

    @staticmethod
    def clean_text_for_copy(text):
        return TextFormatter.clean_text_for_export(text, include_citations=False)


    @staticmethod
    def format_response(text, index, expanded_indices, user_toggled_thinks, mermaid_cache):
        """统一处理包含 Mermaid 图表和 Think 面板的对话渲染"""
        if not text:
            return ""

        tm = ThemeManager()
        pattern = r'```mermaid\s*\n(.*?)\n```'

        def repl_mermaid(match):
            code = match.group(1).strip()
            code_hash = hashlib.md5(code.encode('utf-8')).hexdigest()
            mermaid_cache[code_hash] = code  # 存入外部传入的字典中
            return (
                f"<br><div style='padding:12px; margin: 8px 0; border:1px solid {tm.color('accent')}; border-radius:6px; background-color: transparent;'>"
                f"<div style='margin-bottom: 5px;'><b>Mermaid Diagram Generated</b></div>"
                f"<a href='mermaid://view?hash={code_hash}' style='color:{tm.color('accent')}; text-decoration:none; font-weight:{_strong_css()};'>"
                f"Click here to view / edit interactive diagram</a></div><br>")

        processed_text = re.sub(pattern, repl_mermaid, text, flags=re.DOTALL | re.IGNORECASE)
        return TextFormatter.format_chat_text(processed_text, index, expanded_indices, user_toggled_thinks)

    @staticmethod
    def handle_link_click(url, parent_widget, mermaid_cache, user_toggled_thinks, expanded_indices,
                          render_callback=None):
        """统一分发系统的自定义链接路由 (mermaid://, think://, cite:// 等)"""
        from PySide6.QtWidgets import QWidget
        from PySide6.QtCore import QUrlQuery, QUrl
        from PySide6.QtGui import QDesktopServices
        import os, tempfile, shutil

        # 入参兼容：AI 气泡的 anchorClicked 传入 QUrl，而用户气泡的
        # sig_link_clicked 传 str（linkActivated 亦为 str），统一归一化。
        if isinstance(url, str):
            url = QUrl(url)

        qt_parent = parent_widget if isinstance(parent_widget, QWidget) else None

        scheme = url.scheme()
        query = QUrlQuery(url)

        # 0. 行内引用（ref://cite?n=N）：由气泡层就地弹出悬停卡 / 详情面板处理，
        #    此处显式拦截，避免落到最后的 QDesktopServices 打开无效协议。
        if scheme == "ref":
            return

        # 1. 处理 Mermaid 图表
        if scheme == "mermaid":
            code_hash = query.queryItemValue("hash")
            code = mermaid_cache.get(code_hash, "")
            if code:
                if getattr(parent_widget, 'mermaid_viewer', None) is None:
                    from src.ui.components.mermaid_viewer import MermaidViewer
                    parent_widget.mermaid_viewer = MermaidViewer(qt_parent)
                parent_widget.mermaid_viewer.load_diagram(code)
            else:
                ToastManager().show("Diagram data lost. Please ask the AI to generate it again.", "error")
            return

        # 2. 处理 Think 折叠面板
        if scheme == "think":
            # 兼容 host 或 path (应对归一化)
            action = url.host() if url.host() else url.path().strip('/')
            idx_str = query.queryItemValue("index")
            idx = int(idx_str) if idx_str and idx_str.lstrip('-').isdigit() else -1

            if idx != -1:
                user_toggled_thinks.add(idx)
                if action == 'expand':
                    expanded_indices.add(idx)
                else:
                    expanded_indices.discard(idx)

                if render_callback:
                    render_callback(idx)
            return

        # 3. 处理文献/PDF引用跳转
        if scheme == "cite":
            # 必须用 FullyDecoded 取值：QUrlQuery 默认的 PrettyDecoded 不解码
            # %3A（冒号），而 cite:// 各生产方（chat_tasks / attachments /
            # send_flow / ncbi）统一用 urllib.parse.quote 编码路径，Windows
            # 盘符路径会被解码成 "C%3A\..." 导致文件永远找不到。
            fully_decoded = QUrl.ComponentFormattingOption.FullyDecoded
            file_path = query.queryItemValue("path", fully_decoded)

            if file_path.startswith(("http://", "https://")):
                QDesktopServices.openUrl(QUrl(file_path))
                return

            text_snippet = query.queryItemValue("text", fully_decoded)
            source_name = query.queryItemValue("name", fully_decoded)

            if os.path.exists(file_path):
                ext = source_name.lower().split('.')[-1] if '.' in source_name else ""

                if ext == 'pdf':
                    from src.ui.components.pdf_viewer import InternalPDFViewer
                    if getattr(parent_widget, 'pdf_viewer', None) is None:
                        parent_widget.pdf_viewer = InternalPDFViewer(qt_parent)
                    parent_widget.pdf_viewer.load_document(file_path, 0, text_snippet, display_name=source_name)

                elif ext in ['md', 'txt', 'csv', 'json']:
                    from src.ui.components.pdf_viewer import InternalTextViewer
                    if getattr(parent_widget, 'text_viewer', None) is None:
                        parent_widget.text_viewer = InternalTextViewer(qt_parent)
                    parent_widget.text_viewer.load_document(file_path, text_snippet, display_name=source_name)

                else:
                    temp_dir = tempfile.gettempdir()
                    safe_name = source_name if source_name else "document.bin"
                    temp_file_path = os.path.join(temp_dir, f"scholar_navis_view_{safe_name}")
                    try:
                        shutil.copy2(file_path, temp_file_path)
                        QDesktopServices.openUrl(QUrl.fromLocalFile(temp_file_path))
                    except Exception as e:
                        ToastManager().show(f"Failed to invoke external program: {str(e)}", "error")
            else:
                ToastManager().show(f"File not found: {source_name or file_path}", "error")
            return

        # 3.5 本地文件链接：图片走内部查看器（支持双击打开/保存），其余交由系统默认程序
        if scheme == "file":
            try:
                file_path = url.toLocalFile()
                if not file_path:
                    from urllib.parse import unquote, urlparse
                    file_path = unquote(urlparse(url.toString()).path)
                    if sys.platform == "win32" and file_path.startswith("/"):
                        file_path = file_path.lstrip("/")

                from src.core.image_utils import is_image_file
                from src.ui.components.image_viewer import open_image_viewer

                if file_path and os.path.exists(file_path) and is_image_file(file_path):
                    open_image_viewer(file_path, parent=qt_parent)
                    return
                # 非图片本地文件保留原有行为（交由系统默认程序打开）
            except Exception as e:
                logger.warning(f"Failed to open local file link: {e}")

        # 4. 普通网络链接交由系统默认浏览器
        url_str = url.toString() if hasattr(url, 'toString') else str(url)
        QDesktopServices.openUrl(QUrl(url_str))


