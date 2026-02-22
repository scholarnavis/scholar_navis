import os
import json
import requests
from datetime import datetime

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                               QLabel, QListWidget, QSplitter, QInputDialog,
                               QTextBrowser, QListWidgetItem, QDialog, QLineEdit, QFormLayout,
                               QCheckBox, QScrollArea, QFileDialog, QTableWidget, QHeaderView,
                               QTableWidgetItem, QFrame, QAbstractItemView)
from PySide6.QtCore import Qt, QUrl, QEvent, QThread, Signal
from PySide6.QtGui import QDesktopServices, QTextDocument
from PySide6.QtPrintSupport import QPrinter

from src.tools.base_tool import BaseTool
from src.core.core_task import TaskManager, TaskState
from src.task.rss_tasks import FetchRSSTask
from src.ui.components.toast import ToastManager
from src.ui.components.dialog import ProgressDialog, StandardDialog
from src.core.signals import GlobalSignals

# ==========================================
# 📚 内置核心期刊与数据库源
# ==========================================
BUILT_IN_FEEDS = [
    {"name": "Nature (Biological Sciences)", "url": "https://www.nature.com/subjects/biological-sciences.rss",
     "tags": "Biology, Top Tier"},
    {"name": "Science (Current Issue)", "url": "https://science.org/rss/current.xml", "tags": "General, Top Tier"},
    {"name": "Cell (Current Issue)",
     "url": "https://marlin-prod.literatumonline.com/action/showFeed?jc=cell&type=etoc&feed=rss",
     "tags": "Biology, Top Tier"},
    {"name": "Nucleic Acids Research", "url": "https://academic.oup.com/rss/site_5361/3196.xml",
     "tags": "Bioinformatics"},
    {"name": "Bioinformatics", "url": "https://academic.oup.com/rss/site_5134/3041.xml", "tags": "Bioinformatics"},
    {"name": "The Plant Cell", "url": "https://academic.oup.com/rss/site_5368/3224.xml", "tags": "Plant Biology"},
    {"name": "Plant Physiology", "url": "https://academic.oup.com/rss/site_5431/3282.xml", "tags": "Plant Biology"},
    {"name": "eLife (Latest)", "url": "https://elifesciences.org/rss/recent.xml", "tags": "Biology, OA"},
    {"name": "PLOS Biology", "url": "https://journals.plos.org/plosbiology/feed/atom", "tags": "Biology, OA"},
    {"name": "bioRxiv (Plant Biology)", "url": "https://connect.biorxiv.org/biorxiv_xml.php?subject=plant_biology",
     "tags": "Preprint, Plant"},
    {"name": "bioRxiv (Bioinformatics)", "url": "https://connect.biorxiv.org/biorxiv_xml.php?subject=bioinformatics",
     "tags": "Preprint, Bioinfo"},
]


# --- 独立下载线程 (防假死) ---
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
        self.setWindowTitle("📚 Discover Academic Feeds")
        self.resize(750, 500)
        self.setStyleSheet("background-color: #1e1e1e; color: white;")
        layout = QVBoxLayout(self)

        lbl = QLabel("Select predefined journals or databases to add to your tracker:")
        lbl.setStyleSheet("color: #888; margin-bottom: 5px;")
        layout.addWidget(lbl)

        # 🌟 新增：全选多选框
        self.chk_select_all = QCheckBox("☑ Select All Valid Feeds")
        self.chk_select_all.setCursor(Qt.PointingHandCursor)
        self.chk_select_all.setStyleSheet("color: #05B8CC; font-weight: bold; margin-bottom: 5px;")
        self.chk_select_all.clicked.connect(self._toggle_all)
        layout.addWidget(self.chk_select_all)

        self.table = QTableWidget(len(BUILT_IN_FEEDS), 4)
        self.table.setHorizontalHeaderLabels(["Add?", "Journal / Source", "Tags", "RSS URL"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setStyleSheet("QTableWidget { background-color: #252526; gridline-color: #333; }")

        existing_urls = [f.get("url") for f in current_feeds] if current_feeds else []
        self.checkboxes = []

        for i, feed in enumerate(BUILT_IN_FEEDS):
            chk = QCheckBox()
            if feed["url"] in existing_urls:
                chk.setEnabled(False)
                chk.setToolTip("Already exists in your tracker.")
            else:
                chk.setChecked(False)
            self.checkboxes.append((chk, feed))

            chk_widget = QWidget()
            chk_layout = QHBoxLayout(chk_widget)
            chk_layout.addWidget(chk)
            chk_layout.setAlignment(Qt.AlignCenter)
            chk_layout.setContentsMargins(0, 0, 0, 0)

            self.table.setCellWidget(i, 0, chk_widget)
            self.table.setItem(i, 1, QTableWidgetItem(feed['name']))
            self.table.setItem(i, 2, QTableWidgetItem(feed['tags']))
            self.table.setItem(i, 3, QTableWidgetItem(feed['url']))

        layout.addWidget(self.table)

        btn_box = QHBoxLayout()
        btn_add = QPushButton("➕ Add Selected")
        btn_add.setStyleSheet("background-color: #007acc; color: white; padding: 6px 15px; border-radius: 4px;")
        btn_add.clicked.connect(self.accept)

        btn_custom = QPushButton("Custom Entry...")
        btn_custom.clicked.connect(self.reject_with_custom)

        btn_box.addWidget(btn_custom)
        btn_box.addStretch()
        btn_box.addWidget(btn_add)
        layout.addLayout(btn_box)

        self.custom_trigger = False

    def _toggle_all(self):
        state = self.chk_select_all.isChecked()
        for chk, feed in self.checkboxes:
            if chk.isEnabled():
                chk.setChecked(state)

    def reject_with_custom(self):
        self.custom_trigger = True
        self.reject()

    def get_selected_feeds(self):
        return [{
            "name": feed["name"],
            "url": feed["url"],
            "keywords": ""
        } for chk, feed in self.checkboxes if chk.isChecked() and chk.isEnabled()]


class RestoreFeedsDialog(QDialog):
    def __init__(self, parent=None, current_feeds=None):
        super().__init__(parent)
        self.setWindowTitle("🔁 Restore Built-in Feeds")
        self.resize(600, 400)
        self.setStyleSheet("background-color: #1e1e1e; color: white;")
        layout = QVBoxLayout(self)

        lbl = QLabel("Select default academic feeds to restore:")
        lbl.setStyleSheet("color: #888; margin-bottom: 5px;")
        layout.addWidget(lbl)

        # 🌟 新增：全选多选框
        self.chk_select_all = QCheckBox("☑ Select All Valid Feeds")
        self.chk_select_all.setCursor(Qt.PointingHandCursor)
        self.chk_select_all.setStyleSheet("color: #05B8CC; font-weight: bold; margin-bottom: 5px;")
        self.chk_select_all.setChecked(True)  # 恢复界面默认全选比较好
        self.chk_select_all.clicked.connect(self._toggle_all)
        layout.addWidget(self.chk_select_all)

        self.table = QTableWidget(len(BUILT_IN_FEEDS), 2)
        self.table.setHorizontalHeaderLabels(["Restore?", "Journal / Source"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setStyleSheet("QTableWidget { background-color: #252526; gridline-color: #333; }")

        existing_urls = [f.get("url") for f in current_feeds] if current_feeds else []
        self.checkboxes = []

        for i, feed in enumerate(BUILT_IN_FEEDS):
            chk = QCheckBox()
            if feed["url"] in existing_urls:
                chk.setEnabled(False)
                chk.setToolTip("Already exists in your tracker.")
            else:
                chk.setChecked(True)
            self.checkboxes.append((chk, feed))

            chk_widget = QWidget()
            chk_layout = QHBoxLayout(chk_widget)
            chk_layout.addWidget(chk)
            chk_layout.setAlignment(Qt.AlignCenter)
            chk_layout.setContentsMargins(0, 0, 0, 0)

            self.table.setCellWidget(i, 0, chk_widget)
            self.table.setItem(i, 1, QTableWidgetItem(feed['name']))

        layout.addWidget(self.table)

        btn_box = QHBoxLayout()
        btn_add = QPushButton("🔁 Restore Selected")
        btn_add.setStyleSheet("background-color: #007acc; color: white; padding: 6px 15px; border-radius: 4px;")
        btn_add.clicked.connect(self.accept)
        btn_box.addStretch()
        btn_box.addWidget(btn_add)
        layout.addLayout(btn_box)

    def _toggle_all(self):
        state = self.chk_select_all.isChecked()
        for chk, feed in self.checkboxes:
            if chk.isEnabled():
                chk.setChecked(state)

    def get_feeds_to_restore(self):
        return [feed for chk, feed in self.checkboxes if chk.isChecked() and chk.isEnabled()]

class FeedEditorDialog(QDialog):
    def __init__(self, parent=None, feed_data=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Tracker Rule")
        self.setFixedSize(500, 220)
        self.setStyleSheet("background-color: #252526; color: white;")
        layout = QFormLayout(self)

        self.inp_name = QLineEdit(feed_data.get('name', '') if feed_data else '')
        self.inp_url = QLineEdit(feed_data.get('url', '') if feed_data else '')
        self.inp_keywords = QLineEdit(feed_data.get('keywords', '') if feed_data else '')
        self.inp_keywords.setPlaceholderText("e.g. scRNA-seq, cotton (Leave blank for all)")

        for inp in [self.inp_name, self.inp_url, self.inp_keywords]:
            inp.setStyleSheet("background:#1e1e1e; border:1px solid #444; padding:5px; border-radius:3px;")

        layout.addRow("Source Name:", self.inp_name)
        layout.addRow("RSS XML URL:", self.inp_url)
        layout.addRow("Keyword Filter:", self.inp_keywords)

        hint = QLabel("Only papers containing these keywords in Title/Abstract will be kept.")
        hint.setStyleSheet("color:#888; font-size:11px;")
        layout.addRow("", hint)

        btn_box = QHBoxLayout()
        btn_save = QPushButton("Save Rule")
        btn_save.setStyleSheet("background-color: #007acc; color: white; padding: 6px; border-radius: 4px;")
        btn_save.clicked.connect(self.accept)
        btn_box.addStretch()
        btn_box.addWidget(btn_save)
        layout.addRow(btn_box)

    def get_data(self):
        return {
            "name": self.inp_name.text().strip(),
            "url": self.inp_url.text().strip(),
            "keywords": self.inp_keywords.text().strip()
        }


# ==========================================
# 独立文章卡片组件
# ==========================================
class ArticleWidget(QFrame):
    def __init__(self, article_data, parent=None):
        super().__init__(parent)
        self.article_data = article_data
        self.setStyleSheet(
            "QFrame { background-color: #252526; border: 1px solid #333; border-radius: 6px; margin-bottom: 10px; padding: 10px; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        header_layout = QHBoxLayout()
        self.checkbox = QCheckBox()
        self.checkbox.setStyleSheet("QCheckBox::indicator { width: 16px; height: 16px; }")

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
        lbl_meta.setStyleSheet(
            "color: #888; font-size: 12px; border: none; background: transparent; padding-left: 25px;")
        layout.addWidget(lbl_meta)

        self.text_browser = QTextBrowser()
        self.text_browser.setOpenExternalLinks(True)
        self.text_browser.setHtml(article_data.get('summary', ''))
        self.text_browser.setStyleSheet("""
            QTextBrowser { background: transparent; color: #d4d4d4; border: none; font-size: 13px; line-height: 1.5; selection-background-color: #05B8CC; padding-left: 20px;}
        """)
        self.text_browser.document().setTextWidth(600)
        doc_height = self.text_browser.document().size().height()
        self.text_browser.setFixedHeight(int(max(50, min(doc_height + 20, 600))))
        self.text_browser.installEventFilter(self)
        layout.addWidget(self.text_browser)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(25, 5, 0, 0)

        btn_trans = QPushButton("🌐 Quick Translate")
        btn_trans.setCursor(Qt.PointingHandCursor)
        btn_trans.setStyleSheet(
            "QPushButton { background-color: #333; color: #e0e0e0; border-radius: 4px; padding: 4px 10px; font-size: 12px; border: 1px solid #555; } QPushButton:hover { background-color: #05B8CC; color: white; border: 1px solid #05B8CC; }")
        btn_trans.clicked.connect(self._send_to_translator)
        btn_layout.addWidget(btn_trans)

        if article_data.get('pdf_url'):
            btn_dl = QPushButton("⬇️ Download OA PDF")
            btn_dl.setCursor(Qt.PointingHandCursor)
            btn_dl.setStyleSheet(
                "QPushButton { background-color: #28a745; color: white; border-radius: 4px; padding: 4px 10px; font-size: 12px; border: none; font-weight: bold;} QPushButton:hover { background-color: #218838; }")
            btn_dl.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.article_data['pdf_url'])))
            btn_layout.addWidget(btn_dl)
        else:
            btn_link = QPushButton("🔗 Publisher Link (Non-OA)")
            btn_link.setCursor(Qt.PointingHandCursor)
            btn_link.setStyleSheet(
                "QPushButton { background-color: #444; color: #aaa; border-radius: 4px; padding: 4px 10px; font-size: 12px; border: none;} QPushButton:hover { background-color: #555; color: #fff; }")
            btn_link.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.article_data['link'])))
            btn_layout.addWidget(btn_link)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

    def eventFilter(self, obj, event):
        if obj == self.text_browser and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Space:
                selected_text = self.text_browser.textCursor().selectedText()
                if selected_text and hasattr(GlobalSignals(), 'sig_invoke_translator'):
                    GlobalSignals().sig_invoke_translator.emit(selected_text)
                    cursor = self.text_browser.textCursor()
                    cursor.clearSelection()
                    self.text_browser.setTextCursor(cursor)
                    return True
        return super().eventFilter(obj, event)

    def _send_to_translator(self):
        if hasattr(GlobalSignals(), 'sig_invoke_translator'):
            text = f"{self.article_data['title']}\n\n{self.article_data.get('summary', '')}"
            import re
            clean_text = re.sub(r'<[^>]+>', '', text)
            GlobalSignals().sig_invoke_translator.emit(clean_text)

    def is_checked(self):
        return self.checkbox.isChecked()

    def set_checked(self, state):
        self.checkbox.setChecked(state)


# ==========================================
# 🌟 主工具界面
# ==========================================
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
        self.btn_lib = QPushButton("📚 Add Source")
        self.btn_lib.setStyleSheet("background-color: #007acc; color: white; padding: 6px 12px; border-radius: 4px;")
        self.btn_lib.clicked.connect(self.open_feed_library)

        self.btn_edit = QPushButton("✏️ Edit")
        self.btn_edit.clicked.connect(self.edit_feed)
        self.btn_del = QPushButton("🗑️ Remove")
        self.btn_del.clicked.connect(self.remove_feed)

        self.btn_restore = QPushButton("🔁 Restore")
        self.btn_restore.clicked.connect(self.restore_default_feeds)

        self.lbl_time = QLabel("Last Fetched: Never")
        self.lbl_time.setStyleSheet("color: #888; font-style: italic; margin-left: 10px;")

        self.btn_refresh = QPushButton("🔄 Sync Latest Papers")
        self.btn_refresh.setStyleSheet(
            "background-color: #28a745; color: white; font-weight: bold; padding: 6px 15px; border-radius: 4px;")
        self.btn_refresh.clicked.connect(self.refresh_all_feeds)

        toolbar.addWidget(self.btn_lib)
        toolbar.addWidget(self.btn_edit)
        toolbar.addWidget(self.btn_del)
        toolbar.addWidget(self.btn_restore)
        toolbar.addWidget(self.lbl_time)
        toolbar.addStretch()
        toolbar.addWidget(self.btn_refresh)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Horizontal)

        self.feed_list = QListWidget()
        self.feed_list.currentRowChanged.connect(self._on_feed_selected)
        self.feed_list.setStyleSheet(
            "background-color: #1e1e1e; color: #e0e0e0; border: 1px solid #333; border-radius: 4px; padding: 5px;")
        splitter.addWidget(self.feed_list)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)

        action_bar = QHBoxLayout()
        self.btn_sel_all = QPushButton("☑ Select All")
        self.btn_sel_inv = QPushButton("🔲 Invert")
        self.btn_sel_all.clicked.connect(lambda: self._batch_select(True))
        self.btn_sel_inv.clicked.connect(lambda: self._batch_select("invert"))

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

        splitter.setSizes([280, 800])
        layout.addWidget(splitter, stretch=1)

        self._load_cache()
        self._refresh_feed_ui()
        self.task_mgr.sig_log.connect(self.on_task_log)

        return self.widget

    # --- 交互逻辑 ---
    def open_feed_library(self):
        try:
            # 💡 核心修改：传入 self.feeds 给弹窗用于排重判断
            dlg = FeedLibraryDialog(self.widget, self.feeds)
            if dlg.exec():
                selected = dlg.get_selected_feeds()
                if selected:
                    self.feeds.extend(selected)
                    self._save_config()
                    self._refresh_feed_ui()
                    ToastManager().show(f"Added {len(selected)} sources. Click 'Sync Latest Papers' to fetch.",
                                        "success")
            elif dlg.custom_trigger:
                self._add_custom_feed()
        except Exception as e:
            ToastManager().show(f"Error opening library: {str(e)}", "error")


    def _add_custom_feed(self):
        dlg = FeedEditorDialog(self.widget)
        if dlg.exec():
            new_data = dlg.get_data()
            if new_data['url']:
                self.feeds.append(new_data)
                self._save_config()
                self._refresh_feed_ui()

    def restore_default_feeds(self):
        dlg = RestoreFeedsDialog(self.widget, self.feeds)
        if dlg.exec():
            to_restore = dlg.get_feeds_to_restore()
            if to_restore:
                for f in to_restore: f["keywords"] = ""
                self.feeds.extend(to_restore)
                self._save_config()
                self._refresh_feed_ui()
                ToastManager().show(f"Successfully restored {len(to_restore)} feeds.", "success")

    def edit_feed(self):
        row = self.feed_list.currentRow()
        if row < 0:
            ToastManager().show("Please select a feed from the list on the left to edit.", "warning")
            return
        dlg = FeedEditorDialog(self.widget, self.feeds[row])
        if dlg.exec():
            new_data = dlg.get_data()
            if new_data['url']:
                self.feeds[row] = new_data
                self._save_config()
                self._refresh_feed_ui()

    def remove_feed(self):
        row = self.feed_list.currentRow()
        if row < 0:
            ToastManager().show("Please select a feed to remove.", "warning")
            return
        del self.feeds[row]
        self._save_config()
        self._refresh_feed_ui()
        self._clear_articles()
        ToastManager().show("Feed removed successfully.", "success")

    def refresh_all_feeds(self):
        if not self.feeds:
            ToastManager().show("Your tracker list is empty. Add a source first.", "warning")
            return

        # 修复点：移除 telemetry_config 参数防止抛出 TypeError
        self.pd = ProgressDialog(self.widget, "Fetching Literature", "Connecting to servers and detecting OA...")
        self.pd.show()

        self.task_mgr.sig_progress.connect(self.pd.update_progress)
        self.task_mgr.sig_state_changed.connect(self._on_fetch_done)
        self.pd.sig_canceled.connect(self.task_mgr.cancel_task)

        self.task_mgr.start_task(FetchRSSTask, "rss_fetch", feeds=self.feeds, save_path=self.cache_file)

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
            self._on_feed_selected(self.feed_list.currentRow())
        else:
            self.pd.close_safe()
            ToastManager().show(f"Fetch failed: {msg}", "error")

    # --- 渲染逻辑 ---
    def _clear_articles(self):
        self.current_article_widgets = []
        while self.article_layout.count() > 1:
            item = self.article_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()

    def _on_feed_selected(self, row):
        if row < 0: return
        self._clear_articles()

        url = self.feeds[row]['url']
        articles = self.article_cache.get(url, [])

        if not articles:
            lbl = QLabel("No data available. Click 'Sync Latest Papers' to pull data.")
            lbl.setStyleSheet("color: #888; padding: 20px;")
            self.article_layout.insertWidget(0, lbl)
            return

        for art in articles:
            w = ArticleWidget(art)
            self.current_article_widgets.append(w)
            self.article_layout.insertWidget(self.article_layout.count() - 1, w)

    # --- 批量操作 ---
    def _batch_select(self, mode):
        if not self.current_article_widgets: return
        for w in self.current_article_widgets:
            if mode == "invert":
                w.set_checked(not w.is_checked())
            else:
                w.set_checked(bool(mode))

    def export_to_pdf(self):
        selected = [w.article_data for w in self.current_article_widgets if w.is_checked()]
        if not selected:
            ToastManager().show("Please select at least one article to export.", "warning")
            return

        path, _ = QFileDialog.getSaveFileName(self.widget, "Export to PDF", "Literature_Report.pdf",
                                              "PDF Files (*.pdf)")
        if not path: return

        html = f"<h1>Literature Report</h1><p>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p><hr>"
        for art in selected:
            html += f"<h3>{art['title']}</h3>"
            html += f"<p style='color:gray;'><b>Date:</b> {art.get('pub_date', '')} | <b>DOI:</b> {art.get('doi', '')}</p>"
            html += f"<div>{art.get('summary', '')}</div><hr>"

        doc = QTextDocument()
        doc.setHtml(html)
        printer = QPrinter(QPrinter.HighResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        printer.setOutputFileName(path)
        doc.print_(printer)
        ToastManager().show("PDF report exported successfully.", "success")

    def batch_download_pdfs(self):
        selected_urls = [w.article_data['pdf_url'] for w in self.current_article_widgets if
                         w.is_checked() and w.article_data.get('pdf_url')]
        if not selected_urls:
            ToastManager().show("None of the selected articles have OA PDF links available.", "warning")
            return

        save_dir = QFileDialog.getExistingDirectory(self.widget, "Select Folder to Save PDFs")
        if not save_dir: return

        # 使用安全的 QThread 避免卡死或 Toast 崩溃
        self.dl_thread = DownloadWorker(selected_urls, save_dir)
        self.dl_thread.sig_msg.connect(lambda msg, lvl: ToastManager().show(msg, lvl))
        self.dl_thread.finished.connect(self.dl_thread.deleteLater)
        self.dl_thread.start()

        ToastManager().show("Batch download started in background...", "info")

    # --- 辅助方法 ---
    def _load_config(self):
        from src.core.config_manager import ConfigManager
        cfg = ConfigManager()
        saved_feeds = cfg.user_settings.get("rss_feeds", [])

        if not saved_feeds and os.path.exists(self.feeds_file):
            try:
                with open(self.feeds_file, 'r', encoding='utf-8') as f:
                    saved_feeds = json.load(f)
                    cfg.user_settings["rss_feeds"] = saved_feeds
                    cfg.save_settings()
            except:
                pass

        self.feeds = saved_feeds if saved_feeds else []

    def _save_config(self):
        from src.core.config_manager import ConfigManager
        cfg = ConfigManager()
        cfg.user_settings["rss_feeds"] = self.feeds
        cfg.save_settings()
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
        for feed in self.feeds:
            kws = feed.get('keywords', '')
            suffix = " 🔍" if kws else ""
            item = QListWidgetItem(f"📰 {feed['name']}{suffix}")
            item.setData(Qt.UserRole, feed['url'])
            self.feed_list.addItem(item)
        self.feed_list.blockSignals(False)