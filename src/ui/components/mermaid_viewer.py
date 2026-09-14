import base64
import json
import os

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QMainWindow, QToolBar, QCheckBox,
                               QFileDialog, QComboBox, QSplitter)

from src.core.theme_manager import ThemeManager, apply_native_titlebar_theme
# 🌟 引入你的自定义 Dialog
from src.ui.components.dialog import StandardDialog
from src.ui.components.source_code_viewer import SourceCodeViewer


#: 导出用全局导出函数，注入页面 <script> 后供 Qt 端 runJavaScript 调用。
#: - ``__navisExportSVG()``: 返回 #graphDiv 内 <svg> 的 outerHTML（矢量）。
#: - ``__navisExportRaster(mime, quality, scale, crop)``: 按 SVG 真实尺寸光栅化，
#:   返回 ``data:`` URL。crop=True 时按非背景像素自动裁剪四周空白。
#:   此方案不依赖视口/滚动条/grab，可保证高清不裁切。
_NAVIS_EXPORT_JS = r"""
function __navisSvgNode() {
    return document.querySelector('#graphDiv svg');
}

function __navisSvgLogicalSize(svg) {
    var w = 0, h = 0;
    try {
        var vb = svg.viewBox;
        if (vb && vb.baseVal) { w = vb.baseVal.width; h = vb.baseVal.height; }
    } catch (e) { /* ignore */ }
    if (!w || !h) {
        try { var b = svg.getBBox(); if (b && (b.width || b.height)) { w = b.width; h = b.height; } } catch (e) {}
    }
    if (!w || !h) {
        try {
            var wa = svg.width && svg.width.baseVal ? svg.width.baseVal.value : 0;
            var ha = svg.height && svg.height.baseVal ? svg.height.baseVal.value : 0;
            if (wa && ha) { w = wa; h = ha; }
        } catch (e) {}
    }
    return { w: w || 1200, h: h || 800 };
}

function __navisExportSVG() {
    var svg = __navisSvgNode();
    if (!svg) return null;
    return new XMLSerializer().serializeToString(svg);
}

// 画到底层 canvas，并把绘制区域的 canvas 一并返回，供裁剪用。
function __navisDrawToCanvas(scale) {
    var svg = __navisSvgNode();
    if (!svg) return null;
    var size = __navisSvgLogicalSize(svg);
    // 四周留白，避免个别标签/描边画到边界外被裁掉。
    var pad = 24;
    var W = Math.max(1, Math.round((size.w + pad * 2) * scale));
    var H = Math.max(1, Math.round((size.h + pad * 2) * scale));
    var canvas = document.createElement('canvas');
    canvas.width = W;
    canvas.height = H;
    var ctx = canvas.getContext('2d');
    var filledBg = null;
    try {
        var bg = getComputedStyle(document.body).backgroundColor;
        if (bg && bg !== 'transparent' && bg !== 'rgba(0, 0, 0, 0)') {
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, W, H);
            filledBg = bg;
        }
    } catch (e) { filledBg = null; }

    var c = svg.cloneNode(true);
    var hasViewBox = false;
    try { hasViewBox = !!(c.viewBox && c.viewBox.baseVal && c.viewBox.baseVal.width); } catch (e) {}
    if (!hasViewBox) { c.setAttribute('viewBox', '0 0 ' + size.w + ' ' + size.h); }
    c.setAttribute('width', String(size.w));
    c.setAttribute('height', String(size.h));

    return {
        svgNode: c, size: size, pad: pad, scale: scale,
        canvas: canvas, ctx: ctx, filledBg: filledBg
    };
}

// 根据 body 背景判断某像素是否属于"内容"（裁剪时空白的依据）。
// 有背景填充则比较颜色，无背景则看 alpha。
function __navisIsContentPixel(data, i, filledBg) {
    var a = data[i + 3];
    if (!filledBg) {
        return a > 40;
    }
    if (a < 240) return false;
    return Math.abs(data[i] - filledBg[0]) + Math.abs(data[i + 1] - filledBg[1]) +
           Math.abs(data[i + 2] - filledBg[2]) > 30;
}

function __navisComputeCropBox(ctx, W, H, filledBg) {
    var imgData;
    try { imgData = ctx.getImageData(0, 0, W, H); } catch (e) { return null; }
    var d = imgData.data;
    var minX = W, minY = H, maxX = -1, maxY = -1;
    var ref = null;
    if (filledBg) {
        var rgb = filledBg.match(/rgba?\(([^)]+)\)/);
        if (!rgb) {
            var hex = filledBg.replace('#', '');
            if (hex.length === 3) hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
            ref = [parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16)];
        } else {
            var parts = rgb[1].split(',');
            ref = [parseInt(parts[0], 10), parseInt(parts[1], 10), parseInt(parts[2], 10)];
        }
    }
    var step = 2; // 采样步长，提速且不影响边缘结果太多
    for (var y = 0; y < H; y += step) {
        var row = y * W * 4;
        for (var x = 0; x < W; x += step) {
            var i = row + x * 4;
            if (__navisIsContentPixel(d, i, ref)) {
                if (x < minX) minX = x;
                if (x > maxX) maxX = x;
                if (y < minY) minY = y;
                if (y > maxY) maxY = y;
            }
        }
    }
    if (maxX < 0) return null;
    return { x: minX, y: minY, w: maxX - minX + 1, h: maxY - minY + 1 };
}

function __navisExportRaster(mime, quality, scale, crop) {
    return new Promise(function (resolve, reject) {
        var canvas = __navisDrawToCanvas(scale);
        if (!canvas) { resolve(null); return; }
        var size = canvas.size, pad = canvas.pad, W = canvas.canvas.width, H = canvas.canvas.height;
        var ctx = canvas.ctx;
        var img = new Image();
        var url = URL.createObjectURL(new Blob(
            [new XMLSerializer().serializeToString(canvas.svgNode)],
            { type: 'image/svg+xml;charset=utf-8' }
        ));
        img.onload = function () {
            try {
                ctx.imageSmoothingEnabled = true;
                ctx.imageSmoothingQuality = 'high';
                var dw = size.w * scale, dh = size.h * scale;
                ctx.drawImage(img, pad * scale, pad * scale, dw, dh);
                var outCanvas = canvas.canvas;
                if (crop) {
                    var box = __navisComputeCropBox(ctx, W, H, canvas.filledBg);
                    if (box) {
                        outCanvas = document.createElement('canvas');
                        outCanvas.width = box.w;
                        outCanvas.height = box.h;
                        outCanvas.getContext('2d').drawImage(canvas.canvas, box.x, box.y, box.w, box.h, 0, 0, box.w, box.h);
                    }
                }
                resolve(outCanvas.toDataURL(mime, quality));
            } catch (e) { reject(e); }
            finally { URL.revokeObjectURL(url); }
        };
        img.onerror = function (e) { URL.revokeObjectURL(url); reject(e); };
        img.src = url;
    });
}
"""


class MermaidViewer(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Academic Diagram Viewer - Mermaid.js")
        self.resize(1200, 800)
        self.mermaid_code = ""

        # 主界面分割器：左侧源码，右侧预览
        self.splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(self.splitter)

        # 左侧：源码编辑器（默认折叠，可通过工具栏 "Toggle Source" 展开）
        self.source_editor = SourceCodeViewer(
            title="Mermaid Source Code",
            editable=True,
            collapsed=True,
            max_height=600,
        )
        self.source_editor.textChanged.connect(self._live_update)
        self.splitter.addWidget(self.source_editor)

        # 右侧：Web 引擎渲染器
        self.web_view = QWebEngineView()
        self.splitter.addWidget(self.web_view)

        self.splitter.setSizes([300, 900])

        self._setup_toolbar()

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()


    def _apply_theme(self):
        tm = ThemeManager()

        # 0. 独立顶层窗口的原生标题栏不继承主窗口深浅色状态，需自行设置
        apply_native_titlebar_theme(self, tm.current_theme == 'dark')

        # 1. 源码编辑器主题由 SourceCodeViewer 内部自适应，无需在此处理

        # 2. 更新工具栏
        tb_style = f"""
            QToolBar {{ background: {tm.color('bg_card')}; padding: 6px; border: none; border-bottom: 1px solid {tm.color('border')}; font-family: {tm.font_family()}; }} 
            QToolButton {{ color: {tm.color('text_main')}; padding: 5px 10px; border-radius: 4px; font-weight: bold; font-family: {tm.font_family()}; }} 
            QToolButton:hover {{ background: {tm.color('btn_hover')}; color: {tm.color('accent')}; }}
        """
        for tb in self.findChildren(QToolBar):
            tb.setStyleSheet(tb_style)

        # 3. 工具栏内的下拉框 / 复选框（原硬编码深色，浅色主题下成为暗块）
        combo_style = f"""
            QComboBox {{ color: {tm.color('text_main')}; background: {tm.color('bg_input')};
                         border: 1px solid {tm.color('border')}; border-radius: 4px;
                         padding: 2px 8px; font-family: {tm.font_family()}; }}
            QComboBox:hover {{ border-color: {tm.color('accent')}; }}
            QComboBox QAbstractItemView {{ color: {tm.color('text_main')};
                         background: {tm.color('bg_card')};
                         border: 1px solid {tm.color('border')};
                         selection-background-color: {tm.color('accent')};
                         selection-color: {tm.color('bg_main')}; }}
        """
        combo = getattr(self, 'scale_combo', None)
        if combo is not None:
            combo.setStyleSheet(combo_style)
        theme_combo = getattr(self, 'theme_combo', None)
        if theme_combo is not None:
            theme_combo.setStyleSheet(combo_style)
        chk = getattr(self, 'chk_crop', None)
        if chk is not None:
            chk.setStyleSheet(
                f"QCheckBox {{ color: {tm.color('text_main')}; padding-left: 4px; "
                f"font-family: {tm.font_family()}; }}")

        # 4. 工具栏图标随主题重新着色
        for attr, icon_name in (('act_source', 'edit'), ('act_zoom_in', 'add'),
                                ('act_zoom_out', 'remove'), ('act_reset_zoom', 'refresh'),
                                ('act_export', 'download')):
            action = getattr(self, attr, None)
            if action is not None:
                action.setIcon(tm.icon(icon_name, 'text_main'))

        if self.mermaid_code:
            self.render_diagram()


    def _setup_toolbar(self):
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, tb)

        # 主题切换
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["default", "dark", "forest", "neutral", "base"])
        self.theme_combo.currentTextChanged.connect(self.render_diagram)
        tb.addWidget(self.theme_combo)

        tb.addSeparator()
        tm = ThemeManager()
        # 功能按钮（保存 action 引用，主题切换时刷新图标与文案样式）
        self.act_source = tb.addAction(tm.icon("edit", "text_main"), "Toggle Source")
        self.act_source.triggered.connect(self._toggle_source)

        self.act_zoom_in = tb.addAction(tm.icon("add", "text_main"), "Zoom In",
                                        lambda: self.web_view.setZoomFactor(self.web_view.zoomFactor() + 0.2))
        self.act_zoom_out = tb.addAction(tm.icon("remove", "text_main"), "Zoom Out",
                                         lambda: self.web_view.setZoomFactor(self.web_view.zoomFactor() - 0.2))
        self.act_reset_zoom = tb.addAction(tm.icon("refresh", "text_main"), "Reset Zoom",
                                           lambda: self.web_view.setZoomFactor(1.0))
        self.act_export = tb.addAction(tm.icon("download", "text_main"), "Export Image",
                                       self._export_image)

        tb.addSeparator()

        # 导出设置：倍率（清晰度）与自动裁剪空白。配色统一由 _apply_theme
        # 注入（原硬编码深色底在浅色主题下与周围工具栏割裂）。
        self.scale_combo = QComboBox()
        self.scale_combo.addItems(["1x", "2x", "3x", "4x"])
        self.scale_combo.setToolTip("Raster export scale (SVG export is always lossless)")
        tb.addWidget(self.scale_combo)

        self.chk_crop = QCheckBox("Crop Whitespace")
        self.chk_crop.setToolTip("Automatically trim blank margins around the diagram")
        self.chk_crop.setChecked(True)
        tb.addWidget(self.chk_crop)

        tb.addSeparator()


    def _export_image(self):
        filters = ("PNG Images (*.png);;JPEG Images (*.jpg);;WebP Images (*.webp);;"
                   "SVG Vector Graphics (*.svg)")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Diagram", "academic_diagram.png", filters
        )

        if not path:
            return

        lower = path.lower()
        if lower.endswith('.svg'):
            self.web_view.page().runJavaScript(
                "__navisExportSVG();",
                lambda res: self._save_svg_content(res, path)
            )
            return

        # 位图：基于矢量 SVG 按真实尺寸 × 缩放系数光栅化，分辨率不依赖窗口/缩放，
        # 且不受滚动条与可视区裁切影响，保证导出高清。
        if lower.endswith('.png'):
            mime, quality = 'image/png', 1.0
        elif lower.endswith(('.jpg', '.jpeg')):
            mime, quality = 'image/jpeg', 0.95
        elif lower.endswith('.webp'):
            mime, quality = 'image/webp', 0.95
        else:
            StandardDialog(self, "Export Failed",
                           "Unsupported image extension. Please use PNG, JPG, WebP or SVG.").exec()
            return

        # 导出倍率：优先取工具栏选择（1x-4x）；未初始化时按 DPI 给至少 2x 保底。
        if getattr(self, 'scale_combo', None) is not None:
            text = self.scale_combo.currentText().lower().replace('x', '')
            try:
                scale = float(text)
            except (TypeError, ValueError):
                scale = 2.0
        else:
            dpr = max(1.0, float(self.web_view.devicePixelRatioF()))
            scale = min(4.0, max(2.0, dpr))

        crop = bool(getattr(self, 'chk_crop', None) is not None and self.chk_crop.isChecked())

        script = (
            "__navisExportRaster("
            f"'{mime}', {json.dumps(quality)}, {json.dumps(scale)}, {json.dumps(crop)}"
            ").then(function(d){ return d; }, function(e){ return null; });"
        )
        self.web_view.page().runJavaScript(script, lambda res: self._save_raster(res, path, lower))

    def _save_raster(self, data_url, path, lower):
        if not data_url:
            StandardDialog(self, "Export Failed",
                           "Could not export diagram. It might not be rendered yet.").exec()
            return

        try:
            if "," in data_url:
                _, b64 = data_url.split(",", 1)
            else:
                b64 = data_url
            raw = base64.b64decode(b64)
            with open(path, "wb") as f:
                f.write(raw)
            StandardDialog(self, "Success",
                           f"High-resolution diagram exported successfully to:\n{path}").exec()
        except Exception as e:
            StandardDialog(self, "Export Failed", f"Failed to save image:\n{str(e)}").exec()

    def _save_svg_content(self, html_content, path):
        if not html_content:
            StandardDialog(self, "Export Failed",
                           "Could not extract SVG data. The diagram might not be rendered yet.").exec()
            return

        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            StandardDialog(self, "Success", f"SVG Vector Diagram exported successfully to:\n{path}").exec()
        except Exception as e:
            StandardDialog(self, "Export Failed", f"Failed to save SVG:\n{str(e)}").exec()

    def load_diagram(self, mermaid_code: str):
        cleaned_code = mermaid_code.strip()

        if cleaned_code.lower().startswith("```mermaid"):
            cleaned_code = cleaned_code[10:]
        elif cleaned_code.startswith("```"):
            cleaned_code = cleaned_code[3:]

        if cleaned_code.endswith("```"):
            cleaned_code = cleaned_code[:-3]

        self.mermaid_code = cleaned_code.strip()

        self.source_editor.set_code(self.mermaid_code)

        self.render_diagram()
        self.showNormal()
        self.raise_()
        self.activateWindow()


    def _toggle_source(self):
        self.source_editor.toggle_collapsed()

    def _live_update(self):
        self.mermaid_code = self.source_editor.code()
        self.render_diagram()

    def render_diagram(self, theme=None):
        tm = ThemeManager()

        current_combo_theme = self.theme_combo.currentText()
        if current_combo_theme == "default":
            mermaid_theme = 'dark' if tm.current_theme == 'dark' else 'default'
        else:
            mermaid_theme = current_combo_theme

        safe_code = json.dumps(self.mermaid_code)

        js_path = tm.get_resource_path("assets", "js", "mermaid.min.js")
        js_uri = QUrl.fromLocalFile(js_path).toString()

        # SVG 只用于页内预览（等比缩入容器，避免超宽图产生横向滚动条）；
        # 导出始终走矢量/真实尺寸光栅化，不受此处 max-width 影响。
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                html, body {{
                    margin: 0; padding: 0;
                    background-color: {tm.color('bg_main')};
                }}
                body {{ overflow: auto; }}
                .mermaid {{
                    display: inline-block;
                    padding: 12px;
                    transform-origin: top left;
                }}
                #graphDiv svg {{
                    max-width: 100%;
                    height: auto;
                    display: block;
                }}
            </style>
            <script src="{js_uri}"></script>
        </head>
        <body>
            <div class="mermaid" id="graphDiv"></div>
            <script>
                mermaid.initialize({{ startOnLoad: false, theme: '{mermaid_theme}', securityLevel: 'loose' }});
                const code = {safe_code};

                async function draw() {{
                    try {{
                        const {{ svg }} = await mermaid.render('mermaid-svg', code);
                        document.getElementById('graphDiv').innerHTML = svg;
                    }} catch (e) {{
                        document.getElementById('graphDiv').innerHTML = `<pre style="color:{tm.color('danger')};">Error rendering graph:<br>${{e.message}}</pre>`;
                    }}
                }}
                draw();
            </script>
            <script>
                {_NAVIS_EXPORT_JS}
            </script>
        </body>
        </html>
        """

        base_url = QUrl.fromLocalFile(os.path.dirname(js_path) + "/")
        self.web_view.setHtml(html_content, base_url)