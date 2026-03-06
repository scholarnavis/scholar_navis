import html
import json
import os
import re
import time
from datetime import datetime

from PySide6.QtCore import Qt, QUrl, QEvent, QMarginsF, QTimer, QRectF, QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QDesktopServices, QTextDocument, QPageLayout, QAbstractTextDocumentLayout, QPainter, QFont, \
    QColor
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                               QLabel, QListWidget, QSplitter, QListWidgetItem, QLineEdit, QCheckBox, QScrollArea,
                               QFileDialog, QFrame, QAbstractItemView, QMenu, QApplication)

from src.core.config_manager import ConfigManager
from src.core.core_task import TaskManager, TaskState
from src.core.signals import GlobalSignals
from src.core.theme_manager import ThemeManager
from src.task.rss_tasks import FetchRSSTask
from src.tools.base_tool import BaseTool
from src.ui.components.dialog import ProgressDialog, FeedEditorDialog, FeedLibraryDialog, StandardDialog
from src.ui.components.toast import ToastManager

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


class ArticleWidget(QFrame):
    def __init__(self, article_data, parent=None):
        super().__init__(parent)
        self.article_data = article_data
        self.setObjectName("ArticleFrame")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        header_layout = QHBoxLayout()

        self.checkbox = QCheckBox()
        header_layout.addWidget(self.checkbox)
        header_layout.addSpacing(5)

        if article_data.get('pdf_url'):
            self.icon_oa = QLabel()
            self.icon_oa.setToolTip("Open Access (OA)")
            header_layout.addWidget(self.icon_oa)

        title_link = f"<a href='{article_data['link']}' style='color:#05B8CC; text-decoration:none; font-size: 16px; font-weight:bold;'>{article_data['title']}</a>"
        self.lbl_title = QLabel(title_link)
        self.lbl_title.setOpenExternalLinks(True)
        self.lbl_title.setWordWrap(True)

        header_layout.addWidget(self.lbl_title, stretch=1)
        layout.addLayout(header_layout)

        self.meta_container = QWidget()
        self.meta_layout = QHBoxLayout(self.meta_container)
        self.meta_layout.setContentsMargins(0, 0, 0, 0)
        self.meta_layout.setSpacing(5)

        # 发布日期
        self.lbl_date_icon = QLabel()
        self.lbl_date_text = QLabel(article_data.get('pub_date', 'Unknown Date'))
        self.meta_layout.addWidget(self.lbl_date_icon)
        self.meta_layout.addWidget(self.lbl_date_text)

        # DOI
        if article_data.get('doi'):
            self.lbl_sep1 = QLabel(" | ")
            self.lbl_doi_icon = QLabel()
            self.lbl_doi_text = QLabel(f"DOI: {article_data['doi']}")
            self.meta_layout.addWidget(self.lbl_sep1)
            self.meta_layout.addWidget(self.lbl_doi_icon)
            self.meta_layout.addWidget(self.lbl_doi_text)

        # 标签
        if article_data.get('tags'):
            self.lbl_sep2 = QLabel(" | ")
            self.lbl_tag_icon = QLabel()
            self.lbl_tag_text = QLabel(", ".join(article_data['tags']))
            self.meta_layout.addWidget(self.lbl_sep2)
            self.meta_layout.addWidget(self.lbl_tag_icon)
            self.meta_layout.addWidget(self.lbl_tag_text)

        self.meta_layout.addStretch()
        layout.addWidget(self.meta_container)

        # 摘要区域
        self.text_browser = QLabel()
        self.text_browser.setOpenExternalLinks(True)
        self.text_browser.setTextFormat(Qt.RichText)
        self.text_browser.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.text_browser.setWordWrap(True)
        self.text_browser.setText(article_data.get('summary', ''))
        self.text_browser.installEventFilter(self)
        layout.addWidget(self.text_browser)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(25, 5, 0, 0)

        self.btn_trans = QPushButton(" Quick Translate")
        self.btn_trans.setCursor(Qt.PointingHandCursor)
        self.btn_trans.clicked.connect(self._send_to_translator)
        btn_layout.addWidget(self.btn_trans)

        self.btn_chat = QPushButton(" Send to Chat")
        self.btn_chat.setCursor(Qt.PointingHandCursor)
        self.btn_chat.clicked.connect(self._send_to_chat)
        btn_layout.insertWidget(1, self.btn_chat)

        self.btn_link = QPushButton(" Publisher Link")
        self.btn_link.setCursor(Qt.PointingHandCursor)
        self.btn_link.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.article_data['link'])))
        btn_layout.addWidget(self.btn_link)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

    def _apply_theme(self):
        tm = ThemeManager()
        bg_card = tm.color('bg_card')
        border = tm.color('border')
        text_main = tm.color('text_main')
        text_muted = tm.color('text_muted')
        btn_bg = tm.color('btn_bg')
        btn_hover = tm.color('btn_hover')

        self.setStyleSheet(
            f"QFrame#ArticleFrame {{ background-color: {bg_card}; border: 1px solid {border}; border-radius: 6px; }}")

        btn_style = f"QPushButton {{ background-color: {btn_bg}; color: {text_main}; border: 1px solid {border}; border-radius: 4px; padding: 4px 10px; }} QPushButton:hover {{ background-color: {btn_hover}; }}"

        if hasattr(self, 'btn_trans'):
            self.btn_trans.setIcon(tm.icon("translate", "text_main"))
            self.btn_trans.setStyleSheet(btn_style)

        if hasattr(self, 'btn_chat'):
            self.btn_chat.setIcon(tm.icon("send", "text_main"))
            self.btn_chat.setStyleSheet(btn_style)

        if hasattr(self, 'btn_link'):
            self.btn_link.setIcon(tm.icon("link", "text_main"))
            self.btn_link.setStyleSheet(btn_style)

        for lbl in [self.lbl_date_text, getattr(self, 'lbl_doi_text', None),
                    getattr(self, 'lbl_tag_text', None), getattr(self, 'lbl_sep1', None),
                    getattr(self, 'lbl_sep2', None)]:
            if lbl: lbl.setStyleSheet(f"color: {text_muted}; font-size: 12px;")

        if hasattr(self, 'icon_oa'):
            self.icon_oa.setPixmap(tm.icon("unlock", "success").pixmap(16, 16))

        if hasattr(self, 'lbl_date_icon'):
            self.lbl_date_icon.setPixmap(tm.icon("time", "text_muted").pixmap(14, 14))

        if hasattr(self, 'lbl_doi_icon'):
            self.lbl_doi_icon.setPixmap(tm.icon("link", "text_muted").pixmap(14, 14))

        if hasattr(self, 'lbl_tag_icon'):
            self.lbl_tag_icon.setPixmap(tm.icon("tag", "accent").pixmap(14, 14))


    def _send_to_chat(self):
        if hasattr(GlobalSignals(), 'sig_route_to_chat_with_mcp'):
            clean_summary = clean_html_text(self.article_data.get('summary', ''))
            context_text = f"Title: {self.article_data['title']}\nAbstract: {clean_summary}\nURL: {self.article_data.get('link', '')}"

            prompt = (
                "Please analyze the provided Title and Abstract of this article. "
                "1. Extract the primary research objective, key methodologies, and core findings.\n"
                "2. Evaluate its potential biological or clinical significance.\n"
                "*(Note: Since this is only an abstract, you may trigger the NCBI/Semantic Scholar tools to retrieve more metadata or related literature if you need deeper context.)*"
            )

            if hasattr(GlobalSignals(), 'sig_route_to_chat_with_mcp'):
                # 携带 "Literature" 标签触发带有文献 MCP 权限的聊天
                GlobalSignals().sig_route_to_chat_with_mcp.emit(context_text, prompt, "Literature")
            elif hasattr(GlobalSignals(), 'sig_send_to_chat'):
                # 降级模式，正常发送
                GlobalSignals().sig_send_to_chat.emit(context_text, prompt)

        elif hasattr(GlobalSignals(), 'sig_send_to_chat'):
            pass


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

        base_dir = self.config.BASE_DIR

        self.workspace_dir = os.path.join(base_dir, "scholar_workspace")
        os.makedirs(self.workspace_dir, exist_ok=True)

        config_dir = os.path.join(base_dir, "config")
        os.makedirs(config_dir, exist_ok=True)

        self.feeds_file = os.path.join(config_dir, "rss_feed.json")
        self.cache_file = os.path.join(self.workspace_dir, "rss_cache.json")

        self.feeds = []
        self.article_cache = {}
        self.last_fetched_time = "Never"
        self.current_article_widgets = []
        self.dl_thread = None

        self.task_mgr = TaskManager()

        self._load_config()

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

    def _apply_theme(self):
        if not hasattr(self, 'widget') or not self.widget:
            return

        tm = ThemeManager()

        bg_main = tm.color('bg_main')
        bg_card = tm.color('bg_card')
        border = tm.color('border')
        text_main = tm.color('text_main')
        btn_bg = tm.color('btn_bg')
        btn_hover = tm.color('btn_hover')

        # 列表与输入框
        if hasattr(self, 'feed_list'):
            self.feed_list.setStyleSheet(f"""
                QListWidget {{ background-color: {bg_main}; color: {text_main}; border: 1px solid {border}; border-radius: 4px; padding: 5px; }}
                QListWidget::item {{ padding: 4px 0px; border-bottom: 1px dashed {border}; }}
                QListWidget::item:selected {{ background-color: {tm.color('accent_hover')}; color: {tm.color('bg_card')}; font-weight: bold; }}
            """)

        if hasattr(self, 'inp_search_feed'):
            self.inp_search_feed.setStyleSheet(f"background-color: {bg_card}; color: {text_main}; border: 1px solid {border}; border-radius: 4px; padding: 5px;")

        # 顶部操作栏
        if hasattr(self, 'btn_manage'):
            self.btn_manage.setIcon(tm.icon("folder", "bg_main"))
            self.btn_manage.setStyleSheet(f"background-color: {tm.color('accent')}; color: {tm.color('bg_main')}; padding: 6px 15px; border-radius: 4px; font-weight: bold; border: none;")

        if hasattr(self, 'btn_edit'):
            self.btn_edit.setIcon(tm.icon("edit", "text_main"))
            self.btn_edit.setStyleSheet(f"background-color: {btn_bg}; color: {text_main}; padding: 6px 15px; border-radius: 4px; border: 1px solid {border};")

        if hasattr(self, 'btn_add'):
            self.btn_add.setIcon(tm.icon("add", "bg_main"))
            self.btn_add.setStyleSheet(
                f"background-color: {tm.color('success')}; color: {tm.color('bg_main')}; "
                f"padding: 6px 15px; border-radius: 4px; font-weight: bold; border: none;"
            )

        if hasattr(self, 'btn_unsub'):
            self.btn_unsub.setIcon(tm.icon("delete", "bg_main"))
            self.btn_unsub.setStyleSheet(f"background-color: {tm.color('danger')}; color: {tm.color('bg_main')}; padding: 6px 15px; border-radius: 4px; border: none;")

        if hasattr(self, 'btn_refresh'):
            self.btn_refresh.setIcon(tm.icon("sync", "bg_main"))
            self.btn_refresh.setStyleSheet(f"background-color: {tm.color('success')}; color: {tm.color('bg_main')}; padding: 6px 15px; border-radius: 4px; font-weight: bold; border: none;")

        # 小型选择按钮
        action_btn_style = f"QPushButton {{ background-color: {btn_bg}; color: {text_main}; border: 1px solid {border}; border-radius: 3px; padding: 4px 8px; font-size: 11px; }} QPushButton:hover {{ background-color: {btn_hover}; }}"
        for btn in [getattr(self, 'btn_feed_sel_all', None), getattr(self, 'btn_feed_sel_inv', None),
                    getattr(self, 'btn_sel_all', None), getattr(self, 'btn_sel_inv', None)]:
            if btn: btn.setStyleSheet(action_btn_style)

        if hasattr(self, 'btn_feed_sel_all'): self.btn_feed_sel_all.setIcon(tm.icon("check-circle", "text_main"))
        if hasattr(self, 'btn_sel_all'): self.btn_sel_all.setIcon(tm.icon("check-circle", "text_main"))
        if hasattr(self, 'btn_feed_sel_inv'): self.btn_feed_sel_inv.setIcon(tm.icon("refresh", "text_main"))
        if hasattr(self, 'btn_sel_inv'): self.btn_sel_inv.setIcon(tm.icon("refresh", "text_main"))

        # 右侧快捷操作按钮
        if hasattr(self, 'btn_batch_chat'):
            self.btn_batch_chat.setIcon(tm.icon("brain", "accent"))
            self.btn_batch_chat.setStyleSheet(f"QPushButton {{ color: {tm.color('accent')}; background-color: transparent; border: 1px solid {tm.color('accent')}; padding: 4px 8px; border-radius: 4px; font-weight: bold; }} QPushButton:hover {{ background-color: {tm.color('accent')}; color: {tm.color('bg_main')}; }}")

        if hasattr(self, 'btn_export_pdf'):
            self.btn_export_pdf.setIcon(tm.icon("file-text", "warning"))
            self.btn_export_pdf.setStyleSheet(f"QPushButton {{ color: {tm.color('warning')}; background-color: transparent; border: 1px solid {tm.color('warning')}; padding: 4px 8px; border-radius: 4px; font-weight: bold; }} QPushButton:hover {{ background-color: {tm.color('warning')}; color: {tm.color('bg_main')}; }}")




    def get_ui_widget(self) -> QWidget:
        if hasattr(self, 'widget'): return self.widget

        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(15, 15, 15, 15)

        toolbar = QHBoxLayout()
        self.btn_manage = QPushButton("Manage Subscriptions")
        self.btn_manage.setStyleSheet("background-color: #007acc; color: white; padding: 6px 15px; border-radius: 4px; font-weight: bold;")
        self.btn_manage.clicked.connect(self.open_subscription_manager)

        self.btn_edit = QPushButton("Edit Source")
        self.btn_edit.clicked.connect(self.edit_feed)

        self.btn_unsub = QPushButton("Unsubscribe Selected")
        self.btn_unsub.clicked.connect(lambda: self._batch_action("unsubscribe"))

        self.lbl_time = QLabel("Last Fetched: Never")
        self.lbl_time.setStyleSheet("color: #888; font-style: italic; margin-left: 10px;")

        self.btn_refresh = QPushButton("Sync Selected")
        self.btn_refresh.setStyleSheet("background-color: #28a745; color: white; font-weight: bold; padding: 6px 15px; border-radius: 4px;")
        self.btn_refresh.clicked.connect(lambda: self._batch_action("fetch"))

        self.btn_add = QPushButton(" Add Custom Source")
        self.btn_add.clicked.connect(self.add_custom_feed)


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
        self.inp_search_feed.setPlaceholderText("Search feeds...")
        self.inp_search_feed.textChanged.connect(self._filter_feed_list)
        left_layout.addWidget(self.inp_search_feed)

        left_action_bar = QHBoxLayout()
        self.btn_feed_sel_all = QPushButton("Select All")
        self.btn_feed_sel_inv = QPushButton("Invert")

        for btn in [self.btn_feed_sel_all, self.btn_feed_sel_inv]:
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet("""
                                QPushButton { background-color: #333; color: #ccc; border-radius: 3px; padding: 4px 8px; font-size: 11px; border: 1px solid #444; } 
                                QPushButton:hover { background-color: #444; color: white; }
                            """)


        left_action_bar.addWidget(self.btn_feed_sel_all)
        left_action_bar.addWidget(self.btn_feed_sel_inv)
        left_action_bar.addStretch()
        left_layout.addLayout(left_action_bar)
        self.btn_feed_sel_all.clicked.connect(lambda: self._batch_select_feeds(True))
        self.btn_feed_sel_inv.clicked.connect(lambda: self._batch_select_feeds("invert"))

        self.feed_list = QListWidget()
        self.feed_list.itemDoubleClicked.connect(lambda item: self.edit_feed())
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
        self.btn_sel_all = QPushButton("Select All")
        self.btn_sel_inv = QPushButton("Invert")
        self.btn_sel_all.clicked.connect(lambda: self._batch_select(True))
        self.btn_sel_inv.clicked.connect(lambda: self._batch_select("invert"))

        self.btn_batch_chat = QPushButton("Analyze Selected")
        self.btn_batch_chat.setStyleSheet("color: #8be9fd;")
        self.btn_batch_chat.clicked.connect(self.batch_send_to_chat)

        self.btn_export_pdf = QPushButton("Export to PDF")
        self.btn_export_pdf.setStyleSheet("color: #ffb86c;")
        self.btn_export_pdf.clicked.connect(self.export_to_pdf)


        action_bar.addWidget(self.btn_sel_all)
        action_bar.addWidget(self.btn_sel_inv)
        action_bar.addStretch()
        action_bar.addWidget(self.btn_batch_chat)
        action_bar.addWidget(self.btn_export_pdf)
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

        self.render_timer = QTimer(self.widget)
        self.render_timer.timeout.connect(self._render_batch)
        self.render_queue = []
        self.current_render_url = ""

        self._load_cache()
        self._refresh_feed_ui()
        self.task_mgr.sig_log.connect(self.on_task_log)
        self._apply_theme()
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

    def add_custom_feed(self):
        new_feed = {"name": "", "url": "", "category": "Custom"}
        dlg = FeedEditorDialog(self.widget, new_feed, is_default=False)
        if dlg.exec():
            data = dlg.get_data()
            if data.get('url'):
                data['is_default'] = False
                self.feeds.append(data)
                self._save_config()
                self._refresh_feed_ui()  # 立刻刷新 UI
                ToastManager().show("Custom source added successfully.", "success")

    def batch_send_to_chat(self):
        selected = [w.article_data for w in self.current_article_widgets if w.is_checked()]
        if not selected:
            ToastManager().show("Please check at least one article to analyze.", "warning")
            return

        context = "### Selected Literature for Analysis ###\n\n"
        for i, art in enumerate(selected):
            clean_summary = clean_html_text(art.get('summary', ''))
            context += f"[{i + 1}] Title: {art['title']}\nAbstract: {clean_summary}\nURL: {art.get('link', '')}\n\n"

        prompt = (
            "Based on the Titles and Abstracts of these selected articles, please provide a synthesized summary:\n"
            "1. **Thematic Overview**: Identify the overarching research trends or common problems addressed in these papers.\n"
            "2. **Methodological/Finding Breakdown**: Briefly categorize their distinct methodologies or highlight overlapping conclusions.\n"
            "3. **Synthesis**: Are there any contradictions or synergistic implications among these studies?\n\n"
            "*(Note: You may use your connected Literature MCP tools to fetch deeper context or metadata if needed.)*"
        )

        if hasattr(GlobalSignals(), 'sig_route_to_chat_with_mcp'):
            GlobalSignals().sig_route_to_chat_with_mcp.emit(context, prompt, "Literature")
        elif hasattr(GlobalSignals(), 'sig_send_to_chat'):
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
        tm = ThemeManager()
        menu = QMenu(self.widget)
        menu.setStyleSheet(f"""
            QMenu {{ background-color: {tm.color('bg_card')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; padding: 4px; border-radius: 4px; }} 
            QMenu::item {{ padding: 6px 20px; }}
            QMenu::item:selected {{ background-color: {tm.color('accent')}; color: #fff; }}
        """)

        action_fetch = menu.addAction(tm.icon("sync", "success"), "Fetch Checked / Clicked")
        action_unsub = menu.addAction(tm.icon("delete", "danger"), "Unsubscribe Checked / Clicked")

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

        # Only fallback if it's a specific Right-Click context menu action
        if not indices and pos is not None:
            item = self.feed_list.itemAt(pos)
            if item:
                indices.append(self.feed_list.row(item))


        return sorted(list(set(indices)), reverse=True)

    def _batch_action(self, action_type, pos=None):
        indices = self._get_target_feed_indices(pos)

        if not indices:
            if action_type == "fetch":
                ToastManager().show("Please check the box next to the feeds you want to sync.", "info")
            return

        if action_type == "unsubscribe":
            # --- 使用你的现代主题对话框 ---
            dlg = StandardDialog(
                self.widget,
                title="Confirm Bulk Unsubscribe",
                message=f"Are you sure you want to remove {len(indices)} feeds from your tracker?",
                show_cancel=True
            )

            if dlg.exec():
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
        dlg = FeedLibraryDialog(self.widget, self.feeds, DEFAULT_FEEDS_DICT)
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
            lbl.setStyleSheet(f"color: {ThemeManager().color('text_muted')}; padding: 20px;")
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

        # 1. 启动进度对话框
        telemetry_off = {"cpu": False, "ram": False, "gpu": False, "net": False, "io": False}
        pd = ProgressDialog(self.widget, "Exporting to PDF", "Preparing document layout...",
                            telemetry_config=telemetry_off)
        pd.show()

        self._cancel_export = False

        def on_cancel():
            self._cancel_export = True

        pd.sig_canceled.connect(on_cancel)
        QApplication.instance().processEvents()

        # 2. 将 ThemeManager 图标转为 Base64 字符串供 HTML 内联
        def get_b64_icon(icon_name, color_key):
            pixmap = ThemeManager().icon(icon_name, color_key).pixmap(16, 16)
            byte_array = QByteArray()
            buffer = QBuffer(byte_array)
            buffer.open(QIODevice.WriteOnly)
            pixmap.save(buffer, "PNG")
            return f"data:image/png;base64,{bytes(byte_array.toBase64()).decode('utf-8')}"

        icon_time_b64 = get_b64_icon("time", "text_muted")
        icon_link_b64 = get_b64_icon("link", "text_muted")
        icon_oa_b64 = get_b64_icon("unlock", "success")

        # 3. 拼接带有内嵌图片的 HTML 内容
        html = f"""
        <h1 style='color: #333;'>{feed_name}</h1>
        <p style='color: #666;'>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
        <hr>
        """

        for art in selected:
            landing_url = art.get('link', '#')
            html += f"<h3><a href='{landing_url}' style='color:#05B8CC; text-decoration:none;'>{art['title']}</a></h3>"

            # 使用内嵌的 base64 图像替代原本的 Emoji
            date_str = art.get('pub_date', 'Unknown Date')
            date_html = f"<img src='{icon_time_b64}' width='12' height='12' style='vertical-align: middle;'> {date_str}"

            doi_val = art.get('doi', '')
            doi_html = f" | <img src='{icon_link_b64}' width='12' height='12' style='vertical-align: middle;'> DOI: <a href='https://doi.org/{doi_val}' style='color:#05B8CC; text-decoration:none;'>{doi_val}</a>" if doi_val else ""

            oa_url = art.get('pdf_url', '')
            oa_html = f" | <img src='{icon_oa_b64}' width='12' height='12' style='vertical-align: middle;'> <b style='color:#28a745;'>Open Access</b>" if oa_url else ""

            html += f"<p style='color:#555555; font-size: 10pt;'>{date_html}{doi_html}{oa_html}</p>"
            html += f"<div style='color:#333333; font-size: 11pt; line-height: 1.5;'>{art.get('summary', '')}</div><hr>"

        # 4. 配置打印机与文档
        pd.update_progress(0, "Calculating pages...")
        QApplication.instance().processEvents()


        doc = QTextDocument()
        doc.setHtml(html)

        printer = QPrinter(QPrinter.ScreenResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        margin = QMarginsF(15, 15, 15, 22)
        printer.setPageMargins(margin, QPageLayout.Millimeter)
        printer.setOutputFileName(path)

        page_rect = printer.pageRect(QPrinter.DevicePixel)
        doc.setPageSize(page_rect.size())

        painter = QPainter(printer)
        page_count = doc.pageCount()

        # 5. 核心渲染循环：按页绘制并更新进度
        for page_idx in range(page_count):
            if self._cancel_export:
                painter.end()
                printer.setOutputFileName("")
                pd.close_safe()

                cleaned = False
                for _ in range(3):
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                        cleaned = True
                        break
                    except PermissionError:
                        time.sleep(0.1)

                if cleaned or not os.path.exists(path):
                    ToastManager().show("PDF export cancelled. Temporary file cleaned.", "warning")
                else:
                    ToastManager().show("Export cancelled, but partial file is locked by system.", "warning")

                return

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

            percent = int(((page_idx + 1) / page_count) * 100)
            pd.update_progress(percent, f"Rendering page {page_idx + 1} of {page_count}...")
            QApplication.instance().processEvents()

        painter.end()
        pd.show_success_state("Complete", f"Successfully exported {page_count} pages to PDF.")


    def _load_config(self):
        saved_feeds = self.config.load_json(self.feeds_file, encrypt=False) or []

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

        self.feeds = saved_feeds


    def _save_config(self):
        self.config.save_json(self.feeds_file, self.feeds, encrypt=False)


    def _load_cache(self):
        data = self.config.load_json(self.cache_file, encrypt=False)
        if data:
            meta = data.pop("_meta", {})
            self.last_fetched_time = meta.get("last_fetched", "Unknown")
            self.article_cache = data
            if hasattr(self, 'lbl_time'):
                self.lbl_time.setText(f"Last Fetched: {self.last_fetched_time}")


    def _refresh_feed_ui(self):
        self.feed_list.blockSignals(True)
        self.feed_list.clear()

        self.feeds.sort(key=lambda x: (x.get('category', 'Z'), x['name']))

        tm = ThemeManager()
        color_default = QColor(tm.color('text_muted'))
        color_custom = QColor(tm.color('text_main'))

        for feed in self.feeds:
            cat_prefix = f"[{feed.get('category', 'Other')}] "
            kws = feed.get('keywords', '')
            suffix = " 🔍" if kws else ""

            item_text = f"{cat_prefix}{feed['name']}{suffix}"
            item = QListWidgetItem(item_text)

            if feed.get("is_default"):
                # 内置源：使用 lock 图标，颜色用次要文本色
                item.setIcon(tm.icon("lock", "text_muted"))
                item.setForeground(color_default)
            else:
                item.setIcon(tm.icon("link", "accent"))
                item.setForeground(color_custom)

            item.setData(Qt.UserRole, feed['url'])
            item.setToolTip(f"Category: {feed.get('category', 'Other')}\nSource: {feed['name']}\nURL: {feed['url']}")

            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)

            self.feed_list.addItem(item)

        self.feed_list.blockSignals(False)

        if self.feed_list.count() > 0 and self.feed_list.currentRow() == -1:
            self.feed_list.setCurrentRow(0)