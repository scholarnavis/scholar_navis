"""内部文本查看器：纯文本 / Markdown / 结构化数据的统一阅读窗口。

与 :mod:`src.ui.components.pdf_viewer`（基于 QtWebEngine 的 PDF 阅读器）分离，
两者仅共用搜索工具栏（见 :class:`DocumentSearchBarMixin`）。

按扩展名选择渲染方式（见 :meth:`InternalTextViewer._render_content`）：

* Markdown：复用 :class:`TextFormatter` 的渲染管线；
* yaml / json / toml / xml 等：交给 Pygments 做语法高亮；
* .txt 及未知扩展名：纯文本展示（仅转义 + URL/DOI 可点击）。
"""

import html
import logging
import os
import re
import shutil

from PySide6.QtCore import Qt, QUrl, QEvent
from PySide6.QtGui import (QColor, QDesktopServices, QShortcut, QKeySequence,
                           QTextCharFormat, QTextCursor, QTextDocument)
from PySide6.QtWidgets import (QMainWindow, QToolBar, QMessageBox,
                               QTextBrowser, QLabel)

from src.core.file_types import MARKDOWN_EXTS
from src.core.signals import GlobalSignals
from src.core.theme_manager import ThemeManager, apply_native_titlebar_theme, strong_weight_css
from src.ui.components.document_search import DocumentSearchBarMixin
from src.ui.components.file_dialogs import save_file_name

logger = logging.getLogger(__name__)

#: 跳过语法高亮的体积阈值（字节）。Pygments 词法分析的耗时随文件体积近似
#: 线性增长，超大文件高亮会明显阻塞 UI 线程，此时直接退回纯文本展示。
HIGHLIGHT_MAX_BYTES = 1_500_000


class InternalTextViewer(DocumentSearchBarMixin, QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Text Document Viewer")
        self.resize(1000, 800)

        self.original_file_path = ""
        self.display_name = ""
        #: 最近一次加载的原始文本与其扩展名（主题切换时据此重渲染）
        self._raw_content = None
        self._content_ext = ""

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

    # ------------------------------------------------------------- 主题 ---
    def _apply_theme(self):
        tm = ThemeManager()

        self.setStyleSheet(f"QMainWindow {{ background-color: {tm.color('bg_main')}; }}")
        # 强制操作系统原生标题栏跟随深浅色模式
        apply_native_titlebar_theme(self, tm.current_theme == "dark")

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
                font-weight: {strong_weight_css()};
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
            self.lbl_search_count.setStyleSheet(f"color: {tm.color('text_main')}; font-weight: {strong_weight_css()}; padding: 0 10px;")

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

        # 5. 正文重渲染：Markdown 标题/代码块、Pygments 词法配色、纯文本链接色都是
        #    渲染期固化进 HTML 的内联样式，仅刷 QSS 不会更新，必须重建文档。
        #    用当前滚动位置包裹，避免主题切换后视线跳回文档顶部。
        if self._raw_content is not None:
            scrollbar = self.text_browser.verticalScrollBar()
            pos = scrollbar.value()
            self._render_content()
            scrollbar.setValue(min(pos, scrollbar.maximum()))

    # ----------------------------------------------------------- 工具栏 ---
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

    # --------------------------------------------------------- 读取 / 渲染 ---
    def load_document(self, file_path, highlight_text="", display_name=""):
        self.original_file_path = file_path
        self.display_name = display_name or os.path.basename(file_path)
        self.setWindowTitle(f"Text Viewer - {self.display_name}")

        try:
            self._raw_content = self._read_file_content(file_path)
            self._content_ext = os.path.splitext(file_path or "")[1].lower().lstrip(".")
            # 以文件所在目录作为相对资源基准：Markdown 里引用的本地图片等才能解析
            self.text_browser.setSearchPaths([os.path.dirname(os.path.abspath(file_path))])
            self._render_content()

            if highlight_text:
                self._highlight_and_scroll(highlight_text)

            self.show()
            self.raise_()
            self.activateWindow()
        except Exception as e:
            logger.exception("Failed to open text document: %s", e)
            QMessageBox.critical(self, "Error", f"Failed to open text document:\n{e}")

    @staticmethod
    def _read_file_content(file_path):
        """读取文件文本：DOCX 走 python-docx，其余按探测到的编码解码。"""
        if (file_path or "").lower().endswith('.docx'):
            try:
                import docx
                doc = docx.Document(file_path)
                return "\n".join([p.text for p in doc.paragraphs])
            except Exception as e:
                logger.error("Failed to read DOCX %s: %s", file_path, e)
                return f"Error reading DOCX:\n{e}"

        try:
            import chardet
            with open(file_path, 'rb') as f:
                raw_data = f.read()
            detected = chardet.detect(raw_data) or {}
            encoding = detected.get('encoding') or 'utf-8'
            return raw_data.decode(encoding, errors='replace')
        except Exception as e:
            logger.error("Failed to read text file %s: %s", file_path, e)
            return f"Error reading text file:\n{e}"

    def _render_content(self):
        """按扩展名选择渲染方式并写入浏览器。

        * Markdown（:data:`src.core.file_types.MARKDOWN_EXTS`）：复用项目内
          Markdown 渲染管线（标题 / 列表 / 表格 / 代码块 / LaTeX / 化学式等，
          与聊天气泡同源）；
        * 其余：尝试用 Pygments 做语法高亮（yaml / json / toml / xml 等配置与
          标记语言、脚本）；
        * 高亮不可用或本就无需高亮（.txt、未知扩展名）时回退为纯文本展示。

        三种方式都会把主题色以**内联样式**固化进 HTML，因此主题切换时必须
        重新调用本方法（见 :meth:`_apply_theme`）。
        """
        content = self._raw_content or ""
        ext = self._content_ext
        file_name = self.display_name or self.original_file_path
        mode = self._resolve_render_mode(ext, file_name)

        if mode == "markdown":
            html_content = self._render_markdown(content)
        elif mode == "highlight":
            # 高亮分支仍可能因空内容 / 超大体积 / Pygments 异常而放弃，届时回退纯文本
            html_content = (self._render_highlighted(content, file_name)
                            or self._plain_text_to_html(content))
        else:
            html_content = self._plain_text_to_html(content)

        self.text_browser.setHtml(html_content)
        logger.debug("Text viewer render: file=%s ext=%s mode=%s chars=%d",
                     file_name, ext or "<none>", mode, len(content))

    @staticmethod
    def _lexer_for(file_name):
        """解析文件名对应的 Pygments 词法器；无需高亮或不可用时返回 ``None``。

        **只按文件名判定**，不把内容作为二次消歧输入：渲染模式必须只由扩展名
        决定，否则同一文件在"模式判定"与"实际渲染"两处可能得出不同结果。
        ``.txt`` 这类只命中 ``TextLexer``（不产生任何高亮 token）的文件返回
        ``None``，交由纯文本分支处理。
        """
        try:
            from pygments.lexers import TextLexer, get_lexer_for_filename
            from pygments.util import ClassNotFound
        except ImportError:
            return None

        try:
            lexer = get_lexer_for_filename(file_name)
        except ClassNotFound:
            return None
        except Exception as e:
            logger.warning("Lexer lookup failed for %s: %s", file_name, e)
            return None
        return None if isinstance(lexer, TextLexer) else lexer

    @classmethod
    def _resolve_render_mode(cls, ext: str, file_name: str) -> str:
        """扩展名 → 渲染模式：``markdown`` / ``highlight`` / ``plain``。

        :meth:`_render_content` 与开发者模式自检共用本判定——测试断言的就是
        实际执行的那份逻辑，不会出现"测试覆盖的判定"与"线上判定"各写一份而
        悄悄漂移。
        """
        if ext in MARKDOWN_EXTS:
            return "markdown"
        return "highlight" if cls._lexer_for(file_name) is not None else "plain"

    @staticmethod
    def _render_markdown(content):
        """复用项目内 Markdown 渲染管线（唯一实现在 TextFormatter）。"""
        from src.ui.components.text_formatter import TextFormatter
        return TextFormatter.markdown_to_html(content)

    @classmethod
    def _render_highlighted(cls, content, file_name):
        """用 Pygments 生成语法高亮 HTML；不可用 / 无需高亮时返回 None。

        统一以 ``noclasses=True`` 输出**内联样式**——QTextBrowser 不解析外部
        CSS 类，只有内联 ``style="color:..."`` 才会生效；外层再套一个主题化的
        ``<pre>``（等宽字体 + 主题代码底色），否则深浅主题下会有"浅底配亮字"
        之类的对比度问题。
        """
        if not content.strip():
            return None
        if len(content.encode('utf-8', errors='ignore')) > HIGHLIGHT_MAX_BYTES:
            logger.info("Skip syntax highlighting for oversized file: %s", file_name)
            return None

        try:
            from pygments import highlight as _pygments_highlight
            from pygments.formatters import HtmlFormatter
        except ImportError as e:
            logger.warning("Pygments unavailable; falling back to plain text: %s", e)
            return None

        lexer = cls._lexer_for(file_name)
        if lexer is None:
            return None

        tm = ThemeManager()
        formatter = HtmlFormatter(
            nowrap=True,
            noclasses=True,
            style="monokai" if tm.current_theme == "dark" else "friendly",
        )
        try:
            body = _pygments_highlight(content, lexer, formatter)
        except Exception as e:
            logger.warning("Pygments highlighting failed for %s: %s", file_name, e)
            return None

        from src.ui.components.text_formatter import mono_font_family_css
        return (
            f'<pre style="background-color:{tm.color("code_bg")}; '
            f'color:{tm.color("code_fg")}; font-family:{mono_font_family_css()}; '
            f'font-size:13px; margin:0;">{body}</pre>'
        )

    @staticmethod
    def _plain_text_to_html(content):
        """纯文本 → HTML：转义并按既有规则把 URL / DOI / Markdown 链接变为可点击。

        不做任何 Markdown 解释，保证 ".txt 就是纯文本" 的观感。
        """
        accent_color = ThemeManager().color('accent')

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
            if not part:
                continue
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

        return "".join(html_chunks)

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
            fmt = QTextCharFormat()
            hl_color = QColor(ThemeManager().color('warning'))
            hl_color.setAlpha(120)
            fmt.setBackground(hl_color)
            cursor.mergeCharFormat(fmt)

            self.text_browser.setTextCursor(cursor)
            self.text_browser.ensureCursorVisible()

    # --------------------------------------------------------- 交互 / 搜索 ---
    def _trigger_translation(self):
        """获取选中文本并触发翻译信号"""
        cursor = self.text_browser.textCursor()
        selected_text = cursor.selectedText().strip()
        if selected_text and hasattr(GlobalSignals(), 'sig_invoke_translator'):
            clean_text = selected_text.replace('\u2029', '\n')
            GlobalSignals().sig_invoke_translator.emit(clean_text)

    def _do_search(self, forward=True):
        """通用搜索逻辑：带循环查找和精确计数展示"""
        text = self.search_input.text()
        if not text:
            self.lbl_search_count.setText(" 0 / 0 ")
            self._clear_search()
            return

        flags = QTextDocument.FindFlag(0) if forward else QTextDocument.FindBackward
        found = self.text_browser.find(text, flags)

        if not found:
            # 没找到则从头或从尾巴循环跳转
            self.text_browser.moveCursor(QTextCursor.Start if forward else QTextCursor.End)
            found = self.text_browser.find(text, flags)

        if found:
            # 统计匹配总数，以及当前选中项是第几个
            lower_text = self.text_browser.toPlainText().lower()
            target = text.lower()
            total = lower_text.count(target)
            pos = self.text_browser.textCursor().position()
            current = lower_text[:pos].count(target)

            self.lbl_search_count.setText(f" {current} / {total} ")
        else:
            self.lbl_search_count.setText(" 0 / 0 ")

    def _find_next(self):
        self._do_search(forward=True)

    def _find_prev(self):
        self._do_search(forward=False)

    def _clear_search(self):
        """清除搜索选中态（关闭搜索栏时由 mixin 调用）。"""
        cursor = self.text_browser.textCursor()
        cursor.clearSelection()
        self.text_browser.setTextCursor(cursor)

    # --------------------------------------------------------- 外部工具 ---
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
        save_path, _ = save_file_name(self, "Export Original File", self.display_name, "All Files (*.*)")
        if save_path:
            try:
                shutil.copy2(self.original_file_path, save_path)
                QMessageBox.information(self, "Success", f"File exported successfully to:\n{save_path}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to export file:\n{str(e)}")
