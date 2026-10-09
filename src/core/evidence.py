"""逐字证据库（Verbatim Evidence Store）
=====================================

会话级收集"来自工具结果 / 知识库检索的逐字原文"（论文摘要、KB chunk、网页
摘要、附件文档片段等），为 ``cite_references`` 提交的支撑原文（snippet）提供
**逐字校验与回填**，保证 UI 上展示的 "Cited passage" 必然是来源原文，而非
模型的复述或拼接改写。

数据流（单向，无反馈）::

    工具结果 / KB chunk / 附件 --add_source()--> EvidenceStore
    模型提交的 snippet -------verify_snippet()--> 逐字命中 | 回填 | 丢弃

设计原则
--------
* 高内聚：文本归一化与"逐字"判定只在本模块定义（同一件事只在一处定义）。
* 低耦合：纯内存、纯被动接收，不依赖 runtime / registry / UI。
* 性能：归一化在写入时做一次；校验为 O(来源数) 的子串匹配，会话级来源数
  通常为几十条，代价可忽略。``RLock`` 保护，兼容并行工具执行线程。
* 内存：单条原文截断到 ``_MAX_SOURCE_CHARS``，写入前按归一化文本去重。

逐字判定标准（唯一权威定义）
---------------------------
对 snippet 与候选原文分别做 NFKC 归一化 -> 统一引号/破折号/省略号 -> 小写
-> 移除全部空白，得到比对串；满足以下任一即判为逐字：

1. snippet 的比对串是某条原文比对串的**连续子串**（覆盖 PDF 换行/连字符
   断词等排版差异；同时提供"移除连字符"变体兜底）；
2. snippet 以省略号（``...``）切分后的**每个片段**都在同一条原文中逐字命中
   （允许诚实的省略式引用；跨来源拼接、改写一律不通过）。
"""

from __future__ import annotations

import logging
import threading
import unicodedata
from typing import Dict, List, Optional

logger = logging.getLogger("Core.Evidence")

#: 入库原文的最小长度（字符）：搜索引擎摘要以下的碎片没有溯源价值。
_MIN_SOURCE_CHARS = 60
#: 入库原文的最大长度（字符）：防御性上限，正常摘要/chunk 远小于此值。
_MAX_SOURCE_CHARS = 20000
#: snippet 参与逐字判定的最小归一化长度：过短的"引文"无法与巧合子串区分。
_MIN_SNIPPET_CHARS = 20
#: 省略式引用中，参与判定的最小片段归一化长度。
_MIN_FRAGMENT_CHARS = 15

#: 归一化时的字符统一表：弯引号/破折号/省略号映射为 ASCII 等价物，
#: 消除来源排版与模型转写之间的标点差异。
_PUNCT_TRANS = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "`": "'", "ʼ": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "―": "-",
    "…": "...",
})


def normalize_for_match(text: str, strip_hyphens: bool = False) -> str:
    """把任意文本归一化为逐字比对串（本模块与外部校验共用的唯一定义）。

    NFKC（连字、全角等折叠）-> 标点统一 -> 小写 -> 移除全部空白。
    ``strip_hyphens`` 用于兜底 PDF 行尾连字符断词（``hy-\\nphenation``）。
    """
    text = unicodedata.normalize("NFKC", str(text or "")).translate(_PUNCT_TRANS).lower()
    text = "".join(ch for ch in text if not ch.isspace())
    if strip_hyphens:
        text = text.replace("-", "")
    return text


def _title_match(a: str, b: str) -> bool:
    """标题归一化等价判断：完全相等或互为包含（容忍副标题截断）。"""
    if not a or not b:
        return False
    return a == b or a in b or b in a


class EvidenceStore:
    """线程安全的会话级逐字证据库。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        #: 每条：{"text": 原文, "norm"/"norm_nohy": 比对串, "doi"/"url"/"title": 归一化标识}
        self._sources: List[Dict[str, str]] = []

    # ------------------------------------------------------------------ #
    #  写入
    # ------------------------------------------------------------------ #
    def add_source(self, text: str, doi: str = "", url: str = "", title: str = "") -> bool:
        """登记一条来源原文；重复文本只补全标识，返回是否新入库。

        ``text`` 必须是**未经模型转写**的工具结果 / 检索 chunk 原文。
        """
        text = str(text or "").strip()
        if len(text) < _MIN_SOURCE_CHARS or len(text) > _MAX_SOURCE_CHARS:
            return False
        norm = normalize_for_match(text)
        if not norm:
            return False
        entry = {
            "text": text,
            "norm": norm,
            "norm_nohy": norm.replace("-", ""),
            "doi": str(doi or "").strip().lower(),
            "url": str(url or "").strip().lower().rstrip("/"),
            "title": normalize_for_match(title),
        }
        with self._lock:
            for old in self._sources:
                if old["norm"] == norm:
                    # 同一文本重复入库（多轮检索命中同一论文）：仅补全空标识。
                    for field in ("doi", "url", "title"):
                        if entry[field] and not old[field]:
                            old[field] = entry[field]
                    return False
            self._sources.append(entry)
        logger.debug("Evidence source added (%d chars, title=%s).", len(text), str(title)[:60])
        return True

    # ------------------------------------------------------------------ #
    #  查询
    # ------------------------------------------------------------------ #
    def source_for(self, doi: str = "", url: str = "", title: str = "") -> str:
        """按 DOI / URL / 标题定位已持有的原文（新入库优先）；找不到返回空串。"""
        url_n = str(url or "").strip().lower().rstrip("/")
        doi_n = str(doi or "").strip().lower()
        title_n = normalize_for_match(title)
        with self._lock:
            sources = list(reversed(self._sources))
        for sources_group in (
            [s for s in sources if url_n and s["url"] == url_n],
            [s for s in sources if doi_n and s["doi"] == doi_n],
            [s for s in sources if _title_match(s["title"], title_n)],
        ):
            if sources_group:
                return sources_group[0]["text"]
        return ""

    def verify_snippet(self, snippet: str, doi: str = "", url: str = "",
                       title: str = "") -> Optional[str]:
        """校验 snippet 是否逐字出自某条已持有原文；命中返回该原文，否则 None。

        优先在与 DOI / URL / 标题匹配的"同源"条目内判定，未命中再全局扫描
        （摘要可能以不同标题形态入库）。整段不连续时按省略号切分做片段校验。
        """
        snip = normalize_for_match(snippet)
        if len(snip) < _MIN_SNIPPET_CHARS:
            return None
        snip_nohy = snip.replace("-", "")
        url_n = str(url or "").strip().lower().rstrip("/")
        doi_n = str(doi or "").strip().lower()
        title_n = normalize_for_match(title)

        def _same_source(s: Dict[str, str]) -> bool:
            if url_n and s["url"] and s["url"] == url_n:
                return True
            if doi_n and s["doi"] and s["doi"] == doi_n:
                return True
            return bool(title_n and _title_match(s["title"], title_n))

        def _contained(s: Dict[str, str]) -> bool:
            return snip in s["norm"] or snip_nohy in s["norm_nohy"]

        def _fragments_contained(s: Dict[str, str]) -> bool:
            frags = [f for f in snip.split("...")
                     if len(f) >= _MIN_FRAGMENT_CHARS]
            if not frags:
                return False
            return all(f in s["norm"] or f.replace("-", "") in s["norm_nohy"]
                       for f in frags)

        with self._lock:
            sources = list(reversed(self._sources))  # 新入库优先
        for prefer_same in (True, False):
            for s in sources:
                if prefer_same and not _same_source(s):
                    continue
                if _contained(s) or _fragments_contained(s):
                    return s["text"]
        return None

    def __len__(self) -> int:
        with self._lock:
            return len(self._sources)


__all__ = ["EvidenceStore", "normalize_for_match"]
