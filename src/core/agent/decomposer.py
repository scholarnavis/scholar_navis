"""
Task Decomposer
===============

Decides whether a user query should be decomposed into parallel sub-questions
for deep (multi-path) research, and produces the decomposition.

Design principles:
    - Cheap: a single lightweight LLM call produces the decomposition (or an
      empty list when the query is not decomposable).
    - Bounded: at most ``MAX_SUB_QUERIES`` sub-questions; one level only (sub-
      tasks never re-decompose, preventing token/agent explosion).
    - Reversible: the result is a plain list of sub-queries; the main agent can
      always fall back to the single-agent path when decomposition fails or
      yields nothing useful.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger("Agent.Decomposer")

MAX_SUB_QUERIES = 5
MIN_SUB_QUERIES = 2


@dataclass
class SubTask:
    """A single decomposed sub-question."""
    query: str
    rationale: str = ""


@dataclass
class Decomposition:
    """Result of decomposing a query."""
    sub_tasks: List[SubTask] = field(default_factory=list)

    @property
    def decomposable(self) -> bool:
        return len(self.sub_tasks) >= MIN_SUB_QUERIES


_DECOMPOSE_PROMPT = (
    "You are a research decomposition expert. Given a user's academic query, decide whether "
    "it can be fruitfully decomposed into multiple INDEPENDENT sub-questions that can be "
    "researched in parallel.\n\n"
    "Rules:\n"
    "- Decompose ONLY when the query is multi-dimensional (e.g., a review, a comparison, "
    "or a question spanning several distinct aspects such as mechanism, function, evolution, "
    "structure, applications).\n"
    "- Each sub-question must be self-contained, independently answerable, and phrased in "
    "academic English.\n"
    "- Return 2 to 5 sub-questions. If the query is a simple fact or single-aspect question "
    "that should NOT be decomposed, return an EMPTY list.\n"
    "- Do NOT add questions the user did not ask. Cover every part of the user's query.\n\n"
    "Reply with a single JSON object and nothing else, in exactly this shape:\n"
    '{"sub_tasks": [{"query": "<sub-question>", "rationale": "<why this split>"}]}\n'
)


class TaskDecomposer:
    """Decompose a query into parallel sub-questions via one lightweight LLM call."""

    def __init__(self, main_llm):
        self.main_llm = main_llm

    def decompose(self, query: str) -> Decomposition:
        """Return a Decomposition; empty when the query should stay single-path."""
        query = (query or "").strip()
        if not query:
            return Decomposition()

        try:
            resp = self.main_llm.chat(
                [
                    {"role": "system", "content": _DECOMPOSE_PROMPT},
                    {"role": "user", "content": f"User Query:\n{query}"},
                ],
                is_translation=True,  # plain JSON reply, no tool calls
            )
            raw = resp.get("content", "") if isinstance(resp, dict) else str(resp)
            data = self._safe_parse_json(raw)
            if not isinstance(data, dict):
                logger.warning("Decomposer returned non-object JSON; falling back to single path.")
                return Decomposition()

            items = data.get("sub_tasks") or []
            if not isinstance(items, list):
                items = [items]

            sub_tasks: List[SubTask] = []
            for it in items:
                if isinstance(it, str):
                    sub_tasks.append(SubTask(query=it.strip()))
                elif isinstance(it, dict):
                    q = (it.get("query") or "").strip()
                    if q:
                        sub_tasks.append(SubTask(query=q, rationale=str(it.get("rationale", "")).strip()))
                if len(sub_tasks) >= MAX_SUB_QUERIES:
                    break

            result = Decomposition(sub_tasks=sub_tasks)
            if result.decomposable:
                logger.info(f"Decomposed query into {len(sub_tasks)} sub-tasks.")
            else:
                logger.info("Query not decomposable; single-agent path retained.")
            return result
        except Exception as e:
            logger.warning(f"Decomposition failed ({e}); falling back to single path.")
            return Decomposition()

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
