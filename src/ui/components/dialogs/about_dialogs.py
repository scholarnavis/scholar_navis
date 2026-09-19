"""About-style informational dialogs: data providers and open-source licenses.

拆分自 src/ui/components/dialog.py。
"""

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout,
                               QHeaderView, QLabel, QScrollArea, QTableWidget,
                               QTableWidgetItem, QTextBrowser, QVBoxLayout,
                               QWidget)

from src.ui.components.dialogs.base import BaseDialog
from src.ui.components.dialogs.common import StandardDialog
from src.ui.components.text_formatter import TextFormatter


class ApiProvidersDialog(BaseDialog):
    def __init__(self, parent=None):
        super().__init__(parent, title="Data Providers & External APIs", width=850)
        self.setMinimumHeight(600)

        self.providers = [
            ("AlphaFold DB", "Structural Biology",
             "Comprehensive database of high-accuracy protein structure predictions developed by Google DeepMind."),
            ("ChEBI", "Metabolomics",
             "Dictionary and ontology of molecular entities focused on small chemical compounds of biological interest."),
            ("ChEMBL", "Pharmacology", "Manually curated database of bioactive molecules with drug-like properties."),
            ("Crossref", "Literature Search", "Digital Object Identifier (DOI) registration and metadata tracking."),
            ("EBI Expression Atlas", "Transcriptomics",
             "Open science resource for gene and protein expression across species and biological conditions."),
            ("Ensembl", "Genomics", "Centralized resource for genetics, molecular biology, and genomic annotations."),
            ("Europe PMC", "Preprints", "Access to life sciences publications and preprints (bioRxiv, medRxiv)."),
            ("GBIF", "Ecology & Taxonomy",
             "Global Biodiversity Information Facility providing open access to species occurrence and distribution data."),
            ("g:Profiler", "Systems Biology", "Functional enrichment analysis and gene identifier conversion tool."),
            ("GitHub API", "Code & Repositories", "Search for open-source bioinformatics pipelines and academic code."),
            ("JASPAR", "Gene Regulation",
             "Open-access database of curated, non-redundant transcription factor binding profiles."),
            ("KEGG", "Pathways", "Database resource for understanding high-level functions of the biological system."),
            ("MyGene.info & TAIR", "Genomics",
             "High-performance gene annotation API and The Arabidopsis Information Resource."),
            ("NCBI Entrez", "Genomics & Literature", "Access to PubMed, Taxonomy, SRA, GEO, and other core databases."),
            ("OpenAlex", "Literature Search", "Open catalog of the global research system and citation metrics."),
            ("PubChem", "Cheminformatics", "World's largest collection of freely accessible chemical information."),
            ("QuickGO", "Systems Biology",
             "High-performance browser and API for Gene Ontology (GO) terms and functional annotations."),
            ("RCSB PDB", "Structural Biology",
             "Information about the 3D shapes of proteins, nucleic acids, and complexes."),
            ("Search Engines (Web)", "General Web",
             "Integration with DuckDuckGo, Google, Bing, and Baidu for general internet searches."),
            ("Semantic Scholar", "Literature Search", "AI-backed academic search and citation graph traversal."),
            ("STRING DB", "Systems Biology",
             "Protein-protein interaction networks and functional enrichment analysis."),
            ("UniProt", "Protein Database", "Comprehensive resource for protein sequences, annotations, and mapping."),
            ("Unpaywall", "Literature Search",
             "Open database of free scholarly articles for fetching Open Access PDFs."),
            ("Wikipedia", "General Knowledge", "Free online encyclopedia for quick concept and entity summaries.")
        ]

        self.providers.sort(key=lambda item: item[0].lower())

        self.table = QTableWidget(len(self.providers), 3)
        self.table.setHorizontalHeaderLabels(["Data Provider", "Domain / Type", "Purpose & Description"])
        self.table.setWordWrap(True)

        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)

        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)

        for i, (pkg, domain, desc) in enumerate(self.providers):
            pkg_item = QTableWidgetItem(f" {pkg}")
            pkg_item.setForeground(QColor(self.tm.color('academic_blue')))

            domain_item = QTableWidgetItem(domain)
            domain_item.setForeground(QColor(self.tm.color('text_main')))

            self.table.setItem(i, 0, pkg_item)
            self.table.setItem(i, 1, domain_item)
            self.table.setItem(i, 2, QTableWidgetItem(desc))

        self.table.resizeRowsToContents()
        total_h = self.table.horizontalHeader().height()
        for r in range(self.table.rowCount()):
            row_h = self.table.rowHeight(r) + 24
            self.table.setRowHeight(r, row_h)
            total_h += row_h

        self.table.setFixedHeight(total_h + 10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("background: transparent;")

        scroll_content = QWidget()
        scroll_content.setStyleSheet("background: transparent;")
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(0)
        scroll_layout.addWidget(self.table)

        bottom_spacer = QWidget()
        bottom_spacer.setFixedHeight(50)
        scroll_layout.addWidget(bottom_spacer)

        scroll.setWidget(scroll_content)
        self.content_layout.addWidget(scroll)

        lbl_thanks = QLabel("Powered by the generous open APIs of the global scientific community.")
        lbl_thanks.setStyleSheet(f"color: {self.tm.color('text_muted')}; font-style: italic; font-size: 11px;")
        lbl_thanks.setAlignment(Qt.AlignCenter)
        self.content_layout.addWidget(lbl_thanks)

        self.add_button("Close", self.accept, is_primary=True)
        self._apply_theme()

    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm

        self.table.setStyleSheet(f"""
                    QTableWidget {{ 
                        background-color: transparent; 
                        border: none;
                        alternate-background-color: {tm.color('bg_input')};
                    }}
                    QHeaderView::section {{ 
                        background-color: {tm.color('bg_card')}; 
                        border-bottom: 2px solid {tm.color('border')};
                    }}
                    QTableWidget::item {{ 
                        padding: 12px; 
                        border: none;
                    }}
                """)

class LicenseDialog(BaseDialog):
    def __init__(self, parent=None):
        super().__init__(parent, title="Open Source Licenses", width=800)
        self.setMinimumHeight(600)

        self.PYTORCH_FULL_TEXT =\
        """
From PyTorch:

Copyright (c) 2016-     Facebook, Inc            (Adam Paszke)
Copyright (c) 2014-     Facebook, Inc            (Soumith Chintala)
Copyright (c) 2011-2014 Idiap Research Institute (Ronan Collobert)
Copyright (c) 2012-2014 Deepmind Technologies    (Koray Kavukcuoglu)
Copyright (c) 2011-2012 NEC Laboratories America (Koray Kavukcuoglu)
Copyright (c) 2011-2013 NYU                      (Clement Farabet)
Copyright (c) 2006-2010 NEC Laboratories America (Ronan Collobert, Leon Bottou, Iain Melvin, Jason Weston)
Copyright (c) 2006      Idiap Research Institute (Samy Bengio)
Copyright (c) 2001-2004 Idiap Research Institute (Ronan Collobert, Samy Bengio, Johnny Mariethoz)

From Caffe2:

Copyright (c) 2016-present, Facebook Inc. All rights reserved.

All contributions by Facebook:
Copyright (c) 2016 Facebook Inc.

All contributions by Google:
Copyright (c) 2015 Google Inc.
All rights reserved.

All contributions by Yangqing Jia:
Copyright (c) 2015 Yangqing Jia
All rights reserved.

All contributions by Kakao Brain:
Copyright 2019-2020 Kakao Brain

All contributions by Cruise LLC:
Copyright (c) 2022 Cruise LLC.
All rights reserved.

All contributions by Tri Dao:
Copyright (c) 2024 Tri Dao.
All rights reserved.

All contributions by Arm:
Copyright (c) 2021, 2023-2025 Arm Limited and/or its affiliates

All contributions from Caffe:
Copyright(c) 2013, 2014, 2015, the respective contributors
All rights reserved.

All other contributions:
Copyright(c) 2015, 2016 the respective contributors
All rights reserved.

Caffe2 uses a copyright model similar to Caffe: each contributor holds
copyright over their contributions to Caffe2. The project versioning records
all such contribution and copyright details. If a contributor wants to further
mark their specific copyright on a particular contribution, they should
indicate their copyright solely in the commit message of the change when it is
committed.

All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright
   notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright
   notice, this list of conditions and the following disclaimer in the
   documentation and/or other materials provided with the distribution.

3. Neither the names of Facebook, Deepmind Technologies, NYU, NEC Laboratories America
   and IDIAP Research Institute nor the names of its contributors may be
   used to endorse or promote products derived from this software without
   specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
        """

        # 收录口径（不是"装了什么都列"）：
        #   1. pyproject.toml 里声明的直接依赖；
        #   2. 会随发布产物一起分发的、提供实际功能的传递依赖（纯辅助的小包不单列）；
        #   3. 运行期需用户在本机另行安装的外部运行时 —— R 及其绘图包（见 Purpose 列）。
        # 许可证字段取自各发行包自带的 METADATA（License-Expression → License →
        # "License ::" Classifier 依次回退）；R 包取自 CRAN 官方 PACKAGES 索引。
        # Python 侧复核命令：
        #   python -c "import importlib.metadata as m;[print(d.metadata['Name'], \
        #     d.metadata.get('License-Expression') or d.metadata.get('License')) for d in m.distributions()]"
        self.licenses = [
            # ---- 应用与界面 ----
            ("PySide6 / Shiboken6", "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
             "Official Python bindings for Qt (UI framework)."),
            ("Qt WebEngine & Chromium", "LGPL-3.0 / GPL-3.0 (Qt) · BSD-3-Clause and others (Chromium)",
             "Embedded browser used by the PDF and Mermaid viewers."),
            ("PyQtDarkTheme", "MIT", "Flat dark/light theme for PySide."),
            ("Darkdetect", "BSD-3-Clause", "OS appearance detection backing the theme."),
            # ---- 数据与 AI 栈 ----
            ("ChromaDB", "Apache-2.0", "Embedded vector database for the knowledge base."),
            ("PyTorch", "BSD-3-Clause AND MIT AND Apache-2.0 (composite)",
             "Tensors and dynamic neural networks."),
            ("Transformers", "Apache-2.0", "Pretrained model loading and tokenization."),
            ("Tokenizers", "Apache-2.0", "Fast tokenizer implementations for Transformers."),
            ("HuggingFace Hub", "Apache-2.0", "Model and dataset download client."),
            ("hf-xet", "Apache-2.0", "Efficient large-file storage for Hugging Face."),
            ("Safetensors", "Apache-2.0", "Safe tensor serialization format."),
            ("Optimum / Optimum-ONNX", "Apache-2.0", "Hardware-specific model optimization."),
            ("ONNX", "Apache-2.0", "Open neural network exchange format."),
            ("ONNX Runtime", "MIT", "Cross-platform AI model accelerator."),
            ("NumPy", "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
             "Array computing foundation for the numeric stack."),
            ("SciPy", "BSD-3-Clause", "Scientific computing primitives."),
            ("scikit-learn", "BSD-3-Clause", "Clustering and dimensionality reduction."),
            ("joblib", "BSD-3-Clause", "Lightweight pipelining for NumPy workloads."),
            ("ml_dtypes", "Apache-2.0", "NumPy dtype extensions for ML frameworks."),
            ("SymPy / mpmath", "BSD-3-Clause", "Symbolic mathematics required by PyTorch."),
            ("tiktoken", "MIT", "BPE tokenizer used for context budgeting."),
            ("regex", "Apache-2.0 AND CNRI-Python", "Alternative regular expression engine."),
            ("TQDM", "MPL-2.0 AND MIT", "Progress bars for long-running tasks."),
            ("Jinja2 / MarkupSafe", "BSD-3-Clause", "Templating used by model configs."),
            ("fsspec / filelock", "BSD-3-Clause / MIT", "Filesystem abstraction and locking for model caches."),
            # ---- 文档处理 ----
            ("PyMuPDF / PyMuPDF4LLM", "AGPL-3.0 or Artifex Commercial",
             "High-performance PDF parsing and text extraction."),
            ("python-docx", "MIT", "Create and update Microsoft Word .docx files."),
            ("lxml", "BSD-3-Clause", "XML/HTML parsing backend."),
            ("BeautifulSoup4 / SoupSieve", "MIT", "Screen-scraping and CSS selector engine."),
            ("Markdown", "BSD-3-Clause", "Markdown to HTML rendering."),
            ("Pygments", "BSD-2-Clause", "Syntax highlighting for code blocks."),
            # ---- 科学数据与检索 ----
            ("BioPython", "Biopython License Agreement",
             "Tools for biological computation and sequence handling."),
            ("NetworkX", "BSD-3-Clause", "Study of complex networks and graphs."),
            ("Langdetect", "MIT", "Language detection library port."),
            ("LangChain Core / Text Splitters", "MIT", "Document chunking and LLM orchestration primitives."),
            ("LangSmith", "MIT", "Tracing and evaluation client for LangChain."),
            ("LiteLLM", "MIT", "Unified interface for various LLM providers."),
            ("OpenAI SDK", "Apache-2.0", "Client library used by compatible LLM providers."),
            ("Curl-cffi", "MIT", "TLS-impersonating HTTP client for WAF-protected sources."),
            # ---- 网络与 API 服务 ----
            ("aiohttp", "Apache-2.0 AND MIT", "Async HTTP client/server for API and search backends."),
            ("HTTPX / HTTPCore", "BSD-3-Clause", "Sync/async HTTP client stack."),
            ("HTTPX2 / HTTPCore2", "BSD-3-Clause", "Next-generation HTTP client used by LangSmith."),
            ("truststore", "MIT", "Native system trust store verification."),
            ("requests / urllib3", "Apache-2.0 / MIT", "HTTP client with connection pooling."),
            ("certifi", "MPL-2.0", "Curated CA bundle for TLS verification."),
            ("idna / charset-normalizer", "BSD-3-Clause / MIT", "IDNA and encoding handling for HTTP."),
            ("Chardet", "LGPL-2.1", "Universal character encoding detector."),
            ("h11", "MIT", "HTTP/1.1 protocol implementation."),
            ("socksio", "MIT", "SOCKS proxy support for the HTTP clients."),
            ("starlette / sse-starlette", "BSD-3-Clause", "ASGI framework and server-sent events transport."),
            ("FastAPI", "MIT", "Web framework for the local API server."),
            ("Uvicorn / uvloop / httptools", "BSD-3-Clause / MIT", "High-performance ASGI server stack."),
            ("websockets / WebSocket-client", "BSD-3-Clause / Apache-2.0", "WebSocket transports for MCP."),
            ("watchfiles", "MIT", "Efficient filesystem change watching."),
            ("anyio / sniffio", "MIT / MIT OR Apache-2.0", "Async I/O primitives shared by HTTP stacks."),
            ("multidict / yarl / frozenlist", "Apache-2.0",
             "Data structures backing the async HTTP stack."),
            ("aiohappyeyeballs", "PSF-2.0", "Happy-eyeballs connection racing for aiohttp."),
            ("MCP SDK", "MIT", "Model Context Protocol Python SDK."),
            ("PyJWT", "MIT", "JSON Web Token encoding and validation."),
            ("Email-validator / dnspython", "Unlicense / ISC",
             "Email syntax and deliverability validation."),
            ("Disposable-email-domains", "MIT", "Blocklist of disposable email domains."),
            ("python-multipart", "Apache-2.0", "Multipart form parsing for uploads."),
            ("pydantic / pydantic-core / pydantic-settings", "MIT",
             "Data validation and settings management."),
            ("Jiter", "MIT", "Fast JSON parsing for Pydantic."),
            ("orjson", "MPL-2.0 AND (Apache-2.0 OR MIT)", "High-performance JSON serialization."),
            ("JSONSchema / referencing / rpds-py", "MIT",
             "Schema validation for tool definitions."),
            ("JSONPatch / JSONPointer", "BSD-3-Clause", "JSON document diffing for MCP tool schemas."),
            ("PyYAML", "MIT", "YAML parsing for configuration files."),
            ("PyPika", "Apache-2.0", "SQL query builder used by ChromaDB."),
            ("kubernetes", "Apache-2.0", "Kubernetes client bundled with ChromaDB."),
            ("PostHog", "MIT", "Product analytics client (disabled by default)."),
            ("OpenTelemetry (API / SDK / OTLP)", "Apache-2.0",
             "Tracing and metrics for the retrieval stack."),
            ("gRPC / protobuf / flatbuffers", "Apache-2.0 / BSD-3-Clause",
             "RPC and serialization used by ONNX Runtime and telemetry."),
            ("mmh3 / xxhash", "MIT / BSD-3-Clause", "Fast non-cryptographic hashing for caches."),
            ("zstandard / pybase64 / fastuuid", "BSD-3-Clause / BSD-2-Clause",
             "Compression, encoding and ID helpers for ChromaDB."),
            ("Cryptography", "Apache-2.0 OR BSD-3-Clause",
             "Core cryptographic recipes and primitives."),
            ("cffi / pycparser", "MIT / BSD-3-Clause", "C foreign function interface for Cryptography."),
            ("six / python-dateutil", "MIT / BSD-3-Clause OR Apache-2.0",
             "Compatibility helpers and date parsing."),
            ("packaging / setuptools / build", "Apache-2.0 OR BSD-2-Clause / MIT",
             "Version handling and build-time packaging utilities."),
            ("uuid_utils", "BSD-3-Clause", "Fast UUID generation."),
            ("typing_extensions / typing-inspection", "PSF-2.0 / MIT",
             "Backported typing features for supported interpreters."),
            # ---- 桌面与系统集成 ----
            ("Psutil", "BSD-3-Clause", "Cross-platform process and system utilities."),
            ("NVIDIA-ML-PY", "BSD-3-Clause", "Python bindings for the NVIDIA Management Library."),
            ("bcrypt / SecretStorage / jeepney / keyring", "Apache-2.0 / BSD-3-Clause / MIT",
             "OS keychain integration and credential storage."),
            ("Rich / Typer / Click", "MIT / BSD-3-Clause", "Terminal formatting and CLI parsing for bundled tools."),
            ("Mermaid.js", "MIT", "Diagram and flowchart rendering in the document viewer."),
            # ---- 构建与发布工具（不随运行时功能被调用）----
            ("PyInstaller", "GPL-2.0 with bootloader exception",
             "Build-time only: freezes the application into a standalone binary."),
            ("Boto3 / botocore", "Apache-2.0",
             "Build-time only: publishes release artifacts to Cloudflare R2."),
            ("python-dotenv", "BSD-3-Clause", "Build-time only: loads local release credentials."),
            ("altgraph / pyinstaller-hooks-contrib", "MIT / Apache-2.0",
             "Build-time only: dependency graph and hooks for PyInstaller."),
            # ---- 外部运行时（用户本机安装，不随产物分发）----
            ("R (Rscript)", "GPL-2.0-or-later | GPL-3.0",
             "External runtime: statistical engine that renders the publication figures."),
            ("ggplot2", "MIT", "External R package: core plotting grammar."),
            ("dplyr", "MIT", "External R package: data manipulation in generated scripts."),
            ("tidyr", "MIT", "External R package: data reshaping for plots."),
            ("scales", "MIT", "External R package: axis and colour scale helpers."),
            ("viridis", "MIT", "External R package: perceptually uniform colour maps."),
            ("patchwork", "MIT", "External R package: multi-panel figure composition."),
            ("ragg", "MIT", "External R package: anti-aliased raster graphics device."),
            ("RColorBrewer", "Apache-2.0", "External R package: colour palettes."),
            ("pheatmap", "GPL-2", "External R package: clustered heatmaps."),
            ("ggpubr", "GPL (>= 2)", "External R package: publication-ready plot annotations."),
            ("ggrepel", "GPL-3", "External R package: non-overlapping text labels."),
            ("cowplot", "GPL-2", "External R package: figure alignment and export helpers."),
        ]

        self.licenses.sort(key=lambda item: item[0].lower())
        self.table = QTableWidget(len(self.licenses), 3)
        self.table.setHorizontalHeaderLabels(["Package", "License", "Purpose"])

        self.table.setWordWrap(True)

        # 显式列宽而非 ResizeToContents：本对话框宽度固定（BaseDialog setFixedWidth），
        # 而许可证名可能很长（如 "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only"）。
        # 实测 ResizeToContents 会把前两列撑到 260+362px，把 Purpose 列挤成 40px 的
        # 窄缝、行高被拉到 186px。固定宽度配合已开启的 WordWrap 让长文本换行，列间
        # 留白可控。
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 210)
        self.table.setColumnWidth(1, 200)

        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)

        for i, (pkg, lic, desc) in enumerate(self.licenses):
            if pkg == "PyTorch":
                link_label = QLabel(
                    f'<a href="#pytorch" style="color: {self.tm.color("accent")}; text-decoration: underline;">{pkg}</a>')
                link_label.setOpenExternalLinks(False)  # 禁止外部浏览器打开
                link_label.setCursor(Qt.PointingHandCursor)
                link_label.linkActivated.connect(self._show_pytorch_license)

                container = QWidget()
                cell_layout = QHBoxLayout(container)
                cell_layout.setContentsMargins(12, 0, 0, 0)
                cell_layout.addWidget(link_label)
                self.table.setCellWidget(i, 0, container)
            else:
                pkg_item = QTableWidgetItem(pkg)
                pkg_item.setForeground(QColor(self.tm.color('accent')))
                self.table.setItem(i, 0, pkg_item)

            self.table.setItem(i, 1, QTableWidgetItem(lic))
            self.table.setItem(i, 2, QTableWidgetItem(desc))

        self.content_layout.addWidget(self.table)

        lbl_note = QLabel(
            "Components with functional impact are listed; trivial transitive glue packages are "
            "omitted. Rows marked \u201cExternal\u201d are not bundled \u2014 the plotting engine "
            "detects a local R / Rscript installation. Rows marked \u201cBuild-time only\u201d "
            "contribute no runtime code, except the PyInstaller bootloader embedded in the "
            "executable. Some wheels additionally ship native libraries "
            "(e.g. SciPy bundles OpenBLAS and LAPACK). Licenses are as declared by each "
            "project's own package metadata.")
        lbl_note.setWordWrap(True)
        lbl_note.setStyleSheet(f"color: {self.tm.color('text_muted')}; font-size: 11px;")
        lbl_note.setAlignment(Qt.AlignLeft)
        self.content_layout.addWidget(lbl_note)

        lbl_thanks = QLabel("Thanks to all the maintainers of these incredible projects.")
        lbl_thanks.setStyleSheet(f"color: {self.tm.color('text_muted')}; font-style: italic; font-size: 11px;")
        lbl_thanks.setAlignment(Qt.AlignCenter)
        self.content_layout.addWidget(lbl_thanks)

        self.add_button("Close", self.accept, is_primary=True)
        self._apply_theme()

    def _show_pytorch_license(self):
        dlg = StandardDialog(
            self,
            title="PyTorch / Caffe2 License",
            message=self.PYTORCH_FULL_TEXT
        )

        dlg.setFixedWidth(600)
        dlg.exec()

    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm
        self.table.setStyleSheet(f"""
            QTableWidget {{ 
                background-color: transparent; 
                border: none;
                alternate-background-color: {tm.color('bg_input')};
            }}
            QHeaderView::section {{ 
                background-color: {tm.color('bg_card')}; 
                border-bottom: 2px solid {tm.color('border')};
            }}
            QTableWidget::item {{ 
                padding: 12px; 
                border: none;
            }}
        """)


class ReleaseNotesDialog(BaseDialog):
    """应用内查看更新日志（GitHub Release 的 Markdown 正文）。

    渲染复用 :class:`TextFormatter` 的 Markdown→Qt 富文本管线（与对话气泡同一条
    管线，表格/代码块/引用的观感一致）。主题色是**内联**写进 HTML 的，所以主题
    切换时必须整体重渲染，见 :meth:`_render`。
    """

    def __init__(self, parent=None, version="", markdown_text="", current_version="",
                 channel="", release_url="", download_url=""):
        super().__init__(parent, title=f"Release Notes · v{version}", width=780)
        self.setMinimumHeight(560)

        self._markdown = markdown_text or ""
        self._release_url = release_url or ""
        self._download_url = download_url or ""

        self.lbl_header = QLabel()
        self.lbl_header.setWordWrap(True)
        self.content_layout.addWidget(self.lbl_header)

        self.browser = QTextBrowser()
        # 日志正文里的 GitHub / PR 链接交给系统浏览器打开；此处不拦截链接，
        # 也不做内嵌导航（setOpenExternalLinks 对 http(s) 生效）。
        self.browser.setOpenExternalLinks(True)
        self.browser.setOpenLinks(True)
        self.browser.setFrameShape(QFrame.NoFrame)
        self.content_layout.addWidget(self.browser, 1)

        self._header_html = (
            f"New release <b>v{version}</b>"
            + (f" · {channel} channel" if channel else "")
            + (f"<br>You are currently on v{current_version}." if current_version else "")
        )

        if self._download_url:
            self.add_button("Download", self._open_download, is_primary=True)
        if self._release_url:
            self.add_button("Open on GitHub", self._open_release)
        self.add_button("Close", self.accept)

        self._apply_theme()

    def _open_download(self):
        QDesktopServices.openUrl(QUrl(self._download_url))

    def _open_release(self):
        QDesktopServices.openUrl(QUrl(self._release_url))

    def _render(self):
        """把 Markdown 渲染进浏览器控件；无内容时给出可操作的兜底文案。"""
        if not self._markdown.strip():
            self.browser.setHtml(
                '<div style="color:%s; font-size:14px;">'
                'Release notes are not available in-app right now.<br>'
                'Use <b>Open on GitHub</b> to read them in your browser.'
                '</div>' % self.tm.color('text_muted'))
            return
        self.browser.setHtml(TextFormatter.markdown_to_html(self._markdown))

    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm
        self.lbl_header.setText(
            f'<span style="color:{tm.color("text_main")}; font-size:15px;">'
            f'{self._header_html}</span>')
        self.browser.setStyleSheet(
            f"QTextBrowser {{ background-color: transparent; border: none; "
            f"color: {tm.color('text_main')}; }}")
        self._render()
