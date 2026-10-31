"""取全文窗口：付费墙论文请用户手动选择 PDF 并交回（任务线程同轮等待）。

为什么不再监听目录
------------------
旧实现会在"收件目录 + 用户配置的额外目录"里定时扫描 PDF，并按 DOI / 标题词自动
认领。目录一旦混进同领域的旧论文，认错文件的代价是模型读了另一篇、结论整段错位，
而用户完全看不出问题出在"自动认领"。现在只保留一件事：**用户自己选文件**，交回的
是哪份就是哪份（见 :mod:`src.core.fulltext_bridge` 的同轮会合通道）。

窗口不抢焦点
------------
触发方式由上层决定：本窗口由调用方以非激活方式 ``show()``，同时发一条系统通知
（见 :mod:`src.ui.components.system_notify`）。用户点击通知时才把它拉到前台。
"""

import logging
import os

from PySide6.QtCore import Qt, QStandardPaths, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QFrame, QLabel, QVBoxLayout

from src.core.i18n import tr
from src.core.theme_manager import ThemeManager, strong_weight_css
from src.ui.components.dialogs.base import BaseDialog

logger = logging.getLogger(__name__)

__all__ = ["FullTextRequestDialog"]


def _downloads_dir() -> str:
    """系统下载目录：文件选择框的起始位置（取不到时回退用户主目录）。"""
    return (QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.DownloadLocation)
        or os.path.expanduser("~"))


def _looks_like_pdf(path: str) -> bool:
    """粗校验：PDF 魔数出现在前 1KB 内（规范允许前面有少量垃圾）。"""
    try:
        with open(path, "rb") as f:
            return b"%PDF" in f.read(1024)
    except OSError:
        return False


def _has_text_layer(path: str) -> bool:
    """这份 PDF 是否带可抽取的文本层（纯扫描件没有）。

    窗口只交付"能读"的文件：整篇是扫描图时，任务线程解析为空、这一轮照样白等，
    而窗口在交付瞬间就关闭，用户连重选的入口都没有。只读前几页，几十毫秒。
    解析器不可用时返回 ``True``——判断不了就不要拦。
    """
    try:
        from src.core.pdf_text import probe_pdf

        _doi, head = probe_pdf(path)
        return bool(str(head or "").strip())
    except Exception as e:
        logger.debug("Text-layer probe unavailable for %s: %s", path, e)
        return True


class _DropZone(QFrame):
    """点一下就能选文件的虚线框（整窗拖拽见 ``FullTextRequestDialog.dropEvent``）。"""

    def __init__(self, on_click, parent=None):
        super().__init__(parent)
        self._on_click = on_click

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._on_click()
        super().mousePressEvent(event)


class FullTextRequestDialog(BaseDialog):
    """一次取一份全文：用户选择或拖入 PDF，交回后唤醒挂起的本轮生成。"""

    def __init__(self, parent=None, data=None, conversation_id=""):
        super().__init__(parent, title=tr("Full text needed"), width=560)
        self._data = dict(data or {})
        self._conversation_id = str(conversation_id or "")
        #: 已交付（或已确认没有等待方）后不再重复通知等待方。
        self._settled = False
        self._build_ui()
        self.setAcceptDrops(True)
        self._apply_theme()

    # ------------------------------------------------------------------ 构建

    def _build_ui(self):
        title = str(self._data.get("title") or "").strip()
        doi = str(self._data.get("doi") or "").strip()
        url = str(self._data.get("url") or "").strip()

        # 不要说"付费墙"：自动下载失败可能是出版社 WAF 拦截，与是否付费无关。
        # 具体原因由下方 note/reason 另行说明。
        head = QLabel(tr(
            "The answer needs the full text of this paper. Download the PDF in "
            "your browser, then hand it over here."))
        head.setWordWrap(True)
        head.setObjectName("FulltextMeta")
        self.content_layout.addWidget(head)

        paper = QLabel(title or doi or url or tr("(no title provided)"))
        paper.setWordWrap(True)
        paper.setObjectName("FulltextPaper")
        paper.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.content_layout.addWidget(paper)

        meta_text = " · ".join(p for p in (f"DOI: {doi}" if doi else "", url) if p)
        self.lbl_meta = QLabel(meta_text or tr("(no DOI or link provided)"))
        self.lbl_meta.setWordWrap(True)
        self.lbl_meta.setObjectName("FulltextMeta")
        self.lbl_meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.content_layout.addWidget(self.lbl_meta)

        # reason = 模型自己写的"为什么需要它"；note = 自动升级带来的"为什么没拿到"
        # （已是完整句子，不再套 "Needed for:"）。两者只显示一条，优先 reason。
        reason = str(self._data.get("reason") or "").strip()
        note = str(self._data.get("note") or "").strip()
        detail = (tr("Needed for: {reason}").format(reason=reason) if reason
                  else note)
        if detail:
            self.lbl_reason = QLabel(detail)
            self.lbl_reason.setWordWrap(True)
            self.lbl_reason.setObjectName("FulltextMeta")
            self.lbl_reason.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.content_layout.addWidget(self.lbl_reason)

        self.drop_zone = _DropZone(self._pick_file)
        self.drop_zone.setObjectName("FulltextDropZone")
        self.drop_zone.setCursor(Qt.PointingHandCursor)
        self.drop_zone.setMinimumHeight(84)
        zone_layout = QVBoxLayout(self.drop_zone)
        zone_layout.setContentsMargins(12, 12, 12, 12)
        self.lbl_drop = QLabel(tr("Drop the downloaded PDF here\n"
                                  "or pick it from disk"))
        self.lbl_drop.setWordWrap(True)
        self.lbl_drop.setAlignment(Qt.AlignCenter)
        self.lbl_drop.setObjectName("FulltextDropHint")
        zone_layout.addWidget(self.lbl_drop)
        self.content_layout.addWidget(self.drop_zone)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("FulltextStatus")
        self.status_label.setVisible(False)
        self.content_layout.addWidget(self.status_label)

        btn_cancel = self.add_button(tr("Cancel"), self._on_cancel)
        btn_cancel.setFixedWidth(90)
        if url or doi:
            btn_open = self.add_button(tr("Open Page"), self._open_page)
            btn_open.setFixedWidth(100)
        btn_pick = self.add_button(tr("Choose PDF"), self._pick_file, is_primary=True)
        btn_pick.setFixedWidth(112)

    def _apply_theme(self):
        super()._apply_theme()
        tm = ThemeManager()
        # 追加在基类样式之后：这些 objectName 只有本窗口用。
        self.setStyleSheet(self.styleSheet() + f"""
            QFrame#FulltextDropZone {{
                background-color: {tm.color('bg_input')};
                border: 1px dashed {tm.color('border')};
                border-radius: 8px;
            }}
            QFrame#FulltextDropZone:hover {{ border: 1px dashed {tm.color('accent')}; }}
            QLabel#FulltextPaper {{
                color: {tm.color('text_main')}; font-size: 14px;
                font-weight: {strong_weight_css()}; border: none; background: transparent;
            }}
            QLabel#FulltextMeta, QLabel#FulltextDropHint {{
                color: {tm.color('text_muted')}; font-size: 12px;
                border: none; background: transparent;
            }}
        """)

    # ------------------------------------------------------------------ 交付

    def _set_status(self, text: str, error: bool = False):
        color = ThemeManager().color('warning' if error else 'text_muted')
        self.status_label.setStyleSheet(
            f"color: {color}; font-size: 12px; border: none; background: transparent;")
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))

    def _deliver(self, path: str):
        """把 PDF 交回挂起的任务线程；交付成功即关窗。"""
        if self._settled:
            return
        path = os.path.abspath(str(path or ""))
        if not path.lower().endswith(".pdf") or not os.path.isfile(path):
            self._set_status(
                tr("That is not a readable PDF file: {name}").format(
                    name=os.path.basename(path or "-")), error=True)
            return
        # 0 字节 / 还在写入的文件先挡住：交给任务只会换来一次"PDF 无法解析"，
        # 而那时窗口已经关了，用户连重选的入口都没有。
        if not _looks_like_pdf(path):
            self._set_status(
                tr("This file is not a complete PDF yet: {name} "
                   "(the download may still be running)").format(
                    name=os.path.basename(path)), error=True)
            return
        # 纯扫描件没有文字层，交回只会让这一轮白等：当场说明并留在窗口里让用户重选。
        if not _has_text_layer(path):
            logger.info("Provided PDF has no text layer: %s", os.path.basename(path))
            self._set_status(
                tr("This PDF has no text layer (it looks like a scan), so its text "
                   "cannot be read. Please download the publisher's text PDF instead."),
                error=True)
            return

        from src.core import fulltext_bridge

        if not fulltext_bridge.signal(self._conversation_id, True, path):
            # 等待点已经不在了（多为用户中途终止了本次生成）：如实说明，别把文件默默吞掉。
            logger.info("Full-text PDF not accepted (no waiter) for conversation %s.",
                        self._conversation_id)
            self._set_status(
                tr("This run is no longer waiting for the full text (it may have "
                   "been stopped or timed out). Send the PDF as an attachment instead."),
                error=True)
            self._settled = True
            return

        logger.info("Full-text PDF handed over: %s (conversation=%s).",
                    os.path.basename(path), self._conversation_id)
        self._settled = True
        self.accept()

    def _cancel_pending(self):
        """告知等待方"用户没给文件"，避免任务线程白等到超时。"""
        if self._settled or not self._conversation_id:
            return
        self._settled = True
        from src.core import fulltext_bridge

        fulltext_bridge.signal(self._conversation_id, False, "",
                               "the user closed the picker without a file")

    def _on_cancel(self):
        self._cancel_pending()
        self.reject()

    def closeEvent(self, event):
        self._cancel_pending()
        super().closeEvent(event)

    # ------------------------------------------------------------------ 取文件

    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Choose the paper's PDF"), _downloads_dir(), "PDF (*.pdf)")
        if path:
            self._deliver(path)

    def _open_page(self):
        url = str(self._data.get("url") or "").strip()
        doi = str(self._data.get("doi") or "").strip()
        target = url or (f"https://doi.org/{doi}" if doi else "")
        if not target:
            return
        logger.info("Opening the paper page for the full-text request: %s", target[:120])
        QDesktopServices.openUrl(QUrl(target))

    # ------------------------------------------------------------------ 拖拽

    @staticmethod
    def _first_pdf(event) -> str:
        mime = event.mimeData() if event is not None else None
        if mime is None or not mime.hasUrls():
            return ""
        for url in mime.urls():
            local = url.toLocalFile()
            if local and local.lower().endswith(".pdf"):
                return local
        return ""

    def dragEnterEvent(self, event):
        if self._first_pdf(event):
            event.acceptProposedAction()

    def dropEvent(self, event):
        path = self._first_pdf(event)
        if not path:
            return
        event.acceptProposedAction()
        self._deliver(path)
