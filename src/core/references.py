"""参考文献注册表（唯一事实来源）
================================

编号从哪里来
------------
**编号由受理来源的工具分配**，不是模型写、也不是事后从正文里猜出来的：

* 检索类工具（本地技能 / MCP）返回的每条来源在进入模型视野之前就拿到编号，
  工具结果里随条目附带 :func:`citation_token` 生成的**带超链接的引用文本**
  （``<a href="ref://cite?n=3">[3]</a>``）；
* ``cite_references`` 工具受理模型提交的著录时同样当场分配编号，并把 token 回传；
* 知识库文档在 Context 里已经写成 ``--- [Document n] ---``，编号同样先于正文存在。

模型要做的只是**原样粘贴工具给它的 token**。程序不再扫描正文中的自拟别名
（``[lariguet2004]`` 一类 ASCII key），因此也不存在"别名无人登记 → 句子失去引文"
或"编号按首次出现重排导致列表与正文错位"这两类问题：编号在工具调用那一刻就
固定下来，正文只能引用既有编号。

唯一事实来源（高内聚）
----------------------
``ReferenceItem`` / ``ReferenceRegistry`` 同时服务三条链路，避免多处各写一套：

1. 编号分配与引用 token 生成（KB 文档、在线来源、模型登记的文献共用同一编号空间）；
2. 正文末尾参考文献块的 HTML 渲染（气泡展示 / 导出 Markdown / 复制）；
3. UI 悬停卡与详情面板所需的结构化数据（``to_dict_list``），以及收尾时"按正文
   出现顺序定稿编号"（:meth:`ReferenceRegistry.renumber_by_appearance`）。

编号有两个层次，别混用：**内部来源号**（工具受理顺序，模型粘贴 token 时用）与
**展示编号**（收尾时按出现顺序分配的 1..K）。生成过程中引用标记一律不展示
（:func:`strip_citation_markers`），用户只会看到后者。

页脚标记与 ``ref://`` 引用路由的形态集中在此处定义，``text_formatter`` /
``chat_bubble`` / ``prompt_rules`` 统一引用，保证"同一件事只在一处定义"。
"""

from __future__ import annotations

import html as _html
import logging
import os
import re
import threading
from dataclasses import dataclass, asdict, replace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.core.think_tags import strip as strip_think_blocks

logger = logging.getLogger("Core.References")

# --------------------------------------------------------------------------- #
#  UI 页脚标记（单一事实来源）
# --------------------------------------------------------------------------- #
#: 参考文献块起始的分隔线（历史格式，``follow_ups`` 依赖该字面量做页脚分离）。
FOOTER_RULE_HTML = "<br><hr style='border:0; height:1px; background:#444; margin:15px 0;'>"
#: 参考文献块标题（保留历史字面量，兼容既有导出/复制解析）。
CITES_TITLE = "📚 Cited Sources:"
CITES_HEADER_HTML = f"<b>{CITES_TITLE}</b><br>"
#: 页脚整体起始标记：正文/追问与参考文献块的分界点。
FOOTER_MARKER = FOOTER_RULE_HTML + CITES_HEADER_HTML

#: Provenance（运行追溯日志）块标题。它由 chat_tasks 追加在同一段页脚末尾，
#: 属于**本机运行信息**：导出/复制时随页脚整体丢弃，只有参考文献行会被回收。
PROVENANCE_TITLE = "📊 Provenance (trace log):"
PROVENANCE_HEADER_HTML = f"<b>{PROVENANCE_TITLE}</b><br>"

#: 正文行内引用标记 ``[n]``（n 为 1~3 位数字）：编号由工具/程序分配，模型只照抄。
INLINE_CITE_RE = re.compile(r"\[(\d{1,3})\]")

#: 引用 token 的应用内路由（**协议唯一事实来源**）：工具回传的 token、正文渲染出的
#: 锚点、气泡的悬停/点击识别都只认这一个形态，任何一侧改协议都必须改这里。
CITE_LINK_TEMPLATE = "ref://cite?n={index}"
CITE_LINK_RE = re.compile(r"ref://cite\?n=(\d{1,3})", re.IGNORECASE)

#: "未核实"标记文案（**唯一事实来源**）：参考文献列表行尾与导出重排版共用同一
#: 文案，避免同一条目在不同出口出现两种说法。判定标准见
#: :attr:`ReferenceItem.needs_manual_review`。
UNVERIFIED_LABEL = "未核实"
UNVERIFIED_MARK_HTML = (
    f" <span style='color:#C0392B; font-size:12px;'>（{UNVERIFIED_LABEL}）</span>")

#: 中文输入法/模型常把编号写成全角 ``［27］`` 或 ``【27】``；先归一成 ASCII，
#: 否则渲染与"用了哪些条目"的选取都会漏掉它。只归一**数字**编号：字母形态的
#: 方括号用法是正文自己的内容，不属于引用协议。
_FULLWIDTH_CITE_RE = re.compile(r"[［【]\s*(\d{1,3})\s*[］】]")


def normalize_citation_brackets(text: str) -> str:
    if not text:
        return text or ""
    return _FULLWIDTH_CITE_RE.sub(lambda m: f"[{m.group(1)}]", str(text))


def citation_href(index: int) -> str:
    """引用编号的应用内跳转地址（``ref://cite?n=N``）。"""
    return CITE_LINK_TEMPLATE.format(index=int(index))


def citation_token(index: int) -> str:
    """工具回传给模型的**可粘贴引用 token**：带超链接的编号文本。

    这里的编号是**内部来源号**（受理顺序），只在模型与程序之间流转：用户看到的
    是收尾时按出现顺序重排后的 1..K（见 :meth:`ReferenceRegistry.renumber_by_appearance`）。
    模型只负责原样粘贴 token，程序据此知道每一处引用的是哪条来源。
    """
    idx = int(index)
    return f'<a href="{citation_href(idx)}">[{idx}]</a>'


#: 正文里的一处引用标记（两种等价写法，含其前面的空白）。
#: 锚点分支必须整体排在前面：否则锚文本里的 ``[n]`` 会被当成裸编号单独处理。
CITE_MARK_RE = re.compile(
    r"[ \t]*(?:<a\b[^>]*href=['\"]" + CITE_LINK_RE.pattern + r"['\"][^>]*>[^<]*</a>"
    r"|" + INLINE_CITE_RE.pattern + r")",
    re.IGNORECASE)


def _mark_id(match: "re.Match[str]") -> Optional[int]:
    """取出一处引用标记指向的来源号（锚点分支为 group(1)，裸编号为 group(2)）。"""
    value = match.group(1) or match.group(2)
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


#: 引用 token 的**开标签**（不含锚文本）：用于识别"只发了半截"的锚点。
_OPEN_CITE_TAG_RE = re.compile(
    r"<a\b[^>]*href=['\"]" + CITE_LINK_RE.pattern + r"['\"][^>]*>", re.IGNORECASE)


def _drop_unclosed_cite_anchors(text: str) -> str:
    """把"只发了开标签、还没发 ``</a>``"的引用锚点降级成裸编号。

    流式生成时 token 是一个字符一个字符到达的：锚点可能只到一半，直接渲染会让
    用户看到 ``<a href="ref://cite?n=3">[3`` 这样的半截 HTML。去掉开标签后，锚文本
    里的 ``[N]`` 仍在，会被当作裸编号处理（隐藏或重排）。有闭合标签的锚点原样
    保留，交给正常匹配。
    """
    kept: List[str] = []
    cursor = 0
    for match in _OPEN_CITE_TAG_RE.finditer(text):
        if text.find("</a>", match.end()) != -1:
            continue            # 完整锚点：原样保留
        kept.append(text[cursor:match.start()])
        cursor = match.end()
    if not kept:
        return text
    kept.append(text[cursor:])
    return "".join(kept)


#: 代码块 / 行内代码：里面的 ``[1]`` 是代码本身（``arr[1]``、数组示例），
#: 不是引用。两个处理函数都必须跳过这些片段，否则会把代码改坏。
_CODE_SPAN_RE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)


def _code_segments(text: str) -> List[Tuple[bool, str]]:
    """把文本切成 ``[(是否代码, 片段), …]``，代码片段原样保留。"""
    segments: List[Tuple[bool, str]] = []
    cursor = 0
    for match in _CODE_SPAN_RE.finditer(text):
        if match.start() > cursor:
            segments.append((False, text[cursor:match.start()]))
        segments.append((True, match.group(0)))
        cursor = match.end()
    if cursor < len(text):
        segments.append((False, text[cursor:]))
    return segments or [(False, text)]


def _map_outside_code(text: str, func) -> str:
    """只对代码之外的片段应用 ``func``（代码原样保留）。"""
    return "".join(chunk if is_code else func(chunk)
                   for is_code, chunk in _code_segments(text))


def strip_citation_markers(text: str) -> str:
    """抹掉正文里的全部引用标记（锚点形态与裸编号），用于生成过程中的中间态显示。

    生成阶段用户不该看到任何引用：编号要等整条回答收尾、按出现顺序统一分配，
    中途出现的 ``[7]`` 既没有列表可对，收尾时又会被整体改写。标记连同其前面的
    空格一起去掉，避免"...是必需的 。"这种悬空空格。

    代码块与行内代码不参与处理：``arr[1]`` 里的 ``[1]`` 是代码，不是引用。
    """
    if not text:
        return text or ""
    text = _drop_unclosed_cite_anchors(normalize_citation_brackets(str(text)))
    cleaned = _map_outside_code(text, lambda chunk: CITE_MARK_RE.sub("", chunk))
    # 流式截断处可能正停在半个标记上（"[3"、`<a href="ref://cite?n=3`）：
    # 这些残片既点不开，也不该闪给用户看。只在"尾部不在代码里"时清理。
    tail_is_code = _code_segments(cleaned)[-1][0]
    if not tail_is_code:
        cleaned = re.sub(r"<a\b[^>]*ref://cite[^>]*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\[\d{0,3}$", "", cleaned)
    return cleaned


#: 条目文本的展示上限（超出仅用于展示截断，原始数据不丢）。
_SNIPPET_DISPLAY_MAX = 2000


def _escape(value: Any) -> str:
    """转义为可安全内联进 Qt 富文本的文本（保留引号，HTML 实体转义）。"""
    return _html.escape("" if value is None else str(value), quote=True)


#: 标题里"原标题自带"的斜体信号：命中才算"来源确实标了斜体"。三类写法都算证据——
#:   * HTML / JATS 标签 ``<i>`` / ``<em>`` / ``<italic>``（PubMed、Crossref 的元数据
#:     就是这么标物种名与基因名的）；
#:   * LaTeX ``\textit{...}`` / ``\emph{...}``；
#:   * Markdown 单星号强调 ``*...*``（前后紧邻星号的 ``**`` 不算）。
#: 刻意**不认**下划线 ``_..._``：基因名、序列标识、DOI 里下划线遍地都是，认它等于
#: 把正常字符当标记吞掉。
_TITLE_ITALIC_RE = re.compile(
    r"<(?P<tag>i|em|italic)\b[^>]*>(?P<tagged>.*?)</(?P=tag)\s*>"
    r"|\\(?:textit|emph)\{(?P<braced>[^{}]*)\}"
    r"|(?<!\*)\*(?![\s*])(?P<starred>[^*\n]+?)(?<![\s*])\*(?!\*)",
    re.DOTALL | re.IGNORECASE)

#: 未能配对的斜体标签（残缺 / 交错）：它们是来源的元数据标记，不该当正文显示，
#: 在普通段落里直接丢弃。
_STRAY_ITALIC_TAG_RE = re.compile(r"</?\s*(?:i|em|italic)\b[^>]*>", re.IGNORECASE)


_SUB_TRANSLATION = str.maketrans(
    "0123456789+-=()aehijklmnoprstuvx",
    "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ")
_SUP_TRANSLATION = str.maketrans(
    "0123456789+-=()ni",
    "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ")


def normalize_bibliographic_text(value: Any, keep_italic: bool = False) -> str:
    """把标题里的 ``<sub>`` / ``<sup>`` 转成 Unicode，并清掉残留 HTML。

    Crossref/OpenAlex 标题常带 ``CO<sub>2</sub>``、``Ca<sup>2+</sup>``。
    普通 QTableWidget 不解析富文本，直接显示会露出标签；引用卡虽然用富文本，
    但 ``title_html`` 会转义标签。统一转成 Unicode 后，表格、引用卡、导出
    Markdown 都能正确显示，且不引入未转义 HTML。
    """
    text = _html.unescape("" if value is None else str(value))

    def _sub(match):
        return str(match.group(1)).translate(_SUB_TRANSLATION)

    def _sup(match):
        return str(match.group(1)).translate(_SUP_TRANSLATION)

    text = re.sub(r"<sub\b[^>]*>(.*?)</sub\s*>", _sub, text,
                  flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<sup\b[^>]*>(.*?)</sup\s*>", _sup, text,
                  flags=re.IGNORECASE | re.DOTALL)
    # 不完整的开/闭标签（例如只出现 </sup>）也不应显示成正文。
    text = re.sub(r"</?su[bp]\b[^>]*>", "", text, flags=re.IGNORECASE)
    if keep_italic:
        # 只保留斜体族标签（``i`` / ``em`` / ``italic``，PubMed、Crossref、JATS 三种
        # 写法都用），其余标签清掉——它们正是 :data:`_TITLE_ITALIC_RE` 要识别的信号，
        # 一旦在这里被删掉，"来源确实标了斜体"的证据就丢失了。不能写成
        # ``</?(?!i\b)[^>]+>``：可选斜杠会让 ``</i>`` 从"斜杠"位置开始匹配，
        # 结果开标签留下、闭标签被删，整段后续都变成斜体。
        def _strip_non_italic(match):
            tag = match.group(0)
            name = re.match(r"</?\s*([A-Za-z]+)", tag)
            keep = name is not None and name.group(1).lower() in ("i", "em", "italic")
            return tag if keep else ""
        text = re.sub(r"</?[^>]+>", _strip_non_italic, text)
    else:
        text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def title_html(title: Any) -> str:
    """把标题渲染为富文本：**默认正体**，只保留原标题自带的斜体信号。

    整条标题一律斜体既不符合学术惯例（斜体只落在物种名、基因名、刊名一类局部成分
    上），也会让整片参考文献糊成一片。因此只有 :data:`_TITLE_ITALIC_RE` 命中的片段
    才包 ``<i>``，其余一律正体。

    转义与包裹的顺序不能反：先包裹再转义会把 ``<i>`` 变成可见的 ``&lt;i&gt;``——这正是
    "标题整条斜体"与"标签被当正文显示"两种毛病并发的成因。这里先按信号切分、逐段
    转义，再只给信号段套标签；普通段里残留的孤立标签直接丢弃。

    页脚参考文献（:meth:`ReferenceItem.citation_text_html`）与引用卡标题共用本函数，
    两处"标题该怎么斜"因此只有一个说法。
    """
    text = normalize_bibliographic_text(title, keep_italic=True)
    if not text:
        return ""
    parts: List[str] = []
    cursor = 0
    for match in _TITLE_ITALIC_RE.finditer(text):
        if match.start() < cursor:      # 与上一处信号重叠：交给上一处处理
            continue
        parts.append(_escape(_STRAY_ITALIC_TAG_RE.sub("", text[cursor:match.start()])))
        inner = match.group("tagged") or match.group("braced") or match.group("starred") or ""
        parts.append(f"<i>{_escape(_STRAY_ITALIC_TAG_RE.sub('', inner))}</i>")
        cursor = match.end()
    parts.append(_escape(_STRAY_ITALIC_TAG_RE.sub("", text[cursor:])))
    return "".join(parts)


def _basename(path: str) -> str:
    try:
        return os.path.basename(str(path).replace("\\", "/")) or ""
    except Exception:  # pragma: no cover - 纯防御
        return ""


def _ensure_period(text: str) -> str:
    """补句点：已以句末标点结尾则原样返回（避免 ``et al..`` 这类重复句点）。"""
    text = str(text or "").strip()
    if not text:
        return ""
    return text if text.endswith((".", "。", "!", "！", "?", "？")) else text + "."


#: 姓名缩写形态：``J.`` / ``J.R.`` / ``JR.``（**必须带点**，用于识别"姓, 缩写"写法）。
#: 强制带点是为了不与 ``A, B, C`` 这类"纯姓氏列表"混淆——后者没有点，不应配对。
_AUTHOR_INITIALS_RE = re.compile(r"^[A-Z](?:\.?\s?[A-Z])*\.$")


def _split_authors(authors: str) -> List[str]:
    """把作者串切成"整名"列表（供缩略与著录使用）。

    作者写法有两种常见形态，逗号在两者中的含义完全不同：

    * ``A, B, C`` —— 逗号是**作者分隔符**，每段就是一个整名；
    * ``Jumper, J.; Evans, R.`` —— 逗号是**姓与缩写之间的分隔符**，整名之间用分号。

    旧实现把逗号一律当分隔符，于是 ``Jumper, J.; Evans, R.; Pritzel, A.; Green, T.``
    被切成 ``Jumper / J. / Evans / R. / …`` 八段，缩略结果成了
    ``Jumper, J., Evans et al.``——既丢作者，又把 Evans 粘进了 Jumper 的姓名里。

    规则分两级：

    1. **有强分隔符时**（分号、顿号、``&``、``and``、换行）：强分隔符已经界定了
       整名，组内的逗号属于姓名本身，不再切分——``Smith, John; Doe, Jane`` 是
       两位作者，不是四位。
    2. **只有逗号时**：才需要判断逗号是"作者分隔符"还是"姓与缩写之间的分隔符"，
       判据是后一段是否形如**带点**的缩写（``J.`` / ``J.R.`` / ``JR.``）；是则与
       前一段配成一个整名。``A, B, C`` 没有点，仍按三个作者处理。
    """
    text = str(authors or "").strip()
    if not text:
        return []
    groups = [g.strip()
              for g in re.split(r"\s*(?:;|；|、|\band\b|&|\n)\s*", text) if g.strip()]
    if len(groups) > 1:
        return groups
    tokens = [t.strip() for t in groups[0].split(",") if t.strip()]
    names: List[str] = []
    i = 0
    while i < len(tokens):
        if i + 1 < len(tokens) and _AUTHOR_INITIALS_RE.match(tokens[i + 1]):
            names.append(f"{tokens[i]}, {tokens[i + 1]}")       # 姓, 缩写
            i += 2
        else:
            names.append(tokens[i])
            i += 1
    return names


def _truncate_authors(authors: str, max_names: int = 3) -> str:
    """作者列表过长时折叠为 ``A, B, C et al.``（展示友好，语义不变）。"""
    text = str(authors or "").strip()
    if not text:
        return ""
    names = _split_authors(text)
    if len(names) <= max_names:
        return text
    # 整名内部自带逗号（"姓, 缩写"）时必须改用分号连接，否则读者分不清作者边界。
    joiner = "; " if any("," in name for name in names) else ", "
    return joiner.join(names[:max_names]) + " et al."


@dataclass
class ReferenceItem:
    """单条参考文献（也承载本地文档 / 在线来源）。

    字段刻意保持扁平，便于 JSON 序列化后在 UI 线程重建：

    * ``index``    —— 引用编号（= 正文里的 ``[n]``），由受理来源的工具分配；
    * ``title``    —— 标题；
    * ``authors``  —— 作者（字符串，分号/逗号分隔均可）；
    * ``year``     —— 年份；
    * ``journal``  —— 期刊 / 会议 / 数据库名；
    * ``doi``      —— DOI（不含 ``https://doi.org/`` 前缀亦可）；
    * ``url``      —— 在线链接（优先于 DOI 作为跳转目标）；
    * ``snippet``  —— 支撑该引用的原文片段（供溯源）；
    * ``note``     —— 引用理由 / 上下文（可选）；
    * ``path``     —— 本地文件路径（KB 文档；用于内部查看器跳转）；
    * ``page``     —— 本地文档页码；
    * ``kind``     —— ``reference`` | ``local_document`` | ``web``；
    * ``verified`` —— ``snippet`` 是否经 :mod:`~src.core.evidence` 逐字校验命中
      （True 时 UI 标注 "verbatim"；False 只可能是 snippet 为空）。
    """

    index: int
    title: str = ""
    authors: str = ""
    year: str = ""
    journal: str = ""
    doi: str = ""
    url: str = ""
    snippet: str = ""
    note: str = ""
    path: str = ""
    page: int = 1
    kind: str = "reference"
    verified: bool = False

    # ------------------------------------------------------------------ #
    #  规范化
    # ------------------------------------------------------------------ #
    def normalized(self) -> "ReferenceItem":
        """就地清洗字段：去首尾空白、DOI 去前缀、kind 归一。"""
        self.title = str(self.title or "").strip()
        self.authors = str(self.authors or "").strip()
        self.year = str(self.year or "").strip()
        self.journal = str(self.journal or "").strip()
        self.doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(self.doi or "").strip(),
                          flags=re.IGNORECASE)
        self.url = str(self.url or "").strip()
        self.snippet = str(self.snippet or "").strip()
        self.note = str(self.note or "").strip()
        self.path = str(self.path or "").strip()
        try:
            self.page = max(1, int(self.page or 1))
        except (TypeError, ValueError):
            self.page = 1
        self.kind = str(self.kind or "reference").strip().lower() or "reference"
        return self

    # ------------------------------------------------------------------ #
    #  展示
    # ------------------------------------------------------------------ #
    @property
    def dedupe_key(self) -> Optional[str]:
        """归一键：DOI/URL 优先，其次 ``title|year``；都没有则返回 None。"""
        if self.doi:
            return f"doi:{self.doi.lower().rstrip('/')}"
        if self.url:
            return f"url:{self.url.lower().rstrip('/')}"
        if self.path:
            return f"path:{self.path.lower().rstrip('/')}"
        if self.title:
            return f"title:{self.title.lower()}|{self.year}"
        return None

    @property
    def authors_display(self) -> str:
        return _truncate_authors(self.authors)

    @property
    def citation_token(self) -> str:
        """该条目的可粘贴引用 token（带超链接的编号文本，见 :func:`citation_token`）。"""
        return citation_token(self.index)

    @property
    def needs_manual_review(self) -> bool:
        """是否属于"无法自动核实、只能人工核查"的条目（列表行尾据此打标）。

        判定标准（唯一事实来源）：

        * 有外部标识（``doi`` / ``url``）或本地路径（``path``）——用户能自行点开核对，
          不打标；
        * ``verified=True``——snippet 已由程序逐字校验命中已采集原文，有程序侧证据，
          不打标；
        * 两者都没有——该条目的著录完全来自模型书写，没有可核对的落点，打标记。

        边界（刻意不打的那些）：模型**伪造 DOI/URL** 的条目会在第一条被放行（有"标识"
        但在线不可解析）。离线渲染无法判断 DOI 真伪，这类只能靠开发者模式的
        Literature Trust (live) 联网核查；因此标记的语义是"无落点可查"，不是"已证实为假"。
        """
        if self.verified:
            return False
        return not (self.doi or self.url or self.path)

    @property
    def display_name(self) -> str:
        """无作者/标题时的兜底名称（本地文件名 / 链接 / 在线来源标题）。"""
        return (self.title or _basename(self.path) or self.url
                or f"Reference {self.index}")

    @property
    def citable_link(self) -> str:
        """**可引用的稳定地址**：DOI 优先（``https://doi.org/<doi>``），其次在线链接。

        与 :attr:`primary_link` 的优先级差异是刻意的，两者服务于不同的动作：

        * :attr:`primary_link` 是"打开源文件"的目标，用户此刻的意图是看原始页面，
          因此在线链接优先；
        * 本属性是"复制引文地址"的目标，学术场景下 DOI 才是可长期寻址的标识，
          原始 URL 可能随时改版失效。

        本地文档没有可复制的网址，返回空串（调用方据此隐藏入口）。
        """
        if self.doi:
            return f"https://doi.org/{self.doi}"
        return self.url

    @property
    def primary_link(self) -> str:
        """点击条目时的跳转目标：在线链接 > DOI > 内部文档查看器。"""
        if self.url:
            return self.url
        if self.doi:
            return f"https://doi.org/{self.doi}"
        if self.path:
            from urllib.parse import quote
            return ("cite://view?path=" + quote(self.path)
                    + f"&page={self.page}"
                    + "&text=" + quote(self.snippet[:100])
                    + "&name=" + quote(_basename(self.path) or "document"))
        return ""

    def citation_text(self) -> str:
        """单行著录文本（展示用，不含编号）。

        顺序遵循学术惯例：作者. (年份). 标题. 期刊. 含 DOI 时附加。
        """
        parts: List[str] = []
        if self.authors_display:
            parts.append(_ensure_period(self.authors_display))
        if self.year:
            parts.append(f"({self.year}).")
        if self.title:
            parts.append(_ensure_period(self.title))
        if self.journal:
            parts.append(_ensure_period(self.journal))
        if not parts:
            parts.append(_ensure_period(self.display_name))
        if self.doi:
            parts.append(f"DOI: {self.doi}")
        return " ".join(parts)

    def citation_text_html(self) -> str:
        """可视化著录：**默认正体**，仅保留原标题自带的斜体信号（见 :func:`title_html`）。
        """
        parts: List[str] = []
        if self.authors_display:
            parts.append(_escape(_ensure_period(self.authors_display)))
        if self.year:
            parts.append(f"({_escape(self.year)}).")
        if self.title:
            # 句末句点必须落在斜体之外：先补句点，再交给标题渲染器决定局部斜体。
            parts.append(title_html(_ensure_period(self.title)))
        if self.journal:
            parts.append(_escape(_ensure_period(
                normalize_bibliographic_text(self.journal))))
        if not any((self.authors_display, self.year, self.title, self.journal)):
            parts.append(_escape(_ensure_period(self.display_name)))
        html = " ".join(parts)
        if self.doi:
            html += f" <span style='opacity:0.75;'>DOI: {_escape(self.doi)}</span>"
        return html

    @property
    def snippet_display(self) -> str:
        text = self.snippet or self.note
        if len(text) > _SNIPPET_DISPLAY_MAX:
            return text[:_SNIPPET_DISPLAY_MAX] + " …"
        return text

    # ------------------------------------------------------------------ #
    #  序列化
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReferenceItem":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        payload = {k: v for k, v in (data or {}).items() if k in known}
        payload.setdefault("index", 0)
        item = cls(**payload)
        return item.normalized()

    @classmethod
    def from_ai_payload(cls, raw: Dict[str, Any], index: int) -> "ReferenceItem":
        """把模型工具参数（字段名可能有别名）规整为 :class:`ReferenceItem`。

        编号一律由注册表在 :meth:`ReferenceRegistry.add` 里分配，模型提交的任何
        编号字段都会被忽略——这是"编号由工具生成"的口径，不接受模型自报数字。
        """
        data = raw or {}
        item = cls(
            index=index,
            title=str(data.get("title") or data.get("name") or ""),
            authors=str(data.get("authors") or data.get("author") or ""),
            year=str(data.get("year") or data.get("date") or ""),
            journal=str(data.get("journal") or data.get("venue")
                        or data.get("conference") or data.get("publisher") or ""),
            doi=str(data.get("doi") or ""),
            url=str(data.get("url") or data.get("link") or data.get("href") or ""),
            snippet=str(data.get("snippet") or data.get("quote") or data.get("evidence")
                        or data.get("abstract") or ""),
            note=str(data.get("note") or data.get("context") or data.get("reason") or ""),
            kind=str(data.get("source_type") or data.get("kind") or "reference"),
        )
        return item.normalized()


class ReferenceRegistry:
    """会话级引用编号注册表。

    编号空间统一：本地 KB 文档、在线 MCP 来源、模型通过 ``cite_references``
    登记的文献共用同一自增空间，从根本上避免"同一来源两个号 / 两来源同一个号"。
    编号一旦分配就固定（来源去重时复用原号），不随正文出现顺序重排——正文只是
    "引用已有的编号"，因此列表与正文不可能对不上。

    线程安全：Agent 循环会并发执行工具调用，多个在线来源可能同时登记，因此
    所有读写都用可重入锁串行化。
    """

    def __init__(self) -> None:
        self._items: Dict[int, ReferenceItem] = {}
        self._by_key: Dict[str, int] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    #  写入
    # ------------------------------------------------------------------ #
    def _next_index(self) -> int:
        return (max(self._items) + 1) if self._items else 1

    def seed(self, index: int, item: ReferenceItem) -> int:
        """以显式编号写入（KB / MCP 来源已在外部分配好编号时使用）。"""
        with self._lock:
            try:
                idx = max(1, int(index))
            except (TypeError, ValueError):
                idx = self._next_index()
            item.index = idx
            item.normalized()
            self._items[idx] = item
            key = item.dedupe_key
            if key:
                self._by_key.setdefault(key, idx)
            logger.debug("Reference seeded: [%d] %s", idx, item.display_name[:80])
            return idx

    def add(self, item: ReferenceItem, index: Optional[int] = None) -> int:
        """登记一条引用；命中归一键时复用既有编号（返回既有编号）。"""
        with self._lock:
            item.normalized()
            key = item.dedupe_key
            if key and key in self._by_key:
                existing = self._by_key[key]
                # 已有条目信息更少时，用新数据补全（不覆盖非空字段）。
                self._items[existing] = self._merge(self._items[existing], item)
                logger.debug("Reference deduplicated: [%d] %s", existing, item.display_name[:80])
                return existing
            idx = int(index) if index else self._next_index()
            item.index = idx
            self._items[idx] = item
            if key:
                self._by_key[key] = idx
            logger.debug("Reference registered: [%d] %s", idx, item.display_name[:80])
            return idx

    @staticmethod
    def _merge(old: ReferenceItem, new: ReferenceItem) -> ReferenceItem:
        """合并重复条目：新数据补全旧数据的空字段，非空字段保持首次取值。"""
        for fname in ("title", "authors", "year", "journal", "doi", "url",
                      "snippet", "note", "path"):
            if not getattr(old, fname) and getattr(new, fname):
                setattr(old, fname, getattr(new, fname))
        return old.normalized()

    def add_payloads(self, payloads: Sequence[Dict[str, Any]]) -> List[int]:
        """批量登记模型提交的结构化条目，返回分配的编号列表（顺序对应）。"""
        assigned: List[int] = []
        for raw in payloads or []:
            if not isinstance(raw, dict):
                continue
            item = ReferenceItem.from_ai_payload(raw, index=0)
            if not (item.title or item.url or item.doi or item.path):
                continue
            assigned.append(self.add(item))
        return assigned

    def load_dict_list(self, data: Iterable[Dict[str, Any]]) -> None:
        """从结构化快照恢复（UI 侧跨线程同步 / 历史重建）。"""
        with self._lock:
            for entry in data or []:
                if not isinstance(entry, dict):
                    continue
                item = ReferenceItem.from_dict(entry)
                idx = int(entry.get("index") or 0)
                if idx <= 0:
                    self.add(item)
                else:
                    self.seed(idx, item)

    # ------------------------------------------------------------------ #
    #  读取
    # ------------------------------------------------------------------ #
    def lookup(self, index: int) -> Optional[ReferenceItem]:
        with self._lock:
            try:
                return self._items.get(int(index))
            except (TypeError, ValueError):
                return None

    def items(self) -> List[ReferenceItem]:
        with self._lock:
            return [self._items[k] for k in sorted(self._items)]

    def to_dict_list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [item.to_dict() for item in self.items()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, index: object) -> bool:
        with self._lock:
            try:
                return int(index) in self._items  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return False

    # ------------------------------------------------------------------ #
    #  收尾：按出现顺序定稿编号
    # ------------------------------------------------------------------ #
    def renumber_by_appearance(self, text: str
                               ) -> Tuple[str, List["ReferenceItem"], List[int]]:
        """按**正文首次出现顺序**把引用编号定稿为 1..K，并把正文改写过来。

        生成过程中编号一律不展示（见 :func:`strip_citation_markers`），所以用户
        看到的编号就是这里分配的：第一个被引用的来源拿 ``[1]``、第二个 ``[2]``，
        列表顺序与阅读顺序天然一致。

        两条等价写法（工具 token 的 ``ref://cite?n=内部号`` 锚点、知识库文档的裸
        ``[n]``）都参与排序，改写后统一成裸 ``[n]``，由渲染管线建链。

        ``<think>`` 块不参与排序（面板默认收起，里面的示意不该把编号往后挤），但
        其中的标记会被改写成同一套编号；只在思考里出现、正文从未引用的来源整条
        抹掉——它不构成对来源的引用，留着只会让编号跳号。代码块与行内代码同样
        不参与（``arr[1]`` 是代码，不是引用）。

        :return: ``(new_text, ordered_items, unmatched)``
            - ``new_text``：编号已定稿的正文；
            - ``ordered_items``：按新编号排序的条目副本（``index`` 已改成新号）；
            - ``unmatched``：正文出现但注册表没有的编号（已从正文抹掉，记日志用）。
        """
        if not text:
            return text or "", [], []
        text = _drop_unclosed_cite_anchors(normalize_citation_brackets(str(text)))
        with self._lock:
            # 排序只看正文：思考块里的示意编号不算"引用顺序"，代码里的 [n] 更不是。
            body = strip_think_blocks(text)
            order: Dict[int, int] = {}
            unmatched: List[int] = []
            for is_code, chunk in _code_segments(body):
                if is_code:
                    continue
                for match in CITE_MARK_RE.finditer(chunk):
                    old = _mark_id(match)
                    if old is None:
                        continue
                    if old in self._items:
                        if old not in order:
                            order[old] = len(order) + 1
                    elif old not in unmatched:
                        unmatched.append(old)

            def _rewrite(match: "re.Match[str]") -> str:
                old = _mark_id(match)
                if old is None or old not in order:
                    # 未登记的编号、以及只出现在思考块里的引用：整条抹掉。
                    # 留一个指向空处的编号比不留更糟——用户会去点它。
                    return ""
                # 保留标记前面的空白：英文是 "claim [1]."，中文是 "结论[1]。"，
                # 直接拼上去会把英文的词间距吃没。
                raw = match.group(0)
                lead = raw[: len(raw) - len(raw.lstrip(" \t"))]
                return f"{lead}[{order[old]}]"

            new_text = _map_outside_code(text, lambda chunk: CITE_MARK_RE.sub(_rewrite, chunk))
            ordered = [replace(self._items[i], index=order[i])
                       for i in sorted(order, key=lambda k: order[k])]
        return new_text, ordered, unmatched

    # ------------------------------------------------------------------ #
    #  渲染
    # ------------------------------------------------------------------ #
    @staticmethod
    def render_items(items: Sequence[ReferenceItem]) -> str:
        """按给定顺序渲染参考文献块 HTML（编号取各条目 ``index``；无条目返回空串）。"""
        rows = [ReferenceRegistry._entry_html(it) for it in (items or [])]
        if not rows:
            return ""
        return "\n" + FOOTER_MARKER + "".join(rows)

    def to_html(self, indices: Optional[Iterable[int]] = None) -> str:
        """渲染正文末尾的参考文献块 HTML（无可用条目时返回空串）。

        保留此接口以兼容历史调用；新流程请用
        :meth:`renumber_by_appearance` + :meth:`render_items`（编号按出现顺序定稿）。

        :param indices: 仅渲染这些编号（通常为正文实际引用到的编号）；
                        None 表示渲染全部。
        """
        wanted = None
        if indices is not None:
            wanted = {int(i) for i in indices}
        rows: List[str] = []
        for item in self.items():
            if wanted is not None and item.index not in wanted:
                continue
            rows.append(self._entry_html(item))
        if not rows:
            return ""
        return "\n" + FOOTER_MARKER + "".join(rows)

    @staticmethod
    def _entry_html(item: ReferenceItem) -> str:
        """单条参考文献行：编号可点击跳转（在线链接 / 本地查看器）。

        无任何可核对落点的条目（见 :attr:`ReferenceItem.needs_manual_review`）在行尾
        追加"未核实"标记：它只说明"这条没有可点的出处"，不代表已被判定为假。
        """
        number = f"[{item.index}]"
        label = item.citation_text_html()
        mark = UNVERIFIED_MARK_HTML if item.needs_manual_review else ""
        link = item.primary_link
        if link:
            return (
                f"<div style='margin-bottom: 6px;'>▪ "
                f"<a style='color:#05B8CC; text-decoration:none;' href='{_escape(link)}'>"
                f"<b>{number}</b> {label}</a>{mark}</div>"
            )
        return f"<div style='margin-bottom: 6px;'>▪ <b>{number}</b> {label}{mark}</div>"


# --------------------------------------------------------------------------- #
#  导出 / 复制：把页脚参考文献收回成结构化条目并按目标格式重排版
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FooterReference:
    """从消息页脚解析回来的一条参考文献（供导出 / 复制重排版）。

    与 :class:`ReferenceItem` 的区别是只保留"能确切还原"的字段：编号、
    纯文本著录、可点击地址。页脚里的著录文本已经是拼装好的字符串，反推
    authors / year / journal 只会制造臆测数据，因此不还原为完整条目。
    """

    index: int
    text: str
    link: str


#: 页脚单条参考文献行：以行首的 ``▪`` 为锚，可选锚点链接，内容到本行 ``</div>``。
#: **刻意不锚定**编号的包裹标签（原始 ``<b>``、渲染管线改写出的 ``<span>``）与
#: 容器内联样式——只认某个具体标签时，"复制渲染结果"会因为标签被改写而漏掉全部
#: 条目（页脚在渲染后就是这种形态）。
FOOTER_ENTRY_RE = re.compile(
    r"▪\s*(?:<a\b[^>]*?href=['\"](?P<link>[^'\"]*)['\"][^>]*>)?(?P<body>.*?)</div>",
    re.DOTALL | re.IGNORECASE)

#: 剥标签后条目文本开头的编号 ``[n]``。
_ENTRY_NUMBER_RE = re.compile(r"^\s*\[(\d{1,3})\]\s*")

#: 可出现在导出文件里的地址：仅保留 http(s)，应用内路由（cite://）与本地路径
#: 换机器即失效，写进导出文件只会成为噪声。
_EXPORTABLE_LINK_RE = re.compile(r"^https?://", re.IGNORECASE)

#: 参考文献段落标题（按目标格式取用）。
REFERENCE_HEADINGS = {
    "markdown": "## References",
    "plain": "REFERENCES",
}


def parse_footer_entries(footer_html: str) -> List[FooterReference]:
    """解析页脚 HTML 里的参考文献行；无行返回空列表。"""
    found: List[FooterReference] = []
    for m in FOOTER_ENTRY_RE.finditer(footer_html or ""):
        body = _html.unescape(re.sub(r"<[^>]+>", "", m.group("body") or ""))
        body = " ".join(body.split())
        number = _ENTRY_NUMBER_RE.match(body)
        if not number:
            # 页脚里还有 Provenance 一类同样带 ▪ 的行（工具调用统计），跳过。
            continue
        label = body[number.end():].strip()
        if not label:
            continue
        link = (m.group("link") or "").strip()
        if link and not _EXPORTABLE_LINK_RE.match(link):
            link = ""
        found.append(FooterReference(index=int(number.group(1)), text=label, link=link))
    return found


#: 页脚分隔线的宽松形态：原始 ``FOOTER_RULE_HTML``，或渲染管线改写后的裸 ``<hr …>``
#: （markdown 会吃掉前缀 ``<br>`` 并重写内联样式）。
_FOOTER_RULE_RE = re.compile(r"<br\s*/?>\s*<hr\b[^>]*>|<hr\b[^>]*>", re.IGNORECASE)

#: 标题锚点与它前面的分隔线之间的最大间距：超过则不认为该分隔线属于页脚，
#: 避免把正文里正常的分隔线误划进页脚。
_FOOTER_RULE_GAP_MAX = 240


def _footer_start(text: str) -> int:
    """页脚起点：各页脚标记中最早的位置，并把紧邻其前的分隔线一并纳入。

    渲染管线会把 ``FOOTER_RULE_HTML``（``<br><hr style='…'>``）改写成裸
    ``<hr style='…'>``，因此不能只按字面量找；标题文字是稳定锚点，再向前回吞
    一条紧邻的分隔线——否则复制/导出会在正文末尾留下一条孤立的 ``---``。
    """
    positions = [i for i in (text.find(FOOTER_RULE_HTML), text.find(CITES_TITLE),
                             text.find(PROVENANCE_TITLE)) if i != -1]
    if not positions:
        return -1
    start = min(positions)
    rule = None
    for m in _FOOTER_RULE_RE.finditer(text, 0, start):
        rule = m
    if rule is not None and start - rule.start() <= _FOOTER_RULE_GAP_MAX:
        return rule.start()
    return start


def split_footer(text: str) -> Tuple[str, List[FooterReference]]:
    """把一条消息拆成 ``(正文, 参考文献条目)``。

    页脚（参考文献块 + Provenance 追溯日志）一律位于回复末尾，因此取**各页脚
    标记中最早出现的位置**作为切点。这里不能只认 :data:`FOOTER_RULE_HTML` 一个字面
    量：渲染管线会把 ``<hr>`` 前的 ``<br>`` 吃掉并重写内联样式，渲染结果的页脚
    分隔线已不是原始字符串；标题文字（:data:`CITES_TITLE` /
    :data:`PROVENANCE_TITLE`）才是稳定的锚点。

    页脚中**只回收参考文献行**：Provenance 等运行追溯内容整体丢弃。
    """
    if not text:
        return "", []
    start = _footer_start(text)
    if start == -1:
        return text, []
    rest = text[start:]
    # 参考文献区间止于 Provenance 标题：它同样带 ▪ 行（工具调用统计），
    # 不截断会被误解析成参考文献条目。
    prov_at = rest.find(PROVENANCE_TITLE)
    region = rest[:prov_at] if prov_at != -1 else rest
    return text[:start], parse_footer_entries(region)


def render_footer_references(refs: Sequence[FooterReference], target: str = "plain") -> str:
    """按目标格式渲染参考文献段落；无条目返回空串。

    ``target`` 取 ``"markdown"`` / ``"plain"``：

    * ``markdown`` —— 地址写成 Markdown 自动链接 ``<https://…>``，渲染出来是
      可点击链接（导出 .md、复制 Markdown 用）；
    * ``plain`` —— 直接给出裸地址，纯文本环境里也能复制（导出 .txt 用）。
    """
    items = [r for r in (refs or []) if r.text]
    if not items:
        return ""
    heading = REFERENCE_HEADINGS.get(target, REFERENCE_HEADINGS["plain"])
    lines = [heading, ""]
    for ref in items:
        line = f"[{ref.index}] {ref.text}"
        if ref.link:
            line += f" <{ref.link}>" if target == "markdown" else f" {ref.link}"
        lines.append(line)
    return "\n".join(lines).strip()
