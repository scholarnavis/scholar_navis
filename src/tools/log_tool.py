import html
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QPlainTextEdit,
                               QHBoxLayout, QPushButton, QLabel)
from src.tools.base_tool import BaseTool
from src.core.logger import get_qt_log_handler
from src.core.theme_manager import ThemeManager


class LogTool(BaseTool):
    def __init__(self):
        super().__init__("System Logs")
        self.widget = None
        self.log_viewer = None
        self._log_buffer = []
        self._all_logs = []

        handler = get_qt_log_handler()
        for level, msg in handler.log_history:
            self._log_buffer.append((level, msg))
            self._all_logs.append((level, msg))

        handler.new_log_signal.connect(self.append_log)

    def get_ui_widget(self) -> QWidget:
        if self.widget: return self.widget

        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)

        top_bar = QHBoxLayout()
        self.lbl_title = QLabel("<h2>System Run Logs</h2>")
        top_bar.addWidget(self.lbl_title)
        top_bar.addStretch()

        self.btn_clear = QPushButton("🧹 Clear Logs")
        self.btn_clear.clicked.connect(self.clear_logs)
        top_bar.addWidget(self.btn_clear)

        layout.addLayout(top_bar)

        self.log_viewer = QPlainTextEdit()
        self.log_viewer.setReadOnly(True)
        layout.addWidget(self.log_viewer)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

        self._log_buffer.clear()
        return self.widget

    def _apply_theme(self):
        tm = ThemeManager()
        if not self.widget: return

        self.lbl_title.setStyleSheet(f"color: {tm.color('text_main')};")

        self.btn_clear.setStyleSheet(f"""
            QPushButton {{ background-color: {tm.color('btn_bg')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; padding: 6px 15px; border-radius: 4px; font-weight: bold; }}
            QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}
        """)

        self.log_viewer.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {tm.color('bg_input')}; color: {tm.color('text_main')};
                font-family: 'Consolas', monospace; font-size: 13px;
                border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 10px;
            }}
        """)

        self.log_viewer.clear()
        for level, msg in self._all_logs:
            self._render_html(level, msg)

    def clear_logs(self):
        self._all_logs.clear()
        self._log_buffer.clear()
        if self.log_viewer:
            self.log_viewer.clear()

    def append_log(self, level, msg):
        self._all_logs.append((level, msg))
        if not self.log_viewer:
            self._log_buffer.append((level, msg))
            return
        self._render_html(level, msg)

    def _render_html(self, level, msg):
        tm = ThemeManager()
        color = tm.color('text_muted')
        if level == "INFO":
            color = tm.color('success')
        elif level == "WARNING":
            color = tm.color('warning')
        elif level in ["ERROR", "CRITICAL"]:
            color = tm.color('danger')

        safe_msg = html.escape(str(msg))
        html_str = f'<div style="color:{color}; margin-bottom: 2px; white-space: pre-wrap;">[{level}] {safe_msg}</div>'

        sb = self.log_viewer.verticalScrollBar()
        is_at_bottom = sb.value() >= (sb.maximum() - 15)

        self.log_viewer.appendHtml(html_str)
        if is_at_bottom:
            sb.setValue(sb.maximum())

    def execute_task(self):
        pass