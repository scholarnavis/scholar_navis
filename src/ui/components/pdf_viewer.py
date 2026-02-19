import fitz  # PyMuPDF
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QScrollArea, QLabel,
                               QMainWindow, QToolBar, QApplication)
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QAction
from PySide6.QtCore import Qt


class InternalPDFViewer(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Document Viewer")
        self.resize(1000, 800)
        self.doc = None
        self.current_page = 0
        self.highlight_text = ""

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

    def load_document(self, file_path, page_num=0, highlight_text=""):
        try:
            if self.doc: self.doc.close()
            self.doc = fitz.open(file_path)
            self.highlight_text = highlight_text
            self.goto_page(page_num)

            self.show()
            self.raise_()  # Mac/Linux兼容
            self.activateWindow()  # Windows兼容
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
            self.setWindowTitle(f"Page {self.current_page + 1} / {len(self.doc)} - {self.doc.name}")

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

        # Highlighting Logic
        target_y = None
        if self.highlight_text:
            # 1. Exact Match
            quads = page.search_for(self.highlight_text)

            # 2. Fuzzy Match (if exact match fails and text is long)
            if not quads and len(self.highlight_text) > 20:
                # 只搜索前20个字符，防止因为换行符差异导致匹配失败
                quads = page.search_for(self.highlight_text[:20])

            if quads:
                painter = QPainter(final_pixmap)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(255, 235, 59, 100))  # Yellow transparent

                for quad in quads:
                    rect = quad * mat  # Apply zoom matrix
                    painter.drawRect(rect.x0, rect.y0, rect.width, rect.height)
                painter.end()

                # Calculate scroll target
                first_rect = quads[0] * mat
                target_y = first_rect.y0 + first_rect.height / 2

        self.lbl_page.setPixmap(final_pixmap)

        # Auto Scroll to Highlight
        if target_y is not None:
            QApplication.processEvents()
            # Center the target Y in the scroll area
            scroll_val = int(target_y - self.scroll_area.height() / 2)
            self.scroll_area.verticalScrollBar().setValue(max(0, scroll_val))