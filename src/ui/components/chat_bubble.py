import hashlib
import os
import re
import tempfile
import time

from PySide6.QtCore import Qt, Signal, QEvent, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QTextEdit, QPushButton, QFrame, QSizePolicy, QMenu, QScrollArea, QTextBrowser)

from src.core.core_task import TaskManager, TaskMode
from src.core.theme_manager import ThemeManager
from src.task.chat_tasks import DownloadImageTask
from src.ui.components.text_formatter import TextFormatter
from src.ui.components.toast import ToastManager


def hex_to_rgba(hex_color, alpha):
    hex_color = hex_color.lstrip('#')
    if len(hex_color) == 3:
        hex_color = ''.join([c * 2 for c in hex_color])
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"


class ChatBubbleWidget(QWidget):
    # --- 1. 新增消息类型常量 (请放在类属性最顶端) ---
    MSG_USER = 1
    MSG_AI = 2
    MSG_ERROR = 3

    sig_edit_confirmed = Signal(int, str)
    sig_link_clicked = Signal(str)
    sig_retry_clicked = Signal(int)

    # --- 2. 完整的 __init__ 方法 ---
    def __init__(self, text, is_user, index, context_html=None, parent=None, msg_type=None):
        super().__init__(parent)
        self.original_text = text
        self.is_user = is_user
        self.index = index
        self.context_html = context_html

        if msg_type is not None:
            self.msg_type = msg_type
        else:
            self.msg_type = self.MSG_USER if is_user else self.MSG_AI

        self.is_editing = False
        self._can_edit = True
        self.is_interrupted = False

        self.loading_timer = QTimer(self)
        self.loading_timer.timeout.connect(self._animate_loading)
        self.loading_dots = 0
        self.is_loading = False

        self.downloaded_images = {}
        self.downloading_urls = set()
        self.download_failed_urls = {}
        self.download_timeouts = {}
        self.image_task_mgrs = {}

        self.image_loading_timer = QTimer(self)
        self.image_loading_timer.timeout.connect(self._animate_image_loading)
        self.image_loading_dots = 0

        self.init_ui()
        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

    # --- 3. 完整的 init_ui 方法 ---
    def init_ui(self):
        tm = ThemeManager()

        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(10, 5, 10, 5)
        self.main_layout.setSpacing(10)

        self.spacer = QWidget()
        self.spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        self.content_container = QWidget()
        self.content_container.setObjectName("BubbleWrapper")
        self.content_container.setAttribute(Qt.WA_StyledBackground, True)
        self.content_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        self.content_layout = QVBoxLayout(self.content_container)
        self.content_layout.setContentsMargins(12, 12, 12, 12)
        self.content_layout.setSpacing(6)
        self.content_layout.setAlignment(Qt.AlignTop)

        font_family = tm.font_family()

        if self.context_html:
            self.ctx_frame = QFrame()
            self.ctx_frame.setObjectName("ContextFrame")
            ctx_layout = QVBoxLayout(self.ctx_frame)
            ctx_layout.setContentsMargins(10, 8, 10, 8)
            ctx_layout.setSpacing(4)

            self.ctx_header = QLabel("📎 Attached Context (Click to View)")

            self.ctx_content = QLabel()
            self.ctx_content.setTextFormat(Qt.RichText)
            self.ctx_content.setTextInteractionFlags(Qt.TextBrowserInteraction)
            self.ctx_content.setOpenExternalLinks(False)
            self.ctx_content.linkActivated.connect(self.sig_link_clicked.emit)
            self.ctx_content.setText(self.context_html)
            self.ctx_content.setWordWrap(True)

            self.ctx_frame.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
            self.ctx_content.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

            ctx_layout.addWidget(self.ctx_header)
            ctx_layout.addWidget(self.ctx_content)
            self.content_layout.addWidget(self.ctx_frame)

        self.lbl_text = QTextBrowser()
        self.lbl_text.setOpenExternalLinks(False)
        self.lbl_text.setOpenLinks(False)
        self.lbl_text.setFrameShape(QFrame.NoFrame)
        self.lbl_text.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lbl_text.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.lbl_text.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.lbl_text.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lbl_text.customContextMenuRequested.connect(self.show_context_menu)
        self.lbl_text.anchorClicked.connect(lambda url: self.sig_link_clicked.emit(url.toString()))

        self.lbl_text.document().documentLayout().documentSizeChanged.connect(self._adjust_browser_height)

        # 布局逻辑：MSG_ERROR 靠左（类似 AI 气泡）
        if self.msg_type == self.MSG_ERROR:
            self.main_layout.addWidget(self.content_container)
            self.main_layout.addWidget(self.spacer)
            btn_alignment = Qt.AlignLeft
        elif self.is_user:
            self.main_layout.addWidget(self.spacer)
            self.main_layout.addWidget(self.content_container)
            btn_alignment = Qt.AlignRight
        else:
            self.main_layout.addWidget(self.content_container)
            self.main_layout.addWidget(self.spacer)
            btn_alignment = Qt.AlignLeft

        self.set_content(self.original_text, msg_type=self.msg_type)

        self.edit_input = QTextEdit()
        self.edit_input.setVisible(False)
        self.edit_input.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.edit_input.installEventFilter(self)
        self.edit_input.textChanged.connect(self.adjust_edit_height)

        self.content_layout.addWidget(self.lbl_text)
        self.content_layout.addWidget(self.edit_input)

        self.btn_widget = QWidget()
        self.btn_layout = QHBoxLayout(self.btn_widget)
        self.btn_layout.setContentsMargins(0, 0, 5, 0)
        self.btn_layout.setSpacing(10)
        self.btn_layout.setAlignment(btn_alignment)

        self.btn_copy = QPushButton(" Copy")
        self.btn_copy.setIcon(tm.icon("copy", "text_muted"))
        self.btn_copy.setCursor(Qt.PointingHandCursor)
        self.btn_copy.clicked.connect(self.copy_plain_text)
        self.btn_layout.addWidget(self.btn_copy)

        self.btn_copy_md = QPushButton(" Copy MD")
        self.btn_copy_md.setIcon(tm.icon("markdown_copy", "text_muted"))
        self.btn_copy_md.setCursor(Qt.PointingHandCursor)
        self.btn_copy_md.clicked.connect(self.copy_markdown)
        self.btn_layout.addWidget(self.btn_copy_md)

        if not self.is_user and self.msg_type != self.MSG_ERROR:
            self.btn_bubble_retry = QPushButton(" Retry")
            self.btn_bubble_retry.setIcon(tm.icon("refresh", "warning"))
            self.btn_bubble_retry.setCursor(Qt.PointingHandCursor)
            self.btn_bubble_retry.setStyleSheet("""
                                QPushButton { background-color: transparent; border: none; color: #ff9800; font-size: 12px; padding: 2px 4px; border-radius: 4px; font-weight: bold;} 
                                QPushButton:hover { color: #fff; background-color: #f57c00; }
                            """)
            self.btn_bubble_retry.clicked.connect(lambda: self.sig_retry_clicked.emit(self.index))
            self.btn_bubble_retry.setVisible(False)
            self.btn_layout.addWidget(self.btn_bubble_retry)

        if self.is_user:
            self.btn_edit = QPushButton("Edit")
            self.btn_edit.setIcon(tm.icon("edit", "text_muted"))
            self.btn_edit.setCursor(Qt.PointingHandCursor)
            self.btn_edit.clicked.connect(self.toggle_edit)
            self.btn_layout.addWidget(self.btn_edit)

        self.content_layout.addWidget(self.btn_widget)

        if self.is_user:
            self.edit_btn_widget = QWidget()
            self.edit_btn_layout = QHBoxLayout(self.edit_btn_widget)
            self.edit_btn_layout.setContentsMargins(0, 0, 0, 0)
            self.edit_btn_layout.setSpacing(6)
            self.edit_btn_layout.setAlignment(btn_alignment)

            self.btn_cancel = QPushButton(" Cancel")
            self.btn_cancel.setIcon(tm.icon("close", "text_muted"))
            self.btn_cancel.clicked.connect(self.cancel_edit)

            self.btn_confirm = QPushButton(" Confirm")
            self.btn_confirm.setIcon(tm.icon("check-circle", "bg_main"))
            confirm_style = """
                QPushButton { background-color: #007acc; border: none; color: white; font-size: 12px; padding: 5px 12px; border-radius: 4px; font-weight: bold;} 
                QPushButton:hover { background-color: #005a9e; }
            """
            self.btn_confirm.setStyleSheet(confirm_style)
            self.btn_confirm.clicked.connect(self.save_edit)

            self.edit_btn_layout.addWidget(self.btn_cancel)
            self.edit_btn_layout.addWidget(self.btn_confirm)

            self.edit_btn_widget.setVisible(False)
            self.content_layout.addWidget(self.edit_btn_widget)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        parent = self.parentWidget()
        if parent:
            max_w = int(parent.width() * 0.80)

            if self.lbl_text.maximumWidth() != max_w:
                self.lbl_text.setMaximumWidth(max_w)
                if hasattr(self, 'edit_input'):
                    self.edit_input.setMaximumWidth(max_w)

                self.lbl_text.updateGeometry()

        self._adjust_browser_height()

    def adjust_edit_height(self):
        doc_h = int(self.edit_input.document().size().height())
        new_h = doc_h + 14
        if new_h > 350:
            self.edit_input.setFixedHeight(350)
            self.edit_input.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        else:
            self.edit_input.setFixedHeight(max(40, new_h))
            self.edit_input.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def _animate_image_loading(self):
        if not self.downloading_urls:
            self.image_loading_timer.stop()
            return

        self.image_loading_dots = (self.image_loading_dots + 1) % 4

        current_time = time.time()
        timed_out_urls = []
        for url in list(self.downloading_urls):
            if current_time > self.download_timeouts.get(url, current_time + 30):
                timed_out_urls.append(url)

        if timed_out_urls:
            for url in timed_out_urls:
                self.downloading_urls.remove(url)
                if getattr(self, 'download_failed_urls', None) is None:
                    self.download_failed_urls = {}
                self.download_failed_urls[url] = "网络下载超时 (Timeout)"

        self.set_content(self.original_text)

    def show_context_menu(self, pos):
        tm = ThemeManager()
        menu = QMenu(self)
        menu.setStyleSheet(f"""
                    QMenu {{ background-color: {tm.color('bg_card')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 4px; }} 
                    QMenu::item {{ padding: 6px 20px; border-radius: 2px; }}
                    QMenu::item:selected {{ background-color: {tm.color('btn_hover')}; }}
                """)

        act_copy = menu.addAction(tm.icon("copy", "text_main"), "Copy Plain Text")
        act_copy.triggered.connect(self.copy_plain_text)

        act_copy_md = menu.addAction(tm.icon("file-text", "text_main"), "Copy Markdown")
        act_copy_md.triggered.connect(self.copy_markdown)


        if self.is_user and self._can_edit:
            act_edit = menu.addAction(tm.icon("edit", "text_main"), "编辑 (Edit)")
            act_edit.triggered.connect(self.toggle_edit)

        menu.exec(self.lbl_text.mapToGlobal(pos))

    def disable_edit(self):
        self._can_edit = False
        if hasattr(self, 'btn_edit'):
            self.btn_edit.setVisible(False)
        if self.is_editing:
            self.cancel_edit()

    def _apply_theme(self):
        tm = ThemeManager()
        font_family = tm.font_family()

        if self.msg_type == self.MSG_ERROR:
            # 【关键修复】让外层的 QWidget 来承担背景色和红色左边框，这样就不会出现文字底纹了
            danger_color = tm.color('danger')
            is_dark = tm.current_theme == 'dark'
            bg_color = hex_to_rgba(danger_color, 0.1) if is_dark else hex_to_rgba(danger_color, 0.05)

            self.content_container.setStyleSheet(f"""
                QWidget#BubbleWrapper {{
                    background-color: {bg_color};
                    border: 1px solid {tm.color('border')};
                    border-left: 4px solid {danger_color};
                    border-radius: 4px; 
                }}
            """)
        elif self.is_user:
            bg_color = hex_to_rgba(tm.color('success'), 0.15) if tm.current_theme == 'dark' else hex_to_rgba(
                tm.color('success'), 0.1)
            border_color = tm.color('success')
            self.content_container.setStyleSheet(f"""
                QWidget#BubbleWrapper {{
                    background-color: {bg_color};
                    border: 1px solid {border_color};
                    border-radius: 8px;
                }}
            """)
        else:
            bg_color = tm.color('bg_card')
            border_color = tm.color('border')
            self.content_container.setStyleSheet(f"""
                QWidget#BubbleWrapper {{
                    background-color: {bg_color};
                    border: 1px solid {border_color};
                    border-radius: 8px;
                }}
            """)

        self.lbl_text.setStyleSheet(f"""
                    QTextBrowser {{
                        background-color: transparent; color: {tm.color('text_main')};
                        border: none; padding: 0px; 
                        font-size: 14px; font-family: {font_family};
                    }}
                    QScrollBar:horizontal {{
                        background: transparent; height: 8px; margin: 0px;
                    }}
                    QScrollBar::handle:horizontal {{
                        background: {hex_to_rgba(tm.color('text_muted'), 0.4) if 'hex_to_rgba' in globals() else 'rgba(150, 150, 150, 0.35)'}; 
                        border-radius: 4px;
                    }}
                    QScrollBar::handle:horizontal:hover {{ background: {tm.color('accent')}; }}
                    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}
                """)

        self.edit_input.setStyleSheet(f"""
            QTextEdit {{ 
                background-color: {tm.color('bg_input')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('accent')}; 
                border-radius: 6px; padding: 6px 10px; font-family: {font_family}; font-size: 14px;
            }}
        """)

        btn_style = f"QPushButton {{ background-color: transparent; border: none; color: {tm.color('text_muted')}; font-size: 12px; padding: 2px 4px; border-radius: 4px; }} QPushButton:hover {{ color: {tm.color('text_main')}; background-color: {tm.color('btn_hover')}; }}"
        self.btn_copy.setStyleSheet(btn_style)
        if hasattr(self, 'btn_copy_md'): self.btn_copy_md.setStyleSheet(btn_style)
        if hasattr(self, 'btn_edit'): self.btn_edit.setStyleSheet(btn_style)
        if hasattr(self, 'btn_cancel'): self.btn_cancel.setStyleSheet(btn_style)

        if hasattr(self, 'ctx_frame'):
            self.ctx_frame.setStyleSheet(f"""
                QFrame#ContextFrame {{ background-color: {hex_to_rgba(tm.color('bg_input'), 0.5)}; border-left: 3px solid {tm.color('accent')}; border-radius: 4px; }}
            """)
            self.ctx_header.setStyleSheet(
                f"color: {tm.color('accent')}; font-size: 11px; font-weight: bold; border: none; background: transparent; font-family: {font_family};")
            self.ctx_content.setStyleSheet(
                f"color: {tm.color('text_muted')}; font-size: 12px; border: none; background: transparent; font-family: {font_family}; margin: 0px; padding: 0px;")

    def set_loading(self, loading: bool):
        self.is_loading = loading
        if loading:
            self.loading_dots = 0
            self.btn_widget.hide()

            if not self.original_text.strip():
                self.lbl_text.setText("Thinking")
                self.loading_timer.start(500)
            else:
                self.set_content(self.original_text)
        else:
            self.loading_timer.stop()
            self.btn_widget.show()
            self.set_content(self.original_text)

    def _animate_loading(self):
        if self.original_text.strip():
            self.loading_timer.stop()
            return

        self.loading_dots = (self.loading_dots + 1) % 4
        self.lbl_text.setText("Thinking" + "." * self.loading_dots)


    # --- 5. 完整的 set_content 方法 ---
    def set_content(self, text, msg_type=None):
        self.original_text = text
        if msg_type is not None:
            self.msg_type = msg_type

        if self.msg_type == self.MSG_ERROR:
            import json
            tm = ThemeManager()
            try:
                error_data = json.loads(text)
                title = error_data.get('title', 'Error')
                body = error_data.get('body', text)
            except json.JSONDecodeError:
                title = "Generation Terminated"
                body = text

            danger_color = tm.color('danger')

            html = (
                f"<div style='margin: 0px; padding: 4px 6px;'>"
                f"<div style='color: {danger_color}; font-weight: bold; font-size: 14px; margin-bottom: 8px;'>"
                f"⚠️ {title}</div>"
                f"<div style='font-size: 13px; font-family: {tm.font_family()}; line-height: 1.5;'>"
                f"{body.replace(chr(10), '<br>')}</div>"
                f"</div>"
            )
            self.lbl_text.setText(html)
            self._adjust_browser_height()
            return

        if self.is_loading:
            if not text.strip():
                if not self.loading_timer.isActive():
                    self.loading_dots = 0
                    self.lbl_text.setText("Thinking")
                    self.loading_timer.start(500)
                return
            else:
                if self.loading_timer.isActive():
                    self.loading_timer.stop()

        try:
            html = TextFormatter.markdown_to_html(text)
            tm = ThemeManager()
            border_color = tm.color('border')
            bg_header = hex_to_rgba(tm.color('bg_input'), 0.5) if tm.current_theme == 'dark' else '#f5f5f5'

            html = html.replace('<table>',
                                f'<table border="1" cellspacing="0" cellpadding="8" style="border-collapse: collapse; border-color: {border_color}; margin-top: 10px; margin-bottom: 10px; width: 100%; table-layout: fixed; word-break: break-all;">')

            html = html.replace('<th>',
                                f'<th style="background-color: {bg_header}; font-weight: bold; text-align: left;">')

            def repl_img(match):
                raw_src_url = match.group(1)
                src_url = raw_src_url.replace("&amp;", "&")

                if src_url.startswith("data:image"):
                    try:
                        header, encoded = src_url.split(",", 1)
                        ext = header.split(";")[0].split("/")[1] if "/" in header else "png"
                        import base64
                        import hashlib
                        import os
                        import tempfile

                        img_data = base64.b64decode(encoded)

                        file_name = f"navis_base64_{hashlib.md5(img_data).hexdigest()[:12]}.{ext}"
                        local_path = os.path.join(tempfile.gettempdir(), file_name)

                        if not os.path.exists(local_path):
                            with open(local_path, "wb") as f:
                                f.write(img_data)

                        self.downloaded_images[src_url] = local_path
                        local_uri = f"file:///{local_path.replace(os.sep, '/')}"
                        new_img_tag = f'<img width="420" style="max-width: 100%; border-radius: 8px; margin-top: 5px;" src="{local_uri}" title="Click to view full image" />'
                        return f'<a href="{local_uri}">{new_img_tag}</a>'
                    except Exception as e:
                        print(f"Base64 image decode failed: {e}")
                        return f'<img width="420" style="max-width: 100%;" src="{src_url}" />'

                if src_url.startswith("file://"):
                    new_img_tag = f'<img width="420" style="max-width: 100%; border-radius: 8px; margin-top: 5px;" src="{src_url}" title="Click to view full image" />'
                    return f'<a href="{src_url}">{new_img_tag}</a>'

                if src_url.startswith("http"):
                    if src_url in self.downloaded_images:
                        local_path = self.downloaded_images[src_url].replace('\\', '/')
                        if not local_path.startswith('/'):
                            local_uri = f"file:///{local_path}"
                        else:
                            local_uri = f"file://{local_path}"

                        new_img_tag = f'<img width="420" style="max-width: 100%; border-radius: 8px; margin-top: 5px;" src="{local_uri}" title="Click to view full image" />'
                        return f'<a href="{local_uri}">{new_img_tag}</a>'

                    elif src_url in getattr(self, 'download_failed_urls', {}):
                        error_msg = self.download_failed_urls[src_url]
                        return f'<div style="color:#ff6b6b; padding: 15px; border: 2px dashed #ff6b6b; border-radius: 8px; width: 400px; margin-top: 5px;">❌ <b>Image download failed.</b><br><span style="font-size: 12px;">{error_msg}</span></div>'

                    else:
                        import time
                        if src_url not in self.downloading_urls:
                            self.downloading_urls.add(src_url)
                            self.download_timeouts[src_url] = time.time() + 30
                            self._start_image_download(src_url)

                            if not self.image_loading_timer.isActive():
                                self.image_loading_timer.start(500)

                        dots = "." * getattr(self, 'image_loading_dots', 0)
                        return f'<div style="color:#05B8CC; padding: 20px; border: 2px dashed #05B8CC; border-radius: 8px; width: 400px; margin-top: 5px;">⏳ <span style="vertical-align: middle;">Downloading image to local cache, please wait. {dots}</span></div>'

                return match.group(0)

            html = re.sub(r'<img[^>]+src="([^">]+)"[^>]*>', repl_img, html)

            self.lbl_text.setText(html)
            self.lbl_text.adjustSize()
            self.content_container.adjustSize()
            self.adjustSize()
            self.updateGeometry()

        except Exception as e:
            self.lbl_text.setText(text)
            self.lbl_text.adjustSize()
            self.content_container.adjustSize()
            self.adjustSize()
            self.updateGeometry()

    def clean_up_images(self):
        for path in self.downloaded_images.values():
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
        self.downloaded_images.clear()

    def add_translation_widget(self, translated_text):
        if not self.is_user:
            return

        tm = ThemeManager()

        self.trans_container = QWidget()
        trans_layout = QVBoxLayout(self.trans_container)
        trans_layout.setContentsMargins(0, 5, 0, 0)
        trans_layout.setSpacing(4)

        self.btn_toggle_trans = QPushButton(" Show Translated Query")
        self.btn_toggle_trans.setIcon(tm.icon("language", "text_muted"))
        self.btn_toggle_trans.setCursor(Qt.PointingHandCursor)

        self.lbl_trans = QLabel(translated_text)
        self.lbl_trans.setWordWrap(True)
        self.lbl_trans.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_trans.setVisible(False)

        def toggle_trans():
            is_vis = self.lbl_trans.isVisible()
            self.lbl_trans.setVisible(not is_vis)
            self.btn_toggle_trans.setText(" Hide Translated Query" if not is_vis else " Show Translated Query")
            self._apply_trans_theme()

        self.btn_toggle_trans.clicked.connect(toggle_trans)

        trans_layout.addWidget(self.btn_toggle_trans)
        trans_layout.addWidget(self.lbl_trans)

        if hasattr(self, 'btn_widget'):
            idx = self.content_layout.indexOf(self.btn_widget)
            self.content_layout.insertWidget(max(0, idx), self.trans_container)
        else:
            self.content_layout.addWidget(self.trans_container)

        ThemeManager().theme_changed.connect(self._apply_trans_theme)

        self._apply_trans_theme()

    def _apply_trans_theme(self):
        if not hasattr(self, 'btn_toggle_trans') or not hasattr(self, 'lbl_trans'):
            return

        tm = ThemeManager()
        font_family = tm.font_family()

        self.btn_toggle_trans.setIcon(tm.icon("language", "text_muted"))
        self.btn_toggle_trans.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                text-align: left;
                color: {tm.color('text_muted')};
                font-size: 11px;
                font-weight: bold;
                font-family: {font_family};
                padding: 2px 0px;
            }}
            QPushButton:hover {{
                color: {tm.color('accent')};
            }}
        """)

        if tm.current_theme == 'dark':
            bg = hex_to_rgba(tm.color('accent'), 0.08)
            border = hex_to_rgba(tm.color('accent'), 0.35)
            text_color = tm.color('text_muted')
        else:
            bg = hex_to_rgba(tm.color('academic_blue'), 0.06)
            border = hex_to_rgba(tm.color('academic_blue'), 0.3)
            text_color = tm.color('text_muted')

        self.lbl_trans.setStyleSheet(f"""
                   QLabel {{
                       color: {text_color};
                       background-color: {bg};
                       border: 1px solid {border};
                       border-left: 3px solid {tm.color('accent')};
                       border-radius: 6px;
                       padding: 8px 10px;
                       font-size: 12px;
                       font-family: {font_family};
                   }}
               """)

    def _adjust_browser_height(self):
        doc_height = int(self.lbl_text.document().size().height())
        sb = self.lbl_text.horizontalScrollBar()
        sb_height = 0

        if sb.isVisible():
            sb_height = sb.height()
        else:
            if self.lbl_text.document().idealWidth() > self.lbl_text.viewport().width():
                sb_height = sb.sizeHint().height()

        self.lbl_text.setFixedHeight(doc_height + sb_height + 15)


    def _start_image_download(self, url):
        ext = url.split("?")[0].split(".")[-1]
        if ext.lower() not in ['png', 'jpg', 'jpeg', 'gif', 'webp','svg']:
            ext = 'png'
        file_name = f"navis_img_{hashlib.md5(url.encode()).hexdigest()}.{ext}"
        save_path = os.path.join(tempfile.gettempdir(), file_name)

        if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
            self._on_image_downloaded({"success": True, "url": url, "path": save_path})
            return

        task_mgr = TaskManager()
        task_mgr.sig_result.connect(self._on_image_downloaded)
        self.image_task_mgrs[url] = task_mgr

        task_mgr.start_task(
            DownloadImageTask,
            task_id=f"dl_img_{hashlib.md5(url.encode()).hexdigest()[:8]}",
            mode=TaskMode.THREAD,
            url=url,
            save_path=save_path
        )

    def _on_image_downloaded(self, result):
        success = result.get("success", False)
        url = result.get("url")
        result_path = result.get("path")

        if url in self.downloading_urls:
            self.downloading_urls.remove(url)

        # 清理已完成的任务管理器实例
        if url in self.image_task_mgrs:
            del self.image_task_mgrs[url]

        if success:
            self.downloaded_images[url] = result_path
        else:
            if not hasattr(self, 'download_failed_urls'):
                self.download_failed_urls = {}
            self.download_failed_urls[url] = result.get("msg", "Network transmission error")
            print(f"Failed to fetch image: {result_path}")

        if not self.downloading_urls and hasattr(self, 'image_loading_timer'):
            self.image_loading_timer.stop()

        self.set_content(self.original_text)

    def copy_plain_text(self):
        if self.msg_type == self.MSG_ERROR or getattr(self, 'is_interrupted', False):
            ToastManager().show("Cannot copy interrupted or error messages.", "warning")
            return

        clipboard = QGuiApplication.clipboard()

        raw_text = self.lbl_text.toPlainText()

        if self.is_loading:
            raw_text = re.sub(r'^Thinking\.{0,3}\n*', '', raw_text)

        raw_text = re.sub(r'Initializing\.\.\.', '', raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r'Reasoning & Tool Execution', '', raw_text, flags=re.IGNORECASE)

        lines = [line.rstrip() for line in raw_text.splitlines()]

        cleaned_text = '\n'.join(lines)
        cleaned_text = re.sub(r'\n{3,}', '\n\n', cleaned_text).strip()

        clipboard.setText(cleaned_text)
        ToastManager().show("Plain text successfully copied to clipboard.", "success")


    def copy_markdown(self):
        if self.msg_type == self.MSG_ERROR or getattr(self, 'is_interrupted', False):
            ToastManager().show("Cannot copy interrupted or error messages.", "warning")
            return

        clipboard = QGuiApplication.clipboard()

        raw_text = re.sub(r'<think>.*?</think>', '', self.original_text, flags=re.DOTALL | re.IGNORECASE)
        raw_text = re.sub(r'<mcp_process>.*?</mcp_process>', '', raw_text, flags=re.DOTALL | re.IGNORECASE)

        raw_text = re.sub(r'\[([^\]]+)\]\(cite://[^\)]+\)', r'[\1]', raw_text)
        raw_text = re.sub(r'Initializing\.\.\.', '', raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r'Reasoning & Tool Execution', '', raw_text, flags=re.IGNORECASE)

        if "<br><hr style='border:0; height:1px;" in raw_text:
            raw_text = re.split(
                r'<br><hr style=\'border:0; height:1px; background:#444; margin:15px 0;\'><b>.*?Cited Sources:</b><br>',
                raw_text)[0]

        text_to_copy = re.sub(r'\n{3,}', '\n\n', raw_text).strip()

        clipboard.setText(text_to_copy)
        ToastManager().show("Markdown successfully copied to clipboard.", "success")


    def toggle_edit(self):
        if not self.is_editing:
            self.is_editing = True

            scroll_area = self.window().findChild(QScrollArea)
            current_scroll = scroll_area.verticalScrollBar().value() if scroll_area else 0

            self.lbl_text.setVisible(False)
            self.btn_widget.setVisible(False)

            self.content_layout.setSpacing(4)
            self.content_layout.setContentsMargins(12, 12, 12, 8)

            self.edit_input.setVisible(True)
            self.edit_btn_widget.setVisible(True)

            self.edit_input.setText(self.original_text)
            self.edit_input.setFocus()

            if scroll_area:
                scroll_area.verticalScrollBar().setValue(current_scroll)
        else:
            self.cancel_edit()

    def cancel_edit(self):
        self.is_editing = False
        self.edit_input.setVisible(False)
        if hasattr(self, 'edit_btn_widget'):
            self.edit_btn_widget.setVisible(False)

        self.content_layout.setSpacing(6)
        self.content_layout.setContentsMargins(12, 12, 12, 12)

        self.lbl_text.setVisible(True)
        self.btn_widget.setVisible(True)

    def eventFilter(self, obj, event):
        if obj == self.edit_input and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Return:
                if event.modifiers() & Qt.ShiftModifier:
                    return False
                else:
                    self.save_edit()
                    return True
        return super().eventFilter(obj, event)

    def save_edit(self):
        new_text = self.edit_input.toPlainText().strip()
        self.cancel_edit()

        if new_text and new_text != self.original_text:
            self.sig_edit_confirmed.emit(self.index, new_text)



def closeEvent(self, event):

    # 1. 停止图片加载动画定时器
    if hasattr(self, 'image_loading_timer') and self.image_loading_timer.isActive():
        self.image_loading_timer.stop()
        self.logger.debug("Image loading timer stopped.")

    # 2. 遍历所有任务管理器，取消正在进行的下载任务
    if hasattr(self, 'image_task_mgrs') and self.image_task_mgrs:

        for url in list(self.image_task_mgrs.keys()):
            task_mgr = self.image_task_mgrs[url]
            task_mgr.cancel_task()

            del self.image_task_mgrs[url]

            self.logger.debug(f"Cancelled image download task for URL: {url[:30]}...")

    if hasattr(self, 'downloading_urls'):
        self.downloading_urls.clear()

    super().closeEvent(event)