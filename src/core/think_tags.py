"""思维链标签（思考块 / 工具过程块）的**唯一事实来源**。

背景
----
不同 provider 的"内联思考"包裹写法各异，但语义一致：都应折叠进 Reasoning
面板，绝不能作为正文上屏。本项目历史上在四个位置各写了一份识别规则——
UI 渲染（``text_formatter``）、引用扫描（``references``）、导出与子任务合成
（``chat_tasks``）、HTTP API（``api_server``）——口径宽窄不一，于是出现
"同一段思考在某条链路上被正确折叠、在另一条链路上原样漏进正文"。

此模块把这些规则收敛到一处，所有需要"区分思考与正文"的调用方都必须经过
:func:`normalize` / :func:`strip` / :func:`split` 或 :class:`ThinkStreamSplitter`，
不得再自建正则。

覆盖的写法
----------
1. XML 变体：``<think>`` ``<thinking>`` ``<reasoning>`` ``<reasoning_content>``
   （含 ``<?think>`` 这类前缀变体与小写/大写混排）
2. 管道变体：``<|thinking|>`` ``<|reasoning|>``（GPT-OSS、部分本地推理网关）
3. 符号包裹：``◁think▷``（Kimi K1.5 系列）
4. Harmony 频道：``<|channel|>analysis<|message|>``（思考信道起点），
   结束为 ``<|channel|>final<|message|>``

这些字面量在正常学术正文中几乎不会出现；即便误判，代价也只是内容被移入
可折叠面板而非丢失，因此可安全用于兜底。
"""

import logging
import re
from typing import List, Tuple

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 标签词表
# ----------------------------------------------------------------------
#: 规范化后使用的统一标签（下游一律按这两个字面量做配对提取）。
THINK_OPEN_TAG = "<think>"
THINK_CLOSE_TAG = "</think>"
MCP_OPEN_TAG = "<mcp_process>"
MCP_CLOSE_TAG = "</mcp_process>"

#: 思考块起始写法（任意 provider 变体）。
THINK_OPEN_RE = re.compile(
    r"(?:"
    r"<\s*\??\s*(?:think|thinking|reasoning|reasoning_content)\s*>"
    r"|<\|\s*(?:think|thinking|reasoning)\s*\|>"
    r"|<\|\s*channel\s*\|>\s*analysis\s*<\|message\|>"
    r"|◁\s*think\s*▷"
    r")",
    re.IGNORECASE,
)

#: 思考块结束写法。注意 ``<\s*[/?]+\s*...`` 同时覆盖 ``</think>`` 与 ``<?think>``，
#: 因此做流式判定时必须**让起始写法优先匹配**（见 :data:`THINK_TAG_SCAN_RE`）。
THINK_CLOSE_RE = re.compile(
    r"(?:"
    r"<\s*[/?]+\s*(?:think|thinking|reasoning|reasoning_content)\s*>"
    r"|<\|\s*/\s*(?:think|thinking|reasoning)\s*\|>"
    r"|<\|\s*channel\s*\|>\s*final\s*<\|message\|>"
    r"|◁\s*/\s*think\s*▷"
    r")",
    re.IGNORECASE,
)

#: 推理/Harmony 协议的孤立标记：自身不承载正文，正文抽取后一并清除。
THINK_NOISE_RE = re.compile(
    r"<\|\s*(?:end|start|message|constrain)\s*\|>"
    r"|<\|\s*channel\s*\|>\s*(?:analysis|final)?"
    r"|<\s*[/?]*\s*(?:think|thinking|reasoning|reasoning_content)\s*>"
    r"|◁\s*/?\s*think\s*▷",
    re.IGNORECASE,
)

#: 单遍扫描用：起始写法在前，保证 ``<?think>`` 不被结束写法抢走。
THINK_TAG_SCAN_RE = re.compile(
    r"(?P<open>" + THINK_OPEN_RE.pattern + r")"
    r"|(?P<close>" + THINK_CLOSE_RE.pattern + r")",
    re.IGNORECASE,
)

#: 思考块内容（含"未闭合到底"的兜底），用于整段剥离。
THINK_BLOCK_RE = re.compile(
    r"<(think|mcp_process)>.*?(?:</\1>|$)", re.DOTALL | re.IGNORECASE)

#: 缓冲区末尾可能是一个尚未收完的标签片段（流式切分时需暂扣，避免标签被截断）。
_PARTIAL_TAG_RE = re.compile(r"<[^<>]{0,64}$")


# ----------------------------------------------------------------------
# 整段文本上的操作
# ----------------------------------------------------------------------
def normalize(text: str) -> str:
    """把各家写法的思考/工具过程标签折叠成统一的 ``<think>`` / ``</think>``。

    折叠（而不是删除）是刻意的：删除会让 ``<think>`` 失去配对结束标记，
    随后的正文会被当成"未闭合思考链"整段吞进 Reasoning 面板，正文区变空——
    比原来的泄漏更严重。
    """
    if not text or "<" not in text and "◁" not in text:
        return text
    text = re.sub(r"<\s*think\s*>", THINK_OPEN_TAG, text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*/\s*think\s*>", THINK_CLOSE_TAG, text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*mcp_process\s*>", MCP_OPEN_TAG, text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*/\s*mcp_process\s*>", MCP_CLOSE_TAG, text, flags=re.IGNORECASE)
    text = THINK_OPEN_RE.sub(THINK_OPEN_TAG, text)
    return THINK_CLOSE_RE.sub(THINK_CLOSE_TAG, text)


def strip(text: str, *, keep_mcp: bool = False) -> str:
    """剥离思考块（含未闭合的），返回只含正文的文本。

    ``keep_mcp=True`` 时保留 ``<mcp_process>`` 块（只剥思考链），供需要单独
    呈现工具过程的调用方使用。
    """
    if not text:
        return text
    normalized = normalize(text)
    if keep_mcp:
        pattern = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL | re.IGNORECASE)
    else:
        pattern = THINK_BLOCK_RE
    cleaned = pattern.sub("", normalized)
    # 落单的结束标记/协议噪声不承载正文，一并清掉，避免残留在导出文本里。
    return THINK_NOISE_RE.sub("", cleaned)


def split(text: str) -> Tuple[str, str]:
    """整段切分，返回 ``(reasoning_text, body_text)``。

    与 :meth:`ThinkStreamSplitter` 共用同一套配对规则；区别只在于本函数拿到的是
    完整文本，因此可以处理"标签内容跨 token"的情况。
    """
    if not text:
        return "", ""
    reasoning: List[str] = []
    in_think = False
    body_parts: List[str] = []
    pos = 0
    for m in THINK_TAG_SCAN_RE.finditer(text):
        segment = text[pos:m.start()]
        if segment:
            (reasoning if in_think else body_parts).append(segment)
        in_think = m.group("open") is not None
        pos = m.end()
    tail = text[pos:]
    if tail:
        (reasoning if in_think else body_parts).append(tail)
    return "".join(reasoning).strip(), "".join(body_parts).strip()


class ThinkStreamSplitter:
    """把逐 token 的模型输出切成 ``(reasoning, content)`` 两条流的流式状态机。

    相比"只在字面量 ``<think>`` 上打标记"的旧实现，这里：

    * 识别全部变体写法（见模块文档），不会因写法差异把思考当正文放出去；
    * 允许"正文之后再出现思考"（R1 系模型常见），每遇到起始写法就重新进入
      思考态，而不是一次闭合后永久失效；
    * 暂扣未收完的标签片段（``"<thi"`` + ``"nk>"`` 分两个 token 到达），
      避免标签被截断后既不被识别、又原样漏进正文。

    调用方只需调用 :meth:`feed`；流结束时调用 :meth:`flush` 取回暂扣内容。
    """

    #: 暂扣片段的最大长度：超过说明它不是标签而是一段以 ``<`` 开头的正文
    #: （如数学表达式 ``a < b``），必须立刻放出，否则会一直卡在缓冲里。
    MAX_HOLDBACK = 64

    def __init__(self) -> None:
        self._buffer = ""
        self._in_think = False
        #: 当前思考块是否由 provider 的专用 reasoning 字段打开（而非内联标签）。
        #: 只有这种块会在正文通道到来时自动收尾。
        self._native = False
        self._closed_blocks = 0

    @property
    def in_think(self) -> bool:
        """当前是否处于思考态（供调用方在异常/取消时补闭合标记）。"""
        return self._in_think

    @property
    def closed_blocks(self) -> int:
        """已完整闭合的思考块数量（诊断用）。"""
        return self._closed_blocks

    def feed_chunk(self, reasoning: str = "", content: str = "") -> List[Tuple[str, str]]:
        """按到达顺序处理**同一个 chunk** 的两路文本，返回有序片段列表。

        一个 chunk 里 reasoning 与 content 可能同时存在：reasoning 恒在前。
        上游若只用单一通道（HTTP API 的 token 流），直接调用 :meth:`feed` 即可；
        有独立 reasoning 字段的调用方（litellm 的 ``delta.reasoning_content``）
        应走本方法，才能保住"思考块在此处收尾"的边界语义。

        Returns:
            ``[(reasoning, content), ...]``，调用方按序拼装；两段均为空串的
            条目不会出现。
        """
        segments: List[Tuple[str, str]] = []
        if reasoning:
            # 暂扣缓冲里可能还有上一个 token 的正文尾巴，必须先放出来保序。
            pending_r, pending_c = self._drain_buffer()
            if pending_r or pending_c:
                segments.append((pending_r, pending_c))
            # 原生推理字段整段都是思考，不做标签扫描（思考原文里的
            # "</think>" 字面量不代表思考结束）。
            self._in_think = True
            self._native = True
            segments.append((reasoning, ""))
        if content:
            if self._native:
                # 专用推理字段让位给正文：此处即思考与正文的真实边界。
                self._native = False
                self._in_think = False
                self._closed_blocks += 1
            work = self._buffer + content
            self._buffer = ""
            hold = ""
            m = _PARTIAL_TAG_RE.search(work)
            if m and len(work) - m.start() <= self.MAX_HOLDBACK:
                hold = work[m.start():]
                work = work[:m.start()]
            self._buffer = hold
            r_part, c_part = self._scan(work)
            if r_part or c_part:
                segments.append((r_part, c_part))
        return segments

    def feed(self, text: str) -> Tuple[str, str]:
        """单通道便捷入口：喂入一个 token，返回 ``(reasoning, content)``。

        等价于 :meth:`feed_chunk` 的 ``content`` 通道（文本里的内联标签会被
        识别），返回的仍是"先思考、后正文"的稳定语义。
        """
        if not text:
            return "", ""
        segments = self.feed_chunk(content=text)
        if not segments:
            return "", ""
        # 单通道下最多产出一个片段；若上游真给了一个 chunk，取其中的两路文本。
        reasoning = "".join(r for r, _ in segments)
        content = "".join(c for _, c in segments)
        return reasoning, content

    def flush(self) -> Tuple[str, str]:
        """流结束：放出暂扣片段，避免内容丢失。"""
        return self._drain_buffer()

    def _drain_buffer(self) -> Tuple[str, str]:
        if not self._buffer:
            return "", ""
        pending, self._buffer = self._buffer, ""
        logger.debug("ThinkStreamSplitter drain pending=%r in_think=%s", pending, self._in_think)
        return self._scan(pending)

    def _scan(self, work: str) -> Tuple[str, str]:
        if not work:
            return "", ""
        reasoning: List[str] = []
        content: List[str] = []
        pos = 0
        for m in THINK_TAG_SCAN_RE.finditer(work):
            segment = work[pos:m.start()]
            if segment:
                (reasoning if self._in_think else content).append(segment)
            was_thinking = self._in_think
            self._in_think = m.group("open") is not None
            if was_thinking and not self._in_think:
                self._closed_blocks += 1
            pos = m.end()
        tail = work[pos:]
        if tail:
            (reasoning if self._in_think else content).append(tail)
        return "".join(reasoning), "".join(content)
