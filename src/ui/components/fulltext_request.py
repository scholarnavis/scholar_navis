"""取全文窗口：付费墙论文请用户下载 PDF 并交回（任务线程同轮等待）。

通道见 :mod:`src.core.fulltext_bridge`：任务线程在 ``request_fulltext`` 工具里挂起，
本窗口拿到文件后唤醒它，本轮从原处继续——已检索到的来源、计划与引用注册表都不丢。
"""
import logging
import os
import re
import time

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QFrame, QLabel, QPushButton, QVBoxLayout, QWidget

from src.core.fulltext_dirs import (dir_identity, downloads_dir, inbox_dir,
                                    watch_dirs)
from src.core.theme_manager import ThemeManager, strong_weight_css
from src.ui.components.dialogs.base import BaseDialog

logger = logging.getLogger(__name__)

__all__ = ["FullTextRequestDialog"]

#: 起始扫描间隔（毫秒）：文件在下载完成的瞬间出现，扫太密只是白烧 CPU。
_SCAN_INTERVAL_MS = 1500
#: 空闲退避上限（毫秒）：开窗后一直没动静就逐步降频，别让它长时间空跑。
_SCAN_INTERVAL_MAX_MS = 6000
#: 间隔至少是本轮扫描耗时的这么多倍：目录越庞大扫得越稀疏，避免持续占满主线程。
_SCAN_COST_FACTOR = 8
#: 目录可用性复查间隔（秒）：移动盘/网络盘会中途消失，但每轮都 stat 不划算。
_DIR_RECHECK_SEC = 15.0
#: 单轮扫描超过这个耗时（毫秒）就在日志里点名：通常是设置里加了超大目录。
_SCAN_SLOW_MS = 300.0
#: 候选列表最多显示几个，避免误匹配时把窗口撑爆。
_MAX_CANDIDATES = 3


def _norm(text: str) -> str:
    """归一化用于文件名匹配：只留字母数字，消掉分隔符与大小写差异。"""
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def _matches_request(filename: str, data: dict) -> bool:
    """文件名是否指向请求的那篇：DOI 片段优先，其次标题词命中过半。

    下载器生成的文件名千奇百怪（网页标题、DOI 后缀、加序号……），所以只做宽松命中；
    命中不了的不会丢，进候选列表让用户点一下。
    """
    stem = _norm(os.path.splitext(os.path.basename(filename))[0])
    if not stem:
        return False
    # DOI 常被切掉前缀（10.1038/）后当文件名，因此按片段匹配而非整串包含。
    parts = [p for p in re.split(r"[/:]", str(data.get("doi") or "")) if len(p) >= 6]
    if any(_norm(p) in stem for p in parts):
        return True
    tokens = [t for t in re.findall(r"[A-Za-z]{4,}", str(data.get("title") or "").lower())]
    return bool(tokens) and sum(1 for t in tokens if t in stem) * 2 >= len(tokens)


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
    """一次取全文一个窗口：选择/拖拽为主，"监听目录里出现新 PDF"兜底。

    监听目录 = 内置收件目录 + 用户在"设置 → 获取全文"里配置的额外目录，来源见
    :mod:`src.core.fulltext_dirs`（本窗口不自己拼路径）。
    """

    def __init__(self, parent=None, data=None, conversation_id=""):
        super().__init__(parent, title="需要这篇论文的全文", width=560)
        self._data = dict(data or {})
        self._conversation_id = str(conversation_id or "")
        #: 已交付（或已确认没有等待方）后不再发信号，避免关窗时重复通知等待方。
        self._settled = False
        self._candidates = []
        self._inbox = inbox_dir()
        self._inbox_key = dir_identity(self._inbox)
        self._ensure_inbox()
        #: 本次开窗要扫描的目录（已展开 ~、按目录身份去重、按上限截断）。
        self._dirs = watch_dirs()
        #: 目录 -> (上次复查时刻, 是否可用)：目录可能中途消失，但不必每轮都去看。
        self._dir_state = {}
        self._interval = _SCAN_INTERVAL_MS
        #: 开窗时已存在的 PDF 全部忽略，只认之后新出现的文件。
        self._seen = set()
        self._build_ui()
        self._prime_seen()
        self.setAcceptDrops(True)

        self._scan_timer = QTimer(self)
        self._scan_timer.timeout.connect(self._scan_watch_dirs)
        self._scan_timer.start(self._interval)
        self._apply_theme()

    # ------------------------------------------------------------------ 构建

    def _ensure_inbox(self):
        """建好收件目录；建不出来只记日志，其它监听目录照常工作。"""
        try:
            os.makedirs(self._inbox, exist_ok=True)
        except OSError as e:
            logger.warning("Cannot create the full-text inbox %s: %s", self._inbox, e)

    def _build_ui(self):
        title = str(self._data.get("title") or "").strip()
        doi = str(self._data.get("doi") or "").strip()
        url = str(self._data.get("url") or "").strip()

        head = QLabel("这篇论文的正文在付费墙后面，AI 需要它来支撑结论。"
                      "请在浏览器里下载 PDF，然后交给这个窗口。")
        head.setWordWrap(True)
        head.setObjectName("FulltextMeta")
        self.content_layout.addWidget(head)

        paper = QLabel(title or doi or url or "(未提供标题)")
        paper.setWordWrap(True)
        paper.setObjectName("FulltextPaper")
        paper.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.content_layout.addWidget(paper)

        meta_text = " · ".join(p for p in (f"DOI: {doi}" if doi else "", url) if p)
        self.lbl_meta = QLabel(meta_text or "（未提供 DOI / 链接）")
        self.lbl_meta.setWordWrap(True)
        self.lbl_meta.setObjectName("FulltextMeta")
        self.lbl_meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.content_layout.addWidget(self.lbl_meta)

        reason = str(self._data.get("reason") or "").strip()
        if reason:
            self.lbl_reason = QLabel(f"需要它的原因：{reason}")
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
        self.lbl_drop = QLabel("把下载好的 PDF 拖到这里\n或点下面的「选择 PDF」")
        self.lbl_drop.setWordWrap(True)
        self.lbl_drop.setAlignment(Qt.AlignCenter)
        self.lbl_drop.setObjectName("FulltextDropHint")
        zone_layout.addWidget(self.lbl_drop)
        self.content_layout.addWidget(self.drop_zone)

        if self._dirs:
            self.lbl_inbox = QLabel(
                "也可以把 PDF 放进下面任一目录，窗口会自动认领：\n"
                + "\n".join(self._dirs)
                + "\n（监听目录可在「设置 → 获取全文」里增删）")
            self.lbl_inbox.setWordWrap(True)
            self.lbl_inbox.setObjectName("FulltextDropHint")
            self.lbl_inbox.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.content_layout.addWidget(self.lbl_inbox)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("FulltextStatus")
        self.status_label.setVisible(False)
        self.content_layout.addWidget(self.status_label)

        self.candidates_box = QWidget()
        self.candidates_box.setObjectName("FulltextCandidates")
        self.candidates_layout = QVBoxLayout(self.candidates_box)
        self.candidates_layout.setContentsMargins(0, 0, 0, 0)
        self.candidates_layout.setSpacing(6)
        self.candidates_box.setVisible(False)
        self.content_layout.addWidget(self.candidates_box)

        btn_cancel = self.add_button("取消", self._on_cancel)
        btn_cancel.setFixedWidth(90)
        if url or doi:
            btn_open = self.add_button("打开页面", self._open_page)
            btn_open.setFixedWidth(100)
        btn_pick = self.add_button("选择 PDF", self._pick_file, is_primary=True)
        btn_pick.setFixedWidth(104)

    def _apply_theme(self):
        super()._apply_theme()
        tm = ThemeManager()
        # 追加在基类样式之后：这些 objectName 只有本窗口用。
        self.setStyleSheet(self.styleSheet() + f"""
            QFrame#FulltextDropZone {{
                background-color: {tm.color('bg_input')};
                border: 1px dashed {tm.color('border')};
                border-radius: 6px;
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
            QWidget#FulltextCandidates QPushButton {{
                background-color: {tm.color('btn_hover')};
                color: {tm.color('text_main')};
                border: 1px solid {tm.color('border')};
                border-radius: 4px; padding: 6px 8px; text-align: left;
            }}
            QWidget#FulltextCandidates QPushButton:hover {{ border: 1px solid {tm.color('accent')}; }}
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
            self._set_status(f"不是可读取的 PDF 文件：{os.path.basename(path or '(空)')}", error=True)
            return

        from src.core import fulltext_bridge

        if not fulltext_bridge.signal(self._conversation_id, True, path):
            # 等待点已经不在了（多为用户中途终止了本次生成）：如实说明，别把文件默默吞掉。
            logger.info("Full-text PDF not accepted (no waiter) for conversation %s.",
                        self._conversation_id)
            self._set_status("本轮已不再等待全文（可能已被终止或已超时）：请把该 PDF 作为附件直接发送。",
                             error=True)
            self._settled = True
            return

        logger.info("Full-text PDF handed over: %s (conversation=%s).",
                    os.path.basename(path), self._conversation_id)
        self._settled = True
        self._stop_scanning()
        self.accept()

    def _stop_scanning(self):
        """交付/关窗后停掉扫描：模态窗口隐藏后定时器还活着就是白烧 CPU。"""
        timer = getattr(self, "_scan_timer", None)
        if timer is not None:
            timer.stop()

    def _cancel_pending(self):
        """告知等待方"用户没给文件"，避免任务线程白等到超时。"""
        self._stop_scanning()
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
            self, "选择论文 PDF", downloads_dir() or os.path.expanduser("~"), "PDF (*.pdf)")
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

    @staticmethod
    def _list_pdfs(directory: str):
        """目录里的 PDF 路径：只按名字过滤，不逐文件 stat。

        逐文件 stat 才是大目录上的真成本（几千个条目就是几千次系统调用），所以时间戳
        只在确实需要排序的新文件上取，见 :meth:`_newest_first`。
        """
        try:
            with os.scandir(directory) as entries:
                return [os.path.join(directory, entry.name) for entry in entries
                        if entry.name.lower().endswith(".pdf")]
        except OSError as e:
            logger.debug("Cannot list %s: %s", directory, e)
            return []

    def _dir_usable(self, directory: str) -> bool:
        """目录当前是否可扫；结果带缓存，外接盘/网络盘中途消失时不必每轮都去 stat。"""
        now = time.monotonic()
        checked, ok = self._dir_state.get(directory, (0.0, True))
        if checked and now - checked < _DIR_RECHECK_SEC:
            return ok
        try:
            ok = os.path.isdir(directory) and os.access(directory, os.R_OK)
        except OSError:
            ok = False
        if not ok:
            logger.debug("Full-text watch dir skipped (missing or unreadable): %s", directory)
        self._dir_state[directory] = (now, ok)
        return ok

    def _new_pdfs(self, directory: str):
        """目录里"开窗之后才出现"的 PDF；已知名字直接跳过，只对新名字多 stat 一次。"""
        fresh = []
        for path in self._list_pdfs(directory):
            if path in self._seen:
                continue
            if not os.path.isfile(path):  # 名字叫 xxx.pdf 的目录也算"新名字"，在这一步挡掉
                continue
            self._seen.add(path)
            fresh.append(path)
        return fresh

    @staticmethod
    def _newest_first(paths):
        """同一轮出现多个新文件时按修改时间倒序：刚下载的那份最可能是用户要的。"""
        if len(paths) < 2:
            return paths

        def stamp(path):
            try:
                return os.path.getmtime(path)
            except OSError:
                return 0.0

        return sorted(paths, key=stamp, reverse=True)

    def _prime_seen(self):
        for directory in self._dirs:
            self._seen.update(self._list_pdfs(directory))
        logger.debug("Full-text picker watching %d dir(s): %s (%d pre-existing PDF(s) ignored).",
                     len(self._dirs), self._dirs, len(self._seen))

    def _scan_watch_dirs(self):
        """按目录语义认领新出现的 PDF，并按本轮成本调整下次扫描间隔。

        内置收件目录表示"用户明确放进来了"，文件名对不上也列成候选；设置里的额外目录
        （下载目录、文献库等）是杂物堆，只有能确定是这一篇时才自动认领，否则安静忽略。
        每轮只做"列名 + 差集"，成本取决于目录条目数而非文件大小。
        """
        started = time.monotonic()
        hit = False
        for directory in self._dirs:
            if not self._dir_usable(directory):
                continue
            fresh = self._new_pdfs(directory)
            if not fresh:
                continue
            logger.debug("Full-text watch dir %s: %d new PDF(s).", directory, len(fresh))
            if self._consume(directory, self._newest_first(fresh)):
                hit = True
                break
        self._adapt_interval((time.monotonic() - started) * 1000.0, hit)

    def _consume(self, directory: str, fresh) -> bool:
        """处理一轮新文件；返回是否已交付（真=窗口已关，不用再扫）。"""
        in_inbox = dir_identity(directory) == self._inbox_key
        for path in fresh:
            if _matches_request(path, self._data):
                self._deliver(path)
                return True
            if in_inbox:
                self._add_candidate(path)
        return False

    def _adapt_interval(self, cost_ms: float, hit: bool):
        """按本轮成本与命中情况决定下次扫描间隔。

        没命中就翻倍退避（上限 :data:`_SCAN_INTERVAL_MAX_MS`），命中立刻回到起始间隔
        ——交付靠及时性，命中说明目录是活跃的。另设一条成本下限：间隔至少是本轮耗时的
        :data:`_SCAN_COST_FACTOR` 倍，几十万条目的目录不会把主线程占满。
        """
        base = _SCAN_INTERVAL_MS if hit else min(self._interval * 2, _SCAN_INTERVAL_MAX_MS)
        target = max(base, int(cost_ms * _SCAN_COST_FACTOR))
        if target != self._interval:
            logger.debug("Full-text scan interval %d ms -> %d ms (cost=%.1f ms, hit=%s).",
                         self._interval, target, cost_ms, hit)
            self._interval = target
            self._scan_timer.setInterval(target)
        if cost_ms >= _SCAN_SLOW_MS:
            logger.warning("Full-text watch scan took %.0f ms over %d dir(s); a very large "
                           "directory in Settings -> Full-Text Retrieval slows the picker "
                           "down.", cost_ms, len(self._dirs))

    def _add_candidate(self, path: str):
        if not path or path in self._candidates:
            return
        self._candidates.append(path)
        logger.info("Full-text candidate discovered in the inbox: %s", os.path.basename(path))
        btn = QPushButton(f"使用：{os.path.basename(path)}")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(path)
        btn.clicked.connect(lambda *_, p=path: self._deliver(p))
        self.candidates_layout.addWidget(btn)
        self.candidates_box.setVisible(True)
        if len(self._candidates) > _MAX_CANDIDATES:
            oldest = self.candidates_layout.itemAt(0).widget()
            if oldest is not None:
                self.candidates_layout.removeWidget(oldest)
                oldest.setParent(None)
                oldest.deleteLater()

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