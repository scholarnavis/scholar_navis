import logging
import os
import shutil

from PySide6.QtCore import Qt, QEvent, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from src.core.theme_manager import ThemeManager
from src.ui.components.source_code_viewer import SourceCodeViewer

logger = logging.getLogger(__name__)


class RPlotCardWidget(QFrame):
    """学术风格的 R 绘图结果卡片。

    展示 R 渲染出的图表预览，并提供 PNG / SVG / PDF / 数据 CSV / R 源码
    的下载按钮。R 脚本源码可通过专属控件（可折叠、可复制、独立滚动条、
    深色模式自适应）就地查看。颜色随主题（深色/浅色）自适应。
    """

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self.data = data or {}
        self.setObjectName("rPlotCard")

        self._png_path = self.data.get("png_path", "")
        self._svg_path = self.data.get("svg_path", "")
        self._pdf_path = self.data.get("pdf_path", "")
        self._script_path = self.data.get("script_path", "")
        self._data_path = self.data.get("data_path", "")
        self._chart_title = self.data.get("chart_title", "R Chart")
        self._total_rows = int(self.data.get("total_rows", 0) or 0)
        self._plot_id = self.data.get("plot_id", "")
        # 语义标签：同一对话绘制多张图时，用它区分"哪一张"（plot_2 [volcano] ...）
        self._plot_label = self.data.get("plot_label", "") or self._plot_id
        self._script_loaded = False

        self._build_ui()
        self._load_preview()
        self._apply_theme()
        ThemeManager().theme_changed.connect(self._apply_theme)

    # ------------------------------------------------------------------ UI ---
    def _build_ui(self):
        self._card_layout = QVBoxLayout(self)
        self._card_layout.setContentsMargins(14, 12, 14, 12)
        self._card_layout.setSpacing(8)

        # --- header: R 徽标 + 标题 + 数据信息 ---
        header = QHBoxLayout()
        header.setSpacing(10)

        self._badge = QLabel("R")
        self._badge.setAlignment(Qt.AlignCenter)
        self._badge.setFixedSize(34, 34)
        self._badge.setProperty("cssClass", "rBadge")

        self._title = QLabel(self._chart_title)
        self._title.setProperty("cssClass", "rTitle")
        self._title.setWordWrap(True)

        info = f"{self._total_rows} rows" if self._total_rows else "R / ggplot2"
        if self._plot_label:
            # 展示语义标签（含 plot_id），便于在多图场景下快速对应需求。
            info = f"{info} · {self._plot_label}"
        elif self._plot_id:
            info = f"{info} · {self._plot_id}"
        self._meta = QLabel(info)
        self._meta.setProperty("cssClass", "rMeta")
        self._meta.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        header.addWidget(self._badge)
        header.addWidget(self._title, 1)
        header.addWidget(self._meta)

        # --- preview image ---
        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview.setFixedWidth(480)
        self._preview.setMinimumHeight(120)
        self._preview.setProperty("cssClass", "rPreview")
        self._preview.setCursor(Qt.PointingHandCursor)
        self._preview.setToolTip("Double-click to open with system image viewer")
        self._preview.installEventFilter(self)

        # --- download buttons ---
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.setAlignment(Qt.AlignLeft)

        self._btn_png = self._make_download_btn("PNG", "download", "png")
        self._btn_svg = self._make_download_btn("SVG", "download", "svg")
        self._btn_pdf = self._make_download_btn("PDF", "download", "pdf")
        self._btn_data = self._make_download_btn("Data", "database", "data")
        self._btn_script = self._make_download_btn("R code", "file-text", "script")

        for b in (self._btn_png, self._btn_svg, self._btn_pdf, self._btn_data,
                  self._btn_script):
            btn_row.addWidget(b)
        btn_row.addStretch(1)

        self._card_layout.addLayout(header)
        self._card_layout.addWidget(self._preview)
        self._card_layout.addLayout(btn_row)

        # --- 自然语言修改提示：引导用户继续在对话中调整这张图 ---
        self._edit_hint = QLabel(
            "You can keep modifying this chart with natural language, e.g. "
            '"change the colors" or "switch to a line chart".'
        )
        self._edit_hint.setProperty("cssClass", "rEditHint")
        self._edit_hint.setWordWrap(True)
        self._card_layout.addWidget(self._edit_hint)

        # --- 可折叠的 R 脚本源码查看控件（默认折叠，自动加载源码） ---
        self._source_view = SourceCodeViewer(
            title="R Plot Script (.R)",
            editable=False,
            collapsed=True,
            max_height=300,
        )
        self._card_layout.addWidget(self._source_view)
        self._load_script_source()

    def _make_download_btn(self, text: str, icon_name: str, kind: str) -> QPushButton:
        btn = QPushButton(f" {text}")
        btn.setProperty("cssClass", "rDownloadBtn")
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda _=False, k=kind: self._download(k))
        return btn

    def _load_preview(self):
        """加载预览图：优先 PNG，其次将 SVG 栅格化显示。"""
        try:
            if self._png_path and os.path.exists(self._png_path):
                pixmap = QPixmap(self._png_path)
            elif self._svg_path and os.path.exists(self._svg_path):
                renderer = QSvgRenderer(self._svg_path)
                size = renderer.defaultSize()
                w = max(1, int(size.width())) if size.isValid() and size.width() > 0 else 800
                h = max(1, int(size.height())) if size.isValid() and size.height() > 0 else 600
                pixmap = QPixmap(w, h)
                pixmap.fill(Qt.transparent)
                painter = QPainter(pixmap)
                painter.setRenderHint(QPainter.Antialiasing)
                painter.setRenderHint(QPainter.SmoothPixmapTransform)
                renderer.render(painter)
                painter.end()
            else:
                self._preview.setText("No preview available")
                return

            if pixmap.isNull():
                self._preview.setText("Preview unavailable")
                return

            scaled = pixmap.scaledToWidth(
                460, Qt.SmoothTransformation
            ) if pixmap.width() > 460 else pixmap
            self._preview.setPixmap(scaled)
        except Exception as e:
            logger.warning(f"Failed to load plot preview: {e}")
            self._preview.setText("Preview unavailable")

    # ------------------------------------------------------- open external ---
    def eventFilter(self, obj, event):
        """拦截预览图的双击事件，用系统默认图片查看器打开原图。"""
        if obj is self._preview and event.type() == QEvent.MouseButtonDblClick:
            self._open_external()
            return True
        return super().eventFilter(obj, event)

    def _open_external(self):
        """用系统默认图片查看器打开 PNG（优先）或 SVG 原图。"""
        target = self._png_path if (self._png_path and os.path.exists(self._png_path)) else self._svg_path
        if not target or not os.path.exists(target):
            logger.warning(f"Open external skipped: no image file available")
            return
        try:
            ok = QDesktopServices.openUrl(QUrl.fromLocalFile(target))
            if ok:
                logger.info(f"Opened plot with system viewer: {target}")
            else:
                logger.warning(f"System viewer failed to open: {target}")
        except Exception as e:
            logger.error(f"Failed to open plot with system viewer: {e}")

    # --------------------------------------------------------------- theme ---
    def _apply_theme(self):
        tm = ThemeManager()
        card_bg = tm.color("bg_card")
        border = tm.color("border")
        text_main = tm.color("text_main")
        text_muted = tm.color("text_muted")
        accent = tm.color("accent")
        academic_blue = tm.color("academic_blue")
        academic_hover = tm.color("academic_blue_hover")
        btn_bg = tm.color("btn_bg")
        btn_hover = tm.color("btn_hover")
        font_family = tm.font_family()

        self.setStyleSheet(f"""
            QFrame#rPlotCard {{
                background-color: {card_bg};
                border: 1px solid {border};
                border-left: 4px solid {academic_blue};
                border-radius: 8px;
            }}
            QLabel[cssClass="rBadge"] {{
                background-color: {academic_blue};
                color: #ffffff;
                border-radius: 8px;
                font-size: 18px;
                font-weight: bold;
                font-family: "Georgia", {font_family};
            }}
            QLabel[cssClass="rTitle"] {{
                color: {text_main};
                font-size: 14px;
                font-weight: bold;
                font-family: {font_family};
            }}
            QLabel[cssClass="rMeta"] {{
                color: {text_muted};
                font-size: 11px;
                font-family: {font_family};
            }}
            QLabel[cssClass="rEditHint"] {{
                color: {text_muted};
                font-size: 11px;
                font-family: {font_family};
                background-color: {tm.color('bg_input')};
                border: 1px dashed {border};
                border-radius: 4px;
                padding: 4px 8px;
            }}
            QLabel[cssClass="rPreview"] {{
                background-color: {tm.color('bg_main') if tm.current_theme == 'dark' else '#fafafa'};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 6px;
            }}
            QPushButton[cssClass="rDownloadBtn"] {{
                background-color: {btn_bg};
                color: {text_main};
                border: 1px solid {border};
                border-radius: 5px;
                padding: 5px 12px;
                font-size: 12px;
                font-family: {font_family};
            }}
            QPushButton[cssClass="rDownloadBtn"]:hover {{
                background-color: {btn_hover};
                border-color: {accent};
                color: {accent};
            }}
            QPushButton[cssClass="rDownloadBtn"]:pressed {{
                background-color: {academic_blue};
                color: #ffffff;
            }}
            QPushButton[cssClass="rDownloadBtn"]:disabled {{
                background-color: {btn_bg};
                color: {text_muted};
                border-color: {border};
            }}
        """)

        self._btn_png.setIcon(tm.icon("download", "text_muted"))
        self._btn_svg.setIcon(tm.icon("download", "text_muted"))
        self._btn_pdf.setIcon(tm.icon("download", "text_muted"))
        self._btn_data.setIcon(tm.icon("database", "text_muted"))
        self._btn_script.setIcon(tm.icon("file-text", "text_muted"))

        # 禁用不存在的文件按钮
        self._btn_png.setEnabled(bool(self._png_path and os.path.exists(self._png_path)))
        self._btn_svg.setEnabled(bool(self._svg_path and os.path.exists(self._svg_path)))
        self._btn_pdf.setEnabled(bool(self._pdf_path and os.path.exists(self._pdf_path)))
        self._btn_data.setEnabled(bool(self._data_path and os.path.exists(self._data_path)))
        self._btn_script.setEnabled(bool(self._script_path and os.path.exists(self._script_path)))

    # ---------------------------------------------------------- source ----
    def _load_script_source(self):
        """自动加载 R 脚本源码到查看控件（保持默认折叠）。"""
        if not self._script_path or not os.path.exists(self._script_path):
            logger.warning(f"Load source skipped: missing file {self._script_path}")
            return
        if self._script_loaded:
            return
        try:
            with open(self._script_path, encoding="utf-8") as f:
                self._source_view.set_code(f.read())
            self._script_loaded = True
        except OSError as e:
            logger.error(f"Failed to read R script {self._script_path}: {e}")

    # ----------------------------------------------------------- download ---
    def _download(self, kind: str):
        if kind == "png":
            src, default = self._png_path, f"{self._safe_name()}.png"
        elif kind == "svg":
            src, default = self._svg_path, f"{self._safe_name()}.svg"
        elif kind == "pdf":
            src, default = self._pdf_path, f"{self._safe_name()}.pdf"
        elif kind == "data":
            src, default = self._data_path, f"{self._safe_name()}_data.csv"
        elif kind == "script":
            src, default = self._script_path, f"{self._safe_name()}.R"
        else:
            return

        if not src or not os.path.exists(src):
            logger.warning(f"Download '{kind}' skipped: missing file {src}")
            return

        save_path, _ = QFileDialog.getSaveFileName(self, "Save", default, "All Files (*)")
        if not save_path:
            return

        try:
            shutil.copyfile(src, save_path)
            logger.info(f"Plot file saved: {save_path}")
        except OSError as e:
            logger.error(f"Failed to save plot file: {e}")

    def _safe_name(self) -> str:
        name = self._chart_title.strip() or "r_plot"
        for ch in '\\/:*?"<>|':
            name = name.replace(ch, "_")
        return name

    def closeEvent(self, event):
        """销毁前断开主题信号，避免对已删除对象回调。"""
        try:
            ThemeManager().theme_changed.disconnect(self._apply_theme)
        except (TypeError, RuntimeError):
            pass
        super().closeEvent(event)
