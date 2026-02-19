from PySide6.QtWidgets import (QWidget, QVBoxLayout, QPlainTextEdit,
                               QHBoxLayout, QPushButton, QLabel)
from src.tools.base_tool import BaseTool
from src.core.logger import get_qt_log_handler


class LogTool(BaseTool):
    def __init__(self):
        super().__init__("System Logs")
        self.widget = None
        self.log_viewer = None
        self._log_buffer = []

        handler = get_qt_log_handler()

        for level, msg in handler.log_history:
            self._log_buffer.append((level, msg))


        handler.new_log_signal.connect(self.append_log)

    def get_ui_widget(self) -> QWidget:
        if self.widget: return self.widget

        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)

        top_bar = QHBoxLayout()
        top_bar.addWidget(QLabel("<h2>System Run Logs</h2>"))
        top_bar.addStretch()

        btn_clear = QPushButton("🧹 Clear Logs")
        btn_clear.clicked.connect(self.clear_logs)
        btn_clear.setStyleSheet("""
            QPushButton { background-color: #3e3e42; color: #cccccc; border: 1px solid #555; padding: 6px 15px; border-radius: 4px; font-weight: bold;}
            QPushButton:hover { background-color: #4e4e52; }
        """)
        top_bar.addWidget(btn_clear)
        layout.addLayout(top_bar)

        self.log_viewer = QPlainTextEdit()
        self.log_viewer.setReadOnly(True)
        self.log_viewer.setStyleSheet("""
            QPlainTextEdit {
                background-color: #1e1e1e;
                color: #50fa7b;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 13px;
                border: 1px solid #333;
                border-radius: 4px;
                padding: 10px;
            }
        """)
        layout.addWidget(self.log_viewer)

        # 渲染缓冲区里的日志（包含历史回放的 + UI创建前瞬间产生的）
        if self._log_buffer:
            for level, msg in self._log_buffer:
                self._render_html(level, msg)
            self._log_buffer.clear()

        return self.widget

    def append_log(self, level, msg):
        if not self.log_viewer:
            self._log_buffer.append((level, msg))
            return
        self._render_html(level, msg)

    def _render_html(self, level, msg):
        color = "#a6a6a6"
        if level == "INFO":
            color = "#50fa7b"
        elif level == "WARNING":
            color = "#ffb86c"
        elif level in ["ERROR", "CRITICAL"]:
            color = "#ff5555"

        html = f'<div style="color:{color}; margin-bottom: 2px; white-space: pre-wrap;">[{level}] {msg}</div>'
        self.log_viewer.appendHtml(html)
        sb = self.log_viewer.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear_logs(self):
        if self.log_viewer:
            self.log_viewer.clear()
            self._render_html("INFO", "UI logs cleared by user.")

    def execute_task(self):
        pass