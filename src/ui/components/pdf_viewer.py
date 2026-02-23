import fitz  # PyMuPDF
import os
import shutil
import re
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QScrollArea, QLabel, QMenu,
                               QMainWindow, QToolBar, QApplication, QFileDialog, QMessageBox,
                               QTextBrowser, QRubberBand)
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QTextCharFormat, QTextCursor, Qt, QDesktopServices
from PySide6.QtCore import Qt, QRect, QSize, QPoint, Signal, QUrl, QEvent

from src.core.signals import GlobalSignals



# 支持鼠标划词的交互式 Label
class InteractivePDFLabel(QLabel):
    sig_text_selected = Signal(str)

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
        rect = self.rubber_band.geometry()
        # 只有当框选面积大于一定值，且当前页面和矩阵已加载时才触发文本提取
        if rect.width() > 10 and rect.height() > 10 and self.current_page and self.mat:
            # 矩阵逆运算：将屏幕坐标映射回 PDF 真实坐标
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


# 高级 PDF 阅读器 (带划词翻译、缩放与导出)
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

    def _setup_toolbar(self):
        tb1 = QToolBar()
        tb1.setMovable(False)
        tb1.setStyleSheet(
            "QToolBar { background: #333; padding: 6px; border: none; } QToolButton { color: white; padding: 5px 10px; border-radius: 4px; font-weight: bold; } QToolButton:hover { background: #444; color: #05B8CC; }")
        self.addToolBar(Qt.TopToolBarArea, tb1)

        tb1.addAction("◀ Prev", self.prev_page)
        tb1.addAction("Next ▶", self.next_page)
        tb1.addSeparator()
        tb1.addAction("🔍 Zoom In", self.zoom_in)
        tb1.addAction("🔍 Zoom Out", self.zoom_out)
        tb1.addAction("↔️ Fit Width", self.fit_width)

        self.addToolBarBreak(Qt.TopToolBarArea)

        # 第二行：外部功能区
        tb2 = QToolBar()
        tb2.setMovable(False)
        tb2.setStyleSheet(
            "QToolBar { background: #333; border-bottom: 1px solid #555; padding: 6px; } QToolButton { color: white; padding: 5px 10px; border-radius: 4px; font-weight: bold; } QToolButton:hover { background: #444; color: #05B8CC; }")
        self.addToolBar(Qt.TopToolBarArea, tb2)

        tb2.addAction("🖥️ Open in System", self.open_system_app)
        tb2.addAction("📥 Export Full PDF", self.export_pdf)

        hint = QLabel("  (💡 Tip: Select text and press Space or Right-Click to Translate)")
        hint.setStyleSheet("color: #aaa; font-style: italic; font-size: 13px; padding-left: 10px;")
        tb2.addWidget(hint)


    def load_document(self, file_path, page_num=0, highlight_text="", display_name=""):
        try:
            if self.doc: self.doc.close()
            self.original_file_path = file_path  # 保存原始物理路径用于导出和外部打开
            self.doc = fitz.open(file_path)
            self.highlight_text = highlight_text
            self.display_name = display_name or os.path.basename(file_path)

            self.fit_width()  # 默认自适应宽度
            self.goto_page(page_num)

            self.show()
            self.raise_()
            self.activateWindow()
        except Exception as e:
            print(f"Error opening PDF: {e}")

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
        self.current_page = max(0, min(page_num, len(self.doc) - 1))
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

    def _setup_toolbar(self):
        tb1 = QToolBar()
        tb1.setMovable(False)
        tb1.setStyleSheet(
            "QToolBar { background: #333; padding: 6px; border: none; } QToolButton { color: white; padding: 5px 10px; border-radius: 4px; font-weight: bold; } QToolButton:hover { background: #444; color: #05B8CC; }")
        self.addToolBar(Qt.TopToolBarArea, tb1)

        tb1.addAction("🔍 Zoom In", self.zoom_in)
        tb1.addAction("🔍 Zoom Out", self.zoom_out)

        self.addToolBarBreak(Qt.TopToolBarArea)

        tb2 = QToolBar()
        tb2.setMovable(False)
        tb2.setStyleSheet(
            "QToolBar { background: #333; border-bottom: 1px solid #555; padding: 6px; } QToolButton { color: white; padding: 5px 10px; border-radius: 4px; font-weight: bold; } QToolButton:hover { background: #444; color: #05B8CC; }")
        self.addToolBar(Qt.TopToolBarArea, tb2)

        tb2.addAction("🖥️ Open in System", self.open_system_app)
        tb2.addAction("📥 Export Original File", self.export_file)

        hint = QLabel("  (💡 Tip: Select text and press Space to Translate)")
        hint.setStyleSheet("color: #aaa; font-style: italic; font-size: 13px; padding-left: 10px;")
        tb2.addWidget(hint)


    def eventFilter(self, obj, event):
        if obj == self.text_browser and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Space:
                selected_text = self.text_browser.textCursor().selectedText()
                if selected_text:
                    self._invoke_translator(selected_text)
                    return True  # 拦截事件，防止文本框向下滚动
        return super().eventFilter(obj, event)



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

    # --- 加载与高亮逻辑保持不变 ---
    def load_document(self, file_path, highlight_text="", display_name=""):
        self.original_file_path = file_path
        self.display_name = display_name or os.path.basename(file_path)
        self.setWindowTitle(f"Text Viewer - {self.display_name}")

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
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
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(255, 235, 59, 120))  # 半透明黄色
            cursor.mergeCharFormat(fmt)
            self.text_browser.setTextCursor(cursor)

    # --- 导出与外部打开 ---
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



