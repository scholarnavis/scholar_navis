"""
Synthesizer
===========

Aggregates the per-sub-task results produced by the deep (multi-path) agent into
a single, structured academic answer.

Design principles:
    - Faithful: the synthesizer MUST compose the final answer ONLY from the
      provided sub-task results; it must not fabricate or import facts absent
      from those results (preserving the anti-hallucination guarantee).
    - Structured: the final answer is organized section-by-section, one section
      per sub-question, for reproducibility and traceability.
    - Grounded: citation ids are remapped by the caller (see ``remap`` helpers);
      the synthesizer only reads the placeholder citation markers it is told to
      expect.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List

logger = logging.getLogger("Agent.Synthesizer")

# Placeholder token wrapping each sub-task's citation ids before remapping.
_PH_PREFIX = "\u0001CITE"
_PH_SUFFIX = "\u0002"

_SYNTH_PROMPT = (
    "You are a Senior Research Scientist writing a rigorous, evidence-based synthesis. "
    "You are given the results of several independent sub-investigations into a single "
    "overarching academic question.\n\n"
    "Rules:\n"
    "1. Compose the final answer as clearly delineated sections, one per sub-question, "
    "using the exact section headings provided.\n"
    "2. STRICT GROUNDING: You may ONLY use facts, figures, and citations that appear in "
    "the provided sub-results. NEVER invent, extrapolate, or import information from your "
    "own training data. If a sub-result is empty or insufficient, explicitly state so in "
    "that section rather than filling the gap.\n"
    "3. Preserve every bracketed citation marker (e.g., [1], [101]) exactly as provided — "
    "do not renumber or drop them.\n"
    "4. Write in high-density academic prose, concise but complete. Do not add a new "
    "References section; citations are handled by the system.\n"
    "5. Output pure Markdown (sections as '## Heading')."
)


class Synthesizer:
    """Merge sub-task results into one structured answer."""

    def __init__(self, main_llm):
        self.main_llm = main_llm

    def synthesize(self, query: str, sub_results: List[Dict]) -> str:
        """sub_results: list of {'heading': str, 'query': str, 'text': str}."""
        if not sub_results:
            return ""

        if len(sub_results) == 1:
            return sub_results[0].get("text", "")

        # Build the input block with placeholder-wrapped citations so the caller
        # can remap ids afterwards without ambiguity.
        sections = []
        for i, r in enumerate(sub_results):
            heading = r.get("heading") or f"Part {i + 1}"
            text = self._wrap_citations(r.get("text", ""))
            sections.append(
                f"### SUB-RESULT {i + 1}\n"
                f"Section Heading: {heading}\n"
                f"---\n{text}\n"
            )

        headings = " | ".join((r.get("heading") or f"Part {i+1}") for i, r in enumerate(sub_results))
        user_prompt = (
            f"Overarching Question:\n{query}\n\n"
            f"Use these section headings in order: {headings}\n\n"
            f"Sub-investigation results:\n\n" + "\n".join(sections)
        )

        try:
            resp = self.main_llm.chat(
                [
                    {"role": "system", "content": _SYNTH_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                is_translation=True,  # plain markdown, no tool calls
            )
            raw = resp.get("content", "") if isinstance(resp, dict) else str(resp)
            return self._unwrap_citations(raw or "")
        except Exception as e:
            logger.warning(f"Synthesis failed ({e}); concatenating sub-results directly.")
            return self._fallback_concat(sub_results)

    # ------------------------------------------------------------------ #
    #  Citation placeholder handling
    # ------------------------------------------------------------------ #
    @staticmethod
    def _wrap_citations(text: str) -> str:
        """Wrap [123] markers as placeholders so they survive remapping."""
        return re.sub(r"\[(\d+)\]", f"{_PH_PREFIX}\\1{_PH_SUFFIX}", text)

    @staticmethod
    def _unwrap_citations(text: str) -> str:
        """Restore placeholder-wrapped ids back to [123] form."""
        return re.sub(re.escape(_PH_PREFIX) + r"(\d+)" + re.escape(_PH_SUFFIX), r"[\1]", text)

    @staticmethod
    def _fallback_concat(sub_results: List[Dict]) -> str:
        """Deterministic fallback: concatenate sections without an extra LLM call."""
        parts = []
        for r in sub_results:
            heading = r.get("heading") or ""
            text = r.get("text", "")
            if heading:
                parts.append(f"## {heading}\n\n{text}")
            else:
                parts.append(text)
        return "\n\n".join(p for p in parts if p.strip())
