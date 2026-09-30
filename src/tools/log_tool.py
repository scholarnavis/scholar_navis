import html
import logging
import os

from PySide6.QtCore import QObject, Slot
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QPlainTextEdit

from src.core.i18n import tr
from src.core.logger import get_qt_log_handler
from src.core.theme_manager import ThemeManager, strong_weight_css
from src.tools.base_tool import BaseTool
from src.ui.components.search import DocumentSearcher, SearchBar

logger = logging.getLogger(__name__)


class LogReceiver(QObject):
    """
    代理接收器：通过 @Slot 强制将后台线程发出的日志信号调度到主线程执行，防止跨线程操作 UI 导致静默失败
    """
    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    @Slot(str, str, str, int)
    def receive_log(self, level, msg, path, line):
        self._callback(level, msg, path, line)


class LogTool(BaseTool):
    MAX_LOGS = 1000

    # 由 UI 构建流程创建的控件与快捷键，仅作静态检查声明
    lbl_title: QLabel
    btn_toggle_search: QPushButton
    search_bar: SearchBar
    btn_clear: QPushButton
    shortcut_find: QShortcut
    shortcut_find_prev: QShortcut
    shortcut_find_prev_alt: QShortcut

    def __init__(self):
        super().__init__("System Logs")
        self.widget = None
        self.log_viewer = None
        self._log_buffer = []
        self._all_logs = []

        # 搜索：UI（SearchBar）与引擎（DocumentSearcher）均来自共享组件，
        # 本类只做信号接线，不复制任何遍历/高亮逻辑
        self._searcher = DocumentSearcher()
        self._searcher.sig_matches_changed.connect(self._on_matches_changed)
        self._receiver = LogReceiver(self.append_log)

        handler = get_qt_log_handler()
        for log_entry in handler.log_history:
            if len(log_entry) == 4:
                lvl, msg, path, line = log_entry
            else:
                lvl, msg = log_entry[0], log_entry[1]
                path, line = "", 0

            self._log_buffer.append((lvl, msg, path, line))
            self._all_logs.append((lvl, msg, path, line))

        if len(self._all_logs) > self.MAX_LOGS:
            self._all_logs = self._all_logs[-self.MAX_LOGS:]

        handler.new_log_signal.connect(self._receiver.receive_log)

    def get_ui_widget(self) -> QWidget:
        if self.widget: return self.widget

        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)

        top_bar = QHBoxLayout()
        self.lbl_title = QLabel(tr("<h2>System Run Logs</h2>"))
        top_bar.addWidget(self.lbl_title)
        top_bar.addStretch()

        # 统一用 " Search"（与 _apply_theme 里 setText 的文案同键），
        # 避免初始文案与主题刷新后各写一份造成键重复
        self.btn_toggle_search = QPushButton(tr(" Search"))
        self.btn_toggle_search.setToolTip("Ctrl+F")
        self.btn_toggle_search.clicked.connect(self.show_search)
        top_bar.addWidget(self.btn_toggle_search)

        self.search_bar = SearchBar(placeholder=tr("Search logs (Ctrl+F)..."))
        self.search_bar.setVisible(False)
        top_bar.addWidget(self.search_bar)
        self.search_bar.sig_search_triggered.connect(self._on_search_text)
        self.search_bar.sig_next.connect(self.find_next)
        self.search_bar.sig_prev.connect(self.find_prev)
        self.search_bar.sig_close.connect(self.hide_search)

        self.btn_clear = QPushButton(tr(" Clear"))
        self.btn_clear.clicked.connect(self.clear_logs)
        top_bar.addWidget(self.btn_clear)

        layout.addLayout(top_bar)

        self.log_viewer = QPlainTextEdit()
        self.log_viewer.setReadOnly(True)
        self.log_viewer.setMaximumBlockCount(self.MAX_LOGS)
        layout.addWidget(self.log_viewer)

        # 搜索引擎绑定日志视图
        self._searcher.bind(self.log_viewer)

        # 全局快捷键映射（输入框内的 Enter / Shift+Enter / Esc 由 SearchBar 处理）
        self.shortcut_find = QShortcut(QKeySequence("Ctrl+F"), self.widget)
        self.shortcut_find.activated.connect(self.show_search)

        self.shortcut_find_prev = QShortcut(QKeySequence("Shift+Return"), self.widget)
        self.shortcut_find_prev.activated.connect(self.find_prev)
        self.shortcut_find_prev_alt = QShortcut(QKeySequence("Shift+Enter"), self.widget)
        self.shortcut_find_prev_alt.activated.connect(self.find_prev)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

        self._log_buffer.clear()
        return self.widget

    # ---------- SearchBar / DocumentSearcher 接线 ----------

    def show_search(self):
        self.btn_toggle_search.setVisible(False)
        self.search_bar.open_bar()
        if self.search_bar.text():
            self._searcher.search(self.search_bar.text(), keep_index=True)

    def hide_search(self):
        self.search_bar.close_bar()
        self.btn_toggle_search.setVisible(True)
        self._searcher.clear()
        self.log_viewer.setFocus()

    def _on_search_text(self, query):
        self._searcher.search(query)

    def _on_matches_changed(self, current_index, total):
        self.search_bar.set_count(current_index, total)

    def find_next(self):
        if not self._searcher.match_count and self.search_bar.text():
            self._searcher.search(self.search_bar.text())
            return
        self._searcher.next_match()

    def find_prev(self):
        if not self._searcher.match_count and self.search_bar.text():
            self._searcher.search(self.search_bar.text())
            return
        self._searcher.prev_match()


    def _apply_theme(self):
        tm = ThemeManager()
        if not self.widget: return

        self.lbl_title.setStyleSheet(f"color: {tm.color('text_main')};")

        btn_style = f"""
            QPushButton {{ background-color: {tm.color('btn_bg')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; padding: 6px 15px; border-radius: 4px; font-weight: {strong_weight_css()}; }}
            QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}
        """

        self.btn_toggle_search.setText(tr(" Search"))
        self.btn_toggle_search.setIcon(tm.icon("search", "text_main"))
        self.btn_toggle_search.setStyleSheet(btn_style)

        self.btn_clear.setText(tr(" Clear"))
        self.btn_clear.setIcon(tm.icon("delete", "text_main"))
        self.btn_clear.setStyleSheet(btn_style)

        # 搜索条（输入框/计数/导航按钮）样式由 SearchBar 自管理，无需重复

        self.log_viewer.setStyleSheet(f"""
                    QPlainTextEdit {{
                        background-color: {tm.color('bg_input')};
                        color: {tm.color('text_main')};
                        selection-background-color: {tm.color('accent')};
                        selection-color: {tm.color('bg_base')};
                        font-family: {tm.mono_font_family()}; font-size: 13px;
                        border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 10px;
                    }}
                """)

        if self.log_viewer:
            sb = self.log_viewer.verticalScrollBar()
            val = sb.value()

            self.log_viewer.clear()
            for lvl, msg, path, line in self._all_logs:
                self._render_text(lvl, msg, path, line)

            sb.setValue(val)

            # 主题切换后重扫，刷新高亮选区颜色
            if self.search_bar.isVisible() and self.search_bar.text():
                self._searcher.search(self.search_bar.text(), keep_index=True)

    def clear_logs(self):
        self._all_logs.clear()
        self._log_buffer.clear()
        if self.log_viewer:
            self.log_viewer.clear()
            self._searcher.clear()

    def append_log(self, level, msg, path="", line=0):
        self._all_logs.append((level, msg, path, line))
        if not self.log_viewer:
            self._log_buffer.append((level, msg, path, line))
            return

        self._render_text(level, msg, path, line)

        # 搜索条展开且有查询词时节流重扫，让新日志即时纳入命中
        if self.search_bar.isVisible() and self.search_bar.text():
            self._searcher.schedule_refresh(500)


    def _render_text(self, level, msg, path="", line=0):
        tm = ThemeManager()
        color = tm.color('text_muted')
        if level == "INFO":
            color = tm.color('success')
        elif level == "WARNING":
            color = tm.color('warning')
        elif level in ["ERROR", "CRITICAL"]:
            color = tm.color('danger')

        if path and line:
            file_name = os.path.basename(path)
            log_text = html.escape(f"[{level}] {msg} ({file_name}:{line})").replace('\n', '<br>')
        else:
            log_text = html.escape(f"[{level}] {msg}").replace('\n', '<br>')

        colored_html = f'<span style="color:{color}; white-space: pre-wrap;">{log_text}</span>'

        sb = self.log_viewer.verticalScrollBar()
        is_at_bottom = sb.value() >= (sb.maximum() - 15)

        self.log_viewer.appendHtml(colored_html)

        if is_at_bottom:
            sb.setValue(sb.maximum())
