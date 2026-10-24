"""证据门禁：找出"没有落到具体来源"的高风险陈述段落，并给出可见标记。

判定规则只在这里定义一次（:func:`unsupported_targets`），两个出口共用同一份结果：

- :func:`unsupported_paragraphs` —— 把"该被标的段落"原样交给成文后的补来源轮
  （:meth:`src.core.agent.runtime.AgentRuntime.run` 的 ``evidence_unsupported``）。
  模型据此去补引用，或调用 ``fetch_paper_fulltext`` / ``request_fulltext`` 取回正文；
- :func:`apply_evidence_gate` —— 把同一批段落渲染成正文里的"（未核实）"标记。

两边共用一条规则，才不会出现"标了却不去补来源、补了来源却还标着"的自相矛盾。
"""
import logging
import re
from typing import Callable, Dict, List, Tuple

logger = logging.getLogger(__name__)

#: 文内引用编号：``[3]``、``[3, 5]``、``[3-5]``。判定只看"有没有编号"，不看指向谁
#: ——编号的有效性由参考文献注册表负责（正文出现、注册表没有的号会被抹掉）。
CITE_RE = re.compile(r"\[\d+(?:\s*[,–\-]\s*\d+)*\]")

#: 高风险信号：年份、基因/蛋白样式的编号名、带单位的数值，以及"研究表明"这类断言
#: 口吻。出现这些却没有任何编号的段落，用户无法自行核对，必须点出来。
HIGH_RISK_RE = re.compile(
    r"(?:\b(?:19|20)\d{2}\b"
    r"|\b[A-Z]{2,}[0-9]{1,4}\b"
    r"|\b\d+(?:\.\d+)?\s*(?:%|mg|g|mM|μM|nM|nm|kDa|bp|kb)\b"
    r"|研究表明|研究发现|结果显示|已被证实|显著|已知"
    r"|studies?\s+(?:show|found|reported|demonstrated)"
    r"|was\s+(?:shown|found|reported|demonstrated))",
    re.IGNORECASE,
)

#: 证据行：模型在这些行里给出某条结论的出处（"依据：…[3]"、"Source: …"）。
#: 它本身不是待判定的论断，而是**前面那组陈述**的来源说明。
TAIL_RE = re.compile(
    r"^[\s\-*•]*(?:依据|证据|来源|出处|对应文献|参考文献"
    r"|evidence|sources?|refs?|references?)\s*[:：]",
    re.IGNORECASE,
)

#: 无来源时追加的状态说明（整篇一次，不逐段标）。
NO_SOURCES_FOOTER = ("\n\n> 证据状态 / Evidence: 本轮没有检索到可用文献 / "
                     "no usable literature evidence was retrieved this turn; "
                     "以上内容未核实 / the response above is unverified.")

MARKER_ZH = "（未核实）"
MARKER_EN = " [unverified]"


def split_blocks(body: str) -> List[Tuple[str, str]]:
    """按空行把正文切成 ``[(kind, text), …]``，``kind`` 为 ``"block"``/``"sep"``。

    判定粒度必须是**段落**而不是行：学术回答常写成"陈述一行、依据一行"，
    按行判会把每条陈述都判成"无来源"——那正是满屏"（未核实）"的来源。
    """
    parts = re.split(r"(\n[ \t]*\n)", str(body or ""))
    return [("sep" if index % 2 else "block", part) for index, part in enumerate(parts)]


def _already_marked(chunk: str) -> bool:
    return "未核实" in chunk or MARKER_EN.strip() in chunk


def unsupported_targets(segments: List[Tuple[str, str]]) -> List[int]:
    """返回"该被标"的段落下标：高风险、整组无来源时取**组内最后一段**。

    判定以"陈述组"为单位：**连续的陈述段落算一组**（标题、表格、引用块、分隔线、
    "依据：…"行都会断开分组），组内任意一处出现编号即视为整组已落来源——编号在
    组内哪个位置不影响结论，"引用一段原文、紧接着据此推论"不会被误标。整组都没有
    编号时，只在**组内最后一段**标一次：按行逐句标会把"陈述一行、依据一行"的写法
    整篇标红（实测一条 14 段的回答被标了 14 处），提醒反而失效。

    代码块与思考/工具过程块不参与判定，且状态**跨块**维持（块之间只隔一个空行，
    标签只出现在块首/块尾）：只看当前块会把思考正文当成普通段落，标记就写进了
    Reasoning 面板。
    """
    in_code = False
    in_hidden = False
    pending: List[int] = []          # 当前陈述组的段落下标（连续陈述段落）
    pending_cited = False            # 组内任意一处出现编号 -> 整组已落来源
    groups: List[Tuple[List[int], bool]] = []

    for index, (kind, chunk) in enumerate(segments):
        if kind == "sep":
            continue
        stripped = chunk.strip()
        if not stripped:
            continue
        if "```" in stripped:
            if stripped.count("```") % 2:
                in_code = not in_code
            continue
        if in_code:
            continue
        opens = stripped.count("<think") + stripped.count("<mcp_process>")
        closes = stripped.count("</think") + stripped.count("</mcp_process>")
        if in_hidden:
            if closes >= opens:
                in_hidden = False
            continue
        if opens:
            if opens > closes:
                in_hidden = True
            continue

        lines = [line for line in stripped.splitlines() if line.strip()]
        first = lines[0].strip()
        cited = bool(CITE_RE.search(stripped))
        if TAIL_RE.match(first):
            # 依据行：给前面那组陈述背书，随后另起一组。
            groups.append((pending, pending_cited or cited))
            pending, pending_cited = [], False
            continue
        if first.startswith(("#", "|", ">", "<", "!")) or first.startswith("---"):
            groups.append((pending, pending_cited))
            pending, pending_cited = [], False
            continue
        # 陈述段落归入当前组。**带编号的段落不断组**：它给整组落来源，紧随其后的
        # 承接段落同属这一组；否则"引用一段原文、紧接着据此推论"会被错标未核实。
        pending.append(index)
        pending_cited = pending_cited or cited
    if pending:
        groups.append((pending, pending_cited))

    targets: List[int] = []
    for block_ids, supported in groups:
        if supported or not block_ids:
            continue
        group_text = " ".join(segments[i][1].strip() for i in block_ids)
        if not HIGH_RISK_RE.search(group_text):
            continue
        targets.append(block_ids[-1])
    return targets


def unsupported_paragraphs(text: str) -> List[str]:
    """``text`` 里"会被标（未核实）"的段落文本（已带标记的不算）。

    成文后补来源那一轮用它把段落原样递回模型：这些段落有高风险信号却没有编号，
    模型应当去补引用或取全文，而不是等任务层贴标记。
    """
    segments = split_blocks(text)
    return [segments[index][1].strip() for index in unsupported_targets(segments)
            if not _already_marked(segments[index][1])]


def gate_finder(registry: Dict) -> Callable[[str], List[str]]:
    """成文后补来源那一轮的判定入口工厂。

    ``registry`` 是"本轮已经拿到手的来源登记表"：主路径传 ``sources_map``，深度研究
    里每个子任务传它自己的 ``local_sources``。**一个来源都没有时不空转**——无从补起，
    那种情况由 :func:`apply_evidence_gate` 在文末给一次证据状态说明即可。

    判定规则不在这里重写：真正的"该打回去补来源哪几段"由
    :func:`unsupported_paragraphs` 决定，这里只回答"有没有来源可补"。
    """
    def _unsupported(text: str) -> List[str]:
        if not registry:
            return []
        return unsupported_paragraphs(text)

    return _unsupported


def apply_evidence_gate(text: str, has_sources: bool,
                        research_context: bool = True) -> str:
    """给没有来源的高风险段落加“未核实”，不删原文。

    ``research_context`` 为 False 时直接放行：代码/普通问答不应该因为出现
    年份或数字就被扣上“未核实”。

    - 有证据库来源：只标出 :func:`unsupported_targets` 判定为"高风险且整组无编号"
      的陈述组（组内最后一段）；
    - 没有来源：整段回答末尾加一条明确的证据状态说明。

    这是**展示层门禁**，不替换模型内容；标记的语义是"已经尝试过补来源仍无法核实"
    ——成文后补来源的那一轮见 :mod:`src.core.agent.runtime`，本函数只管把剩下的
    落到用户眼前。
    """
    body = str(text or "")
    if not body.strip() or not research_context:
        return body

    flagged = 0
    if has_sources:
        segments = split_blocks(body)
        for index in unsupported_targets(segments):
            kind, chunk = segments[index]
            if _already_marked(chunk):
                continue
            marker = MARKER_ZH if any("\u4e00" <= ch <= "\u9fff" for ch in chunk) else MARKER_EN
            segments[index] = (kind, chunk.rstrip() + marker)
            flagged += 1
        body = "".join(chunk for _kind, chunk in segments)
        # 段内已就地标了"（未核实）"，不再在文末复述同一件事：追加的页脚会让每个回答
        # 都以一句元信息收尾，而"哪一段没来源"本来就已经写在段尾。次数仍写日志。
    else:
        if len(body.strip()) >= 80 and HIGH_RISK_RE.search(body):
            body += NO_SOURCES_FOOTER
    if flagged:
        logger.info("Evidence gate flagged %d uncited paragraph group(s).", flagged)
    return body