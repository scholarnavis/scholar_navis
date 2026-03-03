import json
import os

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QMainWindow, QToolBar,
                               QFileDialog, QComboBox, QSplitter, QTextEdit)

from src.core.theme_manager import ThemeManager
# 🌟 引入你的自定义 Dialog
from src.ui.components.dialog import StandardDialog


class MermaidViewer(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Academic Diagram Viewer - Mermaid.js")
        self.resize(1200, 800)
        self.mermaid_code = ""

        # 主界面分割器：左侧源码，右侧预览
        self.splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(self.splitter)

        # 左侧：源码编辑器 (默认隐藏)
        self.source_editor = QTextEdit()
        self.source_editor.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4; font-family: Consolas, monospace;")
        self.source_editor.textChanged.connect(self._live_update)
        self.source_editor.setVisible(False)
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

        # 1. 更新源码编辑器
        self.source_editor.setStyleSheet(f"""
            background-color: {tm.color('bg_input')}; 
            color: {tm.color('text_main')}; 
            font-family: Consolas, monospace;
            border: 1px solid {tm.color('border')};
        """)

        # 2. 更新工具栏
        tb_style = f"""
            QToolBar {{ background: {tm.color('bg_card')}; padding: 6px; border: none; border-bottom: 1px solid {tm.color('border')}; }} 
            QToolButton {{ color: {tm.color('text_main')}; padding: 5px 10px; border-radius: 4px; font-weight: bold; }} 
            QToolButton:hover {{ background: {tm.color('btn_hover')}; color: {tm.color('accent')}; }}
        """
        for tb in self.findChildren(QToolBar):
            tb.setStyleSheet(tb_style)

        # 3. 如果当前有图表代码，重新渲染以应用新的网页背景色
        if self.mermaid_code:
            self.render_diagram()


    def _setup_toolbar(self):
        tb = QToolBar()
        tb.setMovable(False)
        tb.setStyleSheet("QToolBar { background: #333; padding: 6px; border: none; } "
                         "QToolButton { color: white; padding: 5px 10px; border-radius: 4px; font-weight: bold; } "
                         "QToolButton:hover { background: #444; color: #05B8CC; }")
        self.addToolBar(Qt.TopToolBarArea, tb)

        # 主题切换
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["default", "dark", "forest", "neutral", "base"])
        self.theme_combo.currentTextChanged.connect(self.render_diagram)
        tb.addWidget(self.theme_combo)

        tb.addSeparator()
        tm = ThemeManager()
        # 功能按钮
        act_source = tb.addAction(tm.icon("edit", "text_main"), "Toggle Source")
        act_source.triggered.connect(self._toggle_source)

        tb.addAction(tm.icon("add", "text_main"), "Zoom In",
                     lambda: self.web_view.setZoomFactor(self.web_view.zoomFactor() + 0.2))
        tb.addAction(tm.icon("remove", "text_main"), "Zoom Out",
                     lambda: self.web_view.setZoomFactor(self.web_view.zoomFactor() - 0.2))
        tb.addAction(tm.icon("refresh", "text_main"), "Reset Zoom", lambda: self.web_view.setZoomFactor(1.0))

        tb.addSeparator()

        tb.addAction("📥 Export Image", self._export_image)

    def _export_image(self):
        filters = "PNG Images (*.png);;JPEG Images (*.jpg);;WebP Images (*.webp);;SVG Vector Graphics (*.svg)"
        path, selected_filter = QFileDialog.getSaveFileName(
            self, "Export Diagram", "academic_diagram.png", filters
        )

        if not path:
            return

        if path.lower().endswith('.svg'):
            self.web_view.page().runJavaScript(
                "document.getElementById('graphDiv').innerHTML;",
                lambda html: self._save_svg_content(html, path)
            )
        else:
            # 位图导出逻辑
            success = self.web_view.grab().save(path)
            if success:
                StandardDialog(self, "Success", f"Diagram exported successfully to:\n{path}").exec()
            else:
                msg = ("Failed to save image.\n\n"
                       "Your Python Qt environment might be missing the codec for this format. "
                       "Please try exporting as PNG, JPG, WebP, or SVG instead.")
                StandardDialog(self, "Export Failed", msg).exec()

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

        self.source_editor.blockSignals(True)
        self.source_editor.setPlainText(self.mermaid_code)
        self.source_editor.blockSignals(False)

        self.render_diagram()
        self.showNormal()
        self.raise_()
        self.activateWindow()


    def _toggle_source(self):
        self.source_editor.setVisible(not self.source_editor.isVisible())

    def _live_update(self):
        self.mermaid_code = self.source_editor.toPlainText()
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

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ 
                    background-color: {tm.color('bg_main')}; 
                    display: flex; justify-content: center; align-items: center; 
                    height: 100vh; margin: 0; overflow: auto; 
                }}
                .mermaid {{ transform-origin: top left; }}
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
        </body>
        </html>
        """

        base_url = QUrl.fromLocalFile(os.path.dirname(js_path) + "/")
        self.web_view.setHtml(html_content, base_url)