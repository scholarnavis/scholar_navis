import html
import os

from PySide6.QtCore import QTimer
from PySide6.QtGui import QShortcut, QKeySequence, QTextCursor, QTextCharFormat, QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit, QTextEdit,
                               QPlainTextEdit)

from src.core.logger import get_qt_log_handler
from src.core.theme_manager import ThemeManager
from src.tools.base_tool import BaseTool


class LogTool(BaseTool):
    MAX_LOGS = 1000

    def __init__(self):
        super().__init__("System Logs")
        self.widget = None
        self.log_viewer = None
        self._log_buffer = []
        self._all_logs = []

        self._search_cursors = []
        self._current_search_index = -1

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

        handler.new_log_signal.connect(self.append_log)

    def get_ui_widget(self) -> QWidget:
        if self.widget: return self.widget

        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)

        top_bar = QHBoxLayout()
        self.lbl_title = QLabel("<h2>System Run Logs</h2>")
        top_bar.addWidget(self.lbl_title)
        top_bar.addStretch()

        self.btn_toggle_search = QPushButton("Search")
        self.btn_toggle_search.setToolTip("Ctrl+F")
        self.btn_toggle_search.clicked.connect(self.show_search)
        top_bar.addWidget(self.btn_toggle_search)

        self.search_widget = QWidget()
        search_layout = QHBoxLayout(self.search_widget)
        search_layout.setContentsMargins(0, 0, 10, 0)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search logs (Ctrl+F)...")
        self.search_input.textChanged.connect(self.on_search_text_changed)
        self.search_input.returnPressed.connect(self.find_next)

        self.lbl_search_count = QLabel("0/0")

        self.btn_find_prev = QPushButton()
        self.btn_find_prev.setToolTip("Previous (Shift+Enter)")
        self.btn_find_prev.clicked.connect(self.find_prev)

        self.btn_find_next = QPushButton()
        self.btn_find_next.setToolTip("Next (Enter)")
        self.btn_find_next.clicked.connect(self.find_next)

        self.btn_close_search = QPushButton()
        self.btn_close_search.setToolTip("Close Search")
        self.btn_close_search.clicked.connect(self.hide_search)

        search_layout.addWidget(self.search_input)
        search_layout.addWidget(self.lbl_search_count)
        search_layout.addWidget(self.btn_find_prev)
        search_layout.addWidget(self.btn_find_next)
        search_layout.addWidget(self.btn_close_search)

        self.search_widget.setVisible(False)
        top_bar.addWidget(self.search_widget)

        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self.clear_logs)
        top_bar.addWidget(self.btn_clear)

        layout.addLayout(top_bar)

        self.log_viewer = QPlainTextEdit()
        self.log_viewer.setReadOnly(True)
        self.log_viewer.setMaximumBlockCount(self.MAX_LOGS)
        layout.addWidget(self.log_viewer)

        # 全局快捷键映射
        self.shortcut_find = QShortcut(QKeySequence("Ctrl+F"), self.widget)
        self.shortcut_find.activated.connect(self.show_search)

        self.shortcut_find_prev = QShortcut(QKeySequence("Shift+Return"), self.widget)
        self.shortcut_find_prev.activated.connect(self.find_prev)
        self.shortcut_find_prev_alt = QShortcut(QKeySequence("Shift+Enter"), self.widget)
        self.shortcut_find_prev_alt.activated.connect(self.find_prev)

        self._search_timer = QTimer(self.widget)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._perform_search)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

        self._log_buffer.clear()
        return self.widget

    def show_search(self):
        self.search_widget.setVisible(True)
        self.btn_toggle_search.setVisible(False)
        self.search_input.setFocus()
        self.search_input.selectAll()
        if self.search_input.text():
            self._perform_search(keep_index=True)

    def hide_search(self):
        self.search_widget.setVisible(False)
        self.btn_toggle_search.setVisible(True)
        self.search_input.clear()
        self.log_viewer.setFocus()

    def on_search_text_changed(self, text):
        self._search_timer.start(250)  # 250ms防抖

    def _perform_search(self, keep_index=False):
        if not self.log_viewer: return
        query = self.search_input.text()

        if not query:
            self.lbl_search_count.setText("0/0")
            self._search_cursors = []
            self._current_search_index = -1
            self.log_viewer.setExtraSelections([])
            return

        document = self.log_viewer.document()
        cursor = QTextCursor(document)
        self._search_cursors = []

        # 兼容深浅色的高亮显示格式
        tm = ThemeManager()
        format = QTextCharFormat()
        format.setBackground(QColor(tm.color("accent")))
        format.setForeground(QColor(tm.color("bg_base")))

        extra_selections = []

        while True:
            cursor = document.find(query, cursor)
            if cursor.isNull():
                break
            self._search_cursors.append(cursor)

            selection = QTextEdit.ExtraSelection()
            selection.format = format
            selection.cursor = cursor
            extra_selections.append(selection)

        self.log_viewer.setExtraSelections(extra_selections)

        if self._search_cursors:
            if keep_index and 0 <= self._current_search_index < len(self._search_cursors):
                pass
            else:
                self._current_search_index = 0
            self._highlight_current_match()
        else:
            self._current_search_index = -1
            self.lbl_search_count.setText("0/0")

    def _highlight_current_match(self):
        if not self._search_cursors or self._current_search_index < 0:
            return

        self.lbl_search_count.setText(f"{self._current_search_index + 1}/{len(self._search_cursors)}")

        cursor = self._search_cursors[self._current_search_index]
        self.log_viewer.setTextCursor(cursor)

    def find_next(self):
        if not self._search_cursors:
            self._perform_search()
            return
        if len(self._search_cursors) > 0:
            self._current_search_index = (self._current_search_index + 1) % len(self._search_cursors)
            self._highlight_current_match()

    def find_prev(self):
        if not self._search_cursors:
            self._perform_search()
            return
        if len(self._search_cursors) > 0:
            self._current_search_index = (self._current_search_index - 1) % len(self._search_cursors)
            self._highlight_current_match()


    def _apply_theme(self):
        tm = ThemeManager()
        if not self.widget: return

        self.lbl_title.setStyleSheet(f"color: {tm.color('text_main')};")
        self.lbl_search_count.setStyleSheet(f"color: {tm.color('text_muted')}; font-weight: bold; margin: 0 5px;")

        btn_style = f"""
            QPushButton {{ background-color: {tm.color('btn_bg')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; padding: 6px 15px; border-radius: 4px; font-weight: bold; }}
            QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}
        """

        self.btn_toggle_search.setText(" Search")
        self.btn_toggle_search.setIcon(tm.icon("search", "text_main"))
        self.btn_toggle_search.setStyleSheet(btn_style)

        self.btn_clear.setText(" Clear")
        self.btn_clear.setIcon(tm.icon("delete", "text_main"))
        self.btn_clear.setStyleSheet(btn_style)

        small_btn_style = f"""
            QPushButton {{ background-color: {tm.color('bg_input')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; padding: 4px 8px; border-radius: 3px; font-weight: bold; }}
            QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}
        """
        self.btn_find_prev.setStyleSheet(small_btn_style)
        self.btn_find_next.setStyleSheet(small_btn_style)
        self.btn_close_search.setStyleSheet(small_btn_style)

        self.btn_find_prev.setIcon(tm.icon("chevron-up", "text_main"))
        self.btn_find_prev.setText("▲" if self.btn_find_prev.icon().isNull() else "")

        self.btn_find_next.setIcon(tm.icon("chevron-down", "text_main"))
        self.btn_find_next.setText("▼" if self.btn_find_next.icon().isNull() else "")

        self.btn_close_search.setIcon(tm.icon("close", "text_main"))
        self.btn_close_search.setText("✕" if self.btn_close_search.icon().isNull() else "")

        self.search_input.setStyleSheet(f"""
                    QLineEdit {{ background-color: {tm.color('bg_input')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; padding: 4px 8px; border-radius: 3px; }}
                """)

        self.log_viewer.setStyleSheet(f"""
                    QPlainTextEdit {{
                        background-color: {tm.color('bg_input')}; 
                        color: {tm.color('text_main')};
                        selection-background-color: {tm.color('accent')};
                        selection-color: {tm.color('bg_base')};
                        font-family: 'Consolas', monospace; font-size: 13px;
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

            # 刷新高亮选区颜色
            if self.search_widget.isVisible() and self.search_input.text():
                self._perform_search(keep_index=True)

    def clear_logs(self):
        self._all_logs.clear()
        self._log_buffer.clear()
        if self.log_viewer:
            self.log_viewer.clear()
            self._search_cursors = []
            self._current_search_index = -1
            self.lbl_search_count.setText("0/0")

    def append_log(self, level, msg, path="", line=0):
        self._all_logs.append((level, msg, path, line))
        if not self.log_viewer:
            self._log_buffer.append((level, msg, path, line))
            return

        self._render_text(level, msg, path, line)

        if hasattr(self, 'search_widget') and self.search_widget.isVisible() and self.search_input.text():
            self._search_timer.start(500)


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
