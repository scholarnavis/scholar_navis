"""聊天会话搜索（ChatSearchMixin）。

复用 :mod:`src.ui.components.search` 的共享组件：
* :class:`SearchBar` —— 搜索输入条 UI 与键盘交互（本 mixin 不复制任何样式/防抖）；
* 跨气泡的搜索、高亮与滚动定位由本 mixin 编排：每个气泡的正文是独立的
  QTextBrowser（独立 QTextDocument），故逐气泡建 QTextCursor 命中表。

职责边界
--------
* 本 mixin 只做"编排 + 导航"，不涉及发送/渲染/附件逻辑；
* 气泡内部控件的获取以 ``getattr`` 守卫，气泡实现变化时不至于崩溃。
"""

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QShortcut, QTextCursor, QTextCharFormat
from PySide6.QtWidgets import QPushButton, QTextEdit

from src.core.i18n import tr
from src.core.theme_manager import ThemeManager, strong_weight_css
from src.ui.components.chat_bubble import ChatBubbleWidget
from src.ui.components.search import SearchBar

logger = logging.getLogger(__name__)


class ChatSearchMixin:
    """聊天会话的搜索入口、命中高亮与导航。"""

    #: 新内容（流式输出等）导致布局变化后的重扫节流间隔（ms）
    _CHAT_SEARCH_REFRESH_MS = 500
    #: 命中滚动入视口时顶端预留的呼吸间距（px）
    _HIT_VIEW_MARGIN = 48

    def setup_chat_search(self, row2_layout, top_bar):
        """创建搜索按钮与搜索条并接线。

        :param row2_layout: 顶栏第 2 行（KB 行），搜索按钮插入到收起按钮之前；
        :param top_bar: 顶栏纵向布局，搜索条追加在其下（展开时占整行）。
        """
        self.btn_toggle_search = QPushButton(tr(" Search"))
        self.btn_toggle_search.setToolTip("Ctrl+F")
        self.btn_toggle_search.setCursor(Qt.PointingHandCursor)
        self.btn_toggle_search.clicked.connect(self.open_chat_search)
        # 插到最后一个控件（收起按钮）之前
        row2_layout.insertWidget(row2_layout.count() - 1, self.btn_toggle_search)

        self.search_bar = SearchBar(placeholder=tr("Search conversation..."))
        self.search_bar.setVisible(False)
        top_bar.addWidget(self.search_bar)
        self.search_bar.sig_search_triggered.connect(self._on_chat_search_text)
        self.search_bar.sig_next.connect(self.chat_find_next)
        self.search_bar.sig_prev.connect(self.chat_find_prev)
        self.search_bar.sig_close.connect(self.close_chat_search)

        # Ctrl+F：会话页内生效（输入框聚焦时同样可触发）
        self.shortcut_chat_find = QShortcut(QKeySequence("Ctrl+F"), self.widget)
        self.shortcut_chat_find.activated.connect(self.open_chat_search)

        # 命中表：[(bubble, browser, cursor), ...]，按文档顺序排列
        self._chat_search_hits = []
        self._chat_search_hit_index = -1
        # 每个浏览器的常规高亮选区：{browser: [ExtraSelection, ...]}
        self._chat_search_selections = {}

        self._chat_search_refresh = QTimer(self.widget)
        self._chat_search_refresh.setSingleShot(True)
        self._chat_search_refresh.timeout.connect(self._rerun_chat_search)

        ThemeManager().theme_changed.connect(self._apply_chat_search_theme)
        self._apply_chat_search_theme()

        logger.debug("Chat search initialized")

    # ---------- 入口 ----------

    def open_chat_search(self):
        logger.debug("Chat search opened")
        self.btn_toggle_search.setVisible(False)
        self.search_bar.open_bar()
        if self.search_bar.text():
            self._run_chat_search(self.search_bar.text(), keep_index=True)

    def close_chat_search(self):
        logger.debug("Chat search closed")
        self.search_bar.close_bar()
        self.btn_toggle_search.setVisible(True)
        self._clear_chat_matches()

    # ---------- 搜索执行 ----------

    def _on_chat_search_text(self, query):
        self._run_chat_search(query)

    def _run_chat_search(self, query, keep_index=False):
        self._clear_chat_matches(reset_index=True)

        if not query:
            self.search_bar.set_count(-1, 0)
            return

        tm = ThemeManager()
        # 常规命中：accent 底色；当前命中：warning 底色（导航时一目了然）
        fmt_normal = QTextCharFormat()
        fmt_normal.setBackground(QColor(tm.color("accent")))
        fmt_normal.setForeground(QColor(tm.color("bg_base")))
        fmt_current = QTextCharFormat()
        fmt_current.setBackground(QColor(tm.color("warning")))
        fmt_current.setForeground(QColor(tm.color("bg_base")))
        self._fmt_chat_hit_normal = fmt_normal
        self._fmt_chat_hit_current = fmt_current

        hits = []
        per_browser = {}

        for bubble in self._iter_chat_bubbles():
            browser = getattr(bubble, "lbl_text", None)
            if browser is None:
                continue
            doc = browser.document()
            cursor = QTextCursor(doc)
            found = []
            while True:
                cursor = doc.find(query, cursor)
                if cursor.isNull():
                    break
                found.append(QTextCursor(cursor))
            if not found:
                continue
            sels = []
            for c in found:
                sel = QTextEdit.ExtraSelection()
                sel.format = fmt_normal
                sel.cursor = c
                sels.append(sel)
            per_browser[browser] = sels
            for c in found:
                hits.append((bubble, browser, c))

        self._chat_search_hits = hits
        self._chat_search_selections = per_browser
        for browser, sels in per_browser.items():
            browser.setExtraSelections(sels)

        logger.debug("Chat search %r: %d hits in %d bubbles",
                     query, len(hits), len(per_browser))

        if hits:
            start = self._chat_search_hit_index if keep_index else -1
            if not (0 <= start < len(hits)):
                start = 0
            self._focus_chat_hit(start)
        else:
            self._chat_search_hit_index = -1
            self.search_bar.set_count(-1, 0)

    def _focus_chat_hit(self, index):
        """把第 index 个命中设为当前项：差异化高亮 + 滚动入视口。"""
        if not self._chat_search_hits:
            return
        index %= len(self._chat_search_hits)
        self._chat_search_hit_index = index

        bubble, browser, cursor = self._chat_search_hits[index]

        # 刷新各浏览器选区：仅当前命中换用 current 格式
        for b, sels in self._chat_search_selections.items():
            changed = False
            for sel in sels:
                is_current = (b is browser and sel.cursor == cursor)
                new_fmt = self._fmt_chat_hit_current if is_current else self._fmt_chat_hit_normal
                if sel.format != new_fmt:
                    sel.format = new_fmt
                    changed = True
            if changed:
                b.setExtraSelections(list(sels))

        browser.setTextCursor(cursor)
        self.search_bar.set_count(index, len(self._chat_search_hits))
        self._scroll_hit_into_view(bubble, browser, cursor)

    def chat_find_next(self):
        if not self._chat_search_hits:
            if self.search_bar.text():
                self._run_chat_search(self.search_bar.text())
            return
        self._focus_chat_hit(self._chat_search_hit_index + 1)

    def chat_find_prev(self):
        if not self._chat_search_hits:
            if self.search_bar.text():
                self._run_chat_search(self.search_bar.text())
            return
        self._focus_chat_hit(self._chat_search_hit_index - 1)

    # ---------- 清理与刷新 ----------

    def _clear_chat_matches(self, reset_index=True):
        for browser in list(self._chat_search_selections):
            try:
                browser.setExtraSelections([])
            except RuntimeError:
                pass  # 气泡已被清理（C++ 对象销毁），跳过
        self._chat_search_hits = []
        self._chat_search_selections = {}
        if reset_index:
            self._chat_search_hit_index = -1

    def _schedule_chat_search_refresh(self):
        """布局可能变化（新气泡/流式增长）后节流重扫，保持命中表新鲜。"""
        if not hasattr(self, "search_bar"):
            return
        if self.search_bar.isVisible() and self.search_bar.text():
            self._chat_search_refresh.start(self._CHAT_SEARCH_REFRESH_MS)

    def _rerun_chat_search(self):
        if self.search_bar.isVisible() and self.search_bar.text():
            self._run_chat_search(self.search_bar.text(), keep_index=True)

    def clear_chat_history(self):
        """清空会话时同步失效命中表，再交回基础实现。"""
        self._clear_chat_matches()
        if hasattr(self, "search_bar"):
            self.search_bar.set_count(-1, 0)
        super().clear_chat_history()

    # ---------- 辅助 ----------

    def _iter_chat_bubbles(self):
        """按对话顺序产出全部消息气泡。"""
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, ChatBubbleWidget):
                yield w

    def _scroll_hit_into_view(self, bubble, browser, cursor):
        """把命中位置平滑滚动到视口内（长回答中的命中也能定位到行）。"""
        try:
            rect = browser.cursorRect(cursor)
        except RuntimeError:
            return
        top_left = browser.viewport().mapTo(self.chat_container, rect.topLeft())
        sb = self.scroll_area.verticalScrollBar()
        target = max(0, top_left.y() - self._HIT_VIEW_MARGIN)
        target = min(target, sb.maximum())

        if hasattr(self, "scroll_anim") and sb.value() != target:
            self.scroll_anim.stop()
            self.scroll_anim.setDuration(250)
            self.scroll_anim.setStartValue(sb.value())
            self.scroll_anim.setEndValue(target)
            self.scroll_anim.start()
        else:
            sb.setValue(target)

    def _apply_chat_search_theme(self):
        """搜索按钮随主题刷新（搜索条样式由 SearchBar 自管理）。"""
        btn = getattr(self, "btn_toggle_search", None)
        if btn is None:
            return
        tm = ThemeManager()
        btn.setText(tr(" Search"))
        btn.setIcon(tm.icon("search", "text_main"))
        btn.setStyleSheet(f"""
            QPushButton {{ background-color: {tm.color('btn_bg')}; color: {tm.color('text_main')};
                           border: 1px solid {tm.color('border')}; padding: 4px 10px;
                           border-radius: 4px; font-weight: {strong_weight_css()}; }}
            QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}
        """)
