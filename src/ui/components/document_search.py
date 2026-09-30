"""文档查看窗口共用的搜索工具栏。

``InternalPDFViewer``（QWebEngineView）与 ``InternalTextViewer``（QTextBrowser）
承载的控件不同，但"搜索输入框 + 上一个 / 下一个 / 命中计数"这套工具栏的
**构建与开关逻辑完全一致**；统一放在本 mixin，避免两个查看器各写一份。

子类需要提供以下钩子：

- :meth:`_find_next` / :meth:`_find_prev`：向前 / 向后查找（无参，供按钮与
  回车直接连接；不要让它们直接接收 ``clicked(bool)``，否则按钮的 bool 会被
  当成方向参数）；
- :meth:`_clear_search`：清除搜索高亮或选中态（关闭搜索栏时调用）。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QToolBar

from src.core.theme_manager import ThemeManager, strong_weight_css

logger = __import__("logging").getLogger(__name__)


class DocumentSearchBarMixin:
    """为文档类查看窗口提供统一的搜索工具栏与交互。"""

    def _setup_search_bar(self):
        """构建搜索工具栏（默认隐藏，Ctrl+F 或菜单触发显示）。"""
        tm = ThemeManager()
        self.search_toolbar = QToolBar()
        self.search_toolbar.setMovable(False)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search in document (Enter to find next)...")
        self.search_input.setMinimumWidth(250)
        self.search_input.returnPressed.connect(self._find_next)

        self.btn_do_search = QPushButton(" Search")
        self.btn_do_search.clicked.connect(self._find_next)

        self.lbl_search_count = QLabel(" 0 / 0 ")
        self.lbl_search_count.setStyleSheet(
            f"color: {tm.color('text_main')}; font-weight: {strong_weight_css()}; padding: 0 10px;")

        self.btn_find_prev = QPushButton(" Prev")
        self.btn_find_prev.clicked.connect(self._find_prev)

        self.btn_find_next = QPushButton(" Next")
        self.btn_find_next.clicked.connect(self._find_next)

        self.btn_close_search = QPushButton(" Close")
        self.btn_close_search.clicked.connect(self._close_search)

        for widget in (self.search_input, self.btn_do_search, self.lbl_search_count,
                       self.btn_find_prev, self.btn_find_next, self.btn_close_search):
            self.search_toolbar.addWidget(widget)

        self.addToolBar(Qt.TopToolBarArea, self.search_toolbar)
        self.search_toolbar.hide()

    def _toggle_search(self):
        if self.search_toolbar.isVisible():
            self._close_search()
        else:
            self.search_toolbar.show()
            self.search_input.setFocus()
            self.search_input.selectAll()

    def _close_search(self):
        """隐藏搜索工具栏并复位计数与高亮。"""
        self.search_toolbar.hide()
        self._clear_search()
        self.lbl_search_count.setText(" 0 / 0 ")

    # ---- 子类钩子（在各自的查看器中实现，因为检索 API 不同） ----
    def _find_next(self):
        raise NotImplementedError

    def _find_prev(self):
        raise NotImplementedError

    def _clear_search(self):
        raise NotImplementedError
