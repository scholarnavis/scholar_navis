import fitz  # PyMuPDF
import os
import shutil
import re
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QScrollArea, QLabel,
                               QMainWindow, QToolBar, QApplication, QFileDialog, QMessageBox, QTextBrowser)
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QTextCharFormat, QTextCursor
from PySide6.QtCore import Qt


class InternalPDFViewer(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Document Viewer")
        self.resize(1000, 800)
        self.doc = None
        self.current_page = 0
        self.highlight_text = ""
        self.display_name = ""
        self.original_file_path = ""

        # UI Layout
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setAlignment(Qt.AlignCenter)
        self.scroll_area.setStyleSheet("background-color: #525659;")

        self.lbl_page = QLabel()
        self.lbl_page.setAlignment(Qt.AlignCenter)
        self.scroll_area.setWidget(self.lbl_page)
        self.setCentralWidget(self.scroll_area)

        # Toolbar
        toolbar = QToolBar()
        toolbar.setMovable(False)
        toolbar.setStyleSheet(
            "QToolBar { background: #333; border-bottom: 1px solid #555; } QToolButton { color: white; }")
        self.addToolBar(toolbar)

        self.btn_prev = toolbar.addAction("◀ Prev", self.prev_page)
        self.btn_next = toolbar.addAction("Next ▶", self.next_page)

        toolbar.addSeparator()
        self.btn_export = toolbar.addAction("📥 Export Full PDF", self.export_pdf)

    def load_document(self, file_path, page_num=0, highlight_text="", display_name=""):
        try:
            if self.doc: self.doc.close()
            self.original_file_path = file_path  # 保存原始物理路径，确保导出的是完整文件
            self.doc = fitz.open(file_path)
            self.highlight_text = highlight_text
            self.display_name = display_name or os.path.basename(file_path)
            self.goto_page(page_num)

            self.show()
            self.raise_()
            self.activateWindow()
        except Exception as e:
            print(f"Error opening PDF: {e}")

    def goto_page(self, page_num):
        if not self.doc: return
        self.current_page = max(0, min(page_num, len(self.doc) - 1))
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
        # 1. 精确匹配
        quads = page.search_for(text)
        if quads: return quads

        # 2. 清理换行符和多余空格后匹配
        clean_text = re.sub(r'\s+', ' ', text).strip()
        quads = page.search_for(clean_text)
        if quads: return quads

        # 3. 智能切片匹配：按标点符号切分
        if len(clean_text) > 30:
            chunks = [c.strip() for c in re.split(r'[,.，。;\n]', clean_text) if len(c.strip()) > 10]
            all_quads = []
            for chunk in chunks:
                q = page.search_for(chunk)
                if q: all_quads.extend(q)
            if all_quads: return all_quads
            # 4. 最终降级：仅匹配头部
            return page.search_for(clean_text[:30])
        return []

    def render_page(self):
        if not self.doc: return
        page = self.doc.load_page(self.current_page)

        # High DPI Rendering
        zoom = 2.0
        mat = fitz.Matrix(zoom, zoom)
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
                painter.setBrush(QColor(255, 235, 59, 120))  # Yellow transparent

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
                # 直接从硬盘复制原始文件，保证 100% 完整性
                shutil.copy2(self.original_file_path, save_path)
                QMessageBox.information(self, "Success", f"Full PDF exported successfully to:\n{save_path}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to export PDF:\n{str(e)}")


# ==========================================
# 🌟 全新的文本阅读器（支持 TXT, MD 等）
# ==========================================
class InternalTextViewer(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Text Document Viewer")
        self.resize(1000, 800)

        self.original_file_path = ""
        self.display_name = ""

        # 使用 QTextBrowser 显示纯文本
        self.text_browser = QTextBrowser()
        self.text_browser.setStyleSheet("""
            QTextBrowser {
                background-color: #1e1e1e;
                color: #d4d4d4;
                font-size: 14px;
                font-family: 'Consolas', 'Microsoft YaHei', monospace;
                padding: 20px;
                border: none;
            }
        """)
        self.setCentralWidget(self.text_browser)

        # Toolbar
        toolbar = QToolBar()
        toolbar.setMovable(False)
        toolbar.setStyleSheet(
            "QToolBar { background: #333; border-bottom: 1px solid #555; } QToolButton { color: white; }")
        self.addToolBar(toolbar)
        self.btn_export = toolbar.addAction("📥 Export Original File", self.export_file)

    def load_document(self, file_path, highlight_text="", display_name=""):
        self.original_file_path = file_path
        self.display_name = display_name or os.path.basename(file_path)
        self.setWindowTitle(f"Text Viewer - {self.display_name}")

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            # 设置纯文本以确保与向量库分块时提取的内容绝对一致
            self.text_browser.setPlainText(content)

            if highlight_text:
                self._highlight_and_scroll(highlight_text)

            self.show()
            self.raise_()
            self.activateWindow()
        except Exception as e:
            print(f"Error opening text file: {e}")

    def _highlight_and_scroll(self, text):
        document = self.text_browser.document()

        # 1. 精确匹配
        cursor = document.find(text)

        # 2. 消除多余换行符/空格后降级匹配
        if cursor.isNull():
            clean_text = re.sub(r'\s+', ' ', text).strip()
            cursor = document.find(clean_text)

        # 3. 提取前30个字符匹配
        if cursor.isNull() and len(text) > 30:
            cursor = document.find(text[:30])

        if not cursor.isNull():
            # 应用高亮样式
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(255, 235, 59, 120))  # 半透明黄色
            cursor.mergeCharFormat(fmt)

            # 将视野滚动到高亮处
            self.text_browser.setTextCursor(cursor)

    def export_file(self):
        if not self.original_file_path or not os.path.exists(self.original_file_path):
            return

        save_path, _ = QFileDialog.getSaveFileName(
            self, "Export Original File", self.display_name, "All Files (*.*)"
        )

        if save_path:
            try:
                # 依然是底层直接拷贝完整文件
                shutil.copy2(self.original_file_path, save_path)
                QMessageBox.information(self, "Success", f"File exported successfully to:\n{save_path}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to export file:\n{str(e)}")