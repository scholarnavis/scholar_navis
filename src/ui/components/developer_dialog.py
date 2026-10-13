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
      (R detection, skill gating, syntax/import sanity, file-type knowledge,
      text-viewer render dispatch, overlay-scrollbar visibility, attachment
      wiring). No AI involved. Checks that only need pure functions never
      create a window and never touch user data.

Design principles:
    * High cohesion: one test = one focused method; no cross-deps.
    * Low coupling: AI tests only need a ``MainWindow`` reference exposing
      ``route_dev_test``; functional tests only touch core modules.
    * Read-only: functional tests use synthetic fixtures and never mutate
      user data.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import logging
import os
import re
import tempfile

from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QLabel,
    QPushButton,
    QHBoxLayout,
    QRadioButton,
)

from src.core.theme_manager import ThemeManager, strong_weight_css
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
            '{"term": "photosynthesis", "category": "BP", "p_value": 2.8e-4, "gene_count": 6, "gene_ratio": 0.06}, '
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
    "image_upload_vision": {
        "note": (
            "Developer test: exercising the <b>image attachment + vision</b> path. "
            "A synthetic PNG is attached and sent with the prompt. Expected: the "
            "image is mounted natively (vision-capable model) or routed through the "
            "vision model (image-to-text), then a text answer describing the image. "
            "If the backend rejects image input, a friendly error panel should appear."
        ),
        "prompt": (
            "Please analyze the attached image and describe its key contents, "
            "including any text, colors, and shapes you can see."
        ),
        #: 测试用合成图片（32x32 四色块 PNG），由 _ai_test 动态生成并附加
        "image": "synthetic",
    },
    "document_attachment": {
        "note": (
            "Developer test: exercising the <b>document attachment</b> path with a "
            "non-image file (a generated YAML). Expected: the file mounts through the "
            "standard attachment pipeline (input-area chip in the preview banner), is "
            "ingested as plain text, and the answer describes its contents. Also "
            "covers the broadened uploadable types (yaml/json/toml/xml) end to end."
        ),
        "prompt": (
            "Please read the attached configuration file and tell me, in plain "
            "words, what it enables and what its numeric limits are."
        ),
        #: 非图片附件类型，由 _ai_test 调用 _make_dev_attachment 生成后挂载
        "attachment": "yaml",
    },
    "ask_user_clarify": {
        "note": (
            "Developer test: exercising the <b>ask_user</b> human-in-the-loop tool. "
            "The prompt contains a deliberate scientific ambiguity (MAPK6 exists in "
            "Arabidopsis, rice, human, etc.; IDs and sequences differ per organism). "
            "Expected: the agent calls ask_user with clickable options INSTEAD of "
            "guessing, an input card appears in the chat, and after the user answers, "
            "the agent continues the task scoped to the chosen organism."
        ),
        "prompt": (
            "I'm working on stress signaling and I'd like a quick briefing on the MAPK6 "
            "gene: what's known about its function, a couple of recent papers on it, and "
            "its protein sequence. Thanks!"
        ),
    },
    "reference_numbering": {
        "note": (
            "Developer test: exercising the <b>citation / reference numbering</b> "
            "pipeline end to end, in the real Chat panel. Expected: the agent really "
            "calls <b>search_academic_literature</b> (and/or search_preprints) and "
            "cites sources inline as <b>[key]</b> (e.g. [sharkey2019]); the app then "
            "renumbers them by FIRST APPEARANCE and renders the '📚 Cited Sources' "
            "list. Verify in the reply: (1) every inline [n] matches the list number "
            "AND the correct paper; (2) list order == order of first appearance in the "
            "text; (3) hovering a [n] shows the SAME paper; (4) clicking [n] opens the "
            "matching detail card. Check the log for 'Unresolved inline citations' — "
            "there should be none when every source was registered."
        ),
        "prompt": (
            "I'm preparing a short literature briefing for a journal club and I need "
            "accurate citations. Could you find recent and relevant studies on the "
            "regulation of photosynthesis in plants, and give me a concise summary "
            "where each claim is supported by a real paper? Please cover: (a) how "
            "the light reactions and the Calvin-Benson cycle are regulated, (b) how "
            "photosynthesis responds to environmental stress such as drought or high "
            "light, and (c) any shared molecular nodes such as Rubisco or the "
            "thioredoxin system. Cite every claim with its source and give me the "
            "full reference list."
        ),
    },
    "deep_plan_confirm": {
        "note": (
            "Developer test: exercising the <b>deep-research plan card</b> (requires "
            "the Deep Mode toggle ON). Expected: the query decomposes, a plan card "
            "lists the sub-investigations and WAITS (no parallel execution yet); "
            "'Confirm & Execute' runs the confirmed plan directly, 'Answer Directly' "
            "answers without decomposition."
        ),
        "prompt": (
            "I'm writing the technology section of a review on CRISPR gene editing in "
            "crops. Please do a deep dive covering: current delivery methods, the main "
            "crops edited so far, reported yield outcomes, and the recent regulatory "
            "landscape. Be thorough."
        ),
    },
}


# --------------------------------------------------------------------------- #
#  Render preview (fake conversation, no AI / no network)
# --------------------------------------------------------------------------- #

#: 渲染预览：注入聊天面板的灰色说明气泡（仅展示，不进 LLM 历史）
PREVIEW_NOTE = (
    "Developer test: <b>Render Preview</b> — a fake conversation (no AI, no "
    "network) exercising every internal link route (cite:// text viewer with "
    "Markdown / plain-text / syntax-highlighted branches, cite:// PDF viewer, "
    "file:// image viewer / system app, mermaid:// viewer), the inline "
    "[n] citations (hover for the card, click for the detail panel), scientific "
    "ID auto-linking and advanced Markdown (tables, code, LaTeX degradation). "
    "Click the links to verify the routing."
)

#: 渲染预览：用户侧假问题（不进 LLM 历史，仅营造对话语境）
PREVIEW_USER_TEXT = (
"Before I use this for my notes, could you show me what the rendering "
"supports? I'd like to check clickable scientific identifiers, local "
"file links, tables, code blocks and LaTeX formulas in one place."
)

#: 引文点击测试：注入 Chat 面板的灰色说明气泡（仅展示，不进 LLM 历史）
CITATION_TEST_NOTE = (
"Developer test: <b>Citation Click-Through</b> — a fake conversation injected "
"into the real Chat panel (no AI, no network). Hover any <b>[n]</b> in the reply "
"to preview the compact citation card, then click it to open the detail panel. "
"Verify: (1) the hovered / clicked entry is the SAME paper as the matching list "
"item; (2) <b>[1]</b> appears twice and opens the same entry both times; (3) "
"<b>[3]</b> is a local document, so 'open the original file' should work; (4) "
"<b>[4]</b> has no captured passage and must show the fallback layout instead of "
"an empty card. Then export the chat and import it back and hover again — the "
"same cards must still open across the round-trip."
)

#: 引文点击测试：用户侧假问题（不进 LLM 历史，仅营造对话语境）
CITATION_TEST_USER = (
"Could you double-check that my citations are clickable before I paste this "
"into my manuscript? I want to click every [n] and see the source behind it."
)

#: 引文点击测试：假 AI 回答（行内 [n] + 著录列表；不调用模型）。
#: 编号 1..4 与 _build_citation_click_references 的条目一一对应；[1] 故意重复出现，
#: 用于验证"同一来源复用同一编号"时每一次点击都落到同一条目。
CITATION_TEST_AI = """## Citation click-through check

This bubble is injected by Developer Mode — **no AI was called**. Every `[n]` below is
a live citation marker produced by the real rendering pipeline.

- The light reactions and the Calvin-Benson cycle are regulated at several levels [1].
- An online record without a DOI backs this claim [2].
- A local document backs this one, so its card can open the original file [3].
- A minimal entry shows the "no passage captured" fallback layout [4].
- The same source cited a second time reuses its number [1].

### 📚 Cited Sources

1. **Jumper, J.; Evans, R.; Pritzel, A.; Green, T.** (2021). *Highly accurate protein structure prediction with AlphaFold*. Nature. https://doi.org/10.1038/s41586-021-03819-2
2. **PubMed record 31955348** — https://pubmed.ncbi.nlm.nih.gov/31955348/
3. **Citation click-through demo document** (local file, opened by the system viewer)
4. **Minimal entry without a supporting passage**
"""

#: 渲染预览：假 AI 回答模板（第 1 部分）。占位符 __MD_CITE__ / __TXT_CITE__ /
#: __YAML_CITE__ / __TOML_CITE__ / __PDF_CITE__ / __MD_FILE__ / __PDF_FILE__ /
#: __PNG_FILE__ / __MERMAID_HASH__ 由 _test_render_preview 运行时替换为演示
#: 文件与哈希的真实值。
#: 使用 r-string 保住 LaTeX 反斜杠；覆盖：标题/表格/代码块/引用/分割线/
#: 行内与块级 LaTeX（降级渲染）/化学式下标/科研标识符自动链接/内部链接路由。
#: 注意：\frac 与 \sqrt 的参数不得含嵌套花括号（降级渲染的已知限制）。
_PREVIEW_PART_A = r"""## Rendering Preview

This message is injected by **Developer Mode** and rendered by the *real*
Markdown pipeline — **no AI was called**. Every section below exercises one
rendering feature.

### 1. Text recognition (identifiers become clickable links)

| Identifier | Example | Opens |
|:-----------|:--------|:------|
| DOI | 10.1038/s41586-021-03819-2 | doi.org |
| PubMed | PMID: 31955348 | pubmed.ncbi.nlm.nih.gov |
| UniProt | P12345 | uniprot.org |
| Gene Ontology | GO:0006915 | QuickGO |
| Arabidopsis AGI | AT1G63700 | TAIR |
| KEGG ortholog | K01647 | kegg.jp |
| SNP | rs429358 | Ensembl |
| Cotton gene (Ghir) | Ghir_D03G12349.1 | CottonGen |
| Cotton gene (Gh) | Gh_A01G0001 | CottonGen |
| Plain URL | https://www.ncbi.nlm.nih.gov | system browser |

Multi-nomenclature cotton IDs are recognized as well: Ghir_A05G01234, GH_A13G2516, GhChrD09G1234, Ghi_D03G5678, Gh_D11G324566, Gohir.A01G000100.

### 2. Link routing (one row per internal viewer / route)

| Link | Route | Expected |
|:-----|:------|:---------|
| [Demo Markdown in the internal text viewer](__MD_CITE__) | cite:// (.md) | InternalTextViewer — **rendered as Markdown** (headings / table / code fence) |
| [Demo .txt in the internal text viewer](__TXT_CITE__) | cite:// (.txt) | InternalTextViewer — plain text (no Markdown interpretation) |
| [Demo YAML in the internal text viewer](__YAML_CITE__) | cite:// (.yaml) | InternalTextViewer — syntax highlighting |
| [Demo TOML in the internal text viewer](__TOML_CITE__) | cite:// (.toml) | InternalTextViewer — syntax highlighting |
| [Demo PDF in the internal PDF viewer](__PDF_CITE__) | cite:// (.pdf) | InternalPDFViewer (+ keyword highlight) |
| [Demo image in the internal image viewer](__PNG_FILE__) | file:// (.png) | internal image viewer |
| [Demo Markdown via the system default app](__MD_FILE__) | file:// (.md) | system default app |
| [Demo PDF via the system default app](__PDF_FILE__) | file:// (.pdf) | system default app |

Inline image (clicking it opens the same internal image viewer):

![render preview demo image](__PNG_FILE__)

### 3. Markdown basics

**bold**, *italic*, `inline code`, and a [normal web link](https://python-markdown.github.io).

| Feature | Status | Note |
|:--------|:------:|-----:|
| Tables | OK | per-column alignment |
| Fenced code | OK | via the `extra` extension |

```python
def demo():
    # fenced code block -> <pre>
    return "ok"
```

> Blockquote — the `nl2br` and `sane_lists` extensions are active.

---

### 4. LaTeX (degraded rendering, no external engine)

Inline: $E = mc^2$, $\Delta G = \Delta H - T\Delta S$, $\alpha \approx \frac{\beta}{\gamma}$, $25^\circ C$.

$$F = G\frac{m_1 m_2}{r^2}$$

$$\mu = \frac{1}{n}\sum_{i=1}^{n} x_i$$

$$\int_0^{\infty} e^{-x}\,dx = 1$$

Molecular formula: C6H12O6 (chemistry subscripts).

### 5. Mermaid (clickable card + direct link)

"""

#: 渲染预览：Mermaid 图源码。模板里的代码块与"直接打开查看器"的链接共用它，
#: 因此链接里的哈希必然与 ``format_response`` 写入缓存的键一致（单一定义，
#: 不会因为两处各写一份源码而漂移）。
PREVIEW_MERMAID_CODE = (
    "flowchart LR\n"
    "    A[Markdown text] --> B[Renderer]\n"
    "    B --> C[Chat bubble]"
)

_PREVIEW_MERMAID_BLOCK = "```mermaid\n" + PREVIEW_MERMAID_CODE + "\n```\n"

#: 渲染预览：假 AI 回答模板（第 2 部分：行内引用 ``[n]``）。
#: 编号必须与 _build_preview_references 注册的条目一一对应。
_PREVIEW_PART_B = (
    "\n### 6. Inline citations (hover the marker, click for the detail panel)\n\n"
    "AlphaFold reached atomic accuracy on most single-chain proteins [1]. Its public "
    "record is searchable on PubMed [2]. A locally indexed note is cited as [3], and a "
    "minimal entry without a supporting passage is [4]. The passage shown in the card "
    "comes from the reference registry, not from this text.\n\n"
    "> Hover [1] for the compact card; click it for the full panel with the passage.\n"
)

#: 渲染预览：完整假 AI 回答 = 第 1 部分 + Mermaid 卡片与直链 + 行内引用部分
PREVIEW_AI_TEMPLATE = (
    _PREVIEW_PART_A
    + _PREVIEW_MERMAID_BLOCK
    + "\n[Open the Mermaid viewer directly](mermaid://view?hash=__MERMAID_HASH__)"
      " — mermaid:// route\n"
    + _PREVIEW_PART_B
)

#: 渲染预览演示 Markdown（cite:// → 内部文本查看器的 **Markdown 渲染** 分支；
#: file:// → 系统默认程序）。刻意包含标题 / 列表 / 表格 / 围栏代码，便于一眼
#: 确认走的是 Markdown 渲染而不是原样纯文本。正文必须保留 "renderer" 一词——
#: 模板里的 cite:// 链接用它做关键词高亮（见 _build_render_preview_fixtures）。
PREVIEW_DEMO_MD = (
    "# Scholar Navis - Render Preview Demo (Markdown)\n"
    "\n"
    "This file is generated by the developer render-preview test.\n"
    "It is the target of a `cite://` link (internal text viewer) and a\n"
    "`file://` link (system default app).\n"
    "\n"
    "## What to check\n"
    "\n"
    "1. This heading and list are **rendered as Markdown**, not shown raw.\n"
    "2. The table below is laid out as a table.\n"
    "3. The fenced block below gets a monospace code background.\n"
    "\n"
    "| Feature | Expected |\n"
    "|:--------|:---------|\n"
    "| Headings | styled, not raw `#` |\n"
    "| Table | bordered grid |\n"
    "| Code fence | `<pre>` block |\n"
    "\n"
    "```json\n"
    '{"pipeline": "markdown renderer", "ok": true}\n'
    "```\n"
    "\n"
    'The internal viewer highlights the word "renderer" when this file is\n'
    "opened through `cite://`.\n"
)

#: 渲染预览演示 .txt（cite:// -> 内部文本查看器的纯文本分支：不解释 Markdown）
PREVIEW_DEMO_TXT = (
    "Scholar Navis - Render Preview Demo (plain text)\n"
    "=================================================\n"
    "\n"
    "This .txt file is the target of a cite:// link that must open the internal\n"
    "text viewer in its plain-text branch: the '#' rules, list markers and\n"
    "https://example.org/links below stay literal text (URLs still clickable).\n"
    "\n"
    "# this must NOT become a heading\n"
    "- this must NOT become a list\n"
)

#: 渲染预览演示 YAML（cite:// -> 内部文本查看器的 **语法高亮** 分支）
PREVIEW_DEMO_YAML = (
    "# Scholar Navis - Render Preview Demo (YAML)\n"
    "project: scholar_navis\n"
    "features:\n"
    "  - markdown_render: true\n"
    "  - syntax_highlight: true\n"
    "limits:\n"
    "  max_images_per_message: 8\n"
    "  highlight_max_bytes: 1500000\n"
)

#: 渲染预览演示 TOML（cite:// -> 内部文本查看器的 **语法高亮** 分支）
PREVIEW_DEMO_TOML = (
    "# Scholar Navis - Render Preview Demo (TOML)\n"
    "[viewer]\n"
    "markdown = true\n"
    "highlight = true\n"
    "\n"
    "[limits]\n"
    "max_images_per_message = 8\n"
    "highlight_max_bytes = 1500000\n"
)


class DeveloperDialog(BaseDialog):
    """Hidden developer self-test panel."""

    def __init__(self, main_window=None, parent=None):
        # ``main_window`` routes AI tests into the real Chat panel.
        super().__init__(parent or main_window, title="Developer Mode", width=760)
        self.main_window = main_window

        self.setWindowTitle("Developer Mode")
        self.setObjectName("DeveloperDialog")

        # --- Title ---
        self.title_lbl = QLabel("Developer Mode")
        self.content_layout.addWidget(self.title_lbl)

        self.subtitle_lbl = QLabel(
            "AI tests run in the real Chat Assistant panel (note is display-only; "
            "the prompt drives the actual agent). Functional tests run in-process."
        )
        self.subtitle_lbl.setWordWrap(True)
        self.content_layout.addWidget(self.subtitle_lbl)

        #: 分区标题（配色随主题刷新，见 _apply_theme）
        self._section_labels: list = []

        # --- AI tests ---
        self.content_layout.addWidget(self._section_label("AI Tests (run in Chat panel)"))
        ai_row = QHBoxLayout()
        ai_row.setSpacing(8)
        self.btn_ai_plot = self._make_btn("AI: Plot (bubble)", lambda: self._ai_test("plot_bubble"))
        self.btn_ai_prov = self._make_btn("AI: Provenance", lambda: self._ai_test("provenance_chain"))
        self.btn_ai_lit = self._make_btn("AI: Literature (Breadth)", lambda: self._ai_test("literature_breadth"))
        self.btn_ai_img = self._make_btn("AI: Image Vision", lambda: self._ai_test("image_upload_vision"))
        ai_row.addWidget(self.btn_ai_plot)
        ai_row.addWidget(self.btn_ai_prov)
        ai_row.addWidget(self.btn_ai_lit)
        ai_row.addWidget(self.btn_ai_img)
        ai_row.addStretch()
        self.content_layout.addLayout(ai_row)

        # AI 测试第二行：human-in-the-loop（ask_user / deep plan 卡）与非图片附件
        ai_row2 = QHBoxLayout()
        ai_row2.setSpacing(8)
        self.btn_ai_ask = self._make_btn("AI: Ask-User (HITL)",
                                         lambda: self._ai_test("ask_user_clarify"))
        self.btn_ai_deep = self._make_btn("AI: Deep Plan Card",
                                          lambda: self._ai_test("deep_plan_confirm"))
        self.btn_ai_doc = self._make_btn("AI: File Attachment",
                                         lambda: self._ai_test("document_attachment"))
        ai_row2.addWidget(self.btn_ai_ask)
        ai_row2.addWidget(self.btn_ai_deep)
        ai_row2.addWidget(self.btn_ai_doc)
        ai_row2.addStretch()
        self.content_layout.addLayout(ai_row2)

        # AI 测试第三行：参考文献编号链路（真实检索 + 编号一致性核对）
        ai_row3 = QHBoxLayout()
        ai_row3.setSpacing(8)
        self.btn_ai_refnum = self._make_btn("AI: Reference Numbering (real search)",
                                            lambda: self._ai_test("reference_numbering"))
        ai_row3.addWidget(self.btn_ai_refnum)
        ai_row3.addStretch()
        self.content_layout.addLayout(ai_row3)

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
        self.btn_img = self._make_btn("Image Pipeline", self._test_image_pipeline)
        for b in (self.btn_all, self.btn_r, self.btn_prov, self.btn_skill,
                  self.btn_syntax, self.btn_lit, self.btn_img):
            func_row.addWidget(b)
        func_row.addStretch()
        self.content_layout.addLayout(func_row)

        # 功能测试第二行：交互卡链路。Deep Plan Card 放在首位 —— 不依赖
        # AI / 网络，点击立即完成"组件行为 + 气泡渲染链路"全量自检。
        func_row2 = QHBoxLayout()
        func_row2.setSpacing(8)
        self.btn_deep_card = self._make_btn("Deep Plan Card", self._test_deep_plan_card)
        self.btn_hitl = self._make_btn("HITL Pipeline", self._test_hitl_pipeline)
        self.btn_render = self._make_btn("Render Preview", self._test_render_preview)
        func_row2.addWidget(self.btn_deep_card)
        func_row2.addWidget(self.btn_hitl)
        func_row2.addWidget(self.btn_render)
        func_row2.addStretch()
        self.content_layout.addLayout(func_row2)

        # 功能测试第三行：附件与阅读链路自检（可上传类型 / 文本查看器渲染分派 /
        # 滚动条可见性 / 附件芯片接线）。全部只做纯函数与类接口断言：
        # 不创建窗口、不切换面板、不改动用户数据。
        func_row3 = QHBoxLayout()
        func_row3.setSpacing(8)
        self.btn_filetypes = self._make_btn("File Types", self._test_file_types)
        self.btn_textviewer = self._make_btn("Text Viewer", self._test_text_viewer_render)
        self.btn_scrollbar = self._make_btn("Scrollbar Theme", self._test_scrollbar_theme)
        self.btn_attachwire = self._make_btn("Attachment Wiring", self._test_attachment_wiring)
        self.btn_typography = self._make_btn("Chat Typography", self._test_chat_typography)
        self.btn_refnum = self._make_btn("Reference Numbering", self._test_reference_citations)
        self.btn_refint = self._make_btn("Reference Integrity", self._test_reference_integrity)
        # 引文点击：切到 Chat 面板注入带引用的假对话，供用户实际悬停 / 点击 [n]
        self.btn_citeclick = self._make_btn("Citation Click-Through", self._test_citation_click)
        for b in (self.btn_filetypes, self.btn_textviewer,
                  self.btn_scrollbar, self.btn_attachwire, self.btn_typography,
                  self.btn_refnum, self.btn_refint, self.btn_citeclick):
            func_row3.addWidget(b)
        func_row3.addStretch()
        self.content_layout.addLayout(func_row3)

        # 功能测试第四行：引用可信性。Integrity 为离线反捏造审计（进 Run All）；
        # Literature Trust 会发起**真实检索 + DOI 解析**，故独立成按钮、不进 Run All，
        # 避免默认自检占用网络与配额。
        func_row4 = QHBoxLayout()
        func_row4.setSpacing(8)
        self.btn_littrust = self._make_btn("Literature Trust (live)",
                                           self._test_literature_source_trust)
        self.btn_uiinter = self._make_btn("UI Copy / Drag-Scroll",
                                          self._test_ui_copy_scroll)
        for b in (self.btn_littrust, self.btn_uiinter):
            func_row4.addWidget(b)
        func_row4.addStretch()
        self.content_layout.addLayout(func_row4)

        # --- Output area（专属源码/日志输出控件：固定最大高度 + 独立滚动条、
        #     边框底纹、复制、折叠、深色模式自适应） ---
        self.txt_output = SourceCodeViewer(
            title="Console Output",
            editable=False,
            collapsed=False,
            max_height=420,
        )
        self.content_layout.addWidget(self.txt_output, 1)

        # 首次主题应用由 BaseDialog 在事件循环第一帧统一触发（此时本类
        # __init__ 已执行完，_apply_theme 依赖的控件均已存在）。
        self._log("Developer Mode ready. AI tests route to the Chat panel; "
                  "functional tests run here.")

    # ------------------------------------------------------------------ #
    #  UI helpers
    # ------------------------------------------------------------------ #
    def _section_label(self, text) -> QLabel:
        lbl = QLabel(text)
        self._section_labels.append(lbl)
        return lbl

    # ------------------------------------------------------------------ #
    #  Theme
    # ------------------------------------------------------------------ #
    def _apply_theme(self):
        """主题化标题 / 副标题 / 分区标题。

        原实现把 #05B8CC 与 #333 硬编码在控件样式里，浅色主题下分区标题的
        分隔线与正文色对比不足、深色主题下又与背景糊在一起。这里统一改为
        主题取色，并随 BaseDialog 的 theme_changed 自动刷新。
        """
        super()._apply_theme()
        tm = ThemeManager()

        self.title_lbl.setStyleSheet(
            f"font-size: 18px; font-weight: {strong_weight_css()}; color: {tm.color('text_main')}; "
            f"font-family: {tm.font_family()};")
        self.subtitle_lbl.setStyleSheet(
            f"color: {tm.color('text_muted')}; font-size: 12px; "
            f"font-family: {tm.font_family()};")

        section_style = (
            f"font-weight: {strong_weight_css()}; color: {tm.color('accent')}; margin-top: 8px; "
            f"border-bottom: 1px solid {tm.color('border')}; padding-bottom: 3px; "
            f"font-family: {tm.font_family()};")
        for lbl in self._section_labels:
            lbl.setStyleSheet(section_style)

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

        # 测试附件：图片走标准库构造的 PNG，其余类型走 _make_dev_attachment
        attachment_paths = []
        if entry.get("image"):
            path = self._make_synthetic_png()
            if path:
                attachment_paths = [path]
                self._log(f"Synthetic test image: {path}", "INFO")
            else:
                self._log("Failed to create synthetic test image; sending text only.", "WARN")
        elif entry.get("attachment"):
            path = self._make_dev_attachment(entry["attachment"])
            if path:
                attachment_paths = [path]
                self._log(f"Synthetic test attachment: {path}", "INFO")
            else:
                self._log("Failed to create synthetic test attachment; sending text only.",
                          "WARN")

        self._log(f"Dispatching AI test '{key}' to Chat panel...", "INFO")
        self._log(f"Prompt: {entry['prompt'][:80]}...", "INFO")
        try:
            if attachment_paths:
                route(entry["prompt"], note_text=entry["note"],
                      attachment_paths=attachment_paths)
            else:
                route(entry["prompt"], note_text=entry["note"])
            self._log("Sent to Chat panel. Check the Chat Assistant for results.", "OK")
        except TypeError:
            # Fallback: older route method without note_text / attachment_paths.
            try:
                route(entry["prompt"])
                self._log("Sent to Chat panel (no note/attachment support).", "OK")
            except Exception as e2:
                self._log(f"Failed to dispatch AI test: {e2}", "FAIL")
        except Exception as e:
            self._log(f"Failed to dispatch AI test: {e}", "FAIL")

    @staticmethod
    def _make_dev_attachment(kind: str) -> str:
        """写出一份开发测试用的非图片附件，返回路径；失败返回空串。

        覆盖"拓宽后的可上传类型"里需要专用渲染的几种格式，内容复用渲染预览的
        演示文本（单一定义，不会与预览里的文件内容漂移）。写入临时目录，无副作用。
        """
        snippets = {
            "yaml": PREVIEW_DEMO_YAML,
            "toml": PREVIEW_DEMO_TOML,
            "json": ('{\n  "pipeline": "attachment test",\n'
                     '  "limits": {"max_images_per_message": 8}\n}\n'),
        }
        content = snippets.get(kind)
        if content is None:
            logger.warning("Unknown dev attachment kind: %s", kind)
            return ""

        demo_dir = os.path.join(tempfile.gettempdir(), "scholar_navis_devtest")
        os.makedirs(demo_dir, exist_ok=True)
        path = os.path.join(demo_dir, f"devtest_attachment.{kind}")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            logger.warning("Failed to write dev attachment %s: %s", path, e)
            return ""
        return path

    @staticmethod
    def _make_synthetic_png() -> str:
        """生成测试用合成 PNG（32x32 四色块）到临时目录，返回路径。

        标准库构造（zlib + struct 手写 PNG chunk），无 Qt / 第三方依赖，
        保证开发者面板在任何环境下都能生成该测试附件。
        """
        import struct
        import zlib

        def _chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        try:
            # 32x32：四象限红/绿/蓝/白，便于视觉模型给出可验证的结构化描述
            rows = []
            for y in range(32):
                row = b"\x00"  # filter type 0
                for x in range(32):
                    if x < 16 and y < 16:
                        row += b"\xe5\x3a\x3a"      # red
                    elif x >= 16 and y < 16:
                        row += b"\x3a\x9a\x5a"      # green
                    elif x < 16 and y >= 16:
                        row += b"\x3a\x6a\xcf"      # blue
                    else:
                        row += b"\xf2\xf2\xf2"      # white
                rows.append(row)

            ihdr = struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0)  # 8-bit RGB
            png = (b"\x89PNG\r\n\x1a\n"
                   + _chunk(b"IHDR", ihdr)
                   + _chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
                   + _chunk(b"IEND", b""))

            cache_dir = os.path.join(tempfile.gettempdir(), "scholar_navis_cache")
            os.makedirs(cache_dir, exist_ok=True)
            path = os.path.join(cache_dir, "devtest_synthetic.png")
            with open(path, "wb") as f:
                f.write(png)
            return path
        except OSError as e:
            logger.warning(f"Synthetic PNG generation failed: {e}")
            return ""

    @staticmethod
    def _make_synthetic_pdf() -> str:
        """生成一页带固定关键词的 PDF 到临时目录，返回路径（失败返回空串）。

        用途：渲染预览里 ``cite://`` → **内部 PDF 查看器** 这条路由。该查看器基于
        QtWebEngine 的内置 PDF 阅读器，需要结构合法的 PDF；这里直接用 Qt 自带的
        ``QPdfWriter`` 生成（比手写字节流可靠得多），正文含固定关键词，便于同时
        验证"打开 + Ctrl+F 关键词高亮"两步。
        """
        try:
            from PySide6.QtGui import QFont, QPageSize, QPainter, QPdfWriter

            demo_dir = os.path.join(tempfile.gettempdir(), "scholar_navis_devtest")
            os.makedirs(demo_dir, exist_ok=True)
            path = os.path.join(demo_dir, "render_preview_demo.pdf")

            writer = QPdfWriter(path)
            writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
            writer.setResolution(96)          # 低分辨率：便于按像素直接定位文本
            painter = QPainter(writer)
            try:
                painter.setFont(QFont("Helvetica", 14))
                painter.drawText(60, 90, "Scholar Navis - Render Preview PDF")
                painter.drawText(60, 130, "This page is the cite:// PDF viewer target.")
                painter.drawText(60, 170, "Search keyword: preview (Ctrl+F).")
            finally:
                painter.end()
            return path if os.path.exists(path) else ""
        except Exception as e:
            logger.warning(f"Synthetic PDF generation failed: {e}")
            return ""

    # ------------------------------------------------------------------ #
    #  Functional tests
    # ------------------------------------------------------------------ #
    def _test_reference_citations(self, clear: bool = True):
        """Functional test for the citation-numbering pipeline (key protocol).

        Reproduces and guards against "正文编号与参考文献列表对不上"：模型用稳定的
        ``[key]`` 引用，正文编号由程序按"首次出现顺序"统一分配，因此模型先写正文
        还是先调用 cite_references 都不影响一致性。断言：
          * key 解析 + 按首次出现顺序重编号（与注册顺序无关）；
          * 重复出现的同一 key 复用同一编号；
          * 数字引用（本地 KB 文档号）仍可解析；
          * 未登记 key 被如实报告，而非被静默错编；
          * 渲染出的列表顺序与重编号后的顺序一致。
        """
        if clear:
            self._clear()
        self._log("--- Reference Numbering (key protocol) ---", "INFO")
        try:
            from src.core.references import ReferenceRegistry, ReferenceItem

            reg = ReferenceRegistry()
            # 注册顺序故意与正文引用顺序不同（复现"模型提交顺序 ≠ 引用顺序"）。
            reg.add(ReferenceItem(index=0, key="long2006",
                                  title="Can improvement in photosynthesis increase crop yields?",
                                  authors="Long", year="2006"))
            reg.add(ReferenceItem(index=0, key="sharkey2019",
                                  title="The Calvin-Benson cycle and its regulation",
                                  authors="Sharkey", year="2019"))
            # 本地 KB 文档：无 key，只有已分配编号（模拟 '--- [Document 3] ---'）。
            reg.seed(3, ReferenceItem(index=3, title="Local KB doc",
                                      path="/tmp/kb.pdf", kind="local_document"))

            body = ("光合作用受光调节 [sharkey2019]，且碳同化参与 [long2006]；"
                    "再次引用同一来源 [sharkey2019]；本地文档 [3]；"
                    "未登记 [ghost2020]。")
            new_text, ordered, unresolved, repaired = reg.resolve_citations(body)

            fails = []
            # 1) 首次出现顺序：sharkey=1, long=2, local=3
            if [it.index for it in ordered] != [1, 2, 3]:
                fails.append(f"order != [1,2,3]: {[it.index for it in ordered]}")
            if [it.key for it in ordered] != ["sharkey2019", "long2006", ""]:
                fails.append(f"order keys wrong: {[it.key for it in ordered]}")
            # 2) 正文就地改写 + 重复 key 复用同号
            expect = ("光合作用受光调节 [1]，且碳同化参与 [2]；再次引用同一来源 [1]；"
                      "本地文档 [3]；未登记 [ghost2020]。")
            if new_text != expect:
                fails.append(f"rewrite mismatch: got={new_text!r} exp={expect!r}")
            # 3) 未登记 key 被报告；精确命中不应上报任何"识别回填"（否则日志失真）
            if unresolved != ["ghost2020"]:
                fails.append(f"unresolved != ['ghost2020']: {unresolved}")
            if repaired:
                fails.append(f"exact-only body must not report repairs: {repaired}")
            # 4) 渲染列表顺序与编号一致
            html = reg.render_items(ordered)
            if not (0 <= html.find("[1]") < html.find("[2]") < html.find("[3]")):
                fails.append("rendered list order != renumbered order")
            if "<b>[1]</b>" not in html or "Sharkey" not in html:
                fails.append("rendered list missing [1]/Sharkey")

            # 5) 回归场景：模型"先写正文再登记"（两版草稿同 key）也不应错位
            reg2 = ReferenceRegistry()
            reg2.add(ReferenceItem(index=0, key="a2004", title="A", year="2004"))
            reg2.add(ReferenceItem(index=0, key="b2011", title="B", year="2011"))
            draft = "第一版 [b2011][a2004] 结束。\n第二版 [b2011][a2004] 结束。"
            t2, _o2, u2, _r2 = reg2.resolve_citations(draft)
            if t2.count("[1][2]") != 2 or u2:
                fails.append(f"duplicate-draft rewrite failed: {t2!r} unresolved={u2}")

            # 6) 兜底清理回归：落不到著录的引用 key 不再上屏——**含思考面板内的残留**
            #    真实顺序是"先 resolve_citations 重编号，再清理剩余 key 形态 token"；
            #    不再按 unresolved 清单删除（它只覆盖正文，思考块不参与编号）。
            from src.core.references import strip_unregistered_cite_keys
            raw = ("正文 [sharkey2019] 与未登记 [lariguet2004] 混排；"
                   "思考块里也在用 <think>考虑以 [christie2017] 支撑</think>；"
                   "经典文献 [mendel1866]；保留项 [EC]、[3]、[Fig. 2]、[the 1990s]。")
            rewritten6, _o6, unresolved6, _r6 = reg.resolve_citations(raw)
            cleaned, removed = strip_unregistered_cite_keys(rewritten6)
            if "[1]" not in cleaned:
                fails.append(f"registered key not renumbered before cleanup: {cleaned!r}")
            for bad in ("lariguet2004", "christie2017", "mendel1866"):
                if bad in cleaned:
                    fails.append(f"citation-key residue survived cleanup: {bad}")
            for keep in ("[EC]", "[Fig. 2]", "[the 1990s]"):
                if keep not in cleaned:
                    fails.append(f"unrelated bracket usage was dropped: {keep}")
            # [3] 是已登记的本地 KB 文档号：应被**重编号**（首次出现顺序第 2 条），
            # 而不是按"未登记"清理掉——这两件事必须区分开。
            if "[2]" not in cleaned:
                fails.append(f"registered numeric citation must be renumbered, not dropped: {cleaned!r}")
            if "lariguet2004" not in unresolved6:
                fails.append(f"unresolved should report body keys: {unresolved6}")
            if "christie2017" not in removed:
                fails.append(f"think-block key not cleaned: removed={removed}")

            # 7) 识别回归：写歪的 key 必须落回**真实著录**并照常编号，而不是被删掉丢引文
            #    （旧实现把 frutani2007 当"未登记"直接删除，等于让句子无声失去引文）
            reg3 = ReferenceRegistry()
            reg3.add(ReferenceItem(index=0, key="furutani2007",
                                   title="Mutations in the NPH3 gene of Arabidopsis",
                                   authors="Furutani, M.; Sakamoto, N.", year="2007"))
            reg3.add(ReferenceItem(index=0, key="zhang2026condensate",
                                   title="NPH3 condensates", authors="Zhang, Y.", year="2026"))
            body3 = "姓氏拼写偏差 [frutani2007]；主题词被截断 [zhang2026] 结束。"
            t3, o3, u3, r3 = reg3.resolve_citations(body3)
            if t3 != "姓氏拼写偏差 [1]；主题词被截断 [2] 结束。":
                fails.append(f"near-miss key not repaired into [n]: {t3!r}")
            if u3:
                fails.append(f"repaired keys must not be reported unresolved: {u3}")
            if {tier for _t, tier, _n in r3} != {"typo", "prefix"}:
                fails.append(f"repair report wrong: {r3}")
            if [it.index for it in o3] != [1, 2]:
                fails.append(f"repaired items not in first-appearance order: {[it.index for it in o3]}")
            if strip_unregistered_cite_keys(t3)[1]:
                fails.append("fully repaired text should have nothing left to strip")

            # 8) 元数据回填：在线来源只有编号、模型却写成"姓氏+年份"时也能落回真实条目
            reg4 = ReferenceRegistry()
            reg4.seed(7, ReferenceItem(index=7, kind="online_source",
                                       title="Lateral relocation of auxin efflux regulator PIN3",
                                       authors="Friml, J.; Wisniewska, J.", year="2002"))
            t4, o4, u4, r4 = reg4.resolve_citations("PIN3 的侧向定位 [friml2002]。")
            if "[1]" not in t4 or u4 or not o4:
                fails.append(f"metadata fallback failed: {t4!r} {u4}")
            elif [tier for _t, tier, _n in r4] != ["metadata"]:
                fails.append(f"metadata fallback tier wrong: {r4}")

            # 9) 歧义即放弃：前缀能命中两个候选时**不得猜测**（错挂文献比缺标记严重）
            reg5 = ReferenceRegistry()
            reg5.add(ReferenceItem(index=0, key="hu2019cotton", title="A", authors="Hu, X.", year="2019"))
            reg5.add(ReferenceItem(index=0, key="hu2019nph3", title="B", authors="Hu, X.", year="2019"))
            t5, o5, u5, r5 = reg5.resolve_citations("见 [hu2019] 所述。")
            if "[hu2019]" not in t5 or u5 != ["hu2019"] or r5 or o5:
                fails.append(f"ambiguous key must stay unresolved: {t5!r} {u5} {r5}")

            if fails:
                for f in fails:
                    self._log(f"FAIL: {f}", "FAIL")
                self._log(f"Reference numbering test FAILED ({len(fails)} assertion(s)).", "FAIL")
            else:
                self._log("Key resolution + first-appearance renumbering OK.", "OK")
                self._log("Numeric (KB) citation + duplicate-draft key reuse OK.", "OK")
                self._log("Unresolved-key reporting OK.", "OK")
                self._log("Key-residue cleanup OK (body + reasoning panel).", "OK")
                self._log("Near-miss / metadata key repair OK; ambiguity not guessed.", "OK")
                self._log(f"Sample rewritten body: {new_text}", "INFO")
                self._log("Reference numbering test PASSED.", "OK")
        except Exception as e:
            self._log(f"Reference numbering test failed: {e}", "FAIL")

    def _test_reference_integrity(self, clear: bool = True):
        """反捏造审计（离线）：参考文献列表里的每一条都必须有出处。

        列表条目只可能来于三处，逐条钉死其不可捏造性：

        1. 识别回填（folded/prefix/typo/metadata）——只允许命中**已存在**的条目：
           整条引用管线前后，注册表的条目数、每个条目的对象身份都必须不变；
        2. 正文凭空写的 key——识别不中就清理，绝不允许"补造一条列出来"；
        3. 列表条目必须能落回注册表条目并带可核查标识（标题/DOI/URL/本地路径）。

        联网核查（DOI 解析、标题年份比对）见 Literature Trust (live)。
        """
        if clear:
            self._clear()
        self._log("--- Reference Integrity (anti-fabrication) ---", "INFO")
        fails = []
        try:
            from src.core.references import (ReferenceItem, ReferenceRegistry,
                                             strip_unregistered_cite_keys)

            reg = ReferenceRegistry()
            reg.add(ReferenceItem(index=0, key="furutani2007",
                                  title="Mutations in the NPH3 gene of Arabidopsis",
                                  authors="Furutani, M.; Sakamoto, N.", year="2007"))
            reg.add(ReferenceItem(index=0, key="zhang2026condensate",
                                  title="NPH3 condensates", authors="Zhang, Y.", year="2026"))
            reg.add(ReferenceItem(index=0, key="hu2019cotton",
                                  title="A", authors="Hu, X.", year="2019"))
            reg.add(ReferenceItem(index=0, key="hu2019nph3",
                                  title="B", authors="Hu, X.", year="2019"))
            reg.seed(7, ReferenceItem(index=7, kind="online_source",
                                      title="Lateral relocation of auxin efflux regulator PIN3",
                                      authors="Friml, J.", year="2002"))
            before = {it.index: it for it in reg.items()}

            body = ("拼写偏差 [frutani2007]；主题词截断 [zhang2026]；"
                    "只有编号的来源 [friml2002]；歧义 [hu2019]；"
                    "凭空 [fabricated2099]；短姓氏再引一次 [hu2019]。")
            new_text, ordered, unresolved, repaired = reg.resolve_citations(body)

            # 1) 识别链不得新建、替换或改写任何注册条目
            after = {it.index: it for it in reg.items()}
            if len(after) != len(before):
                fails.append(f"registry size changed: {len(before)} -> {len(after)}")
            for idx, item in before.items():
                if after.get(idx) is not item:
                    fails.append(f"registry item {idx} was replaced or mutated by recognition")
            if len(ordered) > len(after):
                fails.append(f"listed more items ({len(ordered)}) than registered ({len(after)})")

            # 2) 列表条目必须落回注册表条目，且带可核查标识
            fingerprints = {(it.key, it.title, it.url, it.doi, it.path) for it in after.values()}
            for it in ordered:
                if (it.key, it.title, it.url, it.doi, it.path) not in fingerprints:
                    fails.append(f"listed item [{it.index}] matches no registry entry")
                if not (it.title or it.url or it.doi or it.path or it.snippet):
                    fails.append(f"listed item [{it.index}] carries no provenance field at all")

            # 3) 凭空 key：不得进列表，且必须被清理（不补条目、也不留坏标记）
            if any("fabricated2099" in f"{it.key} {it.title}" for it in ordered):
                fails.append("fabricated key produced a reference entry")
            cleaned, removed = strip_unregistered_cite_keys(new_text)
            if "fabricated2099" not in removed or "fabricated2099" in cleaned:
                fails.append(f"fabricated key not cleaned: removed={removed}")

            # 4) 猜测防护：歧义（两个候选）与短姓氏一律不识别
            if unresolved.count("hu2019") != 2:
                fails.append(f"ambiguous / short-surname key must stay unresolved: {unresolved}")
            if "hu2019" in {tok for tok, _tier, _num in repaired}:
                fails.append(f"ambiguous key was guessed: {repaired}")
            if not {"typo", "prefix", "metadata"} <= {tier for _tok, tier, _num in repaired}:
                fails.append(f"recognition tiers under-tested (fixture wrong): {repaired}")

            # 5) 模型提交的无标识条目仍须可指认（只能人工核查），不得静默隐身
            bare = ReferenceItem.from_ai_payload({"title": "Some paper", "year": "2099"}, index=0)
            if not bare.dedupe_key:
                fails.append("model-submitted entry without identifiers lost its traceable key")

            # 6) "未核实"标记：只在"无任何可核对落点"时出现，且不破坏导出往返解析
            from src.core.references import UNVERIFIED_LABEL, parse_footer_entries
            marked = ReferenceItem(index=1, title="Model-written entry", year="2099")
            opened = ReferenceItem(index=2, title="Has DOI", year="2004", doi="10.1/abc")
            verbatim = ReferenceItem(index=3, title="Verified by snippet", year="2005",
                                     snippet="verbatim text", verified=True)
            localdoc = ReferenceItem(index=4, title="KB chunk", path="/tmp/kb.pdf",
                                     kind="local_document")
            html = ReferenceRegistry.render_items([marked, opened, verbatim, localdoc])
            if not marked.needs_manual_review or UNVERIFIED_LABEL not in html:
                fails.append("entry without any identifier must carry the unverified mark")
            for it in (opened, verbatim, localdoc):
                if it.needs_manual_review:
                    fails.append(f"entry [{it.index}] must NOT be marked unverified "
                                 f"(doi/path/verbatim present)")
                if UNVERIFIED_LABEL in ReferenceRegistry.render_items([it]):
                    fails.append(f"entry [{it.index}] rendered the unverified mark wrongly")
            parsed = parse_footer_entries(html)
            if [r.index for r in parsed] != [1, 2, 3, 4] or not all(r.text for r in parsed):
                fails.append(f"unverified mark broke footer round-trip: {parsed}")
        except Exception as e:
            fails.append(f"exception: {e}")

        if fails:
            for f in fails:
                self._log(f"FAIL: {f}", "FAIL")
            self._log(f"Reference integrity test FAILED ({len(fails)} assertion(s)).", "FAIL")
            return
        self._log("Recognition never creates, replaces or mutates registry entries.", "OK")
        self._log("Every listed item traces back to a registered entry with provenance.", "OK")
        self._log("Fabricated inline keys are cleaned, never added to the list.", "OK")
        self._log("Ambiguity / short surnames are never guessed into a citation.", "OK")
        self._log("Unverified mark: shown only for entries with no DOI/URL/path and no "
                  "verbatim-checked snippet; footer export round-trip intact.", "OK")
        self._log("Note: a FABRICATED DOI/URL still looks 'identified' — only "
                  "Literature Trust (live) can disprove it over the network.", "WARN")
        self._log("Reference integrity test PASSED.", "OK")

    def _test_citation_click(self, clear: bool = True):
        """引文点击链路测试：离线断言 + 注入真实 Chat 面板供手动点击。

        Part A（离线）逐段钉住"引用能点、点了有内容、换机器也有内容"这条链路：

        1. 行内 ``[n]`` 被真实渲染管线改写成 ``ref://cite?n=`` 链接（悬停 / 点击入口），
           且重复出现的同一编号每一处都带链接；
        2. 悬停卡与详情面板的数据来源按 ``(气泡编号, 引用编号)`` 命中**同一条目**；
        3. 归档序列化（sanitize + JSON）往返后条目一字不差——防"导出再导入引用卡变空"；
        4. 导入白名单保留 ``references`` 与 ``ui_components``——这是引用被静默丢掉的
           头号位置，专测它；
        5. 归档会收集 ``references[].path``，并在导入时改写为解包副本——否则引用卡还在、
           "打开原文"却指向旧机路径。

        Part B（可视）把带引用的假对话注入 Chat 面板，由用户实际悬停 / 点击。与
        Render Preview 同理**不进 Run All**：它会切换主窗口页面，批量跑时是意外副作用。
        """
        if clear:
            self._clear()
        self._log("--- Citation Click-Through (inline [n] / hover card / detail panel) ---",
                  "INFO")
        fails = []

        # 0) [3] 的"打开原文"需要真实文件才点得动
        local_path = ""
        try:
            demo_dir = os.path.join(tempfile.gettempdir(), "scholar_navis_devtest")
            os.makedirs(demo_dir, exist_ok=True)
            local_path = os.path.join(demo_dir, "citation_click_demo.md")
            with open(local_path, "w", encoding="utf-8") as f:
                f.write("# Citation click-through demo document\n\n"
                        "This file exists so the [3] citation card can open it.\n")
        except OSError as e:
            self._log(f"Demo document not written ({e}); [3] will show the fallback only.",
                      "WARN")

        refs = self._build_citation_click_references(local_path)

        # 1) 行内 [n] → ref://cite?n= 链接
        try:
            from src.ui.components.text_formatter import TextFormatter
            html = TextFormatter.format_response(CITATION_TEST_AI, 0, set(), set(), {})
            missing = [n for n in (1, 2, 3, 4) if f"ref://cite?n={n}" not in html]
            if missing:
                fails.append(f"inline [n] not linked: {missing}")
            if html.count("ref://cite?n=1") < 2:
                fails.append("reused citation [1] was not linked at every occurrence")
        except Exception as e:
            fails.append(f"render pipeline: {e}")

        # 2) 悬停卡 / 详情面板的数据来源
        try:
            from src.ui.components.citation_popup import CitationPopupController
            store = CitationPopupController.instance()
            probe = -99      # 负数编号：测试不占用真实消息编号空间
            store.merge_references(refs, probe)
            for entry in refs:
                got = store.lookup(probe, int(entry["index"]))
                if got != entry:
                    fails.append(f"citation store mismatch at [{entry['index']}]: {got}")
        except Exception as e:
            fails.append(f"citation store: {e}")

        # 3) 归档序列化往返
        try:
            import json as _json

            from src.core.chat_archive import sanitize
            round_tripped = _json.loads(
                _json.dumps([sanitize(r) for r in refs], ensure_ascii=False))
            if round_tripped != refs:
                fails.append("archive sanitize/JSON round-trip altered the citation entries")
        except Exception as e:
            fails.append(f"sanitize round-trip: {e}")

        # 4) 导入白名单必须保留 references / ui_components
        try:
            from src.task.chat_tasks import ImportChatHistoryTask
            comps = [{"kind": "ask_user", "key": "probe",
                      "state": {"selected": ["A"], "custom": ""}}]
            norm = ImportChatHistoryTask._normalize_msg(
                {"role": "assistant", "content": "x",
                 "references": refs, "ui_components": comps})
            if norm.get("references") != refs:
                fails.append("import whitelist dropped or altered references")
            if norm.get("ui_components") != comps:
                fails.append("import whitelist dropped or altered ui_components")
        except Exception as e:
            fails.append(f"import whitelist: {e}")

        # 5) 归档收集 + 导入路径改写（引用卡的"打开原文"）
        try:
            from src.core import chat_archive
            probe_msgs = [{"role": "assistant", "content": "x",
                           "references": [dict(r) for r in refs]}]
            if local_path and local_path not in chat_archive._collect_attachments(probe_msgs):
                fails.append("archive did not collect the citation's local file")
            moved = local_path + ".imported"
            hits = chat_archive.remap_attachment_paths(probe_msgs, {local_path: moved})
            got = [r.get("path") for r in probe_msgs[0]["references"]
                   if r.get("index") == 3]
            if not hits or got != [moved]:
                fails.append(f"citation path not remapped on import: hits={hits} got={got}")
        except Exception as e:
            fails.append(f"archive path remap: {e}")

        if fails:
            for item in fails:
                self._log(f"FAIL: {item}", "FAIL")
            self._log(f"Citation click-through assertions FAILED ({len(fails)}).", "FAIL")
        else:
            self._log("Inline [n] -> ref:// link (reused number linked everywhere): OK",
                      "OK")
            self._log("Hover/click store keyed by (message, citation) returns the same entry: OK",
                      "OK")
            self._log("Archive sanitize + JSON round-trip keeps every field: OK", "OK")
            self._log("Import whitelist keeps references + ui_components: OK", "OK")
            self._log("Local original file collected + path remapped on import: OK", "OK")

        # Part B: 注入真实 Chat 面板（不进 history，不调用模型）
        route = getattr(self.main_window, "route_dev_conversation", None)
        if route is None:
            self._log("MainWindow.route_dev_conversation missing; chat injection skipped.",
                      "FAIL")
            return
        self._log("Dispatching the citation demo to the Chat panel...", "INFO")
        try:
            route(CITATION_TEST_NOTE, CITATION_TEST_USER, CITATION_TEST_AI, refs)
            self._log("Citation demo injected. Hover and click the [n] markers in the "
                      "reply to open the compact card and the detail panel.", "OK")
        except Exception as e:
            self._log(f"Failed to inject the citation demo: {e}", "FAIL")

    def _test_ui_copy_scroll(self, clear: bool = True):
        """UI 交互自检：图表文字可复制、悬停卡未核实标记、超宽块拖选自动滚动。

        这三项都是"不点就不知道坏没坏"的交互，用真实控件在自检里跑一遍：

        * 图表页 HTML 不得禁用文字选择，且拖拽平移不得抢占选中；
        * 悬停卡的未核实标记与参考文献列表**同源**判定（无 DOI/URL/路径且未逐字校验）；
        * 超宽块把选区拖到左/右边缘时滚动条跟着走，到边界停表，无横向溢出不启动。

        图表实际取文字要靠 WebEngine 完成页面加载，等 5 秒；超时只记 WARN（无头或
        受限环境可能起不来 WebEngine），不影响其余断言。
        """
        if clear:
            self._clear()
        self._log("--- UI Interactions (copy / drag-scroll / unverified mark) ---", "INFO")
        import time as _time

        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QGuiApplication, QMouseEvent
        from PySide6.QtWidgets import QApplication

        fails, warns = [], []

        # --- 1) 超宽块：拖选到边缘自动滚动 ---
        try:
            from src.ui.components.chat_bubble import OverflowBlock

            def _make_block(html, kind):
                blk = OverflowBlock(html, block_kind=kind)
                try:
                    blk.setAttribute(Qt.WA_DontShowOnScreen, True)  # 自检不弹窗
                except AttributeError:
                    pass    # 个别 Qt 构建没有该属性：退化为短暂显示，不影响断言
                blk.resize(420, 220)
                blk.show()
                QApplication.processEvents()
                blk._resync(force=True)
                return blk

            wide = ("<table width='2600' cellspacing='0' cellpadding='6'>"
                    "<tr><td>Alpha</td><td>Beta</td></tr></table>")
            blk = _make_block(wide, "table")
            vp = blk.browser.viewport()
            hbar = blk.horizontalScrollBar()

            def _send(etype, x, y, buttons=Qt.LeftButton):
                QApplication.sendEvent(vp, QMouseEvent(
                    etype, QPointF(x, y), Qt.LeftButton, buttons, Qt.NoModifier))

            if hbar.maximum() > 0:
                w, h = vp.width(), vp.height()
                _send(QEvent.MouseButtonPress, w // 2, h // 2)
                _send(QEvent.MouseMove, w - 2, h // 2)
                if blk._edge_dir[0] <= 0 or not blk._edge_timer.isActive():
                    fails.append(f"right-edge drag did not arm scrolling: {blk._edge_dir}")
                hbar.setValue(hbar.maximum())
                blk._auto_scroll_step()
                if blk._edge_timer.isActive():
                    fails.append("timer must stop when the scrollbar cannot move further")
                hbar.setValue(hbar.maximum())
                before = hbar.value()
                _send(QEvent.MouseMove, 1, h // 2)
                if blk._edge_dir[0] >= 0:
                    fails.append(f"left-edge drag direction wrong: {blk._edge_dir}")
                blk._auto_scroll_step()
                if hbar.value() >= before:
                    fails.append(f"left-edge step did not scroll: {before} -> {hbar.value()}")
                _send(QEvent.MouseMove, w // 2, h // 2)
                if blk._edge_timer.isActive() or blk._edge_dir != (0, 0):
                    fails.append(f"mid-area move must stop edge scrolling: {blk._edge_dir}")
                _send(QEvent.MouseButtonPress, w - 2, h // 2)
                _send(QEvent.MouseButtonRelease, w - 2, h // 2, buttons=Qt.NoButton)
                if blk._edge_dir != (0, 0) or blk._edge_timer.isActive():
                    fails.append("release must stop edge scrolling")
            else:
                warns.append("wide-table fixture did not overflow here — "
                             "scroll assertions skipped (geometry-dependent)")
            blk.hide()
            blk.deleteLater()

            # 无横向溢出的块不得启动自动滚动
            narrow = _make_block("<p>short</p>", "text")
            narrow._update_edge_dir(0, 0)
            if narrow._edge_dir != (0, 0) or narrow._edge_timer.isActive():
                fails.append("block without overflow must not auto-scroll")
            narrow.hide()
            narrow.deleteLater()
        except Exception as e:
            fails.append(f"edge-scroll check raised: {e}")

        # --- 2) 悬停卡：未核实标记与列表同源 ---
        try:
            from src.core.references import UNVERIFIED_LABEL
            from src.ui.components.citation_popup import CitationPopup

            pop = CitationPopup()
            pop.set_reference({"index": 1, "kind": "reference",
                               "title": "Model-written entry", "year": "2099"},
                              pop.MODE_FULL)
            if UNVERIFIED_LABEL not in pop._lbl_unverified.text():
                fails.append("popup mark text is out of sync with the reference list")
            if pop._lbl_unverified.isHidden():
                fails.append("popup did not mark an entry with no verifiable identifier")
            for data in ({"index": 2, "kind": "online_source", "title": "T",
                          "url": "https://example.org/x"},
                         {"index": 3, "kind": "local_document", "title": "T",
                          "path": "/tmp/kb.pdf"},
                         {"index": 4, "kind": "reference", "title": "T",
                          "snippet": "verbatim text", "verified": True}):
                pop.set_reference(data, pop.MODE_FULL)
                if not pop._lbl_unverified.isHidden():
                    fails.append(f"popup wrongly marked entry {data['index']} as unverified")
            pop.deleteLater()
        except Exception as e:
            fails.append(f"popup mark check raised: {e}")

        # --- 3) Mermaid：文字可选中 / 可复制 ---
        try:
            from src.ui.components.mermaid_viewer import MermaidViewer

            viewer = MermaidViewer()
            viewer.setAttribute(Qt.WA_DontShowOnScreen, True)
            page = viewer._build_page_html("default")
            if "user-select: none" in page:
                fails.append("mermaid page still disables text selection")
            if "user-select: text" not in page:
                fails.append("mermaid labels are not selectable")
            if "navisOnLabel" not in page:
                fails.append("pan handler does not yield to text selection")
            if not hasattr(viewer, "act_copy_text"):
                fails.append("'Copy Text' action is missing from the toolbar")

            # 剪贴板通路（与页面无关的那一段）
            QGuiApplication.clipboard().setText("__navis_selftest__")
            viewer._on_diagram_text("Alpha\nBeta")
            if QGuiApplication.clipboard().text() != "Alpha\nBeta":
                fails.append("diagram text was not written to the clipboard")

            # 真机通路：渲染一张小图，等页面把文字交回来
            viewer.mermaid_code = "flowchart LR\n  A[Alpha] --> B[Beta]"
            QGuiApplication.clipboard().setText("__navis_selftest__")
            viewer.render_diagram()
            deadline = _time.monotonic() + 5.0
            got = ""
            while _time.monotonic() < deadline:
                QApplication.processEvents()
                got = QGuiApplication.clipboard().text()
                if "Alpha" in got:
                    break
                _time.sleep(0.05)
            if "Alpha" not in got:
                warns.append("WebEngine did not return diagram text within 5s "
                             "(headless / restricted environment?)")
            viewer.hide()
            viewer.deleteLater()
        except Exception as e:
            fails.append(f"mermaid copy check raised: {e}")

        for w in warns:
            self._log(f"WARN: {w}", "WARN")
        if fails:
            for f in fails:
                self._log(f"FAIL: {f}", "FAIL")
            self._log(f"UI interaction test FAILED ({len(fails)} assertion(s)).", "FAIL")
            return
        self._log("Drag-select at block edges drives the scrollbar; stops at bounds.", "OK")
        self._log("Blocks without horizontal overflow never auto-scroll.", "OK")
        self._log("Hover card shows the same unverified mark as the reference list.", "OK")
        self._log("Mermaid labels are selectable and 'Copy Text' fills the clipboard.", "OK")
        self._log("UI interaction test PASSED.", "OK")

    def _test_literature_source_trust(self, clear: bool = True):
        """文献检索可信性检查（**联网**）：真实检索 -> 逐条核查 -> 核查表格。

        走与运行时完全相同的链路（技能结果 -> ``AgentRuntime._collect_tool_citations``
        注入引用编号 -> 注册表），然后逐条核对：

        1. 字段齐备：标题 / 四位年份 / (DOI 或 URL)；
        2. 编号可落回：注入的 ``_mcp_cite_id`` 能在注册表里定位到**同一来源**
           （URL 或标题一致）——编号与来源一对一是"引用可信"的前提；
        3. 文献真实存在：DOI 经 OpenAlex 解析成功，且解析回来的标题与年份与检索
           记录一致（关键一步，专门抓上游/模型拼出来的假条目）；
        4. 链接可达：来源记录链接的 HTTP 状态。

        结论分三档：OK（全部通过）/ WARN（字段缺失或链接不可达）/ SUSPECT（DOI 解析
        不到或标题年份不符）。表格逐行给出结论与说明，便于人工抽查原始检索数据。
        """
        if clear:
            self._clear()
        self._log("--- Literature Source Trust (live retrieval) ---", "INFO")
        query = "NPH3 phototropism Arabidopsis"
        limit = 5
        self._log(f"Query: {query!r} | max_results={limit}", "INFO")

        try:
            from src.core.academic.literature import search_academic_literature
        except Exception as e:
            self._log(f"Cannot import literature tools: {e}", "FAIL")
            return

        try:
            raw = search_academic_literature(query=query, max_results=limit)
        except Exception as e:
            self._log(f"Retrieval failed (network/proxy?): {e}", "FAIL")
            return

        import json as _json

        try:
            records = (_json.loads(raw) or {}).get("results") or []
        except (ValueError, AttributeError):
            self._log("Retrieval returned unparsable payload.", "FAIL")
            return
        if not records:
            self._log("Retrieval returned zero records — cannot verify anything.", "WARN")
            return
        records = [r for r in records if isinstance(r, dict)][:limit]

        # 编号注入 + 落回核对（与生产同一实现）
        try:
            from src.core.agent.runtime import AgentRuntime
            from src.core.references import ReferenceItem, ReferenceRegistry
            from src.task.chat_tasks import _registry_item_from_source, _source_dedupe_key

            reg = ReferenceRegistry()
            by_key = {}

            def _collector(meta):
                dk = _source_dedupe_key(meta)
                if dk and dk in by_key:
                    return by_key[dk]
                ref_id = reg.add(_registry_item_from_source(0, meta))
                if dk:
                    by_key[dk] = ref_id
                return ref_id

            runtime = AgentRuntime(main_llm=None, skill_manager=None, mcp_manager=None,
                                   cite_collector=_collector, log_fn=lambda *a, **k: None)
            # annotated 与 records 同序（同一份 payload 的注入结果），可直接逐条比对。
            annotated = _json.loads(runtime._collect_tool_citations(
                "search_academic_literature", raw))["results"][:limit]
        except Exception as e:
            self._log(f"Citation-id injection failed: {e}", "FAIL")
            return

        from src.core.academic.base import mcp_request, openalex_api_key
        from src.core.evidence import normalize_for_match

        def _title_agrees(a, b):
            na, nb = normalize_for_match(a), normalize_for_match(b)
            return bool(na and nb and (na in nb or nb in na))

        def _col(value, width):
            """定宽列：超宽截断加省略号，且恒留一个空格分隔，避免列粘连。"""
            text = str(value or "")
            if len(text) >= width:
                text = text[:width - 1] + "…"
            return text.ljust(width) + " "

        def _check_doi(doi, title, year):
            """DOI 真实性：OpenAlex 能否解析，标题与年份是否一致。"""
            clean = str(doi or "").strip()
            if not clean:
                return "no-doi", "记录未带 DOI"
            url = f"https://api.openalex.org/works/https://doi.org/{clean}"
            if openalex_api_key:
                url += f"?api_key={openalex_api_key}"
            try:
                res = mcp_request("GET", url, timeout=12)
            except Exception as e:
                return "error", f"请求失败：{str(e)[:60]}"
            if res.status_code != 200:
                return "missing", f"OpenAlex HTTP {res.status_code}"
            try:
                work = res.json() or {}
            except ValueError:
                return "error", "返回非 JSON"
            real_title = str(work.get("title") or "")
            real_year = str(work.get("publication_year") or "")
            if not real_title:
                return "missing", "OpenAlex 无标题记录"
            if not _title_agrees(real_title, title):
                return "mismatch", f"标题不符：OpenAlex={real_title[:48]!r}"
            if year and real_year and year != real_year:
                return "mismatch", f"年份不符：OpenAlex={real_year} vs 检索={year}"
            return "ok", f"OpenAlex 标题/年份一致（{real_year}）"

        def _check_url(url):
            if not url:
                return "no-url", ""
            try:
                res = mcp_request("GET", url, timeout=8, stream=True, allow_redirects=True)
                code = int(getattr(res, "status_code", 0))
                try:
                    res.close()
                except Exception:
                    pass
            except Exception as e:
                return "error", f"链接请求失败：{str(e)[:48]}"
            if 200 <= code < 400:
                return "ok", f"HTTP {code}"
            if code in (401, 403, 405):
                return "warn", f"HTTP {code}（站点拒绝自动访问）"
            return "warn", f"HTTP {code}"

        self._log("", "INFO")
        self._log(_col("#", 3) + _col("ID", 5) + _col("年", 6) + _col("DOI/URL", 34)
                  + _col("结论", 8) + "说明", "INFO")
        summary = {"ok": 0, "warn": 0, "suspect": 0}
        for i, (rec, ann) in enumerate(zip(records, annotated), 1):
            title = str(rec.get("title") or "")
            year = str(rec.get("year") or rec.get("publication_year") or "").strip()
            doi = str(rec.get("doi") or "")
            url = str(rec.get("url") or "")
            ref_id = ann.get("_mcp_cite_id")
            notes, verdict = [], "ok"

            def _worse(level):
                nonlocal verdict
                rank = {"ok": 0, "warn": 1, "suspect": 2}
                if rank[level] > rank[verdict]:
                    verdict = level

            if not title:
                _worse("suspect"); notes.append("无标题")
            if not (len(year) == 4 and year.isdigit() and 1500 <= int(year) <= 2100):
                _worse("warn"); notes.append(f"年份异常({year or '空'})")
            if not doi and not url:
                _worse("warn"); notes.append("无 DOI/URL")

            # 编号与来源一对一对齐
            item = reg.lookup(int(ref_id)) if isinstance(ref_id, int) else None
            if item is None:
                _worse("suspect"); notes.append("未注入引用编号")
            elif not (_title_agrees(item.title, title) or (url and item.url == url)):
                _worse("suspect"); notes.append("编号落回的来源与检索条目不一致")

            doi_level, doi_note = _check_doi(doi, title, year)
            _worse({"ok": "ok", "missing": "suspect", "mismatch": "suspect",
                    "no-doi": "warn", "error": "warn"}[doi_level])
            notes.append(doi_note)

            _target_url = url or (item.url if item else "")
            url_level, url_note = _check_url(_target_url)
            _worse({"ok": "ok", "warn": "warn", "no-url": "warn", "error": "warn"}[url_level])
            if url_note:
                notes.append(url_note)

            summary[verdict] += 1
            self._log(_col(i, 3) + _col(ref_id if ref_id is not None else "-", 5)
                      + _col(year, 6) + _col(doi or url or "-", 34)
                      + _col(verdict.upper(), 8) + "; ".join(notes), "OK" if verdict == "ok" else
                      ("WARN" if verdict == "warn" else "FAIL"))

        self._log("", "INFO")
        self._log(f"Checked {len(records)} record(s): "
                  f"OK={summary['ok']} WARN={summary['warn']} SUSPECT={summary['suspect']}",
                  "OK" if summary["suspect"] == 0 else "FAIL")
        if summary["suspect"]:
            self._log("SUSPECT 行代表'检索结果无法被独立来源证实'——请人工核对原始记录，"
                      "不要直接采信。", "FAIL")
        if summary["warn"]:
            self._log(f"{summary['warn']} record(s) could not be fully auto-verified "
                      "(missing identifier / link blocked) — review the WARN rows.", "WARN")
        if not summary["suspect"] and not summary["warn"]:
            self._log("All retrieved sources were independently confirmed "
                      "(DOI resolved + title/year matched).", "OK")

    def _run_all(self):
        self._clear()
        self._log("=== Run All Functional Tests ===", "INFO")
        self._test_syntax(clear=False)
        # 纯函数 / 接口断言先跑：不启动引擎、不创建窗口，代价最低
        self._test_file_types(clear=False)
        self._test_text_viewer_render(clear=False)
        self._test_scrollbar_theme(clear=False)
        self._test_attachment_wiring(clear=False)
        self._test_chat_typography(clear=False)
        self._test_skill_gate(clear=False)
        self._test_r_engine(clear=False)
        self._test_provenance(clear=False)
        self._test_literature_merge(clear=False)
        self._test_reference_citations(clear=False)
        # 反捏造审计：纯函数断言，离线可跑，必须进默认自检
        self._test_reference_integrity(clear=False)
        # UI 交互自检：图表复制、悬停卡标记、超宽块拖选自动滚动
        self._test_ui_copy_scroll(clear=False)
        self._test_image_pipeline(clear=False)
        self._test_deep_plan_card(clear=False)
        self._test_hitl_pipeline(clear=False)
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
                self._check_r_packages(engine)
            else:
                self._log("R not found.", "WARN")
                self._log(engine.install_guidance().replace("\n", " | "), "WARN")
        except Exception as e:
            self._log(f"R engine test failed: {e}", "FAIL")

    def _check_r_packages(self, engine):
        """核心绘图包自检：解释器可用 ≠ 能出图。

        NixOS / 精简发行版上的 R 常常只装了基础解释器（ggplot2 等需另装），
        仅报 "R found" 会给开发者一个假绿灯——真实绘图会在 R 侧 stop() 退出，
        前端只看到 plot_chart 返回 error，表现为"AI 不会画图"。
        """
        from src.core.plot_engine import CORE_R_PACKAGES
        from src.core.r_engine import package_install_guidance

        try:
            status = engine.check_packages(CORE_R_PACKAGES)
        except Exception as e:
            self._log(f"R package check failed: {e}", "FAIL")
            return

        missing = [p for p in CORE_R_PACKAGES if not status.get(p)]
        if not missing:
            self._log(f"R packages OK: {', '.join(CORE_R_PACKAGES)}", "OK")
            return

        self._log(f"R packages missing: {', '.join(missing)} "
                  f"(plot_chart will fail until installed)", "FAIL")
        for line in package_install_guidance(missing).splitlines():
            if line.strip():
                self._log(f"  {line.strip()}", "WARN")

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
             "citation_count": 9, "abstract": "",
             "source_db": "Crossref", "journal": "", "year": 2019},
            # No DOI -> dedup by normalized title (breadth)
            {"title": "Single-cell analysis: methods and pitfalls", "doi": "",
             "citation_count": 2, "abstract": "",
             "source_db": "PubMed", "journal": "Genome Biology", "year": 2020},
            # Distinct paper, low citations (trust: should rank lower)
            {"title": "Another unrelated preprint", "doi": "",
             "citation_count": 0, "abstract": "",
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
                        if not str(ex.get("abstract") or "").strip() and str(rec.get("abstract") or "").strip():
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
                    score += 1.0 if str(rec.get("abstract") or "").strip() else 0.0
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

    def _test_image_pipeline(self, clear: bool = True):
        """Functional test for the image-attachment pipeline (no AI, no network).

        Validates, in order:
          * classification  - is_image_file / is_svg_file / guess_mime matrix
          * encode          - encode_data_url round-trip (bytes & MIME intact)
          * guards          - missing file / oversize image rejected properly
          * capability      - ChatGenerationTask._looks_vision_capable matrix
          * friendly error  - image-rejection 400 -> "Model Cannot Read Images"
          * caption cache   - image-to-text disk cache round-trip
          * svg rasterize   - SVG -> PNG conversion (UI-process stage)
        """
        if clear:
            self._clear()
        self._log("--- Image Pipeline ---", "INFO")
        fails = []

        # 1) 分类与 MIME 推断
        try:
            from src.core.image_utils import (guess_mime, is_image_file, is_svg_file,
                                              IMAGE_EXTENSIONS, MAX_IMAGE_BYTES)
            cases = [("a.png", "image/png", True, False),
                     ("b.JPG", "image/jpeg", True, False),
                     ("c.svg", "image/svg+xml", True, True),
                     ("d.pdf", "image/png", False, False),
                     ("", "image/png", False, False)]
            for name, mime, is_img, is_svg in cases:
                if guess_mime(name) != mime:
                    fails.append(f"guess_mime({name!r}) != {mime}")
                if is_image_file(name) != is_img:
                    fails.append(f"is_image_file({name!r}) != {is_img}")
                if is_svg_file(name) != is_svg:
                    fails.append(f"is_svg_file({name!r}) != {is_svg}")
            self._log(f"Classification: {len(cases)} cases checked, "
                      f"{len(IMAGE_EXTENSIONS)} supported extensions.", "INFO")
        except Exception as e:
            fails.append(f"classification module: {e}")

        # 2) data URL 编码往返（字节与 MIME 保持一致）
        png_path = self._make_synthetic_png()
        if not png_path or not os.path.exists(png_path):
            fails.append("synthetic PNG generation failed")
        else:
            try:
                with open(png_path, "rb") as f:
                    raw = f.read()
                data_url = encode_data_url(png_path)
                if not data_url.startswith("data:image/png;base64,"):
                    fails.append(f"unexpected data URL header: {data_url[:40]}...")
                elif base64.b64decode(data_url.partition(",")[2]) != raw:
                    fails.append("encode_data_url round-trip mismatch")
                else:
                    self._log(f"encode_data_url round-trip OK "
                              f"({len(raw)} B -> {len(data_url) // 1024} KB b64).", "OK")
            except Exception as e:
                fails.append(f"encode round-trip: {e}")

        # 3) 防御路径：文件缺失 / 超限
        try:
            from src.core.image_utils import encode_data_url, MAX_IMAGE_BYTES
            try:
                encode_data_url(os.path.join(tempfile.gettempdir(), "scholar_navis_missing_.png"))
                fails.append("missing image was not rejected")
            except FileNotFoundError:
                self._log("Missing file correctly rejected (FileNotFoundError).", "OK")

            big = os.path.join(tempfile.gettempdir(), "scholar_navis_devtest_oversize.png")
            with open(big, "wb") as f:
                f.truncate(MAX_IMAGE_BYTES + 1)  # 稀疏写入，磁盘占用极小
            try:
                encode_data_url(big)
                fails.append("oversize image was not rejected")
            except ValueError:
                self._log(f"Oversize image correctly rejected (> {MAX_IMAGE_BYTES // (1024 * 1024)} MB).", "OK")
            finally:
                os.remove(big)
        except OSError as e:
            fails.append(f"guard fixtures: {e}")

        # 4) 视觉能力判定矩阵 + 5) 友好错误映射
        try:
            from src.task.chat_tasks import ChatGenerationTask
            matrix = [("gpt-4o", True), ("gpt-4.1-mini", True),
                      ("claude-3-5-sonnet-20241022", True), ("gemini-2.5-flash", True),
                      ("qwen-vl-max", True), ("glm-4v-flash", True), ("o3-mini", True),
                      ("deepseek-chat", False), ("deepseek-vl2", False),  # 显式排除优先
                      ("ernie-4.5", False), ("llama-3.3-70b", False), ("", False)]
            bad = [m for m, want in matrix if ChatGenerationTask._looks_vision_capable(m) != want]
            if bad:
                fails.append(f"vision capability matrix mismatch: {bad}")
            else:
                self._log(f"Vision capability matrix OK ({len(matrix)} models).", "OK")

            data = json.loads(ChatGenerationTask._friendly_error_payload(
                "400 Bad Request", "Invalid content: image_url not supported by this model"))
            if data.get("title") != "Model Cannot Read Images":
                fails.append("image-rejection error not mapped to friendly payload")
            else:
                self._log("Friendly error maps image-rejection 400 correctly.", "OK")
            fmt = json.loads(ChatGenerationTask._friendly_error_payload(
                "400 Bad Request", "The image format webp is not supported by this model"))
            if fmt.get("title") != "Image Format Not Accepted":
                fails.append("image-format error not mapped to friendly payload")
            else:
                self._log("Friendly error maps image-format 400 correctly.", "OK")
            plain = json.loads(ChatGenerationTask._friendly_error_payload("Timeout", "upstream timeout"))
            if plain.get("title") != "Timeout":
                fails.append("friendly error passthrough broken")
        except Exception as e:
            fails.append(f"chat_tasks checks: {e}")

        # 6) 图生文磁盘缓存往返（stub 实例，不启动真实生成任务）
        if png_path and os.path.exists(png_path):
            try:
                from src.task.chat_tasks import ChatGenerationTask

                class _Stub:
                    logger = logging.getLogger("devtest.caption")

                stub = _Stub()
                info = {"name": "devtest.png", "image_path": png_path}
                desc = "red/green/blue/white quadrant test image"
                ChatGenerationTask._save_caption_cache(stub, info, desc)
                if ChatGenerationTask._load_caption_cache(stub, info) != desc:
                    fails.append("caption cache round-trip mismatch")
                elif ChatGenerationTask._load_caption_cache(stub, {"image_path": "no_such.png"}) is not None:
                    fails.append("caption cache miss should return None")
                else:
                    self._log("Caption cache round-trip OK.", "OK")
                os.remove(ChatGenerationTask._caption_cache_path(stub, info))
            except Exception as e:
                fails.append(f"caption cache: {e}")

        # 7) SVG 栅格化（UI 进程阶段，QSvgRenderer）
        try:
            from src.tools.chat_mixins.attachments import ChatAttachmentsMixin
            svg_path = os.path.join(tempfile.gettempdir(), "scholar_navis_devtest.svg")
            with open(svg_path, "w", encoding="utf-8") as f:
                f.write('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
                        '<rect width="64" height="64" fill="#05b8cc"/></svg>')
            png_out = ChatAttachmentsMixin._rasterize_svg(svg_path)
            if png_out and os.path.exists(png_out) and os.path.getsize(png_out) > 0:
                self._log(f"SVG rasterized -> {os.path.basename(png_out)}.", "OK")
                os.remove(svg_path)
            else:
                fails.append("SVG rasterization returned no PNG")
        except Exception as e:
            fails.append(f"svg rasterize: {e}")

        if fails:
            for msg in fails:
                self._log(msg, "FAIL")
            self._log("Image pipeline test FAILED.", "FAIL")
        else:
            self._log("Image pipeline test passed.", "OK")

    def _test_deep_plan_card(self, clear: bool = True):
        """Dedicated offline test for the deep-plan card (no AI, no network).

        Part A (widget): confirm emits the edited plan and locks the card;
        decisions after lock are ignored; skip emits the original query;
        restore resets the checklist to the original plan.
        Part B (render chain): the ``<deep_plan data=...>`` marker ->
        base64 JSON -> ChatBubbleWidget must render exactly one
        DeepPlanCardWidget, strip the marker from the body, forward
        confirm/skip through the bubble signals, dedupe repeated markers
        by query key, and drop malformed payloads without side effects.
        """
        if clear:
            self._clear()
        self._log("--- Deep Plan Card (widget + render chain) ---", "INFO")
        fails = []

        from src.ui.components.chat_bubble import ChatBubbleWidget
        from src.ui.components.deep_plan_card import DeepPlanCardWidget

        query = "CRISPR in crops: deep dive"
        sub_tasks = [{"query": "Delivery methods", "rationale": "core"},
                     {"query": "Regulation", "rationale": ""}]

        # --- Part A: 组件级行为 ---
        try:
            plan = DeepPlanCardWidget({"query": query, "sub_tasks": sub_tasks})
            conf = []
            plan.sig_confirm.connect(conf.append)
            plan._edit.setPlainText("1. Delivery methods\n2. Yield outcomes")
            plan._on_confirm()
            if not conf or conf[0] != "1. Delivery methods\n2. Yield outcomes":
                fails.append(f"confirm mismatch: {conf}")
            else:
                self._log("Confirm emits the edited plan.", "OK")
            if plan._btn_confirm.isEnabled() or plan._btn_skip.isEnabled() \
                    or plan._edit.isEnabled():
                fails.append("card must fully lock after confirm (no duplicate send)")
            else:
                self._log("Card fully locks after confirm.", "OK")
            plan._on_skip()
            plan._on_confirm()
            if len(conf) != 1:
                fails.append("decisions after lock must be ignored")
            else:
                self._log("Decisions after lock are ignored.", "OK")
            plan.close()
            plan.deleteLater()

            plan2 = DeepPlanCardWidget({"query": query, "sub_tasks": sub_tasks})
            skip = []
            plan2.sig_skip.connect(skip.append)
            plan2._edit.setPlainText("user-edited plan")
            plan2._on_restore()
            restored = plan2._edit.toPlainText()
            if "Delivery methods" not in restored or "user-edited" in restored:
                fails.append(f"restore did not reset to the original plan: {restored!r}")
            else:
                self._log("Restore resets to the original plan.", "OK")
            plan2._on_skip()
            if not skip or skip[0] != query:
                fails.append(f"skip mismatch: {skip}")
            else:
                self._log("Skip emits the original query.", "OK")
            plan2.close()
            plan2.deleteLater()
        except Exception as e:
            fails.append(f"widget level: {e}")

        # --- Part B: 渲染链路级（标记 -> 气泡卡片 -> 信号转发） ---
        try:
            encoded = base64.b64encode(
                json.dumps({"query": query, "sub_tasks": sub_tasks}).encode("utf-8")
            ).decode("ascii")
            marker = f'<deep_plan data="{encoded}"></deep_plan>'

            bubble = ChatBubbleWidget("", is_user=False, index=0)
            confirmed, skipped = [], []
            bubble.sig_deep_plan_confirm.connect(confirmed.append)
            bubble.sig_deep_plan_skip.connect(skipped.append)
            bubble.set_content(f"Before {marker} after")

            cards = bubble.findChildren(DeepPlanCardWidget)
            if len(cards) != 1:
                fails.append(f"render chain: expected exactly 1 card, got {len(cards)}")
            else:
                self._log("Marker rendered as exactly one DeepPlanCard.", "OK")
            body = bubble.original_text
            if "<deep_plan" in body or "Before" not in body or "after" not in body:
                fails.append(f"marker not stripped from body cleanly: {body!r}")
            else:
                self._log("Marker stripped; body text preserved.", "OK")

            if cards:
                cards[0]._edit.setPlainText("1. Delivery methods")
                cards[0]._on_confirm()
                if not confirmed or confirmed[0] != "1. Delivery methods":
                    fails.append(f"bubble signal forwarding (confirm) mismatch: {confirmed}")
                else:
                    self._log("Confirm forwards through the bubble signal.", "OK")

            bubble.set_content(marker)  # 同一 query 重复到达 -> 必须去重
            if len(bubble.findChildren(DeepPlanCardWidget)) != 1:
                fails.append("duplicate marker must be deduped by query key")
            else:
                self._log("Duplicate marker deduped.", "OK")
            bubble.close()
            bubble.deleteLater()

            bad = ChatBubbleWidget("", is_user=False, index=0)
            bad.set_content('Body <deep_plan data="!!!not-base64!!!"></deep_plan> tail')
            if bad.findChildren(DeepPlanCardWidget):
                fails.append("malformed payload must not render a card")
            elif "Body" not in bad.original_text or "tail" not in bad.original_text:
                fails.append(f"malformed marker must be dropped cleanly: {bad.original_text!r}")
            else:
                self._log("Malformed payload dropped without side effects.", "OK")
            bad.close()
            bad.deleteLater()
        except Exception as e:
            fails.append(f"render chain: {e}")

        if fails:
            for msg in fails:
                self._log(msg, "FAIL")
            self._log("Deep plan card test FAILED.", "FAIL")
        else:
            self._log("Deep plan card test passed.", "OK")

    def _test_hitl_pipeline(self, clear: bool = True):
        """Functional test for the human-in-the-loop pipeline (no AI, no network).

        Validates, in order:
          * registration - ask_user schema present in runtime._ALWAYS_TOOLS
          * runtime      - AgentRuntime._handle_ask_user emits an
                           ``<ask_user data=...>`` marker whose base64 JSON
                           payload round-trips; empty question rejected
          * tasks        - _strip_interactive_markers removes card markers;
                           _parse_confirmed_deep_plan / skipped sentinel;
                           _deep_plan_waiting_text localization
          * cards        - AskUserCardWidget (single/multi select + free text)
                           signals behave as wired into the send pipeline
                           (DeepPlanCardWidget has its own dedicated test:
                           the "Deep Plan Card" button, which also covers the
                           marker -> bubble render chain)
        """
        if clear:
            self._clear()
        self._log("--- HITL Pipeline (ask_user / deep_plan) ---", "INFO")
        fails = []

        # 1) 工具注册：ask_user 出现在常驻工具池且参数齐全
        try:
            from src.core.agent import runtime as agent_runtime
            schema = agent_runtime._ALWAYS_TOOLS.get("ask_user")
            params = (schema or {}).get("function", {}).get("parameters", {})
            props = params.get("properties", {})
            if "question" not in props or "options" not in props:
                fails.append("ask_user missing from runtime._ALWAYS_TOOLS "
                             "(or no question/options params)")
            else:
                self._log("ask_user registered with question/options/multi_select.", "OK")
        except Exception as e:
            fails.append(f"tool registration: {e}")

        # 2) runtime._handle_ask_user：标记流出与 base64 载荷往返
        try:
            from src.core.agent.runtime import AgentRuntime
            rt = AgentRuntime.__new__(AgentRuntime)
            rt.log_fn = lambda *a, **k: None
            emitted = []
            args = {"question": "Which species?", "options": ["Arabidopsis", "Rice"],
                    "multi_select": False, "context": "ID mapping differs"}
            ret = json.loads(AgentRuntime._handle_ask_user(rt, args, emitted.append))
            if ret.get("status") != "success":
                fails.append(f"_handle_ask_user returned status={ret.get('status')}")
            marker = next((t for t in emitted if t.startswith("<ask_user")), "")
            m = re.search(r'<ask_user data="([^"]*)"', marker)
            if not m:
                fails.append("no <ask_user data=...> marker emitted")
            else:
                payload = json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
                if (payload.get("question") != "Which species?"
                        or payload.get("options") != ["Arabidopsis", "Rice"]):
                    fails.append("ask_user payload round-trip mismatch")
                else:
                    self._log("ask_user marker emitted; payload round-trip OK.", "OK")
            ret2 = json.loads(AgentRuntime._handle_ask_user(rt, {"question": "  "}, None))
            if ret2.get("status") != "error":
                fails.append("empty question should return status=error")
            else:
                self._log("Empty question correctly rejected.", "OK")
        except Exception as e:
            fails.append(f"runtime handler: {e}")

        # 3) 任务层：哨兵解析 / 卡片标记剥离 / 等待文案
        try:
            from src.task import chat_tasks as ct
            from src.task.chat_tasks import ChatGenerationTask

            class _Stub:
                pass

            stub = _Stub()
            sentinels = (ct._DEEP_PLAN_CONFIRMED_TAG, ct._DEEP_PLAN_SKIPPED_TAG)
            if not all(s.startswith("[") and s.endswith("]") for s in sentinels):
                fails.append(f"deep-plan sentinels malformed: {sentinels}")

            tasks = ChatGenerationTask._parse_confirmed_deep_plan(
                stub, f"{ct._DEEP_PLAN_CONFIRMED_TAG}\n"
                      "1. Delivery methods\n2) Regulatory landscape\n- Yield outcomes")
            want = ["Delivery methods", "Regulatory landscape", "Yield outcomes"]
            if tasks is None or [t.query for t in tasks] != want:
                fails.append(f"_parse_confirmed_deep_plan mismatch: {tasks}")
            else:
                self._log("Confirmed-plan sentinel parsed; numbering stripped.", "OK")
            if ChatGenerationTask._parse_confirmed_deep_plan(
                    stub, "a normal user question") is not None:
                fails.append("non-sentinel text must return None from plan parser")

            dirty = ('before <ask_user data="QUJD"></ask_user> mid '
                     '<deep_plan data="WFla"></deep_plan> after')
            clean = ChatGenerationTask._strip_interactive_markers(dirty)
            if any(t in clean for t in ("ask_user", "deep_plan")) or \
                    not (clean.startswith("before") and clean.endswith("after")):
                fails.append(f"_strip_interactive_markers mismatch: {clean!r}")
            else:
                self._log("Interactive markers stripped before LLM context.", "OK")

            wait_en = ChatGenerationTask._deep_plan_waiting_text(stub)
            stub.reply_lang = "Chinese"
            wait_zh = ChatGenerationTask._deep_plan_waiting_text(stub)
            if "Confirm" not in wait_en or "确认" not in wait_zh:
                fails.append("_deep_plan_waiting_text localization mismatch")
            else:
                self._log("Plan waiting text localized (EN/ZH).", "OK")
        except Exception as e:
            fails.append(f"sentinel/strip: {e}")

        # 4) 卡片组件：选项逻辑与确认/跳过信号（QApplication 内直接实例化）
        try:
            from src.ui.components.ask_user_card import AskUserCardWidget

            # --- 单选：预设选项提交 + 提交后整卡锁定 ---
            card = AskUserCardWidget({"question": "Species?",
                                      "options": ["Arabidopsis", "Rice"],
                                      "multi_select": False})
            answers = []
            card.sig_submit.connect(answers.append)
            radios = card.findChildren(QRadioButton)
            if len(radios) != 3:  # 2 预设 + 1 "My own answer"
                fails.append(f"expected 3 radio options, got {len(radios)}")
            else:
                if card._edit.isEnabled():
                    fails.append("free-text edit must stay disabled until 'My own answer' is selected")
                if card._btn_submit.isEnabled():
                    fails.append("submit must stay disabled before any selection")
                radios[1].setChecked(True)
                if not card._btn_submit.isEnabled():
                    fails.append("submit must enable after selecting a preset option")
                card._on_submit()
                if not answers or answers[0] != "Rice":
                    fails.append(f"single-select submit mismatch: {answers}")
                else:
                    self._log("AskUserCard single-select preset submit OK.", "OK")
                if card._btn_submit.isEnabled():
                    fails.append("card must lock after submit (no duplicate send)")
                card._on_submit()
                if len(answers) != 1:
                    fails.append("resubmit after lock must be ignored by AskUserCard")
                else:
                    self._log("AskUserCard locks after submit OK.", "OK")
            card.close()
            card.deleteLater()

            # --- 单选：选中 "My own answer" 才能输入自由文本 ---
            own_card = AskUserCardWidget({"question": "Species?",
                                          "options": ["Arabidopsis", "Rice"],
                                          "multi_select": False})
            own_ans = []
            own_card.sig_submit.connect(own_ans.append)
            own_radios = own_card.findChildren(QRadioButton)
            own_radios[2].setChecked(True)  # "My own answer"
            if not own_card._edit.isEnabled():
                fails.append("'My own answer' must enable the free-text edit")
            if own_card._btn_submit.isEnabled():
                fails.append("own answer without text must keep submit disabled")
            own_card._edit.setPlainText("AT1G63700, please expand")
            if not own_card._btn_submit.isEnabled():
                fails.append("submit must enable with non-empty own answer")
            own_card._on_submit()
            if not own_ans or own_ans[0] != "AT1G63700, please expand":
                fails.append(f"own-answer submit mismatch: {own_ans}")
            else:
                self._log("AskUserCard own-answer submit OK.", "OK")
            own_card.close()
            own_card.deleteLater()

            # --- 多选：预设选项组合提交 ---
            multi = AskUserCardWidget({"question": "Which omics layers?",
                                       "options": ["Transcriptomics", "Proteomics"],
                                       "multi_select": True})
            m_ans = []
            multi.sig_submit.connect(m_ans.append)
            boxes = multi.findChildren(QCheckBox)
            if len(boxes) != 3:  # 2 预设 + 1 "My own answer"
                fails.append(f"expected 3 checkboxes, got {len(boxes)}")
            else:
                boxes[0].setChecked(True)
                boxes[1].setChecked(True)
                multi._on_submit()
                if not m_ans or m_ans[0] != "Transcriptomics; Proteomics":
                    fails.append(f"multi-select join mismatch: {m_ans}")
                else:
                    self._log("AskUserCard multi-select join OK.", "OK")
            multi.close()
            multi.deleteLater()

            # --- 多选："My own answer" 与预设选项互斥 ---
            multi2 = AskUserCardWidget({"question": "Which omics layers?",
                                        "options": ["Transcriptomics", "Proteomics"],
                                        "multi_select": True})
            m2_boxes = multi2.findChildren(QCheckBox)
            m2_boxes[0].setChecked(True)
            m2_boxes[2].setChecked(True)  # "My own answer"
            if m2_boxes[0].isChecked():
                fails.append("'My own answer' must deselect preset options (mutual exclusion)")
            if not multi2._edit.isEnabled():
                fails.append("'My own answer' must enable the free-text edit (multi-select)")
            else:
                self._log("AskUserCard own-answer mutual exclusion OK.", "OK")
            multi2.close()
            multi2.deleteLater()
        except Exception as e:
            fails.append(f"card widgets: {e}")

        if fails:
            for msg in fails:
                self._log(msg, "FAIL")
            self._log("HITL pipeline test FAILED.", "FAIL")
        else:
            self._log("HITL pipeline test passed.", "OK")

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
            "src/core/image_utils.py",
            # 文件类型知识 + 查看器拆分后的模块（pdf_viewer / text_viewer /
            # document_search 三者必须同时可解析，缺一即为拆分回归）
            "src/core/file_types.py",
            "src/ui/components/document_search.py",
            "src/ui/components/pdf_viewer.py",
            "src/ui/components/text_viewer.py",
            "src/ui/components/text_formatter.py",
            "src/tools/chat_input_widgets.py",
            "src/tools/chat_mixins/attachments.py",
            "src/ui/components/ask_user_card.py",
            "src/ui/components/deep_plan_card.py",
            "src/ui/components/chat_bubble.py",
            "src/ui/components/dialog.py",
            "src/ui/components/developer_dialog.py",
            "src/ui/components/image_viewer.py",
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

    # ------------------------------------------------------------------ #
    #  Attachment / reader chain self-checks (pure functions, no windows)
    # ------------------------------------------------------------------ #
    def _test_file_types(self, clear: bool = True):
        """文件类型知识自检（``core.file_types``）。

        "拓宽可上传类型"是结构性改动：链接路由（cite:// 交给谁打开）、上传过滤
        （选择器 / 拖拽 / 粘贴）与查看器渲染三处读的是同一份集合。集合关系一旦
        改坏，症状只落在个别格式上（"能上传却按纯文本显示""能打开却选不到文件"），
        逐条断言不变量比逐个格式盲试更快定位。
        """
        if clear:
            self._clear()
        self._log("--- File Types (viewer / markdown / uploadable sets) ---", "INFO")
        try:
            from src.core import file_types as ft
        except Exception as e:
            self._log(f"file_types import failed: {e}", "FAIL")
            return

        checks = [
            ("MARKDOWN_EXTS ⊆ TEXT_VIEWER_EXTS",
             ft.MARKDOWN_EXTS <= ft.TEXT_VIEWER_EXTS),
            ("DOCUMENT_EXTS ∩ TEXT_VIEWER_EXTS = ∅",
             not (ft.DOCUMENT_EXTS & ft.TEXT_VIEWER_EXTS)),
            ("IMAGE_EXTS ⊆ ATTACHABLE_EXTS", ft.IMAGE_EXTS <= ft.ATTACHABLE_EXTS),
            ("ATTACHABLE_EXTS = viewer ∪ document ∪ image",
             ft.ATTACHABLE_EXTS == (ft.TEXT_VIEWER_EXTS | ft.DOCUMENT_EXTS | ft.IMAGE_EXTS)),
            ("md -> markdown + viewable + attachable",
             ft.is_markdown("a.md") and ft.is_text_viewable("A.MD")
             and ft.is_attachable("a.md")),
            ("txt -> plain but viewable + attachable",
             (not ft.is_markdown("notes.txt")) and ft.is_text_viewable("notes.txt")
             and ft.is_attachable("notes.txt")),
            ("yaml/yml/toml/xml/json -> viewable + attachable",
             all(ft.is_text_viewable(n) and ft.is_attachable(n) for n in
                 ("conf.yaml", "conf.yml", "Cargo.toml", "feed.xml", "data.JSON"))),
            ("csv/tsv/log -> viewable",
             all(ft.is_text_viewable(n) for n in ("x.csv", "x.tsv", "run.log"))),
            ("pdf/docx -> attachable, not text-viewable",
             all(ft.is_attachable(n) and not ft.is_text_viewable(n)
                 for n in ("report.pdf", "doc.docx"))),
            ("images -> attachable, not text-viewable",
             ft.is_attachable("pic.PNG") and ft.is_attachable("icon.svg")
             and not ft.is_text_viewable("pic.PNG")),
            ("zip/exe/extension-less -> rejected",
             not any(ft.is_attachable(n) for n in ("archive.zip", "app.exe", "README"))),
            ("file_extension() handles case, dotted dirs, empty input",
             ft.file_extension("/tmp/dir.with.dots/Y.Toml") == "toml"
             and ft.file_extension("") == "" and ft.file_extension(None) == ""),
        ]
        fails = [name for name, ok in checks if not ok]
        for name, ok in checks:
            self._log(f"{name}: {'OK' if ok else 'FAIL'}", "OK" if ok else "FAIL")
        self._log(f"viewer-visible={len(ft.TEXT_VIEWER_EXTS)} "
                  f"attachables={len(ft.ATTACHABLE_EXTS)}", "INFO")
        self._log("File type invariants OK." if not fails else
                  f"File type invariants FAILED: {', '.join(fails)}",
                  "OK" if not fails else "FAIL")

    def _test_text_viewer_render(self, clear: bool = True):
        """文本查看器：扩展名分派 + 三种渲染器的回归自检。

        只调用纯函数（``_resolve_render_mode`` / ``_render_markdown`` /
        ``_render_highlighted`` / ``_plain_text_to_html``），**不创建窗口**：
        自检面板不该弹出额外窗口，也不该依赖用户当前所在的面板。
        """
        if clear:
            self._clear()
        self._log("--- Text Viewer (extension -> render mode) ---", "INFO")
        try:
            from src.ui.components.text_viewer import InternalTextViewer
        except Exception as e:
            self._log(f"text_viewer import failed: {e}", "FAIL")
            return

        try:
            import pygments  # noqa: F401
            pygments_ok = True
        except ImportError:
            pygments_ok = False
            self._log("Pygments not installed: highlight assertions skipped.", "WARN")

        fails = []

        def record(name, ok):
            self._log(f"{name}: {'OK' if ok else 'FAIL'}", "OK" if ok else "FAIL")
            if not ok:
                fails.append(name)

        # 1) 渲染模式分派（与 _render_content 共用同一份判定）
        mode_cases = [("demo.md", "markdown"), ("notes.txt", "plain"),
                      ("unknown.zzz", "plain")]
        if pygments_ok:
            mode_cases += [("conf.yaml", "highlight"), ("Cargo.toml", "highlight"),
                           ("feed.xml", "highlight"), ("data.json", "highlight"),
                           ("script.py", "highlight")]
        for name, want in mode_cases:
            ext = name.rsplit(".", 1)[-1].lower()
            got = InternalTextViewer._resolve_render_mode(ext, name)
            record(f"mode {name} -> {got} (want {want})", got == want)

        # 2) .txt：不解释 Markdown，但转义 HTML、URL 可点击
        plain_html = InternalTextViewer._plain_text_to_html(
            "# not a heading\n<b>raw</b> tag\nsee https://example.org/x\n")
        record("plain keeps '#' literal",
               "# not a heading" in plain_html and "<h1" not in plain_html)
        record("plain escapes HTML", "&lt;b&gt;raw&lt;/b&gt;" in plain_html)
        record("plain linkifies URL", 'href="https://example.org/x"' in plain_html)

        # 3) Markdown：走项目内渲染管线
        md_html = InternalTextViewer._render_markdown(
            "# Heading One\n\n| a | b |\n|:--|:--|\n| 1 | 2 |\n\n"
            "```python\nprint(1)\n```\n")
        record("markdown heading rendered", "<h1" in md_html)
        record("markdown table rendered", "<table" in md_html)
        record("markdown code block rendered", "<pre" in md_html)
        record("markdown fence consumed", "```" not in md_html)

        # 4) 语法高亮：内联样式 + 主题代码底色；.txt 不该套高亮外观
        record(".txt not highlighted",
               InternalTextViewer._render_highlighted("a line\n", "notes.txt") is None)
        if pygments_ok:
            hl_html = InternalTextViewer._render_highlighted(
                "key: 1\nlist:\n  - a\n", "conf.yaml")
            record("yaml highlighted to HTML", bool(hl_html) and "<pre" in hl_html)
            record("highlight uses inline styles", 'style="color:' in (hl_html or ""))
            record("highlight themed code bg",
                   ThemeManager().color('code_bg') in (hl_html or ""))
        else:
            self._log("yaml/json/toml/xml highlighting not verified (Pygments missing).", "WARN")

        self._log("Text viewer render dispatch OK." if not fails else
                  f"Text viewer checks FAILED: {', '.join(fails)}",
                  "OK" if not fails else "FAIL")

    def _test_scrollbar_theme(self, clear: bool = True):
        """滚动条可见性 + 拆分块滚动范围自检。

        用户可见的缺陷有两类，都在这里盯住：

        1. **滑块与轨道（底色条）分不出**：早期常态 alpha=0（只有鼠标落进 8px 窄条
           才显形）；0.4 → 0.6 都仍不够——取用户截图实测，浅色主题下轨道 #919191、
           滑块 #777777，**对比度只有 1.3:1**。现代码滑块取**不透明**的主题
           ``text_muted``（对比度 3.5~4.3:1），悬停/拖动切强调色；``idle_alpha=0``
           仍能回退旧行为，``groove_alpha=0`` 可得到无轨道外观。这里既核对 QSS
           文本，也**渲染实况**量一次滑块与轨道的实际像素差（只信文本会漏掉
           "滑块被画满整条"这类绘制问题，那正是上一轮的坑）。
        2. **没溢出却能滚**：容器自身的 QSS 边框/内边距会占掉视口高度，早期块高
           计算没算这部分，于是每个块都留着几像素滚动余量——AsNeeded 策略下还会
           凭这点差值弹出一条多余滚动条。这里直接量 ``ScrollBar.maximum()``。
        另外顺带核对应用样式表里的 QToolTip 覆盖（提示条必须按主题取色）。
        """
        if clear:
            self._clear()
        self._log("--- Overlay Scrollbar Visibility ---", "INFO")
        try:
            from src.core.theme_manager import (ThemeManager, hex_to_rgba,
                                                overlay_scrollbar_qss)
        except Exception as e:
            self._log(f"theme_manager import failed: {e}", "FAIL")
            return

        tm = ThemeManager()
        qss = overlay_scrollbar_qss(thickness=8)
        # rgba 出现顺序：轨道底色 → 滑块常态（悬停/按压已改为强调色，不再用 alpha）
        alphas = [float(a) for a in re.findall(r"rgba\([^)]*,\s*([0-9.]+)\)", qss)]
        groove, idle = (alphas + [0.0] * 2)[:2]
        accent, accent_hover = tm.color('accent'), tm.color('accent_hover')

        idle_block = re.search(
            r"QScrollBar::handle:vertical, QScrollBar::handle:horizontal \{(.*?)\}",
            qss, re.S)
        idle_body = idle_block.group(1) if idle_block else ""

        tooltip_block = re.search(r"QToolTip \{(.*?)\}", tm.get_custom_qss(), re.S)
        tooltip_body = tooltip_block.group(1) if tooltip_block else ""

        # 拆分块的滚动范围：不溢出的块必须恰好贴合内容（maximum == 0），
        # 真正超长的代码块仍然要能滚。
        block_ranges, tall_code_scrolls = {}, False
        handle_hex, track_hex, contrast_ok, track_neutral = "", "", False, False
        try:
            from PySide6.QtCore import QPoint, Qt
            from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

            from src.ui.components.chat_bubble import ChatBubbleWidget

            host = QWidget()
            # WA_DontShowOnScreen：按"已显示"结算布局与滚动条几何，但不会真的
            # 弹窗——开发模式点这个按钮不该闪出一个 760x900 的窗口；而完全不
            # show() 的话控件内部几何不结算（视口还停在 640x480 默认值），
            # 量出来的滚动范围全是假值。
            host.setAttribute(Qt.WA_DontShowOnScreen, True)
            host_layout = QVBoxLayout(host)
            host_layout.setContentsMargins(8, 8, 8, 8)
            bubble = ChatBubbleWidget("", is_user=False, index=0)
            host_layout.addWidget(bubble)
            host_layout.addStretch()
            host.resize(760, 900)
            host.show()
            bubble.set_content(
                "Intro paragraph.\n\n"
                "| a | b |\n|:--|:--|\n| 1 | 2 |\n\n"
                "```python\ndef f():\n    return 1\n```\n")
            bubble.force_resync_height()
            for _ in range(8):
                QApplication.processEvents()
            block_ranges = {
                b._block_kind: (b.verticalScrollBar().maximum(),
                                b.horizontalScrollBar().maximum())
                for b in bubble._extra_blocks}

            bubble.set_content("```python\n" + "\n".join(
                f"line_{i} = {i}" for i in range(40)) + "\n```\n")
            bubble.force_resync_height()
            for _ in range(8):
                QApplication.processEvents()
            tall = bubble._extra_blocks[0]
            # 只断言"滚动范围 > 0"：本自检不显示窗口（避免闪窗），而隐藏状态下的
            # isVisible() 恒为 False，用它会得到假失败。
            tall_range = (tall.height(), tall.viewport().height(),
                          tall.verticalScrollBar().maximum())
            tall_code_scrolls = tall.verticalScrollBar().maximum() > 0

            # 渲染实况：滑块与轨道要真的分得出来。内容停在顶部，所以滚动条中线上
            # "顶部一点"在滑块内、"底部一点"在轨道内。通道差之和 ≥ 40 ≈ 肉眼明显
            # 可辨；滑块被画满整条（或两者同色）时这个值会趋近 0。
            sb = tall.verticalScrollBar()
            shot = host.grab().toImage()
            sx = sb.mapTo(host, QPoint(sb.width() // 2, 0)).x()
            sy = sb.mapTo(host, QPoint(0, 0)).y()
            handle_px = shot.pixelColor(sx, sy + 12)
            track_px = shot.pixelColor(sx, sy + sb.height() - 12)
            handle_hex, track_hex = handle_px.name(), track_px.name()
            contrast_ok = (abs(handle_px.red() - track_px.red())
                           + abs(handle_px.green() - track_px.green())
                           + abs(handle_px.blue() - track_px.blue())) >= 40
            # 轨道必须是中性灰：被 QScrollBar:hover::handle 涂上的强调色是"彩"的
            track_neutral = (max(track_px.red(), track_px.green(), track_px.blue())
                             - min(track_px.red(), track_px.green(),
                                   track_px.blue())) <= 12

            host.hide()
            host.deleteLater()
        except Exception as e:
            self._log(f"block scroll-range probe raised {e}", "FAIL")

        checks = [
            ("idle handle is opaque (max contrast vs track)", abs(idle - 1.0) < 1e-6),
            ("groove makes the track visible (alpha > 0)", groove > 0),
            ("groove stays lighter than the idle handle", groove < idle),
            ("idle handle is not transparent", "transparent" not in idle_body),
            ("idle color derives from theme text_muted",
             hex_to_rgba(tm.color('text_muted'), idle) in qss),
            ("hover/pressed switch to accent (distinct cue)",
             accent in qss and accent_hover in qss
             and accent != tm.color('text_muted')),
            ("idle_alpha=0 still restores a hidden handle",
             re.search(r"rgba\([^)]*,\s*0\.0\)",
                       overlay_scrollbar_qss(idle_alpha=0)) is not None),
            ("groove_alpha=0 hides the track",
             re.findall(r"rgba\([^)]*,\s*([0-9.]+)\)",
                        overlay_scrollbar_qss(groove_alpha=0))[:1] == ["0.0"]),
            ("track/arrows stay non-occupying",
             "QScrollBar::add-line, QScrollBar::sub-line" in qss and "height: 0px" in qss),
            ("overlay look kept (rounded + sized)",
             "border-radius: 4px" in qss and "height: 8px" in qss),
            (f"no phantom scroll range on fitted blocks ({block_ranges})",
             bool(block_ranges) and all(v == 0 and h == 0
                                        for v, h in block_ranges.values())),
            (f"an over-long code block still scrolls (h/vp/range={tall_range})",
             tall_code_scrolls),
            (f"rendered handle vs track visibly differ ({handle_hex} vs {track_hex})",
             contrast_ok),
            (f"track stays a neutral groove ({track_hex})", track_neutral),
            ("no QScrollBar:hover::handle rule (Qt paints it on the track)",
             "QScrollBar:hover::handle" not in re.sub(r"/\*.*?\*/", "", qss, flags=re.S)),
            ("tooltip colors come from the app stylesheet (theme-aware)",
             "background-color" in tooltip_body
             and tm.color('bg_card') in tooltip_body
             and tm.color('text_main') in tooltip_body),
            ("app stylesheet reuses the single scrollbar spec (no duplicate rules)",
             tm.get_custom_qss().count(
                 "QScrollBar::handle:vertical, QScrollBar::handle:horizontal") == 1),
        ]
        fails = [name for name, ok in checks if not ok]
        for name, ok in checks:
            self._log(f"{name}: {'OK' if ok else 'FAIL'}", "OK" if ok else "FAIL")
        self._log(f"scrollbar: groove_alpha={groove} idle_alpha={idle} "
                  f"handle/track={handle_hex}/{track_hex} accent={accent}", "INFO")
        self._log("Scrollbar visibility OK." if not fails else
                  f"Scrollbar checks FAILED: {', '.join(fails)}",
                  "OK" if not fails else "FAIL")

    def _test_attachment_wiring(self, clear: bool = True):
        """附件链路接线自检：可上传类型清单 + 输入区芯片的打开 / 移除钩子。

        "输入栏里的附件打不开"曾是真实缺陷：文档附件只以纯文本列出、没有任何
        可点击入口。这条自检盯住两点——(1) 文件选择器确实放开了新格式；
        (2) 输入区信号与 chat 侧的打开 / 移除钩子仍然存在（被重命名即失败）。
        真正的"点芯片打开 / 点 x 移除"需要手动交互，见输出末尾提示。
        """
        if clear:
            self._clear()
        self._log("--- Attachment Wiring (uploadable types + input chips) ---", "INFO")
        fails = []

        def record(name, ok):
            self._log(f"{name}: {'OK' if ok else 'FAIL'}", "OK" if ok else "FAIL")
            if not ok:
                fails.append(name)

        try:
            from src.core.file_types import ATTACHABLE_EXTS, is_attachable
            from src.tools.chat_input_widgets import ChatInputContainer
            from src.tools.chat_mixins.attachments import (ChatAttachmentsMixin,
                                                          _glob_patterns)
        except Exception as e:
            self._log(f"attachment wiring import failed: {e}", "FAIL")
            return

        globs = _glob_patterns(ATTACHABLE_EXTS)
        wanted = ["pdf", "docx", "md", "txt", "json", "yaml", "yml", "toml",
                  "xml", "csv", "png", "svg"]
        missing = [e for e in wanted if f"*.{e}" not in globs]
        record(f"file dialog offers all key types (missing: {missing or 'none'})",
               not missing)
        record("executables / archives still rejected",
               not is_attachable("app.exe") and not is_attachable("archive.zip"))

        def declares(cls, name) -> bool:
            try:
                return getattr(cls, name, None) is not None
            except Exception:
                return False

        record("input container declares file-chip signals",
               declares(ChatInputContainer, "sig_open_file")
               and declares(ChatInputContainer, "sig_remove_file"))
        record("input container exposes set_file_chips",
               callable(getattr(ChatInputContainer, "set_file_chips", None)))
        record("chat mixin exposes open/remove attachment hooks",
               all(callable(getattr(ChatAttachmentsMixin, n, None)) for n in
                   ("open_attachment_file", "remove_attached_file",
                    "_refresh_attachment_preview")))
        self._log("Manual step: attach a .pdf/.json, then click its chip to open "
                  "and 'x' to remove (chips are not covered headlessly).", "INFO")
        self._log("Attachment wiring OK." if not fails else
                  f"Attachment wiring FAILED: {', '.join(fails)}",
                  "OK" if not fails else "FAIL")

    def _test_chat_typography(self, clear: bool = True):
        """聊天气泡排版自检：参数规格、夹取、默认值与**标题层级的自动偏移**。

        这条链路是纯数据驱动的，最容易出的三类问题是：**默认值漂移**（用户
        从不打开设置面板，外观也必须与历史一致）、**越界值落盘**（配置文件被
        手改或面板被绕过）、**层级被拉平**（字号调大后 H1 反而比正文小、各级
        标题段距变成同一个值）。三者都不会报错，只会"看着不对劲"，因此逐条断言。
        全部为纯函数调用，不创建窗口。
        """
        if clear:
            self._clear()
        self._log("--- Chat Typography (bubble font / spacing specs) ---", "INFO")
        try:
            from src.core.chat_typography import (PARAMS, SCOPES, config_key,
                                                  defaults, read, scope_values,
                                                  serialize)
        except Exception as e:
            self._log(f"chat_typography import failed: {e}", "FAIL")
            return

        fails = []

        def record(name, ok):
            self._log(f"{name}: {'OK' if ok else 'FAIL'}", "OK" if ok else "FAIL")
            if not ok:
                fails.append(name)

        base = defaults()
        record(f"defaults cover {len(PARAMS)} params x {len(SCOPES)} scopes",
               len(base) == len(PARAMS) * len(SCOPES) == 10)
        record("config keys use the chat_bubble_ prefix",
               all(k.startswith("chat_bubble_") for k in base)
               and config_key("font_size", "ai") == "chat_bubble_font_size_ai")
        # 默认值必须与改造前的硬编码外观一致（14px / 行距 150% / 段前 4px / 段后 8px）
        record("defaults preserve the historical look",
               base[config_key("font_size", "ai")] == 14.0
               and base[config_key("font_size", "user")] == 14.0
               and base[config_key("line_height", "ai")] == 150.0
               and base[config_key("space_before", "ai")] == 4.0
               and base[config_key("space_after", "ai")] == 8.0
               and base[config_key("letter_spacing", "ai")] == 0.0)

        record("empty settings -> defaults", read({}) == base)
        oversize = read({config_key("font_size", "ai"): 999})
        record("out-of-range values are clamped",
               oversize[config_key("font_size", "ai")] == 28.0)
        junk = read({config_key("font_size", "ai"): "huge"})
        record("non-numeric values fall back to the default",
               junk[config_key("font_size", "ai")] == 14.0)
        # 落盘精度：整数参数存 int，小数参数保留声明的小数位
        record("serialize() keeps config tidy",
               serialize(PARAMS[0], 16.4) == 16 and serialize(PARAMS[1], 0.2612) == 0.26)

        scoped = scope_values(read({}), "user")
        record("scope_values() exposes named params",
               set(scoped) == {p.name for p in PARAMS} and scoped["font_size"] == 14.0)
        record("scope_values() tolerates a partial draft",
               scope_values({}, "ai")["font_size"] == 14.0)

        # 标题层级的**自动偏移**：面板只给正文字参，H1–H6 的大小与段距必须自动派生，
        # 且层级关系在任何设置下都不能被拉平（否则"调到 21px 以上 h1 比正文小"）。
        try:
            from src.core.chat_typography import heading_metrics
            from src.ui.components.text_formatter import (TextFormatter,
                                                          _BASE_FONT_PX)
            record("heading baseline follows the typography default",
                   float(_BASE_FONT_PX) == base[config_key("font_size", "ai")])

            metrics = [heading_metrics(level) for level in (1, 2, 3, 4, 5, 6)]
            sizes = [m[0] for m in metrics]
            tops = [m[1] for m in metrics]
            record("heading size ladder matches the historical px values",
                   sizes == [21, 18, 16, 15, 14, 13])
            record("heading sizes are strictly descending",
                   all(a > b for a, b in zip(sizes, sizes[1:])))
            record("heading spacing offsets are strictly per level",
                   tops == [12, 10, 8, 7, 6, 5]
                   and all(a > b for a, b in zip(tops, tops[1:]))
                   and all(t > 0 for t in tops))

            big = [heading_metrics(level, base_font_px=28, space_before=16)
                   for level in (1, 2, 3)]
            record("heading offsets scale with the body parameters",
                   big[0][0] == 42 and big[0][1] == 48
                   and all(a[0] > b[0] for a, b in zip(big, big[1:]))
                   and all(a[1] > b[1] for a, b in zip(big, big[1:])))

            default = TextFormatter.markdown_to_html("# H1\n")
            scaled = TextFormatter.markdown_to_html("# H1\n", base_font_px=28)
            spaced = TextFormatter.markdown_to_html(
                "# H1\n", space_before=16, space_after=24)
            record("h1 scales with the base font size",
                   "font-size:42px" in scaled and "font-size:21px" in default)
            record("h1 keeps its historical spacing by default",
                   "margin-top:12px" in default and "margin-bottom:4px" in default)
            record("markdown_to_html applies the heading spacing params",
                   "margin-top:48px" in spaced and "margin-bottom:12px" in spaced)
        except Exception as e:
            record(f"heading offset check raised {e}", False)

        # 排版参数必须真的落到**文档与 HTML** 上——这是最容易静默失效的一环：
        # 早期实现里 setLineHeight 传的是枚举对象，而 PySide6 6.10 起该形参按 int
        # 校验，TypeError 被方法内的 except 吞进 debug 日志，于是"行距 + 段距"
        # 整体失效：滑块怎么拖都没反应，日志里连一行痕迹都没有。因此这里用探针
        # 气泡走一遍真实链路，把结果断言成可检查的块格式与 HTML。
        try:
            from PySide6.QtGui import QTextBlockFormat

            from src.core.chat_typography import config_key as tkey
            from src.ui.components.chat_bubble import ChatBubbleWidget
            from src.ui.components.dialogs import chat_typography_dialog as ctd

            probe = ChatBubbleWidget("", is_user=False, index=0)
            try:
                probe.set_content(TextFormatter.format_response(
                    "First paragraph, long enough to wrap at least twice in a "
                    "narrow bubble.\n\nSecond paragraph.", 0, set(), set(), {}))

                probe.refresh_typography({tkey("line_height", "ai"): 260.0})
                fmt = probe.lbl_text.document().begin().blockFormat()
                # lineHeightType() 返回 int，而 LineHeightTypes 是纯 Enum（枚举成员
                # 与 int 不相等），比较一律取 .value。
                prop_height = QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
                record("line spacing reaches the document (block line height)",
                       abs(fmt.lineHeight() - 260.0) < 1e-6
                       and fmt.lineHeightType() == prop_height)

                # 段距必须**可上可下**：Qt 给 <p> 的内置默认段距是上下各 12px，
                # 靠块格式"取较大值"补的话，12px 以下怎么调都没反应——正是
                # "Space after 小于一定值后再修改就没用了"那个现象。
                probe.refresh_typography({tkey("space_before", "ai"): 0.0,
                                          tkey("space_after", "ai"): 0.0})
                tightened = probe.lbl_text.document().begin().blockFormat()
                record("paragraph spacing can be tightened to 0",
                       tightened.topMargin() == 0.0
                       and tightened.bottomMargin() == 0.0)

                probe.refresh_typography({tkey("space_before", "ai"): 21.0,
                                          tkey("space_after", "ai"): 40.0})
                widened = probe.lbl_text.document().begin().blockFormat()
                record("paragraph spacing can be widened past Qt's default",
                       widened.topMargin() == 21.0
                       and widened.bottomMargin() == 40.0)
            finally:
                probe.deleteLater()

            spaced = TextFormatter.markdown_to_html("one\n\ntwo\n",
                                                    space_before=0, space_after=40)
            untouched = TextFormatter.markdown_to_html("one\n\ntwo\n")
            record("body paragraph margins are injected only when params are given",
                   'margin-top:0px; margin-bottom:40px;' in spaced
                   and 'margin-bottom:40px' not in untouched)

            # 面板宽度必须放得下参数行：滑块与数值框都是固定宽度，声明宽度小于
            # 布局最小宽度时 Qt 只会挤压参数行、让控件溢出各自单元格。
            panel = ctd.ChatTypographyDialog()
            try:
                record("typography panel is wide enough for its controls",
                       panel.content_widget.minimumSizeHint().width()
                       <= ctd._PANEL_WIDTH)
            finally:
                panel.deleteLater()
        except Exception as e:
            record(f"typography application check raised {e}", False)

        # 已保存后能就地生效的接线：气泡重排方法 + 广播信号
        try:
            from src.core.signals import GlobalSignals
            from src.ui.components.chat_bubble import ChatBubbleWidget
            record("bubbles expose refresh_typography()",
                   callable(getattr(ChatBubbleWidget, "refresh_typography", None)))
            record("chat_typography_changed signal exists",
                   getattr(GlobalSignals, "chat_typography_changed", None) is not None)
        except Exception as e:
            record(f"typography wiring check raised {e}", False)

        # 设置面板的预览必须是**生产链路**：同一个气泡组件 + 同一个渲染入口 +
        # 同一个"回源重渲染"契约（气泡把重排委托给宿主）。三者任一被换成简化实现，
        # 预览就会与聊天页偷偷分叉——这正是最容易发生、又最难肉眼发现的问题。
        try:
            from src.ui.components.chat_bubble import ChatBubbleWidget
            from src.ui.components.dialogs import chat_typography_dialog as ctd
            from src.ui.components.text_formatter import TextFormatter

            record("preview reuses the production bubble widget",
                   ctd.ChatBubbleWidget is ChatBubbleWidget)
            record("preview implements the ChatTool re-render contract",
                   callable(getattr(ctd.ChatTypographyDialog,
                                    "rerender_bubble_from_source", None))
                   and set(ctd._PREVIEW_SOURCES) == {0, 1})
            record("preview renders through TextFormatter.format_response",
                   ctd.ChatTypographyDialog._format_preview(ctd._PREVIEW_AI_TEXT, 0)
                   == TextFormatter.format_response(ctd._PREVIEW_AI_TEXT, 0,
                                                    set(), set(), {}))
        except Exception as e:
            record(f"preview fidelity check raised {e}", False)

        # 组件在**任意宿主容器**里都必须保持同一外观。气泡正文是 QTextBrowser
        # （QTextEdit 的子类），宿主样式表里的通用输入框规则会以"特异性相同时祖先
        # 优先"命中它——表现为正文外凭空多出一层带边框与内边距的底纹；滚动块同理
        # 会被宿主的通用 QScrollBar 规则换掉。因此这些控件必须用**带 id 的选择器**，
        # 且选择器与 objectName 必须对得上（对不上就会静默退回被覆盖状态）。
        try:
            from PySide6.QtCore import QPoint
            from PySide6.QtGui import QPalette
            from PySide6.QtWidgets import QApplication

            from src.ui.components import chat_bubble as cb

            probe = cb.ChatBubbleWidget("scope probe", is_user=False, index=0)
            try:
                record("text browser carries its id + id-scoped QSS",
                       probe.lbl_text.objectName() == cb._BROWSER_OBJECT_NAME
                       and f"#{cb._BROWSER_OBJECT_NAME}" in probe.lbl_text.styleSheet())
                record("block scrollbar QSS is scoped to the block container",
                       cb._BLOCK_SELECTOR in probe._scrollbar_qss())
                record("edit box selector is id-scoped",
                       cb._EDIT_SELECTOR.startswith("QTextEdit#"))

                # 结构性容器必须**完全不画背景**：全局样式表里那条裸
                # ``QWidget { background: ... }``（qdarktheme 下发）会让所有纯
                # QWidget 容器被 Qt 打开 WA_StyledBackground 并填上一块与气泡无关
                # 的底色——正文外多一层"底纹"、按钮行变色、气泡旁多一条暗带。
                # 先断言调色板背景全透明（与主题无关、与布局无关），再渲染一次
                # 比对像素（覆盖"任何来源的底色"，深色下尤其明显）。
                probe.ensurePolished()
                opaque = [w.objectName() for w in (probe.blocks_host,
                                                   probe.btn_widget, probe.spacer)
                          if w.palette().color(QPalette.Window).alpha() != 0]
                record("structural containers paint no background",
                       not opaque)

                probe.resize(520, 160)
                probe.set_content("Stray background probe: the text area must match "
                                  "the bubble colour.")
                probe.force_resync_height()
                for _ in range(6):
                    QApplication.processEvents()
                shot = probe.grab().toImage()
                bubble_px = shot.pixelColor(
                    probe.content_container.mapTo(probe, QPoint(4, 4))).name()
                text_px = shot.pixelColor(
                    probe.blocks_host.mapTo(probe, QPoint(2, 2))).name()
                actions_px = shot.pixelColor(
                    probe.btn_widget.mapTo(probe, QPoint(4, 4))).name()
                record("no stray background inside the bubble (rendered)",
                       probe.blocks_host.height() > 10
                       and bubble_px == text_px == actions_px)

                # 代码块必须是**一整块**底纹 + 边框：Qt 不支持块级 border，背景又是
                # 逐 QTextBlock 绘制的（行距产生的行间间隙不被覆盖 → 看起来是一排
                # 断开的灰条）。现在的做法是容器画底纹 + 边框、内部 <pre> 用同一个
                # code_bg，故这里断言"容器确有边框"且"容器底色与 <pre> 底色同源"。
                code_probe = cb.ChatBubbleWidget("", is_user=False, index=0)
                try:
                    code_probe.resize(520, 320)
                    code_probe.set_content("```python\ndef f():\n    return 1\n```\n")
                    code_probe.force_resync_height()
                    for _ in range(6):
                        QApplication.processEvents()
                    blk = code_probe._extra_blocks[0]
                    code_bg = ThemeManager().color('code_bg')
                    record("code block draws one container background + border",
                           blk.frameWidth() >= 1
                           and code_bg in blk.styleSheet()
                           and "border: 1px solid" in blk.styleSheet()
                           and code_bg in blk.browser.toHtml())
                finally:
                    code_probe.deleteLater()
            finally:
                probe.deleteLater()
        except Exception as e:
            record(f"selector scoping check raised {e}", False)

        self._log("Chat typography OK." if not fails else
                  f"Chat typography FAILED: {', '.join(fails)}",
                  "OK" if not fails else "FAIL")

    # ------------------------------------------------------------------ #
    #  Render preview (fake conversation, no AI)
    # ------------------------------------------------------------------ #
    def _build_render_preview_fixtures(self):
        """生成渲染预览演示文件，返回全部链接目标与 Mermaid 哈希。

        产出四个文本演示文件（覆盖内部文本查看器的三条渲染分支）：
        ``render_preview_demo.md``（Markdown 渲染）、``.txt``（纯文本）、
        ``.yaml`` / ``.toml``（语法高亮）；另有 ``render_preview_demo.pdf``
        （cite:// → 内部 PDF 查看器）与 ``devtest_synthetic.png``
        （file:// → 图片查看器）。
        文件固定写入临时目录：重复测试覆盖写入；不做清理，因为注入到聊天气泡里
        的链接在会话期间必须保持可点击。

        :return: dict —— *_path（md/txt/yaml/toml/pdf）、*_cite、md_file /
                 pdf_file / png_file、mermaid_hash。
        """
        demo_dir = os.path.join(tempfile.gettempdir(), "scholar_navis_devtest")
        os.makedirs(demo_dir, exist_ok=True)

        demo_texts = {
            "md": PREVIEW_DEMO_MD,
            "txt": PREVIEW_DEMO_TXT,
            "yaml": PREVIEW_DEMO_YAML,
            "toml": PREVIEW_DEMO_TOML,
        }
        demo_paths = {}
        for ext, content in demo_texts.items():
            path = os.path.join(demo_dir, f"render_preview_demo.{ext}")
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            demo_paths[ext] = path

        md_path, txt_path = demo_paths["md"], demo_paths["txt"]
        yaml_path, toml_path = demo_paths["yaml"], demo_paths["toml"]
        pdf_path = self._make_synthetic_pdf()
        png_path = self._make_synthetic_png()

        def _cite(path: str, highlight: str) -> str:
            """构造 cite:// 链接：path/name/text 整段百分号编码。

            QUrlQuery 取值时会自动解码（见 TextFormatter.handle_link_click，
            必须用 FullyDecoded，否则 Windows 盘符的 %3A 解不出来）。
            """
            return (f"cite://doc?path={quote(path, safe='')}"
                    f"&name={quote(os.path.basename(path), safe='')}"
                    f"&text={quote(highlight, safe='')}")

        # file:// 走 Path.as_uri()：自动处理 Windows 盘符、反斜杠与空格编码
        fixtures = {
            "md_path": md_path,
            "txt_path": txt_path,
            "yaml_path": yaml_path,
            "toml_path": toml_path,
            "pdf_path": pdf_path,
            # 关键词按各文件正文可选；.md 的 "renderer" 必须保留在演示内容里
            "md_cite": _cite(md_path, "renderer"),
            "txt_cite": _cite(txt_path, "viewer"),
            "yaml_cite": _cite(yaml_path, "highlight"),
            "toml_cite": _cite(toml_path, "highlight"),
            "pdf_cite": _cite(pdf_path, "preview") if pdf_path else "",
            "md_file": Path(md_path).as_uri(),
            "pdf_file": Path(pdf_path).as_uri() if pdf_path else Path(md_path).as_uri(),
            "png_file": (Path(png_path).as_uri()
                         if png_path and os.path.exists(png_path)
                         else Path(md_path).as_uri()),
            # 与 format_response 计算缓存键的方式保持一致（md5(code)）
            "mermaid_hash": hashlib.md5(PREVIEW_MERMAID_CODE.encode("utf-8")).hexdigest(),
        }

        logger.info("Render preview fixtures ready: md=%s txt=%s yaml=%s toml=%s pdf=%s png=%s",
                    md_path, txt_path, yaml_path, toml_path, pdf_path, png_path)
        return fixtures

    @staticmethod
    def _build_preview_references(fixtures) -> list:
        """渲染预览用的引用条目（编号与模板里的 ``[1]``..``[4]`` 一一对应）。

        刻意覆盖四种形态，便于一次看全引用弹窗的排版分支：

        * ``[1]`` 期刊论文：DOI + URL + 支撑原文 + 引用理由；
        * ``[2]`` 在线来源：仅 URL（无 DOI）；
        * ``[3]`` 本地文献：仅 ``path``（走文件名回落 + 本地打开）；
        * ``[4]`` 最小条目：无支撑原文，用于验证"未记录原文片段"的占位布局。
        """
        return [
            {
                "index": 1, "kind": "article",
                "title": "Highly accurate protein structure prediction with AlphaFold",
                "authors": "Jumper, J.; Evans, R.; Pritzel, A.; Green, T.",
                "year": "2021", "journal": "Nature",
                "doi": "10.1038/s41586-021-03819-2",
                "url": "https://www.nature.com/articles/s41586-021-03819-2",
                "snippet": ("The resulting system, AlphaFold, demonstrates that it is "
                            "possible to predict protein structures with atomic accuracy "
                            "even in cases where no similar structure is known."),
                "note": "Cited for the structure-prediction claim.",
            },
            {
                "index": 2, "kind": "web",
                "title": "PubMed record 31955348",
                "url": "https://pubmed.ncbi.nlm.nih.gov/31955348/",
                "snippet": ("PubMed entry referenced by the identifier auto-linking test "
                            "in section 1 (PMID: 31955348)."),
                "note": "Online source without a DOI.",
            },
            {
                "index": 3, "kind": "local_document",
                "title": "Render preview demo document",
                "path": fixtures.get("md_path", ""),
                "snippet": ("This document is the target of the cite:// link in "
                            "section 2 and is rendered by the internal text viewer."),
                "note": "Local document: only a file path, no DOI / URL.",
            },
            {
                "index": 4, "kind": "reference",
                "title": "Minimal entry without a supporting passage",
                "note": "Verifies the 'no passage captured' fallback layout.",
            },
        ]

    @staticmethod
    def _build_citation_click_references(local_path: str) -> list:
        """引文点击测试的引用条目（编号与 CITATION_TEST_AI 里的 ``[1]``..``[4]`` 对应）。

        四种分支各一，便于一次点全引用卡的所有排版分支：期刊论文（DOI + URL +
        支撑原文 + 引用理由）、在线来源（只有 URL）、本地文献（只有 path，走
        "打开原文"）、最小条目（无支撑原文）。``local_path`` 指向测试自己写出的
        真实文件——路径不存在就点不出"打开原文"，那这一条分支等于没测。
        """
        return [
            {
                "index": 1, "kind": "article",
                "title": "Highly accurate protein structure prediction with AlphaFold",
                "authors": "Jumper, J.; Evans, R.; Pritzel, A.; Green, T.",
                "year": "2021", "journal": "Nature",
                "doi": "10.1038/s41586-021-03819-2",
                "url": "https://www.nature.com/articles/s41586-021-03819-2",
                "snippet": ("The resulting system, AlphaFold, demonstrates that it is "
                            "possible to predict protein structures with atomic accuracy "
                            "even in cases where no similar structure is known."),
                "note": "Cited twice in the demo: verifies number reuse.",
            },
            {
                "index": 2, "kind": "web",
                "title": "PubMed record 31955348",
                "url": "https://pubmed.ncbi.nlm.nih.gov/31955348/",
                "snippet": ("PubMed entry used to exercise the no-DOI online branch of "
                            "the citation card."),
                "note": "Online source without a DOI.",
            },
            {
                "index": 3, "kind": "local_document",
                "title": "Citation click-through demo document",
                "path": local_path or "",
                "snippet": ("This document exists on disk so the card's 'open the "
                            "original file' action can actually be exercised."),
                "note": "Local document: only a file path, no DOI / URL.",
            },
            {
                "index": 4, "kind": "reference",
                "title": "Minimal entry without a supporting passage",
                "note": "Verifies the 'no passage captured' fallback layout.",
            },
        ]

    def _test_render_preview(self, clear: bool = True):
        """Fake-conversation render preview (no AI, no network).

        Part A (console): render the preview text through the REAL pipeline
        (``TextFormatter.format_response``) and assert scientific-ID
        auto-linking, every internal link route (cite:// text viewer with its
        Markdown / plain / highlight branches, cite:// PDF viewer, file://
        image/system-app, mermaid:// viewer), the render mode each demo file
        will get in the text viewer, inline ``[n]`` citation linking, LaTeX
        degradation (sup/sub generated, no command residue), tables, code
        blocks, chemistry subscripts and the Mermaid card.

        Part B (visual): generate the demo files and inject a fake user+AI
        bubble pair into the Chat panel (``route_dev_conversation``),
        together with the demo citation entries so the ``[n]`` hover card /
        detail panel can be opened right away.

        Deliberately NOT part of ``Run All``: Part B switches the main
        window to the Chat page, which would be a surprising side effect
        for a batch run.
        """
        if clear:
            self._clear()
        self._log("--- Render Preview (markdown / links / LaTeX, fake chat) ---", "INFO")
        fails = []

        # --- Part A: 渲染断言（真实管线，纯函数调用，无界面副作用） ---
        ai_text = ""
        try:
            from src.ui.components.text_formatter import TextFormatter
            from src.ui.components.text_viewer import InternalTextViewer

            fixtures = self._build_render_preview_fixtures()
            # cite:// 路由契约自检：QUrlQuery 默认 PrettyDecoded 不解码 %3A
            # （Windows 盘符路径会失效），路由器必须用 FullyDecoded 取值
            from PySide6.QtCore import QUrl, QUrlQuery
            decoded_cite_path = QUrlQuery(QUrl(fixtures["md_cite"])).queryItemValue(
                "path", QUrl.ComponentFormattingOption.FullyDecoded)

            try:
                import pygments  # noqa: F401
                pygments_ok = True
            except ImportError:
                pygments_ok = False
                self._log("Pygments missing: .yaml/.toml highlight assertions skipped.",
                          "WARN")

            def _mode_for(path: str) -> str:
                """演示文件将被文本查看器采用的渲染模式（与实际渲染同一份判定）。"""
                ext = os.path.splitext(path)[1].lower().lstrip(".")
                return InternalTextViewer._resolve_render_mode(ext, path)

            ai_text = PREVIEW_AI_TEMPLATE
            for token, value in (
                    ("__MD_CITE__", fixtures["md_cite"]),
                    ("__TXT_CITE__", fixtures["txt_cite"]),
                    ("__YAML_CITE__", fixtures["yaml_cite"]),
                    ("__TOML_CITE__", fixtures["toml_cite"]),
                    ("__PDF_CITE__", fixtures["pdf_cite"]),
                    ("__MD_FILE__", fixtures["md_file"]),
                    ("__PDF_FILE__", fixtures["pdf_file"]),
                    ("__PNG_FILE__", fixtures["png_file"]),
                    ("__MERMAID_HASH__", fixtures["mermaid_hash"])):
                ai_text = ai_text.replace(token, value)

            mermaid_cache = {}
            html = TextFormatter.format_response(ai_text, 0, set(), set(), mermaid_cache)

            def theme_rerender_is_clean(rendered_html: str) -> bool:
                """主题重渲染幂等回归检查。

                `set_content` 收到的入参是"已渲染 HTML"，主题切换时会对它
                重渲染。渲染管线必须先把上一次注入的主题内联样式清掉再按新
                主题重建，否则会出现"浅色主题下标题发白、代码块仍是深色底"
                的残留。这里用另一个主题重渲染一次，断言当前主题的正文色与
                边框色均不残留（两套主题这两个色值互不相同）。
                """
                themer = ThemeManager()
                other = 'light' if themer.current_theme == 'dark' else 'dark'
                stale_text = themer.color('text_main')
                stale_border = themer.color('border')
                regenerated = TextFormatter.markdown_to_html(rendered_html, theme_key=other)
                if stale_text.lower() in regenerated.lower():
                    self._log(f"stale text color survives re-render: {stale_text}", "FAIL")
                    return False
                if stale_border.lower() in regenerated.lower():
                    self._log(f"stale border color survives re-render: {stale_border}", "FAIL")
                    return False
                return True

            checks = [
                ("DOI auto-linked", "https://doi.org/10.1038/s41586-021-03819-2" in html),
                ("PMID auto-linked", "pubmed.ncbi.nlm.nih.gov/31955348" in html),
                ("UniProt auto-linked", "uniprot.org/uniprotkb/P12345" in html),
                ("GO auto-linked", "QuickGO/term/GO:0006915" in html),
                ("AGI auto-linked", "arabidopsis.org" in html),
                ("KEGG auto-linked", "kegg.jp/entry/K01647" in html),
                ("SNP auto-linked", "ensembl.org/Variation/Explore?v=rs429358" in html),
                ("Cotton Ghir auto-linked",
                 "cottongen.org/feature/Ghir_D03G12349.1" in html),
                ("Cotton nomenclature coverage",
                 all(f"cottongen.org/feature/{gid}" in html for gid in (
                     "Gh_A01G0001", "Ghir_A05G01234", "GH_A13G2516",
                     "GhChrD09G1234", "Ghi_D03G5678", "Gh_D11G324566",
                     "Gohir.A01G000100"))),
                ("Code block styled", '<pre style="' in html),
                ("Inline code styled", '<code style="' in html),
                # --- 内部链接路由：每条路由一个断言，断链时一眼看出是哪一条 ---
                ("cite:// (.md) -> text viewer",
                 "cite://doc?path=" in html
                 and quote(fixtures["md_path"], safe="") in html),
                ("cite:// (.txt) -> text viewer",
                 quote(fixtures["txt_path"], safe="") in html),
                ("cite:// (.yaml) -> text viewer (highlight branch)",
                 quote(fixtures["yaml_path"], safe="") in html),
                ("cite:// (.toml) -> text viewer (highlight branch)",
                 quote(fixtures["toml_path"], safe="") in html),
                ("cite:// (.pdf) -> pdf viewer",
                 bool(fixtures["pdf_cite"])
                 and quote(fixtures["pdf_path"], safe="") in html),
                ("cite:// path round-trip", os.path.exists(decoded_cite_path)),
                ("PDF fixture on disk", os.path.exists(fixtures["pdf_path"])),
                ("YAML/TOML fixtures on disk",
                 os.path.exists(fixtures["yaml_path"])
                 and os.path.exists(fixtures["toml_path"])),
                ("file:// link present", "file://" in html),
                ("file:// (.png) -> image viewer", fixtures["png_file"] in html),
                ("file:// (.pdf) -> system app", fixtures["pdf_file"] in html),
                ("LaTeX sup/sub", "<sup>" in html and "<sub>" in html),
                ("LaTeX no command residue", not re.search(r"\\[a-zA-Z]+", html)),
                # 注意：主题化注入后表格/代码块均带属性，统一用前缀匹配
                # （<table border=... style=...>、<pre style=...>）。
                ("Table rendered", "<table" in html),
                ("Table styled", "border-collapse:collapse" in html
                 and 'border-color:' in html),
                ("Code block rendered", "<pre" in html),
                ("Chemistry subscript", "H<sub>12</sub>O<sub>6</sub>" in html),
                ("Mermaid card", "mermaid://view?hash=" in html
                 and len(mermaid_cache) == 1),
                ("mermaid:// direct link (hash = code)",
                 f"mermaid://view?hash={fixtures['mermaid_hash']}" in html),
                ("Inline citation [1] -> ref:// link", "ref://cite?n=1" in html),
                ("Inline citations [2]-[4] linked",
                 all(f"ref://cite?n={n}" in html for n in (2, 3, 4))),
                ("Theme re-render idempotent", theme_rerender_is_clean(html)),
            ]
            # 文本查看器的渲染分派：预览里的链接点开后应命中与扩展名匹配的分支
            # （.md → Markdown 渲染；.txt → 纯文本；.yaml/.toml → Pygments 高亮）。
            # 与 InternalTextViewer 共用同一份判定，不是在这里复写一遍规则。
            checks.append(("viewer mode (.md) = markdown",
                           _mode_for(fixtures["md_path"]) == "markdown"))
            checks.append(("viewer mode (.txt) = plain",
                           _mode_for(fixtures["txt_path"]) == "plain"))
            if pygments_ok:
                checks.append(("viewer mode (.yaml) = highlight",
                               _mode_for(fixtures["yaml_path"]) == "highlight"))
                checks.append(("viewer mode (.toml) = highlight",
                               _mode_for(fixtures["toml_path"]) == "highlight"))
            else:
                self._log("Pygments missing: .yaml/.toml viewer mode not verified.", "WARN")

            for name, ok in checks:
                self._log(f"{name}: {'OK' if ok else 'MISSING'}", "OK" if ok else "FAIL")
                if not ok:
                    fails.append(name)
        except Exception as e:
            self._log(f"Renderer pipeline check failed: {e}", "FAIL")
            fails.append(f"renderer pipeline: {e}")

        if fails:
            self._log("Render preview assertions FAILED; chat injection skipped.", "FAIL")
            return

        # --- Part B: 注入假对话（切到 Chat 面板，不调用 AI） ---
        route = getattr(self.main_window, "route_dev_conversation", None)
        if route is None:
            self._log("MainWindow.route_dev_conversation missing.", "FAIL")
            return
        self._log("Dispatching fake conversation to the Chat panel...", "INFO")
        try:
            route(PREVIEW_NOTE, PREVIEW_USER_TEXT, ai_text,
                  self._build_preview_references(fixtures))
            self._log("Fake conversation injected. Check the Chat Assistant "
                      "and click the links to verify routing.", "OK")
        except Exception as e:
            self._log(f"Failed to inject fake conversation: {e}", "FAIL")
