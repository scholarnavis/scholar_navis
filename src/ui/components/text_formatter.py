import re
import logging
import markdown
import os
import sys
import tempfile
import shutil
import hashlib
from urllib.parse import urlparse, parse_qs
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from src.core.theme_manager import ThemeManager
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
    """返回可安全内嵌进 Qt 富文本 style 属性的单族名（带单引号）。

    只取一个真实族名：Qt 不支持字体栈，多余族名会导致声明整体失效。
    单族选择走 :func:`pick_cjk_font_family`，保证中文字形由注入族直接
    渲染，不依赖系统回退链。
    """
    families = resolve_qt_font_families()
    return f"'{pick_cjk_font_family(families)}'"


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
            installed = set(QFontDatabase().families())
            resolved = next(
                (c for c in _MONO_FAMILY_CANDIDATES if c in installed), resolved)
        except Exception as e:
            logger.warning(f"Failed to resolve monospace font, fallback to Courier New: {e}")
        logger.info(f"Resolved monospace font family: {resolved}")
        _mono_family_cache = resolved
    return f"'{_mono_family_cache}'"


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

        final_answer_match = re.search(r'\[FINAL_ANSWER\]\s*', text, flags=re.IGNORECASE)

        # 统一规范化标签
        text = re.sub(r'<\s*think\s*>', '<think>', text, flags=re.IGNORECASE)
        text = re.sub(r'<\s*/\s*think\s*>', '</think>', text, flags=re.IGNORECASE)
        text = re.sub(r'<\s*mcp_process\s*>', '<mcp_process>', text, flags=re.IGNORECASE)
        text = re.sub(r'<\s*/\s*mcp_process\s*>', '</mcp_process>', text, flags=re.IGNORECASE)

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
                is_expanded = not is_closed

            action = "collapse" if is_expanded else "expand"
            icon_name = "chevron-down" if is_expanded else "chevron-right"
            icon_html = f"<img src='assets/icons/{icon_name}.svg' width='14' height='14' style='vertical-align: middle;' />"

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

            main_text = re.sub(r'<br\s*/?>', '\n', main_text, flags=re.IGNORECASE)

            rendered_main_html = TextFormatter.markdown_to_html(main_text)
            final_html += f"\n\n{rendered_main_html}"

        return final_html

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
        # 1. 修复连成一行的水平分割线
        processed_text = re.sub(r'(?<=\S)\s+(--+)\s+(?=\S)', r'\n\n\1\n\n', processed_text)
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
        _text_main = _tm.color('text_main', theme_key)
        _border = _tm.color('border', theme_key)
        for _lvl, _size in ((1, 21), (2, 18), (3, 16), (4, 15), (5, 14), (6, 13)):
            _h_style = (f"color:{_text_main}; font-size:{_size}px; "
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
                                         f"font-weight:bold; border:{_cell_border}; "
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
        replacements = [
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
        # 专用样式被通用链接色覆盖。
        _accent = _tm.color('accent', theme_key)
        html = re.sub(
            r'<a(?![^>]*style=)(\s+href=)',
            lambda m: (f'<a style="color:{_accent}; text-decoration:none; '
                       f'font-weight:bold;"{m.group(1)}'),
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

        # Qt 富文本引擎不支持 CSS 逗号字体栈；这里只注入第一个真实族名，
        # 且 style 属性统一用双引号，避免族名内单引号截断属性导致声明失效。
        # 同时显式注入正文文字色：正文/段落/列表/加粗等无独立样式的节点由
        # 容器调色板着色，而调色板（qdarktheme）与 ThemeManager 当前主题在
        # 极端时序下可能不一致，导致"主题色节点正确、正文节点反色"。外div
        # 显式 color 让所有无样式文本跟随 theme_key，与主题色节点同源。
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
    def clean_text_for_export(text, include_citations=True):
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
            main_text = re.sub(r"<[^>]+>", "", parts[0].replace("<br>", "\n")).strip()
            citations_text = "\n\n📚 Reference:\n"
            if len(parts) > 1:
                raw_cites = parts[1]
                matches = re.findall(r"<b>\[(\d+)\]</b>\s*(.*?)\s*\(Page (\d+)\)", raw_cites)
                for m in matches:
                    idx, name, page = m
                    citations_text += f"[{idx}] {name.strip()} (第 {page} 页)\n"
            text = main_text + citations_text
        else:
            text = re.sub(r"<[^>]+>", "", text.replace("<br>", "\n")).strip()

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
                f"<a href='mermaid://view?hash={code_hash}' style='color:{tm.color('accent')}; text-decoration:none; font-weight:bold;'>"
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


