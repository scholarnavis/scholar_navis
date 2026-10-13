"""参考文献注册表（唯一事实来源）
================================

背景与设计目标
--------------
本模块把"参考文献"从**模型正文**中彻底解耦：

* 过去：模型在答案末尾自己写一段 ``References``，程序再用正则去解析/清洗，
  格式一旦漂移（漏写、编号错位、被 markdown 改写）整块就丢失或错位。
* 现在：模型只通过常驻工具 ``cite_references`` 提交**结构化条目**
  （标题 / 作者 / 年份 / 期刊 / DOI / 链接 / 支撑原文），编号由程序分配并回传，
  模型只在正文里写 ``[n]``；参考文献列表由 **程序渲染**。

唯一事实来源（高内聚）
----------------------
``ReferenceItem`` / ``ReferenceRegistry`` 同时服务三条链路，避免多处各写一套：

1. 编号分配（KB 文档、在线 MCP 来源、模型登记的文献共用同一编号空间）；
2. 正文末尾参考文献块的 HTML 渲染（气泡展示 / 导出 Markdown / 复制）；
3. UI 悬停卡与详情面板所需的结构化数据（``to_dict_list``）。

页脚标记常量集中在此处定义，``follow_ups`` / ``chat_tasks`` / 气泡导出逻辑
统一引用，保证"同一件事只在一处定义"。
"""

from __future__ import annotations

import html as _html
import logging
import os
import re
import threading
from dataclasses import dataclass, asdict, replace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

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

#: 正文行内引用标记 ``[n]``（n 为 1~3 位数字；本地 KB 文档等已编号来源使用）。
INLINE_CITE_RE = re.compile(r"\[(\d{1,3})\]")

#: "未核实"标记文案（**唯一事实来源**）：参考文献列表行尾与导出重排版共用同一
#: 文案，避免同一条目在不同出口出现两种说法。判定标准见
#: :attr:`ReferenceItem.needs_manual_review`。
UNVERIFIED_LABEL = "未核实"
UNVERIFIED_MARK_HTML = (
    f" <span style='color:#C0392B; font-size:12px;'>（{UNVERIFIED_LABEL}）</span>")

#: 正文行内引用 key（``[lariguet2004]`` 形态）：字母开头、长度受限的 ASCII slug。
#: 模型为每个来源自拟 key 并在正文中复用；编号由程序按"首次出现顺序"统一分配，
#: 从根本上消除"模型猜数字"导致的编号错位（模型写正文与调用工具孰先孰后都不影响）。
_KEY_TOKEN_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9._:-]{0,63})\]")

#: 统一的正文引用 token 扫描：``[key]`` 或 ``[n]``（用于按序编号与就地改写）。
INLINE_TOKEN_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9._:-]{0,63}|\d{1,3})\]")

#: key 合法性校验（与 _KEY_TOKEN_RE 同形，整串匹配）。
_VALID_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,63}$")

# --------------------------------------------------------------------------- #
#  引用 key 兜底清理口径（唯一事实来源）
# --------------------------------------------------------------------------- #
#: 判定"key 形态的 token 是引用别名而非别的方括号用法"的必要条件：内含四位年份。
#: 年份下限取 1500（而不只是 19xx/20xx）：生物学经典文献的别名（``[mendel1866]``）
#: 同样是模型自拟 key，收窄到 19xx/20xx 会漏掉它们。
_CITE_KEY_YEAR_RE = re.compile(r"(?:1[5-9]\d{2}|20\d{2})")


def strip_unregistered_cite_keys(text: str) -> Tuple[str, List[str]]:
    """移除落不到任何著录的引用 key 坏标记，返回 ``(清理后文本, 被移除的 token)``。

    必须在 :meth:`ReferenceRegistry.resolve_citations` **之后**调用：那时"能认出来的
    key"（含被 :meth:`ReferenceRegistry._recognize_key` 近似识别回填的）已就地改写为
    ``[n]``，文本里剩下的 key 形态 token 只可能是"认不出、也没有近似可落回"的别名——
    本轮与历史都没登记过，或只出现在思考块里因而拿不到编号——留在屏幕上只会呈现为
    ``[lariguet2004]`` / ``[hu2019cotton]`` 这样的坏标记。

    清理范围是**全文**（含 ``<think>`` 思考块与工具过程块）：思考面板同样上屏，别名
    在那里同样是读者不该看到的噪声；只按"正文里未登记的 token 清单"删除会漏掉这一半。

    只命中 :data:`_KEY_TOKEN_RE` 的 key 形态（``[lariguet2004]``）；数字型 ``[1]``、
    无年份的短缩写 ``[EC]``、``[Fig. 2]``、``[the 1990s]`` 这类正文自身的方括号用法
    一律原样保留——刻意不做"任意含年份短语"的宽匹配：模型被约定的引用形态就是 slug，
    宽匹配换不来多少覆盖，却会把正常文字误删。

    为什么剩下的不编号：编号必须指向一条真实著录，而这类 token 后面**没有任何来源**
    （注册表、KB、在线来源、历史快照里都查不到）。凭空给它造一条条目就是捏造文献。
    """
    if not text:
        return text or "", []

    removed: List[str] = []

    def _drop(match: "re.Match[str]") -> str:
        token = match.group(1)
        if not _CITE_KEY_YEAR_RE.search(token):
            return match.group(0)          # 无年份：不是引用 key，原样保留
        removed.append(token)
        return ""

    return _KEY_TOKEN_RE.sub(_drop, text), removed


# --------------------------------------------------------------------------- #
#  key 近似识别（把模型写歪的 key 落回真实著录）
# --------------------------------------------------------------------------- #
def _fold_key(value: str) -> str:
    """key 折叠：小写化并剔除分隔符，使 ``Lariguet-2004`` / ``lariguet_2004`` 等价。"""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


#: 模型自拟 key 的约定形态：姓氏 + 四位年份 + 可选主题词（``zhang2026condensate``）。
#: 识别近似 key 时按该形态把年份与姓氏拆开：**年份必须一致**，姓氏才允许容错——
#: 年份是模型从著录里抄来的硬信息，姓氏才是最容易被写歪的部分。
_KEY_YEAR_SPLIT_RE = re.compile(
    r"^(?P<name>[a-z][a-z0-9]*?)(?P<year>1[5-9]\d{2}|20\d{2})(?P<suffix>[a-z0-9]*)$")


def _edit_distance_within(a: str, b: str, limit: int) -> int:
    """有界编辑距离：超过 ``limit`` 立即返回 ``limit + 1``（只服务近似 key 判定）。"""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def _name_in_text(name: str, text: str) -> bool:
    """姓名是否以**独立词**出现在（小写）文本里：``hu`` 不应命中 ``Huang``。"""
    if not name or not text:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", text) is not None


#: 思维链 / 工具过程块的识别口径统一在 src/core/think_tags.py（见 strip()）：
#: 统计"首次出现顺序"时排除，避免模型在思考里的示例编号抢占正文的首次出现位置。

#: 条目文本的展示上限（超出仅用于展示截断，原始数据不丢）。
_SNIPPET_DISPLAY_MAX = 2000


def _escape(value: Any) -> str:
    """转义为可安全内联进 Qt 富文本的文本（保留引号，HTML 实体转义）。"""
    return _html.escape("" if value is None else str(value), quote=True)


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

    * ``index``    —— 引用编号（= 正文里的 ``[n]``），程序按首次出现顺序分配；
    * ``key``      —— 正文里的引用 key（``[key]``，模型自拟的 ASCII slug），可空；
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
    key: str = ""
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
        """就地清洗字段：去首尾空白、DOI 去前缀、kind 归一、key 合法性校验。"""
        self.key = self._normalize_key(self.key)
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

    @staticmethod
    def _normalize_key(value: str) -> str:
        """规整正文引用 key：去方括号/空白，非法形态（含空格、中文、超长）则丢弃。"""
        raw = str(value or "").strip().strip("[]").strip()
        if not raw or not _VALID_KEY_RE.match(raw):
            return ""
        return raw

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
        """可视化著录（标题斜体、期刊跟随），仅转义后拼接，无外部输入注入风险。"""
        parts: List[str] = []
        if self.authors_display:
            parts.append(_escape(_ensure_period(self.authors_display)))
        if self.year:
            parts.append(f"({_escape(self.year)}).")
        if self.title:
            parts.append(f"<i>{_escape(_ensure_period(self.title))}</i>")
        if self.journal:
            parts.append(_escape(_ensure_period(self.journal)))
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
        """把模型工具参数（字段名可能有别名）规整为 :class:`ReferenceItem`。"""
        data = raw or {}
        item = cls(
            index=index,
            key=str(data.get("key") or data.get("ref_key") or data.get("cite_key")
                    or data.get("slug") or ""),
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

    线程安全：Agent 循环会并发执行工具调用，多个在线来源可能同时登记，因此
    所有读写都用可重入锁串行化。
    """

    def __init__(self) -> None:
        self._items: Dict[int, ReferenceItem] = {}
        self._by_key: Dict[str, int] = {}
        #: 正文引用 key（小写）-> 注册表编号。仅登记模型显式提供 key 的条目。
        self._key_index: Dict[str, int] = {}
        self._lock = threading.RLock()

    def _index_key(self, item: ReferenceItem, idx: int) -> None:
        """登记条目的正文引用 key（重复 key 保留首次登记，避免抢占既有编号）。"""
        if item.key:
            self._key_index.setdefault(item.key.lower(), idx)

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
            self._index_key(item, idx)
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
            self._index_key(item, idx)
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
    #  引用使用集
    # ------------------------------------------------------------------ #
    @staticmethod
    def used_indices(text: str) -> Set[int]:
        """抽取文本中出现的所有 ``[n]`` 编号。"""
        if not text:
            return set()
        return {int(m) for m in INLINE_CITE_RE.findall(text)}

    def lookup_key(self, key: str) -> Optional[int]:
        """按正文引用 key 查编号（大小写不敏感）；未登记返回 None。"""
        with self._lock:
            return self._key_index.get(str(key or "").strip().lower())

    #: 近似 key 修复的最小姓氏长度：短姓氏（li/lu/hu）差一个字符就可能指向另一个人，
    #: 宁可判定"无法识别"（记日志、由清理环节处理），也不做猜测性关联。
    _MIN_SURNAME_FOR_FUZZY = 6

    def _recognize_key(self, token: str) -> Tuple[Optional[int], str]:
        """把正文 token 识别为注册表编号，返回 ``(编号, 识别方式)``；认不出返回 ``(None, "")``。

        识别**由严到宽**逐级尝试，任一级出现多个候选即放弃该级（宁可判定不认识，也不
        猜测——把一句论断错挂到别人的论文上，比标记缺失严重得多）：

        1. ``exact``：key 完全一致（大小写不敏感）；
        2. ``folded``：折叠分隔符后一致（``Lariguet-2004`` = ``lariguet2004``）；
        3. ``prefix``：年份后的主题词被截断或补全（``zhang2026`` ↔ ``zhang2026condensate``）；
        4. ``typo``：**年份相同**、姓氏编辑距离 ≤1、首字母一致、姓氏长度
           ≥ :attr:`_MIN_SURNAME_FOR_FUZZY`（``frutani2007`` -> ``furutani2007``）；
        5. ``metadata``：**年份相同**且姓氏以独立词出现在条目的作者/标题里（模型把在线
           来源的姓氏直接当 key 用、却没登记该 key 时，仍能落回真实著录）。

        3/4/5 级是有据可依的回填：命中的是注册表里**真实存在**的条目，不是新造的引文；
        每次命中都会随 :meth:`resolve_citations` 的返回值上报，由调用方记入日志供人工核查。
        """
        token = str(token or "").strip()
        if not token:
            return None, ""
        idx = self._key_index.get(token.lower())
        if idx is not None:
            return idx, "exact"
        # 注意：不能因为"注册表里一条 key 都没有"就提前返回——KB / 在线来源只有编号、
        # 没有 key，第 5 级（作者/标题元数据）正是为它们准备的落回路径。
        folded = _fold_key(token)
        hits = sorted({i for k, i in self._key_index.items() if _fold_key(k) == folded})
        if hits:
            return (hits[0], "folded") if len(hits) == 1 else (None, "")

        split = _KEY_YEAR_SPLIT_RE.match(folded)
        if not split:
            return None, ""
        name, year = split.group("name"), split.group("year")

        # 3) 年份后主题词差异（截断 / 补全）
        hits = sorted({i for k, i in self._key_index.items()
                       if _fold_key(k).startswith(folded) or folded.startswith(_fold_key(k))})
        if hits:
            return (hits[0], "prefix") if len(hits) == 1 else (None, "")

        # 4) 姓氏拼写近似（年份必须一致）
        if len(name) >= self._MIN_SURNAME_FOR_FUZZY:
            hits = set()
            for k, i in self._key_index.items():
                other = _KEY_YEAR_SPLIT_RE.match(_fold_key(k))
                if not other or other.group("year") != year:
                    continue
                other_name = other.group("name")
                if not other_name or other_name[0] != name[0]:
                    continue
                if _edit_distance_within(name, other_name, 1) <= 1:
                    hits.add(i)
            if hits:
                return (hits.pop(), "typo") if len(hits) == 1 else (None, "")

        # 5) 作者 / 标题元数据（同年份、姓氏独立词命中）
        if len(name) >= 4:
            hits = set()
            for i, item in self._items.items():
                if str(item.year or "").strip() != year:
                    continue
                haystack = f"{item.authors} {item.title}".lower()
                if _name_in_text(name, haystack):
                    hits.add(i)
            if hits:
                return (hits.pop(), "metadata") if len(hits) == 1 else (None, "")
        return None, ""

    def _resolve_token(self, token: str) -> Tuple[Optional[int], str]:
        """把正文 token 解析为注册表编号：纯数字按编号，其余按 key（含近似识别）。"""
        token = str(token or "").strip()
        if not token:
            return None, ""
        if token.isdigit():
            idx = int(token)
            return (idx, "exact") if idx in self._items else (None, "")
        return self._recognize_key(token)

    # ------------------------------------------------------------------ #
    #  正文引用 -> 编号（唯一权威映射）
    # ------------------------------------------------------------------ #
    def resolve_citations(self, text: str):
        """扫描正文引用（``[key]`` / ``[n]``），按"首次出现顺序"重新编号。

        这是"正文编号 ↔ 参考文献列表"的唯一权威映射：

        * 模型只需为每个来源自拟一个 key 并在正文复用，**不必猜数字**——因此
          无论它先写正文还是先调用 ``cite_references``，编号都不会错位；
        * 编号 1..K 严格按正文中该来源的首次出现顺序分配，列表顺序与正文一致
          （符合学术惯例，也消除了"提交顺序 ≠ 引用顺序"的历史问题）。

        :return: ``(new_text, ordered_items, unresolved, repaired)``
            - ``new_text``：引用已就地改写为 ``[1..K]`` 后的正文；
            - ``ordered_items``：按新编号排序的条目副本（``index`` 已改为新编号）；
            - ``unresolved``：既非已登记 key、也没有近似可落回条目的 token（记日志用）；
            - ``repaired``：``(token, 识别方式, 分配到的编号)``——写歪但已落回**真实**
              著录的 key（见 :meth:`_recognize_key`），供调用方记日志、供人工核查。
        """
        if not text:
            return text or "", [], [], []
        with self._lock:
            # 统计首次出现顺序时排除思考块/工具过程块：模型可能在其中写
            # "用 [x] 引用"这类示意，不应参与正文编号。
            scan_text = strip_think_blocks(text)
            order: Dict[int, int] = {}
            unresolved: List[str] = []
            #: token -> (编号, 识别方式)：同一 token 反复出现（含思考块里的再次出现）
            #: 只识别一次，避免近似匹配在长正文上被反复重算。
            cache: Dict[str, Tuple[Optional[int], str]] = {}
            repaired: List[Tuple[str, str, int]] = []

            def _lookup(token: str) -> Tuple[Optional[int], str]:
                if token not in cache:
                    cache[token] = self._resolve_token(token)
                return cache[token]

            def _scan(m: "re.Match[str]") -> str:
                token = m.group(1)
                idx, tier = _lookup(token)
                if idx is None:
                    unresolved.append(token)
                elif idx not in order:
                    order[idx] = len(order) + 1
                if idx is not None and tier != "exact":
                    repaired.append((token, tier, idx))
                return m.group(0)

            INLINE_TOKEN_RE.sub(_scan, scan_text)

            def _rewrite(m: "re.Match[str]") -> str:
                idx, _tier = _lookup(m.group(1))
                if idx is None or idx not in order:
                    # 认不出的 token / 仅出现在 <think> 中的示意 token：原样保留，
                    # 交后续清理环节（见 strip_unregistered_cite_keys）。
                    return m.group(0)
                return f"[{order[idx]}]"

            new_text = INLINE_TOKEN_RE.sub(_rewrite, text)
            ordered = [replace(self._items[i], index=order[i])
                       for i in sorted(order, key=lambda k: order[k])]
            reported: List[Tuple[str, str, int]] = []
            seen = set()
            for token, tier, idx in repaired:
                mark = (token.lower(), tier)
                if mark in seen or idx not in order:
                    continue
                seen.add(mark)
                reported.append((token, tier, order[idx]))
        return new_text, ordered, unresolved, reported

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

        保留此接口以兼容历史调用；新流程请用 :meth:`resolve_citations` +
        :meth:`render_items`（编号按正文首次出现顺序，而非注册顺序）。

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
