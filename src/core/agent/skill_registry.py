"""
Self-describing Skill Registry
==============================

Wraps the legacy ``SkillManager`` and enriches every Skill with structured
metadata so the Agent can reason about *when* and *how* to use a Skill
accurately and cheaply.

For every Skill (academic + external), we derive a lightweight ``SkillMeta``:
    - name:          canonical tool name
    - pool:          'academic' | 'external'
    - category:      extracted from the ``[Tags: ...]`` header (e.g. Literature)
    - keywords:      metadata labels (name + category + tags)
    - description:   the OpenAI-style function description
    - schema:        the raw OpenAI-compatible function schema
    - is_disabled:   disabled via the per-Skill enable gate

This keeps the actual execution path untouched (it still goes through
``SkillManager.call_skill``) while giving the Planner a compact,
self-describing view of every enabled capability.

NOTE: the registry does NOT do keyword routing anymore. Modern AGENT selection
is delegated to the main LLM's native function calling; this registry simply
makes the full enabled tool set discoverable and presentable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("Agent.SkillRegistry")

# Category keywords mapped from the [Tags: ...] header to broad domains.
# Used by the lightweight router as a zero-latency pre-filter.
_CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "Literature": ["literature", "reference", "citation", "paper", "review", "doi", "preprint"],
    "Web": ["web", "url", "wikipedia", "search", "news"],
    "Sequence": ["sequence", "fasta", "nucleotide", "protein sequence"],
    "Taxonomy": ["taxonomy", "species", "taxid", "gbif", "occurrence"],
    "Structure": ["structure", "pdb", "alphafold", "3d"],
    "Genomics": ["genome", "gene", "ensembl", "genomic", "variant"],
    "Transcriptomics": ["transcriptom", "expression", "sra", "geo", "rnaseq"],
    "Regulatory": ["motif", "jaspar", "transcription factor", "promoter"],
    "Proteomics": ["uniprot", "protein", "go annotation", "proteom"],
    "Metabolomics": ["metabolite", "pubchem", "chebi", "phytochemistry", "compound"],
    "Interaction": ["interaction", "ppi", "enrichment", "network", "string", "gprofiler"],
    "Function": ["function", "pathway", "kegg", "go", "ontology"],
    "Systems Biology": ["systems", "multiomic", "network", "pathway"],
    "ID Mapping": ["id mapping", "uniprot accession", "tair", "hgnc", "ensembl"],
    "Code": ["github", "code", "repository", "pipeline"],
}


@dataclass
class SkillMeta:
    """Lightweight self-describing metadata for a single Skill."""
    name: str
    pool: str  # 'academic' | 'external'
    category: str = "General"
    description: str = ""
    schema: Optional[dict] = None
    keywords: List[str] = field(default_factory=list)
    is_disabled: bool = False

    def to_short_repr(self) -> str:
        """A one-line, token-cheap description the Planner can scan."""
        return f"[{self.category}] {self.name}: {self.description[:160]}"


class SkillRegistry:
    """Aggregates and enriches Skills from SkillManager into self-describing metadata."""

    def __init__(self, skill_manager=None):
        self._sm = skill_manager
        self._metas: Dict[str, SkillMeta] = {}
        self._built = False

    # ------------------------------------------------------------------ #
    #  Build
    # ------------------------------------------------------------------ #
    def build(self, skill_manager=None) -> "SkillRegistry":
        """Rebuild the metadata index. Safe to call multiple times."""
        if skill_manager is not None:
            self._sm = skill_manager
        if self._sm is None:
            from src.core.skill_manager import SkillManager
            self._sm = SkillManager.get_instance()
        if self._sm is None:
            return self

        self._metas.clear()
        deselected = self._get_deselected_academic_skills()
        for name, schema in self._sm.academic_schemas.items():
            meta = self._build_meta(name, schema, pool="academic")
            meta.is_disabled = name in deselected
            self._metas[name] = meta
        for name, schema in self._sm.external_schemas.items():
            self._metas[name] = self._build_meta(name, schema, pool="external")

        self._built = True
        logger.info(
            f"SkillRegistry rebuilt: {len(self._metas)} skills "
            f"({sum(1 for m in self._metas.values() if m.pool == 'academic')} academic, "
            f"{sum(1 for m in self._metas.values() if m.pool == 'external')} external)."
        )
        return self

    @staticmethod
    def _get_deselected_academic_skills() -> set:
        """Read the set of disabled internal academic tool names from config."""
        try:
            from src.core.config_manager import ConfigManager
            return ConfigManager().get_deselected_academic_skills()
        except Exception as e:
            logger.warning(f"Failed to read deselected academic skills: {e}")
            return set()

    def _build_meta(self, name: str, schema: Optional[dict], pool: str) -> SkillMeta:
        func = (schema or {}).get("function", {}) if isinstance(schema, dict) else {}
        desc = func.get("description", "") or ""
        category = self._extract_category(desc)

        meta = SkillMeta(
            name=name,
            pool=pool,
            category=category,
            description=desc,
            schema=schema,
            keywords=self._build_keywords(name, category, desc),
        )
        return meta

    @staticmethod
    def _extract_category(description: str) -> str:
        """Read the first [Tags: X, Y] header; fall back to keyword matching."""
        m = re.search(r"\[Tags:\s*(.*?)\]", description)
        if m:
            tags = [t.strip() for t in m.group(1).split(",") if t.strip()]
            if tags:
                # Prefer a known category over an arbitrary label.
                for tag in tags:
                    for cat, kws in _CATEGORY_KEYWORDS.items():
                        if cat.lower() == tag.lower():
                            return cat
                return tags[0].title()
        # Heuristic fallback over the whole description.
        lower = description.lower()
        for cat, kws in _CATEGORY_KEYWORDS.items():
            if any(k in lower for k in kws):
                return cat
        return "General"

    @staticmethod
    def _build_keywords(name: str, category: str, description: str) -> List[str]:
        kws = {name.lower(), category.lower()}
        kws.update(_CATEGORY_KEYWORDS.get(category, []))
        # Add the explicit [Tags] labels too.
        m = re.search(r"\[Tags:\s*(.*?)\]", description)
        if m:
            kws.update(t.strip().lower() for t in m.group(1).split(",") if t.strip())
        return sorted(k for k in kws if k)

    # ------------------------------------------------------------------ #
    #  Access
    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> List[SkillMeta]:
        return [m for m in self._metas.values() if not m.is_disabled]

    def get(self, name: str) -> Optional[SkillMeta]:
        return self._metas.get(name)

    def all(self) -> List[SkillMeta]:
        return list(self._metas.values())

    def names(self) -> List[str]:
        return list(self._metas.keys())

    def by_pool(self, pool: str) -> List[SkillMeta]:
        return [m for m in self._metas.values() if m.pool == pool]

    def categories(self) -> List[str]:
        return sorted({m.category for m in self._metas.values()})


