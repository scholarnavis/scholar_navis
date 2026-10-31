"""Settings UI mixin: 已关联的论文 PDF（DOI ↔ 本地文件）。

只保留一件事：查看与清理"交过一次、以后自动复用"的 PDF 关联表（口径见
:mod:`src.core.fulltext_store`）。目录自动扫描已下线——取全文时由用户在窗口里
自己选文件，程序不再猜哪份 PDF 是哪篇论文。

列表强制单行显示：文件名可以很长，跨行会让每条记录占两三行，一屏看不了几篇。
超长部分按中间省略（保留 DOI 前缀与文件扩展名），完整内容在悬浮提示里。
"""

import logging
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QGroupBox, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton,
                               QVBoxLayout)

from src.core.i18n import tr
from src.core.platform_env import open_with_system
from src.core.theme_manager import ThemeManager
from src.ui.components.toast import ToastManager

logger = logging.getLogger("Settings.FulltextSection")

#: 列表最小高度（像素）：约四行，再多就滚动。
_LIST_MIN_HEIGHT = 132


class FulltextSectionMixin:
    """已关联 PDF 列表：只读展示 + 打开 + 清理。"""

    def init_fulltext_section(self):
        self.group_fulltext = QGroupBox(tr("Full-Text Retrieval"))
        self.group_fulltext.setObjectName("group_fulltext")
        layout = QVBoxLayout(self.group_fulltext)
        layout.setSpacing(8)

        self.lbl_fulltext_hint = QLabel(tr(
            "A paper you hand over once is remembered by DOI and reused "
            "automatically, so the app never asks for the same file twice. When the "
            "answer needs a body that is not available yet, a system notification "
            "asks you to pick the PDF yourself — nothing is scanned behind your back."))
        self.lbl_fulltext_hint.setWordWrap(True)
        layout.addWidget(self.lbl_fulltext_hint)

        self.lbl_fulltext_links = QLabel()
        self.lbl_fulltext_links.setWordWrap(True)
        layout.addWidget(self.lbl_fulltext_links)

        self.list_fulltext_links = QListWidget()
        self.list_fulltext_links.setObjectName("list_fulltext_links")
        self.list_fulltext_links.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list_fulltext_links.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection)
        # 单行 + 中间省略：强制不换行（换行会让长文件名吃掉整屏）
        self.list_fulltext_links.setWordWrap(False)
        self.list_fulltext_links.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.list_fulltext_links.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_fulltext_links.setUniformItemSizes(True)
        self.list_fulltext_links.setMinimumHeight(_LIST_MIN_HEIGHT)
        self.list_fulltext_links.itemDoubleClicked.connect(
            self._on_fulltext_link_double_clicked)
        layout.addWidget(self.list_fulltext_links)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_open_fulltext_link = QPushButton(tr("Open File"))
        self.btn_open_fulltext_link.clicked.connect(self._on_open_fulltext_link)
        row.addWidget(self.btn_open_fulltext_link)

        self.btn_del_fulltext_link = QPushButton(tr("Remove Selected Link"))
        self.btn_del_fulltext_link.clicked.connect(self._on_remove_fulltext_link)
        row.addWidget(self.btn_del_fulltext_link)

        self.btn_clear_fulltext_links = QPushButton(tr("Clear All Links"))
        self.btn_clear_fulltext_links.clicked.connect(self._on_clear_fulltext_links)
        row.addWidget(self.btn_clear_fulltext_links)
        row.addStretch()
        layout.addLayout(row)

        self._refresh_fulltext_links()
        self.layout.addWidget(self.group_fulltext)

    # ---------- 展示 ----------

    def _refresh_fulltext_links(self):
        """刷新"DOI ↔ 本地 PDF"列表；失效条目由 fulltext_store 顺手清掉。"""
        from src.core import fulltext_store

        links = {}
        try:
            links = fulltext_store.all_links()
        except Exception as e:      # 关联表坏了不该让设置页打不开
            logger.warning("Cannot read the full-text link table: %s", e)

        self.list_fulltext_links.clear()
        for doi, entry in sorted(links.items()):
            entry = entry or {}
            path = str(entry.get("path") or "")
            # 优先显示论文标题（人读得懂），没有标题才退回文件名。
            label = str(entry.get("title") or "").strip() or os.path.basename(path) or path
            # 单行：DOI 在前便于按文献定位，标题在后便于认内容。
            item = QListWidgetItem(f"{doi}  ·  {label}" if label else doi)
            item.setData(Qt.ItemDataRole.UserRole, doi)
            item.setToolTip(f"{label}\nDOI: {doi}\n{path}".strip())
            self.list_fulltext_links.addItem(item)

        if links:
            self.lbl_fulltext_links.setText(
                tr("Linked PDFs ({n})").format(n=len(links)))
        else:
            self.lbl_fulltext_links.setText(tr(
                "No linked PDF yet. The first paper you hand over shows up here."))

        has_rows = self.list_fulltext_links.count() > 0
        self.btn_del_fulltext_link.setEnabled(has_rows)
        self.btn_clear_fulltext_links.setEnabled(has_rows)
        self.btn_open_fulltext_link.setEnabled(has_rows)

    # ---------- 打开 ----------

    def _on_fulltext_link_double_clicked(self, item):
        self._open_link(str(item.data(Qt.ItemDataRole.UserRole) or ""))

    def _on_open_fulltext_link(self):
        items = self.list_fulltext_links.selectedItems()
        if not items:
            ToastManager().show(tr("Select a link to open first."), "info")
            return
        self._open_link(str(items[0].data(Qt.ItemDataRole.UserRole) or ""))

    def _open_link(self, doi: str):
        """按 DOI 取关联文件并用系统查看器打开；文件不在了就顺手解关联。

        直接读关联表取路径，不走 :func:`fulltext_store.resolve`：后者会做内容校验
        （哈希 + 抽页），那是"要拿它当正文用"的场景才付的代价，点开看一眼不需要。
        """
        from src.core import fulltext_store

        path = ""
        try:
            path = str((fulltext_store.all_links().get(doi) or {}).get("path") or "")
        except Exception as e:
            logger.warning("Cannot resolve the full-text link for %s: %s", doi, e)
        if not path or not os.path.isfile(path):
            ToastManager().show(tr("This PDF is no longer there."), "warning")
            if doi:
                fulltext_store.unlink(doi)
            self._refresh_fulltext_links()
            return
        if not open_with_system(path):
            ToastManager().show(tr("Could not open the file with the system viewer."),
                                "warning")

    # ---------- 清理 ----------

    def _on_remove_fulltext_link(self):
        from src.core import fulltext_store

        selected = self.list_fulltext_links.selectedItems()
        if not selected:
            ToastManager().show(tr("Select the links to remove first."), "info")
            return
        removed = sum(1 for item in selected
                      if fulltext_store.unlink(item.data(Qt.ItemDataRole.UserRole)))
        logger.info("Full-text links removed: %d", removed)
        self._refresh_fulltext_links()
        ToastManager().show(tr("{n} link(s) removed.").format(n=removed), "success")

    def _on_clear_fulltext_links(self):
        from src.core import fulltext_store

        count = fulltext_store.clear()
        logger.info("Full-text links cleared: %d", count)
        self._refresh_fulltext_links()
        ToastManager().show(tr("{n} link(s) removed.").format(n=count), "success")

    # ---------- 主题 ----------

    def _apply_fulltext_theme(self):
        """列表与说明文字的配色（由设置页主题刷新统一调用）。"""
        if not hasattr(self, "list_fulltext_links"):
            return
        tm = ThemeManager()
        self.list_fulltext_links.setStyleSheet(
            f"QListWidget {{ color: {tm.color('text_main')};"
            f" background: {tm.color('bg_main')};"
            f" border: 1px solid {tm.color('border')};"
            f" border-radius: 6px; padding: 4px; }}"
            f"QListWidget::item {{ padding: 6px 8px; }}")
        for lbl_name in ("lbl_fulltext_hint", "lbl_fulltext_links"):
            if hasattr(self, lbl_name):
                getattr(self, lbl_name).setStyleSheet(
                    f"color: {tm.color('text_muted')}; font-size: 11px;")
