"""
通用源码查看 / 编辑控件
======================

统一项目内所有"源码展示"场景（R 绘图脚本、Mermaid 源码、开发者模式
日志输出等）的 UI 表现，保证：

    * 最大高度 / 最大宽度固定，超出部分由控件内部独立的滚动条接管；
    * 自带边框与标题栏底纹，与周围内容视觉区分；
    * 标题栏提供"复制源码"与"折叠 / 展开"按钮；
    * 深色 / 浅色模式跟随 ThemeManager 自适应。

设计原则：
    * 高内聚：自身完成布局、主题、复制、折叠，不依赖调用方拼 QSS；
    * 低耦合：仅依赖 ThemeManager，任何需要展示源码的地方均可直接使用。

用法示例::

    viewer = SourceCodeViewer(title="R Plot Script (.R)", collapsed=True)
    viewer.set_code(script_text)
    layout.addWidget(viewer)
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.core.theme_manager import ThemeManager

logger = logging.getLogger(__name__)

_MONO_FAMILY = "'Consolas', 'Courier New', monospace"


def _hex_to_rgba(color: str, alpha: float) -> str:
    """将 #RRGGBB 颜色转为 rgba()，非 hex 值原样返回。"""
    color = str(color).strip()
    if not color.startswith("#") or len(color) != 7:
        return color
    try:
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
    except ValueError:
        return color
    a = max(0.0, min(1.0, alpha))
    return f"rgba({r}, {g}, {b}, {a})"


class SourceCodeViewer(QFrame):
    """带标题栏（复制 / 折叠）、边框底纹、独立滚动条与深色模式适配的源码控件。"""

    textChanged = Signal()
    collapsedChanged = Signal(bool)

    def __init__(
        self,
        title: str = "Source Code",
        editable: bool = False,
        collapsed: bool = False,
        max_height: int = 320,
        max_width: Optional[int] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setObjectName("SourceCodeViewer")

        self._title = title
        self._editable = editable
        self._collapsed = collapsed
        self._max_height = max(80, max_height)
        self._max_width = max_width
        self._extra_tool_buttons: list[QPushButton] = []

        self._build_ui()
        self._apply_theme()
        ThemeManager().theme_changed.connect(self._apply_theme)

    # --------------------------------------------------------------- UI ---
    def _build_ui(self):
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(), self.sizePolicy().verticalPolicy())
        if self._max_width:
            self.setMaximumWidth(self._max_width)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        # --- 标题栏 ---
        self._header = QWidget(self)
        self._header.setObjectName("SCVHeader")
        self._header.setFixedHeight(30)
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(8, 0, 6, 0)
        header_layout.setSpacing(4)

        self._title_label = QLabel(self._title)
        self._title_label.setProperty("scvRole", "title")

        self._btn_copy = QPushButton(" Copy")
        self._btn_copy.setProperty("scvRole", "btn")
        self._btn_copy.setCursor(Qt.PointingHandCursor)
        self._btn_copy.clicked.connect(self._copy_code)

        self._btn_collapse = QPushButton()
        self._btn_collapse.setProperty("scvRole", "btn")
        self._btn_collapse.setCursor(Qt.PointingHandCursor)
        self._btn_collapse.clicked.connect(self.toggle_collapsed)

        header_layout.addWidget(self._title_label)
        header_layout.addStretch(1)
        header_layout.addWidget(self._btn_copy)
        header_layout.addWidget(self._btn_collapse)
        self._layout.addWidget(self._header)

        # --- 源码内容区（独立滚动条由 QPlainTextEdit 内置） ---
        self._editor = QPlainTextEdit()
        self._editor.setObjectName("SCVEditor")
        self._editor.setReadOnly(not self._editable)
        self._editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._editor.setMaximumHeight(self._max_height)
        self._editor.setMinimumHeight(60)
        self._editor.textChanged.connect(self.textChanged)
        self._layout.addWidget(self._editor)

        self.set_collapsed(self._collapsed, emit=False)

    # ------------------------------------------------------------ theme ---
    def _apply_theme(self):
        tm = ThemeManager()
        card_bg = tm.color("bg_card")
        border = tm.color("border")
        text_main = tm.color("text_main")
        text_muted = tm.color("text_muted")
        accent = tm.color("accent")
        btn_bg = tm.color("btn_bg")
        btn_hover = tm.color("btn_hover")
        bg_main = tm.color("bg_main")
        bg_input = tm.color("bg_input")
        font_family = tm.font_family()

        header_bg = _hex_to_rgba(bg_main, 0.5) if str(bg_main).startswith("#") else bg_main
        editor_bg = bg_input if str(bg_input) not in ("", "None") else bg_main
        if str(editor_bg) in ("", "None"):
            editor_bg = bg_main
        # 深色模式下代码区使用更深的底色，便于与正文区分
        if tm.current_theme == "dark":
            editor_bg = _hex_to_rgba(bg_main, 0.85) if str(bg_main).startswith("#") else bg_main
        else:
            editor_bg = "#f7f7f9"

        self.setStyleSheet(f"""
            QFrame#SourceCodeViewer {{
                background-color: {card_bg};
                border: 1px solid {border};
                border-radius: 6px;
            }}
            QWidget#SCVHeader {{
                background-color: {header_bg};
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
                border-bottom: 1px solid {border};
            }}
            QLabel[scvRole="title"] {{
                color: {text_muted};
                font-size: 11px;
                font-weight: bold;
                font-family: {font_family};
            }}
            QPushButton[scvRole="btn"] {{
                background: transparent;
                border: none;
                color: {text_muted};
                font-size: 11px;
                font-family: {font_family};
                padding: 2px 6px;
                border-radius: 3px;
            }}
            QPushButton[scvRole="btn"]:hover {{
                background: {btn_hover};
                color: {accent};
            }}
            QPlainTextEdit#SCVEditor {{
                background-color: {editor_bg};
                color: {text_main};
                border: none;
                border-bottom-left-radius: 5px;
                border-bottom-right-radius: 5px;
                padding: 4px;
                font-family: {_MONO_FAMILY};
                font-size: 12px;
                selection-background-color: {accent};
                selection-color: #ffffff;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 10px;
                margin: 2px;
            }}
            QScrollBar::handle:vertical {{
                background: {_hex_to_rgba(text_muted, 0.35)};
                border-radius: 4px;
                min-height: 24px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {accent};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar:horizontal {{
                background: transparent;
                height: 10px;
                margin: 2px;
            }}
            QScrollBar::handle:horizontal {{
                background: {_hex_to_rgba(text_muted, 0.35)};
                border-radius: 4px;
                min-width: 24px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background: {accent};
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0;
            }}
        """)

        self._btn_copy.setIcon(tm.icon("copy", "text_muted"))
        self._update_collapse_icon()

    # ----------------------------------------------------------- public ---
    def set_code(self, text: str):
        """设置源码内容（不触发 textChanged 信号）。"""
        self._editor.blockSignals(True)
        self._editor.setPlainText(text or "")
        self._editor.blockSignals(False)

    def code(self) -> str:
        return self._editor.toPlainText()

    def editor(self) -> QPlainTextEdit:
        """返回内部编辑器（用于外挂语法高亮器、自定义事件等）。"""
        return self._editor

    def document(self):
        """返回内部编辑器的 document（供 QSyntaxHighlighter 使用）。"""
        return self._editor.document()

    def append(self, text: str):
        self._editor.appendPlainText(text)

    def clear(self):
        self._editor.clear()

    def is_collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool, emit: bool = True):
        collapsed = bool(collapsed)
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self._editor.setVisible(not collapsed)
        self._update_collapse_icon()
        if emit:
            self.collapsedChanged.emit(collapsed)

    def toggle_collapsed(self):
        self.set_collapsed(not self._collapsed)

    def add_tool_button(self, text: str, slot: Callable[[], None]) -> QPushButton:
        """在标题栏追加自定义工具按钮（如"下载"），返回该按钮以便后续操作。"""
        btn = QPushButton(f" {text}")
        btn.setProperty("scvRole", "btn")
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(slot)
        self._extra_tool_buttons.append(btn)
        header_layout = self._header.layout()
        header_layout.insertWidget(header_layout.count() - 2, btn)
        self._apply_theme()
        return btn

    def set_header_visible(self, visible: bool):
        self._header.setVisible(visible)

    # --------------------------------------------------------- internal ---
    def _copy_code(self):
        text = self._editor.toPlainText()
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)
        old = self._btn_copy.text()
        self._btn_copy.setText(" Copied")
        self._btn_copy.setEnabled(False)
        QTimer.singleShot(1200, lambda: (self._btn_copy.setText(old),
                                         self._btn_copy.setEnabled(True)))
        logger.debug(f"Source code copied to clipboard ({len(text)} chars)")

    def _update_collapse_icon(self):
        if not hasattr(self, "_btn_collapse"):
            return
        tm = ThemeManager()
        if self._collapsed:
            self._btn_collapse.setIcon(tm.icon("chevron-right", "text_muted"))
            self._btn_collapse.setToolTip("Expand source code")
        else:
            self._btn_collapse.setIcon(tm.icon("chevron-down", "text_muted"))
            self._btn_collapse.setToolTip("Collapse source code")

    def closeEvent(self, event):
        try:
            ThemeManager().theme_changed.disconnect(self._apply_theme)
        except (TypeError, RuntimeError):
            pass
        super().closeEvent(event)
