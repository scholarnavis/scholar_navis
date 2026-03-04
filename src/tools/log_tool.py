import html
import os
import shutil
import sys
import subprocess
import urllib.parse
from PySide6.QtCore import Qt, QUrl, QUrlQuery
from PySide6.QtGui import QShortcut, QKeySequence, QTextDocument, QTextCursor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QTextBrowser,
                               QHBoxLayout, QPushButton, QLabel, QLineEdit)
from src.tools.base_tool import BaseTool
from src.core.logger import get_qt_log_handler
from src.core.theme_manager import ThemeManager


class LogTool(BaseTool):
    MAX_LOGS = 1000

    def __init__(self):
        super().__init__("System Logs")
        self.widget = None
        self.log_viewer = None
        self._log_buffer = []
        self._all_logs = []

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

        # 顶部工具栏
        top_bar = QHBoxLayout()
        self.lbl_title = QLabel("<h2>System Run Logs</h2>")
        top_bar.addWidget(self.lbl_title)
        top_bar.addStretch()

        # ---------------- 优雅的搜索组件 ----------------
        self.search_widget = QWidget()
        search_layout = QHBoxLayout(self.search_widget)
        search_layout.setContentsMargins(0, 0, 10, 0)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search logs (Ctrl+F)...")
        self.search_input.returnPressed.connect(lambda: self.find_text(backward=False))

        self.btn_find_prev = QPushButton("▲")
        self.btn_find_prev.setToolTip("Previous (Shift+Enter)")
        self.btn_find_prev.clicked.connect(lambda: self.find_text(backward=True))

        self.btn_find_next = QPushButton("▼")
        self.btn_find_next.setToolTip("Next (Enter)")
        self.btn_find_next.clicked.connect(lambda: self.find_text(backward=False))

        self.btn_close_search = QPushButton("✕")
        self.btn_close_search.clicked.connect(self.hide_search)

        search_layout.addWidget(self.search_input)
        search_layout.addWidget(self.btn_find_prev)
        search_layout.addWidget(self.btn_find_next)
        search_layout.addWidget(self.btn_close_search)

        self.search_widget.setVisible(False)
        top_bar.addWidget(self.search_widget)
        # ------------------------------------------------

        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self.clear_logs)
        top_bar.addWidget(self.btn_clear)

        layout.addLayout(top_bar)

        self.log_viewer = QTextBrowser()
        self.log_viewer.setReadOnly(True)
        self.log_viewer.setOpenLinks(False)
        self.log_viewer.anchorClicked.connect(self._handle_link_clicked)
        self.log_viewer.document().setMaximumBlockCount(self.MAX_LOGS)
        layout.addWidget(self.log_viewer)

        self.shortcut_find = QShortcut(QKeySequence("Ctrl+F"), self.widget)
        self.shortcut_find.activated.connect(self.show_search)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

        self._log_buffer.clear()
        return self.widget

    # --- 搜索功能逻辑 ---
    def show_search(self):
        self.search_widget.setVisible(True)
        self.search_input.setFocus()
        self.search_input.selectAll()

    def hide_search(self):
        self.search_widget.setVisible(False)
        self.log_viewer.setFocus()
        cursor = self.log_viewer.textCursor()
        cursor.clearSelection()
        self.log_viewer.setTextCursor(cursor)

    def find_text(self, backward=False):
        query = self.search_input.text()
        if not query:
            return

        flags = QTextDocument.FindFlag(0)
        if backward:
            flags |= QTextDocument.FindBackward

        found = self.log_viewer.find(query, flags)

        if not found:
            cursor = self.log_viewer.textCursor()
            cursor.movePosition(QTextCursor.End if backward else QTextCursor.Start)
            self.log_viewer.setTextCursor(cursor)
            self.log_viewer.find(query, flags)

    # --- 文件跳转逻辑 (修复版) ---
    def _handle_link_clicked(self, url: QUrl):
        # 拦截自定义的 ide 协议
        if url.scheme() == "ide":
            query = QUrlQuery(url)
            # 解析被编码的安全路径
            file_path = urllib.parse.unquote(query.queryItemValue("file"))
            line = query.queryItemValue("line")

            if os.path.exists(file_path):
                if shutil.which('code'):
                    subprocess.Popen(['code', '-g', f'{file_path}:{line}'], shell=(sys.platform == 'win32'))

                elif shutil.which('pycharm'):
                    subprocess.Popen(['pycharm', '--line', str(line), file_path], shell=(sys.platform == 'win32'))
                elif shutil.which('pycharm64'):
                    subprocess.Popen(['pycharm64', '--line', str(line), file_path], shell=(sys.platform == 'win32'))

                else:
                    try:
                        if sys.platform == 'win32':
                            os.startfile(file_path)
                        elif sys.platform == 'darwin':
                            subprocess.Popen(['open', file_path])
                        else:
                            subprocess.Popen(['xdg-open', file_path])
                    except Exception as e:
                        print(f"Failed to open file: {e}")

    def _apply_theme(self):
        tm = ThemeManager()
        if not self.widget: return

        self.lbl_title.setStyleSheet(f"color: {tm.color('text_main')};")

        btn_style = f"""
            QPushButton {{ background-color: {tm.color('btn_bg')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; padding: 6px 15px; border-radius: 4px; font-weight: bold; }}
            QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}
        """
        self.btn_clear.setText(" Clear")
        self.btn_clear.setIcon(tm.icon("delete", "text_main"))
        self.btn_clear.setStyleSheet(btn_style)

        small_btn_style = f"""
            QPushButton {{ background-color: {tm.color('bg_input')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; padding: 4px 8px; border-radius: 3px; }}
            QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}
        """
        self.btn_find_prev.setStyleSheet(small_btn_style)
        self.btn_find_next.setStyleSheet(small_btn_style)
        self.btn_close_search.setStyleSheet(small_btn_style)

        self.search_input.setStyleSheet(f"""
            QLineEdit {{ background-color: {tm.color('bg_input')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; padding: 4px 8px; border-radius: 3px; }}
        """)

        self.log_viewer.setStyleSheet(f"""
            QTextBrowser {{
                background-color: {tm.color('bg_input')}; 
                color: {tm.color('text_main')};
                selection-background-color: {tm.color('accent')};
                selection-color: #ffffff;
                font-family: 'Consolas', monospace; font-size: 13px;
                border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 10px;
            }}
        """)

        if self.log_viewer:
            self.log_viewer.clear()
            for lvl, msg, path, line in self._all_logs:
                self._render_html(lvl, msg, path, line)

    def clear_logs(self):
        self._all_logs.clear()
        self._log_buffer.clear()
        if self.log_viewer:
            self.log_viewer.clear()

    def append_log(self, level, msg, path="", line=0):
        self._all_logs.append((level, msg, path, line))
        if not self.log_viewer:
            self._log_buffer.append((level, msg, path, line))
            return
        self._render_html(level, msg, path, line)

    def _render_html(self, level, msg, path="", line=0):
        tm = ThemeManager()
        color = tm.color('text_muted')
        if level == "INFO":
            color = tm.color('success')
        elif level == "WARNING":
            color = tm.color('warning')
        elif level in ["ERROR", "CRITICAL"]:
            color = tm.color('danger')

        safe_msg = html.escape(str(msg))

        # 侦测是否为 Nuitka/PyInstaller 打包环境
        is_compiled = getattr(sys, 'frozen', False) or hasattr(sys, 'compiled') or "nuitka" in sys.modules

        # 只有在有路径且非打包开发环境下，才渲染超链接 Tag
        if path and line and not is_compiled:
            safe_path = urllib.parse.quote(path)
            file_url = f"ide://open?file={safe_path}&line={line}"
            level_tag = f'<a href="{file_url}" style="color:{color}; text-decoration: none; font-weight: bold; cursor: pointer;">[{level}]</a>'
        else:
            # 打包后，或者没有路径信息时，直接渲染普通文本
            level_tag = f'<span style="color:{color}; font-weight: bold;">[{level}]</span>'

        html_str = f'<div style="color:{color}; margin-bottom: 2px; white-space: pre-wrap;">{level_tag} {safe_msg}</div>'

        sb = self.log_viewer.verticalScrollBar()
        is_at_bottom = sb.value() >= (sb.maximum() - 15)

        self.log_viewer.append(html_str)

        if is_at_bottom:
            sb.setValue(sb.maximum())