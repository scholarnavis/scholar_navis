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
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

logger = logging.getLogger("Core.References")

# --------------------------------------------------------------------------- #
#  UI 页脚标记（单一事实来源）
# --------------------------------------------------------------------------- #
#: 参考文献块起始的分隔线（历史格式，``follow_ups`` 依赖该字面量做页脚分离）。
FOOTER_RULE_HTML = "<br><hr style='border:0; height:1px; background:#444; margin:15px 0;'>"
#: 参考文献块标题（保留历史字面量，兼容既有导出/复制解析）。
CITES_HEADER_HTML = "<b>📚 Cited Sources:</b><br>"
#: 页脚整体起始标记：正文/追问与参考文献块的分界点。
FOOTER_MARKER = FOOTER_RULE_HTML + CITES_HEADER_HTML

#: 正文行内引用标记 ``[n]``（n 为 1~3 位数字）。
INLINE_CITE_RE = re.compile(r"\[(\d{1,3})\]")

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

    * ``index``    —— 引用编号（= 正文里的 ``[n]``），程序分配；
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
    * ``kind``     —— ``reference`` | ``local_document`` | ``web``。
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
    def display_name(self) -> str:
        """无作者/标题时的兜底名称（本地文件名 / 链接 / 在线来源标题）。"""
        return (self.title or _basename(self.path) or self.url
                or f"Reference {self.index}")

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
    #  引用使用集
    # ------------------------------------------------------------------ #
    @staticmethod
    def used_indices(text: str) -> Set[int]:
        """抽取文本中出现的所有 ``[n]`` 编号。"""
        if not text:
            return set()
        return {int(m) for m in INLINE_CITE_RE.findall(text)}

    # ------------------------------------------------------------------ #
    #  渲染
    # ------------------------------------------------------------------ #
    def to_html(self, indices: Optional[Iterable[int]] = None) -> str:
        """渲染正文末尾的参考文献块 HTML（无可用条目时返回空串）。

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
        """单条参考文献行：编号可点击跳转（在线链接 / 本地查看器）。"""
        number = f"[{item.index}]"
        label = item.citation_text_html()
        link = item.primary_link
        if link:
            return (
                f"<div style='margin-bottom: 6px;'>▪ "
                f"<a style='color:#05B8CC; text-decoration:none;' href='{_escape(link)}'>"
                f"<b>{number}</b> {label}</a></div>"
            )
        return f"<div style='margin-bottom: 6px;'>▪ <b>{number}</b> {label}</div>"
