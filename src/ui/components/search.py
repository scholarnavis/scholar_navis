"""可复用搜索组件（唯一来源，供日志面板 / 聊天面板等宿主共用）。

组成
----
* :class:`SearchBar` —— 搜索输入条 UI：输入框、命中计数、上一个/下一个/关闭
  按钮；内置 300ms 防抖与 Enter / Shift+Enter / Esc 键盘交互，样式跟随主题。
* :class:`DocumentSearcher` —— 面向 QTextDocument 系编辑器（QPlainTextEdit /
  QTextEdit / QTextBrowser）的搜索引擎：全量高亮 + 逐个导航，通过信号向宿主
  汇报命中进度。

约定
----
宿主只负责"把 SearchBar 的信号接到合适的搜索器/导航逻辑上"，不在自己的
代码里复制任何输入框样式、防抖或高亮遍历逻辑——改一处即全局生效。
"""

import logging

from PySide6.QtCore import QObject, QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QTextCursor, QTextCharFormat
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QTextEdit, QWidget)

from src.core.i18n import tr
from src.core.theme_manager import ThemeManager, strong_weight_css

logger = logging.getLogger(__name__)

#: 输入防抖间隔（ms）：停止输入后才开始/重跑搜索，避免逐字符全量扫描。
SEARCH_DEBOUNCE_MS = 300


class SearchBar(QWidget):
    """搜索输入条：输入框 + 计数 + 导航/关闭按钮。

    信号
    ----
    ``sig_search_triggered(str)``  查询词变化（已防抖；清空时立即发出空串）
    ``sig_next`` / ``sig_prev``    Enter / Shift+Enter 或按钮触发
    ``sig_close``                  关闭按钮或 Esc 触发
    """

    sig_search_triggered = Signal(str)
    sig_next = Signal()
    sig_prev = Signal()
    sig_close = Signal()

    def __init__(self, parent=None, placeholder=""):
        super().__init__(parent)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 10, 0)

        self.search_input = QLineEdit()
        if placeholder:
            self.search_input.setPlaceholderText(placeholder)
        self.search_input.textChanged.connect(self._on_text_changed)
        # 输入框内的回车/Esc 由事件过滤器拦截，避免 QLineEdit 吃掉按键
        self.search_input.installEventFilter(self)

        self.lbl_count = QLabel("0/0")

        self.btn_prev = QPushButton()
        self.btn_prev.setToolTip(tr("Previous (Shift+Enter)"))
        self.btn_prev.clicked.connect(self.sig_prev.emit)

        self.btn_next = QPushButton()
        self.btn_next.setToolTip(tr("Next (Enter)"))
        self.btn_next.clicked.connect(self.sig_next.emit)

        self.btn_close = QPushButton()
        self.btn_close.setToolTip(tr("Close Search"))
        self.btn_close.clicked.connect(self.sig_close.emit)

        layout.addWidget(self.search_input)
        layout.addWidget(self.lbl_count)
        layout.addWidget(self.btn_prev)
        layout.addWidget(self.btn_next)
        layout.addWidget(self.btn_close)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._emit_query)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

        logger.debug("SearchBar initialized")

    # ---------- 对宿主的便捷 API ----------

    def text(self) -> str:
        return self.search_input.text()

    def open_bar(self):
        """展开并聚焦输入框（全选已有词，便于直接改写）。"""
        self.setVisible(True)
        self.search_input.setFocus()
        self.search_input.selectAll()

    def close_bar(self):
        """收起并静默清空查询词（不触发 sig_search_triggered）。"""
        self.setVisible(False)
        self.set_query_silent("")

    def set_query_silent(self, text: str):
        """程序化设置查询词但不触发任何搜索信号。"""
        self.search_input.blockSignals(True)
        self.search_input.setText(text)
        self.search_input.blockSignals(False)

    def set_count(self, current_index: int, total: int):
        """刷新命中计数显示。

        :param current_index: 当前命中下标（0 基；-1 表示尚无当前项）
        :param total: 命中总数（0 时显示 0/0）
        """
        if total <= 0:
            self.lbl_count.setText("0/0")
        else:
            self.lbl_count.setText(f"{max(current_index, 0) + 1}/{total}")

    # ---------- 内部实现 ----------

    def _on_text_changed(self, text: str):
        if not text:
            # 清空立即生效，让宿主马上撤销高亮
            self._debounce.stop()
            self.sig_search_triggered.emit("")
            return
        self._debounce.start(SEARCH_DEBOUNCE_MS)

    def _emit_query(self):
        self.sig_search_triggered.emit(self.search_input.text())

    def eventFilter(self, obj, event):
        if obj == self.search_input and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ShiftModifier:
                    self.sig_prev.emit()
                else:
                    self.sig_next.emit()
                return True
            if event.key() == Qt.Key_Escape:
                self.sig_close.emit()
                return True
        return super().eventFilter(obj, event)

    def _apply_theme(self):
        tm = ThemeManager()
        self.lbl_count.setStyleSheet(
            f"color: {tm.color('text_muted')}; font-weight: {strong_weight_css()}; margin: 0 5px;")

        small_btn_style = f"""
            QPushButton {{ background-color: {tm.color('bg_input')}; color: {tm.color('text_main')};
                           border: 1px solid {tm.color('border')}; padding: 4px 8px;
                           border-radius: 3px; font-weight: {strong_weight_css()}; }}
            QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}
        """
        self.btn_prev.setStyleSheet(small_btn_style)
        self.btn_next.setStyleSheet(small_btn_style)
        self.btn_close.setStyleSheet(small_btn_style)

        self.btn_prev.setIcon(tm.icon("chevron-up", "text_main"))
        self.btn_prev.setText("▲" if self.btn_prev.icon().isNull() else "")
        self.btn_next.setIcon(tm.icon("chevron-down", "text_main"))
        self.btn_next.setText("▼" if self.btn_next.icon().isNull() else "")
        self.btn_close.setIcon(tm.icon("close", "text_main"))
        self.btn_close.setText("✕" if self.btn_close.icon().isNull() else "")

        self.search_input.setStyleSheet(f"""
            QLineEdit {{ background-color: {tm.color('bg_input')}; color: {tm.color('text_main')};
                         border: 1px solid {tm.color('border')}; padding: 4px 8px; border-radius: 3px; }}
        """)


class DocumentSearcher(QObject):
    """QTextDocument 系编辑器的搜索/高亮/导航引擎。

    职责单一：只处理"一个编辑器文档内的全部命中"。宿主（如日志面板）把
    :class:`SearchBar` 的信号接到本类方法即可，无需自己实现遍历与高亮。
    """

    #: (当前命中下标（-1 表示无）, 命中总数)
    sig_matches_changed = Signal(int, int)

    def __init__(self, editor=None, parent=None):
        super().__init__(parent)
        self._editor = editor
        self._query = ""
        self._cursors = []
        self._current_index = -1

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.refresh)

        logger.debug("DocumentSearcher initialized (editor=%s)", bool(editor))

    def bind(self, editor):
        """绑定（或更换）目标编辑器。"""
        self._editor = editor

    # ---------- 搜索 ----------

    def search(self, query: str, keep_index: bool = False):
        """按 query 全量搜索并高亮。

        :param keep_index: True 时若当前下标仍有效则保持不动（用于主题刷新、
                           增量追加后的重扫），False 时跳回第一个命中。
        """
        if not self._editor:
            return

        if not query:
            self.clear()
            return

        self._query = query
        document = self._editor.document()
        cursor = QTextCursor(document)
        self._cursors = []

        # 兼容深浅色主题的高亮格式
        tm = ThemeManager()
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(tm.color("accent")))
        fmt.setForeground(QColor(tm.color("bg_base")))

        extra_selections = []
        while True:
            cursor = document.find(query, cursor)
            if cursor.isNull():
                break
            self._cursors.append(QTextCursor(cursor))

            selection = QTextEdit.ExtraSelection()
            selection.format = fmt
            selection.cursor = self._cursors[-1]
            extra_selections.append(selection)

        self._editor.setExtraSelections(extra_selections)

        if self._cursors:
            if keep_index and 0 <= self._current_index < len(self._cursors):
                pass  # 保持原下标
            else:
                self._current_index = 0
            self._highlight_current()
        else:
            self._current_index = -1
            self.sig_matches_changed.emit(-1, 0)

        logger.debug("DocumentSearcher search %r: %d matches", query, len(self._cursors))

    def refresh(self, keep_index: bool = True):
        """用上次的查询词重扫（编辑器内容或主题变化后调用）。"""
        if self._query:
            self.search(self._query, keep_index=keep_index)

    def schedule_refresh(self, delay: int = 500):
        """节流触发 :meth:`refresh`（新内容陆续到达时避免频繁全扫）。"""
        self._refresh_timer.start(delay)

    # ---------- 导航 ----------

    def next_match(self):
        if not self._cursors:
            return
        self._current_index = (self._current_index + 1) % len(self._cursors)
        self._highlight_current()

    def prev_match(self):
        if not self._cursors:
            return
        self._current_index = (self._current_index - 1) % len(self._cursors)
        self._highlight_current()

    @property
    def match_count(self) -> int:
        return len(self._cursors)

    @property
    def current_index(self) -> int:
        return self._current_index

    # ---------- 清理 ----------

    def clear(self):
        self._query = ""
        self._cursors = []
        self._current_index = -1
        if self._editor:
            self._editor.setExtraSelections([])
        self.sig_matches_changed.emit(-1, 0)

    # ---------- 内部实现 ----------

    def _highlight_current(self):
        if not self._cursors or self._current_index < 0:
            return
        self.sig_matches_changed.emit(self._current_index, len(self._cursors))
        self._editor.setTextCursor(self._cursors[self._current_index])
