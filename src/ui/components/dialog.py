import os
import re
import time

import psutil
import torch
from PySide6.QtCore import Qt, Signal, QTimer, QThread, QObject, QRegularExpression, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QRegularExpressionValidator, QGuiApplication
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QWidget, QFrame, QFormLayout,
                               QLineEdit, QTextEdit, QComboBox, QProgressBar,
                               QSizePolicy, QHeaderView, QAbstractItemView, QTableWidget,
                               QCheckBox, QTableWidgetItem, QListWidget, QListWidgetItem, QScrollArea)

from src.core.mcp_manager import MCPManager
from src.core.models_registry import EMBEDDING_MODELS
from src.core.theme_manager import ThemeManager
from src.ui.components.param_editor import ParamEditorWidget
from src.ui.components.toast import ToastManager


class BaseDialog(QDialog):
    def __init__(self, parent=None, title="Dialog", width=450):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        self.setWindowTitle(title)
        self.setFixedWidth(width)

        self._is_closing = False

        self.tm = ThemeManager()
        self._tracked_buttons = []

        self.v_layout = QVBoxLayout(self)
        self.v_layout.setContentsMargins(0, 0, 0, 0)
        self.v_layout.setSpacing(0)

        self.content_widget = QWidget()
        self.content_widget.setObjectName("ContentWidget")
        self.content_widget.setAttribute(Qt.WA_StyledBackground, True)
        self.content_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(24, 24, 24, 24)
        self.content_layout.setSpacing(16)

        self.v_layout.addWidget(self.content_widget, 1)

        self.footer_widget = QWidget()
        self.footer_widget.setAttribute(Qt.WA_StyledBackground, True)
        self.footer_widget.setFixedHeight(55)

        self.footer_layout = QHBoxLayout(self.footer_widget)
        self.footer_layout.setContentsMargins(15, 0, 15, 0)
        self.footer_layout.addStretch()
        self.v_layout.addWidget(self.footer_widget)

        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.MinimumExpanding)
        self.tm.theme_changed.connect(self._apply_theme)

    def _apply_theme(self):
        tm = self.tm

        bg_color = QColor(tm.color('bg_main'))
        luminance = (0.299 * bg_color.red() + 0.587 * bg_color.green() + 0.114 * bg_color.blue())

        if luminance > 128:
            QGuiApplication.styleHints().setColorScheme(Qt.ColorScheme.Light)
        else:
            QGuiApplication.styleHints().setColorScheme(Qt.ColorScheme.Dark)

        self.setStyleSheet(f"""
            QDialog {{
                background-color: {tm.color('bg_main')};
            }}
            QWidget#ContentWidget {{
                background-color: {tm.color('bg_main')};
            }}
        """)

        self.footer_widget.setStyleSheet(f"""
            background-color: {tm.color('bg_card')}; 
            border-top: 1px solid {tm.color('border')};
        """)

        for btn, b_type in self._tracked_buttons:
            self._update_button_style(btn, b_type)

    def _update_button_style(self, btn, b_type):
        tm = self.tm

        if b_type == "primary":
            style = f"""
                QPushButton {{ 
                    border-radius: 4px; font-family: 'Segoe UI'; font-size: 13px; font-weight: 500;
                    background-color: {tm.color('accent')}; 
                    color: {tm.color('bg_main')}; 
                    border: 1px solid {tm.color('accent')}; 
                }}
                QPushButton:hover {{ background-color: {tm.color('accent_hover')}; }}
            """
        elif b_type == "danger":
            style = f"""
                QPushButton {{ 
                    border-radius: 4px; font-family: 'Segoe UI'; font-size: 13px; font-weight: 500;
                    background-color: transparent; 
                    color: {tm.color('danger')}; 
                    border: 1px solid {tm.color('danger')}; 
                }}
                QPushButton:hover {{ background-color: {tm.color('danger')}; color: {tm.color('bg_main')}; }}
            """
        else:
            style = f"""
                QPushButton {{ 
                    border-radius: 4px; font-family: 'Segoe UI'; font-size: 13px; font-weight: 500;
                    background-color: {tm.color('btn_bg')}; 
                    color: {tm.color('text_main')}; 
                    border: 1px solid {tm.color('border')}; 
                }}
                QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}
            """

        btn.setStyleSheet(style)


    def add_button(self, text, callback, is_primary=False, is_danger=False):
        btn = QPushButton(text)
        btn.setFixedSize(90, 32)
        btn.setCursor(Qt.PointingHandCursor)

        if callback:
            btn.clicked.connect(lambda *args, cb=callback: cb())

        b_type = "primary" if is_primary else ("danger" if is_danger else "default")
        self._tracked_buttons.append((btn, b_type))
        self._update_button_style(btn, b_type)

        self.footer_layout.addWidget(btn)
        return btn




class StandardDialog(BaseDialog):
    def __init__(self, parent=None, title="Notification", message="", show_cancel=False):
        super().__init__(parent, title=title, width=420)

        self.is_long_text = len(message) > 300 or message.count('\n') > 8

        self.msg_label = QLabel(message)
        self.msg_label.setWordWrap(True)
        self.msg_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.msg_label.setTextInteractionFlags(Qt.TextBrowserInteraction)

        if self.is_long_text:
            self.scroll_area = QScrollArea()
            self.scroll_area.setWidgetResizable(True)
            self.scroll_area.setFrameShape(QFrame.NoFrame)
            self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

            self.scroll_area.setWidget(self.msg_label)
            self.scroll_area.setMaximumHeight(350)
            self.scroll_area.setMinimumHeight(200)
            self.content_layout.addWidget(self.scroll_area)
        else:
            self.content_layout.addWidget(self.msg_label)

        if show_cancel:
            self.add_button("Cancel", self.reject)
        self.add_button("OK", self.accept, is_primary=True)

        self._apply_theme()

    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm
        self.msg_label.setStyleSheet(
            f"color: {tm.color('text_main')}; background-color: transparent; font-size: 14px; padding: 5px; border: none;"
        )

        if self.is_long_text:
            self.scroll_area.setStyleSheet(f"""
                QScrollArea {{ 
                    background-color: transparent; 
                    border: none; 
                }}
                QScrollBar:vertical {{ 
                    border: none;
                    background-color: transparent;
                    width: 6px;
                    margin: 0px;
                }}
                QScrollBar::handle:vertical {{ 
                    background-color: {tm.color('border')};
                    border-radius: 3px;
                    min-height: 20px;
                }}
                QScrollBar::handle:vertical:hover {{
                    background-color: {tm.color('text_muted')};
                }}
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                    height: 0px; border: none; background: none;
                }}
                QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                    background: none;
                }}
            """)
            self.scroll_area.viewport().setStyleSheet("background-color: transparent;")


try:
    import pynvml

    pynvml.nvmlInit()
    HAS_NVML = True
except Exception:
    HAS_NVML = False


class FeedEditorDialog(BaseDialog):
    def __init__(self, parent=None, feed_data=None, is_default=False, categories=None):
        title = "Edit Tracker Rule" if is_default else "Custom Feed Settings"
        super().__init__(parent, title=title, width=450)

        self.form_widget = QWidget()
        self.form_layout = QFormLayout(self.form_widget)
        self.form_layout.setSpacing(15)
        self.form_layout.setLabelAlignment(Qt.AlignRight)

        self.inp_name = QLineEdit(feed_data.get('name', '') if feed_data else '')
        self.inp_url = QLineEdit(feed_data.get('url', '') if feed_data else '')

        self.inp_category = QComboBox()
        self.inp_category.setEditable(True)
        cats = categories or []
        if "Custom Sources" not in cats:
            cats.append("Custom Sources")
        self.inp_category.addItems(cats)

        if feed_data and feed_data.get('category'):
            self.inp_category.setCurrentText(feed_data['category'])
        else:
            self.inp_category.setCurrentText("Custom Sources")

        if is_default:
            self.inp_name.setReadOnly(True)
            self.inp_url.setReadOnly(True)
            self.inp_category.setEnabled(False)
            self.form_layout.addRow("", QLabel("Built-in source: Read-only."))

        self.form_layout.addRow("Source Name:", self.inp_name)
        self.form_layout.addRow("RSS URL:", self.inp_url)
        self.form_layout.addRow("Category:", self.inp_category)

        self.content_layout.addWidget(self.form_widget)

        self.add_button("Cancel", self.reject)
        self.btn_save = self.add_button("Save", self.accept, is_primary=True)
        if is_default:
            self.btn_save.setEnabled(False)

        self._apply_theme()

    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm

        style = f"background-color: {tm.color('bg_input')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; padding: 6px; border-radius: 4px;"
        disabled_style = f"background-color: {tm.color('bg_main')}; color: {tm.color('text_muted')}; border: 1px dashed {tm.color('border')}; padding: 6px; border-radius: 4px;"

        if not self.inp_name.isReadOnly():
            self.inp_name.setStyleSheet(style)
            self.inp_url.setStyleSheet(style)
            self.inp_category.setStyleSheet(style)
        else:
            self.inp_name.setStyleSheet(disabled_style)
            self.inp_url.setStyleSheet(disabled_style)
            self.inp_category.setStyleSheet(disabled_style)

    def get_data(self):
        return {
            "name": self.inp_name.text().strip(),
            "url": self.inp_url.text().strip(),
            "category": self.inp_category.currentText().strip()
        }


class FeedLibraryDialog(BaseDialog):
    def __init__(self, parent=None, current_feeds=None, default_feeds_dict=None):
        super().__init__(parent, title="Subscription Manager", width=850)
        self.setMinimumHeight(650)

        self.current_user_feeds = current_feeds if current_feeds else []
        self.subscribed_urls = {f["url"] for f in self.current_user_feeds}
        self.display_dict = {}
        self.default_feeds_dict = default_feeds_dict or {}

        for cat, feeds in self.default_feeds_dict.items():
            self.display_dict[cat] = [f.copy() for f in feeds]

        for f in self.current_user_feeds:
            if not f.get("is_default", False):
                cat = f.get("category", "Custom Sources")
                if cat not in self.display_dict:
                    self.display_dict[cat] = []
                self.display_dict[cat].append(f.copy())

        top_bar = QHBoxLayout()
        lbl_cat = QLabel("Category / Journal:")  # 🧹 Removed Emoji
        self.combo_category = QComboBox()
        self.combo_category.addItems(list(self.display_dict.keys()))
        self.combo_category.currentTextChanged.connect(self._render_table)

        self.inp_search_lib = QLineEdit()
        self.inp_search_lib.setPlaceholderText("Search journal names...")  # 🧹 Removed Emoji
        self.inp_search_lib.textChanged.connect(self._filter_library_table)

        self.btn_add_custom = QPushButton(" Add Custom Source")  # 🧹 Removed Emoji
        self.btn_add_custom.setCursor(Qt.PointingHandCursor)
        self._tracked_buttons.append((self.btn_add_custom, "default"))
        self.btn_add_custom.clicked.connect(self._on_add_custom)

        top_bar.addWidget(lbl_cat)
        top_bar.addWidget(self.combo_category)
        top_bar.addWidget(self.inp_search_lib, stretch=1)
        top_bar.addWidget(self.btn_add_custom)
        self.content_layout.addLayout(top_bar)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Subscribe", "Journal / Source", "RSS URL", "Actions"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)  # 操作列
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.content_layout.addWidget(self.table)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)

        self.lbl_status = QLabel(f"Selected: {len(self.subscribed_urls)}")
        self.footer_layout.insertWidget(0, self.lbl_status)

        self.add_button("Cancel", self.reject)
        self.add_button("Save", self.accept, is_primary=True)

        self.checkboxes_map = {}
        self._render_table(self.combo_category.currentText())
        self.table.cellClicked.connect(self._on_cell_clicked)

        self._apply_theme()

    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm
        self.btn_add_custom.setIcon(tm.icon("add", "text_main"))  # Added SVG

        self.table.setStyleSheet(f"""
            QTableWidget {{ background-color: {tm.color('bg_card')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; gridline-color: {tm.color('bg_main')}; outline: none; }}
            QHeaderView::section {{ background-color: {tm.color('bg_input')}; color: {tm.color('text_muted')}; border: none; padding: 6px; border-right: 1px solid {tm.color('border')}; border-bottom: 1px solid {tm.color('border')}; font-weight: bold; }}
            QTableWidget::item:selected {{ background-color: {tm.color('btn_hover')}; }}
        """)
        self.combo_category.setStyleSheet(
            f"background-color: {tm.color('bg_input')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; padding: 6px; border-radius: 4px;")
        self.inp_search_lib.setStyleSheet(
            f"background-color: {tm.color('bg_input')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; padding: 6px; border-radius: 4px;")
        self.lbl_status.setStyleSheet(f"color: {tm.color('text_muted')}; font-weight: bold;")

        self._render_table(self.combo_category.currentText())


    def _render_table(self, category):
        self.table.setRowCount(0)
        self.checkboxes_map.clear()
        feeds = self.display_dict.get(category, [])
        self.table.setRowCount(len(feeds))
        tm = self.tm

        for i, feed in enumerate(feeds):
            chk = QCheckBox()
            chk.setChecked(feed["url"] in self.subscribed_urls)
            chk.toggled.connect(lambda checked, url=feed["url"]: self._on_checkbox_toggled(url, checked))

            chk_widget = QWidget()
            chk_layout = QHBoxLayout(chk_widget)
            chk_layout.addWidget(chk)
            chk_layout.setAlignment(Qt.AlignCenter)
            chk_layout.setContentsMargins(0, 0, 0, 0)

            name_item = QTableWidgetItem(f" {feed['name']}")

            if feed.get("is_default"):
                name_item.setToolTip("Built-in Default Source")
                name_item.setIcon(tm.icon("lock", "text_muted"))
                name_item.setForeground(QColor(tm.color('text_muted')))
            else:
                name_item.setToolTip("Custom Source")
                name_item.setIcon(tm.icon("tag", "accent"))
                name_item.setForeground(QColor(tm.color('text_main')))

            self.table.setCellWidget(i, 0, chk_widget)
            self.table.setItem(i, 1, name_item)
            self.table.setItem(i, 2, QTableWidgetItem(feed["url"]))

            #  列操作按钮区
            action_widget = QWidget()
            action_layout = QHBoxLayout(action_widget)
            action_layout.setContentsMargins(5, 2, 5, 2)
            action_layout.setSpacing(8)

            if not feed.get("is_default"):
                # 编辑按钮
                btn_edit = QPushButton()
                btn_edit.setIcon(tm.icon("edit", "text_main"))
                btn_edit.setToolTip("Edit Source")
                btn_edit.setCursor(Qt.PointingHandCursor)
                btn_edit.setStyleSheet("background: transparent; border: none; padding: 2px;")
                btn_edit.clicked.connect(lambda checked=False, f=feed: self._edit_custom_feed(f))

                # 删除按钮
                btn_delete = QPushButton()
                btn_delete.setIcon(tm.icon("delete", "danger"))
                btn_delete.setToolTip("Delete Source")
                btn_delete.setCursor(Qt.PointingHandCursor)
                btn_delete.setStyleSheet("background: transparent; border: none; padding: 2px;")
                btn_delete.clicked.connect(lambda checked=False, f=feed: self._delete_custom_feed(f))

                action_layout.addWidget(btn_edit)
                action_layout.addWidget(btn_delete)
            else:
                action_layout.addStretch() # 内置源占位，保持排版对其

            self.table.setCellWidget(i, 3, action_widget)

    def _on_cell_double_clicked(self, row, col):
        """双击任意列触发编辑"""
        category = self.combo_category.currentText()
        feeds = self.display_dict.get(category, [])
        if row < len(feeds):
            feed = feeds[row]
            if not feed.get("is_default"):
                self._edit_custom_feed(feed)

    def _edit_custom_feed(self, feed):
        old_cat = feed.get("category", "Custom Sources")
        old_url = feed["url"]

        dlg = FeedEditorDialog(self, feed_data=feed, is_default=False, categories=list(self.default_feeds_dict.keys()))
        if dlg.exec():
            new_data = dlg.get_data()
            if new_data["url"]:
                new_data["is_default"] = False
                new_cat = new_data.get("category", "Custom Sources")

                # 从旧分类中移除
                if old_cat in self.display_dict:
                    self.display_dict[old_cat] = [f for f in self.display_dict[old_cat] if f["url"] != old_url]

                # 加入新分类
                if new_cat not in self.display_dict:
                    self.display_dict[new_cat] = []
                    self.combo_category.addItem(new_cat)
                self.display_dict[new_cat].append(new_data)

                # 更新订阅状态缓存
                if old_url in self.subscribed_urls:
                    self.subscribed_urls.remove(old_url)
                    self.subscribed_urls.add(new_data["url"])

                # 刷新 UI
                self.combo_category.setCurrentText(new_cat)
                self._render_table(self.combo_category.currentText())

    def _delete_custom_feed(self, feed):
        cat = feed.get("category", "Custom Sources")
        url = feed["url"]

        # 从字典缓存中剥离
        if cat in self.display_dict:
            self.display_dict[cat] = [f for f in self.display_dict[cat] if f["url"] != url]

        # 从已订阅列表中剥离
        self.subscribed_urls.discard(url)
        self.lbl_status.setText(f"Selected: {len(self.subscribed_urls)}")

        # 刷新视图
        self._render_table(self.combo_category.currentText())



    def _on_cell_clicked(self, row, col):
        if col == 1:
            chk_widget = self.table.cellWidget(row, 0)
            if chk_widget:
                chk = chk_widget.layout().itemAt(0).widget()
                chk.setChecked(not chk.isChecked())

    def _filter_library_table(self, text):
        text = text.lower()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if item:
                self.table.setRowHidden(row, text not in item.text().lower())


    def _on_checkbox_toggled(self, url, is_checked):
        if is_checked:
            self.subscribed_urls.add(url)
        else:
            self.subscribed_urls.discard(url)
        self.lbl_status.setText(f"Selected: {len(self.subscribed_urls)}")

    def _on_add_custom(self):
        dlg = FeedEditorDialog(self, categories=list(self.default_feeds_dict.keys()))
        if dlg.exec():
            new_feed = dlg.get_data()
            if new_feed["url"]:
                new_feed["is_default"] = False
                cat = new_feed.get("category", "Custom Sources")

                if cat not in self.display_dict:
                    self.display_dict[cat] = []
                    self.combo_category.addItem(cat)

                self.display_dict[cat].append(new_feed)
                self.subscribed_urls.add(new_feed["url"])
                self.lbl_status.setText(f"Selected: {len(self.subscribed_urls)}")
                self.combo_category.setCurrentText(cat)

                self._render_table(cat)

    def get_final_feeds(self):
        final_list = []
        for cat, feeds in self.display_dict.items():
            for f in feeds:
                if f["url"] in self.subscribed_urls:
                    final_list.append(f)

        unique_feeds = {f["url"]: f for f in final_list}
        return list(unique_feeds.values())


class McpConfigDialog(BaseDialog):
    def __init__(self, parent=None, server_name="", server_config=None):
        title = "Edit MCP Server" if server_config else "Add MCP Server"
        super().__init__(parent, title=title, width=660)

        self.form_widget = QWidget()
        self.form_layout = QFormLayout(self.form_widget)
        self.form_layout.setSpacing(15)
        self.form_layout.setLabelAlignment(Qt.AlignRight)

        self.inp_name = QLineEdit(server_name)
        self.inp_name.setPlaceholderText("e.g. remote-database-mcp")
        if server_name in ["builtin", "external"]:
            self.inp_name.setEnabled(False)
            self.inp_name.setToolTip("Core component identifier cannot be changed")


        self.desc_container = QWidget()
        desc_v_layout = QVBoxLayout(self.desc_container)
        desc_v_layout.setContentsMargins(0, 0, 0, 0)
        desc_v_layout.setSpacing(4)

        self.inp_desc = QLineEdit()
        self.inp_desc.setPlaceholderText("e.g. Provide 12306 train ticket search capabilities")

        self.desc_hint_widget = QWidget()
        hint_layout = QHBoxLayout(self.desc_hint_widget)
        hint_layout.setContentsMargins(0, 0, 0, 0)
        hint_layout.setSpacing(6)

        self.lbl_desc_icon = QLabel()
        self.lbl_desc_icon.setFixedSize(14, 14)

        self.lbl_desc_text = QLabel(
            "<b>Crucial for AI:</b> Clearly describe the tool's purpose so the AI knows exactly when to use it.")
        self.lbl_desc_text.setWordWrap(True)
        self.lbl_desc_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        hint_layout.addWidget(self.lbl_desc_icon, 0, Qt.AlignTop)
        hint_layout.addWidget(self.lbl_desc_text, 1)

        desc_v_layout.addWidget(self.inp_desc)
        desc_v_layout.addWidget(self.desc_hint_widget)

        self.combo_type = QComboBox()
        self.combo_type.addItems(["stdio", "sse"])

        self.inp_cmd_url = QLineEdit()
        self.inp_args = QLineEdit()
        self.inp_args.setPlaceholderText("arg1, arg2 (comma-separated)")

        self.env_editor = ParamEditorWidget()

        env_btn_layout = QHBoxLayout()
        self.btn_add_env = QPushButton(" Add Entry")
        self.btn_add_env.setIcon(self.tm.icon("add", "text_main"))
        self.btn_add_env.clicked.connect(lambda: self.env_editor.add_param_row())

        self.btn_add_auth = QPushButton(" Insert Authorization Header")
        self.btn_add_auth.setIcon(self.tm.icon("lock", "warning"))
        self.btn_add_auth.clicked.connect(self._add_auth_header)

        env_btn_layout.addWidget(self.btn_add_env)
        env_btn_layout.addWidget(self.btn_add_auth)
        env_btn_layout.addStretch()

        self.env_container = QWidget()
        env_layout = QVBoxLayout(self.env_container)
        env_layout.setContentsMargins(0, 0, 0, 0)
        env_layout.addWidget(self.env_editor)
        env_layout.addLayout(env_btn_layout)

        self.lbl_args = QLabel("Arguments:")
        self.lbl_env = QLabel("Environment:")

        self.form_layout.addRow("Server ID:", self.inp_name)
        self.form_layout.addRow("Description:", self.desc_container)
        self.form_layout.addRow("Transport:", self.combo_type)
        self.form_layout.addRow("Command / URL:", self.inp_cmd_url)
        self.form_layout.addRow(self.lbl_args, self.inp_args)
        self.form_layout.addRow(self.lbl_env, self.env_container)

        if server_config:
            self.inp_desc.setText(server_config.get("description", ""))
            type_idx = {"stdio": 0, "sse": 1, "streamable_http": 2}
            self.combo_type.setCurrentIndex(type_idx.get(server_config.get("type", "stdio"), 0))

            if self.combo_type.currentIndex() == 0:  # stdio
                self.inp_cmd_url.setText(server_config.get("command", ""))
                self.inp_args.setText(", ".join(server_config.get("args", [])))
                dict_data = server_config.get("env", {})
            else:
                self.inp_cmd_url.setText(server_config.get("url", ""))
                dict_data = server_config.get("headers", {})

            if dict_data:
                param_list = [{"name": k, "type": "str", "value": str(v)} for k, v in dict_data.items()]
                self.env_editor.load_data(param_list)

        self.content_layout.addWidget(self.form_widget)

        self.btn_test = self.add_button("Test Connection", self._on_test_clicked)
        self.btn_test.setFixedSize(140, 32)
        self.footer_layout.removeWidget(self.btn_test)
        self.footer_layout.insertWidget(0, self.btn_test)

        self.add_button("Cancel", self.reject)
        self.add_button("Save", self.accept, is_primary=True)

        self.combo_type.currentIndexChanged.connect(self._on_type_changed)
        self._on_type_changed()

        self._apply_theme()
        self.adjustSize()

    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm
        self.form_widget.setStyleSheet(f"""
            QLabel {{ color: {tm.color('text_muted')}; font-size: 13px; border: none; }} 
            QLineEdit, QComboBox {{ 
                background-color: {tm.color('bg_input')}; border: 1px solid {tm.color('border')}; color: {tm.color('text_main')}; 
                border-radius: 4px; padding: 6px; selection-background-color: {tm.color('accent')}; selection-color: {tm.color('selection_fg')};
            }} 
            QLineEdit:focus, QComboBox:focus {{ border: 1px solid {tm.color('accent')}; }}
            QLineEdit:disabled {{ background-color: {tm.color('bg_main')}; color: {tm.color('text_muted')}; }}
        """)

        self.lbl_desc_icon.setPixmap(tm.icon("help", "warning").pixmap(14, 14))
        self.lbl_desc_text.setStyleSheet(
            f"color: {tm.color('text_muted')}; font-size: 11.5px; font-style: italic; border: none; background: transparent;")
        self.desc_hint_widget.setStyleSheet("background: transparent;")

        self.btn_add_auth.setStyleSheet(
            f"color: {tm.color('warning')}; font-weight: bold; background: transparent; border: none;")
        self.btn_add_env.setStyleSheet(f"color: {tm.color('text_main')}; background: transparent; border: none;")
        self.btn_test.setStyleSheet(
            f"QPushButton {{ background-color: {tm.color('btn_bg')}; color: {tm.color('warning')}; border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 5px 10px; }} QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}")

    def _on_type_changed(self):
        stype = self.combo_type.currentText()
        is_stdio = (stype == "stdio")

        self.inp_args.setVisible(is_stdio)
        self.lbl_args.setVisible(is_stdio)
        self.btn_add_auth.setVisible(not is_stdio)

        if is_stdio:
            self.inp_cmd_url.setPlaceholderText("e.g. python, npx, node")
            self.lbl_env.setText("Environment:")
        elif stype == "sse":
            self.inp_cmd_url.setPlaceholderText("e.g. http://domain.com/sse")
            self.lbl_env.setText("HTTP Headers:")

    def _add_auth_header(self):
        current_data = self.env_editor.extract_data()
        for p in current_data:
            if p.get("name") == "Authorization": return
        current_data.append({"name": "Authorization", "type": "str", "value": "Bearer "})
        self.env_editor.blockSignals(True)
        self.env_editor.load_data(current_data)
        self.env_editor.blockSignals(False)
        self.adjustSize()

    def get_config(self):
        name = self.inp_name.text().strip()
        stype = self.combo_type.currentText()
        cfg = {"type": stype, "description": self.inp_desc.text().strip()}

        raw_params = self.env_editor.extract_data()
        env_dict = {p["name"].strip(): str(p.get("value", "")) for p in raw_params if p.get("name", "").strip()}

        if stype == "stdio":
            cfg["command"] = self.inp_cmd_url.text().strip()
            args_raw = self.inp_args.text().strip()
            cfg["args"] = [a.strip() for a in args_raw.split(",") if a.strip()]
            if env_dict: cfg["env"] = env_dict
        else:
            cfg["url"] = self.inp_cmd_url.text().strip()
            if env_dict: cfg["headers"] = env_dict

        return name, cfg

    def _on_test_clicked(self):
        name, cfg = self.get_config()
        if not name or (not cfg.get("command") and not cfg.get("url")):
            StandardDialog(self, "Missing Info", "Please enter at least a Server ID and Command/URL.").exec()
            return

        self.btn_test.setEnabled(False)
        self.pd = ProgressDialog(self, "Testing Connection", f"Connecting to [{name}]...\nPlease wait...")
        self.pd.show()

        self.test_thread = QThread()
        self.test_worker = McpTestWorker(name, cfg)
        self.test_worker.moveToThread(self.test_thread)
        self.pd.sig_canceled.connect(self.test_thread.terminate)

        def cleanup_test_mcp():
            try:
                MCPManager.get_instance().disconnect_server(f"test_{name}")
            except Exception:
                pass

        self.pd.sig_canceled.connect(cleanup_test_mcp)

        self.test_thread.started.connect(self.test_worker.run)
        self.test_worker.sig_finished.connect(self._on_test_finished)
        self.test_worker.sig_finished.connect(self.test_thread.quit)
        self.test_worker.sig_finished.connect(self.test_worker.deleteLater)
        self.test_thread.finished.connect(self.test_thread.deleteLater)

        self.test_thread.start()

    def _on_test_finished(self, success, msg):
        self.btn_test.setEnabled(True)
        self.pd.close_safe()
        if success:
            StandardDialog(self, "Connection Successful", f"{msg}").exec()
        else:
            err_dialog = StandardDialog(self, "Connection Failed", f"Unable to connect to server:\n{msg}")
            err_dialog.setFixedWidth(500)
            err_dialog.exec()



class SelectKBFileDialog(BaseDialog):
    def __init__(self, parent=None, files=None):
        super().__init__(parent, title="Select Files from Knowledge Base", width=580)
        self.setMinimumHeight(500)
        self._all_files = files or []

        # --- 搜索栏 ---
        self.inp_search = QLineEdit()
        self.inp_search.setPlaceholderText("Search file names...")
        self.inp_search.textChanged.connect(self._filter_list)
        self.content_layout.addWidget(self.inp_search)

        # --- 文件列表 ---
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list_widget.setSpacing(2)
        self.list_widget.itemDoubleClicked.connect(self.accept)
        self.list_widget.itemSelectionChanged.connect(self._update_status)
        self.content_layout.addWidget(self.list_widget, stretch=1)

        self._populate_list(self._all_files)

        # --- 底部状态提示 ---
        self.lbl_status = QLabel()
        self.footer_layout.insertWidget(0, self.lbl_status)

        self.add_button("Cancel", self.reject)
        self.btn_attach = self.add_button("Attach", self.accept, is_primary=True)

        self._update_status()
        self._apply_theme()

    def _populate_list(self, files):
        self.list_widget.clear()
        for f in files:
            item = QListWidgetItem(f"  {f['name']}")
            item.setData(Qt.UserRole, f['path'])
            item.setToolTip(f['path'])
            self.list_widget.addItem(item)

    def _filter_list(self, text):
        text = text.lower()
        filtered = [f for f in self._all_files if text in f['name'].lower()]
        self._populate_list(filtered)
        self._update_status()

    def _update_status(self):
        selected = len(self.list_widget.selectedItems())
        total = self.list_widget.count()
        if selected > 0:
            self.lbl_status.setText(f"{selected} of {total} selected")
        else:
            self.lbl_status.setText(f"{total} file(s) available")

    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm

        self.inp_search.setStyleSheet(f"""
            QLineEdit {{
                background-color: {tm.color('bg_input')};
                color: {tm.color('text_main')};
                border: 1px solid {tm.color('border')};
                border-radius: 4px;
                padding: 7px 10px;
                font-size: 13px;
                selection-background-color: {tm.color('accent')};
                selection-color: {tm.color('selection_fg')};
            }}
            QLineEdit:focus {{ border: 1px solid {tm.color('accent')}; }}
        """)

        self.list_widget.setStyleSheet(f"""
            QListWidget {{
                background-color: {tm.color('bg_card')};
                color: {tm.color('text_main')};
                border: 1px solid {tm.color('border')};
                border-radius: 6px;
                padding: 4px;
                font-size: 13px;
                outline: none;
            }}
            QListWidget::item {{
                padding: 9px 8px;
                border-radius: 4px;
                border-bottom: 1px solid {tm.color('bg_main')};
            }}
            QListWidget::item:hover {{
                background-color: {tm.color('btn_hover')};
            }}
            QListWidget::item:selected {{
                background-color: {tm.color('accent')};
                color: {tm.color('selection_fg')};
                border-bottom: 1px solid {tm.color('accent')};
            }}
        """)

        self.lbl_status.setStyleSheet(
            f"color: {tm.color('text_muted')}; font-size: 12px; font-weight: bold;")

    def get_selected_paths(self):
        return [item.data(Qt.UserRole) for item in self.list_widget.selectedItems()]



class AddModelDialog(BaseDialog):
    def __init__(self, parent=None):
        super().__init__(parent, title="Add Custom Model", width=350)
        self.inp_name = QLineEdit()
        self.inp_name.setPlaceholderText("Enter model ID/name...")
        self.content_layout.addWidget(self.inp_name)

        self.add_button("Cancel", self.reject)
        self.add_button("Add", self.accept, is_primary=True)

        self._apply_theme()

    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm
        self.inp_name.setStyleSheet(
            f"background: {tm.color('bg_input')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; padding: 6px; border-radius: 4px;")

    def get_name(self):
        return self.inp_name.text().strip()


class ProgressDialog(BaseDialog):
    sig_canceled = Signal()

    def __init__(self, parent=None, title="Processing", message="Please wait...", telemetry_config=None):
        super().__init__(parent, title=title, width=540)
        self.setWindowModality(Qt.ApplicationModal)

        self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint | Qt.CustomizeWindowHint)

        if telemetry_config is None:
            self.telemetry = {"cpu": True, "ram": True, "gpu": True, "net": False, "io": True}
        else:
            self.telemetry = telemetry_config

        # --- UI 初始化 ---
        self.lbl_message = QLabel(message)
        self.lbl_message.setWordWrap(True)
        self.content_layout.addWidget(self.lbl_message)

        self.pbar = QProgressBar()
        self.pbar.setFixedHeight(18)
        self.pbar.setRange(0, 0)
        self.pbar.setAlignment(Qt.AlignCenter)
        self.pbar.setTextVisible(True)
        self.content_layout.addWidget(self.pbar)

        self.lbl_metrics = QLabel("Initializing App Profiler...")
        self.lbl_metrics.setWordWrap(True)
        self.content_layout.addWidget(self.lbl_metrics)

        self.stalled_warning_widget = QWidget()
        warn_layout = QHBoxLayout(self.stalled_warning_widget)
        warn_layout.setContentsMargins(5, 5, 5, 5)
        self.lbl_warn_icon = QLabel()
        self.lbl_warn_icon.setFixedSize(16, 16)
        self.lbl_warn_text = QLabel(
            "Task is still in progress. Large models or network latency may take extra time, please wait...")
        self.lbl_warn_text.setWordWrap(True)
        warn_layout.addWidget(self.lbl_warn_icon, 0, Qt.AlignTop)
        warn_layout.addWidget(self.lbl_warn_text, 1)
        self.stalled_warning_widget.setVisible(False)
        self.content_layout.addWidget(self.stalled_warning_widget)

        self.content_layout.addStretch()

        self.btn_cancel = self.add_button("Cancel Task", self.on_cancel_clicked, is_danger=True)

        self._apply_theme()
        self.adjustSize()

        self._last_progress = -1
        self._last_progress_time = time.time()
        self.stall_timer = QTimer(self)
        self.stall_timer.timeout.connect(self._check_stalled_progress)
        self.stall_timer.start(2000)

        self.main_process = psutil.Process(os.getpid())

        self.main_process.cpu_percent(interval=None)

        if any(self.telemetry.values()):
            self.metric_timer = QTimer(self)
            self.metric_timer.timeout.connect(self._update_metrics)
            self._last_time = time.time()
            if self.telemetry.get("net"):
                try:
                    self._last_net_io = psutil.net_io_counters()
                except:
                    self.telemetry["net"] = False
            if self.telemetry.get("io"):
                try:
                    self._last_disk_io = self._get_process_tree_io()
                except:
                    self.telemetry["io"] = False
            self.metric_timer.start(1000)
        else:
            self.lbl_metrics.setVisible(False)

    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm
        self.lbl_message.setStyleSheet(
            f"font-size: 13px; color: {tm.color('text_main')}; margin-bottom: 5px; border: none;")

        self.pbar.setStyleSheet(f"""
            QProgressBar {{ 
                border: 1px solid {tm.color('border')}; 
                background-color: {tm.color('bg_input')}; 
                border-radius: 4px; 
                color: {tm.color('text_main')}; 
                font-weight: bold; 
                font-size: 11px; 
                text-align: center; 
            }}
            QProgressBar::chunk {{ background-color: {tm.color('accent')}; border-radius: 3px; }}
        """)


        self.lbl_metrics.setStyleSheet(f"""
                   QLabel {{
                       font-family: 'Consolas', 'Courier New', monospace; 
                       color: {tm.color('success')}; font-size: 11px; background-color: {tm.color('bg_main')};
                       border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 6px; margin-top: 5px;
                   }}
               """)

        self.lbl_warn_icon.setPixmap(tm.icon("info", "warning").pixmap(16, 16))

        self.lbl_warn_icon.setStyleSheet("border: none; background: transparent;")

        self.lbl_warn_text.setStyleSheet(
            f"color: {tm.color('warning')}; font-size: 12px; font-weight: bold; border: none; background: transparent;")


        self.stalled_warning_widget.setObjectName("StallWarningBox")
        self.stalled_warning_widget.setStyleSheet(
            f"QWidget#StallWarningBox {{ background-color: {tm.color('bg_input')}; border: 1px dashed {tm.color('warning')}; border-radius: 4px; }}")


    def _format_speed(self, bytes_per_sec):
        if bytes_per_sec >= 1024 * 1024:
            return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
        elif bytes_per_sec >= 1024:
            return f"{bytes_per_sec / 1024:.0f} KB/s"
        else:
            return f"{bytes_per_sec:.0f} B/s"

    def _get_process_tree(self):
        try:
            return [self.main_process] + self.main_process.children(recursive=True)
        except psutil.NoSuchProcess:
            return [self.main_process]

    def _check_stalled_progress(self):
        if time.time() - self._last_progress_time > 120:
            if not self.stalled_warning_widget.isVisible() and self.pbar.isVisible():
                self.stalled_warning_widget.setVisible(True)


    def _get_process_tree_io(self):
        read_bytes, write_bytes = 0, 0
        for p in self._get_process_tree():
            try:
                io = p.io_counters()
                read_bytes += io.read_bytes
                write_bytes += io.write_bytes
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                pass
        return read_bytes, write_bytes

    def _update_metrics(self):
        try:
            stats = []
            curr_time = time.time()
            dt = curr_time - self._last_time
            if dt <= 0: return

            procs = self._get_process_tree()
            pids = [p.pid for p in procs]

            if self.telemetry.get("cpu"):
                app_cpu = 0.0
                for p in procs:
                    try:
                        app_cpu += p.cpu_percent(interval=None)
                    except:
                        pass
                stats.append(f"CPU: {app_cpu:04.1f}%")

            if self.telemetry.get("ram"):
                app_ram = 0
                for p in procs:
                    try:
                        app_ram += p.memory_info().rss
                    except:
                        pass
                stats.append(f"RAM: {app_ram / (1024 ** 2):.1f} MB")

            if self.telemetry.get("io"):
                curr_disk_io = self._get_process_tree_io()
                read_spd = (curr_disk_io[0] - self._last_disk_io[0]) / dt
                write_spd = (curr_disk_io[1] - self._last_disk_io[1]) / dt
                stats.append(f"I/O: R:{self._format_speed(read_spd)} W:{self._format_speed(write_spd)}")
                self._last_disk_io = curr_disk_io

            if self.telemetry.get("gpu"):
                if HAS_NVML:
                    try:
                        app_vram_mb = 0
                        sys_used_vram_gb = 0
                        gpu_usage = 0
                        device_count = pynvml.nvmlDeviceGetCount()
                        for i in range(device_count):
                            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                            gpu_usage = max(gpu_usage, util.gpu)
                            mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                            sys_used_vram_gb += mem_info.used / (1024 ** 3)
                            gpu_procs = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
                            for gp in gpu_procs:
                                if gp.pid in pids and gp.usedGpuMemory is not None:
                                    app_vram_mb += gp.usedGpuMemory / (1024 ** 2)
                        if app_vram_mb > 0:
                            stats.append(f"App VRAM: {app_vram_mb:.0f} MB [GPU: {gpu_usage}%]")
                        else:
                            stats.append(f"Sys VRAM: {sys_used_vram_gb:.1f}G [GPU: {gpu_usage}%]")
                    except:
                        stats.append("GPU: Active")
                elif torch.cuda.is_available():
                    try:
                        free, total = torch.cuda.mem_get_info()
                        used_gb = (total - free) / (1024 ** 3)
                        stats.append(f"Sys VRAM: {used_gb:.1f}G")
                    except:
                        stats.append("GPU: Active")
                else:
                    stats.append("GPU: N/A")

            if self.telemetry.get("net"):
                curr_net_io = psutil.net_io_counters()
                recv_spd = (curr_net_io.bytes_recv - self._last_net_io.bytes_recv) / dt
                sent_spd = (curr_net_io.bytes_sent - self._last_net_io.bytes_sent) / dt
                stats.append(f"Sys Net: ↓{self._format_speed(recv_spd)} ↑{self._format_speed(sent_spd)}")
                self._last_net_io = curr_net_io

            self._last_time = curr_time
            if len(stats) > 3:
                self.lbl_metrics.setText(f"{' | '.join(stats[:3])}\n{' | '.join(stats[3:])}")
            else:
                self.lbl_metrics.setText(" | ".join(stats))
        except Exception as e:
            self.lbl_metrics.setText(f"Profiler Error: {str(e)}")


    def update_progress(self, percent, msg=None):
        if percent != self._last_progress:
            self._last_progress = percent
            self._last_progress_time = time.time()
            if self.stalled_warning_widget.isVisible():
                self.stalled_warning_widget.setVisible(False)

        if percent < 0:
            if self.pbar.maximum() != 0:
                self.pbar.setRange(0, 0)
                self.pbar.setTextVisible(False)
        else:
            if self.pbar.maximum() == 0:
                self.pbar.setRange(0, 100)
                self.pbar.setTextVisible(True)
            self.pbar.setValue(percent)
        if msg: self.lbl_message.setText(msg)

    def show_success_state(self, title="Success", message="Task completed successfully."):
        if hasattr(self, 'metric_timer'): self.metric_timer.stop()
        if hasattr(self, 'stall_timer'): self.stall_timer.stop()
        self.lbl_metrics.setVisible(False)
        self.stalled_warning_widget.setVisible(False)

        self.pbar.setVisible(False)

        self.setWindowTitle(title)

        self.setWindowFlags(self.windowFlags() | Qt.WindowCloseButtonHint)
        self.show()

        self.lbl_message.setText(message)
        self.btn_cancel.setText("OK")
        self.btn_cancel.setEnabled(True)

        tm = self.tm
        self.btn_cancel.setStyleSheet(f"""
            QPushButton {{ background-color: {tm.color('accent')}; color: {tm.color('bg_main')}; border-radius: 4px; border: none; font-weight:bold;}}
            QPushButton:hover {{ background-color: {tm.color('accent_hover')}; }}
        """)

        try:
            self.btn_cancel.clicked.disconnect()
        except:
            pass
        self.btn_cancel.clicked.connect(self.accept)


    def on_cancel_clicked(self):
        self.lbl_message.setText("Stopping... forcing termination.")
        self.btn_cancel.setEnabled(False)

        # 1. 停止监控
        if hasattr(self, 'metric_timer'): self.metric_timer.stop()
        if hasattr(self, 'stall_timer'): self.stall_timer.stop()

        # 2. 发送信号给 TaskManager 去杀进程
        self.sig_canceled.emit()

        # 3. 强制关闭窗口（不再无限等待后台）
        QTimer.singleShot(500, self.reject)

    def close_safe(self):
        if hasattr(self, 'metric_timer'): self.metric_timer.stop()
        if hasattr(self, 'stall_timer'): self.stall_timer.stop()
        self.accept()


    def closeEvent(self, event):
        if hasattr(self, 'metric_timer'): self.metric_timer.stop()
        if hasattr(self, 'stall_timer'): self.stall_timer.stop()
        super().closeEvent(event)


class UnsavedChangesDialog(BaseDialog):
    def __init__(self, parent):
        super().__init__(parent, title="Unsaved Modifications", width=460)
        self.user_choice = "close"  # Default fallback action

        # Configure the message label
        msg_label = QLabel(
            "You have unsaved configuration changes.\n"
            "Please specify how you would like to proceed before navigating away:"
        )
        msg_label.setWordWrap(True)
        msg_label.setStyleSheet(
            f"color: {self.tm.color('text_main')}; font-size: 14px; border: none; background: transparent;"
        )
        self.content_layout.addWidget(msg_label)

        # Callback generator for buttons
        def set_choice(action, accept=False):
            self.user_choice = action
            self.accept() if accept else self.reject()

        # Construct Footer Buttons
        btn_close = self.add_button("Close", lambda: set_choice("close"))
        btn_revert = self.add_button("Revert Changes", lambda: set_choice("revert"), is_danger=True)
        btn_save = self.add_button("Save Settings", lambda: set_choice("save", True), is_primary=True)

        btn_close.setFixedWidth(80)
        btn_revert.setFixedWidth(130)
        btn_save.setFixedWidth(130)



class ExportPasswordDialog(BaseDialog):
    def __init__(self, parent):
        super().__init__(parent, title="Export Security", width=420)
        self.password = None
        self.is_cancelled = True
        self.regex = re.compile(r'^[a-zA-Z0-9@_\-+=!#$&^*]+$')

        lbl = QLabel(
            "Set a password to encrypt the exported configuration.\nLeave completely blank for an unencrypted JSON export.")
        lbl.setWordWrap(True)
        lbl.setStyleSheet(
            f"color: {self.tm.color('text_main')}; font-size: 13px; border: none; background: transparent;")

        self.inp_pass = QLineEdit()
        self.inp_pass.setEchoMode(QLineEdit.Password)
        self.inp_pass.setPlaceholderText("Min 6 chars (a-zA-Z0-9@_-+=!#$&^*)")
        self.inp_pass.setStyleSheet(
            f"background: {self.tm.color('bg_input')}; color: {self.tm.color('text_main')}; border: 1px solid {self.tm.color('border')}; padding: 6px; border-radius: 4px;")

        self.content_layout.addWidget(lbl)
        self.content_layout.addWidget(self.inp_pass)

        btn_cancel = self.add_button("Cancel", self.reject)
        btn_confirm = self.add_button("Confirm", self._validate, is_primary=True)
        btn_cancel.setFixedWidth(100)
        btn_confirm.setFixedWidth(100)

    def _validate(self):
        pwd = self.inp_pass.text()
        if not pwd:
            self.is_cancelled = False
            self.accept()
            return

        if len(pwd) < 6 or not self.regex.match(pwd):
            ToastManager().show("Invalid password! Min 6 chars. Allowed: a-zA-Z0-9@_-+=!#$&^*", "error")
            return

        self.password = pwd
        self.is_cancelled = False
        self.accept()


class ImportPasswordDialog(BaseDialog):
    def __init__(self, parent):
        super().__init__(parent, title="Encrypted Bundle Detected", width=420)
        self.password = None
        self.is_cancelled = True

        lbl = QLabel("This configuration is encrypted.\nPlease enter the password to unlock:")
        lbl.setWordWrap(True)
        lbl.setStyleSheet(
            f"color: {self.tm.color('text_main')}; font-size: 13px; border: none; background: transparent;")

        self.inp_pass = QLineEdit()
        self.inp_pass.setEchoMode(QLineEdit.Password)
        self.inp_pass.setPlaceholderText("Enter decryption password...")
        self.inp_pass.setStyleSheet(
            f"background: {self.tm.color('bg_input')}; color: {self.tm.color('text_main')}; border: 1px solid {self.tm.color('border')}; padding: 6px; border-radius: 4px;")

        self.content_layout.addWidget(lbl)
        self.content_layout.addWidget(self.inp_pass)

        btn_cancel = self.add_button("Cancel", self.reject)
        btn_confirm = self.add_button("Confirm", self._validate, is_primary=True)
        btn_cancel.setFixedWidth(100)
        btn_confirm.setFixedWidth(100)

    def _validate(self):
        pwd = self.inp_pass.text()
        if not pwd:
            from src.ui.components.toast import ToastManager
            ToastManager().show("Password cannot be empty.", "error")
            return
        self.password = pwd
        self.is_cancelled = False
        self.accept()


class ProjectEditorDialog(BaseDialog):
    def __init__(self, parent=None, is_edit=False, current_data=None):
        title = "Edit Library Info" if is_edit else "Create New Library"
        super().__init__(parent, title=title, width=480)

        self.form_widget = QWidget()
        self.form_layout = QFormLayout(self.form_widget)
        self.form_layout.setSpacing(15)
        self.form_layout.setLabelAlignment(Qt.AlignRight)

        self.inp_name = QLineEdit()
        self.inp_name.setPlaceholderText("e.g. Cotton Genomics")
        self.form_layout.addRow("Name:", self.inp_name)

        self.inp_domain = QLineEdit()
        self.inp_domain.setPlaceholderText("e.g. Plant Biology")

        regex = QRegularExpression(r"^[a-zA-Z0-9\s\-_.,]*$")
        validator = QRegularExpressionValidator(regex, self.inp_domain)
        self.inp_domain.setValidator(validator)

        self.form_layout.addRow("Domain:", self.inp_domain)

        self.lbl_domain_hint = QLabel("This is the focus area for AI analysis and processing.")
        self.lbl_domain_hint.setWordWrap(True)
        self.form_layout.addRow("", self.lbl_domain_hint)

        self.inp_desc = QTextEdit()
        self.inp_desc.setPlaceholderText("Optional description...")
        self.inp_desc.setMaximumHeight(70)
        self.form_layout.addRow("Desc:", self.inp_desc)

        self.combo_model = QComboBox()
        active_models = EMBEDDING_MODELS
        for m in active_models:
            self.combo_model.addItem(m['ui_name'], m['id'])
        self.form_layout.addRow("AI Model:", self.combo_model)

        self.content_layout.addWidget(self.form_widget)

        if is_edit and current_data:
            self.inp_name.setText(current_data.get('name', ''))
            self.inp_domain.setText(current_data.get('domain', ''))
            self.inp_desc.setText(current_data.get('description', ''))
            current_mid = current_data.get('model_id')
            idx = self.combo_model.findData(current_mid)
            if idx >= 0: self.combo_model.setCurrentIndex(idx)
            self.model_warn = QLabel(
                "Changing the model invalidates existing vector data. Index rebuild required after saving.")
            self.model_warn.setWordWrap(True)
            self.form_layout.addRow("", self.model_warn)

        self.add_button("Cancel", self.reject)
        self.add_button("Save", self.accept, is_primary=True)

        self._apply_theme()

    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm
        self.form_widget.setStyleSheet(f"""
            QLabel {{ color: {tm.color('text_muted')}; font-size: 13px; border: none; }} 
            QLineEdit, QTextEdit, QComboBox {{ 
                background-color: {tm.color('bg_input')}; border: 1px solid {tm.color('border')}; color: {tm.color('text_main')}; 
                border-radius: 4px; padding: 5px; selection-background-color: {tm.color('accent')}; 
            }} 
            QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{ border: 1px solid {tm.color('accent')}; }}
        """)
        if hasattr(self, 'model_warn'):
            self.model_warn.setStyleSheet(
                f"color: {tm.color('warning')}; font-size: 11px; font-weight: bold; border: none;")

        if hasattr(self, 'lbl_domain_hint'):
            self.lbl_domain_hint.setStyleSheet(f"color: {tm.color('text_muted')}; font-size: 11px; font-style: italic;")

    def get_data(self):
        return {
            "name": self.inp_name.text().strip(),
            "domain": self.inp_domain.text().strip(),
            "description": self.inp_desc.toPlainText().strip(),
            "model_id": self.combo_model.currentData()
        }


class McpTestWorker(QObject):
    """后台测试 MCP 连接的线程，防止阻塞主 UI"""
    sig_finished = Signal(bool, str)

    def __init__(self, server_name, config):
        super().__init__()
        self.server_name = server_name
        # 测试时强制起一个临时名字，防止污染实际的连接池
        self.test_name = f"test_{server_name}"
        self.config = config

    def run(self):
        try:
            mgr = MCPManager.get_instance()

            # 1. 尝试同步连接 (底层最多等待 10 秒)
            success = mgr._sync_start(self.test_name, self.config)
            status = mgr.get_server_status(self.test_name)

            # 2. 获取加载的工具数量作为成功提示
            if success:
                tool_count = sum(1 for v in mgr.tool_map.values() if v == self.test_name)
                msg = f"连接成功！共加载了 {tool_count} 个可用工具。"
            else:
                msg = status

            # 3. 测试完毕后立即断开清理，不占用系统资源
            mgr.disconnect_server(self.test_name)

            self.sig_finished.emit(success, msg)
        except Exception as e:
            self.sig_finished.emit(False, str(e))


class ApiProvidersDialog(BaseDialog):
    def __init__(self, parent=None):
        super().__init__(parent, title="Data Providers & External APIs", width=850)
        self.setMinimumHeight(600)

        self.providers = [
            ("NCBI Entrez", "Genomics & Literature", "Access to PubMed, Taxonomy, SRA, GEO, and other core databases."),
            ("Semantic Scholar", "Literature Search", "AI-backed academic search and citation graph traversal."),
            ("OpenAlex", "Literature Search", "Open catalog of the global research system and citation metrics."),
            ("Crossref", "Literature Search", "Digital Object Identifier (DOI) registration and metadata tracking."),
            ("UniProt", "Protein Database", "Comprehensive resource for protein sequences, annotations, and mapping."),
            ("RCSB PDB", "Structural Biology",
             "Information about the 3D shapes of proteins, nucleic acids, and complexes."),
            ("STRING DB", "Systems Biology",
             "Protein-protein interaction networks and functional enrichment analysis."),
            ("Ensembl", "Genomics", "Centralized resource for genetics, molecular biology, and genomic annotations."),
            ("KEGG", "Pathways", "Database resource for understanding high-level functions of the biological system."),
            ("PubChem", "Cheminformatics", "World's largest collection of freely accessible chemical information."),
            ("ChEMBL", "Pharmacology", "Manually curated database of bioactive molecules with drug-like properties."),
            ("Europe PMC", "Preprints", "Access to life sciences publications and preprints (bioRxiv, medRxiv)."),
            ("Wikipedia", "General Knowledge", "Free online encyclopedia for quick concept and entity summaries."),
            ("GitHub API", "Code & Repositories", "Search for open-source bioinformatics pipelines and academic code.")
        ]

        self.providers.sort(key=lambda item: item[0].lower())

        self.table = QTableWidget(len(self.providers), 3)
        self.table.setHorizontalHeaderLabels(["Data Provider", "Domain / Type", "Purpose & Description"])
        self.table.setWordWrap(True)

        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)

        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)

        for i, (pkg, domain, desc) in enumerate(self.providers):
            pkg_item = QTableWidgetItem(f" {pkg}")
            pkg_item.setForeground(QColor(self.tm.color('academic_blue')))

            domain_item = QTableWidgetItem(domain)
            domain_item.setForeground(QColor(self.tm.color('text_main')))

            self.table.setItem(i, 0, pkg_item)
            self.table.setItem(i, 1, domain_item)
            self.table.setItem(i, 2, QTableWidgetItem(desc))

        self.table.resizeRowsToContents()
        total_h = self.table.horizontalHeader().height()
        for r in range(self.table.rowCount()):
            row_h = self.table.rowHeight(r) + 24
            self.table.setRowHeight(r, row_h)
            total_h += row_h

        self.table.setFixedHeight(total_h + 10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("background: transparent;")

        scroll_content = QWidget()
        scroll_content.setStyleSheet("background: transparent;")
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(0)
        scroll_layout.addWidget(self.table)

        bottom_spacer = QWidget()
        bottom_spacer.setFixedHeight(50)
        scroll_layout.addWidget(bottom_spacer)

        scroll.setWidget(scroll_content)
        self.content_layout.addWidget(scroll)

        lbl_thanks = QLabel("Powered by the generous open APIs of the global scientific community.")
        lbl_thanks.setStyleSheet(f"color: {self.tm.color('text_muted')}; font-style: italic; font-size: 11px;")
        lbl_thanks.setAlignment(Qt.AlignCenter)
        self.content_layout.addWidget(lbl_thanks)

        self.add_button("Close", self.accept, is_primary=True)
        self._apply_theme()

    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm
        self.table.setStyleSheet(f"""
            QTableWidget {{ 
                background-color: transparent; 
                color: {tm.color('text_main')}; 
                border: none;
                alternate-background-color: {tm.color('bg_input')};
            }}
            QHeaderView::section {{ 
                background-color: {tm.color('bg_card')}; 
                color: {tm.color('text_muted')}; 
                padding: 10px; 
                border: none;
                border-bottom: 2px solid {tm.color('border')};
                font-weight: bold;
            }}
            QTableWidget::item {{ 
                padding: 12px; 
                border: none;
            }}
        """)

class LicenseDialog(BaseDialog):
    def __init__(self, parent=None):
        super().__init__(parent, title="Open Source Licenses", width=800)
        self.setMinimumHeight(600)

        self.PYTORCH_FULL_TEXT =\
        """
From PyTorch:

Copyright (c) 2016-     Facebook, Inc            (Adam Paszke)
Copyright (c) 2014-     Facebook, Inc            (Soumith Chintala)
Copyright (c) 2011-2014 Idiap Research Institute (Ronan Collobert)
Copyright (c) 2012-2014 Deepmind Technologies    (Koray Kavukcuoglu)
Copyright (c) 2011-2012 NEC Laboratories America (Koray Kavukcuoglu)
Copyright (c) 2011-2013 NYU                      (Clement Farabet)
Copyright (c) 2006-2010 NEC Laboratories America (Ronan Collobert, Leon Bottou, Iain Melvin, Jason Weston)
Copyright (c) 2006      Idiap Research Institute (Samy Bengio)
Copyright (c) 2001-2004 Idiap Research Institute (Ronan Collobert, Samy Bengio, Johnny Mariethoz)

From Caffe2:

Copyright (c) 2016-present, Facebook Inc. All rights reserved.

All contributions by Facebook:
Copyright (c) 2016 Facebook Inc.

All contributions by Google:
Copyright (c) 2015 Google Inc.
All rights reserved.

All contributions by Yangqing Jia:
Copyright (c) 2015 Yangqing Jia
All rights reserved.

All contributions by Kakao Brain:
Copyright 2019-2020 Kakao Brain

All contributions by Cruise LLC:
Copyright (c) 2022 Cruise LLC.
All rights reserved.

All contributions by Tri Dao:
Copyright (c) 2024 Tri Dao.
All rights reserved.

All contributions by Arm:
Copyright (c) 2021, 2023-2025 Arm Limited and/or its affiliates

All contributions from Caffe:
Copyright(c) 2013, 2014, 2015, the respective contributors
All rights reserved.

All other contributions:
Copyright(c) 2015, 2016 the respective contributors
All rights reserved.

Caffe2 uses a copyright model similar to Caffe: each contributor holds
copyright over their contributions to Caffe2. The project versioning records
all such contribution and copyright details. If a contributor wants to further
mark their specific copyright on a particular contribution, they should
indicate their copyright solely in the commit message of the change when it is
committed.

All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright
   notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright
   notice, this list of conditions and the following disclaimer in the
   documentation and/or other materials provided with the distribution.

3. Neither the names of Facebook, Deepmind Technologies, NYU, NEC Laboratories America
   and IDIAP Research Institute nor the names of its contributors may be
   used to endorse or promote products derived from this software without
   specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
        """


        self.licenses = [
            ("PySide6", "LGPL v3", "Official Python bindings for Qt."),
            ("PyMuPDF / 4LLM", "AGPL v3", "High-performance PDF & Document parsing."),
            ("ChromaDB", "Apache 2.0", "AI-native open-source vector database."),
            ("LangChain / Splitters", "MIT", "Advanced text chunking and LLM framework."),
            ("PyTorch", "BSD 3-Clause License", "Tensors and Dynamic neural networks."),
            ("ONNX Runtime", "MIT", "Cross-platform AI model accelerator."),
            ("Optimum / ONNX", "Apache 2.0", "Hardware-specific AI model optimization."),
            ("Scikit-learn", "BSD-3-Clause", "Machine learning and data mining tools."),
            ("BioPython", "Biopython", "Tools for biological computation."),
            ("NetworkX", "BSD-3-Clause", "Study of complex networks and graphs."),
            ("BeautifulSoup4", "MIT", "Screen-scraping library for HTML/XML."),
            ("DuckDuckGo Search", "MIT", "Search engine integration without tracking."),
            ("Curl-cffi", "MIT", "Python binding for curl-impersonate."),
            ("OpenAI", "Apache 2.0", "OpenAI Python API library."),
            ("MCP SDK", "MIT", "Model Context Protocol Python SDK."),
            ("Cryptography", "Apache 2.0", "Core cryptographic recipes and primitives."),
            ("Psutil", "BSD-3-Clause", "Cross-platform process and system utilities."),
            ("NVIDIA-ML-PY", "BSD-3-Clause", "Python bindings for NVIDIA Management Library."),
            ("Keyring", "MIT", "Store and access credentials safely."),
            ("Python-docx", "MIT", "Create and update Microsoft Word .docx files."),
            ("Email-validator", "public domain", "Robust email syntax and deliverability validation."),
            ("Ddisposable-email-domains", "MIT", "List of disposable email domains."),
            ("Langdetect", "MIT", "Language detection library port."),
            ("Markdown", "BSD-3-Clause", "Python implementation of Markdown."),
            ("PyQtDarkTheme", "MIT", "Flat dark theme for PySide/PyQt."),
            ("Chardet", "MIT", "Universal character encoding detector."),
            ("hf_xet", "Apache Software License", "Efficient large-file storage for Hugging Face."),
            ("Mermaid.js", "MIT", "Generation of diagrams and flowcharts."),
        ]

        self.licenses.sort(key=lambda item: item[0].lower())
        self.table = QTableWidget(len(self.licenses), 3)
        self.table.setHorizontalHeaderLabels(["Package", "License", "Purpose"])

        self.table.setWordWrap(True)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)

        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)

        for i, (pkg, lic, desc) in enumerate(self.licenses):
            if pkg == "PyTorch":
                link_label = QLabel(
                    f'<a href="#pytorch" style="color: {self.tm.color("accent")}; text-decoration: underline;">{pkg}</a>')
                link_label.setOpenExternalLinks(False)  # 禁止外部浏览器打开
                link_label.setCursor(Qt.PointingHandCursor)
                link_label.linkActivated.connect(self._show_pytorch_license)

                container = QWidget()
                cell_layout = QHBoxLayout(container)
                cell_layout.setContentsMargins(12, 0, 0, 0)
                cell_layout.addWidget(link_label)
                self.table.setCellWidget(i, 0, container)
            else:
                pkg_item = QTableWidgetItem(pkg)
                pkg_item.setForeground(QColor(self.tm.color('accent')))
                self.table.setItem(i, 0, pkg_item)

            self.table.setItem(i, 1, QTableWidgetItem(lic))
            self.table.setItem(i, 2, QTableWidgetItem(desc))

        self.content_layout.addWidget(self.table)


        lbl_thanks = QLabel("Thanks to all the maintainers of these incredible projects.")
        lbl_thanks.setStyleSheet(f"color: {self.tm.color('text_muted')}; font-style: italic; font-size: 11px;")
        lbl_thanks.setAlignment(Qt.AlignCenter)
        self.content_layout.addWidget(lbl_thanks)

        self.add_button("Close", self.accept, is_primary=True)
        self._apply_theme()

    def _show_pytorch_license(self):
        dlg = StandardDialog(
            self,
            title="PyTorch / Caffe2 License",
            message=self.PYTORCH_FULL_TEXT
        )

        dlg.setFixedWidth(600)
        dlg.exec()

    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm
        # 优化表格样式，使其融入主窗体
        self.table.setStyleSheet(f"""
            QTableWidget {{ 
                background-color: transparent; 
                color: {tm.color('text_main')}; 
                border: none;
                alternate-background-color: {tm.color('bg_input')};
            }}
            QHeaderView::section {{ 
                background-color: {tm.color('bg_card')}; 
                color: {tm.color('text_muted')}; 
                padding: 10px; 
                border: none;
                border-bottom: 2px solid {tm.color('border')};
                font-weight: bold;
                font-family: 'Segoe UI';
            }}
            QTableWidget::item {{ 
                padding: 12px; 
                border: none;
            }}
        """)