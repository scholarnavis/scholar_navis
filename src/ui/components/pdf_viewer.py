# ====== 文件：pdf_viewer.py ======

import html
import os
import re
import shutil

from PySide6.QtPrintSupport import QPrinter, QPrintDialog
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtCore import Qt, QUrl, QEvent, QPoint
from PySide6.QtGui import QColor, QDesktopServices, QFont, QKeyEvent, QShortcut, QKeySequence, QTextCursor, \
    QTextDocument
from PySide6.QtWidgets import (QMainWindow, QToolBar, QApplication, QFileDialog, QMessageBox,
                               QTextBrowser, QWidget, QHBoxLayout, QLineEdit, QPushButton, QLabel, QMenu)

from src.core.signals import GlobalSignals
from src.core.theme_manager import ThemeManager


def _apply_windows_dark_titlebar(window, tm):
    """底层 Hack：强制将 Windows 操作系统原生标题栏适配深色/浅色模式"""
    import sys
    if sys.platform == "win32":
        try:
            import ctypes
            import platform
            bg = tm.color('bg_main')
            is_dark = False
            # 通过主背景色的亮度来判定是否为深色模式
            if bg and bg.startswith('#') and len(bg) >= 7:
                r, g, b = int(bg[1:3], 16), int(bg[3:5], 16), int(bg[5:7], 16)
                is_dark = (0.299 * r + 0.587 * g + 0.114 * b) < 128

            hwnd = int(window.winId())
            build = int(platform.version().split('.')[2])
            # Windows 11 及部分 Windows 10 使用 20，较老版本使用 19
            attr = 20 if build >= 22000 else 19
            val = ctypes.c_int(1 if is_dark else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass


class InternalPDFViewer(QMainWindow):
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

    def _apply_theme(self):
        tm = ThemeManager()

        self.setStyleSheet(f"QMainWindow {{ background-color: {tm.color('bg_main')}; }}")
        _apply_windows_dark_titlebar(self, tm)

        tb_style = f"""
            QToolBar {{ background: {tm.color('bg_card')}; padding: 6px; border: none; border-bottom: 1px solid {tm.color('border')}; }} 
            QToolButton, QPushButton {{ color: {tm.color('text_main')}; padding: 5px 10px; border-radius: 4px; font-weight: bold; font-family: {tm.font_family()}; background: transparent; border: none; }} 
            QToolButton:hover, QPushButton:hover {{ background: {tm.color('btn_hover')}; color: {tm.color('accent')}; }}
        """
        for tb in self.findChildren(QToolBar):
            tb.setStyleSheet(tb_style)

        self.search_input.setStyleSheet(
            f"background-color: {tm.color('bg_input')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 4px 8px;")

        if hasattr(self, 'lbl_search_count'):
            self.lbl_search_count.setStyleSheet(f"color: {tm.color('text_main')}; font-weight: bold; padding: 0 10px;")

        if hasattr(self, 'act_open_sys'):
            self.act_open_sys.setIcon(tm.icon("link", "text_main"))
            self.act_export.setIcon(tm.icon("download", "text_main"))
            self.act_search.setIcon(tm.icon("search", "text_main"))
            self.btn_find_prev.setIcon(tm.icon("chevron-left", "text_main"))
            self.btn_find_next.setIcon(tm.icon("chevron-right", "text_main"))
            self.btn_close_search.setIcon(tm.icon("close", "danger"))
            self.btn_do_search.setIcon(tm.icon("search", "text_main"))

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

    def _setup_search_bar(self):
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
        self.lbl_search_count.setStyleSheet(f"color: {tm.color('text_main')}; font-weight: bold; padding: 0 10px;")

        self.btn_find_prev = QPushButton(" Prev")
        self.btn_find_prev.clicked.connect(self._find_prev)

        self.btn_find_next = QPushButton(" Next")
        self.btn_find_next.clicked.connect(self._find_next)

        self.btn_close_search = QPushButton(" Close")
        self.btn_close_search.clicked.connect(self._close_search)

        self.search_toolbar.addWidget(self.search_input)
        self.search_toolbar.addWidget(self.btn_do_search)
        self.search_toolbar.addWidget(self.lbl_search_count)
        self.search_toolbar.addWidget(self.btn_find_prev)
        self.search_toolbar.addWidget(self.btn_find_next)
        self.search_toolbar.addWidget(self.btn_close_search)

        self.addToolBar(Qt.TopToolBarArea, self.search_toolbar)
        self.search_toolbar.hide()

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

    def _toggle_search(self):
        if self.search_toolbar.isVisible():
            self._close_search()
        else:
            self.search_toolbar.show()
            self.search_input.setFocus()
            self.search_input.selectAll()

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

    def _close_search(self):
        self.search_toolbar.hide()
        self.web_view.findText("")
        self.lbl_search_count.setText(" 0 / 0 ")

    def open_system_app(self):
        if self.original_file_path and os.path.exists(self.original_file_path):
            try:
                import tempfile
                temp_dir = tempfile.gettempdir()
                safe_name = self.display_name if self.display_name else "document.pdf"
                if not safe_name.lower().endswith('.pdf'): safe_name += ".pdf"

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

        save_path, _ = QFileDialog.getSaveFileName(
            self, "Export Original PDF", default_name, "PDF Files (*.pdf)"
        )

        if save_path:
            try:
                shutil.copy2(self.original_file_path, save_path)
                QMessageBox.information(self, "Success", f"Full PDF exported successfully to:\n{save_path}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to export PDF:\n{str(e)}")


class InternalTextViewer(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Text Document Viewer")
        self.resize(1000, 800)

        self.original_file_path = ""
        self.display_name = ""

        self.text_browser = QTextBrowser(self)
        self.text_browser.setOpenExternalLinks(True)
        base_font = self.text_browser.font()
        base_font.setPointSize(13)
        self.text_browser.setFont(base_font)

        self.setCentralWidget(self.text_browser)
        self.text_browser.installEventFilter(self)

        # 使用 QShortcut 强制注册快捷键，避免 QTextBrowser 吞噬键盘事件
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

    def _apply_theme(self):
        tm = ThemeManager()

        # 为主窗口设置背景色
        self.setStyleSheet(f"QMainWindow {{ background-color: {tm.color('bg_main')}; }}")
        # 核心修复：强制操作系统标题栏跟随深浅色模式
        _apply_windows_dark_titlebar(self, tm)

        # 1. 文本区域样式
        self.text_browser.setStyleSheet(f"""
            QTextBrowser {{
                background-color: {tm.color('bg_main')};
                color: {tm.color('text_main')};
                border: none;
                selection-background-color: {tm.color('accent')};
                selection-color: {tm.color('selection_fg')};
            }}
        """)

        # 2. 统一所有工具栏样式
        common_tb_style = f"""
            QToolBar {{ 
                background: {tm.color('bg_card')}; 
                border-bottom: 1px solid {tm.color('border')}; 
                padding: 6px; 
            }} 
            QToolButton, QPushButton {{ 
                color: {tm.color('text_main')}; 
                padding: 5px 10px; 
                border-radius: 4px; 
                font-weight: bold;
                background: transparent; 
                border: none;
            }} 
            QToolButton:hover, QPushButton:hover {{ 
                background: {tm.color('btn_hover')}; 
                color: {tm.color('accent')}; 
            }}
        """
        for tb in self.findChildren(QToolBar):
            tb.setStyleSheet(common_tb_style)

        # 3. 搜索输入框样式
        self.search_input.setStyleSheet(f"""
            background-color: {tm.color('bg_input')}; 
            color: {tm.color('text_main')}; 
            border: 1px solid {tm.color('border')}; 
            border-radius: 4px; 
            padding: 4px 8px;
        """)

        # 4. 图标更新与文本颜色更新 (支持深色/浅色动态切换)
        if hasattr(self, 'lbl_search_count'):
            self.lbl_search_count.setStyleSheet(f"color: {tm.color('text_main')}; font-weight: bold; padding: 0 10px;")

        if hasattr(self, 'act_zoom_in'):
            self.act_zoom_in.setIcon(tm.icon("add", "text_main"))
            self.act_zoom_out.setIcon(tm.icon("remove", "text_main"))
            self.act_open_sys.setIcon(tm.icon("link", "text_main"))
            self.act_export.setIcon(tm.icon("download", "text_main"))

        if hasattr(self, 'btn_find_prev'):
            self.btn_do_search.setIcon(tm.icon("search", "text_main"))
            self.btn_find_prev.setIcon(tm.icon("chevron-left", "text_main"))
            self.btn_find_next.setIcon(tm.icon("chevron-right", "text_main"))
            self.btn_close_search.setIcon(tm.icon("close", "danger"))

    def _setup_toolbar(self):
        tm = ThemeManager()

        tb1 = QToolBar()
        tb1.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, tb1)

        self.act_zoom_in = tb1.addAction(tm.icon("add", "text_main"), "Zoom In", self.zoom_in)
        self.act_zoom_out = tb1.addAction(tm.icon("remove", "text_main"), "Zoom Out", self.zoom_out)

        self.addToolBarBreak(Qt.TopToolBarArea)

        tb2 = QToolBar()
        tb2.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, tb2)

        self.act_open_sys = tb2.addAction(tm.icon("link", "text_main"), "Open in System", self.open_system_app)
        self.act_export = tb2.addAction(tm.icon("download", "text_main"), "Export File", self.export_file)

        hint = QLabel("  (Tip: Select text and Press Space to Translate)")
        hint.setStyleSheet(f"color: {tm.color('text_muted')}; font-style: italic; font-size: 13px; padding-left: 10px;")
        tb2.addWidget(hint)

    def zoom_in(self):
        self.text_browser.zoomIn(2)

    def zoom_out(self):
        self.text_browser.zoomOut(2)

    def eventFilter(self, obj, event):
        if obj == self.text_browser:
            if event.type() == QEvent.Wheel and event.modifiers() == Qt.ControlModifier:
                delta = event.angleDelta().y()
                if delta > 0:
                    self.text_browser.zoomIn(1)
                else:
                    self.text_browser.zoomOut(1)
                return True
        return super().eventFilter(obj, event)

    def load_document(self, file_path, highlight_text="", display_name=""):
        self.original_file_path = file_path
        self.display_name = display_name or os.path.basename(file_path)
        self.setWindowTitle(f"Text Viewer - {self.display_name}")

        try:
            content = ""
            ext = file_path.lower()

            if ext.endswith('.docx'):
                try:
                    import docx
                    doc = docx.Document(file_path)
                    content = "\n".join([p.text for p in doc.paragraphs])
                except Exception as e:
                    content = f"Error reading DOCX:\n{str(e)}"
            else:
                try:
                    import chardet
                    with open(file_path, 'rb') as f:
                        raw_data = f.read()
                        detected = chardet.detect(raw_data)
                        encoding = detected['encoding'] if detected['encoding'] else 'utf-8'
                        content = raw_data.decode(encoding, errors='replace')
                except Exception as e:
                    content = f"Error reading text file:\n{str(e)}"

            tm = ThemeManager()
            accent_color = tm.color('accent')

            pattern = re.compile(
                r'('
                r'\[[^\]]+\]\([^)]+\)|'
                r'<a\s+[^>]*>.*?</a>|'
                r'https?://[^\s<]+[^\s<.,;?)\]]|'
                r'10\.\d{4,9}/[-._;()/:A-Za-z0-9]+[A-Za-z0-9/]'
                r')',
                flags=re.IGNORECASE
            )

            parts = pattern.split(content)
            html_chunks = []

            for i, part in enumerate(parts):
                if not part: continue
                if i % 2 == 0:
                    html_chunks.append(html.escape(part).replace('\n', '<br>'))
                else:
                    if part.startswith('['):
                        match_md = re.match(r'\[([^\]]+)\]\(([^)]+)\)', part)
                        if match_md:
                            t = html.escape(match_md.group(1))
                            u = html.escape(match_md.group(2))
                            html_chunks.append(
                                f'<a href="{u}" style="color: {accent_color}; text-decoration: none;">{t}</a>')
                        else:
                            html_chunks.append(html.escape(part))
                    elif part.lower().startswith('<a '):
                        html_chunks.append(part)
                    elif part.lower().startswith('http'):
                        u = html.escape(part)
                        html_chunks.append(
                            f'<a href="{u}" style="color: {accent_color}; text-decoration: none;">{u}</a>')
                    elif part.startswith('10.'):
                        doi = html.escape(part)
                        html_chunks.append(
                            f'<a href="https://doi.org/{doi}" style="color: {accent_color}; text-decoration: none;">{doi}</a>')
                    else:
                        html_chunks.append(html.escape(part))

            html_content = "".join(html_chunks)
            self.text_browser.setHtml(html_content)

            if highlight_text:
                self._highlight_and_scroll(highlight_text)

            self.show()
            self.raise_()
            self.activateWindow()
        except Exception as e:
            print(f"Error opening text document: {e}")

    def _highlight_and_scroll(self, text):
        if not text: return
        document = self.text_browser.document()
        clean_text = re.sub(r'\s+', ' ', text).strip()
        cursor = document.find(clean_text)

        if cursor.isNull():
            chunks = [c.strip() for c in re.split(r'[,.，。;\n]', clean_text) if len(c.strip()) > 15]
            for chunk in chunks:
                cursor = document.find(chunk)
                if not cursor.isNull(): break

        if cursor.isNull() and len(clean_text) > 25:
            cursor = document.find(clean_text[:25])

        if not cursor.isNull():
            from PySide6.QtGui import QTextCharFormat, QColor
            fmt = QTextCharFormat()
            hl_color = QColor(ThemeManager().color('warning'))
            hl_color.setAlpha(120)
            fmt.setBackground(hl_color)
            cursor.mergeCharFormat(fmt)

            self.text_browser.setTextCursor(cursor)
            self.text_browser.ensureCursorVisible()

    def _trigger_translation(self):
        """获取选中文本并触发翻译信号"""
        cursor = self.text_browser.textCursor()
        selected_text = cursor.selectedText().strip()
        if selected_text and hasattr(GlobalSignals(), 'sig_invoke_translator'):
            clean_text = selected_text.replace('\u2029', '\n')
            GlobalSignals().sig_invoke_translator.emit(clean_text)

    def _setup_search_bar(self):
        """完全参照 PDF Viewer 初始化的文本搜索工具栏"""
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
        self.lbl_search_count.setStyleSheet(f"color: {tm.color('text_main')}; font-weight: bold; padding: 0 10px;")

        self.btn_find_prev = QPushButton(" Prev")
        self.btn_find_prev.clicked.connect(self._find_prev)

        self.btn_find_next = QPushButton(" Next")
        self.btn_find_next.clicked.connect(self._find_next)

        self.btn_close_search = QPushButton(" Close")
        self.btn_close_search.clicked.connect(self._close_search)

        self.search_toolbar.addWidget(self.search_input)
        self.search_toolbar.addWidget(self.btn_do_search)
        self.search_toolbar.addWidget(self.lbl_search_count)
        self.search_toolbar.addWidget(self.btn_find_prev)
        self.search_toolbar.addWidget(self.btn_find_next)
        self.search_toolbar.addWidget(self.btn_close_search)

        self.addToolBar(Qt.TopToolBarArea, self.search_toolbar)
        self.search_toolbar.hide()

    def _toggle_search(self):
        if self.search_toolbar.isVisible():
            self._close_search()
        else:
            self.search_toolbar.show()
            self.search_input.setFocus()
            self.search_input.selectAll()

    def _do_search(self, forward=True):
        """通用搜索逻辑：带循环查找和精确计数展示"""
        text = self.search_input.text()
        if not text:
            self.lbl_search_count.setText(" 0 / 0 ")
            cursor = self.text_browser.textCursor()
            cursor.clearSelection()
            self.text_browser.setTextCursor(cursor)
            return

        flags = QTextDocument.FindFlag(0) if forward else QTextDocument.FindBackward
        found = self.text_browser.find(text, flags)

        if not found:
            # 没找到则从头或从尾巴循环跳转
            self.text_browser.moveCursor(QTextCursor.Start if forward else QTextCursor.End)
            found = self.text_browser.find(text, flags)

        if found:
            # 巧妙计算匹配项数量
            plain_text = self.text_browser.toPlainText()
            lower_text = plain_text.lower()
            target = text.lower()
            total = lower_text.count(target)

            # 统计当前选中的是第几个结果
            pos = self.text_browser.textCursor().position()
            substring = lower_text[:pos]
            current = substring.count(target)

            self.lbl_search_count.setText(f" {current} / {total} ")
        else:
            self.lbl_search_count.setText(" 0 / 0 ")

    def _find_next(self):
        self._do_search(forward=True)

    def _find_prev(self):
        self._do_search(forward=False)

    def _close_search(self):
        self.search_toolbar.hide()
        cursor = self.text_browser.textCursor()
        cursor.clearSelection()
        self.text_browser.setTextCursor(cursor)
        self.lbl_search_count.setText(" 0 / 0 ")

    def open_system_app(self):
        if self.original_file_path and os.path.exists(self.original_file_path):
            try:
                import tempfile
                temp_dir = tempfile.gettempdir()
                safe_name = self.display_name if self.display_name else "document.txt"
                temp_file_path = os.path.join(temp_dir, f"scholar_navis_sys_{safe_name}")
                shutil.copy2(self.original_file_path, temp_file_path)
                QDesktopServices.openUrl(QUrl.fromLocalFile(temp_file_path))
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to open with system app:\n{str(e)}")

    def export_file(self):
        if not self.original_file_path or not os.path.exists(self.original_file_path):
            return
        save_path, _ = QFileDialog.getSaveFileName(self, "Export Original File", self.display_name, "All Files (*.*)")
        if save_path:
            try:
                shutil.copy2(self.original_file_path, save_path)
                QMessageBox.information(self, "Success", f"File exported successfully to:\n{save_path}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to export file:\n{str(e)}")