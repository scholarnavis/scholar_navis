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
AI_TESTS = {
    "plot_bubble": {
        "note": (
            "Developer test: exercising the <b>plot_chart</b> skill (R plotting). "
            "Expected tool call: plot_chart -> bubble (GO/KEGG enrichment Dotplot) "
            "-> SVG/PNG/PDF rendering."
        ),
        "prompt": (
            "The user wants a GO/KEGG enrichment bubble plot (Dotplot). This is "
            "the canonical way such a figure is drawn (memorize it for any "
            "enrichment bubble request — it matches the clusterProfiler / "
            "enrichplot reference style used in thousands of publications):\n"
            "  - It conveys FOUR dimensions at once through a 2D plane plus "
            "visual encodings:\n"
            "    (1) Y axis = GO term / KEGG pathway names. The term with the "
            "LARGEST Gene Ratio is plotted at the TOP; the rest are sorted in "
            "DESCENDING order of Gene Ratio, so the figure forms a smooth "
            "diagonal from upper-right to lower-left (matches the reference).\n"
            "    (2) X axis = enrichment ratio (Gene Ratio / Rich Factor), "
            "plotted HORIZONTALLY at the BOTTOM (do NOT flip the axes).\n"
            "    (3) bubble size = gene count (Gene Count, bigger bubble = more "
            "genes). The right-side size legend shows continuous reference dots "
            "covering the observed count range.\n"
            "    (4) bubble color = statistical significance (FDR / p.adjust). "
            "Use a BLUE-to-WHITE-to-RED continuous gradient (similar to the "
            "RdBu reversed / viridis::plasma ramp): smaller FDR -> more red, "
            "larger FDR -> more blue. A vertical color bar on the right side "
            "labels this gradient with the heading 'FDR'.\n"
            "  - Render it as a bubble chart: x = gene_ratio, y = term "
            "(ordered by gene_ratio descending), size = gene_count, "
            "color = fdr (with the gradient above); compute FDR from the "
            "p_value column with Benjamini-Hochberg BEFORE the ggplot call.\n\n"
            "Workflow rule: if the user's plotting request is NOT clear on chart "
            "type, the x/y/size/color columns, the title, or the styling, you MUST "
            "first call the propose_plot_plan tool to show the user a concrete, "
            "editable proposal (chart type, x, y, title, style/palette/theme, and "
            "a short rationale) and wait for their confirmation or edits. Only "
            "after the user confirms or adjusts the plan may you call plot_chart "
            "to render. Never call plot_chart directly when the requirement is "
            "ambiguous.\n\n"
            "For this specific test, the requirements below are fully specified, "
            "so you may proceed directly:\n"
            'Data: {"results": ['
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
            "]}\n"
            "Draw this as a bubble chart (x = gene_ratio horizontal axis, "
            "y = term ordered by gene_ratio descending so the largest ratio "
            "is at the top, size = gene_count, color = fdr via BH correction of "
            "p_value, using the blue-white-red gradient). Call the plot_chart "
            "tool to produce the figure."
        ),
    },
    "provenance_chain": {
        "note": (
            "Developer test: exercising the <b>literature search</b> + "
            "<b>Provenance</b> chain. Expected: search_academic_literature call, "
            "then a Provenance summary block at the end of the reply."
        ),
        "prompt": (
            "Use search_academic_literature to find recent papers on "
            "'CRISPR plant genome editing', then summarize the core finding of "
            "one of them. After your answer, I expect to see the Provenance "
            "summary at the end of the conversation."
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
        ai_row.addWidget(self.btn_ai_plot)
        ai_row.addWidget(self.btn_ai_prov)
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
        for b in (self.btn_all, self.btn_r, self.btn_prov, self.btn_skill, self.btn_syntax):
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
