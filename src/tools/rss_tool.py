import os
import json
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                               QLabel, QListWidget, QSplitter, QInputDialog,
                               QTextBrowser, QListWidgetItem)
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices

from src.tools.base_tool import BaseTool
from src.core.core_task import TaskManager, TaskState
from src.task.rss_tasks import FetchRSSTask
from src.ui.components.toast import ToastManager
from src.ui.components.dialog import ProgressDialog


class RSSTool(BaseTool):
    def __init__(self):
        super().__init__("Academic Tracker (RSS)")
        self.workspace_dir = os.path.join(os.getcwd(), "scholar_workspace")
        self.feeds_file = os.path.join(self.workspace_dir, "rss_subscriptions.json")
        self.cache_file = os.path.join(self.workspace_dir, "rss_cache.json")

        # 默认内置一些顶级生物/学术源
        self.feeds = [
            {"name": "Nature (Biology)", "url": "https://www.nature.com/subjects/biological-sciences.rss"},
            {"name": "bioRxiv (Plant Biology)",
             "url": "https://connect.biorxiv.org/biorxiv_xml.php?subject=plant_biology"}
        ]
        self.article_cache = {}
        self.task_mgr = TaskManager()

        os.makedirs(self.workspace_dir, exist_ok=True)
        self._load_config()

    def get_ui_widget(self) -> QWidget:
        if hasattr(self, 'widget'): return self.widget

        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(15, 15, 15, 15)

        # 顶部工具栏
        toolbar = QHBoxLayout()
        self.btn_add = QPushButton("➕ Add Feed")
        self.btn_add.clicked.connect(self.add_feed)
        self.btn_del = QPushButton("🗑️ Remove")
        self.btn_del.clicked.connect(self.remove_feed)
        self.btn_refresh = QPushButton("🔄 Fetch Latest")
        self.btn_refresh.setStyleSheet("background-color: #007acc; color: white; font-weight: bold;")
        self.btn_refresh.clicked.connect(self.refresh_all_feeds)

        toolbar.addWidget(self.btn_add)
        toolbar.addWidget(self.btn_del)
        toolbar.addStretch()
        toolbar.addWidget(self.btn_refresh)
        layout.addLayout(toolbar)

        # 分割视窗
        splitter = QSplitter(Qt.Horizontal)

        # 左侧：订阅源列表
        self.feed_list = QListWidget()
        self.feed_list.currentRowChanged.connect(self._on_feed_selected)
        self.feed_list.setStyleSheet("background-color: #1e1e1e; color: #e0e0e0; border: 1px solid #333;")
        splitter.addWidget(self.feed_list)

        # 右侧：文章渲染面板
        self.article_view = QTextBrowser()
        self.article_view.setOpenExternalLinks(False)
        self.article_view.anchorClicked.connect(self._on_link_clicked)
        self.article_view.setStyleSheet("""
            QTextBrowser { background-color: #252526; color: #d4d4d4; padding: 15px; border: 1px solid #333; font-size: 14px; }
            a { color: #05B8CC; text-decoration: none; }
        """)
        splitter.addWidget(self.article_view)

        splitter.setSizes([250, 750])
        layout.addWidget(splitter)

        self._refresh_feed_ui()
        self._load_cache()

        # 绑定日志系统（你的 BaseTool 已经提供了 on_task_log）
        self.task_mgr.sig_log.connect(self.on_task_log)

        return self.widget

    def _load_config(self):
        if os.path.exists(self.feeds_file):
            try:
                with open(self.feeds_file, 'r', encoding='utf-8') as f:
                    self.feeds = json.load(f)
            except:
                pass

    def _save_config(self):
        with open(self.feeds_file, 'w', encoding='utf-8') as f:
            json.dump(self.feeds, f, indent=4)

    def _load_cache(self):
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    self.article_cache = json.load(f)
            except:
                pass

    def _refresh_feed_ui(self):
        self.feed_list.clear()
        for feed in self.feeds:
            item = QListWidgetItem(f"📰 {feed['name']}")
            item.setData(Qt.UserRole, feed['url'])
            self.feed_list.addItem(item)

    def add_feed(self):
        url, ok = QInputDialog.getText(self.widget, "New RSS Feed", "Enter RSS URL:")
        if ok and url.strip():
            # 简单校验
            self.feeds.append({"name": "New Subscription", "url": url.strip()})
            self._save_config()
            self._refresh_feed_ui()
            ToastManager().show("Subscription added. Please click 'Fetch Latest'.", "success")

    def remove_feed(self):
        row = self.feed_list.currentRow()
        if row >= 0:
            del self.feeds[row]
            self._save_config()
            self._refresh_feed_ui()
            self.article_view.clear()

    def refresh_all_feeds(self):
        if not self.feeds: return

        self.pd = ProgressDialog(self.widget, "Fetching RSS", "Connecting to servers...",
                                 telemetry_config={"net": True})
        self.pd.show()

        self.task_mgr.sig_progress.connect(self.pd.update_progress)
        self.task_mgr.sig_state_changed.connect(self._on_fetch_done)
        self.pd.sig_canceled.connect(self.task_mgr.cancel_task)

        # 发射后台任务
        self.log("启动后台 RSS 拉取任务...")
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
            self.pd.show_success_state("Complete", "Academic feeds updated.")
            self._load_cache()  # 重新从磁盘加载最新数据
            self._on_feed_selected(self.feed_list.currentRow())  # 刷新当前视图
        else:
            self.pd.close_safe()
            ToastManager().show(f"Fetch failed: {msg}", "error")

    def _on_feed_selected(self, row):
        if row < 0: return
        url = self.feeds[row]['url']
        name = self.feeds[row]['name']

        articles = self.article_cache.get(url, [])
        if not articles:
            self.article_view.setHtml(
                f"<h3>{name}</h3><p style='color:#888;'>No data available. Click 'Fetch Latest'.</p>")
            return

        html = f"<h2>📰 {name}</h2><hr style='border: 1px solid #444;'>"
        for idx, art in enumerate(articles):
            title = art.get('title', 'Unknown Title')
            link = art.get('link', '#')
            date = art.get('pub_date', '')
            summary = art.get('summary', '')[:500] + "..."  # 截断过长的摘要

            # 使用 base64 或直接的 a href
            html += f"""
            <div style="margin-bottom: 25px;">
                <a href="{link}" style="font-size: 16px; font-weight: bold;">{title}</a>
                <div style="color: #888; font-size: 12px; margin-top: 4px; margin-bottom: 8px;">🕒 {date}</div>
                <div style="color: #bbb; line-height: 1.5;">{summary}</div>
            </div>
            """
        self.article_view.setHtml(html)

    def _on_link_clicked(self, url: QUrl):
        """用系统浏览器打开文献链接"""
        QDesktopServices.openUrl(url)