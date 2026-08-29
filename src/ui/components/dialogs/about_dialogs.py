"""About-style informational dialogs: data providers and open-source licenses.

拆分自 src/ui/components/dialog.py。
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout,
                               QHeaderView, QLabel, QScrollArea, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from src.ui.components.dialogs.base import BaseDialog
from src.ui.components.dialogs.common import StandardDialog


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

        self.licenses = [
            ("BeautifulSoup4", "MIT", "Screen-scraping library for HTML/XML."),
            ("BioPython", "Biopython", "Tools for biological computation."),
            ("Boto3", "Apache 2.0", "AWS SDK for Python."),
            ("Chardet", "MIT", "Universal character encoding detector."),
            ("ChromaDB", "Apache 2.0", "AI-native open-source vector database."),
            ("Cryptography", "Apache 2.0", "Core cryptographic recipes and primitives."),
            ("Curl-cffi", "MIT", "Python binding for curl-impersonate."),
            ("Disposable-email-domains", "MIT", "List of disposable email domains."),
            ("Email-validator", "public domain", "Robust email syntax and deliverability validation."),
            ("FastAPI", "MIT", "Modern, high-performance web framework for building APIs."),
            ("hf_xet", "Apache Software License", "Efficient large-file storage for Hugging Face."),
            ("Keyring", "MIT", "Store and access credentials safely."),
            ("LangChain / Splitters", "MIT", "Advanced text chunking and LLM framework."),
            ("Langdetect", "MIT", "Language detection library port."),
            ("LiteLLM", "MIT", "Unified interface for integrating various Large Language Model (LLM) providers."),
            ("Markdown", "BSD-3-Clause", "Python implementation of Markdown."),
            ("MCP SDK", "MIT", "Model Context Protocol Python SDK."),
            ("Mermaid.js", "MIT", "Generation of diagrams and flowcharts."),
            ("NetworkX", "BSD-3-Clause", "Study of complex networks and graphs."),
            ("NVIDIA-ML-PY", "BSD-3-Clause", "Python bindings for NVIDIA Management Library."),
            ("ONNX Runtime", "MIT", "Cross-platform AI model accelerator."),
            ("Optimum / ONNX", "Apache 2.0", "Hardware-specific AI model optimization."),
            ("Psutil", "BSD-3-Clause", "Cross-platform process and system utilities."),
            ("PyInstaller", "GPL-2.0", "Bundles a Python application into a single package."),
            ("PyQtDarkTheme", "MIT", "Flat dark theme for PySide/PyQt."),
            ("PySide6", "LGPL v3", "Official Python bindings for Qt."),
            ("PyTorch", "BSD 3-Clause License", "Tensors and Dynamic neural networks."),
            ("Python-docx", "MIT", "Create and update Microsoft Word .docx files."),
            ("PyMuPDF / 4LLM", "AGPL v3", "High-performance PDF & Document parsing."),
            ("Scikit-learn", "BSD-3-Clause", "Machine learning and data mining tools."),
            ("Uvicorn", "BSD-3-Clause", "High-speed ASGI server implementation for Python.")
        ]

        self.licenses.sort(key=lambda item: item[0].lower())
        self.table = QTableWidget(len(self.licenses), 3)
        self.table.setHorizontalHeaderLabels(["Package", "License", "Purpose"])

        self.table.setWordWrap(True)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)

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
