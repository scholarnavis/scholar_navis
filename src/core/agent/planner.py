"""
Intent Planner
==============

Decides *how* the Agent should present its Skills to the main LLM.

Modern AGENT principle:
    The LLM itself decides which tools to call via native function calling.
    The Planner's job is NOT to "guess and filter" tools with brittle keyword
    matching (which can drop the exact tool a semantic query needs). Instead it
    provides *assistance* without ever stripping the model's choice:

    1. Always expose the full enabled tool set to the LLM (full agency).
    2. When the tool set is large, run one lightweight semantic-focus call to
       produce a *suggestion* ("these tools are most relevant"), injected as a
       prompt reminder. This reduces accidental mis-selection and token waste
       WITHOUT removing any tool.
    3. The final tool call decision is always made natively by the main LLM.

The output is an ``AgentPlan`` describing:
    - the query intent category (diagnostic / UI only),
    - the FULL tool name list to expose,
    - an optional ordered "focus" hint the model should strongly consider,
    - a short rationale for debugging / UI.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("Agent.Planner")

# Threshold above which the Planner runs the optional semantic-focus call.
# Below this, exposing everything is cheap enough that focus adds no value.
_SEMANTIC_FOCUS_THRESHOLD = 6

_FOCUS_PROMPT = (
    "You are a research copilot. Given a user's academic query and the list of available Skills, "
    "identify the user's core intent and rank the Skills by likely usefulness.\n"
    "Rules:\n"
    "- This is guidance ONLY; the main model still has every tool.\n"
    "- Choose tools that match the semantic meaning of the query, not just keywords.\n"
    "- The user may need MULTIPLE tools for multi-dimensional queries; include all that fit.\n"
    "- If no tool clearly fits, return an empty tools list (the model may answer from knowledge).\n\n"
    "Reply with a single JSON object and nothing else:\n"
    "{\"intent\": \"<short intent label>\", \"tools\": [\"skill_name\", ...]}\n"
)


@dataclass
class AgentPlan:
    query: str
    intent: str = "General"
    tool_names: List[str] = field(default_factory=list)
    focus_hint: List[str] = field(default_factory=list)
    rationale: str = ""

    @property
    def has_tools(self) -> bool:
        return bool(self.tool_names)

    @property
    def has_focus(self) -> bool:
        return bool(self.focus_hint)


class IntentPlanner:
    """Modern Skill planner: full tool agency + optional semantic focus hint."""

    def __init__(self, registry):
        self._registry = registry

    @property
    def registry(self):
        """Read-only access to the underlying SkillRegistry (used by AgentRuntime)."""
        return self._registry

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #
    def plan(self, query: str, pool: Optional[str] = None) -> AgentPlan:
        """
        Build the AgentPlan. ALWAYS exposes the full enabled tool set so the
        LLM keeps full choice. A lightweight heuristic labels the intent purely
        for diagnostics (never used to filter tools).
        """
        query = (query or "").strip()
        plan = AgentPlan(query=query)
        if not query:
            return plan

        enabled = self._registry.by_pool(pool) if pool else self._registry.enabled
        plan.tool_names = [m.name for m in enabled]
        plan.intent = self._detect_intent(query)
        plan.rationale = (
            f"Intent='{plan.intent}'; exposing all {len(enabled)} enabled tools "
            f"for native LLM function calling."
        )
        logger.info(plan.rationale)
        return plan

    # ------------------------------------------------------------------ #
    #  低信息量 / 无明确领域意图的查询不再支付一次全往返的语义聚焦 LLM 调用。
    #  聚焦仅是“提示”，从未裁剪工具；即使跳过，主 LLM 仍持有全部工具自行决策，
    #  因此跳过对正确性零影响，却能省掉一次串行延迟（简单问题往往省 ~10-16s）。
    # ------------------------------------------------------------------ #
    _CHIT_CHAT_HINTS = {
        "hi", "hello", "hey", "hola", "yo", "hiya", "thanks", "thank", "ok", "okay",
        "好的", "谢谢", "你好", "哈喽", "嗨", "测试", "试试", "test", "hi there",
        "what can you do", "who are you", "help", "你是谁", "你能做什么",
    }

    @classmethod
    def _looks_low_information(cls, query: str, intent: str) -> bool:
        """判断查询是否低信息量到不值得做语义聚焦往返。

        仅当同时满足下面两点才返回 True（保守，避免误伤真实研究问题）：
        1) 意图未被识别为任何明确研究领域（intent == General）；
        2) 文本很短，或主要是数字/单字母，或是寒暄/闲聊。
        """
        q = (query or "").strip().lower()
        if not q:
            return True
        if intent != "General":
            return False
        # 纯数字 / 无意义的极短输入：如 "123"、"5"、"ab"
        if re.fullmatch(r"[0-9a-z\s\-_]+", q) and len(q) <= 8:
            return True
        # 寒暄/闲聊
        if q in cls._CHIT_CHAT_HINTS:
            return True
        if len(q) < 8:
            return True
        return False

    def semantic_focus(self, query: str, main_llm, pool: Optional[str] = None) -> AgentPlan:
        """
        Produce an AgentPlan with an optional semantic focus hint.

        Uses ONE lightweight LLM call to suggest which of the exposed tools are
        most relevant. The suggestion is a *hint* only: tool_names still contain
        the full set, and focus_hint carries the ordered recommendations. Runs
        only when the tool set is large enough to justify it; otherwise focus is
        empty (the main LLM decides entirely on its own).
        """
        base = self.plan(query, pool=pool)
        if not base.tool_names or main_llm is None:
            return base

        if len(base.tool_names) <= _SEMANTIC_FOCUS_THRESHOLD:
            base.rationale += " (tool set small; no semantic focus needed.)"
            logger.info(base.rationale)
            return base

        # 低信息量 / 无领域意图：跳过语义聚焦往返，主回答可立即开始。
        if self._looks_low_information(query, base.intent):
            base.rationale += (
                " (low-information query; skipped semantic-focus round-trip to avoid latency.)"
            )
            logger.info(base.rationale)
            return base

        metas = [self._registry.get(n) for n in base.tool_names]
        metas = [m for m in metas if m is not None]
        menu = "\n".join(m.to_short_repr() for m in metas)
        try:
            resp = main_llm.chat(
                [
                    {"role": "system", "content": _FOCUS_PROMPT},
                    {"role": "user", "content": f"User Query: {query}\n\nAvailable Skills:\n{menu}"},
                ],
                is_translation=True,  # plain-text reply, no tools
            )
            raw = resp.get("content", "") if isinstance(resp, dict) else str(resp)
            data = self._safe_parse_json(raw)
            if isinstance(data, dict):
                picked = data.get("tools") or []
                if not isinstance(picked, list):
                    picked = [picked]
                # Keep only names we actually expose.
                valid = [p for p in picked if p in set(base.tool_names)]
                base.focus_hint = valid
                base.intent = str(data.get("intent", base.intent))
            base.rationale = (
                f"Intent='{base.intent}'; exposing {len(base.tool_names)} tools, "
                f"focus hint on {len(base.focus_hint)}."
            )
            logger.info(base.rationale)
        except Exception as e:
            logger.warning(f"Semantic focus failed, continuing with full tool set: {e}")
        return base

    def build_focus_reminder(self, plan: AgentPlan) -> str:
        """Turn a plan's focus hint into a short system-prompt reminder (or '')."""
        if not plan.has_focus:
            return ""
        names = ", ".join(plan.focus_hint)
        return (
            "### TOOL GUIDANCE (suggestion, not a restriction):\n"
            f"Based on the query intent ('{plan.intent}'), these tools are likely most relevant "
            f"and SHOULD be strongly considered: {names}.\n"
            "You still have access to ALL other enabled tools if they are genuinely needed. "
            "If the user asks for multi-dimensional data, use multiple tools to fulfill every part.\n"
        )

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _detect_intent(query: str) -> str:
        q = query.lower()
        intents = {
            "Literature Review": ["literature review", "references", "citation", "papers", "journal", "mini-review"],
            "Sequence Analysis": ["sequence", "fasta", "nucleotide", "protein sequence", "cds"],
            "Structure Analysis": ["structure", "pdb", "alphafold", "3d"],
            "Functional Enrichment": ["enrichment", "pathway", "go term", "kegg"],
            "Network Interaction": ["interaction", "ppi", "network", "binding"],
            "Gene Expression": ["expression", "rna-seq", "transcriptom", "sra", "geo"],
            "Taxonomy": ["taxonomy", "species", "taxid", "occurrence", "gbif"],
            "Chemical Compound": ["compound", "metabolite", "pubchem", "smiles", "chebi"],
            "Web Search": ["search the web", "latest", "news", "current events", "what is the current"],
            "Image Generation": ["generate an image", "draw", "create a picture", "make an image"],
        }
        for name, kws in intents.items():
            if any(k in q for k in kws):
                return name
        return "General"

    @staticmethod
    def _safe_parse_json(text: str) -> Optional[dict]:
        """Extract the first JSON object from a model reply (tolerant of fences/prose)."""
        text = (text or "").strip()
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
        m = re.search(r"```json\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(1))
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                pass
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                pass
        return None
