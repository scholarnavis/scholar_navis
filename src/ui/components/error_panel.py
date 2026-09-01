"""统一错误面板组件：所有 LLM / 任务错误共用同一美术样式。

面板包含三部分：

- 标题行（danger 色摘要）；
- 友好说明（面向用户的可操作建议）；
- 可折叠的 "Technical Details" 栏：展示程序运行中真实的错误信息
  （程序自身的异常 / LLM 原始返回），只读、等宽字体、可选中复制。

主题跟随 ThemeManager。由 ``chat_bubble`` 在两种场景创建：

1. MSG_ERROR 气泡（任务端友好终止，如"图片不被当前模型支持"）；
2. AI 气泡流式输出中的 ``<error_panel>`` 标记（运行时捕获的
   LLM 流式错误），解码协议见 ``src/core/llm_errors.py``。
"""
import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (QLabel, QPlainTextEdit, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from src.core.theme_manager import ThemeManager

logger = logging.getLogger(__name__)


def _rgba(hex_color: str, alpha: float) -> str:
    hex_color = hex_color.lstrip('#')
    if len(hex_color) == 3:
        hex_color = ''.join([c * 2 for c in hex_color])
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"


class ErrorPanelWidget(QWidget):
    """错误面板：标题 / 建议 / 可折叠技术详情，全应用统一错误样式。"""

    #: 技术详情展开区的最大高度（像素），超出后内部滚动
    MAX_DETAILS_HEIGHT = 200

    def __init__(self, payload: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("ErrorPanel")
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self._expanded = False

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(10, 8, 10, 8)
        self._root.setSpacing(6)

        self.lbl_title = QLabel(self)
        self.lbl_title.setWordWrap(True)
        self._root.addWidget(self.lbl_title)

        self.lbl_body = QLabel(self)
        self.lbl_body.setWordWrap(True)
        self.lbl_body.setTextFormat(Qt.PlainText)
        self._root.addWidget(self.lbl_body)

        self.btn_details = QPushButton(self)
        self.btn_details.setCursor(Qt.PointingHandCursor)
        self.btn_details.setFlat(True)
        self.btn_details.clicked.connect(self.toggle_details)
        self._root.addWidget(self.btn_details)

        self.txt_details = QPlainTextEdit(self)
        self.txt_details.setReadOnly(True)
        self.txt_details.setMaximumHeight(self.MAX_DETAILS_HEIGHT)
        self.txt_details.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.txt_details.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self._root.addWidget(self.txt_details)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self.update_payload(payload if isinstance(payload, dict)
                            else {"title": "Generation Error", "body": str(payload)})
        self._apply_theme()

    # ------------------------------------------------------------------ #
    #  内容装载
    # ------------------------------------------------------------------ #
    def update_payload(self, payload: dict):
        """幂等装载 payload（重复渲染同一错误不会闪烁或重复插入）。"""
        if not isinstance(payload, dict):
            payload = {"title": "Generation Error", "body": str(payload)}

        self.lbl_title.setText(f"⚠ {payload.get('title', 'Generation Error')}")

        body = str(payload.get('body', '') or '')
        self.lbl_body.setText(body)
        self.lbl_body.setVisible(bool(body))

        details = str(payload.get('details', '') or '').strip()
        self.txt_details.setPlainText(details)
        self.btn_details.setVisible(bool(details))
        self.set_expanded(False)

    # ------------------------------------------------------------------ #
    #  折叠栏
    # ------------------------------------------------------------------ #
    def set_expanded(self, expanded: bool):
        self._expanded = bool(expanded)
        has_details = bool(self.txt_details.toPlainText().strip())
        self.txt_details.setVisible(self._expanded and has_details)
        self.btn_details.setText(
            "▾ Hide Technical Details" if self._expanded else "▸ Show Technical Details")

    def toggle_details(self):
        self.set_expanded(not self._expanded)

    # ------------------------------------------------------------------ #
    #  主题适配
    # ------------------------------------------------------------------ #
    def _apply_theme(self):
        tm = ThemeManager()
        danger = tm.color('danger')
        is_dark = tm.current_theme == 'dark'
        bg = _rgba(danger, 0.10 if is_dark else 0.05)
        font_family = tm.font_family()
        mono_family = self.txt_details.font().family()

        # 外框样式与全局错误语义一致：danger 左侧竖条 + 浅 danger 底
        self.setStyleSheet(f"""
            QWidget#ErrorPanel {{
                background-color: {bg};
                border: 1px solid {tm.color('border')};
                border-left: 4px solid {danger};
                border-radius: 6px;
            }}
        """)
        self.lbl_title.setStyleSheet(
            f"color: {danger}; font-weight: bold; font-size: 14px; "
            f"background: transparent; border: none; font-family: {font_family};")
        self.lbl_body.setStyleSheet(
            f"color: {tm.color('text_main')}; font-size: 13px; "
            f"background: transparent; border: none; font-family: {font_family};")
        self.btn_details.setStyleSheet(f"""
            QPushButton {{
                background: transparent; border: none; text-align: left;
                color: {tm.color('text_muted')}; font-size: 11px; font-weight: bold;
                font-family: {font_family}; padding: 2px 0px;
            }}
            QPushButton:hover {{ color: {tm.color('accent')}; }}
        """)
        self.txt_details.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {tm.color('bg_main')}; color: {tm.color('text_main')};
                border: 1px dashed {tm.color('border')}; border-radius: 4px;
                font-family: "{mono_family}"; font-size: 11px; padding: 6px;
            }}
        """)
