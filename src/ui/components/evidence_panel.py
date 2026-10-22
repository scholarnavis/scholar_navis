"""会话证据面板：查看本对话已入库的文献 / 私有 PDF，并可补全文或删除私有证据。"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout,
                               QHeaderView, QLabel, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from src.core.i18n import tr
from src.core.references import normalize_bibliographic_text
from src.core.theme_manager import ThemeManager, strong_weight_css

logger = logging.getLogger("UI.EvidencePanel")


#: 检索源给的 type 五花八门（article / journal-article / book-chapter /
#: proceedings-article / posted-content / dataset …），这里统一折叠成用户能一眼
#: 分清的几类。未知值原样显示，避免"其他"吃掉真实信息。
_TYPE_LABELS = {
    "article": "Journal article", "journal-article": "Journal article",
    "review": "Review",
    "book": "Book", "book-chapter": "Book chapter", "monograph": "Monograph",
    "proceedings-article": "Conference paper", "conference-paper": "Conference paper",
    "preprint": "Preprint", "posted-content": "Preprint",
    "dataset": "Dataset", "thesis": "Thesis", "report": "Report",
    "patent": "Patent", "web": "Web page",
}


def _type_label(kind) -> str:
    raw = str(kind or "").strip().lower()
    if not raw:
        return "—"
    return tr(_TYPE_LABELS.get(raw, raw[:24]))


class EvidencePanel(QDialog):
    """非模态证据面板；每次打开按当前 conversation_id 重新读取。"""

    sig_get_fulltext = Signal(str, str, str)
    sig_get_missing_fulltext = Signal()
    sig_deleted = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Conversation Evidence"))
        self.setModal(False)
        self.resize(980, 560)
        self._conversation_id = ""
        self._items = []
        self._build_ui()
        self._apply_theme()
        ThemeManager().theme_changed.connect(self._apply_theme)

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        self.lbl_summary = QLabel("")
        self.lbl_summary.setWordWrap(True)
        outer.addWidget(self.lbl_summary)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            tr("Title"), tr("Type"), tr("Year"), tr("Journal"), tr("Source"),
            tr("Status")])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3, 4, 5):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table.itemDoubleClicked.connect(self._open_item)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        outer.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        row.addWidget(self.lbl_status, 1)
        self.btn_refresh = QPushButton(tr("Refresh"))
        self.btn_get = QPushButton(tr("Get full text"))
        self.btn_next_missing = QPushButton(tr("Get next missing"))
        self.btn_delete = QPushButton(tr("Delete private"))
        self.btn_close = QPushButton(tr("Close"))
        for btn in (self.btn_refresh, self.btn_get, self.btn_next_missing,
                    self.btn_delete, self.btn_close):
            btn.setCursor(Qt.PointingHandCursor)
        self.btn_refresh.clicked.connect(lambda: self.refresh())
        self.btn_get.clicked.connect(lambda: self._get_fulltext())
        self.btn_next_missing.clicked.connect(
            lambda: self.sig_get_missing_fulltext.emit())
        self.btn_delete.clicked.connect(lambda: self._delete_private())
        self.btn_close.clicked.connect(lambda: self.close())
        row.addWidget(self.btn_refresh)
        row.addWidget(self.btn_get)
        row.addWidget(self.btn_next_missing)
        row.addWidget(self.btn_delete)
        row.addWidget(self.btn_close)
        outer.addLayout(row)

    def refresh(self, conversation_id: str | None = None):
        if conversation_id is not None:
            self._conversation_id = str(conversation_id)
        from src.core.auto_evidence import (list_conversation_evidence,
                                            purge_orphan_evidence)
        # 打开面板时先扫一遍"对话已不存在却还挂着"的证据：旧版本清空对话、导入失败、
        # 进程被杀都可能留下这种孤儿，用户会以为"明明清空了，证据里还有东西"。
        try:
            purge_orphan_evidence()
        except Exception as e:
            logger.debug("Evidence sweep on open skipped: %s", e)
        self.lbl_status.setText(tr("Loading evidence..."))
        self._items = list_conversation_evidence(self._conversation_id)
        self.table.setRowCount(len(self._items))
        for row, item in enumerate(self._items):
            values = [
                normalize_bibliographic_text(item.get("title")) or "Untitled",
                _type_label(item.get("type")),
                item.get("year") or "",
                normalize_bibliographic_text(item.get("journal")),
                ", ".join(item.get("origins") or []),
                tr("Full text") if item.get("has_fulltext") else tr("Abstract"),
            ]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(str(item.get("doi") or item.get("url") or ""))
                self.table.setItem(row, col, cell)
        private_count = sum(1 for it in self._items if it.get("visibility") == "private")
        fulltext_count = sum(1 for it in self._items if it.get("has_fulltext"))
        abstract_count = max(0, len(self._items) - fulltext_count)
        self.lbl_summary.setText(
            tr("This conversation has {n} evidence item(s), {p} private, "
               "{f} full text, {a} abstract-only. Select an abstract-only row "
               "and click Get full text; log in once and cookies are reused.")
            .format(n=len(self._items), p=private_count,
                    f=fulltext_count, a=abstract_count))
        self.lbl_status.setText("")
        self._sync_buttons()

    def _selected_item(self):
        row = self.table.currentRow()
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def _sync_buttons(self):
        item = self._selected_item()
        can_delete = bool(item and item.get("visibility") == "private")
        can_get = bool(item and not item.get("has_fulltext")
                       and (item.get("doi") or item.get("url")))
        self.btn_delete.setEnabled(can_delete)
        self.btn_get.setEnabled(can_get)
        has_missing = any(
            not it.get("has_fulltext") and (it.get("doi") or it.get("url"))
            for it in self._items)
        self.btn_next_missing.setEnabled(has_missing)

    def _open_item(self, _cell):
        item = self._selected_item()
        if not item:
            return
        doi = str(item.get("doi") or "")
        url = str(item.get("url") or "")
        path = str(item.get("path") or "")
        if url:
            QDesktopServices.openUrl(QUrl(url))
        elif doi:
            QDesktopServices.openUrl(QUrl(f"https://doi.org/{doi}"))
        elif path and os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _get_fulltext(self):
        item = self._selected_item()
        if not item:
            return
        self.sig_get_fulltext.emit(
            str(item.get("doi") or ""), str(item.get("title") or ""),
            str(item.get("url") or ""))

    def _delete_private(self):
        item = self._selected_item()
        if not item or item.get("visibility") != "private":
            return
        from src.core.auto_evidence import delete_private_evidence
        if delete_private_evidence(self._conversation_id, item.get("paper_id", "")):
            self.lbl_status.setText(tr("Private evidence deleted."))
            self.refresh()
            self.sig_deleted.emit()
        else:
            self.lbl_status.setText(tr("Delete failed."))

    def _apply_theme(self):
        tm = ThemeManager()
        self.setStyleSheet(
            f"QDialog {{ background-color: {tm.color('bg_main')}; color: {tm.color('text_main')}; }}"
            f"QLabel {{ color: {tm.color('text_main')}; }}"
            f"QTableWidget {{ background-color: {tm.color('bg_card')}; color: {tm.color('text_main')}; "
            f"gridline-color: {tm.color('border')}; border: 1px solid {tm.color('border')}; }}"
            f"QHeaderView::section {{ background-color: {tm.color('bg_input')}; "
            f"color: {tm.color('text_main')}; border: none; padding: 4px; }}"
            f"QPushButton {{ background-color: {tm.color('btn_bg')}; color: {tm.color('text_main')}; "
            f"border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 5px 12px; }}"
            f"QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}"
            f"QPushButton:disabled {{ color: {tm.color('text_muted')}; }}")
        self.lbl_summary.setStyleSheet(
            f"font-weight: {strong_weight_css()}; color: {tm.color('text_main')};")
        self.lbl_status.setStyleSheet(f"color: {tm.color('text_muted')};")


__all__ = ["EvidencePanel"]
