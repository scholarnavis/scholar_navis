"""内部 PDF 查看器（基于 QtWebEngine 的内置 PDF 阅读器）。

文本 / Markdown / 结构化数据的阅读窗口已拆分到
:mod:`src.ui.components.text_viewer`；两者仅共用搜索工具栏
（:class:`DocumentSearchBarMixin`）。
"""

import logging
import os
import shutil

from PySide6.QtPrintSupport import QPrinter, QPrintDialog
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QShortcut, QKeySequence
from PySide6.QtWidgets import QMainWindow, QToolBar, QMessageBox, QLabel

from src.core.signals import GlobalSignals
from src.core.theme_manager import ThemeManager, apply_native_titlebar_theme, strong_weight_css
from src.ui.components.document_search import DocumentSearchBarMixin
from src.ui.components.file_dialogs import save_file_name

logger = logging.getLogger(__name__)


class InternalPDFViewer(DocumentSearchBarMixin, QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Advanced PDF Viewer")
        self.resize(1100, 850)
        self.original_file_path = ""
        self.display_name = ""

        self.web_view = QWebEngineView(self)

        settings = self.web_view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PdfViewerEnabled, True)

        self.web_view.page().printRequested.connect(self._handle_print_request)
        self.web_view.page().profile().downloadRequested.connect(self._handle_download_request)

        # 绑定 PDF 搜索结果信号
        self.web_view.page().findTextFinished.connect(self._on_find_text_finished)

        self.setCentralWidget(self.web_view)

        # 强制接管快捷键，防止被 WebEngine 拦截
        self.shortcut_space = QShortcut(QKeySequence(Qt.Key_Space), self)
        self.shortcut_space.activated.connect(self._trigger_translation)

        self.shortcut_ctrl_f = QShortcut(QKeySequence("Ctrl+F"), self)
        self.shortcut_ctrl_f.activated.connect(self._toggle_search)

        self.shortcut_esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self.shortcut_esc.activated.connect(self._close_search)

        self._setup_toolbar()
        self._setup_search_bar()

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

    # ------------------------------------------------------------- 主题 ---
    def _apply_theme(self):
        tm = ThemeManager()

        self.setStyleSheet(f"QMainWindow {{ background-color: {tm.color('bg_main')}; }}")
        # 强制操作系统原生标题栏跟随深浅色模式
        apply_native_titlebar_theme(self, tm.current_theme == "dark")

        tb_style = f"""
            QToolBar {{ background: {tm.color('bg_card')}; padding: 6px; border: none; border-bottom: 1px solid {tm.color('border')}; }} 
            QToolButton, QPushButton {{ color: {tm.color('text_main')}; padding: 5px 10px; border-radius: 4px; font-weight: {strong_weight_css()}; font-family: {tm.font_family()}; background: transparent; border: none; }} 
            QToolButton:hover, QPushButton:hover {{ background: {tm.color('btn_hover')}; color: {tm.color('accent')}; }}
        """
        for tb in self.findChildren(QToolBar):
            tb.setStyleSheet(tb_style)

        self.search_input.setStyleSheet(
            f"background-color: {tm.color('bg_input')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 4px 8px;")

        if hasattr(self, 'lbl_search_count'):
            self.lbl_search_count.setStyleSheet(f"color: {tm.color('text_main')}; font-weight: {strong_weight_css()}; padding: 0 10px;")

        if hasattr(self, 'act_open_sys'):
            self.act_open_sys.setIcon(tm.icon("link", "text_main"))
            self.act_export.setIcon(tm.icon("download", "text_main"))
            self.act_search.setIcon(tm.icon("search", "text_main"))
            self.btn_find_prev.setIcon(tm.icon("chevron-left", "text_main"))
            self.btn_find_next.setIcon(tm.icon("chevron-right", "text_main"))
            self.btn_close_search.setIcon(tm.icon("close", "danger"))
            self.btn_do_search.setIcon(tm.icon("search", "text_main"))

    # ----------------------------------------------------------- 工具栏 ---
    def _setup_toolbar(self):
        tm = ThemeManager()
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, tb)

        self.act_search = tb.addAction(tm.icon("search", "text_main"), "Search (Ctrl+F)", self._toggle_search)
        tb.addSeparator()

        self.act_open_sys = tb.addAction(tm.icon("link", "text_main"), "Open in System", self.open_system_app)
        self.act_export = tb.addAction(tm.icon("download", "text_main"), "Export Full PDF", self.export_pdf)

        hint = QLabel("  (Tip: Select text and Press Space/Right-Click to Translate)")
        hint.setStyleSheet(f"color: {tm.color('text_muted')}; font-style: italic; font-size: 13px; padding-left: 10px;")
        tb.addWidget(hint)

    # --------------------------------------------------------- 文档加载 ---
    def load_document(self, file_path, page_num=0, highlight_text="", display_name=""):
        self.original_file_path = file_path
        self.display_name = display_name or os.path.basename(file_path)
        self.setWindowTitle(f"Advanced PDF Viewer - {self.display_name}")

        self.web_view.setUrl(QUrl.fromLocalFile(file_path))

        if highlight_text:
            self.search_input.setText(highlight_text)
            from PySide6.QtCore import QTimer
            QTimer.singleShot(500, lambda: self.web_view.findText(highlight_text))

        self.show()
        self.raise_()
        self.activateWindow()

    def _trigger_translation(self):
        """利用 Web 原生动作复制选中内容并调用翻译"""
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtCore import QTimer

        self.web_view.page().triggerAction(QWebEnginePage.WebAction.Copy)

        def emit_trans():
            new_text = QGuiApplication.clipboard().text()
            if new_text and hasattr(GlobalSignals(), 'sig_invoke_translator'):
                GlobalSignals().sig_invoke_translator.emit(new_text.strip())

        QTimer.singleShot(150, emit_trans)

    # ------------------------------------------------------------- 搜索 ---
    def _on_find_text_finished(self, result):
        """处理 WebEngine 的搜索结果返回，更新搜索计数器"""
        if result.numberOfMatches() > 0:
            self.lbl_search_count.setText(f" {result.activeMatch()} / {result.numberOfMatches()} ")
        else:
            self.lbl_search_count.setText(" 0 / 0 ")

    def _find_next(self):
        text = self.search_input.text()
        if not text:
            self.web_view.findText("")
            self.lbl_search_count.setText(" 0 / 0 ")
            return
        self.web_view.findText(text)

    def _find_prev(self):
        text = self.search_input.text()
        if not text:
            self.web_view.findText("")
            self.lbl_search_count.setText(" 0 / 0 ")
            return
        self.web_view.findText(text, QWebEnginePage.FindFlag.FindBackward)

    def _clear_search(self):
        """清除 WebEngine 的搜索高亮（关闭搜索栏时由 mixin 调用）。"""
        self.web_view.findText("")

    # --------------------------------------------------------- 外部工具 ---
    def open_system_app(self):
        if self.original_file_path and os.path.exists(self.original_file_path):
            try:
                import tempfile
                temp_dir = tempfile.gettempdir()
                safe_name = self.display_name if self.display_name else "document.pdf"
                if not safe_name.lower().endswith('.pdf'):
                    safe_name += ".pdf"

                temp_file_path = os.path.join(temp_dir, f"scholar_navis_sys_{safe_name}")
                shutil.copy2(self.original_file_path, temp_file_path)
                QDesktopServices.openUrl(QUrl.fromLocalFile(temp_file_path))
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to open with system app:\n{str(e)}")

    def _handle_print_request(self):
        printer = QPrinter(QPrinter.HighResolution)
        dialog = QPrintDialog(printer, self)
        if dialog.exec() == QPrintDialog.Accepted:
            self.web_view.page().print(printer, lambda success: None)

    def _handle_download_request(self, download_item):
        download_item.cancel()
        self.export_pdf()

    def export_pdf(self):
        if not self.original_file_path or not os.path.exists(self.original_file_path):
            return

        default_name = self.display_name
        if not default_name.lower().endswith('.pdf'):
            default_name += ".pdf"

        save_path, _ = save_file_name(
            self, "Export Original PDF", default_name, "PDF Files (*.pdf)"
        )

        if save_path:
            try:
                shutil.copy2(self.original_file_path, save_path)
                QMessageBox.information(self, "Success", f"Full PDF exported successfully to:\n{save_path}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to export PDF:\n{str(e)}")
