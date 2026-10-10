"""文本形态工具调用（fallback tool call）的**唯一处理口径**。

背景
----
本项目提示词要求"没有原生 function-calling 的推理模型"把工具调用直接写在正文里
（见 ``chat_tasks`` 的 ``FALLBACK TOOL CALLING`` 段：```` ```json {"name": "tool_name",
"arguments": {...}} ``` ````）。运行时随后会在缓冲内容里识别并真正执行它，但正文是
**逐 token 实时上屏**的（:meth:`src.core.agent.runtime.AgentRuntime._llm_step_stream`），
于是这段 JSON 会永久留在气泡里；工具轮结束后下一轮正文又写进同一个气泡，屏幕上
看到的就是"正文 → 工具 JSON → 后续正文"。历史上唯一的剥离实现
（``TextFormatter._strip_tool_json``）只服务导出 / 复制，显示链路从未调用，因此
"每轮回答末尾都夹着一段 JSON"是必然结果，而不是偶发。

本模块把四件事收敛到一处：

* :func:`is_tool_call_object` —— "什么算文本工具调用"的**展示**口径；
* :func:`strip` —— 对**完整文本**的剥离（导出 / 复制 / 渲染兜底共用）；
* :func:`iter_tool_call_objects` —— **执行**口径的提取（运行时把文本形态调用
  当作工具真正执行）；
* :class:`ToolCallStreamFilter` —— 流式**暂扣**过滤器（emit 侧使用），只影响显示：
  原始 token 仍须完整累积进缓冲，交给
  ``AgentRuntime._extract_tool_calls`` 解析执行。

展示口径（:func:`is_tool_call_object`）要求额外的 ``arguments``/``parameters``/``input``
键，执行口径（:func:`is_executable_tool_call`）只要求非空 ``name``：剥错会删掉用户
内容，执行漏了则工具不会跑，因此展示从严、执行从宽——两者共用同一套扫描器。

定位工具调用的边界一律走"括号配平 + 闭合围栏确认"（:func:`_fence_body_scan` /
:func:`_scan_object`），不再用"找下一个 ```"或裸括号计数：``cite_references`` 的
``snippet`` 是来源**逐字**原文，里面出现 ``{``、``}`` 乃至 ```` ``` ```` 都在预期之内，
朴素切分会截断 JSON 从而漏剥。调用方不得再自建正则或括号计数。
"""

import json
import logging
import re
from html import unescape as _html_unescape
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Core.ToolCallLeak")

#: 判定"文本工具调用"所需的参数键：命中任一即可（与运行时的解析口径一致）。
_TOOL_CALL_ARG_KEYS = ("arguments", "parameters", "input")

#: 触发暂扣的"对象头"写法（比较前会去掉空白；覆盖 JSON 与 HTML 转义两种形态）。
_TOOL_CALL_HEAD_KEYS = ('"name":', "'name':", '&quot;name&quot;:', '\\"name\\":')

#: 触发暂扣的"对象头"最长回看字符数（``{"name":`` 只有 8 个字符）。
_HEAD_PROBE_CHARS = 64

#: Markdown 围栏：语言标记可选，正文允许与围栏同处一行（提示词给的就是单行形态）。
_FENCE_RE = re.compile(
    r"```[ \t]*(?P<lang>[A-Za-z0-9_+\-]*)[ \t]*(?P<body>.*?)```",
    re.DOTALL,
)
#: 围栏起始处的语言标记（用于流式期间判断语言词是否已经写完）。
_FENCE_OPEN_RE = re.compile(r"```[ \t]*(?P<lang>[A-Za-z0-9_+\-]*)")
_FENCE_TOKEN = "```"
_WHITESPACE = " \t\r\n"

#: 围栏正文解析结论。
_TOOL_CALL = "tool_call"
_OTHER = "other"
_INCOMPLETE = "incomplete"


# --------------------------------------------------------------------------- #
# 基础扫描
# --------------------------------------------------------------------------- #
def is_tool_call_object(obj: Any) -> bool:
    """一个已解析的 JSON 值是否是"文本形态工具调用"。

    判定只认结构（带非空 ``name``，且带 ``arguments`` / ``parameters`` / ``input``
    之一），不认工具名——工具名由调用方自行决定是否白名单化。

    Args:
        obj: :func:`json.loads` 的结果。

    Returns:
        是文本工具调用时为 ``True``。
    """
    return (
        isinstance(obj, dict)
        and "name" in obj
        and any(k in obj for k in _TOOL_CALL_ARG_KEYS)
    )


def _loads_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _is_tool_call_fragment(fragment: str) -> bool:
    """片段（原始或 HTML 转义形态）是否解析为文本工具调用对象。"""
    for candidate in (fragment, _html_unescape(fragment)):
        if is_tool_call_object(_loads_json(candidate)):
            return True
    return False


def _scan_object(text: str, start: int) -> int:
    """返回从 ``text[start] == "{"`` 起配平的右括号之后的下标；未闭合返回 ``-1``。

    跟踪字符串与转义状态：``snippet`` 里的 ``{}`` 不会让计数提前收尾。
    """
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _probe_tool_call_head(head: str) -> str:
    """判断 ``{`` 之后的文本是否是"文本工具调用"的开头。

    Args:
        head: ``{`` 之后的文本（调用方自行截断）。

    Returns:
        ``"yes"``（已确认）、``"no"``（已排除）、``"undecided"``（可能是前缀，需更多字符）。
    """
    compact = re.sub(r"\s+", "", head[:_HEAD_PROBE_CHARS])
    if not compact:
        return "undecided"
    for key in _TOOL_CALL_HEAD_KEYS:
        if compact.startswith(key):
            return "yes"
        if key.startswith(compact):
            return "undecided"
    return "no"


def _find_object_head(text: str, start: int = 0) -> Tuple[int, str]:
    """返回 ``start`` 之后第一个"可能是文本工具调用开头"的 ``{``，以及其判定。

    Returns:
        ``(下标, "yes"|"undecided")``；没有候选时返回 ``(-1, "no")``。
    """
    pos = text.find("{", start)
    while pos >= 0:
        head = text[pos + 1: pos + 1 + _HEAD_PROBE_CHARS]
        verdict = _probe_tool_call_head(head)
        if verdict != "no":
            return pos, verdict
        pos = text.find("{", pos + 1)
    return -1, "no"


def _find_fence_close(text: str, start: int) -> int:
    """返回 ``start`` 之后最近的闭合围栏结束下标；没有则 ``-1``。"""
    pos = text.find(_FENCE_TOKEN, start)
    return -1 if pos < 0 else pos + len(_FENCE_TOKEN)


def _fence_body_scan(text: str, body_start: int) -> Tuple[str, int]:
    """解析围栏正文（一个或多个 ``{...}`` 对象 + 闭合围栏）。

    与"找下一个 ```` ``` ````"不同，这里先用括号配平定位 JSON 结束，再要求其后
    出现闭合围栏：``snippet`` 里出现 ```` ``` ```` 字面量时不会把 JSON 截断。

    判定要求 ``name`` 位于对象**首个键位**（与裸对象路径同一口径；模型按提示词
    输出时恒成立）。先做这一层 64 字符的头部探测，可以整块跳过普通数据块，
    避免为每段展示用 JSON 跑一遍完整解析。

    Args:
        text: 含围栏的文本。
        body_start: 围栏正文起点（语言标记之后的下标）。

    Returns:
        ``(_TOOL_CALL, end)`` 确认是文本工具调用（``end`` 为围栏结束下标）；
        ``(_OTHER, end)`` 确认不是（``end`` 为可确定的围栏结束下标）；
        ``(_INCOMPLETE, 0)`` 信息不足，需更多文本。
    """
    pos = body_start
    n = len(text)
    saw_object = False
    while True:
        while pos < n and text[pos] in _WHITESPACE:
            pos += 1
        if pos >= n:
            return _INCOMPLETE, 0
        if text.startswith(_FENCE_TOKEN, pos):
            if saw_object:
                return _TOOL_CALL, pos + len(_FENCE_TOKEN)
            return _OTHER, pos + len(_FENCE_TOKEN)
        if text[pos] != "{":
            # 正文不是 JSON 对象序列（普通代码块）：确认不是工具调用。
            end = _find_fence_close(text, pos)
            return (_OTHER, end) if end > 0 else (_INCOMPLETE, 0)
        head = text[pos + 1: pos + 1 + _HEAD_PROBE_CHARS]
        if _probe_tool_call_head(head) == "no":
            # 首个键位不是 name：展示用数据块，无需再做解析。
            end = _find_fence_close(text, pos)
            return (_OTHER, end) if end > 0 else (_INCOMPLETE, 0)
        end = _scan_object(text, pos)
        if end < 0:
            return _INCOMPLETE, 0
        if not _is_tool_call_fragment(text[pos:end]):
            close = _find_fence_close(text, end)
            return (_OTHER, close) if close > 0 else (_INCOMPLETE, 0)
        saw_object = True
        pos = end


def _merge_intervals(intervals: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """排序并合并重叠/相邻区间。"""
    merged: List[Tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


# --------------------------------------------------------------------------- #
# 完整文本剥离
# --------------------------------------------------------------------------- #
def find_tool_call_spans(text: str) -> List[Tuple[int, int]]:
    """定位文本中所有文本工具调用的字符区间（左闭右开）。

    先处理"语言标记为空或 ``json``"的围栏块（带明确语言的围栏是内容而非协议），
    再扫描围栏之外的裸对象；围栏内部永不重复扫描。
    """
    if not text:
        return []

    spans: List[Tuple[int, int]] = []
    fences: List[Tuple[int, int]] = []
    for match in _FENCE_RE.finditer(text):
        fences.append((match.start(), match.end()))
        # 语言标记是强信号：带明确语言（python/sh/bash…）的围栏是内容，不是协议。
        if (match.group("lang") or "").lower() not in ("", "json"):
            continue
        state, end = _fence_body_scan(text, match.start("body"))
        if state == _TOOL_CALL and end > 0:
            spans.append((match.start(), end))

    blocked = _merge_intervals(fences + spans)
    block_idx = 0
    i = 0
    n = len(text)
    while i < n:
        while block_idx < len(blocked) and blocked[block_idx][1] <= i:
            block_idx += 1
        if block_idx < len(blocked) and blocked[block_idx][0] <= i:
            i = blocked[block_idx][1]
            continue
        if text[i] == "{" and _probe_tool_call_head(text[i + 1: i + 1 + _HEAD_PROBE_CHARS]) != "no":
            end = _scan_object(text, i)
            if end > 0 and _is_tool_call_fragment(text[i:end]):
                spans.append((i, end))
                i = end
                continue
        i += 1

    return _merge_intervals(spans)


def strip(text: str) -> str:
    """剥离正文中的文本工具调用，返回可直接展示 / 导出的文本。

    只删除**结构上确定**是工具调用的 JSON（见 :func:`is_tool_call_object`），
    正文里合法的 JSON 代码块与表格数据保持不变。
    """
    if not text:
        return text or ""
    spans = find_tool_call_spans(text)
    if not spans:
        return text
    out: List[str] = []
    cursor = 0
    for start, end in spans:
        if start < cursor:
            continue
        out.append(text[cursor:start])
        cursor = end
    out.append(text[cursor:])
    # 摘掉整块后常留下成片空行，收敛到最多一个空行。
    stripped = re.sub(r"\n{3,}", "\n\n", "".join(out))
    logger.debug("Stripped %d text tool-call span(s).", len(spans))
    return stripped.strip()


def is_executable_tool_call(obj: Any) -> bool:
    """一个已解析的 JSON 值是否应被**执行**（执行口径，比展示口径宽松）。

    只要求带非空字符串 ``name``。宽松是刻意的：这里放宽只是可能多执行一次模型
    自己写出来的调用；若收紧则会让"模型确实想调用"的文本漏执行——它不会注册、
    引用随之丢失，而那段 JSON 仍会出现在正文里（本模块的起因）。
    """
    if not isinstance(obj, dict):
        return False
    name = obj.get("name")
    return isinstance(name, str) and bool(name)


def _load_executable_tool_call(fragment: str) -> Optional[Dict[str, Any]]:
    """片段（原始或 HTML 转义）解析为应执行的工具调用对象；不是则 ``None``。"""
    for candidate in (fragment, _html_unescape(fragment)):
        data = _loads_json(candidate)
        if is_executable_tool_call(data):
            return data
    return None


def tool_call_fingerprint(obj: Dict[str, Any]) -> str:
    """工具调用的去重指纹：``(name, arguments)`` 归一化后的稳定字符串。"""
    return json.dumps(
        {"name": obj.get("name"), "arguments": obj.get("arguments")},
        ensure_ascii=False, sort_keys=True, default=str,
    )


def iter_tool_call_objects(text: str) -> List[Dict[str, Any]]:
    """按出现顺序提取文本里的工具调用对象（执行侧使用）。

    与 :func:`find_tool_call_spans` 共用括号配平扫描，差别只在范围：执行侧必须
    承认正文**任意位置**的裸对象（含 HTML 转义形态），而剥离侧为避免误删展示
    内容会跳过带明确语言的围栏内部。

    命中的对象内部会被整段跳过（其 ``arguments`` 里全是嵌套数据，没必要再扫）；
    未命中时只前进一个字符，以便找出被包了一层的调用。
    """
    if not text:
        return []
    found: List[Dict[str, Any]] = []
    seen = set()
    i = 0
    n = len(text)
    while i < n:
        start = text.find("{", i)
        if start < 0:
            break
        end = _scan_object(text, start)
        if end < 0:
            i = start + 1
            continue
        obj = _load_executable_tool_call(text[start:end])
        if obj is None:
            i = start + 1
            continue
        fingerprint = tool_call_fingerprint(obj)
        if fingerprint not in seen:
            seen.add(fingerprint)
            found.append(obj)
        i = end
    return found


# --------------------------------------------------------------------------- #
# 流式暂扣过滤器（emit 侧）
# --------------------------------------------------------------------------- #
class ToolCallStreamFilter:
    """把"可能是文本工具调用"的片段暂扣，确认后丢弃，否则原样放行。

    使用方式：把每个正文 delta 交给 :meth:`feed`，返回的文本才上屏；流结束时
    调用 :meth:`flush` 放行剩余内容。**只影响显示**：调用方仍须把原始 delta
    累积进缓冲，交给运行时的工具调用解析器执行。

    设计要点：

    * 只有"看起来像 ``{"name":``"的 ``{`` 与 Markdown 围栏会触发暂扣；正文里
      普通的 ``{``（代码片段、集合字面量）在少数几个字符内就会被排除并立刻放行；
    * 围栏标记逐个字符到达，凑齐 3 个反引号之前一律暂扣，避免半截围栏被当正文
      放出、进而让 JSON 与围栏脱钩（屏幕上会留下 ```` ```json ```` / ```` ``` ```` 残骸）；
    * 边界判定与 :func:`strip` 共用同一套扫描（括号配平 + 闭合围栏确认）；
    * 收尾（:meth:`flush`）时，**已确认**是工具调用却未闭合的片段直接丢弃——生成
      被用户中断、连接断开时最常见的就是"半截 JSON"，放出去比丢掉更糟；尚未
      确认的候选一律原样放行，暂扣只能是延迟显示，不能吞掉正文；
    * 暂扣量受 ``max_hold`` 约束，超限即整体放行（宁可短暂可见，不可丢失正文）。
    """

    #: 暂扣上限：超出后放行，避免异常输出把缓冲区无界撑大。
    MAX_HOLD = 262_144

    def __init__(self, max_hold: int = MAX_HOLD):
        self._max_hold = max_hold
        self._hold = ""
        # 待决片段：None / ("fence", body_start, must_check, confirmed)
        # / ("object", start, confirmed)；confirmed 表示"已确认是工具调用"，收尾
        # 未闭合时据此决定丢弃还是放行（见 _drain_tail）。
        self._pending: Optional[Tuple] = None
        self._reset_pending()

    # ---- 对外接口 -------------------------------------------------------- #
    def feed(self, delta: str) -> str:
        """送入一段正文 delta，返回可以立即上屏的文本（可能为空）。"""
        if not delta:
            return ""
        self._hold += delta
        if len(self._hold) > self._max_hold:
            logger.warning(
                "Tool-call filter held %d chars (limit %d); releasing buffer as-is.",
                len(self._hold), self._max_hold,
            )
            released, self._hold = self._hold, ""
            self._reset_pending()
            return released
        return self._drain(final=False)

    def flush(self) -> str:
        """流结束：放行剩余文本（未闭合的暂扣片段原样放出，绝不丢正文）。"""
        released = self._drain(final=True)
        tail, self._hold = self._hold, ""
        self._reset_pending()
        if tail:
            released += tail
        return released

    # ---- 状态机 ---------------------------------------------------------- #
    def _reset_pending(self) -> None:
        self._pending = None
        self._cursor = 0        # 对象增量扫描游标（hold 内绝对下标）
        self._depth = 0
        self._in_str = False
        self._escaped = False
        self._fence_search = 0

    def _drain(self, final: bool) -> str:
        out: List[str] = []
        while self._hold:
            if self._pending is None:
                action, count = self._locate(final)
                if action == "plain":
                    out.append(self._hold)
                    self._hold = ""
                    break
                if action == "emit":
                    out.append(self._hold[:count])
                    self._hold = self._hold[count:]
                    continue
                if action == "wait":
                    break
                continue
            action, count = self._resolve(final)
            if action == "wait":
                break
            if action == "drop":
                logger.debug("Withheld a text tool call (%d chars).", count)
                self._hold = self._hold[count:]
            else:
                out.append(self._hold[:count])
                self._hold = self._hold[count:]
            self._reset_pending()
        return "".join(out)

    def _locate(self, final: bool) -> Tuple[str, int]:
        """在当前缓冲里找最早的"可疑起点"并登记待决状态。

        Returns:
            ``("plain", 0)`` 无可疑内容；``("emit", n)`` 先放行前 n 个字符；
            ``("pending", 0)`` 已登记待决；``("wait", 0)`` 数据不足，等下一批。
        """
        hold = self._hold
        fence_pos = hold.find(_FENCE_TOKEN)
        object_pos, object_verdict = _find_object_head(hold)
        partial_fence = -1 if final else self._partial_fence_start(hold)
        candidates = [p for p in (fence_pos, object_pos, partial_fence) if p >= 0]
        if not candidates:
            return "plain", 0
        start = min(candidates)
        if start > 0:
            return "emit", start
        if start == partial_fence:
            return "wait", 0
        if start == fence_pos:
            return self._begin_fence()
        return self._begin_object(object_verdict)

    @staticmethod
    def _partial_fence_start(hold: str) -> int:
        """缓冲末尾"尚未凑齐的围栏标记"起点；没有则 ``-1``。

        ```` ``` ```` 是逐字符到达的，只有凑齐 3 个才能判定为围栏。若先把 1~2 个
        反引号当正文放出，随后到达的 JSON 就与围栏脱钩，会被当作裸对象剥离，
        屏幕上留下 ```` ```json ```` 与 ```` ``` ```` 这对残骸。
        """
        run = 0
        i = len(hold) - 1
        while i >= 0 and hold[i] == "`":
            run += 1
            i -= 1
        if 0 < run < len(_FENCE_TOKEN):
            return i + 1
        return -1

    def _begin_fence(self) -> Tuple[str, int]:
        """缓冲以围栏起始：判断该围栏是否可能承载文本工具调用。"""
        hold = self._hold
        match = _FENCE_OPEN_RE.match(hold)
        lang = (match.group("lang") or "").lower()
        lang_end = match.end()
        if lang_end >= len(hold):
            # 语言词可能还没写完（例如刚收到 "```j"），等下一批再判。
            return "wait", 0
        if lang and lang != "json":
            # 语言明确不是 json：整块按普通代码块放行，内部不再扫描。
            must_check, confirmed = False, False
        elif lang == "json":
            # 语言标记即协议声明，收尾未闭合时也按工具调用丢弃。
            must_check, confirmed = True, True
        else:
            # 无语言标记：靠正文头判断（```` ``` {...} ```` 这种内联写法）。
            # 正文还没到或刚好停在 ``{`` 时一律按"可能需要检查"处理（保守等待，
            # 宁可晚放行，也不能把工具调用当普通代码块放出去）。
            body = hold[lang_end: lang_end + _HEAD_PROBE_CHARS].lstrip()
            if not body or body.startswith("{"):
                verdict = _probe_tool_call_head(body[1:])
                must_check = verdict != "no"
                confirmed = verdict == "yes"
            else:
                must_check, confirmed = False, False
        self._pending = ("fence", lang_end, must_check, confirmed)
        return "pending", 0

    def _begin_object(self, verdict: str) -> Tuple[str, int]:
        """缓冲以 ``{`` 起始且可能是工具调用：登记括号配平扫描。"""
        self._pending = ("object", 0, verdict == "yes")
        self._cursor = 1
        self._depth = 1
        self._in_str = False
        self._escaped = False
        return "pending", 0

    @staticmethod
    def _is_confirmed_head(head: str) -> bool:
        """头部文本是否已明确指向 ``name`` 键（工具调用的必要条件）。"""
        return _probe_tool_call_head(head) == "yes"

    def _confirm_pending(self, index: int, head: str) -> None:
        """片段头部一旦变得明确，就把待决状态升级为"已确认"。

        对象/围栏可能先以"可能是前缀"（例如只看到 ``{``）的姿态进来，随后才
        凑出 ``"name":``。必须持续升级，否则中断收尾时会被当成普通正文放出。
        """
        if not self._pending[index] and self._is_confirmed_head(head):
            pending = list(self._pending)
            pending[index] = True
            self._pending = tuple(pending)

    def _resolve(self, final: bool) -> Tuple[str, int]:
        """尝试判定已登记的待决片段。"""
        if self._pending[0] == "fence":
            return self._resolve_fence(final)
        return self._resolve_object(final)

    def _resolve_fence(self, final: bool) -> Tuple[str, int]:
        """围栏待决：等闭合围栏出现后按配平结果判定（普通代码块原样放行）。"""
        _, body_start, must_check, confirmed = self._pending
        if not must_check:
            end = _find_fence_close(self._hold, body_start)
            if end > 0:
                return "emit", end
            return self._drain_tail(final, confirmed)
        state, end = _fence_body_scan(self._hold, body_start)
        if state == _TOOL_CALL:
            return "drop", end
        if state == _OTHER:
            return "emit", end
        # 还没定论：持续把正文头部与已确认状态对齐（见 _confirm_pending）。
        body = self._hold[body_start: body_start + _HEAD_PROBE_CHARS].lstrip()
        if body.startswith("{"):
            self._confirm_pending(3, body[1:])
        return self._drain_tail(final, self._pending[3])

    def _drain_tail(self, final: bool, confirmed: bool) -> Tuple[str, int]:
        """未判定完成：收尾时按"是否已确认是工具调用"处置，否则继续等。

        已确认（``{"name":`` 或 ```` ```json ```` 已经露面）说明这段**不可能**是
        正文：生成被用户中断、连接断开等导致 JSON 半途而废时，丢弃比放出半截
        JSON 正确。只有尚未确认的候选（例如正文里孤立的 ``{``）才原样放行——
        暂扣只能是延迟显示，绝不能吞掉可能需要展示的内容。
        """
        if not final:
            return "wait", 0
        return ("drop", len(self._hold)) if confirmed else ("emit", len(self._hold))

    def _resolve_object(self, final: bool) -> Tuple[str, int]:
        """继续括号配平扫描（增量进行，整体复杂度 O(n)）。"""
        hold = self._hold
        i = self._cursor
        n = len(hold)
        depth, in_str, escaped = self._depth, self._in_str, self._escaped
        end = -1
        while i < n:
            ch = hold[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    i += 1
                    break
            i += 1
        self._depth, self._in_str, self._escaped, self._cursor = depth, in_str, escaped, i
        if end < 0:
            # 还没配平：同时把头部判定升级（见 _confirm_pending）。
            self._confirm_pending(2, hold[1: 1 + _HEAD_PROBE_CHARS])
            return self._drain_tail(final, self._pending[2])
        if _is_tool_call_fragment(hold[:end]):
            return "drop", end
        return "emit", end
