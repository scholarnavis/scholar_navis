import html
import os
import json
import re

import requests
from datetime import datetime

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                               QLabel, QListWidget, QSplitter, QComboBox,
                               QTextBrowser, QListWidgetItem, QDialog, QLineEdit, QFormLayout,
                               QCheckBox, QScrollArea, QFileDialog, QTableWidget, QHeaderView,
                               QTableWidgetItem, QFrame, QAbstractItemView, QMessageBox, QMenu, QSizePolicy)
from PySide6.QtCore import Qt, QUrl, QEvent, QThread, Signal, QMarginsF, QTimer, QRectF
from PySide6.QtGui import QDesktopServices, QTextDocument, QPageLayout, QAbstractTextDocumentLayout, QPainter, QFont, \
    QColor
from PySide6.QtPrintSupport import QPrinter

from src.tools.base_tool import BaseTool
from src.core.core_task import TaskManager, TaskState
from src.task.rss_tasks import FetchRSSTask
from src.ui.components.toast import ToastManager
from src.ui.components.dialog import ProgressDialog
from src.core.signals import GlobalSignals

DEFAULT_FEEDS_DICT = {
    "Nature (Main Subjects)": [
        {"name": "Biochemistry", "url": "https://www.nature.com/subjects/biochemistry.rss"},
        {"name": "Biological Techniques", "url": "https://www.nature.com/subjects/biological-techniques.rss"},
        {"name": "Biotechnology", "url": "https://www.nature.com/subjects/biotechnology.rss"},
        {"name": "Cell Biology", "url": "https://www.nature.com/subjects/cell-biology.rss"},
        {"name": "Biophysics", "url": "https://www.nature.com/subjects/biophysics.rss"},
        {"name": "Genetics", "url": "https://www.nature.com/subjects/genetics.rss"},
        {"name": "Microbiology", "url": "https://www.nature.com/subjects/microbiology.rss"},
        {"name": "Molecular Biology", "url": "https://www.nature.com/subjects/molecular-biology.rss"},
        {"name": "Physiology", "url": "https://www.nature.com/subjects/physiology.rss"},
        {"name": "Diseases", "url": "https://www.nature.com/subjects/diseases.rss"},
        {"name": "Ecology", "url": "https://www.nature.com/subjects/ecology.rss"},
        {"name": "Climate Sciences", "url": "https://www.nature.com/subjects/climate-sciences.rss"},
        {"name": "Environmental Sciences", "url": "https://www.nature.com/subjects/environmental-sciences.rss"},
        {"name": "Health Care", "url": "https://www.nature.com/subjects/health-care.rss"},
        {"name": "Anatomy", "url": "https://www.nature.com/subjects/anatomy.rss"},
        {"name": "Astronomy and Planetary Science", "url": "https://www.nature.com/subjects/astronomy-and-planetary-science.rss"},
        {"name": "Chemistry", "url": "https://www.nature.com/subjects/chemistry.rss"},
        {"name": "Engineering", "url": "https://www.nature.com/subjects/engineering.rss"},
        {"name": "Materials Science", "url": "https://www.nature.com/subjects/materials-science.rss"},
        {"name": "Mathematics and Computing", "url": "https://www.nature.com/subjects/mathematics-and-computing.rss"}
    ],
    "Nature (Sub-journals)": [
        {"name": "Nature Cell Biology", "url": "https://www.nature.com/ncb.rss"},
        {"name": "Nature Biotechnology", "url": "https://www.nature.com/nbt.rss"},
        {"name": "Nature Methods", "url": "https://www.nature.com/nmeth.rss"},
        {"name": "Nature Genetics", "url": "https://www.nature.com/ng.rss"},
        {"name": "Nature Neuroscience", "url": "https://www.nature.com/neuro.rss"},
        {"name": "Nature Communications", "url": "https://www.nature.com/ncomms.rss"},
        {"name": "Nature Reviews Genetics", "url": "https://www.nature.com/nrg.rss"},
        {"name": "Nature Reviews Molecular Cell Biology", "url": "https://www.nature.com/nrm.rss"},
        {"name": "Nature Plants", "url": "https://www.nature.com/nplants.rss"},
        {"name": "Nature Medicine", "url": "https://www.nature.com/nm.rss"}
    ],
    "Science": [
        {"name": "Science Table of Contents", "url": "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science"},
        {"name": "Science Podcast", "url": "https://feeds.megaphone.fm/AAAS8717073854"},
        {"name": "Science First Release", "url": "https://www.science.org/action/showFeed?type=axatoc&feed=rss&jc=science"},
        {"name": "Science Daily News Feeds", "url": "https://www.science.org/rss/news_current.xml"},
        {"name": "Science Signaling", "url": "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=signaling"},
        {"name": "Science Translational Medicine", "url": "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=stm"},
        {"name": "Science Advances", "url": "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=sciadv"},
        {"name": "Science Immunology", "url": "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=sciimmunol"},
        {"name": "Science Robotics", "url": "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=scirobotics"},
        {"name": "Science Careers", "url": "https://www.science.org/digital-feed/careers-articles"},
        {"name": "Science In the Pipeline", "url": "https://www.science.org/blogs/pipeline/feed"}
    ],
    "Cell": [
        {"name": "Cell (Online now)", "url": "https://www.cell.com/cell/inpress.rss"},
        {"name": "Cell (Current issue)", "url": "https://www.cell.com/cell/current.rss"},
        {"name": "Molecular Cell (Online now)", "url": "https://www.cell.com/molecular-cell/inpress.rss"},
        {"name": "Molecular Cell (Current issue)", "url": "https://www.cell.com/molecular-cell/current.rss"},
        {"name": "Developmental Cell (Online now)", "url": "https://www.cell.com/developmental-cell/inpress.rss"},
        {"name": "Developmental Cell (Current issue)", "url": "https://www.cell.com/developmental-cell/current.rss"},
        {"name": "Molecular Plant (Articles in press)", "url": "https://www.cell.com/molecular-plant/inpress.rss"},
        {"name": "Molecular Plant (Latest issue)", "url": "https://www.cell.com/molecular-plant/current.rss"},
        {"name": "Cell Reports (Online now)", "url": "https://www.cell.com/cell-reports/inpress.rss"},
        {"name": "Cell Reports (Current issue)", "url": "https://www.cell.com/cell-reports/current.rss"},
        {"name": "Trends in Plant Science (Online now)", "url": "https://www.cell.com/trends/plant-science/inpress.rss"},
        {"name": "Trends in Plant Science (Current issue)", "url": "https://www.cell.com/trends/plant-science/current.rss"},
        {"name": "Trends in Genetics (Online now)", "url": "https://www.cell.com/trends/genetics/inpress.rss"},
        {"name": "Trends in Genetics (Current issue)", "url": "https://www.cell.com/trends/genetics/current.rss"}
    ],
    "PNAS": [
        {"name": "PNAS Applied Mathematics", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCode=app-math"},
        {"name": "PNAS Chemistry", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCode=chem"},
        {"name": "PNAS Mathematics", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCode=math"},
        {"name": "PNAS Applied Physical Sciences", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCode=app-phys"},
        {"name": "PNAS Physics", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCode=phys"},
        {"name": "PNAS Computer Sciences", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCode=comp-sci"},
        {"name": "PNAS Engineering", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCode=eng"},
        {"name": "PNAS Environmental Sciences", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCodeOr=env-sci-bio&tagCodeOr=env-sci-soc&tagCodeOr=env-sci-phys"},
        {"name": "PNAS Agricultural Sciences", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCode=ag-sci"},
        {"name": "PNAS Ecology", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCode=eco"},
        {"name": "PNAS Physiology", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCode=physio"},
        {"name": "PNAS Plant Biology", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCode=plant-bio"},
        {"name": "PNAS Genetics", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCode=genetics"},
        {"name": "PNAS Biochemistry", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCode=biochem"},
        {"name": "PNAS Medical Sciences", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCode=med-sci"},
        {"name": "PNAS Biophysics and Computational Biology", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCodeOr=biophys-bio&tagCodeOr=biophys-phys"},
        {"name": "PNAS Cell Biology", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCode=cell-bio"},
        {"name": "PNAS Microbiology", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCode=microbio"},
        {"name": "PNAS Neuroscience", "url": "https://www.pnas.org/action/showFeed?type=searchTopic&taxonomyCode=topic&tagCode=neuro"}
    ],
    "bioRxiv": [
        {"name": "bioRxiv Plant Biology", "url": "https://connect.biorxiv.org/biorxiv_xml.php?subject=plant_biology"},
        {"name": "bioRxiv Bioinformatics", "url": "https://connect.biorxiv.org/biorxiv_xml.php?subject=bioinformatics"},
        {"name": "bioRxiv Genomics", "url": "https://connect.biorxiv.org/biorxiv_xml.php?subject=genomics"},
        {"name": "bioRxiv Cell Biology", "url": "https://connect.biorxiv.org/biorxiv_xml.php?subject=cell_biology"}
    ],
    "Annual Reviews": [
        {"name": "Animal Biosciences", "url": "https://www.annualreviews.org/rss/content/journals/animal/latestarticles?fmt=rss"},
        {"name": "Biochemistry", "url": "https://www.annualreviews.org/rss/content/journals/biochem/latestarticles?fmt=rss"},
        {"name": "Biomedical Engineering", "url": "https://www.annualreviews.org/rss/content/journals/bioeng/latestarticles?fmt=rss"},
        {"name": "Biomedical Data Science", "url": "https://www.annualreviews.org/rss/content/journals/biodatasci/latestarticles?fmt=rss"},
        {"name": "Biophysics", "url": "https://www.annualreviews.org/rss/content/journals/biophys/latestarticles?fmt=rss"},
        {"name": "Cancer Biology", "url": "https://www.annualreviews.org/rss/content/journals/cancerbio/latestarticles?fmt=rss"},
        {"name": "Cell and Developmental Biology", "url": "https://www.annualreviews.org/rss/content/journals/cellbio/latestarticles?fmt=rss"},
        {"name": "Chemical and Biomolecular Engineering", "url": "https://www.annualreviews.org/rss/content/journals/chembioeng/latestarticles?fmt=rss"},
        {"name": "Ecology, Evolution, and Systematics", "url": "https://www.annualreviews.org/rss/content/journals/ecolsys/latestarticles?fmt=rss"},
        {"name": "Food Science and Technology", "url": "https://www.annualreviews.org/rss/content/journals/food/latestarticles?fmt=rss"},
        {"name": "Genetics", "url": "https://www.annualreviews.org/rss/content/journals/genet/latestarticles?fmt=rss"},
        {"name": "Genomics and Human Genetics", "url": "https://www.annualreviews.org/rss/content/journals/genom/latestarticles?fmt=rss"},
        {"name": "Immunology", "url": "https://www.annualreviews.org/rss/content/journals/immunol/latestarticles?fmt=rss"},
        {"name": "Medicine", "url": "https://www.annualreviews.org/rss/content/journals/med/latestarticles?fmt=rss"},
        {"name": "Microbiology", "url": "https://www.annualreviews.org/rss/content/journals/micro/latestarticles?fmt=rss"},
        {"name": "Neuroscience", "url": "https://www.annualreviews.org/rss/content/journals/neuro/latestarticles?fmt=rss"},
        {"name": "Pathology: Mechanisms of Disease", "url": "https://www.annualreviews.org/rss/content/journals/pathmechdis/latestarticles?fmt=rss"},
        {"name": "Pharmacology and Toxicology", "url": "https://www.annualreviews.org/rss/content/journals/pharmtox/latestarticles?fmt=rss"},
        {"name": "Physical Chemistry", "url": "https://www.annualreviews.org/rss/content/journals/physchem/latestarticles?fmt=rss"},
        {"name": "Physiology", "url": "https://www.annualreviews.org/rss/content/journals/physiol/latestarticles?fmt=rss"},
        {"name": "Phytopathology", "url": "https://www.annualreviews.org/rss/content/journals/phyto/latestarticles?fmt=rss"},
        {"name": "Plant Biology", "url": "https://www.annualreviews.org/rss/content/journals/arplant/latestarticles?fmt=rss"},
        {"name": "Virology", "url": "https://www.annualreviews.org/rss/content/journals/virology/latestarticles?fmt=rss"}
    ],
    "Other Journals": [
        {"name": "Journal of Cell Biology (Recent issues)", "url": "https://rupress.org/rss/site_1000001/1000003.xml"},
        {"name": "Journal of Cell Biology (Latest Articles)", "url": "https://rupress.org/rss/site_1000001/LatestArticles_1000003.xml"},
        {"name": "Bioinformatics (Latest Issue)", "url": "https://academic.oup.com/rss/site_5139/3001.xml"},
        {"name": "Bioinformatics (Advance Articles)", "url": "https://academic.oup.com/rss/site_5139/advanceAccess_3001.xml"},
        {"name": "Bioinformatics (Open Access)", "url": "https://academic.oup.com/rss/site_5139/OpenAccess.xml"},
        {"name": "Nucleic Acids Research (Latest Issue)", "url": "https://academic.oup.com/rss/site_5127/3091.xml"},
        {"name": "Nucleic Acids Research (Advance Articles)", "url": "https://academic.oup.com/rss/site_5127/advanceAccess_3091.xml"},
        {"name": "Nucleic Acids Research (Open Access)", "url": "https://academic.oup.com/rss/site_5127/OpenAccess.xml"},
        {"name": "The Plant Cell (Latest Issue)", "url": "https://academic.oup.com/rss/site_6317/4077.xml"},
        {"name": "The Plant Cell (Advance Articles)", "url": "https://academic.oup.com/rss/site_6317/advanceAccess_4077.xml"},
        {"name": "The Plant Cell (Open Access)", "url": "https://academic.oup.com/rss/site_6317/OpenAccess.xml"},
        {"name": "Plant Physiology (Latest Issue)", "url": "https://academic.oup.com/rss/site_6323/4080.xml"},
        {"name": "Plant Physiology (Advance Articles)", "url": "https://academic.oup.com/rss/site_6323/advanceAccess_4080.xml"},
        {"name": "Plant Physiology (Open Access)", "url": "https://academic.oup.com/rss/site_6323/OpenAccess.xml"},
        {"name": "Ecology Letters (Most recent)", "url": "https://onlinelibrary.wiley.com/feed/14610248/most-recent"},
        {"name": "Ecology Letters (Most cited)", "url": "https://onlinelibrary.wiley.com/feed/14610248/most-cited"},
        {"name": "New Phytologist", "url": "https://onlinelibrary.wiley.com/feed/14698137/most-recent"}
    ]
}


for category, feeds in DEFAULT_FEEDS_DICT.items():
    for f in feeds:
        f['is_default'] = True
        f['category'] = category

ALL_BUILTIN_FEEDS = [f for feeds in DEFAULT_FEEDS_DICT.values() for f in feeds]

def clean_html_text(raw_text):
    if not raw_text: return ""
    text = re.sub(r'</?(p|br|div|li|tr|h\d)[^>]*>', '\n', raw_text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    text = re.sub(r'\n\s*\n', '\n\n', text).strip()
    return text

class DownloadWorker(QThread):
    sig_msg = Signal(str, str)

    def __init__(self, urls, save_dir):
        super().__init__()
        self.urls = urls
        self.save_dir = save_dir

    def run(self):
        success_count = 0
        for url in self.urls:
            try:
                res = requests.get(url, timeout=15)
                if res.status_code == 200:
                    name = url.split('/')[-1]
                    if not name.endswith('.pdf'): name += ".pdf"
                    with open(os.path.join(self.save_dir, name), 'wb') as f:
                        f.write(res.content)
                    success_count += 1
            except Exception:
                pass
        self.sig_msg.emit(f"Batch download complete. Successfully saved {success_count} PDFs.", "success")


class FeedLibraryDialog(QDialog):
    def __init__(self, parent=None, current_feeds=None):
        super().__init__(parent)
        self.setWindowTitle("📚 Subscription Manager")
        self.resize(800, 550)
        self.setStyleSheet("background-color: #1e1e1e; color: white;")
        layout = QVBoxLayout(self)

        self.current_user_feeds = current_feeds if current_feeds else []
        self.subscribed_urls = {f["url"] for f in self.current_user_feeds}

        self.display_dict = {}
        for cat, feeds in DEFAULT_FEEDS_DICT.items():
            self.display_dict[cat] = [f.copy() for f in feeds]

        for f in self.current_user_feeds:
            if not f.get("is_default", False):
                cat = f.get("category", "Custom Sources")
                if cat not in self.display_dict:
                    self.display_dict[cat] = []
                self.display_dict[cat].append(f.copy())

        top_bar = QHBoxLayout()
        lbl_cat = QLabel("📂 Category / Journal:")
        lbl_cat.setStyleSheet("color: #05B8CC; font-weight: bold;")
        self.combo_category = QComboBox()
        self.combo_category.addItems(list(self.display_dict.keys()))
        self.combo_category.setStyleSheet("padding: 5px; background: #252526; border: 1px solid #444;")
        self.combo_category.currentTextChanged.connect(self._render_table)

        # 弹窗库搜索框
        self.inp_search_lib = QLineEdit()
        self.inp_search_lib.setPlaceholderText("🔍 Search journal names...")
        self.inp_search_lib.setStyleSheet(
            "padding: 5px; background: #252526; border: 1px solid #444; border-radius: 3px;")
        self.inp_search_lib.textChanged.connect(self._filter_library_table)

        btn_add_custom = QPushButton("➕ Add Custom Source")
        btn_add_custom.setStyleSheet("background-color: #333; color: white; padding: 5px 15px; border-radius: 4px;")
        btn_add_custom.clicked.connect(self._on_add_custom)

        top_bar.addWidget(lbl_cat)
        top_bar.addWidget(self.combo_category)
        top_bar.addWidget(self.inp_search_lib, stretch=1)
        top_bar.addWidget(btn_add_custom)
        layout.addLayout(top_bar)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Subscribe", "Journal / Source", "RSS URL"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setStyleSheet("QTableWidget { background-color: #252526; gridline-color: #333; }")
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.table)

        btn_box = QHBoxLayout()
        self.lbl_status = QLabel(f"Selected: {len(self.subscribed_urls)}")
        self.lbl_status.setStyleSheet("color: #888;")

        btn_save = QPushButton("💾 Save Subscriptions")
        btn_save.setStyleSheet("background-color: #007acc; color: white; padding: 6px 15px; border-radius: 4px; font-weight: bold;")
        btn_save.clicked.connect(self.accept)

        btn_box.addWidget(self.lbl_status)
        btn_box.addStretch()
        btn_box.addWidget(btn_save)
        layout.addLayout(btn_box)

        self.checkboxes_map = {}
        self._render_table(self.combo_category.currentText())

    def _filter_library_table(self, text):
        text = text.lower()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if item:
                self.table.setRowHidden(row, text not in item.text().lower())

    def _render_table(self, category):
        self.table.setRowCount(0)
        self.checkboxes_map.clear()
        feeds = self.display_dict.get(category, [])
        self.table.setRowCount(len(feeds))

        for i, feed in enumerate(feeds):
            chk = QCheckBox()
            chk.setChecked(feed["url"] in self.subscribed_urls)
            chk.toggled.connect(lambda checked, url=feed["url"]: self._on_checkbox_toggled(url, checked))

            chk_widget = QWidget()
            chk_layout = QHBoxLayout(chk_widget)
            chk_layout.addWidget(chk)
            chk_layout.setAlignment(Qt.AlignCenter)
            chk_layout.setContentsMargins(0, 0, 0, 0)

            name_item = QTableWidgetItem(feed["name"])
            if feed.get("is_default"):
                name_item.setToolTip("Built-in Default Source")
                name_item.setForeground(Qt.white)
            else:
                name_item.setToolTip("Custom Source")
                name_item.setForeground(Qt.cyan)

            self.table.setCellWidget(i, 0, chk_widget)
            self.table.setItem(i, 1, name_item)
            self.table.setItem(i, 2, QTableWidgetItem(feed["url"]))

    def _on_checkbox_toggled(self, url, is_checked):
        if is_checked:
            self.subscribed_urls.add(url)
        else:
            self.subscribed_urls.discard(url)
        self.lbl_status.setText(f"Selected: {len(self.subscribed_urls)}")

    def refresh_all_feeds(self):
        if not self.feeds:
            ToastManager().show("Your tracker list is empty. Add a source first.", "warning")
            return

        telemetry_off = {"cpu": False, "ram": False, "gpu": False, "net": False, "io": False}
        self.pd = ProgressDialog(self.widget, "Fetching Literature", "Connecting to servers and detecting OA...",
                                 telemetry_config=telemetry_off)
        self.pd.show()

        self.task_mgr.sig_progress.connect(self.pd.update_progress)
        self.task_mgr.sig_state_changed.connect(self._on_fetch_done)
        self.pd.sig_canceled.connect(self.task_mgr.cancel_task)

        self.task_mgr.start_task(FetchRSSTask, "rss_fetch", feeds=self.feeds, save_path=self.cache_file)

    def _on_add_custom(self):
        dlg = FeedEditorDialog(self)
        if dlg.exec():
            new_feed = dlg.get_data()
            if new_feed["url"]:
                new_feed["is_default"] = False
                cat = new_feed.get("category", "Custom Sources")

                if cat not in self.display_dict:
                    self.display_dict[cat] = []
                    self.combo_category.addItem(cat)

                self.display_dict[cat].append(new_feed)
                self.subscribed_urls.add(new_feed["url"])
                self.lbl_status.setText(f"Selected: {len(self.subscribed_urls)}")
                self.combo_category.setCurrentText(cat)

    def get_final_feeds(self):
        final_list = []
        for cat, feeds in self.display_dict.items():
            for f in feeds:
                if f["url"] in self.subscribed_urls:
                    final_list.append(f)

        unique_feeds = {f["url"]: f for f in final_list}
        return list(unique_feeds.values())


class FeedEditorDialog(QDialog):
    def __init__(self, parent=None, feed_data=None, is_default=False):
        super().__init__(parent)
        self.setWindowTitle("Edit Tracker Rule" if is_default else "Custom Feed Settings")
        self.setFixedSize(450, 180)
        self.setStyleSheet("background-color: #252526; color: white;")
        layout = QFormLayout(self)

        self.inp_name = QLineEdit(feed_data.get('name', '') if feed_data else '')
        self.inp_url = QLineEdit(feed_data.get('url', '') if feed_data else '')

        self.inp_category = QComboBox()
        self.inp_category.setEditable(True)
        self.inp_category.addItems(list(DEFAULT_FEEDS_DICT.keys()) + ["Custom Sources"])
        if feed_data and feed_data.get('category'):
            self.inp_category.setCurrentText(feed_data['category'])
        else:
            self.inp_category.setCurrentText("Custom Sources")

        if is_default:
            for inp in [self.inp_name, self.inp_url]:
                inp.setReadOnly(True)
                inp.setStyleSheet("background:#222; border:1px solid #333; padding:5px; border-radius:3px; color:#888;")
            self.inp_category.setEnabled(False)
            self.inp_category.setStyleSheet("background:#222; border:1px solid #333; padding:5px; border-radius:3px; color:#888;")
            layout.addRow("", QLabel("🔒 Built-in source: Read-only."))
        else:
            for inp in [self.inp_name, self.inp_url]:
                inp.setStyleSheet("background:#1e1e1e; border:1px solid #444; padding:5px; border-radius:3px;")
            self.inp_category.setStyleSheet("background:#1e1e1e; border:1px solid #444; padding:5px; border-radius:3px;")

        layout.addRow("Source Name:", self.inp_name)
        layout.addRow("RSS URL:", self.inp_url)
        layout.addRow("Category:", self.inp_category)

        btn_box = QHBoxLayout()
        btn_save = QPushButton("Save")
        if is_default:
            btn_save.setEnabled(False)
            btn_save.setStyleSheet("background-color: #444; color: #888; padding: 6px; border-radius: 4px;")
        else:
            btn_save.setStyleSheet("background-color: #007acc; color: white; padding: 6px; border-radius: 4px;")
            btn_save.clicked.connect(self.accept)

        btn_box.addStretch()
        btn_box.addWidget(btn_save)
        layout.addRow(btn_box)

    def get_data(self):
        return {
            "name": self.inp_name.text().strip(),
            "url": self.inp_url.text().strip(),
            "category": self.inp_category.currentText().strip()
        }


class ArticleWidget(QFrame):
    def __init__(self, article_data, parent=None):
        super().__init__(parent)
        self.article_data = article_data

        # 🟢 核心修复 1：指定专属 ID，防止 QFrame 样式污染内部的 Checkbox 和 Label
        self.setObjectName("ArticleFrame")
        self.setStyleSheet(
            "QFrame#ArticleFrame { background-color: #252526; border: 1px solid #333; border-radius: 6px; margin-bottom: 10px; padding: 10px; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        header_layout = QHBoxLayout()

        self.checkbox = QCheckBox()
        self.checkbox.setStyleSheet("color: white; background: transparent;")
        header_layout.addWidget(self.checkbox)
        header_layout.addSpacing(5)

        title_link = f"<a href='{article_data['link']}' style='color:#05B8CC; text-decoration:none; font-size: 16px; font-weight:bold;'>{article_data['title']}</a>"
        lbl_title = QLabel(title_link)
        lbl_title.setOpenExternalLinks(True)
        lbl_title.setWordWrap(True)
        lbl_title.setStyleSheet("border: none; background: transparent;")

        header_layout.addWidget(self.checkbox)
        header_layout.addWidget(lbl_title, stretch=1)
        layout.addLayout(header_layout)

        meta_text = f"🕒 {article_data.get('pub_date', 'Unknown Date')}"
        if article_data.get('doi'): meta_text += f" | 🔗 DOI: {article_data['doi']}"
        if article_data.get('tags'): meta_text += f" | 🏷️ {', '.join(article_data['tags'])}"

        lbl_meta = QLabel(meta_text)
        lbl_meta.setStyleSheet("color: #888; font-size: 12px; border: none; background: transparent; padding-left: 25px;")
        layout.addWidget(lbl_meta)

        self.text_browser = QLabel()
        self.text_browser.setOpenExternalLinks(True)
        self.text_browser.setTextFormat(Qt.RichText)
        self.text_browser.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.text_browser.setWordWrap(True)
        self.text_browser.setText(article_data.get('summary', ''))
        self.text_browser.setStyleSheet("QLabel { background: transparent; color: #d4d4d4; border: none; font-size: 13px; line-height: 1.5; selection-background-color: #05B8CC; padding-left: 20px;}")
        self.text_browser.installEventFilter(self)
        layout.addWidget(self.text_browser)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(25, 5, 0, 0)

        btn_trans = QPushButton("🌐 Quick Translate")
        btn_trans.setCursor(Qt.PointingHandCursor)
        btn_trans.setStyleSheet("QPushButton { background-color: #333; color: #e0e0e0; border-radius: 4px; padding: 4px 10px; font-size: 12px; border: 1px solid #555; } QPushButton:hover { background-color: #05B8CC; color: white; border: 1px solid #05B8CC; }")
        btn_trans.clicked.connect(self._send_to_translator)
        btn_layout.addWidget(btn_trans)

        btn_chat = QPushButton("💬 Send to Chat")
        btn_chat.setCursor(Qt.PointingHandCursor)
        btn_chat.setStyleSheet("QPushButton { background-color: #333; color: #e0e0e0; border-radius: 4px; padding: 4px 10px; font-size: 12px; border: 1px solid #555; } QPushButton:hover { background-color: #007acc; color: white; border: 1px solid #007acc; }")
        btn_chat.clicked.connect(self._send_to_chat)
        btn_layout.insertWidget(1, btn_chat)

        if article_data.get('pdf_url'):
            btn_dl = QPushButton("⬇️ Download OA PDF")
            btn_dl.setCursor(Qt.PointingHandCursor)
            btn_dl.setStyleSheet("QPushButton { background-color: #28a745; color: white; border-radius: 4px; padding: 4px 10px; font-size: 12px; border: none; font-weight: bold;} QPushButton:hover { background-color: #218838; }")
            btn_dl.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.article_data['pdf_url'])))
            btn_layout.addWidget(btn_dl)
        else:
            btn_link = QPushButton("🔗 Publisher Link (Non-OA)")
            btn_link.setCursor(Qt.PointingHandCursor)
            btn_link.setStyleSheet("QPushButton { background-color: #444; color: #aaa; border-radius: 4px; padding: 4px 10px; font-size: 12px; border: none;} QPushButton:hover { background-color: #555; color: #fff; }")
            btn_link.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.article_data['link'])))
            btn_layout.addWidget(btn_link)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

    def _send_to_chat(self):
        if hasattr(GlobalSignals(), 'sig_send_to_chat'):
            clean_summary = clean_html_text(self.article_data.get('summary', ''))
            context_text = f"Title: {self.article_data['title']}\nAbstract: {clean_summary}\nURL: {self.article_data.get('link', '')}"

            prompt = (
                "Please analyze the provided Title and Abstract of this article. "
                "1. Extract the primary research objective, key methodologies, and core findings.\n"
                "2. Evaluate its potential biological or clinical significance.\n"
                "*(Note: Since this is only an abstract, you may trigger the NCBI/Semantic Scholar tools to retrieve more metadata or related literature if you need deeper context.)*"
            )
            GlobalSignals().sig_send_to_chat.emit(context_text, prompt)

    def eventFilter(self, obj, event):
        if obj == self.text_browser and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Space:
                selected_text = self.text_browser.selectedText()
                if selected_text and hasattr(GlobalSignals(), 'sig_invoke_translator'):
                    GlobalSignals().sig_invoke_translator.emit(selected_text)
                    return True
        return super().eventFilter(obj, event)

    def _send_to_translator(self):
        if hasattr(GlobalSignals(), 'sig_invoke_translator'):
            raw_text = f"{self.article_data['title']}\n\n{self.article_data.get('summary', '')}"
            clean_text = clean_html_text(raw_text)
            GlobalSignals().sig_invoke_translator.emit(clean_text)

    def is_checked(self):
        return self.checkbox.isChecked()

    def set_checked(self, state):
        self.checkbox.setChecked(state)


class RSSTool(BaseTool):
    def __init__(self):
        super().__init__("Literature Tracker")
        self.workspace_dir = os.path.join(os.getcwd(), "scholar_workspace")
        self.feeds_file = os.path.join(self.workspace_dir, "rss_subscriptions.json")
        self.cache_file = os.path.join(self.workspace_dir, "rss_cache.json")

        self.feeds = []
        self.article_cache = {}
        self.last_fetched_time = "Never"
        self.current_article_widgets = []
        self.dl_thread = None

        self.task_mgr = TaskManager()
        os.makedirs(self.workspace_dir, exist_ok=True)
        self._load_config()

    def get_ui_widget(self) -> QWidget:
        if hasattr(self, 'widget'): return self.widget

        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(15, 15, 15, 15)

        toolbar = QHBoxLayout()
        self.btn_manage = QPushButton("📚 Manage Subscriptions")
        self.btn_manage.setStyleSheet("background-color: #007acc; color: white; padding: 6px 15px; border-radius: 4px; font-weight: bold;")
        self.btn_manage.clicked.connect(self.open_subscription_manager)

        self.btn_edit = QPushButton("✏️ Edit Source")
        self.btn_edit.clicked.connect(self.edit_feed)

        self.btn_unsub = QPushButton("❌ Unsubscribe Selected")
        self.btn_unsub.clicked.connect(lambda: self._batch_action("unsubscribe"))

        self.lbl_time = QLabel("Last Fetched: Never")
        self.lbl_time.setStyleSheet("color: #888; font-style: italic; margin-left: 10px;")

        self.btn_refresh = QPushButton("🔄 Sync Selected / All")
        self.btn_refresh.setStyleSheet("background-color: #28a745; color: white; font-weight: bold; padding: 6px 15px; border-radius: 4px;")
        self.btn_refresh.clicked.connect(lambda: self._batch_action("fetch"))

        toolbar.addWidget(self.btn_manage)
        toolbar.addWidget(self.btn_edit)
        toolbar.addWidget(self.btn_unsub)
        toolbar.addWidget(self.lbl_time)
        toolbar.addStretch()
        toolbar.addWidget(self.btn_refresh)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Horizontal)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)

        # RSS 源搜索框
        self.inp_search_feed = QLineEdit()
        self.inp_search_feed.setPlaceholderText("🔍 Search feeds...")
        self.inp_search_feed.setStyleSheet(
            "background-color: #252526; color: white; border: 1px solid #444; border-radius: 4px; padding: 5px;")
        self.inp_search_feed.textChanged.connect(self._filter_feed_list)
        left_layout.addWidget(self.inp_search_feed)

        left_action_bar = QHBoxLayout()
        self.btn_feed_sel_all = QPushButton("☑ Select All")
        self.btn_feed_sel_inv = QPushButton("🔲 Invert")

        for btn in [self.btn_feed_sel_all, self.btn_feed_sel_inv]:
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet("""
                                QPushButton { background-color: #333; color: #ccc; border-radius: 3px; padding: 4px 8px; font-size: 11px; border: 1px solid #444; } 
                                QPushButton:hover { background-color: #444; color: white; }
                            """)

        self.btn_feed_sel_all.clicked.connect(lambda: self._batch_select_feeds(True))
        self.btn_feed_sel_inv.clicked.connect(lambda: self._batch_select_feeds("invert"))

        left_action_bar.addWidget(self.btn_feed_sel_all)
        left_action_bar.addWidget(self.btn_feed_sel_inv)
        left_action_bar.addStretch()
        left_layout.addLayout(left_action_bar)

        self.feed_list = QListWidget()
        self.feed_list.currentRowChanged.connect(self._on_feed_selected)
        self.feed_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.feed_list.customContextMenuRequested.connect(self._show_feed_context_menu)
        self.feed_list.setSelectionMode(QAbstractItemView.ExtendedSelection)

        self.feed_list.setWordWrap(True)
        self.feed_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.feed_list.setStyleSheet("""
                    QListWidget { background-color: #1e1e1e; color: #e0e0e0; border: 1px solid #333; border-radius: 4px; padding: 5px; }
                    QListWidget::indicator { width: 15px; height: 15px; }
                    QListWidget::item { padding: 4px 0px; border-bottom: 1px dashed #333; }
                """)
        left_layout.addWidget(self.feed_list)
        splitter.addWidget(left_panel)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)

        action_bar = QHBoxLayout()
        self.btn_sel_all = QPushButton("☑ Select All")
        self.btn_sel_inv = QPushButton("🔲 Invert")
        self.btn_sel_all.clicked.connect(lambda: self._batch_select(True))
        self.btn_sel_inv.clicked.connect(lambda: self._batch_select("invert"))

        self.btn_batch_chat = QPushButton("💬 Analyze Selected")
        self.btn_batch_chat.setStyleSheet("color: #8be9fd;")
        self.btn_batch_chat.clicked.connect(self.batch_send_to_chat)

        self.btn_export_pdf = QPushButton("📤 Export to PDF")
        self.btn_export_pdf.setStyleSheet("color: #ffb86c;")
        self.btn_export_pdf.clicked.connect(self.export_to_pdf)

        self.btn_batch_dl = QPushButton("⬇️ Download Selected OA")
        self.btn_batch_dl.setStyleSheet("color: #50fa7b;")
        self.btn_batch_dl.clicked.connect(self.batch_download_pdfs)

        hint = QLabel("💡 Hint: Select text and press Space to translate.")
        hint.setStyleSheet("color: #aaa; font-style: italic; font-size: 11px;")

        action_bar.addWidget(self.btn_sel_all)
        action_bar.addWidget(self.btn_sel_inv)
        action_bar.addStretch()
        action_bar.addWidget(hint)
        action_bar.addWidget(self.btn_batch_chat)
        action_bar.addWidget(self.btn_export_pdf)
        action_bar.addWidget(self.btn_batch_dl)
        right_layout.addLayout(action_bar)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self.article_container = QWidget()
        self.article_container.setStyleSheet("background: transparent;")
        self.article_layout = QVBoxLayout(self.article_container)
        self.article_layout.setContentsMargins(0, 0, 10, 0)
        self.article_layout.addStretch()
        self.scroll_area.setWidget(self.article_container)

        right_layout.addWidget(self.scroll_area)
        splitter.addWidget(right_panel)

        splitter.setSizes([340, 860])
        layout.addWidget(splitter, stretch=1)

        self._load_cache()
        self._refresh_feed_ui()
        self.task_mgr.sig_log.connect(self.on_task_log)


        self.render_timer = QTimer(self.widget)
        self.render_timer.timeout.connect(self._render_batch)
        self.render_queue = []
        self.current_render_url = ""

        return self.widget

    def _clear_articles(self):
        # 停止正在进行的渲染
        if hasattr(self, 'render_timer'):
            self.render_timer.stop()
        self.render_queue = []

        # 隐藏并移出当前控件（利用缓存）
        for w in self.current_article_widgets:
            self.article_layout.removeWidget(w)
            w.hide()
        self.current_article_widgets = []

        while self.article_layout.count() > 1:
            item = self.article_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def batch_send_to_chat(self):
        selected = [w.article_data for w in self.current_article_widgets if w.is_checked()]
        if not selected:
            ToastManager().show("Please check at least one article to analyze.", "warning")
            return

        context = "### Selected Literature for Analysis ###\n\n"
        for i, art in enumerate(selected):
            clean_summary = clean_html_text(art.get('summary', ''))
            context += f"[{i + 1}] Title: {art['title']}\nAbstract: {clean_summary}\nURL: {art.get('link', '')}\n\n"

        if hasattr(GlobalSignals(), 'sig_send_to_chat'):
            prompt = (
                "Based on the Titles and Abstracts of these selected articles, please provide a synthesized summary:\n"
                "1. **Thematic Overview**: Identify the overarching research trends or common problems addressed in these papers.\n"
                "2. **Methodological/Finding Breakdown**: Briefly categorize their distinct methodologies or highlight overlapping conclusions.\n"
                "3. **Synthesis**: Are there any contradictions or synergistic implications among these studies?"
            )
            GlobalSignals().sig_send_to_chat.emit(context, prompt)

    def edit_feed(self):
        row = self.feed_list.currentRow()
        if row < 0:
            ToastManager().show("Please select a feed from the list on the left to edit.", "warning")
            return

        feed = self.feeds[row]
        is_default = feed.get("is_default", False)

        dlg = FeedEditorDialog(self.widget, feed, is_default=is_default)
        if dlg.exec():
            new_data = dlg.get_data()
            if new_data['url']:
                new_data['is_default'] = is_default
                self.feeds[row] = new_data
                self._save_config()
                self._refresh_feed_ui()

    def _filter_feed_list(self, text):
        text = text.lower()
        for i in range(self.feed_list.count()):
            item = self.feed_list.item(i)
            item.setHidden(text not in item.text().lower())

    def _show_feed_context_menu(self, pos):
        menu = QMenu(self.widget)
        menu.setStyleSheet("""
            QMenu { background-color: #252526; color: white; border: 1px solid #444; } 
            QMenu::item:selected { background-color: #007acc; }
        """)

        action_fetch = menu.addAction("🔄 Fetch Checked / Clicked")
        action_unsub = menu.addAction("❌ Unsubscribe Checked / Clicked")

        action = menu.exec(self.feed_list.mapToGlobal(pos))
        if action == action_fetch:
            self._batch_action("fetch", pos)
        elif action == action_unsub:
            self._batch_action("unsubscribe", pos)

    def _get_target_feed_indices(self, pos=None):
        indices = []
        for i in range(self.feed_list.count()):
            if self.feed_list.item(i).checkState() == Qt.Checked:
                indices.append(i)

        if not indices:
            if pos is not None:
                item = self.feed_list.itemAt(pos)
                if item:
                    indices.append(self.feed_list.row(item))
            else:
                row = self.feed_list.currentRow()
                if row >= 0:
                    indices.append(row)

        return sorted(list(set(indices)), reverse=True)

    def _batch_action(self, action_type, pos=None):
        indices = self._get_target_feed_indices(pos)

        if not indices:
            if action_type == "fetch":
                indices = list(range(len(self.feeds)))
            else:
                ToastManager().show("Please check or select at least one feed.", "warning")
                return

        if action_type == "unsubscribe":
            reply = QMessageBox.question(self.widget, 'Confirm Bulk Unsubscribe',
                                         f"Are you sure you want to remove {len(indices)} feeds from your tracker?",
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                for idx in indices:
                    del self.feeds[idx]
                self._save_config()
                self._refresh_feed_ui()
                self._clear_articles()
                ToastManager().show(f"Unsubscribed {len(indices)} feeds successfully.", "success")

        elif action_type == "fetch":
            target_feeds = [self.feeds[idx] for idx in indices]
            self.refresh_specific_feeds(target_feeds)

    def open_subscription_manager(self):
        dlg = FeedLibraryDialog(self.widget, self.feeds)
        if dlg.exec():
            self.feeds = dlg.get_final_feeds()
            self._save_config()
            self._refresh_feed_ui()
            ToastManager().show(f"Subscriptions updated. Current active feeds: {len(self.feeds)}.", "success")

    def refresh_specific_feeds(self, target_feeds):
        if not target_feeds:
            return

        telemetry_off = {"cpu": False, "ram": False, "gpu": False, "net": False, "io": False}
        self.pd = ProgressDialog(self.widget, "Fetching Literature", f"Syncing {len(target_feeds)} feeds...", telemetry_config=telemetry_off)
        self.pd.show()

        self.task_mgr.sig_progress.connect(self.pd.update_progress)
        self.task_mgr.sig_state_changed.connect(self._on_fetch_done)
        self.pd.sig_canceled.connect(self.task_mgr.cancel_task)

        self.task_mgr.start_task(FetchRSSTask, "rss_fetch", feeds=target_feeds, save_path=self.cache_file)

    def _on_fetch_done(self, state, msg):
        try:
            self.task_mgr.sig_state_changed.disconnect(self._on_fetch_done)
        except:
            pass
        try:
            self.task_mgr.sig_progress.disconnect(self.pd.update_progress)
        except:
            pass

        if state == TaskState.SUCCESS.value:
            self.pd.show_success_state("Complete", "Literature synced successfully.")
            self._load_cache()

            if hasattr(self, 'article_widgets_cache'):
                for url, widgets in self.article_widgets_cache.items():
                    for w in widgets:
                        w.deleteLater()
                self.article_widgets_cache.clear()

            self._on_feed_selected(self.feed_list.currentRow())
        else:
            self.pd.close_safe()
            ToastManager().show(f"Fetch failed: {msg}", "error")

    def _clear_articles(self):
        for w in self.current_article_widgets:
            self.article_layout.removeWidget(w)
            w.hide()
        self.current_article_widgets = []

        while self.article_layout.count() > 1:
            item = self.article_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _on_feed_selected(self, row):
        if row < 0: return

        feed = self.feeds[row]
        if feed.get("is_default", False):
            self.btn_edit.setEnabled(False)
            self.btn_edit.setToolTip("Built-in Default Source (Cannot edit)")
        else:
            self.btn_edit.setEnabled(True)
            self.btn_edit.setToolTip("Edit Custom Source")

        self._clear_articles()

        url = feed['url']
        articles = self.article_cache.get(url, [])

        if not articles:
            lbl = QLabel("No data available. Select feed and click 'Sync' to pull data.")
            lbl.setStyleSheet("color: #888; padding: 20px;")
            self.article_layout.insertWidget(0, lbl)
            return

        if not hasattr(self, 'article_widgets_cache'):
            self.article_widgets_cache = {}

        self.current_render_url = url
        if url not in self.article_widgets_cache:
            self.article_widgets_cache[url] = []
            self.render_queue = articles.copy()
        else:
            self.render_queue = self.article_widgets_cache[url].copy()

        # 启动分片渲染，每 15ms 触发一次
        self.render_timer.start(15)

    def _render_batch(self):
        if not self.render_queue:
            self.render_timer.stop()
            return

        batch = self.render_queue[:4]
        self.render_queue = self.render_queue[4:]

        for item in batch:
            if isinstance(item, dict):
                w = ArticleWidget(item)
                self.article_widgets_cache[self.current_render_url].append(w)
            else:
                w = item

            self.current_article_widgets.append(w)
            self.article_layout.insertWidget(self.article_layout.count() - 1, w)
            w.show()


    def _batch_select(self, mode):
        if not self.current_article_widgets: return
        for w in self.current_article_widgets:
            if mode == "invert":
                w.set_checked(not w.is_checked())
            else:
                w.set_checked(bool(mode))

    def _batch_select_feeds(self, mode):
        self.feed_list.blockSignals(True)
        for i in range(self.feed_list.count()):
            item = self.feed_list.item(i)
            if mode == "invert":
                new_state = Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked
            else:
                new_state = Qt.Checked if mode else Qt.Unchecked
            item.setCheckState(new_state)
        self.feed_list.blockSignals(False)

    def export_to_pdf(self):

        selected = [w.article_data for w in self.current_article_widgets if w.is_checked()]
        if not selected:
            ToastManager().show("Please select at least one article to export.", "warning")
            return

        row = self.feed_list.currentRow()
        feed_name = self.feeds[row]['name'] if 0 <= row < len(self.feeds) else "Literature_Report"
        safe_filename = re.sub(r'[\\/*?:"<>|]', "_", feed_name)

        path, _ = QFileDialog.getSaveFileName(self.widget, "Export to PDF", f"{safe_filename}.pdf",
                                              "PDF Files (*.pdf)")
        if not path: return

        html = f"""
        <h1 style='color: #333;'>{feed_name}</h1>
        <p style='color: #666;'>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
        <hr>
        """

        for art in selected:
            landing_url = art.get('link', '#')
            html += f"<h3><a href='{landing_url}' style='color:#05B8CC; text-decoration:none;'>{art['title']}</a></h3>"
            doi_val = art.get('doi', '')
            doi_html = f"<a href='https://doi.org/{doi_val}' style='color:#05B8CC; text-decoration:none;'>{doi_val}</a>" if doi_val else "N/A"
            oa_url = art.get('pdf_url', '')
            oa_html = f" | <b>OA PDF:</b> <a href='{oa_url}' style='color:#28a745; text-decoration:none;'>⬇️ Click to Download</a>" if oa_url else ""
            html += f"<p style='color:gray; font-size: 10pt;'><b>Date:</b> {art.get('pub_date', '')} | <b>DOI:</b> {doi_html}{oa_html}</p>"
            html += f"<div style='font-size: 11pt; line-height: 1.4;'>{art.get('summary', '')}</div><hr>"

        doc = QTextDocument()
        doc.setHtml(html)

        printer = QPrinter(QPrinter.HighResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)

        margin = QMarginsF(15, 15, 15, 22)
        printer.setPageMargins(margin, QPageLayout.Millimeter)
        printer.setOutputFileName(path)

        page_rect = printer.pageRect(QPrinter.DevicePixel)
        doc.setPageSize(page_rect.size())

        painter = QPainter(printer)
        page_count = doc.pageCount()

        for page_idx in range(page_count):
            if page_idx > 0:
                printer.newPage()

            painter.save()
            painter.translate(0, -(page_idx * page_rect.height()))
            clip_rect = QRectF(0, page_idx * page_rect.height(), page_rect.width(), page_rect.height())

            ctx = QAbstractTextDocumentLayout.PaintContext()
            ctx.clip = clip_rect
            doc.documentLayout().draw(painter, ctx)
            painter.restore()

            painter.save()
            font = QFont("Arial", 10)
            painter.setFont(font)
            painter.setPen(QColor(150, 150, 150))

            fm = painter.fontMetrics()
            text_height = fm.height()

            text_rect = QRectF(0, page_rect.height() - text_height * 1.5, page_rect.width(), text_height)
            painter.drawText(text_rect, Qt.AlignCenter | Qt.AlignBottom, f"- {page_idx + 1} -")
            painter.restore()

        painter.end()

        ToastManager().show(f"PDF exported successfully ({page_count} pages).", "success")

    def batch_download_pdfs(self):
        selected_urls = [w.article_data['pdf_url'] for w in self.current_article_widgets if
                         w.is_checked() and w.article_data.get('pdf_url')]
        if not selected_urls:
            ToastManager().show("None of the selected articles have OA PDF links available.", "warning")
            return

        save_dir = QFileDialog.getExistingDirectory(self.widget, "Select Folder to Save PDFs")
        if not save_dir: return

        self.dl_thread = DownloadWorker(selected_urls, save_dir)
        self.dl_thread.sig_msg.connect(lambda msg, lvl: ToastManager().show(msg, lvl))
        self.dl_thread.finished.connect(self.dl_thread.deleteLater)
        self.dl_thread.start()

        ToastManager().show("Batch download started in background...", "info")

    def _load_config(self):
        saved_feeds = []
        if os.path.exists(self.feeds_file):
            try:
                with open(self.feeds_file, 'r', encoding='utf-8') as f:
                    saved_feeds = json.load(f)
            except:
                pass

        for feed in saved_feeds:
            if "category" not in feed:
                feed["category"] = "Legacy Sources"
            if "is_default" not in feed:
                is_def = False
                for built_in in ALL_BUILTIN_FEEDS:
                    if built_in["url"] == feed["url"]:
                        is_def = True
                        feed["category"] = built_in["category"]
                        feed["name"] = built_in["name"]
                        break
                feed["is_default"] = is_def

        self.feeds = saved_feeds if saved_feeds else []

    def _save_config(self):
        try:
            with open(self.feeds_file, 'w', encoding='utf-8') as f:
                json.dump(self.feeds, f, indent=4)
        except:
            pass

    def _load_cache(self):
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    meta = data.pop("_meta", {})
                    self.last_fetched_time = meta.get("last_fetched", "Unknown")
                    self.article_cache = data
                    if hasattr(self, 'lbl_time'):
                        self.lbl_time.setText(f"Last Fetched: {self.last_fetched_time}")
            except:
                pass

    def _refresh_feed_ui(self):
        self.feed_list.blockSignals(True)
        self.feed_list.clear()

        self.feeds.sort(key=lambda x: (x.get('category', 'Z'), x['name']))

        for feed in self.feeds:
            cat_prefix = f"[{feed.get('category', 'Other')}] "
            kws = feed.get('keywords', '')

            icon = "🔒" if feed.get("is_default") else "📰"
            suffix = " 🔍" if kws else ""

            item_text = f"{icon} {cat_prefix}{feed['name']}{suffix}"
            item = QListWidgetItem(item_text)
            item.setData(Qt.UserRole, feed['url'])

            item.setToolTip(f"Category: {feed.get('category', 'Other')}\nSource: {feed['name']}\nURL: {feed['url']}")

            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)

            if feed.get("is_default"):
                item.setForeground(Qt.lightGray)
            else:
                item.setForeground(Qt.cyan)

            self.feed_list.addItem(item)

        self.feed_list.blockSignals(False)

        if self.feed_list.count() > 0 and self.feed_list.currentRow() == -1:
            self.feed_list.setCurrentRow(0)