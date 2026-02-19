from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QTextEdit, QPushButton, QFrame, QSizePolicy, QMenu)
from PySide6.QtCore import Qt, Signal, QSize, QEvent, QTimer
from PySide6.QtGui import QClipboard, QGuiApplication, QCursor
from src.ui.components.toast import ToastManager
import markdown
import re


class ChatBubbleWidget(QWidget):
    sig_edit_confirmed = Signal(int, str)

    def __init__(self, text, is_user, index, parent=None):
        super().__init__(parent)
        self.original_text = text
        self.is_user = is_user
        self.index = index
        self.is_editing = False
        self._can_edit = True

        self.loading_timer = QTimer(self)
        self.loading_timer.timeout.connect(self._animate_loading)
        self.loading_dots = 0
        self.is_loading = False

        self.init_ui()

    def init_ui(self):
        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(10, 5, 10, 15)
        self.main_layout.setSpacing(10)

        self.spacer = QWidget()
        self.spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        # 核心：最大化容器
        self.content_container = QWidget()
        self.content_container.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Minimum)

        self.content_layout = QVBoxLayout(self.content_container)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(6)  # 默认阅读模式间距：6px (舒适)
        self.content_layout.setAlignment(Qt.AlignTop)

        self.lbl_text = QLabel()
        self.lbl_text.setWordWrap(True)
        self.lbl_text.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.lbl_text.setOpenExternalLinks(False)
        self.lbl_text.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

        self.lbl_text.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lbl_text.customContextMenuRequested.connect(self.show_context_menu)

        font_family = (
            "'Microsoft YaHei', 'PingFang SC', 'Segoe UI', "
            "'Segoe UI Emoji', 'Segoe UI Symbol', sans-serif"
        )

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

        # 灵动自适应编辑框
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

        # 按钮条 1：常规模式（复制 / 编辑）
        self.btn_widget = QWidget()
        self.btn_layout = QHBoxLayout(self.btn_widget)
        self.btn_layout.setContentsMargins(0, 0, 5, 0)
        self.btn_layout.setSpacing(10)
        self.btn_layout.setAlignment(btn_alignment)

        btn_style = """
            QPushButton { background-color: transparent; border: none; color: #777; font-size: 12px; padding: 2px 4px; border-radius: 4px; } 
            QPushButton:hover { color: #ccc; background-color: #444; }
        """

        self.btn_copy = QPushButton("📄 复制")
        self.btn_copy.setCursor(Qt.PointingHandCursor)
        self.btn_copy.setStyleSheet(btn_style)
        self.btn_copy.clicked.connect(self.copy_text)
        self.btn_layout.addWidget(self.btn_copy)

        if self.is_user:
            self.btn_edit = QPushButton("✎ 编辑")
            self.btn_edit.setCursor(Qt.PointingHandCursor)
            self.btn_edit.setStyleSheet(btn_style)
            self.btn_edit.clicked.connect(self.toggle_edit)
            self.btn_layout.addWidget(self.btn_edit)

        self.content_layout.addWidget(self.btn_widget)

        # 🚀 按钮条 2：编辑模式（取消 / 确定）
        if self.is_user:
            self.edit_btn_widget = QWidget()
            self.edit_btn_layout = QHBoxLayout(self.edit_btn_widget)
            self.edit_btn_layout.setContentsMargins(0, 0, 0, 0)
            self.edit_btn_layout.setSpacing(10)
            self.edit_btn_layout.setAlignment(btn_alignment)

            self.btn_cancel = QPushButton("❌ 取消")
            self.btn_cancel.setCursor(Qt.PointingHandCursor)
            self.btn_cancel.setStyleSheet(btn_style)
            self.btn_cancel.clicked.connect(self.cancel_edit)

            self.btn_confirm = QPushButton("✅ 确定 (Enter)")
            self.btn_confirm.setCursor(Qt.PointingHandCursor)
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
            max_w = int(parent.width() * 0.66)
            self.lbl_text.setMaximumWidth(max_w)
            self.edit_input.setMaximumWidth(max_w)

    def adjust_edit_height(self):
        doc_h = int(self.edit_input.document().size().height())
        new_h = doc_h + 14
        if new_h > 200:
            self.edit_input.setFixedHeight(200)
            self.edit_input.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        else:
            self.edit_input.setFixedHeight(max(40, new_h))
            self.edit_input.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def show_context_menu(self, pos):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #2d2d30; color: white; border: 1px solid #444; border-radius: 4px; padding: 4px; } 
            QMenu::item { padding: 6px 20px; border-radius: 2px; }
            QMenu::item:selected { background-color: #007acc; }
        """)

        act_copy = menu.addAction("📄 复制 (Copy)")
        act_copy.triggered.connect(self.copy_text)

        # 必须同时满足是用户发的消息，且处于“可编辑状态”才显示编辑菜单
        if self.is_user and self._can_edit:
            act_edit = menu.addAction("✎ 编辑 (Edit)")
            act_edit.triggered.connect(self.toggle_edit)

        menu.exec(self.lbl_text.mapToGlobal(pos))

    def disable_edit(self):
        self._can_edit = False  # 彻底锁死权限
        if hasattr(self, 'btn_edit'):
            self.btn_edit.setVisible(False)
        if self.is_editing:
            self.cancel_edit()

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
            html = markdown.markdown(text, extensions=['extra', 'nl2br', 'sane_lists', 'tables'])
            html = html.replace("<a href=",
                                "<a style='color: #4daafc; text-decoration: none; font-weight: bold;' href=")
            self.lbl_text.setText(html)
        except:
            self.lbl_text.setText(text)

    def copy_text(self):
        clipboard = QGuiApplication.clipboard()
        text_to_copy = self.original_text

        if not self.is_user and "<b>📚 Cited Sources:</b>" in text_to_copy:
            parts = text_to_copy.split("<b>📚 Cited Sources:</b><br>")
            main_text = re.sub(r"<[^>]+>", "", parts[0].replace("<br>", "\n")).strip()
            citations_text = "\n\n📚 参考文献:\n"
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
        ToastManager().show("✅ 已复制到剪贴板", "success")

    def toggle_edit(self):
        if not self.is_editing:
            self.is_editing = True
            self.lbl_text.setVisible(False)
            self.btn_widget.setVisible(False)

            self.content_layout.setSpacing(2)

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