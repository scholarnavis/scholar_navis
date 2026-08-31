"""
Developer Mode Dialog
=====================

A hidden diagnostic panel, activated by clicking the version label in the
About page five times.

Two test categories:

    * AI tests  — routed to the real Chat Assistant panel via
      ``MainWindow.route_dev_test``. A display-only note labels the test in the
      chat (visible to the user, never sent to the LLM), while the actual
      prompt drives the real agent pipeline (tool selection -> execution ->
      provenance -> plot rendering).
    * Functional tests — run in-process against the core modules directly
      (R detection, skill gating, syntax/import sanity). No AI involved.

Design principles:
    * High cohesion: one test = one focused method; no cross-deps.
    * Low coupling: AI tests only need a ``MainWindow`` reference exposing
      ``route_dev_test``; functional tests only touch core modules.
    * Read-only: functional tests use synthetic fixtures and never mutate
      user data.
"""

from __future__ import annotations

import ast
import logging
import os
import tempfile

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QLabel, QPushButton, QHBoxLayout)

from src.ui.components.dialog import BaseDialog
from src.ui.components.source_code_viewer import SourceCodeViewer

logger = logging.getLogger("UI.DeveloperDialog")

# AI tests: each entry pairs a display-only note (shown to the user, NOT sent
# to the LLM) with the actual prompt (sent to the LLM to drive real tool use).
#
# IMPORTANT: the prompt should read like a REAL user typing in the chat box —
# natural, conversational, with a concrete research goal — NOT a developer
# instruction that names tools or parameters. Let the agent pick the right tool
# itself, exactly as it would for a human user. The ``note`` is where the
# developer intent (which skill / parameter path is being exercised) lives.
AI_TESTS = {
    "plot_bubble": {
        "note": (
            "Developer test: exercising the <b>plot_chart</b> skill (R plotting). "
            "Expected tool call: plot_chart -> bubble (GO/KEGG enrichment Dotplot) "
            "-> SVG/PNG/PDF rendering."
        ),
        "prompt": (
            "I just ran a GO enrichment analysis on my RNA-seq dataset and got "
            "these terms back. Could you visualize them as an enrichment dotplot "
            "(bubble plot) for me? Here is the data: {\"results\": ["
            '{"term": "response to far red light", "category": "BP", "p_value": 2.1e-12, "gene_count": 18, "gene_ratio": 0.18}, '
            '{"term": "photoperiodism, flowering", "category": "BP", "p_value": 8.4e-11, "gene_count": 15, "gene_ratio": 0.15}, '
            '{"term": "circadian rhythm", "category": "BP", "p_value": 3.2e-9, "gene_count": 22, "gene_ratio": 0.22}, '
            '{"term": "response to red or far red light", "category": "BP", "p_value": 5.6e-8, "gene_count": 12, "gene_ratio": 0.12}, '
            '{"term": "regulation of flower development", "category": "BP", "p_value": 1.7e-6, "gene_count": 9, "gene_ratio": 0.09}, '
            '{"term": "response to blue light", "category": "BP", "p_value": 4.3e-5, "gene_count": 11, "gene_ratio": 0.11}, '
            '{"term": "phototropism", "category": "BP", "p_value": 2.8e-4, "gene_count": 6, "gene_ratio": 0.06}, '
            '{"term": "seed germination", "category": "BP", "p_value": 1.2e-3, "gene_count": 8, "gene_ratio": 0.08}, '
            '{"term": "response to cold", "category": "BP", "p_value": 6.7e-3, "gene_count": 14, "gene_ratio": 0.14}, '
            '{"term": "response to gibberellin", "category": "BP", "p_value": 2.4e-2, "gene_count": 19, "gene_ratio": 0.19}'
            "]} \n"
            "A classic GO dotplot would be great, the kind you'd put in a paper. "
            "Could you also tell me what the size and color of the bubbles represent?"
        ),
    },
    "provenance_chain": {
        "note": (
            "Developer test: exercising the <b>literature search</b> + "
            "<b>Provenance</b> chain. Expected: search_academic_literature call, "
            "then a Provenance summary block at the end of the reply."
        ),
        "prompt": (
            "I'm writing the introduction to my plant biology paper and need "
            "recent, citable papers on CRISPR-based genome editing in plants. "
            "Could you find me a few good recent reviews and summarize what the "
            "key finding of one of them is? Please make sure to cite everything "
            "properly with full references."
        ),
    },
    "literature_breadth": {
        "note": (
            "Developer test: exercising the enhanced <b>literature search</b> "
            "(breadth / depth / trust). Expected: a single search_academic_literature "
            "call with source='auto' returning an 'aggregated' payload, per-record "
            "source_dbs + confidence fields, and a 'source_stats' block. "
            "Use min_year to constrain recency."
        ),
        "prompt": (
            "I'm starting a new project on single-cell RNA sequencing analysis "
            "and want to get a broad picture of the field before I dive in. "
            "Could you look for review-level papers from the last decade, point "
            "out which are the most influential / highly cited ones, and give me "
            "a sense of which journals they tend to appear in? Summarize the main "
            "takeaways and cite every claim with the references."
        ),
    },
}


class DeveloperDialog(BaseDialog):
    """Hidden developer self-test panel."""

    def __init__(self, main_window=None, parent=None):
        # ``main_window`` routes AI tests into the real Chat panel.
        super().__init__(parent or main_window, title="Developer Mode", width=760)
        self.main_window = main_window

        self.setWindowTitle("Developer Mode")
        self.setObjectName("DeveloperDialog")

        # --- Title ---
        title = QLabel("Developer Mode")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        self.content_layout.addWidget(title)

        subtitle = QLabel(
            "AI tests run in the real Chat Assistant panel (note is display-only; "
            "the prompt drives the actual agent). Functional tests run in-process."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #888; font-size: 12px;")
        self.content_layout.addWidget(subtitle)

        # --- AI tests ---
        self.content_layout.addWidget(self._section_label("AI Tests (run in Chat panel)"))
        ai_row = QHBoxLayout()
        ai_row.setSpacing(8)
        self.btn_ai_plot = self._make_btn("AI: Plot (bubble)", lambda: self._ai_test("plot_bubble"))
        self.btn_ai_prov = self._make_btn("AI: Provenance", lambda: self._ai_test("provenance_chain"))
        self.btn_ai_lit = self._make_btn("AI: Literature (Breadth)", lambda: self._ai_test("literature_breadth"))
        ai_row.addWidget(self.btn_ai_plot)
        ai_row.addWidget(self.btn_ai_prov)
        ai_row.addWidget(self.btn_ai_lit)
        ai_row.addStretch()
        self.content_layout.addLayout(ai_row)

        # --- Functional tests ---
        self.content_layout.addWidget(self._section_label("Functional Tests"))
        func_row = QHBoxLayout()
        func_row.setSpacing(8)
        self.btn_all = self._make_btn("Run All", self._run_all)
        self.btn_r = self._make_btn("R Engine", self._test_r_engine)
        self.btn_prov = self._make_btn("Provenance (module)", self._test_provenance)
        self.btn_skill = self._make_btn("Skill Gate", self._test_skill_gate)
        self.btn_syntax = self._make_btn("Syntax/Import", self._test_syntax)
        self.btn_lit = self._make_btn("Literature Merge", self._test_literature_merge)
        for b in (self.btn_all, self.btn_r, self.btn_prov, self.btn_skill, self.btn_syntax, self.btn_lit):
            func_row.addWidget(b)
        func_row.addStretch()
        self.content_layout.addLayout(func_row)

        # --- Output area（专属源码/日志输出控件：固定最大高度 + 独立滚动条、
        #     边框底纹、复制、折叠、深色模式自适应） ---
        self.txt_output = SourceCodeViewer(
            title="Console Output",
            editable=False,
            collapsed=False,
            max_height=420,
        )
        self.content_layout.addWidget(self.txt_output, 1)

        self._log("Developer Mode ready. AI tests route to the Chat panel; "
                  "functional tests run here.")

    # ------------------------------------------------------------------ #
    #  UI helpers
    # ------------------------------------------------------------------ #
    def _section_label(self, text) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "font-weight: bold; color: #05B8CC; margin-top: 8px; "
            "border-bottom: 1px solid #333; padding-bottom: 3px;"
        )
        return lbl

    def _make_btn(self, text, handler) -> QPushButton:
        btn = QPushButton(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(handler)
        return btn

    def _log(self, msg: str, level: str = "INFO"):
        prefix = {"INFO": "[ ]", "OK": "[OK]", "FAIL": "[FAIL]", "WARN": "[!!]"}.get(level, "[ ]")
        self.txt_output.append(f"{prefix} {msg}")

    def _clear(self):
        self.txt_output.clear()

    # ------------------------------------------------------------------ #
    #  AI tests (route to real Chat panel)
    # ------------------------------------------------------------------ #
    def _ai_test(self, key: str):
        entry = AI_TESTS.get(key)
        if not entry:
            self._log(f"Unknown AI test key: {key}", "FAIL")
            return
        route = getattr(self.main_window, "route_dev_test", None) or \
                getattr(self.main_window, "route_to_chat", None)
        if route is None:
            self._log("Cannot route to Chat panel (MainWindow route method missing).", "FAIL")
            return

        self._log(f"Dispatching AI test '{key}' to Chat panel...", "INFO")
        self._log(f"Prompt: {entry['prompt'][:80]}...", "INFO")
        try:
            route(entry["prompt"], note_text=entry["note"])
            self._log("Sent to Chat panel. Check the Chat Assistant for results.", "OK")
        except TypeError:
            # Fallback: older route method without note_text.
            try:
                route(entry["prompt"])
                self._log("Sent to Chat panel (no note support).", "OK")
            except Exception as e2:
                self._log(f"Failed to dispatch AI test: {e2}", "FAIL")
        except Exception as e:
            self._log(f"Failed to dispatch AI test: {e}", "FAIL")

    # ------------------------------------------------------------------ #
    #  Functional tests
    # ------------------------------------------------------------------ #
    def _run_all(self):
        self._clear()
        self._log("=== Run All Functional Tests ===", "INFO")
        self._test_syntax(clear=False)
        self._test_skill_gate(clear=False)
        self._test_r_engine(clear=False)
        self._test_provenance(clear=False)
        self._test_literature_merge(clear=False)
        self._log("=== All functional tests finished ===", "INFO")

    def _test_r_engine(self, clear: bool = True):
        if clear:
            self._clear()
        self._log("--- R Engine Detection ---", "INFO")
        try:
            from src.core.r_engine import get_r_engine
            engine = get_r_engine()
            info = engine.detect()
            if info.get("available"):
                self._log(f"R found: {info.get('executable')} (R {info.get('version')})", "OK")
            else:
                self._log("R not found.", "WARN")
                self._log(engine.install_guidance().replace("\n", " | "), "WARN")
        except Exception as e:
            self._log(f"R engine test failed: {e}", "FAIL")

    def _test_provenance(self, clear: bool = True):
        if clear:
            self._clear()
        self._log("--- Provenance Module ---", "INFO")
        try:
            from src.core.provenance import ProvenanceCollector
            c = ProvenanceCollector(app_version="dev-test")
            c.record("search_academic_literature", "academic",
                     {"query": "CRISPR", "max_results": 3}, "success", "found 3 papers")
            c.record("plot_chart", "academic", {"chart_title": "test"}, "success",
                     source="g:Profiler", result_summary="bubble plot rendered")
            n = len(c)
            self._log(f"Recorded {n} records.", "OK")

            d = os.path.join(tempfile.gettempdir(), "scholar_navis_devtest")
            path = c.export_to_dir(d, conversation_id="devtest")
            if path and os.path.exists(path):
                lines = sum(1 for _ in open(path, encoding="utf-8"))
                self._log(f"Exported JSONL: {path} ({lines} lines)", "OK")
                os.remove(path)
            else:
                self._log("Export failed (empty or no path).", "FAIL")

            c2 = ProvenanceCollector()
            c2.record("x", "mcp", {"api_key": "secret", "token": "t"}, "success")
            snap = c2.snapshot()
            assert snap[0]["params"]["api_key"] == "<redacted>", "api_key not redacted"
            self._log("Sensitive-key redaction verified.", "OK")
        except Exception as e:
            self._log(f"Provenance test failed: {e}", "FAIL")

    def _test_literature_merge(self, clear: bool = True):
        """Functional test for the enhanced literature search aggregation.

        Validates the breadth/depth/trust upgrades of
        ``search_academic_literature`` (multi-source aggregation) without any
        network call: it feeds synthetic records into the real ``_merge_records``
        pipeline and asserts:
          * breadth  - cross-source dedup merges duplicate DOIs into one record
          * depth    - journal name and a richer record are preserved/merged
          * trust    - ``confidence`` scoring ranks higher-quality papers first
        """
        if clear:
            self._clear()
        self._log("--- Literature Merge (breadth/depth/trust) ---", "INFO")

        # Synthetic fixtures mimicking raw per-source results. No network involved.
        fixtures = [
            # Same DOI, different sources -> must be merged (breadth + depth)
            {"title": "A study on single-cell RNA-seq", "doi": "10.1000/abc123",
             "citation_count": 5, "abstract": "real abstract from OpenAlex",
             "source_db": "OpenAlex", "journal": "Nature Methods", "year": 2019},
            {"title": "A study on single-cell RNA-seq", "doi": "https://doi.org/10.1000/abc123",
             "citation_count": 9, "abstract": "No abstract",
             "source_db": "Crossref", "journal": "", "year": 2019},
            # No DOI -> dedup by normalized title (breadth)
            {"title": "Single-cell analysis: methods and pitfalls", "doi": "",
             "citation_count": 2, "abstract": "No abstract",
             "source_db": "PubMed", "journal": "Genome Biology", "year": 2020},
            # Distinct paper, low citations (trust: should rank lower)
            {"title": "Another unrelated preprint", "doi": "",
             "citation_count": 0, "abstract": "No abstract",
             "source_db": "Semantic Scholar", "journal": "", "year": 2021},
        ]

        try:
            from src.core.academic.literature import _merge_records, _normalize_doi
            merged = _merge_records(list(fixtures))
            origin = "literature._merge_records"
        except Exception as e:
            self._log(f"Import literature failed ({e}); falling back to inline logic.", "WARN")
            # Inline replica so the developer panel still self-checks on machines
            # without the biopython runtime (e.g. CI). Explicitly labelled.
            import re as _re

            def _norm_title(t):
                return _re.sub(r"\s+", " ", _re.sub(r"[^a-z0-9 ]", " ", str(t).lower())).strip()

            def _normalize_doi(d):
                if not d:
                    return ""
                return _re.sub(r"^(https?://(dx\.)?doi\.org/|http://)", "", str(d).strip(), flags=_re.IGNORECASE)

            def _merge_records(records):
                merged, order = {}, []
                for rec in records:
                    if not isinstance(rec, dict) or not rec.get("title"):
                        continue
                    doi = _normalize_doi(rec.get("doi"))
                    key = f"doi:{doi}" if doi else f"title:{_norm_title(rec.get('title'))}"
                    if key in merged:
                        ex = merged[key]
                        srcs = ex.get("source_dbs") or []
                        for s in rec.get("source_db") and [rec["source_db"]] or []:
                            if s and s not in srcs:
                                srcs.append(s)
                        ex["source_dbs"] = srcs
                        ex["citation_count"] = max(ex.get("citation_count", 0) or 0,
                                                   rec.get("citation_count", 0) or 0)
                        if ex.get("abstract") in (None, "", "No abstract") and rec.get("abstract") not in (
                                None, "", "No abstract"):
                            ex["abstract"] = rec["abstract"]
                        if not ex.get("journal") and rec.get("journal"):
                            ex["journal"] = rec["journal"]
                    else:
                        rec.setdefault("source_dbs", [rec["source_db"]] if rec.get("source_db") else [])
                        rec.setdefault("journal", "")
                        rec.setdefault("pmid", "")
                        merged[key] = rec
                        order.append(key)
                ranked = []
                for key in order:
                    rec = merged[key]
                    score = 0.0
                    n = len(rec.get("source_dbs") or [])
                    score += 1.0 * min(n, 3)
                    score += 1.0 if rec.get("doi") else 0.0
                    score += 1.0 if rec.get("abstract") not in (None, "", "No abstract") else 0.0
                    score += 0.5 if rec.get("journal") else 0.0
                    score += 0.2 * min(float(rec.get("citation_count", 0) or 0) / 100.0, 2.0)
                    rec["confidence"] = round(score, 2)
                    ranked.append(rec)
                ranked.sort(key=lambda r: (r.get("citation_count", 0) or 0, r.get("confidence", 0)), reverse=True)
                return ranked

            merged = _merge_records(list(fixtures))
            origin = "inline replica (marked)"

        # --- Assertions ---
        fails = []
        if len(merged) != 3:
            fails.append(f"expected 3 merged records, got {len(merged)}")
        if not any(r.get("doi") == "10.1000/abc123" and len(r.get("source_dbs", [])) == 2 for r in merged):
            fails.append("DOI duplicate was not cross-source merged (breadth)")
        if not any(r.get("journal") == "Nature Methods" for r in merged):
            fails.append("journal name not preserved (depth)")
        if not any(r.get("citation_count") == 9 for r in merged):
            fails.append("citation_count should take the max across sources (trust)")
        if not any(r.get("abstract") == "real abstract from OpenAlex" for r in merged):
            fails.append("richer abstract not preferred (depth)")
        ranked_first = merged[0] if merged else {}
        if merged and ranked_first.get("title", "").startswith("A study on single-cell"):
            self._log("Highest-cited merged paper ranked first (trust).", "OK")
        else:
            fails.append("ranking should place the highest-cited paper first (trust)")

        for r in merged:
            self._log(
                f"  [{r.get('title', '')[:40]}] src={r.get('source_dbs')} "
                f"cites={r.get('citation_count')} conf={r.get('confidence')}",
                "INFO")

        if fails:
            for msg in fails:
                self._log(msg, "FAIL")
            self._log(f"Literature merge test FAILED (via {origin}).", "FAIL")
        else:
            self._log(f"Literature merge test passed (via {origin}).", "OK")

    def _test_skill_gate(self, clear: bool = True):
        if clear:
            self._clear()
        self._log("--- Skill Gate (deselected_academic_skills) ---", "INFO")
        try:
            from src.core.config_manager import ConfigManager
            cm = ConfigManager()
            deselected = cm.get_deselected_academic_skills()
            self._log(f"Deselected skills: {sorted(deselected)}", "INFO")

            from src.core.skill_manager import SkillManager
            sm = SkillManager()
            schemas = sm.get_academic_schemas(tags=None)
            names = {s["function"]["name"] for s in schemas}
            leaked = deselected & names
            if leaked:
                self._log(f"Gate leak: {sorted(leaked)} still exposed.", "FAIL")
            else:
                self._log("No gated skill leaked through schemas.", "OK")
            self._log(f"Total academic schemas after gating: {len(names)}", "INFO")

            if "plot_chart" in names:
                self._log("plot_chart is registered and exposed.", "OK")
            else:
                self._log("plot_chart missing from schemas.", "WARN")
        except Exception as e:
            self._log(f"Skill gate test failed: {e}", "FAIL")

    def _test_syntax(self, clear: bool = True):
        if clear:
            self._clear()
        self._log("--- Syntax / Import Sanity ---", "INFO")
        files = [
            "src/core/plot_engine.py",
            "src/core/provenance.py",
            "src/core/r_engine.py",
            "src/core/academic_agent.py",
            "src/core/config_manager.py",
            "src/core/skill_manager.py",
            "src/core/agent/runtime.py",
            "src/core/agent/skill_registry.py",
            "src/core/agent/planner.py",
            "src/core/agent/decomposer.py",
            "src/core/agent/synthesizer.py",
            "src/task/chat_tasks.py",
            "src/ui/components/chat_bubble.py",
            "src/ui/components/dialog.py",
            "src/ui/components/developer_dialog.py",
        ]
        ok = 0
        for rel in files:
            path = os.path.normpath(os.path.join(
                os.path.dirname(__file__), "..", "..", "..", rel.replace("/", os.sep)))
            try:
                with open(path, encoding="utf-8") as f:
                    ast.parse(f.read())
                ok += 1
            except FileNotFoundError:
                self._log(f"{rel}: NOT FOUND", "FAIL")
            except SyntaxError as e:
                self._log(f"{rel}: SYNTAX ERROR {e}", "FAIL")
        self._log(f"Syntax OK: {ok}/{len(files)} files.", "OK" if ok == len(files) else "FAIL")
