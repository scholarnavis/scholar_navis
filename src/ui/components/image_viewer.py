"""Image viewer dialog.

对话中出现的一切图片（用户上传、AI 生成、工具产图）都可通过该
查看器进行浏览与持久化：

- 打开 / 双击图片 -> 以内部查看器展示（不再依赖系统默认程序）；
- 保存（Save As）-> 原始字节落盘；SVG 支持另存为 PNG；
- 复制图片 / 用系统默认程序打开；
- 缩放（按钮 / 鼠标滚轮 / 适配窗口），放大后可拖拽平移。

SVG 通过 ``QSvgRenderer`` 以 2x 分辨率栅格化显示，保存时保留
原始 SVG（可选导出 PNG）。
"""
import logging
import os

from PySide6.QtCore import Qt, QSize, QPointF, QPoint
from PySide6.QtGui import QImageReader, QPainter, QPixmap, QDesktopServices
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QScrollArea, QPushButton, QFileDialog)

from src.core.theme_manager import ThemeManager
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)

#: SVG 显示时的栅格化放大倍数（保证高 DPI 下清晰）
SVG_RENDER_SCALE = 2.0

#: 滚轮缩放步进系数（与按钮缩放的 1.25 区分，滚轮更细腻）
WHEEL_ZOOM_FACTOR = 1.15


class _ZoomableScrollArea(QScrollArea):
    """支持滚轮缩放与拖拽平移的图片滚动区。

    - 滚轮（含 Ctrl+滚轮）：以鼠标位置为锚点缩放——缩放前后鼠标指向
      的内容点保持不动，符合主流图片查看器的操作习惯；
    - 左键拖拽：平移视图（放大后查看视口外区域），光标呈抓手态；
    - Shift+滚轮：保留原生水平滚动（部分用户习惯）。

    缩放本体委托宿主 ``viewer.set_scale``，锚点校正由本类在缩放前后
    读取/回写滚动条实现，两者职责分离。
    """

    def __init__(self, viewer, parent=None):
        super().__init__(parent)
        self._viewer = viewer
        self._drag_active = False
        self._drag_last = QPointF()
        self.setCursor(Qt.OpenHandCursor)

    # ---------- 滚轮缩放 ----------
    def wheelEvent(self, event):
        if event.modifiers() & Qt.ShiftModifier:
            # Shift+滚轮：水平滚动（查看超宽图时保留原生行为）
            super().wheelEvent(event)
            return
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        factor = WHEEL_ZOOM_FACTOR if delta > 0 else 1 / WHEEL_ZOOM_FACTOR
        self._zoom_anchored(event.position().toPoint(), factor)
        event.accept()

    def _zoom_anchored(self, anchor: QPoint, factor: float):
        """以 ``anchor``（视口坐标）为锚点缩放，并校正滚动条位置。"""
        old_scale = self._viewer._scale
        # 缩放前：锚点在内容坐标系中的位置（滚动偏移 + 视口内偏移）
        content_x = self.horizontalScrollBar().value() + anchor.x()
        content_y = self.verticalScrollBar().value() + anchor.y()

        self._viewer.set_scale(old_scale * factor)

        # setPixmap 后 widget 尺寸与滚动范围要等布局事件（异步）才刷新，
        # 此处强制同步调整，避免下方 setValue 被过期的滚动范围 clamp。
        widget = self.widget()
        if widget is not None:
            widget.adjustSize()

        # 缩放后：内容坐标随 scale 等比放大，把锚点拉回鼠标下方
        ratio = self._viewer._scale / old_scale
        if ratio != 1.0:
            self.horizontalScrollBar().setValue(round(content_x * ratio - anchor.x()))
            self.verticalScrollBar().setValue(round(content_y * ratio - anchor.y()))

    # ---------- 拖拽平移 ----------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_active = True
            self._drag_last = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_active:
            pos = event.position()
            dx = pos.x() - self._drag_last.x()
            dy = pos.y() - self._drag_last.y()
            self._drag_last = pos
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - round(dx))
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - round(dy))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_active:
            self._drag_active = False
            self.setCursor(Qt.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ImageViewerDialog(QDialog):
    """图片查看器对话框。"""

    def __init__(self, image_path=None, raw_bytes=None, svg_bytes=None, title=None, parent=None):
        """创建查看器。

        :param image_path: 本地图片路径（含 SVG）。
        :param raw_bytes: 内存中的图片字节（优先于路径）。
        :param svg_bytes: SVG 源码字节（用于保存原始矢量数据）。
        :param title: 窗口标题，默认取文件名。
        """
        super().__init__(parent)
        self.setWindowTitle(title or "Image Viewer")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.resize(860, 640)

        self.source_path = image_path or ""
        self.svg_bytes = svg_bytes
        self.raw_bytes = raw_bytes
        self._pixmap = QPixmap()
        self._scale = 1.0
        self._fit_mode = True

        tm = ThemeManager()
        self.setStyleSheet(f"""
            QDialog {{ background-color: {tm.color('bg_main')}; }}
            QLabel {{ color: {tm.color('text_main')}; }}
        """)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(8)

        # ---- 图片浏览区（滚轮缩放 / 拖拽平移，见 _ZoomableScrollArea） ----
        self.scroll_area = _ZoomableScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("QScrollArea { border: none; }")
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet("background: transparent;")
        self.scroll_area.setWidget(self.image_label)
        main_layout.addWidget(self.scroll_area, 1)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet(
            f"color: {tm.color('text_muted')}; font-size: 11px; border: none;")
        main_layout.addWidget(self.lbl_status)

        # ---- 控制按钮区 ----
        btn_bar = QHBoxLayout()
        btn_bar.setSpacing(6)

        def _btn(text, icon_key, slot):
            b = QPushButton(text)
            b.setIcon(tm.icon(icon_key, "text_main"))
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(slot)
            return b

        btn_bar.addWidget(_btn(" Zoom In", "search", lambda: self.set_scale(self._scale * 1.25)))
        btn_bar.addWidget(_btn(" Zoom Out", "remove", lambda: self.set_scale(self._scale / 1.25)))
        btn_bar.addWidget(_btn(" Fit", "refresh", self.fit_to_window))
        btn_bar.addWidget(_btn(" 1:1", "check", lambda: self.set_scale(1.0)))
        btn_bar.addStretch()
        btn_bar.addWidget(_btn(" Copy", "copy", self.copy_image))
        btn_bar.addWidget(_btn(" Save As...", "download", self.save_image_as))
        btn_bar.addWidget(_btn(" Open Externally", "open", self.open_externally))

        main_layout.addLayout(btn_bar)

        self._load_image()
        self.fit_to_window()

    # ---------- 加载 ----------
    def _load_image(self):
        """根据路径或字节加载 QPixmap（SVG 走栅格化）。"""
        try:
            if self.source_path and self.source_path.lower().endswith('.svg'):
                self._load_svg()
            elif self.raw_bytes is not None:
                from PySide6.QtCore import QBuffer, QByteArray
                buf = QBuffer(QByteArray(self.raw_bytes))
                buf.open(QBuffer.ReadOnly)
                reader = QImageReader(buf)
                reader.setAutoTransform(True)
                img = reader.read()
                if img.isNull():
                    raise ValueError(reader.errorString())
                self._pixmap = QPixmap.fromImage(img)
            elif self.source_path:
                reader = QImageReader(self.source_path)
                reader.setAutoTransform(True)
                img = reader.read()
                if img.isNull():
                    raise ValueError(reader.errorString())
                self._pixmap = QPixmap.fromImage(img)
            else:
                raise ValueError("No image source provided.")

            self.lbl_status.setText(self._describe())
        except Exception as e:
            logger.warning(f"Failed to load image for viewer: {e}")
            self._pixmap = QPixmap()
            self.image_label.setText(f"Failed to load image: {e}")
            self.lbl_status.setText("")

        self._apply_scale()

    def _load_svg(self):
        """用 QSvgRenderer 栅格化 SVG（按原始尺寸放大渲染保证清晰度）。"""
        from PySide6.QtCore import QBuffer, QByteArray

        svg_source = self.svg_bytes
        if svg_source is None and self.source_path:
            with open(self.source_path, 'rb') as f:
                svg_source = f.read()

        renderer = QSvgRenderer(QByteArray(svg_source))
        if not renderer.isValid():
            raise ValueError("Invalid SVG data.")

        size = renderer.defaultSize()
        if not size.isValid() or size.isEmpty():
            size = QSize(1024, 768)

        width = int(size.width() * SVG_RENDER_SCALE)
        height = int(size.height() * SVG_RENDER_SCALE)

        from PySide6.QtGui import QImage
        img = QImage(width, height, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.Antialiasing)
        renderer.render(painter)
        painter.end()
        self._pixmap = QPixmap.fromImage(img)

    def _describe(self) -> str:
        """生成状态栏描述：尺寸 + 体积 + 来源。"""
        if self._pixmap.isNull():
            return ""
        name = os.path.basename(self.source_path) if self.source_path else "generated image"
        size_kb = len(self.raw_bytes) / 1024 if self.raw_bytes is not None else (
            os.path.getsize(self.source_path) / 1024 if os.path.exists(self.source_path) else 0)
        return (f"{name}  |  {self._pixmap.width()} x {self._pixmap.height()} px"
                + (f"  |  {size_kb:.1f} KB" if size_kb > 0 else "")
                + "  |  Scroll: Zoom · Drag: Pan")

    # ---------- 缩放 ----------
    def _apply_scale(self):
        if self._pixmap.isNull():
            return
        scaled = self._pixmap.scaled(
            self._pixmap.size() * self._scale,
            Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.image_label.setPixmap(scaled)

    def set_scale(self, scale: float):
        self._scale = max(0.05, min(scale, 16.0))
        self._fit_mode = False
        self._apply_scale()

    def fit_to_window(self):
        if self._pixmap.isNull():
            return
        vw = self.scroll_area.viewport().width() - 20
        vh = self.scroll_area.viewport().height() - 20
        if vw <= 0 or vh <= 0:
            return
        self._scale = min(vw / self._pixmap.width(), vh / self._pixmap.height())
        self._fit_mode = True
        self._apply_scale()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit_mode:
            self.fit_to_window()

    # ---------- 操作 ----------
    def copy_image(self):
        if self._pixmap.isNull():
            return
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setPixmap(self._pixmap)
        ToastManager().show("Image copied to clipboard.", "success")

    def save_image_as(self):
        """保存图片。SVG 默认保存原始矢量；也可另存为 PNG。"""
        if self._pixmap.isNull() and not self.source_path:
            return

        is_svg = self.svg_bytes is not None or (self.source_path and self.source_path.lower().endswith('.svg'))
        default_name = os.path.basename(self.source_path) if self.source_path else "generated_image.png"
        if not default_name:
            default_name = "image.svg" if is_svg else "image.png"

        image_filter = "Image (*.png *.jpg *.jpeg *.webp *.bmp)"
        if is_svg:
            image_filter = "SVG Vector (*.svg);;" + image_filter

        target, _ = QFileDialog.getSaveFileName(self, "Save Image As", default_name, image_filter)
        if not target:
            return

        try:
            if target.lower().endswith('.svg'):
                if self.svg_bytes is not None:
                    with open(target, 'wb') as f:
                        f.write(self.svg_bytes)
                elif self.source_path:
                    with open(self.source_path, 'rb') as src, open(target, 'wb') as dst:
                        dst.write(src.read())
                else:
                    raise ValueError("Original SVG data unavailable; save as PNG instead.")
            elif target.lower() == self.source_path.lower():
                # 原地保存：直接复制源文件，避免重复编码损失
                import shutil
                shutil.copy2(self.source_path, target)
            elif self.raw_bytes is not None and os.path.splitext(target)[1].lower() == \
                    os.path.splitext(self.source_path or '.png')[1].lower():
                with open(target, 'wb') as f:
                    f.write(self.raw_bytes)
            else:
                if not self._pixmap.save(target):
                    raise IOError(f"Failed to save image to {target}")
            ToastManager().show(f"Image saved: {os.path.basename(target)}", "success")
        except Exception as e:
            logger.error(f"Failed to save image: {e}")
            ToastManager().show(f"Failed to save image: {e}", "error")

    def open_externally(self):
        """用系统默认程序打开（需要本地路径或先落盘临时文件）。"""
        path = self.source_path
        if not path and self.raw_bytes is not None:
            import hashlib
            import tempfile
            ext = '.png'
            path = os.path.join(
                tempfile.gettempdir(),
                f"navis_view_{hashlib.md5(self.raw_bytes).hexdigest()[:10]}{ext}")
            try:
                with open(path, 'wb') as f:
                    f.write(self.raw_bytes)
            except OSError as e:
                ToastManager().show(f"Failed to open externally: {e}", "error")
                return

        if path and os.path.exists(path):
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            ToastManager().show("Image file not found.", "error")


def open_image_viewer(image_path=None, parent=None, raw_bytes=None, svg_bytes=None):
    """以非模态方式打开查看器（窗口关闭时自动销毁）。

    同一父窗口下相同图片已打开时，直接激活既有窗口而不是重复弹窗
    （双击/单击连续触发时防止窗口堆积）。
    """
    key = os.path.abspath(image_path) if image_path else None
    if key is not None and parent is not None:
        existing = getattr(parent, "_image_viewers", None)
        if existing and key in existing:
            dlg = existing[key]
            try:
                if dlg.isVisible():
                    dlg.raise_()
                    dlg.activateWindow()
                    return dlg
            except RuntimeError:
                # 窗口已销毁，清理失效引用
                del existing[key]

    dlg = ImageViewerDialog(image_path=image_path, raw_bytes=raw_bytes,
                            svg_bytes=svg_bytes, parent=parent)
    dlg.setAttribute(Qt.WA_DeleteOnClose)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()

    if key is not None and parent is not None:
        if not hasattr(parent, "_image_viewers"):
            parent._image_viewers = {}
        parent._image_viewers[key] = dlg
    return dlg
