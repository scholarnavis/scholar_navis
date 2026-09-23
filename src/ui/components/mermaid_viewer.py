import base64
import json
import logging
import os

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QMainWindow, QToolBar, QCheckBox,
                               QComboBox, QSplitter)

from src.core.theme_manager import ThemeManager, apply_native_titlebar_theme, strong_weight_css
# 🌟 引入你的自定义 Dialog
from src.ui.components.dialog import StandardDialog
from src.ui.components.file_dialogs import save_file_name
from src.ui.components.source_code_viewer import SourceCodeViewer

logger = logging.getLogger(__name__)


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
    // 预览时脚本会给 SVG 写入内联 style（width/height 适配容器宽度），克隆体若
    // 带着这些样式，导出会按"预览尺寸"而不是矢量真实尺寸光栅化（画布尺寸与实际
    // 内容不匹配 → 被裁切或糊）。这里先清掉尺寸类内联样式，再按真实尺寸设置属性。
    try { c.style.width = ''; c.style.height = ''; c.style.maxWidth = ''; } catch (e) {}
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
    #: 源码面板 : 预览面板 的默认宽度分配（源码展开时使用）。
    _SPLIT_SIZES = (300, 900)

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
        # 折叠态下源码面板只剩一条标题栏，却仍占着一整列宽度：预览区会白白
        # 让出约四分之一窗口（观感上就是"图形显示区域只有一点点"）。因此折叠时
        # 把面板整块移出分割器，把宽度全部还给预览区，展开时再按原比例放回。
        self.source_editor.collapsedChanged.connect(self._on_source_collapsed)
        self.splitter.addWidget(self.source_editor)

        # 右侧：Web 引擎渲染器
        self.web_view = QWebEngineView()
        self.splitter.addWidget(self.web_view)

        self.splitter.setSizes(list(self._SPLIT_SIZES))
        self._apply_source_pane()

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
            QToolButton {{ color: {tm.color('text_main')}; padding: 5px 10px; border-radius: 4px; font-weight: {strong_weight_css()}; font-family: {tm.font_family()}; }} 
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
                                ('act_zoom_out', 'remove'), ('act_fit', 'refresh'),
                                ('act_actual', 'check'), ('act_export', 'download')):
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

        # 缩放/平移与 image_viewer 对齐：按钮缩放图形本身（1.25 步进），滚轮在页面内
        # 以鼠标为锚点缩放（1.15 步进），Fit 为"整图缩入窗口"，1:1 回到矢量原始像素。
        self.act_zoom_in = tb.addAction(tm.icon("add", "text_main"), "Zoom In",
                                        lambda: self._run_js("navisZoomIn();"))
        self.act_zoom_out = tb.addAction(tm.icon("remove", "text_main"), "Zoom Out",
                                         lambda: self._run_js("navisZoomOut();"))
        self.act_fit = tb.addAction(tm.icon("refresh", "text_main"), "Fit to Window",
                                    lambda: self._run_js("navisFitToWindow();"))
        self.act_actual = tb.addAction(tm.icon("check", "text_main"), "Actual Size (1:1)",
                                       lambda: self._run_js("navisSetScale(1.0);"))
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
        path, _ = save_file_name(
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

        # 先显示窗口再渲染：预览页的视口尺寸取自 QWebEngineView 的实际几何，
        # 若在窗口尚未完成布局时 setHtml，页面首次 fit 会用到中间态尺寸（甚至
        # 0），图形被缩成很小一条且其后不一定再有 resize 事件来纠正。
        self.showNormal()
        self.render_diagram()
        self.raise_()
        self.activateWindow()

    def _toggle_source(self):
        self.source_editor.toggle_collapsed()

    def _on_source_collapsed(self, collapsed: bool):
        """源码面板折叠状态变化 → 重新分配分割器宽度。"""
        self._apply_source_pane()

    def _apply_source_pane(self):
        """折叠时隐藏源码面板（宽度全给预览），展开时恢复左右分栏比例。

        QSplitter 不会因为子控件自身折叠就回收该列宽度，直接把面板从分割器
        中隐去，预览区才能拿到整个窗口宽度。
        """
        collapsed = self.source_editor.is_collapsed()
        self.source_editor.setVisible(not collapsed)
        if not collapsed:
            self.splitter.setSizes(list(self._SPLIT_SIZES))
        logger.debug("Mermaid source pane collapsed=%s", collapsed)

    def _run_js(self, script: str):
        """在预览页执行脚本（页面尚未创建时静默忽略）。

        缩放/平移状态全部由页面内的 JS 持有（与 image_viewer 的交互一致），
        Qt 侧只负责转发工具栏动作，避免两套缩放（zoomFactor 与图形缩放）叠加。
        """
        page = self.web_view.page()
        if page is not None:
            page.runJavaScript(script)

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

        # 预览交互对齐 image_viewer（图片查看器）：滚轮以鼠标为锚点缩放（步进 1.15）、
        # 左键拖拽平移（抓手光标）、Shift+滚轮保留原生水平滚动、缩放区间 0.05~16x、
        # "适应窗口"模式下窗口变化自动重适配。平移实现为改变滚动位置，因此滚动条与
        # 拖动天然同步。导出始终走矢量/真实尺寸光栅化，不受预览缩放影响。
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
                #graphDiv {{
                    padding: 12px;
                    box-sizing: border-box;
                    cursor: grab;
                }}
                /* 拖动中整页维持抓手态，避免指针移出图形后变回箭头 */
                body.navis-dragging, body.navis-dragging * {{ cursor: grabbing !important; }}
                #graphDiv svg {{
                    display: block;
                    max-width: none;
                    height: auto;
                    user-select: none;
                }}
            </style>
            <script src="{js_uri}"></script>
        </head>
        <body>
            <div id="graphDiv"></div>
            <script>
                mermaid.initialize({{
                    startOnLoad: false,
                    theme: '{mermaid_theme}',
                    securityLevel: 'loose',
                    useMaxWidth: false,
                    flowchart: {{ useMaxWidth: false, htmlLabels: true }},
                    sequence: {{ useMaxWidth: false }},
                    gantt: {{ useMaxWidth: false }},
                    state: {{ useMaxWidth: false }},
                    class: {{ useMaxWidth: false }},
                    er: {{ useMaxWidth: false }},
                    mindmap: {{ useMaxWidth: false }},
                    timeline: {{ useMaxWidth: false }}
                }});
                const code = {safe_code};

                // ---- 缩放/平移参数，与 image_viewer 保持一致 ----
                const NAVIS_MIN_SCALE = 0.05;          // 缩放区间 0.05 ~ 16x
                const NAVIS_MAX_SCALE = 16.0;
                const NAVIS_WHEEL_ZOOM_FACTOR = 1.15;  // 滚轮步进（比按钮更细腻）
                const NAVIS_BUTTON_ZOOM_FACTOR = 1.25; // 工具栏按钮步进
                const NAVIS_MARGIN = 24;               // 适应窗口时预留的边距
                const NAVIS_MIN_VIEWPORT = 120;        // 视口小于此值视为"尚未布局完成"
                const NAVIS_FIT_RETRY_MS = [0, 60, 200]; // fit 收敛重试阶梯（ms）
                const NAVIS_FIT_MAX_ROUND = 8;         // 重试轮数上限，避免空转

                var navisNatural = {{ w: 0, h: 0 }};   // 矢量真实尺寸（1:1 像素）
                var navisScale = 1.0;
                var navisFitMode = true;               // 适应窗口模式：随窗口变化重适配
                var navisLastViewport = {{ w: 0, h: 0 }}; // 上次 fit 时的视口，用于去重

                function navisSvg() {{ return document.querySelector('#graphDiv svg'); }}

                function navisMeasure(svg) {{
                    var w = 0, h = 0;
                    try {{
                        var vb = svg.viewBox && svg.viewBox.baseVal;
                        if (vb && vb.width) {{ w = vb.width; h = vb.height; }}
                    }} catch (e) {{ /* ignore */ }}
                    if (!w || !h) {{
                        var r = svg.getBoundingClientRect();
                        w = w || r.width; h = h || r.height;
                    }}
                    return {{ w: w || 800, h: h || 600 }};
                }}

                function navisApplyScale(scale) {{
                    var svg = navisSvg();
                    if (!svg || !navisNatural.w) return;
                    navisScale = Math.max(NAVIS_MIN_SCALE, Math.min(NAVIS_MAX_SCALE, scale));
                    svg.style.width = Math.round(navisNatural.w * navisScale) + 'px';
                    svg.style.height = Math.round(navisNatural.h * navisScale) + 'px';
                }}

                // ---- 供工具栏调用 ----
                function navisSetScale(scale) {{ navisFitMode = false; navisApplyScale(scale); }}
                function navisZoomBy(factor) {{ navisSetScale(navisScale * factor); }}
                function navisZoomIn() {{ navisZoomBy(NAVIS_BUTTON_ZOOM_FACTOR); }}
                function navisZoomOut() {{ navisZoomBy(1 / NAVIS_BUTTON_ZOOM_FACTOR); }}
                function navisViewport() {{
                    return {{ w: document.documentElement.clientWidth,
                             h: document.documentElement.clientHeight }};
                }}

                // 视口过小说明页面还没完成布局（窗口刚创建 / 分割器刚调整）。
                // 此刻算出的缩放会极小，而且之后不一定再有 resize 事件纠正，
                // 图形就"缩成一条"且再也回不来——因此改为稍后重试。
                function navisFitToWindow() {{
                    var svg = navisSvg();
                    if (!svg) return;
                    if (!navisNatural.w) navisNatural = navisMeasure(svg);
                    var vp = navisViewport();
                    if (vp.w < NAVIS_MIN_VIEWPORT || vp.h < NAVIS_MIN_VIEWPORT) {{
                        navisScheduleFit();
                        return;
                    }}
                    var availW = Math.max(NAVIS_MIN_VIEWPORT, vp.w - NAVIS_MARGIN);
                    var availH = Math.max(NAVIS_MIN_VIEWPORT, vp.h - NAVIS_MARGIN);
                    navisFitMode = true;
                    navisLastViewport = vp;
                    navisApplyScale(Math.min(availW / navisNatural.w, availH / navisNatural.h));
                }}

                // fit 收敛重试：首帧视口常是中间态，按阶梯补算几次直到尺寸稳定。
                var navisFitTimers = [];
                var navisFitRound = 0;
                function navisScheduleFit() {{
                    if (navisFitTimers.length) return;        // 已有待执行的收敛序列
                    if (navisFitRound >= NAVIS_FIT_MAX_ROUND) return;
                    navisFitRound += 1;
                    NAVIS_FIT_RETRY_MS.forEach(function (ms) {{
                        navisFitTimers.push(setTimeout(function () {{
                            navisFitTimers.shift();
                            if (navisFitMode) navisFitToWindow();
                        }}, ms));
                    }});
                }}

                // 视口尺寸变化即重算（仅"适应窗口"模式）：ResizeObserver 覆盖
                // "首帧拿到中间态尺寸、其后不再有 resize 事件"的情况。
                function navisFitOnViewportChange() {{
                    var vp = navisViewport();
                    if (vp.w === navisLastViewport.w && vp.h === navisLastViewport.h) return;
                    if (navisFitMode) navisFitToWindow();
                }}

                // 以鼠标位置为锚点缩放，并回写滚动位置（等价 image_viewer._zoom_anchored）
                function navisZoomAnchored(clientX, clientY, factor) {{
                    var oldScale = navisScale;
                    var contentX = window.scrollX + clientX;
                    var contentY = window.scrollY + clientY;
                    navisSetScale(oldScale * factor);
                    var ratio = navisScale / oldScale;
                    if (ratio !== 1) {{
                        void document.body.offsetWidth;   // 强制布局，避免 scrollTo 被旧滚动范围夹取
                        window.scrollTo(Math.max(0, Math.round(contentX * ratio - clientX)),
                                        Math.max(0, Math.round(contentY * ratio - clientY)));
                    }}
                }}

                // ---- 滚轮缩放；Shift+滚轮保留原生水平滚动 ----
                window.addEventListener('wheel', function (e) {{
                    if (e.shiftKey || e.deltaY === 0) return;
                    e.preventDefault();
                    navisZoomAnchored(e.clientX, e.clientY,
                                      e.deltaY < 0 ? NAVIS_WHEEL_ZOOM_FACTOR : 1 / NAVIS_WHEEL_ZOOM_FACTOR);
                }}, {{ passive: false }});

                // ---- 左键拖拽平移（改滚动位置 → 与滚动条同步）----
                var navisDrag = {{ active: false, x: 0, y: 0 }};
                window.addEventListener('mousedown', function (e) {{
                    if (e.button !== 0 || !e.target.closest('#graphDiv') || !navisSvg()) return;
                    navisDrag.active = true; navisDrag.x = e.clientX; navisDrag.y = e.clientY;
                    document.body.classList.add('navis-dragging');
                    e.preventDefault();   // 避免拖拽时选中 SVG 文本
                }});
                window.addEventListener('mousemove', function (e) {{
                    if (!navisDrag.active) return;
                    window.scrollBy(-(e.clientX - navisDrag.x), -(e.clientY - navisDrag.y));
                    navisDrag.x = e.clientX; navisDrag.y = e.clientY;
                    e.preventDefault();
                }});
                window.addEventListener('mouseup', function () {{
                    if (!navisDrag.active) return;
                    navisDrag.active = false;
                    document.body.classList.remove('navis-dragging');
                }});

                // 窗口尺寸变化：仅"适应窗口"模式重算（手动缩放后尊重用户选择）
                window.addEventListener('resize', navisFitOnViewportChange);
                window.addEventListener('load', navisFitOnViewportChange);
                try {{
                    new ResizeObserver(navisFitOnViewportChange).observe(document.documentElement);
                }} catch (e) {{ /* 旧内核无 ResizeObserver：退回 resize 事件 */ }}

                async function draw() {{
                    try {{
                        navisFitRound = 0;   // 每次重新渲染都重置收敛重试预算
                        const {{ svg }} = await mermaid.render('mermaid-svg', code);
                        document.getElementById('graphDiv').innerHTML = svg;
                        // 等容器完成布局，再取矢量真实尺寸并适应窗口
                        requestAnimationFrame(function () {{
                            navisNatural = navisMeasure(navisSvg());
                            navisFitToWindow();
                            navisScheduleFit();   // 首帧视口可能是中间态，阶梯补算
                        }});
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