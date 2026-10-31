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
      wiring, bubble typography + glued-list repair, reference integrity and
      title italics, history-replay chunking). No AI involved. Checks that only
      need pure functions never create a window and never touch user data.
      Two checks deliberately stay OUT of Run All because they need the real
      world: the live literature-trust check (network + DOI resolution) and the
      paywalled full-text handover (a human downloads the PDF into the picker
      window, which the task thread waits on).

Design principles:
    * High cohesion: one test = one focused method; no cross-deps.
    * Low coupling: AI tests only need a ``MainWindow`` reference exposing
      ``route_dev_test``; functional tests only touch core modules.
    * Read-only: functional tests use synthetic fixtures and never mutate
      user data (the full-text check only reads the RSS cache and the PDF the
      user hands over).
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
import threading
import time

from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QLabel,
    QPushButton,
    QRadioButton,
)

from src.core.fulltext_samples import resolve_samples
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
            "calls <b>search_academic_literature</b> (and/or search_preprints); each "
            "tool result item carries a <b>citation_token</b> (hyperlink text such as "
            "&lt;a href='ref://cite?n=3'&gt;[3]&lt;/a&gt;) whose number the app assigned, "
            "and the agent pastes that token at the claims it supports. The app only "
            "looks up which numbers are cited and renders the '📚 Cited Sources' list. "
            "Verify in the reply: (1) every inline [n] matches the list number AND the "
            "correct paper; (2) list order == number order (no renumbering); "
            "(3) hovering a [n] shows the SAME paper; (4) clicking [n] opens the "
            "matching detail card. Check the log for 'no registered source' — it must "
            "be empty when every number came from a tool."
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
    "fulltext_fetch": {
        "note": (
            "Developer test: exercising the <b>paywalled full-text</b> path end to end "
            "in the real Chat panel. The prompt names a subscription article (the first "
            "paywalled hit of the local RSS cache, otherwise a bundled sample shipped "
            "with the app). Expected: fetch_paper_fulltext reports no open-access body "
            "-> request_fulltext -> a system notification appears (the picker waits in "
            "the background) -> after you pick the PDF, the SAME turn continues with a "
            "reading brief "
            "(bullets + verbatim quotes + [pN] page numbers). Wanting exact quotes is "
            "deliberate: it forces the full-text route instead of paraphrasing the "
            "abstract."
        ),
        "prompt": (
            "There's a subscription paper I have to quote precisely in my manuscript "
            "{sample}. The abstract doesn't give me what I need, so could you get the "
            "actual full text and pull out the specific findings it reports, quoting "
            "the exact wording with page numbers? If you need the PDF, just ask me — "
            "I'll download it from my library and hand it to you."
        ),
        #: 提示词里的 {sample} 由 _ai_test 用真实样例填（见 _fill_sample）
        "sample": True,
    },
}


def _fill_sample(template: str, sample) -> str:
    """把样例文章填进 AI 测试提示词：``{sample}`` 换成标题 / DOI / 链接。

    模型要靠这些标识才找得到那篇付费墙论文；没有样例时也不留裸占位符。
    """
    if not sample:
        return template.replace("{sample}", "(I don't have the identifier handy)")
    parts = [f"\"{sample['title']}\""] if sample.get("title") else []
    if sample.get("doi"):
        parts.append(f"DOI {sample['doi']}")
    if sample.get("url"):
        parts.append(str(sample["url"]))
    filled = f"({', '.join(parts)})" if parts else "(I don't have the identifier handy)"
    return template.replace("{sample}", filled)


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
- A tool-returned citation token pasted verbatim renders identically <a href="ref://cite?n=2">[2]</a>.

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


#: "Literature Trust" 按钮的两态文案。**唯一定义处**：创建、后台运行中、收尾三处
#: 都引用它，避免按钮文字与运行状态说明各写一份而漂移。
LITTRUST_BTN_IDLE = "Literature Trust (live)"
LITTRUST_BTN_RUNNING = "Literature Trust (running…)"

#: 取全文自检里交给抽取子代理的问题。样例文章各不相同，问法必须与具体论文无关。
DEV_FULLTEXT_QUESTION = (
    "What are the paper's key findings, and what evidence does it report for them?"
)

#: 取全文自检里抽取子代理的桩回复：用来证明"子代理的结论被原样回传给主模型"，
#: 而不是走了"读不出正文"的兜底分支。
DEV_FULLTEXT_STUB_BRIEF = "DEV-STUB BRIEF"

#: 取全文自检等窗口交回 PDF 的上限（秒）。窗口关闭时一定会通知等待方，这里只防极端
#: 情况下把自检挂死；人工下载耗时不计入（文件交回后 ``wait`` 立刻返回）。
DEV_FULLTEXT_WAIT_TIMEOUT = 5.0


class _DevLogStream(QObject):
    """后台线程 → UI 线程的单向输出通道。

    联网自检（真实检索 + DOI / 链接核查，单条超时上限合计可达百秒级）必须离开 UI
    线程，否则整个窗口在等待期间不响应；但结果要写进对话框的输出控件，而 Qt 只允许
    在对象所属线程使用对象。跨线程因此只走信号：PySide6 会自动改用队列连接，槽在主
    线程执行，后台线程一根控件都不碰。
    """

    #: (消息, 等级) —— 等级取值与 :meth:`DeveloperDialog._log` 一致
    sig_line = Signal(str, str)
    #: 后台自检结束（无论成功或异常），用于恢复按钮状态
    sig_finished = Signal()


class DeveloperDialog(BaseDialog):
    """Hidden developer self-test panel."""

    def __init__(self, main_window=None, parent=None):
        # ``main_window`` routes AI tests into the real Chat panel.
        super().__init__(parent or main_window, title="Developer Mode", width=760)
        self.main_window = main_window

        self.setWindowTitle("Developer Mode")
        self.setObjectName("DeveloperDialog")

        #: 按钮组登记：(按钮列表, 列数)。对话框宽度由它们的真实文字宽度反推
        #: （见 _fit_width_to_buttons），保证按钮文字永远不被截断。
        self._btn_groups: list = []

        #: 联网自检的后台线程与它的回传通道（见 _test_literature_source_trust）
        self._littrust_thread: threading.Thread | None = None
        #: 生信前缀安装线程（见 _test_bioenv_prefix）
        self._bioenv_install_thread: threading.Thread | None = None
        self._dev_stream = _DevLogStream(self)
        self._dev_stream.sig_line.connect(self._log)
        self._dev_stream.sig_finished.connect(self._on_littrust_finished)

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
        # 每个按钮都保持"刚好放得下文字"的宽度；用网格而不是单行 QHBoxLayout，
        # 否则一行塞 4 个长标题时按钮会被压到文字宽度以下（文字被截断）。
        self.content_layout.addWidget(self._section_label("AI Tests (run in Chat panel)"))
        self.btn_ai_plot = self._make_btn("AI: Plot (bubble)", lambda: self._ai_test("plot_bubble"))
        self.btn_ai_prov = self._make_btn("AI: Provenance", lambda: self._ai_test("provenance_chain"))
        self.btn_ai_lit = self._make_btn("AI: Literature (Breadth)", lambda: self._ai_test("literature_breadth"))
        self.btn_ai_img = self._make_btn("AI: Image Vision", lambda: self._ai_test("image_upload_vision"))
        # 第二行：human-in-the-loop（ask_user / deep plan 卡）与非图片附件、
        # 参考文献编号链路（真实检索 + 编号一致性核对）
        self.btn_ai_ask = self._make_btn("AI: Ask-User (HITL)",
                                         lambda: self._ai_test("ask_user_clarify"))
        self.btn_ai_deep = self._make_btn("AI: Deep Plan Card",
                                          lambda: self._ai_test("deep_plan_confirm"))
        self.btn_ai_doc = self._make_btn("AI: File Attachment",
                                         lambda: self._ai_test("document_attachment"))
        self.btn_ai_refnum = self._make_btn("AI: Reference Numbering (real search)",
                                            lambda: self._ai_test("reference_numbering"))
        self.content_layout.addLayout(self._button_grid(
            (self.btn_ai_plot, self.btn_ai_prov, self.btn_ai_lit, self.btn_ai_img), cols=4))
        self.content_layout.addLayout(self._button_grid(
            (self.btn_ai_ask, self.btn_ai_deep, self.btn_ai_doc, self.btn_ai_refnum),
            cols=4))
        # 第三行：付费墙取全文（真实模型 + 真实取全文窗口 + 人工下载 PDF，全链路）
        self.btn_ai_fulltext = self._make_btn("AI: Full Text Fetch (paywall)",
                                              lambda: self._ai_test("fulltext_fetch"))
        self.content_layout.addLayout(self._button_grid((self.btn_ai_fulltext,), cols=4))

        # --- Functional tests ---
        self.content_layout.addWidget(self._section_label("Functional Tests"))
        # 离线纯函数 / 接口断言：不启动引擎、不创建窗口、不碰用户数据
        self.btn_all = self._make_btn("Run All", self._run_all)
        self.btn_r = self._make_btn("R Engine", self._test_r_engine)
        self.btn_prov = self._make_btn("Provenance (module)", self._test_provenance)
        self.btn_skill = self._make_btn("Skill Gate", self._test_skill_gate)
        self.btn_syntax = self._make_btn("Syntax/Import", self._test_syntax)
        self.btn_lit = self._make_btn("Literature Merge", self._test_literature_merge)
        self.btn_img = self._make_btn("Image Pipeline", self._test_image_pipeline)
        # 交互卡与对话区状态。Deep Plan Card 不依赖 AI / 网络，点击立即完成
        # "组件行为 + 气泡渲染链路"全量自检；History Restore 覆盖分片回灌 + 快照闸口。
        self.btn_deep_card = self._make_btn("Deep Plan Card", self._test_deep_plan_card)
        self.btn_hitl = self._make_btn("HITL Pipeline", self._test_hitl_pipeline)
        self.btn_render = self._make_btn("Render Preview", self._test_render_preview)
        self.btn_restore = self._make_btn("History Restore", self._test_history_restore)
        # 附件与阅读链路自检（可上传类型 / 文本查看器渲染分派 / 滚动条可见性 /
        # 附件芯片接线 / 排版 / 引用编号与反捏造审计 / 引文点击）
        self.btn_filetypes = self._make_btn("File Types", self._test_file_types)
        self.btn_textviewer = self._make_btn("Text Viewer", self._test_text_viewer_render)
        self.btn_scrollbar = self._make_btn("Scrollbar Theme", self._test_scrollbar_theme)
        self.btn_attachwire = self._make_btn("Attachment Wiring", self._test_attachment_wiring)
        self.btn_typography = self._make_btn("Chat Typography", self._test_chat_typography)
        self.btn_refnum = self._make_btn("Reference Numbering", self._test_reference_citations)
        self.btn_refint = self._make_btn("Reference Integrity", self._test_reference_integrity)
        self.btn_storage = self._make_btn("Storage Round-Trip", self._test_storage_roundtrip)
        self.btn_citeclick = self._make_btn("Citation Click-Through", self._test_citation_click)
        # 引用可信性。Integrity 为离线反捏造审计（进 Run All）；Literature Trust 会
        # 发起**真实检索 + DOI 解析**，故独立成按钮、不进 Run All，避免默认自检占用
        # 网络与配额，且它必须跑在后台线程（见 _test_literature_source_trust）。
        self.btn_littrust = self._make_btn(LITTRUST_BTN_IDLE,
                                           self._test_literature_source_trust)
        self.btn_uiinter = self._make_btn("UI Copy / Drag-Scroll",
                                          self._test_ui_copy_scroll)
        # 付费墙取全文。样例优先取本机 RSS 缓存里的付费墙文章，没有订阅时回退到随包
        # 分发的内置样例（见 src.core.fulltext_samples），因此分发后也能测。链路要人工
        # 去浏览器下载 PDF 并交回窗口，所以它独立成按钮、不进 Run All。
        self.btn_fulltext = self._make_btn("Full Text Fetch", self._test_fulltext_fetch)
        # 生信工具层。离线自检进 Run All；安装前缀会联网下载数百 MB，独立成按钮
        # 且在后台线程里跑，避免把对话框卡住数分钟。
        self.btn_bioenv = self._make_btn("Bioinformatics", self._test_bioenv)
        self.btn_bioenv_install = self._make_btn("Bioenv Install (network)",
                                                 self._test_bioenv_prefix)
        self.content_layout.addLayout(self._button_grid(
            (self.btn_all, self.btn_r, self.btn_prov, self.btn_skill,
             self.btn_syntax, self.btn_lit, self.btn_img,
             self.btn_deep_card, self.btn_hitl, self.btn_render, self.btn_restore,
             self.btn_filetypes, self.btn_textviewer, self.btn_scrollbar,
             self.btn_attachwire, self.btn_typography, self.btn_refnum,
             self.btn_refint, self.btn_storage, self.btn_citeclick,
             self.btn_littrust, self.btn_uiinter, self.btn_fulltext,
             self.btn_bioenv, self.btn_bioenv_install),
            cols=4))

        # --- Output area（专属源码/日志输出控件：固定最大高度 + 独立滚动条、
        #     边框底纹、复制、折叠、深色模式自适应） ---
        self.txt_output = SourceCodeViewer(
            title="Console Output",
            editable=False,
            collapsed=False,
            max_height=420,
        )
        self.content_layout.addWidget(self.txt_output, 1)

        # 按按钮的真实文字宽度定宽：BaseDialog 是固定宽度对话框，宽度不够时 Qt 只能
        # 裁掉右侧内容（表现为按钮文字被切掉），所以必须在这里先把宽度算准。
        self._fit_width_to_buttons()

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
        """自检按钮：**最小宽度 = 文字宽度**（+ 余量），因此任何一行都不会切字。

        QPushButton 的 sizeHint 已经含样式表的内边距与边框，取它作最小宽度即可；
        额外加的 8px 是给字体渲染差异与主题切换后的内边距变化留的余量。
        """
        btn = QPushButton(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(handler)
        btn.setMinimumWidth(btn.sizeHint().width() + 8)
        return btn

    def _button_grid(self, buttons, cols: int) -> QGridLayout:
        """把按钮排成"每行 ``cols`` 个"的网格，并登记宽度需求。

        为什么不用单行 ``QHBoxLayout``：一行塞 7~8 个按钮时 Qt 会把每个按钮压缩到
        低于文字宽度（截断成 "Citation Click-Thro"），而网格 + 按钮最小宽度让每一行
        自己折行，文字始终完整。
        """
        buttons = list(buttons)
        grid = QGridLayout()
        grid.setSpacing(8)
        for index, btn in enumerate(buttons):
            grid.addWidget(btn, index // cols, index % cols)
        # 末列右侧留一列弹性：按钮保持自然宽度并左对齐，不被拉伸成大间距
        grid.setColumnStretch(cols, 1)
        self._btn_groups.append((buttons, cols))
        return grid

    def _fit_width_to_buttons(self):
        """按各按钮组的真实文字宽度决定对话框宽度（不超过屏幕的 90%）。

        ``BaseDialog`` 用 ``setFixedWidth`` 锁定宽度，内容最小宽度超出时 Qt 只能裁剪
        （历史症状：按钮文字被切掉）。这里按"每列最宽按钮之和 + 间距 + 内容边距"算出
        所需宽度，取最大值与当前目标宽度的较大者——因此既不会裁字，也不会无谓变宽。
        """
        needed = 0
        for buttons, cols in self._btn_groups:
            col_w = [0] * cols
            for index, btn in enumerate(buttons):
                # 取"布局绝不会压到的下限"：minimumWidth（_make_btn 已设为文字宽度
                # + 余量），缺省时退回 sizeHint。用 sizeHint 会在按钮被显式加宽
                # （如字体变大后重设最小宽度）时低估需求。
                col_w[index % cols] = max(col_w[index % cols],
                                          btn.minimumWidth(), btn.sizeHint().width())
            needed = max(needed, sum(col_w) + 8 * max(0, cols - 1))
        if not needed:
            return

        # content_layout 左右各 24px 边距 + 少量余量（滚动条占位、字体渲染差异）
        target = int(needed) + 48 + 16
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            target = min(target, int(screen.availableGeometry().width() * 0.9))
        target = max(int(self._target_width), target)
        if target != self._target_width:
            logger.debug("Developer dialog width widened to %dpx (button groups=%d).",
                         target, len(self._btn_groups))
            self._target_width = target
            self.setFixedWidth(target)

    def _log(self, msg: str, level: str = "INFO"):
        prefix = {"INFO": "[ ]", "OK": "[OK]", "FAIL": "[FAIL]", "WARN": "[!!]"}.get(level, "[ ]")
        self.txt_output.append(f"{prefix} {msg}")

    def _emit_dev_log(self, msg: str, level: str = "INFO"):
        """后台线程写日志的**唯一**入口：只发信号，绝不直接碰控件。

        对话框可能已被关闭销毁（后台线程仍在跑），此时发信号会抛
        "Internal C++ object already deleted"——静默忽略即可，线程自行收尾。
        """
        try:
            self._dev_stream.sig_line.emit(msg, level)
        except RuntimeError:
            pass

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

        # 需要点名具体论文的测试（付费墙取全文）在发车前才解析样例：本机 RSS 缓存
        # 里的付费墙文章优先，没有订阅时用随包分发的内置样例，于是分发后也点得出题目。
        prompt = entry["prompt"]
        if entry.get("sample"):
            samples, origin = resolve_samples(limit=1)
            if samples:
                sample = samples[0]
                self._log(f"Full-text sample from {origin}", "INFO")
                self._log(f"Paper: {sample.get('title') or '(untitled)'} "
                          f"[{sample.get('doi') or sample.get('url') or 'no id'}]", "INFO")
            else:
                sample = None
                self._log("No paywalled sample available (RSS cache empty and the bundled "
                          "sample file missing) — sending the prompt without a paper.", "WARN")
            prompt = _fill_sample(prompt, sample)

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
        self._log(f"Prompt: {prompt[:80]}...", "INFO")
        try:
            if attachment_paths:
                route(prompt, note_text=entry["note"],
                      attachment_paths=attachment_paths)
            else:
                route(prompt, note_text=entry["note"])
            self._log("Sent to Chat panel. Check the Chat Assistant for results.", "OK")
        except TypeError:
            # Fallback: older route method without note_text / attachment_paths.
            try:
                route(prompt)
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
        """引用编号自检：生成期全部隐藏 + 收尾按出现顺序定稿为 1..K。

        这条链路的硬约束有两条，必须同时成立：

        1. **生成过程中不显示任何引用**（正文与思考面板都不露），否则用户会看到
           一串还没有列表可对的内部号；
        2. **收尾按正文首次出现顺序**分配 1..K，列表顺序 = 阅读顺序。

        断言因此覆盖：内部号与 token 的形态、隐藏函数对四种写法的处理、定稿时的
        排序 / 改写 / 未登记编号抹除 / 思考块处理，以及最终列表编号与条目一致。
        """
        if clear:
            self._clear()
        self._log("--- Reference Numbering (hidden while streaming, 1..K by appearance) ---",
                  "INFO")
        try:
            from src.core.references import (CITE_LINK_RE, ReferenceItem,
                                             ReferenceRegistry, citation_href,
                                             citation_token, strip_citation_markers)

            reg = ReferenceRegistry()
            # 受理顺序与正文引用顺序故意不同：定稿编号只看正文出现顺序。
            reg.add(ReferenceItem(index=0,
                                  title="Can improvement in photosynthesis increase crop yields?",
                                  authors="Long", year="2006"))          # 内部号 1
            token_b = reg.add(ReferenceItem(index=0,
                                            title="The Calvin-Benson cycle and its regulation",
                                            authors="Sharkey", year="2019"))   # 内部号 2
            # 本地 KB 文档：编号来自 '--- [Document 3] ---'，同样先于正文存在。
            reg.seed(3, ReferenceItem(index=3, title="Local KB doc",
                                      path="/tmp/kb.pdf", kind="local_document"))

            fails = []
            # 1) 内部号递增、重复来源复用（token 因此稳定）
            again = reg.add(ReferenceItem(index=0,
                                          title="The Calvin-Benson cycle and its regulation",
                                          authors="Sharkey", year="2019"))
            if again != token_b:
                fails.append(f"duplicate source got a new internal id: {again} != {token_b}")
            if len(reg) != 3:
                fails.append(f"duplicate registration inflated the registry: {len(reg)}")

            # 2) token 形态：可粘贴的超链接文本，且内部号可原样解回
            token = citation_token(token_b)
            if token != f'<a href="{citation_href(token_b)}">[{token_b}]</a>':
                fails.append(f"unexpected token shape: {token!r}")
            match = CITE_LINK_RE.search(token)
            if not match or int(match.group(1)) != token_b:
                fails.append(f"token href does not parse back to [{token_b}]: {token!r}")

            # 3) 生成期隐藏：锚点 / 裸编号 / 思考块 / 半截 token 一律不露
            hidden_src = (f"结论一 {token}。结论二 [1]。"
                          f"<think>思考里 {token} 与 [3]</think>"
                          f"半截 <a href=\"ref://cite?n=2\">[2")
            hidden = strip_citation_markers(hidden_src)
            for residue in ("ref://cite", "<a ", "[1]", "[2", "[3]"):
                if residue in hidden:
                    fails.append(f"citation marker survived during generation: {residue!r}")
            if "思考里" not in hidden or "结论二" not in hidden:
                fails.append(f"hiding must not eat the prose: {hidden!r}")

            # 4) 收尾定稿：正文先引 [2] -> [1]，再引 [1] -> [2]，KB 文档 -> [3]
            body = (f"光合作用受光调节 {token}，且碳同化参与 [1]；"
                    f"再次引用同一来源 {token}；本地文档 [3]；未登记 [42]。")
            new_text, cited, unmatched = reg.renumber_by_appearance(body)
            if "受光调节 [1]" not in new_text or "碳同化参与 [2]" not in new_text:
                fails.append(f"numbering is not in order of appearance: {new_text!r}")
            if "本地文档 [3]" not in new_text:
                fails.append(f"KB document number not folded into the sequence: {new_text!r}")
            if [it.index for it in cited] != [1, 2, 3]:
                fails.append(f"cited items != [1,2,3]: {[it.index for it in cited]}")
            if [it.title[:6] for it in cited] != ["The Ca", "Can im", "Local "]:
                fails.append(f"list order must follow the text: {[it.title[:6] for it in cited]}")
            if unmatched != [42]:
                fails.append(f"unmatched != [42]: {unmatched}")
            if "[42]" in new_text:
                fails.append("an unregistered number must be removed from the answer")
            if len(reg) != 3:
                fails.append("numbering must not create registry entries")
            # 全角编号（中文输入法常见）同样参与定稿
            wide, wide_items, _ = reg.renumber_by_appearance("见［2］与【3】")
            if wide != "见[1]与[2]" or [it.index for it in wide_items] != [1, 2]:
                fails.append(f"full-width brackets not normalized: {wide!r}")

            # 5) 思考块不参与排序，但同一套编号；只在思考里出现的来源整条抹掉
            think_src = "正文 [1]。\n<think>这里 [3] 与 [99]</think>"
            t5, o5, u5 = reg.renumber_by_appearance(think_src)
            if "正文[1]" not in t5.replace(" ", ""):
                fails.append(f"body numbering broken: {t5!r}")
            if "[3]" in t5 or "[99]" in t5:
                fails.append(f"think-only citation must be dropped: {t5!r}")
            if [it.index for it in o5] != [1]:
                fails.append(f"think-only source leaked into the list: {[it.index for it in o5]}")

            # 6) 代码块 / 行内代码里的 [n] 是代码本身，隐藏与定稿都不得动它
            code_src = (f"正文 {token}。\n```python\narr = items[1]\n```\n"
                        f"行内 `x = data[2]` 保持原样。")
            code_hidden = strip_citation_markers(code_src)
            if "items[1]" not in code_hidden or "data[2]" not in code_hidden:
                fails.append(f"code spans were mangled while hiding: {code_hidden!r}")
            code_final, code_items, _ = reg.renumber_by_appearance(code_src)
            if "items[1]" not in code_final or "data[2]" not in code_final:
                fails.append(f"code spans were renumbered as citations: {code_final!r}")
            if [it.index for it in code_items] != [1]:
                fails.append(f"code brackets leaked into the list: {[it.index for it in code_items]}")

            # 7) 列表顺序 = 定稿编号顺序，且条目编号与正文一致
            html = reg.render_items(cited)
            if not (0 <= html.find("[1]") < html.find("[2]") < html.find("[3]")):
                fails.append("rendered list order != appearance order")
            if "<b>[1]</b>" not in html or "Sharkey" not in html:
                fails.append("rendered list missing [1]/Sharkey")

            if fails:
                for f in fails:
                    self._log(f"FAIL: {f}", "FAIL")
                self._log(f"Reference numbering test FAILED ({len(fails)} assertion(s)).", "FAIL")
            else:
                self._log("Nothing citation-like is shown while the answer streams.", "OK")
                self._log("Final numbers are 1..K in order of appearance (anchors rewritten).", "OK")
                self._log("Think-block markers follow the same numbering; think-only ones drop.", "OK")
                self._log("Unregistered numbers are removed and reported.", "OK")
                self._log(f"Sample token: {token}", "INFO")
                self._log("Reference numbering test PASSED.", "OK")
        except Exception as e:
            self._log(f"Reference numbering test failed: {e}", "FAIL")

    def _test_reference_integrity(self, clear: bool = True):
        """反捏造审计（离线）：参考文献列表里的每一条都必须有出处。

        列表条目只可能来于"工具受理过的来源"，逐条钉死其不可捏造性：

        1. 定稿过程（:meth:`ReferenceRegistry.renumber_by_appearance`）只做编号
           查表，前后注册表的条目数、每个条目的对象身份都必须不变；
        2. 正文凭空写的编号（没有工具分配过）不会变成条目，只被如实上报并抹掉；
        3. 模型提交条目时自报的编号一律被忽略，编号只能由注册表分配；
        4. 列表条目必须能落回注册表条目并带可核查标识（标题/DOI/URL/本地路径）。

        联网核查（DOI 解析、标题年份比对）见 Literature Trust (live)。
        """
        if clear:
            self._clear()
        self._log("--- Reference Integrity (anti-fabrication) ---", "INFO")
        fails = []
        try:
            from src.core.references import ReferenceItem, ReferenceRegistry

            reg = ReferenceRegistry()
            reg.add(ReferenceItem(index=0,
                                  title="Mutations in the NPH3 gene of Arabidopsis",
                                  authors="Furutani, M.; Sakamoto, N.", year="2007"))
            reg.add(ReferenceItem(index=0, title="NPH3 condensates",
                                  authors="Zhang, Y.", year="2026"))
            reg.seed(7, ReferenceItem(index=7, kind="online_source",
                                      title="Lateral relocation of auxin efflux regulator PIN3",
                                      authors="Friml, J.", year="2002"))
            before = {it.index: it for it in reg.items()}

            body = ("正文按内部号引用 [1] 与 [7]；"
                    "另有正文自拟的 [3] 与 [99]。")
            new_text, cited, unmatched = reg.renumber_by_appearance(body)

            # 1) 定稿不得新建、替换或改写任何注册条目
            after = {it.index: it for it in reg.items()}
            if len(after) != len(before):
                fails.append(f"registry size changed: {len(before)} -> {len(after)}")
            for idx, item in before.items():
                if after.get(idx) is not item:
                    fails.append(f"registry item {idx} was replaced or mutated by numbering")
            if [it.index for it in cited] != [1, 2]:
                fails.append(f"final numbers != [1, 2]: {[it.index for it in cited]}")
            if [it.title[:6] for it in cited] != ["Mutati", "Latera"]:
                fails.append(f"final order must follow the text: {[it.title[:6] for it in cited]}")

            # 2) 列表条目必须落回注册表条目，且带可核查标识
            fingerprints = {(it.title, it.url, it.doi, it.path) for it in after.values()}
            for it in cited:
                if (it.title, it.url, it.doi, it.path) not in fingerprints:
                    fails.append(f"listed item [{it.index}] matches no registry entry")
                if not (it.title or it.url or it.doi or it.path or it.snippet):
                    fails.append(f"listed item [{it.index}] carries no provenance field at all")

            # 3) 正文自拟编号：不得进列表，也不得"近似"落到别人的条目上
            if unmatched != [3, 99]:
                fails.append(f"unregistered numbers not reported: {unmatched}")
            if "[3]" in new_text or "[99]" in new_text:
                fails.append(f"unregistered numbers survived into the answer: {new_text!r}")

            # 4) 模型不能自报编号：payload 里的 index 字段一律被忽略
            forced = ReferenceItem.from_ai_payload(
                {"title": "Forced number", "index": 99, "year": "2099"}, index=0)
            if forced.index != 0:
                fails.append("model-supplied number leaked into the entry")
            if reg.add(forced) != 8:
                fails.append("the registry must assign the next number itself")

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

            # 6b) 正文"未核实"门禁的判定粒度：以"陈述组 + 依据行"为单位，不是逐行。
            #     逐行判定会把"陈述一行、依据一行"的学术写法整篇标红（真实回答里
            #     14 段被标 14 处），提醒随之失效。
            from src.core.evidence_gate import apply_evidence_gate, unsupported_paragraphs
            gate_mark = "（未核实）"
            pairs = ("【PIN3 侧向化】→【背光侧分布】\n\n"
                     "依据：单侧蓝光诱导 PIN3 向背光侧极性化[1]。\n\n"
                     "【NPH3】→【光依赖去磷酸化后释放】\n\n"
                     "依据：蓝光依赖的磷酸化决定 NPH3 的质膜定位[2]。")
            if gate_mark in apply_evidence_gate(pairs, True, True):
                fails.append("a statement followed by its 依据 line must count as supported")
            lone = "【PIN3 侧向化】在 2019 年被证实显著增强生长素的不对称分布。"
            if apply_evidence_gate(lone, True, True).count(gate_mark) != 1:
                fails.append("an uncited high-risk paragraph must be marked exactly once")
            # 6b-2) 编号在组内**哪个位置**不影响判定：引用一段原文、紧接着据此推论，
            #      同属一组、同判"已落来源"。旧实现遇到编号就断组，把这种写法错标成
            #      "（未核实）"（真实回答里"原文明确：…"[11] 之后一段推论被标过一次）。
            cited_first = ('原文明确："phot1 在黑暗中不影响向地性" [1]。\n\n'
                           '也就是说：phot1 突变体的向地性丧失另有蓝光机制。')
            if gate_mark in apply_evidence_gate(cited_first, True, True):
                fails.append("a citation must cover the following paragraphs of the same group")
            if apply_evidence_gate(f"{lone}\n\n{lone}", True, True).count(gate_mark) != 1:
                fails.append("an uncited run must still be marked exactly once")
            # 6b-4) 判定与标记同源：成文后"打回去补来源"那一轮拿到的段落
            #      （unsupported_paragraphs）必须与最终被标的段落完全一致，否则会出现
            #      "被要求补来源的段落没标、标了的段落没被要求补"这种自相矛盾。
            if unsupported_paragraphs(lone) != [lone]:
                fails.append("unsupported_paragraphs must hand back the exact paragraph text")
            for label, sample in (("pairs", pairs), ("lone", lone), ("cited-first", cited_first),
                                  ("uncited run", f"{lone}\n\n{lone}")):
                marked_count = apply_evidence_gate(sample, True, True).count(gate_mark)
                if len(unsupported_paragraphs(sample)) != marked_count:
                    fails.append(f"unsupported_paragraphs and the marker disagree on {label}")
            # 6b-5) 打回指令只点名**手里真有**的工具：深度研究的子 Agent 没有
            #      cite_references / request_fulltext（它触达不到用户），照主路径措辞
            #      写会让它去调一个不存在的工具，这一轮就白烧了。
            from src.core.agent.runtime import _evidence_round_notice
            def as_tools(*names):
                return [{"type": "function", "function": {"name": n}} for n in names]
            main_notice = _evidence_round_notice(
                [lone], as_tools("cite_references", "fetch_paper_fulltext", "request_fulltext"))
            sub_notice = _evidence_round_notice([lone], as_tools("fetch_paper_fulltext"))
            if "cite_references" not in main_notice or "request_fulltext" not in main_notice:
                fails.append("the sourcing round must name the tools the main path really has")
            if "cite_references" in sub_notice or "request_fulltext" in sub_notice:
                fails.append("the sourcing round must not name tools the sub-agent lacks")
            if "fetch_paper_fulltext" not in sub_notice:
                fails.append("the sourcing round must still offer full-text retrieval")
            for label, sample in (("table", "| A | B |\n|---|---|\n| PIN3 | 2019 |"),
                                  ("code", "```python\nPIN3_2019 = 1\n```"),
                                  ("think", "<think>PIN3 2019 显著 [1]</think>")):
                if gate_mark in apply_evidence_gate(sample, True, True):
                    fails.append(f"evidence gate must not mark {label} content")
            # 多段思考块（空行分段）同样不能标：状态必须跨块延续，否则思考正文
            # 会被当成普通段落，标记直接写进 Reasoning 面板。
            multi_block_think = ("<think>\n\nThe user wants PIN3 and 2019 data.\n\n"
                                 "Let me search.\n\n</think>\n\n"
                                 "PKS1 在 2019 年被证实参与竞争[1]。")
            marked_think = apply_evidence_gate(multi_block_think, True, True).split("</think>")[0]
            if gate_mark in marked_think:
                fails.append("evidence gate wrote a marker inside the reasoning panel")

            # 6c) 工具调用步骤的"我先检索…"属于过程：收尾要折进过程面板，不能冒充正文。
            from src.core.agent.runtime import _fold_narration
            folded = _fold_narration("前言\nI'll gather evidence.\n\n## 结论\nPKS1[1]。",
                                     ["I'll gather evidence."])
            if "<mcp_process>I'll gather evidence.</mcp_process>" not in folded:
                fails.append("tool-step narration must be folded into the process panel")
            # 6d) 整轮只有思考和检索计划时，必须识别为"没有回答"（否则会留空气泡）。
            from src.task.chat_tasks import _has_visible_answer
            if _has_visible_answer("<think>only thinking</think>\n<mcp_process>x</mcp_process>"):
                fails.append("a reasoning-only turn must be detected as having no answer")
            if not _has_visible_answer("<think>t</think>\n\n真实结论。"):
                fails.append("a real answer must be recognised as visible text")
            # 6e) 取全文"同轮会合"通道：用户交回 PDF 时等待方必须立刻拿到路径并按此续跑
            #     本轮；没人等待 / 用户拒绝 / 用户取消时不得给出假路径（否则模型会对着
            #     不存在的全文编结论）。
            import threading as _threading

            from src.core import fulltext_bridge as _ftb

            _cid = "selfcheck-fulltext"
            _handle = _ftb.begin(_cid)
            _threading.Thread(
                target=lambda: _ftb.signal(_cid, True, "/tmp/selfcheck-paper.pdf"),
                daemon=True).start()
            _ok, _path, _note = _ftb.wait(_cid, _handle, timeout=5.0)
            if not (_ok and _path == "/tmp/selfcheck-paper.pdf"):
                fails.append(f"full-text rendezvous lost the PDF: ok={_ok} path={_path!r} note={_note!r}")
            if _ftb.pending(_cid):
                fails.append("a consumed full-text wait must be released")
            if _ftb.signal(_cid, True, "/tmp/nobody-waits.pdf"):
                fails.append("signalling a full-text result without a waiter must report failure")
            _handle = _ftb.begin(_cid)
            _threading.Thread(
                target=lambda: _ftb.signal(_cid, False, "", "no file"),
                daemon=True).start()
            if _ftb.wait(_cid, _handle, timeout=5.0)[0]:
                fails.append("a refused full-text request must not report a delivered PDF")
            _handle = _ftb.begin(_cid)
            if _ftb.wait(_cid, _handle, timeout=5.0, cancel_check=lambda: True)[2] != "cancelled by the user":
                fails.append("cancelling while waiting for the PDF must return the user-cancel reason")
            _ftb.release(_cid)

            # 7) 标题斜体：**默认正体**，只有原标题自带的斜体信号才保留。整条标题斜体
            #    既不符合学术惯例（斜体只落在物种名/基因名/刊名一类局部成分上），也让
            #    整片参考文献糊成一片。另有一处易错顺序：先包裹再转义会把 <i> 变成可见的
            #    &lt;i&gt;——"整条斜体"与"标签当正文显示"两种毛病同源，故一并钉住。
            from src.core.references import title_html
            plain_title = title_html("Phototropin blue-light receptors")
            if "<i>" in plain_title:
                fails.append(f"an unmarked title must render upright: {plain_title!r}")
            signals = {
                "JATS <i>": ("Cryo-EM of <i>Arabidopsis thaliana</i> NPH3",
                             "Cryo-EM of <i>Arabidopsis thaliana</i> NPH3"),
                "<em>": ("Growth of <em>Zea mays</em>", "Growth of <i>Zea mays</i>"),
                "<italic …>": ('Role of <italic id="x">PKS1</italic>',
                               "Role of <i>PKS1</i>"),
                "LaTeX": (r"Signalling in \textit{Arabidopsis}",
                          "Signalling in <i>Arabidopsis</i>"),
                "Markdown": ("Growth of *A. thaliana*", "Growth of <i>A. thaliana</i>"),
            }
            for label, (src, expect) in signals.items():
                got = title_html(src)
                if got != expect:
                    fails.append(f"{label} italic signal not preserved: {got!r}")
            if title_html("Unclosed <i>tag title") != "Unclosed tag title":
                fails.append("stray italic markup must be dropped, not shown as body text")
            if title_html("a < b & c") != "a &lt; b &amp; c":
                fails.append("title escaping must stay injection-safe")
            if "NPH3_R1" not in title_html("Gene NPH3_R1 and DOI 10.1/a_b"):
                fails.append("underscores must not be read as an italic signal")
            # Markdown 粗体（``**x**``）不是斜体信号：判定口径见 _TITLE_ITALIC_RE。
            if title_html("A **x** B").count("<i>") != 0:
                fails.append("double asterisks must not be read as an italic signal")

            # 著录行同样默认正体，且句末句点必须落在斜体之外
            upright = ReferenceItem(index=5, title="Phototropin blue-light receptors",
                                    authors="Christie JM", year="2007")
            if "<i>" in upright.citation_text_html():
                fails.append("citation_text_html must render unmarked titles upright")
            partial = ReferenceItem(index=6, title="Cryo-EM of <i>A. thaliana</i> NPH3",
                                    authors="Sullivan S", year="2021")
            partial_html = partial.citation_text_html()
            if "<i>A. thaliana</i> NPH3." not in partial_html:
                fails.append(f"the trailing period must fall outside the italic span: "
                             f"{partial_html!r}")

            # 引用卡与页脚必须共用同一个渲染器：两处各写一套判定，迟早会分叉
            try:
                from src.ui.components import citation_popup
                if citation_popup.title_html is not title_html:
                    fails.append("the citation card must reuse references.title_html")
            except Exception as e:
                fails.append(f"citation card title wiring check failed: {e}")
        except Exception as e:
            fails.append(f"exception: {e}")

        if fails:
            for f in fails:
                self._log(f"FAIL: {f}", "FAIL")
            self._log(f"Reference integrity test FAILED ({len(fails)} assertion(s)).", "FAIL")
            return
        self._log("Numbering never creates, replaces or mutates registry entries.", "OK")
        self._log("Every listed item traces back to a registered entry with provenance.", "OK")
        self._log("Numbers no tool assigned are reported, never turned into entries.", "OK")
        self._log("Model-supplied numbers are ignored; the registry assigns them.", "OK")
        self._log("Unverified mark: shown only for entries with no DOI/URL/path and no "
                  "verbatim-checked snippet; footer export round-trip intact.", "OK")
        self._log("Titles render upright; italics kept only where the source itself "
                  "signalled them (JATS/LaTeX/Markdown), shared by footer and card.", "OK")
        self._log("Note: a FABRICATED DOI/URL still looks 'identified' — only "
                  "Literature Trust (live) can disprove it over the network.", "WARN")
        self._log("Reference integrity test PASSED.", "OK")

    def _test_storage_roundtrip(self, clear: bool = True):
        """存储往返自检：历史库与导出归档都必须原样带回引用、书签、组件与标签。

        这是"参考文献不能在历史 / 导入导出里丢"的机器可验证版本，覆盖三层：

        1. **历史记录**（``chat_session``，SQLite）：``save_tab`` → ``load_tab``
           之后 ``references`` / ``bookmarks`` / ``ui_components`` / ``content``
           与写入前逐字段相等，标签也能读回；
        2. **导出归档**（``.schat``，SQLite 容器）：``write_archive`` →
           ``read_archive`` → ``extract_tabs`` 同样逐字段相等，且本地文件被内嵌
           后重新解到内容寻址仓库（换机器仍能打开）；
        3. **旧容器兼容**：``archive_version`` 1（纯 JSON）与 4（ZIP + assets）
           仍能被 ``read_archive`` 读出，参考文献不丢。

        测试全程使用临时目录（临时改写各模块的 ``BASE_DIR``），结束后恢复：
        自检不许在用户的真实工作区里留下数据库或资产副本。
        """
        if clear:
            self._clear()
        self._log("--- Storage Round-Trip (history DB / archive / legacy) ---", "INFO")
        fails = []
        temp_root = tempfile.mkdtemp(prefix="scholar_navis_storage_")
        try:
            import zipfile

            from src.core import chat_archive, chat_assets, chat_session, chat_store

            saved_base = {module: module.BASE_DIR
                          for module in (chat_archive, chat_assets, chat_session)}
            for module in saved_base:
                module.BASE_DIR = temp_root
            try:
                fails.extend(self._storage_assertions(
                    temp_root, chat_archive, chat_assets, chat_session, chat_store,
                    zipfile))
            finally:
                for module, base in saved_base.items():
                    module.BASE_DIR = base
        except Exception as e:
            fails.append(f"exception: {e}")
        finally:
            import shutil
            shutil.rmtree(temp_root, ignore_errors=True)

        if fails:
            for item in fails:
                self._log(f"FAIL: {item}", "FAIL")
            self._log(f"Storage round-trip test FAILED ({len(fails)} assertion(s)).", "FAIL")
            return
        self._log("History DB keeps references / bookmarks / cards / tags verbatim.", "OK")
        self._log("Archive (SQLite .schat) keeps them verbatim and re-extracts assets.", "OK")
        self._log("Legacy JSON (v1) and ZIP (v4) archives still read with references.", "OK")
        self._log("Storage round-trip test PASSED.", "OK")

    @staticmethod
    def _storage_assertions(temp_root, chat_archive, chat_assets, chat_session,
                            chat_store, zipfile):
        """存储往返的实际断言（抽成静态方法，便于读、也便于单独调用）。"""
        fails = []
        ref = {
            "index": 1, "kind": "article", "title": "Phototropin blue-light receptors",
            "authors": "Christie, J. M.", "year": "2007", "journal": "Annu Rev Plant Biol",
            "doi": "10.1146/annurev.arplant.58.032806.103951",
            "url": "https://doi.org/10.1146/annurev.arplant.58.032806.103951",
            "snippet": "Phototropins are blue-light receptors ...", "verified": True,
            "note": "backs the claim", "asset_placeholder": {"status": "missing", "name": "x.pdf"},
        }
        bookmark = {"id": "abc123def456", "title": "关键结论", "text": "claim [1]",
                    "color": "#05B8CC", "occurrence": 0, "created": 1.0}
        cards = [{"kind": "ask_user", "key": "probe", "state": {"selected": ["A"]}}]
        # 正文刻意超过压缩阈值：库内 zlib 路径也要被这条断言覆盖到。
        usage = {"prompt_tokens": 1234, "completion_tokens": 567,
                 "estimated": True, "elapsed_ms": 3400.0}
        message = {"role": "assistant", "content": "claim [1]. " + "长文" * 800,
                   "references": [dict(ref)], "bookmarks": [dict(bookmark)],
                   "ui_components": cards, "status": "ok", "usage": dict(usage)}

        # 1) 历史库往返
        tab_id = "c" * 32
        chat_session.save_manifest(tab_id, [{"id": tab_id, "name": "Storage probe"}])
        if not chat_session.save_tab(tab_id, [message], tags=["植物", "光信号"]):
            fails.append("history: save_tab reported failure")
        back = chat_session.load_tab(tab_id)
        if not back:
            fails.append("history: load_tab returned nothing")
        else:
            got = back[0]
            if got.get("references") != [ref]:
                fails.append(f"history: references changed -> {got.get('references')}")
            if got.get("bookmarks") != [bookmark]:
                fails.append("history: bookmarks changed")
            if got.get("ui_components") != cards:
                fails.append("history: ui_components changed")
            if got.get("usage") != usage:
                fails.append(f"history: token usage lost -> {got.get('usage')}")
            if got.get("content") != message["content"]:
                fails.append("history: content changed (truncated or unescaped?)")
        if chat_session.load_tab_tags(tab_id) != ["植物", "光信号"]:
            fails.append("history: tags were not stored")
        if chat_session.tabs_with_tag("植物") != [tab_id]:
            fails.append("history: tag query does not return the conversation")


        # 2) 归档往返（含资产内嵌 / 解包）
        image_path = os.path.join(temp_root, "figure.png")
        with open(image_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 256)
        archived_message = dict(message)
        archived_message["external_files"] = [
            {"type": "image", "path": image_path, "name": "figure.png"}]
        archive_path = os.path.join(temp_root, "probe.schat")
        chat_archive.write_archive(archive_path, [{
            "name": "Storage probe", "tags": ["植物"],
            "messages": [archived_message]}])
        if not chat_store.is_sqlite_file(archive_path):
            fails.append("archive: container is not SQLite")
        payload, meta = chat_archive.read_archive(archive_path)
        if meta.get("container") != "sqlite":
            fails.append(f"archive: container reported as {meta.get('container')!r}")
        tabs = chat_archive.extract_tabs(payload)
        if not tabs:
            fails.append("archive: no tab extracted")
        else:
            tab = tabs[0]
            if tab.get("name") != "Storage probe":
                fails.append(f"archive: tab name lost -> {tab.get('name')!r}")
            if tab.get("tags") != ["植物"]:
                fails.append(f"archive: tags lost -> {tab.get('tags')}")
            msg = (tab.get("messages") or [{}])[0]
            if msg.get("references") != [ref]:
                fails.append("archive: references changed in round-trip")
            if msg.get("bookmarks") != [bookmark]:
                fails.append("archive: bookmarks changed in round-trip")
            if msg.get("ui_components") != cards:
                fails.append("archive: ui_components changed in round-trip")
            if msg.get("usage") != usage:
                fails.append(f"archive: token usage lost -> {msg.get('usage')}")
            if msg.get("content") != message["content"]:
                fails.append("archive: content changed in round-trip")
        os.remove(image_path)
        attachment_map = meta.get("attachment_map") or {}
        if not attachment_map:
            fails.append("archive: local file was not embedded")
        elif not all(os.path.isfile(path) and chat_assets.is_asset_store_path(path)
                     for path in attachment_map.values()):
            fails.append("archive: embedded file was not restored into the asset store")

        # 3) 旧容器兼容：v1 纯 JSON
        legacy_json = os.path.join(temp_root, "legacy.json")
        with open(legacy_json, "w", encoding="utf-8") as f:
            json.dump({"messages": [{"role": "assistant", "content": "old [1]",
                                     "references": [dict(ref)]}]}, f, ensure_ascii=False)
        payload, meta = chat_archive.read_archive(legacy_json)
        old_tabs = chat_archive.extract_tabs(payload)
        if meta.get("container") != "json" or not old_tabs:
            fails.append("legacy: plain-JSON archive is no longer readable")
        elif old_tabs[0]["messages"][0].get("references") != [ref]:
            fails.append("legacy: references lost from a plain-JSON archive")

        # 4) 旧容器兼容：v4 ZIP + 内容寻址资产
        data = b"\x89PNG\r\n\x1a\n" + b"1" * 128
        digest = hashlib.sha256(data).hexdigest()
        legacy_zip = os.path.join(temp_root, "legacy.schat")
        with zipfile.ZipFile(legacy_zip, "w") as zf:
            zf.writestr(f"assets/{digest}.png", data)
            zf.writestr(chat_archive.ENTRY_JSON, json.dumps({
                "format": chat_archive.FORMAT, "version": 1, "archive_version": 4,
                "tabs": [{"name": "legacy", "messages": [{
                    "role": "assistant", "content": "zip [1]",
                    "references": [dict(ref)]}]}],
                "assets": {"/old/figure.png": {"entry": f"assets/{digest}.png",
                                               "sha256": digest, "name": "figure.png",
                                               "size": len(data)}},
                "placeholders": {}, "attachments": {}}, ensure_ascii=False))
        payload, meta = chat_archive.read_archive(legacy_zip)
        if meta.get("container") != "zip" or int(meta.get("archive_version") or 0) != 4:
            fails.append(f"legacy: ZIP container misdetected -> {meta}")
        if not (meta.get("attachment_map") or {}):
            fails.append("legacy: ZIP assets were not extracted")
        if not chat_archive.extract_tabs(payload)[0]["messages"][0].get("references"):
            fails.append("legacy: references lost from a ZIP archive")

        # 5) "清除对话"必须真的清掉落盘内容：只删内存不删快照，重启后会复活。
        chat_session.clear_tab(tab_id)
        if chat_session.load_tab(tab_id):
            fails.append("history: messages survived clear_tab")
        return fails

    def _test_citation_click(self, clear: bool = True):
        """引文点击链路测试：离线断言 + 注入真实 Chat 面板供手动点击。

        Part A（离线）逐段钉住"引用能点、点了有内容、换机器也有内容"这条链路：

        1. 行内 ``[n]`` 被真实渲染管线改写成 ``ref://cite?n=`` 链接（悬停 / 点击入口），
           且重复出现的同一编号每一处都带链接；
        2. 悬停卡与详情面板的数据来源按 ``(气泡编号, 引用编号)`` 命中**同一条目**；
        3. 归档序列化（sanitize）往返后条目一字不差——防"导出再导入引用卡变空"
          （完整的历史库 / 归档落盘往返见 Storage Round-Trip 自检）；
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

        # 1) 行内 [n] → ref://cite?n= 链接；工具 token（原样粘贴的超链接）必须归一成
        #    同一形态，不得产生嵌套锚点或丢失可点击性。
        try:
            from src.ui.components.text_formatter import TextFormatter
            html = TextFormatter.format_response(CITATION_TEST_AI, 0, set(), set(), {})
            missing = [n for n in (1, 2, 3, 4) if f"ref://cite?n={n}" not in html]
            if missing:
                fails.append(f"inline [n] not linked: {missing}")
            if html.count("ref://cite?n=1") < 2:
                fails.append("reused citation [1] was not linked at every occurrence")
            if html.count("ref://cite?n=2") < 2:
                fails.append("pasted citation token was not kept as one clickable link")
            if re.search(r"<a[^>]*>\s*<a\b", html, re.IGNORECASE):
                fails.append("pasted citation token produced nested anchors")
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

        # 4) 导入规范化必须保留 references / ui_components / bookmarks
        try:
            from src.task.chat_tasks import ImportChatHistoryTask
            comps = [{"kind": "ask_user", "key": "probe",
                      "state": {"selected": ["A"], "custom": ""}}]
            marks = [{"id": "deadbeef0001", "title": "probe", "text": "x"}]
            norm = ImportChatHistoryTask._normalize_msg(
                {"role": "assistant", "content": "x",
                 "references": refs, "ui_components": comps, "bookmarks": marks})
            if norm.get("references") != refs:
                fails.append("import normalization dropped or altered references")
            if norm.get("ui_components") != comps:
                fails.append("import normalization dropped or altered ui_components")
            if norm.get("bookmarks") != marks:
                fails.append("import normalization dropped or altered bookmarks")
        except Exception as e:
            fails.append(f"import normalization: {e}")

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
            self._log("Import normalization keeps references + ui_components + bookmarks: OK", "OK")
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
            page = viewer._build_page_html()
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

    def _test_history_restore(self, clear: bool = True):
        """历史回灌自检：分片推进契约 + 加载遮罩 + 快照写入闸口。

        起因：恢复上次对话曾整段同步跑在 UI 线程（逐条重建气泡、渲染正文），期间事件
        循环不转——窗口"启动即冻结"，任何加载动画都停在第一帧。修好之后有三处必须
        一直成立，否则会退化成"看着像好了、其实又卡了"或"数据被写坏"：

        1. **分片推进**：``_apply_imported_history`` 只做同步前置，逐条重建交给
           ``_rebuild_history_step`` 按时间预算分片、片间让出事件循环；收尾一律走
           ``on_done`` 回调。这两个形参一旦被改掉，完成提示（导入完成对话框 /
           "已恢复 N 条"）会静默消失——没人会立刻发现。
        2. **加载遮罩**：小会话不闪现（延迟亮相）、跟随宿主尺寸、可重复 start/stop。
        3. **快照闸口**：回灌期间 ``self.history`` 只是"已重建的一部分"，此刻落盘会用
           截断的会话覆盖磁盘上那份完整快照（而回灌数据正是从它读出来的）。因此
           ``_autosave_session`` 与 ``flush_session_snapshot`` 都必须被拦住。

        第 1、3 项是纯 Python 断言；第 2 项用真实控件探针（``WA_DontShowOnScreen``，
        不弹窗）。整轮只读：第 3 项把落盘函数临时换成记录器，绝不写用户数据。
        """
        if clear:
            self._clear()
        self._log("--- History Restore (chunked replay / loading overlay / snapshot gate) ---",
                  "INFO")
        fails = []
        warns = []

        def record(name, ok):
            self._log(f"{name}: {'OK' if ok else 'FAIL'}", "OK" if ok else "FAIL")
            if not ok:
                fails.append(name)

        # 1) 分片推进契约
        try:
            import inspect

            from src.tools.chat_mixins.attachments import ChatAttachmentsMixin

            params = inspect.signature(
                ChatAttachmentsMixin._apply_imported_history).parameters
            record("replay takes on_done / progress_text for asynchronous callers",
                   {"on_done", "progress_text"} <= set(params))
            record("replay exposes its shard steps",
                   all(callable(getattr(ChatAttachmentsMixin, name, None)) for name in
                       ("_rebuild_history_step", "_rebuild_history_message",
                        "_finish_history_rebuild")))
            budget = ChatAttachmentsMixin._HISTORY_REBUILD_SLICE_MS
            record("shard budget stays under one frame (keeps the UI repainting)",
                   0 < budget < 16)
        except Exception as e:
            record(f"replay contract check raised {e}", False)

        # 2) 快照闸口：用最小替身调用**真实**方法，落盘函数临时换成记录器
        try:
            from src.core import chat_session
            from src.tools.chat_mixins.session import ChatSessionMixin

            calls = []
            real_save = chat_session.save_tab
            real_clear = chat_session.clear_inflight
            chat_session.save_tab = lambda tab_id, history: (calls.append("save"), True)[1]
            chat_session.clear_inflight = lambda tab_id: calls.append("clear")
            try:
                class _SnapshotProbe:
                    """最小替身：只提供两个方法真正用到的那点状态。"""

                    widget = object()       # flush_session_snapshot 的前置判据
                    tab_id = "0" * 32       # 快照按标签页落盘

                    def __init__(self, rebuilding):
                        self.history = [{"role": "user", "content": "probe"}]
                        self._history_rebuild_state = {} if rebuilding else None

                    def _sync_all_bubble_cards(self):
                        calls.append("sync_cards")

                _SnapshotProbe._snapshot_writable = ChatSessionMixin._snapshot_writable
                _SnapshotProbe._tab_id = ChatSessionMixin._tab_id

                ChatSessionMixin._autosave_session(_SnapshotProbe(False))
                record("a finished conversation is still flushed to disk",
                       calls == ["sync_cards", "save", "clear"])

                calls.clear()
                busy = _SnapshotProbe(True)
                ChatSessionMixin._autosave_session(busy)
                ChatSessionMixin.flush_session_snapshot(busy)
                record("no snapshot is written while a replay is in flight",
                       calls == [])
            finally:
                chat_session.save_tab = real_save
                chat_session.clear_inflight = real_clear
        except Exception as e:
            record(f"snapshot gate check raised {e}", False)

        # 3) 加载遮罩：真实控件探针。断言一律用 isHidden() 而非 isVisible()——宿主不在
        #    屏幕上时 isVisible() 恒为 False，用它断言会得到假失败（同类坑见
        #    UI Copy / Drag-Scroll 里的滚动范围断言）。
        try:
            import time as _time

            from PySide6.QtCore import QEvent
            from PySide6.QtWidgets import QApplication, QWidget

            from src.ui.components.RotatingSpinner import LoadingOverlay

            host = QWidget()
            host.setAttribute(Qt.WA_DontShowOnScreen, True)     # 自检不弹窗
            host.resize(320, 200)
            host.show()
            overlay = LoadingOverlay(host, "restoring")
            try:
                record("the overlay starts hidden and declares a reveal delay",
                       overlay.REVEAL_DELAY_MS > 0 and overlay.isHidden())

                overlay.start("restoring")
                record("a pending reveal keeps the overlay hidden (no flash)",
                       overlay.isHidden() and overlay._reveal_timer.isActive())

                deadline = _time.monotonic() + 2.0
                while _time.monotonic() < deadline and overlay.isHidden():
                    QApplication.processEvents()
                    _time.sleep(0.01)
                record("the overlay appears and animates once the delay elapses",
                       not overlay.isHidden() and overlay.spinner.timer.isActive()
                       and not overlay._reveal_timer.isActive())

                host.resize(640, 480)
                for _ in range(3):
                    QApplication.processEvents()
                if overlay.size() != host.size():
                    # 有的 Qt 构建不给"未上屏的可见控件"派发 resize 事件。此时直接驱动
                    # 同一个过滤器入口来判定几何跟随，避免把环境差异记成产品缺陷；但也
                    # 如实说明"真实事件投递"这一环本次没被覆盖。
                    overlay.eventFilter(host, QEvent(QEvent.Type.Resize))
                    warns.append("resize events are not delivered to off-screen probes "
                                 "in this environment; overlay geometry was verified "
                                 "through its filter entry point instead")
                record("the overlay fills its host and follows resizes",
                       host.width() == 640 and host.height() == 480
                       and overlay.size() == host.size())

                overlay.start("still loading")
                record("a second start() only swaps the text",
                       not overlay.isHidden() and overlay.label.text() == "still loading")

                overlay.stop()
                overlay.stop()      # 必须可重复调用
                record("stop() hides the overlay and stops its animation",
                       overlay.isHidden() and not overlay.spinner.timer.isActive())
            finally:
                overlay.deleteLater()
                host.deleteLater()
        except Exception as e:
            record(f"loading overlay check raised {e}", False)

        for w in warns:
            self._log(f"WARN: {w}", "WARN")
        self._log("History restore OK." if not fails else
                  f"History restore FAILED: {', '.join(fails)}",
                  "OK" if not fails else "FAIL")

    def _test_literature_source_trust(self, clear: bool = True):
        """文献检索可信性检查（**联网**）：真实检索 -> 逐条核查 -> 核查表格。

        本方法只负责"起线程 + 交代活儿"，核查在
        :meth:`_literature_trust_check` 中执行。原因：整条链路都是**阻塞式网络调用**
        （一次检索 + 每条记录一次 DOI 解析 + 一次链接可达性检查，单条超时上限合计可达
        百秒级），跑在 UI 线程上会让窗口在这段时间里完全不响应。

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
        if self._littrust_thread is not None and self._littrust_thread.is_alive():
            self._log("Literature Trust is already running; wait for it to finish.", "WARN")
            return
        if clear:
            self._clear()
        self._log("--- Literature Source Trust (live retrieval) ---", "INFO")
        self._log("Running on a background thread — the window stays responsive.", "INFO")
        self.btn_littrust.setEnabled(False)
        self.btn_littrust.setText(LITTRUST_BTN_RUNNING)
        self._littrust_thread = threading.Thread(
            target=self._literature_trust_worker,
            name="dev-literature-trust",
            daemon=True,
        )
        self._littrust_thread.start()

    def _on_littrust_finished(self):
        """后台自检收尾（主线程槽）：恢复按钮文字与可用状态。"""
        self.btn_littrust.setEnabled(True)
        self.btn_littrust.setText(LITTRUST_BTN_IDLE)
        self._log("Literature Trust finished.", "INFO")

    def _literature_trust_worker(self):
        """后台线程外壳：保证异常被记录、按钮一定被恢复。"""
        try:
            self._literature_trust_check()
        except Exception as e:      # 后台线程的异常不该只留在 stderr
            logger.exception("Literature trust check crashed.")
            self._emit_dev_log(f"Literature Trust crashed: {e}", "FAIL")
        finally:
            try:
                self._dev_stream.sig_finished.emit()
            except RuntimeError:
                pass    # 对话框已销毁

    def _literature_trust_check(self):
        """联网核查实现：**运行在后台线程**，只能经 ``_emit_dev_log`` 输出。"""
        query = "NPH3 phototropism Arabidopsis"
        limit = 5
        self._emit_dev_log(f"Query: {query!r} | max_results={limit}", "INFO")

        try:
            from src.core.academic.literature import search_academic_literature
        except Exception as e:
            self._emit_dev_log(f"Cannot import literature tools: {e}", "FAIL")
            return

        try:
            raw = search_academic_literature(query=query, max_results=limit)
        except Exception as e:
            self._emit_dev_log(f"Retrieval failed (network/proxy?): {e}", "FAIL")
            return

        import json as _json

        try:
            records = (_json.loads(raw) or {}).get("results") or []
        except (ValueError, AttributeError):
            self._emit_dev_log("Retrieval returned unparsable payload.", "FAIL")
            return
        if not records:
            self._emit_dev_log("Retrieval returned zero records — cannot verify anything.", "WARN")
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
            self._emit_dev_log(f"Citation-id injection failed: {e}", "FAIL")
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

        self._emit_dev_log("", "INFO")
        self._emit_dev_log(_col("#", 3) + _col("ID", 5) + _col("年", 6) + _col("DOI/URL", 34)
                           + _col("结论", 8) + "说明", "INFO")
        summary = {"ok": 0, "warn": 0, "suspect": 0}
        for i, (rec, ann) in enumerate(zip(records, annotated), 1):
            title = str(rec.get("title") or "")
            year = str(rec.get("year") or rec.get("publication_year") or "").strip()
            doi = str(rec.get("doi") or "")
            url = str(rec.get("url") or "")
            ref_id = ann.get("_mcp_cite_id")
            notes, verdict = [], "ok"
            # 长回答：每条记录检查前先报进度（后台线程里最慢的就是这两次网络往返）
            self._emit_dev_log(f"Checking {i}/{len(records)}: {title[:72] or '(no title)'}",
                               "INFO")

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
            self._emit_dev_log(
                _col(i, 3) + _col(ref_id if ref_id is not None else "-", 5)
                + _col(year, 6) + _col(doi or url or "-", 34)
                + _col(verdict.upper(), 8) + "; ".join(notes),
                "OK" if verdict == "ok" else ("WARN" if verdict == "warn" else "FAIL"))

        self._emit_dev_log("", "INFO")
        self._emit_dev_log(f"Checked {len(records)} record(s): "
                           f"OK={summary['ok']} WARN={summary['warn']} "
                           f"SUSPECT={summary['suspect']}",
                           "OK" if summary["suspect"] == 0 else "FAIL")
        if summary["suspect"]:
            self._emit_dev_log(
                "SUSPECT 行代表'检索结果无法被独立来源证实'——请人工核对原始记录，"
                "不要直接采信。", "FAIL")
        if summary["warn"]:
            self._emit_dev_log(
                f"{summary['warn']} record(s) could not be fully auto-verified "
                "(missing identifier / link blocked) — review the WARN rows.", "WARN")
        if not summary["suspect"] and not summary["warn"]:
            self._emit_dev_log("All retrieved sources were independently confirmed "
                               "(DOI resolved + title/year matched).", "OK")

    # ------------------------------------------------------------------ #
    #  Full text fetch (paywalled paper; needs a manual PDF download)
    # ------------------------------------------------------------------ #
    def _test_fulltext_fetch(self, clear: bool = True):
        """付费墙"获取全文"端到端自检（要人工下载 PDF，故不进 Run All）。

        链路与生产**完全同一条**：``AgentRuntime._handle_request_fulltext`` 调用注入的
        等待方 -> 真取全文窗口 -> 经 ``fulltext_bridge`` 同轮会合 -> PDF 解析入库 ->
        抽取子代理回简报。样例先取本机 RSS 缓存里的付费墙文章，本机没有订阅时回退到随
        包分发的内置样例（见 :mod:`src.core.fulltext_samples`），因此分发后照样能测。

        抽取子代理的模型调用在这里用桩替换：本按钮不消耗配额、不需要联网，只核对"送进
        子代理的提示词与库里的原文是否正确"。真实模型跑完整链路请用 AI 测试里的
        "AI: Full Text Fetch (paywall)"。
        """
        if clear:
            self._clear()
        self._log("--- Full Text Fetch (paywalled sample) ---", "INFO")

        samples, origin = resolve_samples(limit=5)
        if not samples:
            self._log("No paywalled sample available: the RSS cache has none and the "
                      "bundled sample file is unreadable.", "FAIL")
            return
        self._log(f"Sample source: {origin}", "INFO")
        for cand in samples:
            self._log(f"  candidate: {cand['title'] or '(untitled)'} "
                      f"[{cand['doi'] or cand['url'] or 'no id'}]", "INFO")
        sample = samples[0]
        payload = {
            "title": sample["title"],
            "doi": sample["doi"],
            "url": sample["url"],
            "reason": "开发者自检：付费墙取全文链路",
        }
        self._log("A picker window opens next: download the PDF in your browser, then drop "
                  "it on that window or click 'Choose PDF'. Closing it aborts the check.",
                  "INFO")
        self._log("This self-test does not remember the DOI↔PDF link, so the same paper "
                  "can be tested again (a normal conversation does remember it).", "INFO")

        from src.core.agent.runtime import AgentRuntime

        captured: dict = {}
        self._dev_fulltext_last = (False, "", "")
        runtime = self._dev_runtime(self._dev_fulltext_waiter, DEV_FULLTEXT_QUESTION)
        runtime._llm_step = self._dev_llm_stub(captured)
        started = time.time()
        try:
            result = json.loads(AgentRuntime._handle_request_fulltext(runtime, payload))
        except Exception as e:
            self._log(f"request_fulltext handler raised: {e}", "FAIL")
            return
        elapsed = time.time() - started

        handed_ok, path, note = self._dev_fulltext_last
        if result.get("status") != "success":
            self._log(f"Handler returned '{result.get('status')}': "
                      f"{str(result.get('message') or '')[:200]}", "FAIL")
            self._log(f"The chain stopped before the reading sub-agent "
                      f"({note or 'no PDF handed over'}).", "WARN")
            return
        self._log(f"PDF handed over: {path}", "INFO")

        checks = [("picker handed over the PDF",
                   bool(handed_ok and path and os.path.isfile(path)))]
        checks += self._fulltext_chain_checks(runtime, captured, result, path, payload)
        checks += self._fulltext_contract_checks(payload)
        passed = sum(1 for _name, good in checks if good)
        for name, good in checks:
            self._log(f"{'PASS' if good else 'FAIL'} · {name}", "OK" if good else "FAIL")
        self._log(f"--- Full Text Fetch: {passed}/{len(checks)} checks passed "
                  f"({elapsed:.1f}s incl. the manual download) ---",
                  "OK" if passed == len(checks) else "FAIL")

    def _dev_runtime(self, waiter, query: str):
        """构造一个只跑取全文链路的 ``AgentRuntime``（不注入模型 / 技能管理器）。

        ``_llm_step`` 由调用方按需替换：自检用桩，避免消耗配额、也不需要联网。
        """
        from src.core.agent.runtime import AgentRuntime

        runtime = AgentRuntime(main_llm=None, skill_manager=None, mcp_manager=None,
                               log_fn=self._runtime_log)
        runtime.fulltext_waiter = waiter
        runtime.current_query = query
        # 自检不读写 DOI↔PDF 关联：同一篇论文要能反复演练"请用户交文件"这条路径，
        # 也要能验证"关联记忆"确实没有被写进去。正常对话保持默认（开启关联）。
        runtime.fulltext_link_exempt = True
        return runtime

    @staticmethod
    def _dev_llm_stub(captured: dict):
        """抽取子代理的模型调用桩：记下送进去的提示词，回一段可辨识的简报。"""
        def _step(messages, tools=None, emit_token=None, is_cancelled=None):
            captured["messages"] = list(messages or [])
            return {"content": DEV_FULLTEXT_STUB_BRIEF}

        return _step

    def _runtime_log(self, level: str, msg: str):
        """把运行时的链路日志转到本控制台（取全文的关键节点都经 ``log_fn`` 出来）。"""
        noisy = str(level).upper() in ("WARNING", "ERROR")
        self._log(f"[runtime] {msg}", "WARN" if noisy else "INFO")

    def _dev_fulltext_waiter(self, payload: dict):
        """自检用的等待方：真窗口 + 真会合，与生产 ``ChatTask._wait_for_fulltext`` 等价。

        进程里只有一条 Qt 主线程，所以这里就在 UI 线程 ``exec()``：嵌套事件循环照常处理
        点击与拖拽，用户交回 PDF 的信号唤醒 ``fulltext_bridge`` 后 ``wait`` 立刻返回。
        """
        from src.core import fulltext_bridge
        from src.ui.components.fulltext_request import FullTextRequestDialog

        conversation_id = f"dev-fulltext-{int(time.time())}"
        handle = fulltext_bridge.begin(conversation_id)
        if handle is None:
            self._log("The rendezvous channel refused to open a wait point.", "FAIL")
            return False, "", "no wait handle"

        self._log("Picker opened — download the PDF in your browser, then drop it on the "
                  "window or click 'Choose PDF'.", "INFO")
        dialog = FullTextRequestDialog(self, data=payload, conversation_id=conversation_id)
        dialog.exec()
        # 交付 / 取消 / 关窗都会通知等待方，因此这里的等待几乎是即时的。
        result = fulltext_bridge.wait(conversation_id, handle,
                                      timeout=DEV_FULLTEXT_WAIT_TIMEOUT)
        self._dev_fulltext_last = result
        if result[0]:
            self._log(f"Handover acknowledged: {result[1]}", "OK")
        else:
            self._log(f"No handover: {result[2] or 'no file'}", "WARN")
        return result

    def _fulltext_chain_checks(self, runtime, captured: dict, result: dict,
                               path: str, payload: dict):
        """取全文成功后的断言：解析 / 入库 / 页码 / 与抽取子代理之间的口径。"""
        from src.core.pdf_text import extract_pdf

        checks = []
        brief = str(result.get("content") or "").strip()
        checks.append(("the reading sub-agent's brief reached the main model",
                       brief == DEV_FULLTEXT_STUB_BRIEF))

        pages_expected, _meta = extract_pdf(path)
        result_pages = int(result.get("pages") or 0)
        checks.append(("page blocks parsed out of the delivered PDF", result_pages >= 1))
        checks.append((f"page count matches the PDF ({result_pages} block(s))",
                       result_pages == len(pages_expected)))

        if payload.get("doi"):
            checks.append((f"requested DOI echoed back ({payload['doi']})",
                           str(result.get("doi") or "") == payload["doi"]))
        else:
            checks.append(("no DOI invented for a sample that has none",
                           not str(result.get("doi") or "").strip()))

        # 自检豁免：这条链路刻意不写 DOI↔PDF 关联，否则第二次点按钮会直接命中本地
        # 副本，"请用户交文件"这一步就再也测不到了。判据取"本次交回的这份文件没有被
        # 记进关联表"，而不是"该 DOI 没有任何关联"——用户此前在正常对话里关联过同一篇
        # 是合法状态，不该让自检误报失败。
        from src.core import fulltext_store
        remembered = (fulltext_store.resolve(payload.get("doi", ""),
                                             payload.get("title", ""))
                      if payload.get("doi") else "")
        still_linked = bool(remembered) and os.path.normcase(remembered) == os.path.normcase(path)
        checks.append(("the delivered PDF was NOT linked to its DOI "
                       "(developer self-test exemption)", not still_linked))

        store = runtime.evidence_store
        checks.append(("body kept in the evidence store",
                       bool(store.source_for(doi=payload.get("doi", ""),
                                             url=payload.get("url", ""),
                                             title=payload.get("title", "")))))
        quote_ok, quote_note = self._fulltext_verbatim_probe(store, path, payload)
        checks.append((f"verbatim quote from the PDF verifies ({quote_note})", quote_ok))

        messages = captured.get("messages") or []
        checks.append((f"reading sub-agent called once ({len(messages)} message(s))",
                       len(messages) == 2))
        if len(messages) != 2:
            return checks
        system = str((messages[0] or {}).get("content") or "")
        block = str((messages[1] or {}).get("content") or "")
        checks.append(("question carried into the reading sub-agent",
                       DEV_FULLTEXT_QUESTION[:32] in block))
        checks.append(("page tags kept in the analyst block",
                       bool(re.search(r"\[p\d+\]", block))))
        checks.append((f"page budget reported ({len(pages_expected)} page(s) total)",
                       "Pages sent:" in block and f"of {len(pages_expected)}" in block))
        used = re.search(r"\((\d+) chars", block)
        checks.append(("page selection stayed inside the analyst budget",
                       bool(used) and int(used.group(1)) <= runtime._FULLTEXT_ANALYST_CHARS))
        checks.append(("verbatim-quote rule kept in the system prompt", "VERBATIM" in system))
        checks.append(("NOT_FOUND escape kept in the system prompt", "NOT_FOUND" in system))
        return checks

    @staticmethod
    def _fulltext_verbatim_probe(store, path: str, payload: dict):
        """从交付的 PDF 里取一句原话核对证据库能否逐字命中（引用链路的判据）。

        有意重新解析一次文件：这样"原文"来自文件本身，而不是入库时写进去的那一份，
        校验才独立可信。返回 ``(是否命中, 说明)``。
        """
        from src.core.pdf_text import extract_pdf

        pages, _meta = extract_pdf(path)
        body = "\n".join(text for _page, text in pages)
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if 40 <= len(s) <= 400]
        snippet = sentences[0] if sentences else body[:200].strip()
        if not snippet:
            return False, "the PDF yielded no text"
        hit = store.verify_snippet(snippet, doi=payload.get("doi", ""),
                                   url=payload.get("url", ""),
                                   title=payload.get("title", ""))
        detail = f"{len(snippet)}-char quote from the file"
        return bool(hit), detail if hit else f"{detail} was NOT in the store"

    def _fulltext_contract_checks(self, payload: dict):
        """没界面 / 用户不给 / 没给标识：必须如实回报，不能让模型对着空文件编结论。"""
        from src.core.agent.runtime import AgentRuntime

        checks = []
        result = json.loads(AgentRuntime._handle_request_fulltext(
            self._dev_runtime(None, DEV_FULLTEXT_QUESTION), payload))
        checks.append(("without a UI the tool reports 'unavailable'",
                       result.get("status") == "unavailable"))
        checks.append(("without a UI the model is told the user cannot hand over a file",
                       "cannot hand over" in str(result.get("message") or "").lower()))

        refused = self._dev_runtime(lambda _payload: (False, "", "dev: user refused"),
                                    DEV_FULLTEXT_QUESTION)
        result = json.loads(AgentRuntime._handle_request_fulltext(refused, payload))
        checks.append(("a refusal reports 'unavailable' and forbids asking again",
                       result.get("status") == "unavailable"
                       and "do not ask again" in str(result.get("message") or "").lower()))

        result = json.loads(AgentRuntime._handle_request_fulltext(
            self._dev_runtime(lambda _payload: (True, "/nonexistent-dev.pdf", ""),
                              DEV_FULLTEXT_QUESTION), {}))
        checks.append(("a request with no identifier at all is rejected as an error",
                       result.get("status") == "error"))
        return checks

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
        # 生信工具层：清单/解析/分类/help 前置/端到端，离线可跑
        self._test_bioenv(clear=False)
        self._test_provenance(clear=False)
        self._test_literature_merge(clear=False)
        self._test_reference_citations(clear=False)
        # 反捏造审计：纯函数断言，离线可跑，必须进默认自检
        self._test_reference_integrity(clear=False)
        # 存储往返：历史库 / 归档 / 旧容器兼容，全部在临时目录里跑
        self._test_storage_roundtrip(clear=False)
        # UI 交互自检：图表复制、悬停卡标记、超宽块拖选自动滚动
        self._test_ui_copy_scroll(clear=False)
        # 历史回灌：分片推进契约、加载遮罩、快照闸口（纯断言 + 控件探针，离线）
        self._test_history_restore(clear=False)
        self._test_image_pipeline(clear=False)
        self._test_deep_plan_card(clear=False)
        self._test_hitl_pipeline(clear=False)
        self._log("=== All functional tests finished ===", "INFO")

    def _test_bioenv(self, clear: bool = True):
        """生信工具层自检：清单 → 解析 → 分类 → help 前置 → 端到端执行。

        全部离线、只读用户数据；端到端用的是临时目录里的合成 FASTA，跑完即删。
        私有前缀的创建会联网下载数百 MB，因此单独成按钮、不在默认自检里触发。
        """
        if clear:
            self._clear()
        self._log("--- Bioinformatics Tool Layer ---", "INFO")
        try:
            from src.core.bioenv import catalog, help as help_mod, prefix, resolver, tools, wsl
            from src.core.toolchain import micromamba as mamba

            cat = catalog.load()
            platform = mamba.runtime_platform()
            self._log(f"platform: {platform}", "INFO")

            if cat.problems:
                for problem in cat.problems:
                    self._log(f"catalog: {problem}", "FAIL")
            else:
                self._log(f"catalog: {len(cat.tools)} tool(s), "
                          f"{len(cat.prefix_names())} prefix(es), no problems", "OK")

            reachable = cat.for_reachable(platform)
            locations = resolver.resolve_all(cat, platform)
            level = "OK" if locations else ("WARN" if reachable else "INFO")
            self._log(f"resolution: {len(locations)}/{len(reachable)} reachable", level)
            for entry in reachable:
                location = locations.get(entry.name)
                if location is not None:
                    self._log(f"  {entry.name:<10} {location.source:<8} "
                              f"{location.version or '?'}", "OK")
                else:
                    self._log(f"  {entry.name:<10} {'--':<8} "
                              f"{resolver.unavailable_reason(entry, platform)}", "WARN")

            native = [e.name for e in reachable if e.native_on(platform)]
            if native:
                self._log(f"native vendor build available on {platform}: "
                          f"{', '.join(native)} (detected from PATH, not installable "
                          f"into the private prefix)", "INFO")

            status = wsl.detect()
            level = "OK" if (status.usable or status.state == wsl.STATE_NOT_APPLICABLE) else "WARN"
            self._log(f"wsl: {wsl.describe(status)}", level)

            created = prefix.list_prefixes()
            if created:
                for name in created:
                    ready = prefix.is_ready(name)
                    self._log(f"prefix '{name}': {'ready' if ready else 'incomplete'}, "
                              f"{prefix.size_bytes(name) / 1048576:.1f} MB",
                              "OK" if ready else "WARN")
            else:
                self._log("prefixes: none created yet (system installs are preferred)", "INFO")

            cached = help_mod.cached_tools()
            if cached:
                detail = ", ".join(f"{name}:{help_mod.total_lines(name)}" for name in cached)
                self._log(f"help cache: {detail}", "OK")
            else:
                self._log("help cache: empty (captured on first use of each tool)", "INFO")

            self._check_bioenv_skills()
            self._bioenv_end_to_end(locations.get("seqkit"), tools)
        except Exception as e:
            self._log(f"bioenv test failed: {e}", "FAIL")

    def _check_bioenv_skills(self):
        """确认工具已注册为内置 skill，且带过滤分类标签、参数类型未退化。"""
        from src.core.bioenv.catalog import TOOL_FILTER_TAG
        from src.core.skill_manager import SkillManager

        sm = SkillManager.get_instance()
        names = sorted(getattr(sm, "bio_skill_names", ()))
        if not names:
            self._log("skills: no bioinformatics tool registered", "FAIL")
            return
        self._log(f"skills registered: {', '.join(names)}", "OK")

        expected_prefix = f"[Tags: {TOOL_FILTER_TAG}]"
        for name in names:
            schema = sm.academic_schemas.get(name) or {}
            description = str((schema.get("function") or {}).get("description") or "")
            ok = description.startswith(expected_prefix)
            self._log(f"  {name}: filter tag {'ok' if ok else 'MISSING'}",
                      "OK" if ok else "FAIL")

        params = (((sm.academic_schemas.get("bio_seqkit_stats") or {})
                   .get("function") or {}).get("parameters") or {})
        types = {key: (value or {}).get("type")
                 for key, value in (params.get("properties") or {}).items()}
        # 模块带 `from __future__ import annotations` 时注解会退化成字符串，
        # max_rows 若显示 string 就说明 schema 生成又走回了按对象比较的老路。
        ok = types.get("max_rows") == "integer"
        self._log(f"schema types: {types}", "OK" if ok else "FAIL")

    def _bioenv_end_to_end(self, location, tools):
        """走一遍"help 前置 → 执行 → 结构化解析"，用合成 FASTA 验证真实链路。"""
        if location is None:
            self._log("end-to-end: skipped (seqkit is not available)", "WARN")
            return

        handle, fasta = tempfile.mkstemp(prefix="dev-bioenv-", suffix=".fa")
        os.close(handle)
        try:
            with open(fasta, "w", encoding="utf-8") as fh:
                fh.write(">s1\nACGTACGTACGT\n>s2\nTTTTAAAAGGCC\n")

            if getattr(location, "source", "") == "wsl":
                # WSL 落点的输入路径必须换成发行版内路径，否则工具只会报"文件不存在"。
                from src.core.bioenv import wsl as wsl_mod
                mapped = wsl_mod.to_wsl_path(fasta, getattr(location, "distro", "") or "")
                self._log(f"end-to-end: WSL path mapping {fasta} -> {mapped or '(failed)'}",
                          "OK" if mapped else "FAIL")

            first = json.loads(tools.bio_seqkit_stats(fasta))
            if first.get("status") == "help_required":
                self._log("end-to-end: help gate blocked the first call (expected)", "OK")
                prompted = json.loads(tools.bio_tool_help("seqkit", 5))
                self._log(f"end-to-end: bio_tool_help -> "
                          f"observed={prompted.get('observed')}",
                          "OK" if prompted.get("status") == "ok" else "WARN")
                first = json.loads(tools.bio_seqkit_stats(fasta))

            if first.get("status") != "ok":
                self._log(f"end-to-end: seqkit stats failed: {first.get('message')}", "FAIL")
                return

            rows = first.get("rows") or []
            count = rows[0].get("num_seqs") if rows else None
            ok = count == "2"
            self._log(f"end-to-end: seqkit stats -> num_seqs={count}", "OK" if ok else "FAIL")
            if not ok:
                self._log("end-to-end: expected 2 sequences in the synthetic FASTA", "FAIL")
            self._log(f"end-to-end: run dir {first.get('run_dir')}", "INFO")
        except Exception as e:
            self._log(f"end-to-end crashed: {e}", "FAIL")
        finally:
            try:
                os.remove(fasta)
            except OSError:
                pass

    def _test_bioenv_prefix(self, clear: bool = True):
        """创建私有前缀（联网）。后台线程执行，避免把对话框卡住数分钟。"""
        if clear:
            self._clear()
        self._log("--- Bioinformatics Prefix Install (network) ---", "WARN")
        self._log("This downloads micromamba and conda packages; it runs in the "
                  "background and may take several minutes.", "WARN")

        thread = getattr(self, "_bioenv_install_thread", None)
        if thread is not None and thread.is_alive():
            self._log("An install is already running.", "WARN")
            return

        def worker():
            try:
                from src.core.bioenv import catalog, prefix

                cat = catalog.load()
                for name in cat.prefix_names():
                    self._emit_dev_log(f"[bioenv] creating prefix '{name}' "
                                       f"({len(cat.prefix_packages(name))} package(s))", "INFO")
                    outcome = prefix.create(
                        name, cat.prefix_packages(name), channels=cat.channels,
                        tools=cat.tools_of(name),
                        log_cb=lambda line: self._emit_dev_log(f"[{name}] {line}", "INFO"))
                    if outcome.get("cancelled"):
                        self._emit_dev_log(f"[bioenv] prefix '{name}' cancelled", "WARN")
                        continue
                    if not outcome.get("success"):
                        self._emit_dev_log(f"[bioenv] prefix '{name}' failed: "
                                           f"{outcome.get('error')}", "FAIL")
                        continue
                    self._emit_dev_log(f"[bioenv] prefix '{name}' ready", "OK")
                    for tool, info in prefix.verify(name, cat.tools_of(name)).items():
                        available = info.get("available")
                        self._emit_dev_log(
                            f"[bioenv] {tool}: {info.get('version') or info.get('detail')}",
                            "OK" if available else "WARN")
            except Exception as e:
                self._emit_dev_log(f"[bioenv] install crashed: {e}", "FAIL")
            finally:
                self._emit_dev_log("[bioenv] install finished", "INFO")

        self._bioenv_install_thread = threading.Thread(target=worker, daemon=True)
        self._bioenv_install_thread.start()

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
        """绘图包自检：解释器可用 ≠ 能出图。

        NixOS / 精简发行版上的 R 常常只装了基础解释器（ggplot2 等需另装），
        仅报 "R found" 会给开发者一个假绿灯——真实绘图会在 R 侧 stop() 退出，
        前端只看到 plot_chart 返回 error，表现为"AI 不会画图"。

        检查范围取 :data:`src.core.plot_engine.ALLOWED_R_PACKAGES`：按图型按需
        加载的扩展包（箱线图要 ggpubr、相关图要 ggcorrplot）同样会 stop()
        退出，只查核心包无法解释"部分图型画不出来"。
        """
        from src.core import r_env
        from src.core.plot_engine import ALLOWED_R_PACKAGES
        from src.core.r_engine import package_install_guidance

        self._log(f"Private R library: {r_env.library_dir()}", "INFO")
        try:
            status = engine.check_packages(ALLOWED_R_PACKAGES)
        except Exception as e:
            self._log(f"R package check failed: {e}", "FAIL")
            return

        missing = [p for p in ALLOWED_R_PACKAGES if not status.get(p)]
        if not missing:
            self._log(f"R packages OK: {', '.join(ALLOWED_R_PACKAGES)}", "OK")
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
                                              encode_data_url,
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

        # 压平列表的还原：压缩输出会把整段列表挤成一行（"这一步。 - 证据：A； - 结论：B。"
        # 或 "。 - 证据：A"），python-markdown 因此看不到块边界，标记原样显示成正文——
        # 症状是"读起来像用横线分隔的句子，而不是列表"。修好之后必须仍然成列表，且
        # **不得**把正文里的连字符/减号/破折号或代码块内同形内容误判成列表标记。
        try:
            from src.ui.components.text_formatter import TextFormatter as _TF

            def _tag_count(html: str, tag: str) -> int:
                # 渲染管线会给标签挂内联样式（<ul style="…">），只能按前缀计数
                return html.count("<" + tag)

            glued = ("错处 1：这一步。 - 证据：A； - 结论：B。\n\n"
                     "错处 2：功能划分。- 第一条[1]；- 第二条[2]。")
            glued_html = _TF.markdown_to_html(glued)
            record("list markers glued into one line are still rendered as lists",
                   _tag_count(glued_html, "ul") == 2
                   and _tag_count(glued_html, "li") == 4)

            joined = "结论：这一步。\n1. 第一项\n2. 第二项\n3. 第三项"
            joined_html = _TF.markdown_to_html(joined)
            record("an ordered list glued to a paragraph gets its blank line",
                   _tag_count(joined_html, "ol") == 1
                   and _tag_count(joined_html, "li") == 3)

            under_heading = _TF.markdown_to_html("## 步骤\n- 取样本\n- 跑测序")
            record("a list directly under a heading stays a list",
                   _tag_count(under_heading, "ul") == 1
                   and _tag_count(under_heading, "li") == 2)

            record("list repair is idempotent",
                   _TF._repair_glued_lists(glued) == _TF._repair_glued_lists(
                       _TF._repair_glued_lists(glued)))

            # 反例：这些里面的"-"都不是列表标记，整段必须一字不改
            samples = {
                "hyphens in prose": "减法示例：3 - 2 = 1；A - B 表示差值；"
                                    "范围 10:30 - 11:00。",
                "code fence": "正文。\n\n```python\nx = 1  # 见 A。 - B\n```\n\n结尾。",
                "unsignalled dash": "普通的连字符句子 - 带连字符。",
            }
            for label, src in samples.items():
                record(f"{label} is never rewritten by the list repair",
                       _TF._repair_glued_lists(src) == src)
        except Exception as e:
            record(f"glued-list repair check raised {e}", False)

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
