import os
import re
import hashlib
import tempfile
import time

import markdown
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QTextEdit, QPushButton, QFrame, QSizePolicy, QMenu, QTextBrowser)
from PySide6.QtCore import Qt, Signal, QSize, QEvent, QTimer, QThread, QUrl
from PySide6.QtGui import QClipboard, QGuiApplication, QCursor

from src.core.theme_manager import ThemeManager
from src.ui.components.toast import ToastManager
from src.core.network_worker import LightNetworkWorker

def hex_to_rgba(hex_color, alpha):
    """将 #RRGGBB 格式的十六进制颜色转换为 rgba(r, g, b, alpha) 字符串"""
    hex_color = hex_color.lstrip('#')
    if len(hex_color) == 3:
        hex_color = ''.join([c*2 for c in hex_color])
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"

class ChatBubbleWidget(QWidget):
    sig_edit_confirmed = Signal(int, str)
    sig_link_clicked = Signal(str)
    sig_retry_clicked = Signal(int)

    def __init__(self, text, is_user, index, context_html=None, parent=None):
        super().__init__(parent)
        self.original_text = text
        self.is_user = is_user
        self.index = index
        self.context_html = context_html
        self.is_editing = False
        self._can_edit = True

        self.loading_timer = QTimer(self)
        self.loading_timer.timeout.connect(self._animate_loading)
        self.loading_dots = 0
        self.is_loading = False

        # 异步图片下载管理队列
        self.downloaded_images = {}
        self.downloading_urls = set()
        self.download_failed_urls = {}
        self.image_threads = []
        self.image_loading_timer = QTimer(self)
        self.image_loading_timer.timeout.connect(self._animate_image_loading)
        self.image_loading_dots = 0
        self.download_timeouts = {}
        self.init_ui()
        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

    def init_ui(self):
        tm = ThemeManager()

        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(10, 5, 10, 15)
        self.main_layout.setSpacing(10)

        self.spacer = QWidget()
        self.spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        self.content_container = QWidget()
        self.content_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        self.content_layout = QVBoxLayout(self.content_container)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(6)
        self.content_layout.setAlignment(Qt.AlignTop)

        font_family = (
            "'Microsoft YaHei', 'PingFang SC', 'Segoe UI', "
            "'Segoe UI Emoji', 'Segoe UI Symbol', sans-serif"
        )

        if self.is_user and self.context_html:
            self.ctx_frame = QFrame()
            self.ctx_frame.setStyleSheet("""
                        QFrame { background-color: rgba(0, 0, 0, 0.2); border-left: 3px solid #05B8CC; border-radius: 4px; }
                    """)
            ctx_layout = QVBoxLayout(self.ctx_frame)
            ctx_layout.setContentsMargins(10, 8, 10, 8)
            ctx_layout.setSpacing(4)

            ctx_header = QLabel("📎 Attached Context (Click to View)")
            ctx_header.setStyleSheet(
                f"color: #05B8CC; font-size: 11px; font-weight: bold; border: none; background: transparent; font-family: {font_family};")

            ctx_content = QLabel()
            ctx_content.setTextFormat(Qt.RichText)
            ctx_content.setTextInteractionFlags(Qt.TextBrowserInteraction)
            ctx_content.setOpenExternalLinks(False)
            ctx_content.linkActivated.connect(self.sig_link_clicked.emit)
            ctx_content.setText(self.context_html)
            ctx_content.setWordWrap(True)

            self.ctx_frame.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
            ctx_content.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

            ctx_content.setStyleSheet(
                f"color: #aaa; font-size: 12px; border: none; background: transparent; font-family: {font_family}; margin: 0px; padding: 0px;")

            ctx_layout.addWidget(ctx_header)
            ctx_layout.addWidget(ctx_content)
            self.content_layout.addWidget(self.ctx_frame)

        self.lbl_text = QLabel()
        self.lbl_text.setWordWrap(True)
        self.lbl_text.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.lbl_text.setOpenExternalLinks(False)
        self.lbl_text.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.lbl_text.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lbl_text.customContextMenuRequested.connect(self.show_context_menu)

        if self.is_user:
            bg_color = "#124126"
            border_color = "#1e5e38"
            self.lbl_text.setStyleSheet(f"""
                QLabel {{
                    background-color: {bg_color}; color: #e0e0e0;
                    border: 1px solid {border_color}; border-radius: 8px;
                    padding: 10px 14px; font-size: 14px; font-family: {font_family}; line-height: 1.5;
                }}
            """)
            self.main_layout.addWidget(self.spacer)
            self.main_layout.addWidget(self.content_container)
            btn_alignment = Qt.AlignRight
        else:
            bg_color = "#333333"
            border_color = "#444444"
            self.lbl_text.setStyleSheet(f"""
                QLabel {{
                    background-color: {bg_color}; color: #e0e0e0;
                    border: 1px solid {border_color}; border-radius: 8px;
                    padding: 10px 14px; font-size: 14px; font-family: {font_family}; line-height: 1.5;
                }}
            """)
            self.main_layout.addWidget(self.content_container)
            self.main_layout.addWidget(self.spacer)
            btn_alignment = Qt.AlignLeft

        self.set_content(self.original_text)

        self.edit_input = QTextEdit()
        self.edit_input.setVisible(False)
        self.edit_input.setStyleSheet(f"""
            QTextEdit {{ 
                background-color: #2b2b2b; color: #fff; border: 1px solid #007acc; 
                border-radius: 6px; padding: 6px 10px; font-family: {font_family}; font-size: 14px;
            }}
        """)
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

        btn_style = """
            QPushButton { background-color: transparent; border: none; color: #777; font-size: 12px; padding: 2px 4px; border-radius: 4px; } 
            QPushButton:hover { color: #ccc; background-color: #444; }
        """

        self.btn_copy = QPushButton(" Copy")
        self.btn_copy.setIcon(tm.icon("copy", "text_muted"))
        self.btn_copy.setCursor(Qt.PointingHandCursor)
        self.btn_copy.setStyleSheet(btn_style)
        self.btn_copy.clicked.connect(self.copy_text)
        self.btn_layout.addWidget(self.btn_copy)

        if not self.is_user:
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
            self.btn_edit.setStyleSheet(btn_style)
            self.btn_edit.clicked.connect(self.toggle_edit)
            self.btn_layout.addWidget(self.btn_edit)

        self.content_layout.addWidget(self.btn_widget)

        if self.is_user:
            self.edit_btn_widget = QWidget()
            self.edit_btn_layout = QHBoxLayout(self.edit_btn_widget)
            self.edit_btn_layout.setContentsMargins(0, 0, 0, 0)
            self.edit_btn_layout.setSpacing(10)
            self.edit_btn_layout.setAlignment(btn_alignment)

            self.btn_cancel = QPushButton(" Cancel")
            self.btn_cancel.setIcon(tm.icon("close", "text_muted"))

            self.btn_confirm = QPushButton(" Confirm)")
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
            self.lbl_text.setMaximumWidth(max_w)
            self.edit_input.setMaximumWidth(max_w)

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

        act_copy = menu.addAction(tm.icon("copy", "text_main"), "复制 (Copy)")
        act_copy.triggered.connect(self.copy_text)

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
        font_family = "'Microsoft YaHei', 'PingFang SC', 'Segoe UI', sans-serif"

        # User vs AI Bubble Colors
        if self.is_user:
            bg_color = hex_to_rgba(tm.color('success'), 0.15) if tm.current_theme == 'dark' else hex_to_rgba(
                tm.color('success'), 0.1)
            border_color = tm.color('success')
        else:
            bg_color = tm.color('bg_card')
            border_color = tm.color('border')

        self.lbl_text.setStyleSheet(f"""
            QLabel {{
                background-color: {bg_color}; color: {tm.color('text_main')};
                border: 1px solid {border_color}; border-radius: 8px;
                padding: 10px 14px; font-size: 14px; font-family: {font_family}; line-height: 1.5;
            }}
        """)

        self.edit_input.setStyleSheet(f"""
            QTextEdit {{ 
                background-color: {tm.color('bg_input')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('accent')}; 
                border-radius: 6px; padding: 6px 10px; font-family: {font_family}; font-size: 14px;
            }}
        """)

        btn_style = f"QPushButton {{ background-color: transparent; border: none; color: {tm.color('text_muted')}; font-size: 12px; padding: 2px 4px; border-radius: 4px; }} QPushButton:hover {{ color: {tm.color('text_main')}; background-color: {tm.color('btn_hover')}; }}"
        self.btn_copy.setStyleSheet(btn_style)
        if hasattr(self, 'btn_edit'): self.btn_edit.setStyleSheet(btn_style)
        if hasattr(self, 'btn_cancel'): self.btn_cancel.setStyleSheet(btn_style)


    def set_loading(self, loading: bool):
        self.is_loading = loading
        if loading:
            self.loading_dots = 0
            self.lbl_text.setText("Thinking")
            self.loading_timer.start(500)
            self.btn_widget.hide()
        else:
            self.loading_timer.stop()
            self.btn_widget.show()
            self.set_content(self.original_text)

    def _animate_loading(self):
        self.loading_dots = (self.loading_dots + 1) % 4
        self.lbl_text.setText("Thinking" + "." * self.loading_dots)


    def set_content(self, text):
        self.original_text = text
        if self.is_loading: return

        try:
            processed_text = text

            processed_text = re.sub(r'(?<![="\'/])\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)\b',
                                    r'<a href="https://doi.org/\1">\1</a>', processed_text)
            processed_text = re.sub(r'(?<![="\'/\[\(])\b(https?://[^\s<>\)\]]+)\b', r'<a href="\1">\1</a>',
                                    processed_text)

            html = markdown.markdown(processed_text, extensions=['extra', 'nl2br', 'sane_lists', 'tables'])
            html = html.replace("<a href=",
                                "<a style='color: #4daafc; text-decoration: none; font-weight: bold;' href=")

            def repl_img(match):
                raw_src_url = match.group(1)
                src_url = raw_src_url.replace("&amp;", "&")

                if src_url.startswith("http"):
                    if src_url in self.downloaded_images:
                        local_path = self.downloaded_images[src_url].replace('\\', '/')
                        if not local_path.startswith('/'):
                            local_uri = f"file:///{local_path}"
                        else:
                            local_uri = f"file://{local_path}"

                        new_img_tag = f'<img width="420" style="border-radius: 8px; margin-top: 5px;" src="{local_uri}" />'
                        return f'<a href="{local_uri}">{new_img_tag}</a>'

                    elif src_url in getattr(self, 'download_failed_urls', {}):
                        error_msg = self.download_failed_urls[src_url]
                        return f'<div style="color:#ff6b6b; padding: 15px; border: 2px dashed #ff6b6b; border-radius: 8px; width: 400px; margin-top: 5px;">❌ <b>图像下载失败</b><br><span style="font-size: 12px;">{error_msg}</span></div>'

                    else:
                        if src_url not in self.downloading_urls:
                            self.downloading_urls.add(src_url)
                            self.download_timeouts[src_url] = time.time() + 30  # 🌟 设置 30 秒超时
                            self._start_image_download(src_url)

                            # 启动动画计时器
                            if not self.image_loading_timer.isActive():
                                self.image_loading_timer.start(500)

                        dots = "." * getattr(self, 'image_loading_dots', 0)
                        return f'<div style="color:#05B8CC; padding: 20px; border: 2px dashed #05B8CC; border-radius: 8px; width: 400px; margin-top: 5px;">⏳ <span style="vertical-align: middle;">正在将图像下载到本地缓存，请稍候{dots}</span></div>'

            html = re.sub(r'<img[^>]+src="([^">]+)"[^>]*>', repl_img, html)

            self.lbl_text.setText(html)
        except Exception as e:
            self.lbl_text.setText(text)


    def clean_up_images(self):
        for path in self.downloaded_images.values():
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
        self.downloaded_images.clear()


    def _start_image_download(self, url):
        """丢进后台线程进行安全下载，不卡主界面"""
        ext = url.split("?")[0].split(".")[-1]
        if ext.lower() not in ['png', 'jpg', 'jpeg', 'gif', 'webp']:
            ext = 'png'
        file_name = f"navis_img_{hashlib.md5(url.encode()).hexdigest()}.{ext}"
        save_path = os.path.join(tempfile.gettempdir(), file_name)

        # 命中缓存秒渲染
        if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
            self._on_image_downloaded(True, url, save_path)
            return

        thread = QThread(self)
        worker = LightNetworkWorker()
        thread.worker = worker
        worker.moveToThread(thread)

        worker.img_url = url
        worker.img_save_path = save_path

        thread.started.connect(worker.do_download_image)
        worker.sig_image_downloaded.connect(self._on_image_downloaded)
        worker.sig_image_downloaded.connect(thread.quit)
        worker.sig_image_downloaded.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self.image_threads.append(thread)
        thread.start()

    def _on_image_downloaded(self, success, url, result_path):
        if url in self.downloading_urls:
            self.downloading_urls.remove(url)

        if success:
            self.downloaded_images[url] = result_path
        else:
            if not hasattr(self, 'download_failed_urls'):
                self.download_failed_urls = {}
            self.download_failed_urls[url] = result_path
            print(f"Failed to fetch image: {result_path}")

        if not self.downloading_urls and hasattr(self, 'image_loading_timer'):
            self.image_loading_timer.stop()

        self.set_content(self.original_text)

    def copy_text(self):
        clipboard = QGuiApplication.clipboard()
        text_to_copy = self.original_text
        text_to_copy = re.sub(r'<think>.*?</think>', '', text_to_copy, flags=re.DOTALL).strip()

        if not self.is_user and "<b>📚 Cited Sources:</b>" in text_to_copy:
            parts = text_to_copy.split("<b>📚 Cited Sources:</b><br>")
            main_text = re.sub(r"<[^>]+>", "", parts[0].replace("<br>", "\n")).strip()
            citations_text = "\n\n📚 Reference:\n"
            if len(parts) > 1:
                raw_cites = parts[1]
                matches = re.findall(r"<b>\[(\d+)\]</b>\s*(.*?)\s*\(Page (\d+)\)", raw_cites)
                for m in matches:
                    idx, name, page = m
                    citations_text += f"[{idx}] {name.strip()} (第 {page} 页)\n"
            text_to_copy = main_text + citations_text
        else:
            if not self.is_user:
                text_to_copy = re.sub(r"<[^>]+>", "", text_to_copy.replace("<br>", "\n")).strip()

        clipboard.setText(text_to_copy)
        ToastManager().show("Copied to clipboard.", "success")

    def toggle_edit(self):
        if not self.is_editing:
            self.is_editing = True

            current_height = self.lbl_text.height()

            self.lbl_text.setVisible(False)
            self.btn_widget.setVisible(False)
            self.content_layout.setSpacing(2)

            self.edit_input.setMinimumHeight(current_height)  # 防止塌陷
            self.edit_input.setVisible(True)
            self.edit_btn_widget.setVisible(True)

            self.edit_input.setText(self.original_text)
            self.edit_input.setFocus()

            QTimer.singleShot(0, self.adjust_edit_height)
        else:
            self.cancel_edit()

    def cancel_edit(self):
        self.is_editing = False
        self.edit_input.setVisible(False)
        if hasattr(self, 'edit_btn_widget'):
            self.edit_btn_widget.setVisible(False)

        self.content_layout.setSpacing(6)

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