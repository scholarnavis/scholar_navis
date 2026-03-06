import html

import fitz
import os
import shutil
import re
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QScrollArea, QLabel, QMenu,
                               QMainWindow, QToolBar, QApplication, QFileDialog, QMessageBox,
                               QTextBrowser, QRubberBand)
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QTextCharFormat, QTextCursor, Qt, QDesktopServices
from PySide6.QtCore import Qt, QRect, QSize, QPoint, Signal, QUrl, QEvent

from src.core.signals import GlobalSignals
from src.core.theme_manager import ThemeManager


# 支持鼠标划词的交互式 Label
class InteractivePDFLabel(QLabel):
    sig_text_selected = Signal(str)
    sig_translate = Signal(str)
    sig_prev_page = Signal()
    sig_next_page = Signal()
    sig_close = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rubber_band = QRubberBand(QRubberBand.Rectangle, self)
        self.origin = QPoint()
        self.current_page = None
        self.mat = None
        self.selected_text = ""
        self.setAlignment(Qt.AlignTop | Qt.AlignLeft)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.origin = event.position().toPoint()
            self.rubber_band.setGeometry(QRect(self.origin, QSize()))
            self.rubber_band.show()
            self.selected_text = ""

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton:
            self.rubber_band.setGeometry(QRect(self.origin, event.position().toPoint()).normalized())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            rect = self.rubber_band.geometry()
            if rect.width() > 10 and rect.height() > 10 and self.current_page and self.mat:
                inv_mat = ~self.mat
                f_rect = fitz.Rect(rect.left(), rect.top(), rect.right(), rect.bottom())
                pdf_rect = f_rect * inv_mat
                text = self.current_page.get_text("text", clip=pdf_rect).strip()
                if text:
                    self.selected_text = text
                    self.sig_text_selected.emit(text)
            else:
                self.rubber_band.hide()

    def clear_selection(self):
        self.rubber_band.hide()
        self.selected_text = ""

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        tm = ThemeManager()
        menu.setStyleSheet(f"""
                    QMenu {{ background-color: {tm.color('bg_card')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 4px; }}
                    QMenu::item {{ padding: 6px 25px; border-radius: 2px; }}
                    QMenu::item:selected {{ background-color: {tm.color('accent')}; }}
                """)

        has_text = bool(self.selected_text)

        act_copy = act_trans = act_sel_all = act_close = act_prev = act_next = None

        if has_text:
            act_copy = menu.addAction(tm.icon("copy", "text_main"), "Copy")
            act_trans = menu.addAction(tm.icon("language", "text_main"), "Translate")
            act_sel_all = menu.addAction(tm.icon("menu", "text_main"), "Select All")  # 修复了拼写错误
            act_close = menu.addAction(tm.icon("close", "danger"), "Close")
        else:
            act_prev = menu.addAction(tm.icon("chevron-left", "text_main"), "Previous")
            act_next = menu.addAction(tm.icon("chevron-right", "text_main"), "Next")
            act_sel_all = menu.addAction(tm.icon("menu", "text_main"), "Select All")
            act_close = menu.addAction(tm.icon("close", "danger"), "Close")

        action = menu.exec(event.globalPos())

        if action:
            if action == act_copy:
                from PySide6.QtGui import QGuiApplication
                QGuiApplication.clipboard().setText(self.selected_text)
                self.clear_selection()
            elif action == act_trans:
                self.sig_translate.emit(self.selected_text)
                self.clear_selection()
            elif action == act_prev:
                self.sig_prev_page.emit()
            elif action == act_next:
                self.sig_next_page.emit()
            elif action == act_sel_all:
                if self.current_page:
                    self.selected_text = self.current_page.get_text("text").strip()
                    self.rubber_band.setGeometry(self.rect())
                    self.rubber_band.show()
            elif action == act_close:
                self.sig_close.emit()


class InternalPDFViewer(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Advanced PDF Viewer")
        self.resize(1100, 850)
        self.doc = None
        self.current_page = 0
        self.highlight_text = ""
        self.display_name = ""
        self.original_file_path = ""
        self.zoom_factor = 2.0  # 默认高清渲染倍率

        # UI Layout
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setAlignment(Qt.AlignCenter)
        self.scroll_area.setStyleSheet("background-color: #525659;")

        self.lbl_page = InteractivePDFLabel()
        self.scroll_area.setWidget(self.lbl_page)
        self.setCentralWidget(self.scroll_area)

        self._setup_toolbar()

        # 连接 Label 传出的右键菜单信号
        self.lbl_page.sig_prev_page.connect(self.prev_page)
        self.lbl_page.sig_next_page.connect(self.next_page)
        self.lbl_page.sig_close.connect(self.close)
        self.lbl_page.sig_translate.connect(
            lambda text: GlobalSignals().sig_invoke_translator.emit(text) if hasattr(GlobalSignals(),
                                                                                     'sig_invoke_translator') else None)
        # 拦截滚动条事件，实现滚轮翻页
        self.scroll_area.verticalScrollBar().installEventFilter(self)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

    def _apply_theme(self):
        tm = ThemeManager()

        self.scroll_area.setStyleSheet(f"background-color: {tm.color('bg_main')};")

        tb_style = f"""
            QToolBar {{ background: {tm.color('bg_card')}; padding: 6px; border: none; border-bottom: 1px solid {tm.color('border')}; }} 
            QToolButton {{ color: {tm.color('text_main')}; padding: 5px 10px; border-radius: 4px; font-weight: bold; }} 
            QToolButton:hover {{ background: {tm.color('btn_hover')}; color: {tm.color('accent')}; }}
        """
        for tb in self.findChildren(QToolBar):
            tb.setStyleSheet(tb_style)

        for lbl in self.findChildren(QLabel):
            if "(Tip:" in lbl.text():
                lbl.setStyleSheet(
                    f"color: {tm.color('text_muted')}; font-style: italic; font-size: 13px; padding-left: 10px;")


    def _setup_toolbar(self):
        tm = ThemeManager()
        tb1 = QToolBar()
        tb1.setMovable(False)
        tb1.setStyleSheet(
            "QToolBar { background: #333; padding: 6px; border: none; } QToolButton { color: white; padding: 5px 10px; border-radius: 4px; font-weight: bold; } QToolButton:hover { background: #444; color: #05B8CC; }")
        self.addToolBar(Qt.TopToolBarArea, tb1)

        tb1.addAction(tm.icon("chevron-left", "text_main"), "Prev", self.prev_page)
        tb1.addAction(tm.icon("chevron-right", "text_main"), "Next", self.next_page)
        tb1.addSeparator()
        tb1.addAction(tm.icon("add", "text_main"), "Zoom In", self.zoom_in)
        tb1.addAction(tm.icon("remove", "text_main"), "Zoom Out", self.zoom_out)
        tb1.addAction(tm.icon("menu", "text_main"), "Fit Width", self.fit_width)
        self.addToolBarBreak(Qt.TopToolBarArea)

        tb2 = QToolBar()
        tb2.setMovable(False)
        tb2.setStyleSheet(
            "QToolBar { background: #333; border-bottom: 1px solid #555; padding: 6px; } QToolButton { color: white; padding: 5px 10px; border-radius: 4px; font-weight: bold; } QToolButton:hover { background: #444; color: #05B8CC; }")
        self.addToolBar(Qt.TopToolBarArea, tb2)


        tb2.addAction(tm.icon("link", "text_main"), "Open in System", self.open_system_app)
        tb2.addAction(tm.icon("download", "text_main"), "Export Full PDF", self.export_pdf)

        hint = QLabel("  (Tip: Select text and press Space or Right-Click to Translate)")
        hint.setStyleSheet("color: #aaa; font-style: italic; font-size: 13px; padding-left: 10px;")
        tb2.addWidget(hint)


    def load_document(self, file_path, page_num=0, highlight_text="", display_name=""):
        try:
            if self.doc: self.doc.close()
            self.original_file_path = file_path
            self.doc = fitz.open(file_path)
            self.highlight_text = highlight_text
            self.display_name = display_name or os.path.basename(file_path)

            self.show()
            QApplication.processEvents()
            self.fit_width()

            self.goto_page(page_num)
            self.raise_()
            self.activateWindow()
        except Exception as e:
            print(f"Error opening PDF: {e}")

    def eventFilter(self, obj, event):
        if obj == self.scroll_area.verticalScrollBar() and event.type() == QEvent.Wheel:
            bar = self.scroll_area.verticalScrollBar()
            delta = event.angleDelta().y()

            if delta < 0 and bar.value() >= bar.maximum():
                if self.doc and self.current_page < len(self.doc) - 1:
                    self.next_page()
                    bar.setValue(0)
                    return True
            elif delta > 0 and bar.value() <= bar.minimum():
                if self.doc and self.current_page > 0:
                    self.prev_page()
                    QApplication.processEvents()
                    bar.setValue(bar.maximum())
                    return True
        return super().eventFilter(obj, event)

    # --- 缩放与自适应功能 ---
    def zoom_in(self):
        self.zoom_factor = min(5.0, self.zoom_factor * 1.25)
        self.render_page()

    def zoom_out(self):
        self.zoom_factor = max(0.5, self.zoom_factor / 1.25)
        self.render_page()

    def fit_width(self):
        if not self.doc: return
        page = self.doc.load_page(self.current_page)
        # 减去滚动条余量防止出现横向滚动条
        view_w = self.scroll_area.viewport().width() - 25
        self.zoom_factor = view_w / page.rect.width
        if not self.doc.is_closed: self.render_page()



    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space and self.lbl_page.selected_text:
            self._invoke_translator()
        else:
            super().keyPressEvent(event)

    def _invoke_translator(self):
        if hasattr(GlobalSignals(), 'sig_invoke_translator'):
            GlobalSignals().sig_invoke_translator.emit(self.lbl_page.selected_text)
            self.lbl_page.clear_selection()

    # --- 外部应用打开与导出 ---
    def open_system_app(self):
        if self.original_file_path and os.path.exists(self.original_file_path):
            try:
                # 兼容由于 RAG 切片重命名引发的无后缀问题，可以复制到 Temp 目录赋予真名打开
                import tempfile
                temp_dir = tempfile.gettempdir()
                safe_name = self.display_name if self.display_name else "document.pdf"
                if not safe_name.lower().endswith('.pdf'): safe_name += ".pdf"

                temp_file_path = os.path.join(temp_dir, f"scholar_navis_sys_{safe_name}")
                shutil.copy2(self.original_file_path, temp_file_path)
                QDesktopServices.openUrl(QUrl.fromLocalFile(temp_file_path))
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to open with system app:\n{str(e)}")

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

    # --- 翻页与渲染 ---
    def goto_page(self, page_num):
        if not self.doc: return

        target_page = max(0, min(page_num, len(self.doc) - 1))

        if target_page == self.current_page and self.lbl_page.pixmap() is not None:
            return

        self.current_page = target_page
        self.lbl_page.clear_selection()
        self.render_page()
        self.update_title()

    def prev_page(self):
        self.goto_page(self.current_page - 1)

    def next_page(self):
        self.goto_page(self.current_page + 1)

    def update_title(self):
        if self.doc:
            self.setWindowTitle(f"Page {self.current_page + 1} / {len(self.doc)} - {self.display_name}")

    def _find_text_quads(self, page, text):
        clean_text = re.sub(r'[*_#`>]', '', text)

        clean_text = re.sub(r'\s+', ' ', clean_text).strip()

        if not clean_text:
            return []

        quads = page.search_for(clean_text)
        if quads: return quads

        if len(clean_text) > 30:
            chunks = [c.strip() for c in re.split(r'[,.，。;\n]', clean_text) if len(c.strip()) > 10]
            all_quads = []
            for chunk in chunks:
                q = page.search_for(chunk)
                if q: all_quads.extend(q)
            if all_quads: return all_quads

            return page.search_for(clean_text[:30])

        return []

    def render_page(self):
        if not self.doc: return
        page = self.doc.load_page(self.current_page)
        self.lbl_page.current_page = page

        mat = fitz.Matrix(self.zoom_factor, self.zoom_factor)
        self.lbl_page.mat = mat
        pix = page.get_pixmap(matrix=mat, alpha=False)

        img_data = pix.tobytes("ppm")
        qt_img = QImage()
        qt_img.loadFromData(img_data)

        final_pixmap = QPixmap.fromImage(qt_img)

        target_y = None
        if self.highlight_text:
            quads = self._find_text_quads(page, self.highlight_text)
            if quads:
                painter = QPainter(final_pixmap)
                painter.setPen(Qt.NoPen)
                hl_color = QColor(ThemeManager().color("warning"))
                hl_color.setAlpha(120)
                painter.setBrush(hl_color)

                for quad in quads:
                    rect = quad * mat
                    painter.drawRect(rect.x0, rect.y0, rect.width, rect.height)
                painter.end()

                first_rect = quads[0] * mat
                target_y = first_rect.y0 + first_rect.height / 2

        self.lbl_page.setPixmap(final_pixmap)

        if target_y is not None:
            QApplication.processEvents()
            scroll_val = int(target_y - self.scroll_area.height() / 2)
            self.scroll_area.verticalScrollBar().setValue(max(0, scroll_val))


#  全新的文本阅读器（支持 TXT, MD 等）
class InternalTextViewer(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Text Document Viewer")
        self.resize(1000, 800)

        self.original_file_path = ""
        self.display_name = ""

        # 使用 QTextBrowser 显示纯文本
        self.text_browser = QTextBrowser()
        self.text_browser.setOpenExternalLinks(True)
        self.text_browser.setStyleSheet("""
            QTextBrowser {
                background-color: #1e1e1e;
                color: #d4d4d4;
                font-size: 14px;
                font-family: 'Consolas', 'Microsoft YaHei', monospace;
                padding: 20px;
                border: none;
                selection-background-color: #05B8CC;
                selection-color: white;
            }
        """)
        self.setCentralWidget(self.text_browser)

        # 2. 安装事件过滤器，捕获空格键快捷翻译
        self.text_browser.installEventFilter(self)

        self._setup_toolbar()

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

    def _apply_theme(self):
        tm = ThemeManager()
        self.text_browser.setStyleSheet(f"""
            QTextBrowser {{
                background-color: {tm.color('bg_main')};
                color: {tm.color('text_main')};
                font-size: 14px;
                font-family: 'Consolas', 'Microsoft YaHei', monospace;
                padding: 20px;
                border: none;
                selection-background-color: {tm.color('accent')};
                selection-color: {tm.color('bg_card')};
            }}
        """)

        # 同样更新 ToolBar 样式
        tb_style = f"""
            QToolBar {{ background: {tm.color('bg_card')}; border-bottom: 1px solid {tm.color('border')}; padding: 6px; }} 
            QToolButton {{ color: {tm.color('text_main')}; padding: 5px 10px; border-radius: 4px; font-weight: bold; }} 
            QToolButton:hover {{ background: {tm.color('btn_hover')}; color: {tm.color('accent')}; }}
        """
        for tb in self.findChildren(QToolBar):
            tb.setStyleSheet(tb_style)


    def _setup_toolbar(self):
        tm = ThemeManager()

        tb1 = QToolBar()
        tb1.setMovable(False)
        tb1.setStyleSheet(
            "QToolBar { background: #333; padding: 6px; border: none; } QToolButton { color: white; padding: 5px 10px; border-radius: 4px; font-weight: bold; } QToolButton:hover { background: #444; color: #05B8CC; }")
        self.addToolBar(Qt.TopToolBarArea, tb1)

        tb1.addAction(tm.icon("add", "text_main"), "Zoom In", self.zoom_in)
        tb1.addAction(tm.icon("remove", "text_main"), "Zoom Out", self.zoom_out)

        self.addToolBarBreak(Qt.TopToolBarArea)

        tb2 = QToolBar()
        tb2.setMovable(False)
        tb2.setStyleSheet(
            "QToolBar { background: #333; border-bottom: 1px solid #555; padding: 6px; } QToolButton { color: white; padding: 5px 10px; border-radius: 4px; font-weight: bold; } QToolButton:hover { background: #444; color: #05B8CC; }")
        self.addToolBar(Qt.TopToolBarArea, tb2)

        tb2.addAction(tm.icon("link", "text_main"), "Open in System", self.open_system_app)
        tb2.addAction(tm.icon("download", "text_main"), "Export Full PDF", self.export_pdf)

        hint = QLabel("  (Tip: Select text and press Space or Right-Click to Translate)")
        hint.setStyleSheet("color: #aaa; font-style: italic; font-size: 13px; padding-left: 10px;")
        tb2.addWidget(hint)



    # --- 呼出全局翻译 ---
    def _invoke_translator(self, text):
        if hasattr(GlobalSignals(), 'sig_invoke_translator'):
            GlobalSignals().sig_invoke_translator.emit(text)
            # 翻译后自动取消选中状态，保持 UI 干净
            cursor = self.text_browser.textCursor()
            cursor.clearSelection()
            self.text_browser.setTextCursor(cursor)

    # --- 缩放控制 ---
    def zoom_in(self):
        self.text_browser.zoomIn(2)

    def zoom_out(self):
        self.text_browser.zoomOut(2)

    def eventFilter(self, obj, event):
        # 监听空格键，实现快捷划词翻译
        if obj == self.text_browser and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Space:
                selected_text = self.text_browser.textCursor().selectedText()
                if selected_text:
                    self._invoke_translator(selected_text)
                    return True  # 拦截事件，防止文本框向下滚动
        return super().eventFilter(obj, event)

    def load_document(self, file_path, highlight_text="", display_name=""):
        self.original_file_path = file_path
        self.display_name = display_name or os.path.basename(file_path)
        self.setWindowTitle(f"Text Viewer - {self.display_name}")

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()


            pattern = re.compile(
                r'('
                r'\[[^\]]+\]\([^)]+\)|'  # 1. 最优先：保护已有的 Markdown 链接
                r'<a\s+[^>]*>.*?</a>|'  # 2. 其次：保护已有的 HTML <a> 标签
                r'https?://[^\s<]+[^\s<.,;?)\]]|'  # 3. 处理纯 HTTP/HTTPS
                r'10\.\d{4,9}/[-._;()/:A-Za-z0-9]+[A-Za-z0-9/]'  # 4. 兜底处理独立的 DOI
                r')',
                flags=re.IGNORECASE
            )

            parts = pattern.split(content)
            html_chunks = []

            for i, part in enumerate(parts):
                if not part:
                    continue

                # 偶数索引代表“未匹配到的普通文本”
                if i % 2 == 0:
                    html_chunks.append(html.escape(part).replace('\n', '<br>'))
                # 奇数索引代表“被上面正则命中抽离的特殊目标”
                else:
                    if part.startswith('['):
                        # 是 Markdown 链接，将其解析为安全的 HTML a 标签
                        match_md = re.match(r'\[([^\]]+)\]\(([^)]+)\)', part)
                        if match_md:
                            t = html.escape(match_md.group(1))
                            u = html.escape(match_md.group(2))
                            html_chunks.append(f'<a href="{u}" style="color: #05B8CC; text-decoration: none;">{t}</a>')
                        else:
                            html_chunks.append(html.escape(part))

                    elif part.lower().startswith('<a '):
                        # 原生 HTML a 标签，信任内容，直接放行
                        html_chunks.append(part)

                    elif part.lower().startswith('http'):
                        u = html.escape(part)
                        html_chunks.append(f'<a href="{u}" style="color: #05B8CC; text-decoration: none;">{u}</a>')

                    elif part.startswith('10.'):
                        doi = html.escape(part)
                        html_chunks.append(
                            f'<a href="https://doi.org/{doi}" style="color: #05B8CC; text-decoration: none;">{doi}</a>')

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
            print(f"Error opening text file: {e}")

    def _highlight_and_scroll(self, text):
        if not text:
            return

        document = self.text_browser.document()

        clean_text = re.sub(r'\s+', ' ', text).strip()

        cursor = document.find(clean_text)

        if cursor.isNull():
            chunks = [c.strip() for c in re.split(r'[,.，。;\n]', clean_text) if len(c.strip()) > 15]
            for chunk in chunks:
                cursor = document.find(chunk)
                if not cursor.isNull():
                    break

        if cursor.isNull() and len(clean_text) > 25:
            cursor = document.find(clean_text[:25])

        if not cursor.isNull():
            from PySide6.QtGui import QTextCharFormat, QColor

            fmt = QTextCharFormat()
            fmt.setBackground(QColor(255, 235, 59, 120))
            cursor.mergeCharFormat(fmt)

            self.text_browser.setTextCursor(cursor)
            self.text_browser.ensureCursorVisible()

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
        save_path, _ = QFileDialog.getSaveFileName(
            self, "Export Original File", self.display_name, "All Files (*.*)"
        )
        if save_path:
            try:
                shutil.copy2(self.original_file_path, save_path)
                QMessageBox.information(self, "Success", f"File exported successfully to:\n{save_path}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to export file:\n{str(e)}")



