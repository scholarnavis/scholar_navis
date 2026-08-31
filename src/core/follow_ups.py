"""Follow-up（追问建议）段落的稳健提取与解析。

背景：
    聊天回复末尾的 suggestions 段落来源不稳定：
    - Agent 模式按系统提示词输出 [FOLLOW_UPS] 强标记；
    - 其他模式下模型可能自发输出各种自然语言标题（写法变体极多，
      且常被 markdown/HTML 包裹）；
    - 极端情况下不写标题，直接在末尾输出一列问题。

设计（高内聚、单一入口）：
    split_follow_ups() 在文本进入渲染前把"正文 / 追问块 / 引用块"分离。
    流式渲染与最终渲染共用同一逻辑，保证追问块永远不会混入正文，
    同时避免"流式期间先显示、完成后又消失"的闪烁。

三级识别策略（从严到宽，任何候选都必须通过统一校验后才生效）：
    1) 强标记 [FOLLOW_UPS]（提示词强制、独占一行）；
    2) 自然语言标题行（宽松变体；其后块必须"几乎纯列表"且 >= 2 项）；
    3) 兜底启发式：文本末尾连续的、全部以问号结尾的列表项块。
"""
from __future__ import annotations

import logging
import re
from typing import List, NamedTuple, Optional

logger = logging.getLogger(__name__)

# 单个追问的长度边界（过短多为空内容噪声，过长多为正文段落）
_MIN_QUESTION_LEN = 2
_MAX_QUESTION_LEN = 400
# 候选块中允许的最大"非列表噪声"字符量（防止把正文段落误吞为追问）
_MAX_BLOCK_NOISE = 250
# 追问数量上限（提示词要求 6 条，留冗余）
_MAX_QUESTIONS = 8

# ---- UI 页脚块（Cited Sources / Provenance）剥离 ----
#    这些块由 chat_tasks 的 Phase 6/7 以 <br><hr ...> 起始追加到回复末尾，
#    与模型生成的正文/追问建议不同源，须整体剥离并原样拼回。
_UI_FOOTER_START = "<br><hr style='border:0; height:1px; background:#444; margin:15px 0;'>"

# 兼容旧式 Only-Cited 匹配（保留引用链接文本）
_CITES_RE = re.compile(
    r"<br><hr style='border:0; height:1px; background:#444; margin:15px 0;'>"
    r"<b>.*?Cited Sources:</b><br>"
)


def _split_ui_footer(text: str) -> tuple[str, str]:
    """把末尾由 <br><hr ...> 起始的 UI 页脚（Cited Sources / Provenance）与正文分离。

    返回 (body, footer_html)。UI 块永远位于回复末尾（Phase 6/7 追加），
    故取第一个 <br><hr ...> 出现位置作为 footer 起点即可。
    """
    idx = text.find(_UI_FOOTER_START)
    if idx == -1:
        return text, ""
    return text[:idx], text[idx:]

# ---- 一级：强标记（提示词要求独占一行的字面 token，兼容大小写/分隔符变体）----
_TOKEN_RE = re.compile(r"\[\s*FOLLOW[_\-\s]*UPS?\s*\]", re.IGNORECASE)

# ---- 二级：自然语言标题核心词（覆盖常见写法变体）----
_CORE = (
    r"(?:suggested|recommended|proposed|possible|potential|further)?\s*"
    r"follow[\s\-_]*ups?"
    r"(?:\s*(?:questions?|suggestions?|ideas?|topics?|queries?))?"
    r"|follow[\s\-_]*up\s*(?:questions?|suggestions?|ideas?|topics?|"
    r"queries?|recommendations?|areas?|directions?)"
    r"|(?:suggested|recommended|discussion|further|more|next|possible|"
    r"clarifying|research)?\s*(?:questions?|suggestions?|recommendations?|inquiries?)"
    r"|(?:next|further|future|possible|potential)\s+steps?"
)
# 标题行整行匹配：允许被 markdown/HTML 包裹、带 emoji、带前导短语。
# 误报由"其后块几乎纯列表 + 至少 2 项"的统一校验兜住。
_HEADER_LINE_RE = re.compile(
    r"^(?:[#>*_\-\s]|<b>|</b>|<strong>|</strong>)*"
    r"(?:💡|✨|❓|🔹|❔)?\s*"
    r"(?:\*\*)?"
    r"(?:[A-Za-z][^\n:]{0,48}?)??"
    r"(?:" + _CORE + r")"
    r"\s*:?\s*(?:\*\*)?"
    r"(?:[#>*_\-\s]|</b>|</strong>)*$",
    re.IGNORECASE,
)

# ---- 列表行：- / * / • / 1. / 1) / a) 等，允许引用前缀 ----
_ITEM_LINE_RE = re.compile(
    r"^\s*(?:>+\s*)?(?:[-*•‣–]|\d{1,2}[.)、]|[A-Za-z][.)])\s+(.+?)\s*$"
)

# ---- 无 bullet 的粗体 tag 行：**Tag**: question（仅当以问号结尾时视为追问行）----
_BOLD_ITEM_RE = re.compile(r"^\*\*([^*]{1,30})\*\*\s*[:：]?\s*(.+)$")

# ---- tag 规范化：与 FollowUpGroupWidget 的配色映射对齐 ----
_CANONICAL_TAGS = {
    "deep dive": "Deep Dive", "deepdive": "Deep Dive", "deep": "Deep Dive",
    "critical": "Critical", "limitation": "Critical", "weakness": "Critical",
    "broader": "Broader", "implication": "Broader", "broad": "Broader",
    "brainstorm": "Brainstorm", "creative": "Brainstorm", "what if": "Brainstorm",
    "similar": "Similar", "parallel": "Similar", "related": "Similar",
    "application": "Application", "applied": "Application", "practical": "Application",
    "general": "General", "explore": "General", "explore more": "General",
    "methodology": "Critical", "next steps": "Application",
}


class FollowUpSplit(NamedTuple):
    """split_follow_ups 的结果：正文、追问列表、引用块 HTML。"""

    main_text: str
    questions: List[dict]
    cites_html: str


def _strip_inline(s: str) -> str:
    """去掉行内装饰：emoji、加粗、引号、首尾空白。"""
    s = s.strip()
    s = re.sub(r"^(?:💡|✨|❓|🔹|❔)\s*", "", s)
    s = s.strip("*").strip()
    s = s.strip("\"'“”‘’").strip()
    return s


def _strip_tokens(s: str) -> str:
    """清理正文中的 [FOLLOW_UPS] 标记：整行标记连同换行删除，行内标记就地删除。"""
    s = re.sub(
        r"^[^\S\n]*\[\s*FOLLOW[_\-\s]*UPS?\s*\][^\S\n]*\r?\n?",
        "", s, flags=re.IGNORECASE | re.MULTILINE,
    )
    return _TOKEN_RE.sub("", s)


def _parse_question(raw: str) -> Optional[dict]:
    """把一行原始文本解析为 {"tag": ..., "text": ...}；无效则返回 None。

    注意：tag 形式的匹配必须在剥除加粗符号之前进行，否则 "**Tag**: q"
    会被误判为普通文本。
    """
    text = raw.strip()
    text = re.sub(r"^(?:💡|✨|❓|🔹|❔)\s*", "", text)
    text = text.strip("\"'“”‘’").strip()
    if len(text) < _MIN_QUESTION_LEN or len(text) > _MAX_QUESTION_LEN:
        return None

    # 形式1: [Tag] question
    m = re.match(r"^\[([^\[\]]{1,30})\]\s*(.+)$", text)
    if m:
        tag = _norm_tag(m.group(1))
        if tag:
            return {"tag": tag, "text": m.group(2).strip()}

    # 形式2: **Tag** question / **Tag**: question
    m = re.match(r"^\*\*([^*]{1,30})\*\*\s*:?\s*(.+)$", text)
    if m:
        tag = _norm_tag(m.group(1))
        if tag:
            return {"tag": tag, "text": m.group(2).strip()}

    # 形式3: Tag: question（Tag 需首字母大写、不超过 25 字符，避免吞掉普通句子）
    m = re.match(r"^([A-Z][A-Za-z /&-]{1,24}?)\s*[:：]\s+(.+)$", text)
    if m:
        tag = _norm_tag(m.group(1))
        if tag:
            return {"tag": tag, "text": m.group(2).strip()}

    # 兜底：剥掉加粗等装饰后作为普通问题
    text = _strip_inline(text)
    if len(text) < _MIN_QUESTION_LEN or len(text) > _MAX_QUESTION_LEN:
        return None
    return {"tag": "General", "text": text}


def _norm_tag(tag: str) -> str:
    """把模型输出的 tag 规范化为 UI 配色映射认识的写法。"""
    key = re.sub(r"[^a-z ]", " ", tag.lower())
    key = re.sub(r"\s+", " ", key).strip()
    if key in _CANONICAL_TAGS:
        return _CANONICAL_TAGS[key]
    # 未知 tag：足够短则原样保留（UI 有 fallback 配色），过长视为正文并入文本
    if len(tag) <= 20:
        return tag.strip()
    return ""


def _extract_questions(block: str):
    """从候选块中解析列表项。

    返回 (questions, noise_chars)：noise_chars 是块内"既非合法追问行、
    又非标题行"的字符量，用于判断该候选是否为误报。

    过滤口径：
        - 追问项要求最终文本以 "?" / "." / 中文全角问号结尾；
          例如 "search_preprints (1 call(s))" 末尾是 ")"，不应误吞。
        - bullet 行不以问号结尾一律视为噪声（防止误识别列表/工具调用等）。
    """
    questions: List[dict] = []
    noise = 0
    for line in block.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        m = _ITEM_LINE_RE.match(line)
        if m:
            raw = m.group(1)
            # 仅当 bullet 行末尾像问句才视为追问；否则按噪声计入
            if not _looks_like_question(raw):
                noise += len(stripped)
                continue
            q = _parse_question(raw)
            if q:
                questions.append(q)
            else:
                noise += len(stripped)
        elif _BOLD_ITEM_RE.match(stripped) and _looks_like_question(stripped):
            q = _parse_question(stripped)
            if q:
                questions.append(q)
            else:
                noise += len(stripped)
        elif _HEADER_LINE_RE.match(stripped):
            continue  # 标题行不算噪声
        else:
            noise += len(stripped)
    return questions, noise


def _looks_like_question(text: str) -> bool:
    """粗判"这是一条追问"：剥除装饰后以 ?、.、! 或中英文全角问号结尾。

    末尾判断同时要求文本长度足够（避免噪声冒名）。
    """
    t = _strip_inline(text or "")
    if len(t) < _MIN_QUESTION_LEN:
        return False
    return t.rstrip().endswith(("?", "!", "？", "！", "."))


def _dedupe(questions: List[dict]) -> List[dict]:
    """按文本去重并截断到上限。"""
    seen = set()
    unique = []
    for q in questions:
        key = q["text"].lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(q)
    return unique[:_MAX_QUESTIONS]


def split_follow_ups(text: str) -> FollowUpSplit:
    """把模型输出拆分为 (正文, 追问列表, 引用块 HTML)。

    处理顺序：
        0) 剥离末尾 UI 页脚（Cited Sources / Provenance，以 <br><hr ...> 起始），
           避免其吞掉/干扰位于其前的 follow-ups 识别，并原样保留 footer。
        1~3) 在"正文 + 追问建议"上做三级 follow-ups 识别。
        4) 残余的引用块剥离归入 footer。

    任何一级识别失败都会安全回退：追问块保持原文渲染（与旧行为一致），
    仅额外清理残留的 [FOLLOW_UPS] 标记 token。
    """
    if not text or not text.strip():
        return FollowUpSplit(text or "", [], "")

    # 0) 先剥离末尾的 UI 页脚块（Cited Sources / Provenance）
    body, footer_html = _split_ui_footer(text)

    # 在剩余"正文 + 追问"上识别 follow-ups
    norm_full = body.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    lines_full = norm_full.split("\n")
    offsets_full: List[int] = []
    _pos = 0
    for _line in lines_full:
        offsets_full.append(_pos)
        _pos += len(_line) + 1

    split_at_full: Optional[int] = None
    questions: List[dict] = []
    strategy = "none"

    # ---- 1) 强标记分支 ----
    token_matches = list(_TOKEN_RE.finditer(norm_full))
    for m in reversed(token_matches):
        line_start = norm_full.rfind("\n", 0, m.start()) + 1
        qs, noise = _extract_questions(norm_full[m.end():])
        if len(qs) >= 2 and noise <= _MAX_BLOCK_NOISE:
            split_at_full, questions, strategy = line_start, qs, "token"
            break

    # ---- 2) 标题分支 ----
    if split_at_full is None:
        for i in range(len(lines_full) - 1, -1, -1):
            if not lines_full[i].strip():
                continue
            if not _HEADER_LINE_RE.match(lines_full[i].strip()):
                continue
            qs, noise = _extract_questions("\n".join(lines_full[i + 1:]))
            if len(qs) >= 2 and noise <= _MAX_BLOCK_NOISE:
                split_at_full, questions, strategy = offsets_full[i], qs, "header"
                break

    # ---- 3) 兜底启发式 ----
    if split_at_full is None:
        start_line = None
        collected: List[str] = []
        for i in range(len(lines_full) - 1, -1, -1):
            stripped = lines_full[i].strip()
            if not stripped:
                if collected:
                    break
                continue
            m = _ITEM_LINE_RE.match(lines_full[i])
            if not m and _BOLD_ITEM_RE.match(stripped):
                raw_item = stripped
                content = _strip_inline(_BOLD_ITEM_RE.match(stripped).group(2))
            elif m:
                raw_item = m.group(1)
                content = _strip_inline(raw_item)
            else:
                break
            if not content.endswith("?"):
                break
            collected.append(raw_item)
            start_line = i
        if len(collected) >= 2 and start_line and start_line > 0:
            collected.reverse()
            questions = [q for q in (_parse_question(c) for c in collected) if q]
            if len(questions) >= 2:
                split_at_full, strategy = offsets_full[start_line], "tail-heuristic"

    # ---- 校验失败：整体回退，正文保留（仅清理残留标记 token）----
    if split_at_full is None:
        fallback = _strip_tokens(norm_full).strip()
        if fallback != norm_full.strip():
            logger.debug("Follow-up split: no block found; stripped stray tokens.")
        return FollowUpSplit(fallback, [], footer_html)

    # 切分点之前的正文若仍残留标记 token（如标记误写在中间），一并清理
    main_text = _strip_tokens(norm_full[:split_at_full]).rstrip()
    if not main_text.strip():
        # 整篇都是列表（如用户直接索要问题清单）：不当追问，保持原文
        logger.debug("Follow-up split: candidate would empty the body; ignored.")
        return FollowUpSplit(_strip_tokens(norm_full).strip(), [], footer_html)

    questions = _dedupe(questions)
    logger.info(
        "Follow-up split via '%s': %d questions, main=%d chars, footer=%d chars.",
        strategy, len(questions), len(main_text), len(footer_html),
    )
    return FollowUpSplit(main_text, questions, footer_html)
