import os
import time
import psutil
import torch
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QWidget, QFrame, QFormLayout,
                               QLineEdit, QTextEdit, QComboBox, QProgressBar,
                               QSizePolicy, QGraphicsDropShadowEffect, QHeaderView, QAbstractItemView, QTableWidget,
                               QCheckBox, QTableWidgetItem)
from PySide6.QtCore import Qt, Signal, QTimer, QThread, QObject
from PySide6.QtGui import QColor

from src.core.mcp_manager import MCPManager
from src.core.models_registry import EMBEDDING_MODELS
from src.ui.components.param_editor import ParamEditorWidget
from src.core.theme_manager import ThemeManager


class BaseDialog(QDialog):
    def __init__(self, parent=None, title="Dialog", width=450):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(width)
        self._drag_pos = None

        self.tm = ThemeManager()
        self._tracked_buttons = []

        self.main_frame = QFrame(self)
        self.main_frame.setObjectName("MainFrame")

        # 阴影
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(15)
        shadow.setXOffset(0)
        shadow.setYOffset(4)
        shadow.setColor(QColor(0, 0, 0, 80))
        self.main_frame.setGraphicsEffect(shadow)

        self.v_layout = QVBoxLayout(self.main_frame)
        self.v_layout.setContentsMargins(0, 0, 0, 0)
        self.v_layout.setSpacing(0)

        # --- 标题栏 ---
        self.title_bar = QWidget()
        self.title_bar.setFixedHeight(40)
        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(15, 0, 10, 0)

        self.lbl_title = QLabel(title)
        title_layout.addWidget(self.lbl_title)
        title_layout.addStretch()

        self.btn_close = QPushButton("✕")
        self.btn_close.setFixedSize(30, 30)
        self.btn_close.clicked.connect(self.reject)
        title_layout.addWidget(self.btn_close)
        self.v_layout.addWidget(self.title_bar)

        # --- 内容区 ---
        self.content_widget = QWidget()
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(20, 20, 20, 20)
        self.content_layout.setSpacing(15)
        self.v_layout.addWidget(self.content_widget, 1)

        # --- 底部按钮区 ---
        self.footer_widget = QWidget()
        self.footer_widget.setFixedHeight(55)

        self.footer_layout = QHBoxLayout(self.footer_widget)
        self.footer_layout.setContentsMargins(15, 0, 15, 0)
        self.footer_layout.addStretch()
        self.v_layout.addWidget(self.footer_widget)

        window_layout = QVBoxLayout(self)
        window_layout.setContentsMargins(10, 10, 10, 10)
        window_layout.addWidget(self.main_frame)

        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.MinimumExpanding)

        self.tm.theme_changed.connect(self._apply_theme)

    def _apply_theme(self):
        tm = self.tm
        self.main_frame.setStyleSheet(f"""
            QFrame#MainFrame {{
                background-color: {tm.color('bg_main')};
                border: 1px solid {tm.color('border')};
                border-radius: 6px;
            }}
        """)

        self.title_bar.setStyleSheet(f"""
            background-color: {tm.color('bg_card')}; 
            border-top-left-radius: 6px; 
            border-top-right-radius: 6px; 
            border-bottom: 1px solid {tm.color('border')};
        """)

        self.lbl_title.setStyleSheet(f"""
            color: {tm.color('text_main')}; font-weight: bold; font-family: 'Segoe UI'; font-size: 13px; border: none;
        """)

        self.btn_close.setStyleSheet(f"""
            QPushButton {{ border: none; color: {tm.color('text_muted')}; background: transparent; font-weight: bold; font-size: 14px; }}
            QPushButton:hover {{ color: {tm.color('bg_main')}; background-color: {tm.color('danger')}; border-radius: 4px; }}
        """)

        self.footer_widget.setStyleSheet(f"""
            background-color: {tm.color('bg_card')}; 
            border-bottom-left-radius: 6px; 
            border-bottom-right-radius: 6px; 
            border-top: 1px solid {tm.color('border')};
        """)

        # 更新所有被跟踪的按钮样式
        for btn, b_type in self._tracked_buttons:
            self._update_button_style(btn, b_type)

    def _update_button_style(self, btn, b_type):
        tm = self.tm
        base_style = "QPushButton { border-radius: 4px; font-family: 'Segoe UI'; font-size: 13px; font-weight: 500; }"

        if b_type == "primary":
            style = base_style + f"""
                QPushButton {{ background-color: {tm.color('accent')}; color: {tm.color('bg_main')}; border: 1px solid {tm.color('accent')}; }}
                QPushButton:hover {{ background-color: {tm.color('accent_hover')}; }}
            """
        elif b_type == "danger":
            style = base_style + f"""
                QPushButton {{ background-color: transparent; color: {tm.color('danger')}; border: 1px solid {tm.color('danger')}; }}
                QPushButton:hover {{ background-color: {tm.color('danger')}; color: {tm.color('bg_main')}; }}
            """
        else:
            style = base_style + f"""
                QPushButton {{ background-color: {tm.color('btn_bg')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; }}
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

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            global_pos = event.globalPosition().toPoint()
            if self.title_bar.geometry().contains(self.main_frame.mapFromGlobal(global_pos)):
                self._drag_pos = global_pos - self.frameGeometry().topLeft()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self._drag_pos:
            global_pos = event.globalPosition().toPoint()
            self.move(global_pos - self._drag_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)


class StandardDialog(BaseDialog):
    def __init__(self, parent=None, title="Notification", message="", show_cancel=False):
        super().__init__(parent, title=title, width=420)
        self.msg_label = QLabel(message)
        self.msg_label.setWordWrap(True)
        self.content_layout.addWidget(self.msg_label)

        if show_cancel:
            self.add_button("Cancel", self.reject)
        self.add_button("OK", self.accept, is_primary=True)

        self._apply_theme()
        self.adjustSize()

    def _apply_theme(self):
        super()._apply_theme()
        self.msg_label.setStyleSheet(
            f"color: {self.tm.color('text_main')}; font-size: 14px; padding: 5px; border: none;")


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
            self.form_layout.addRow("", QLabel("🔒 Built-in source: Read-only."))

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
        title = "编辑 MCP 服务器配置" if server_config else "添加 MCP 服务器"
        super().__init__(parent, title=title, width=560)

        self.form_widget = QWidget()
        self.form_layout = QFormLayout(self.form_widget)
        self.form_layout.setSpacing(15)
        self.form_layout.setLabelAlignment(Qt.AlignRight)

        self.inp_name = QLineEdit(server_name)
        self.inp_name.setPlaceholderText("例如: remote-database-mcp")
        if server_name in ["builtin", "external"]:
            self.inp_name.setEnabled(False)
            self.inp_name.setToolTip("核心组件标识符不可更改")

        self.combo_type = QComboBox()
        self.combo_type.addItems(["stdio", "sse"])

        self.inp_cmd_url = QLineEdit()
        self.inp_args = QLineEdit()
        self.inp_args.setPlaceholderText("arg1, arg2 (使用英文逗号分隔)")

        self.env_editor = ParamEditorWidget()

        env_btn_layout = QHBoxLayout()
        self.btn_add_env = QPushButton("➕ 添加一条")
        self.btn_add_env.clicked.connect(lambda: self.env_editor.add_param_row())

        self.btn_add_auth = QPushButton("🔑 一键填入 Authorization")
        self.btn_add_auth.clicked.connect(self._add_auth_header)

        env_btn_layout.addWidget(self.btn_add_env)
        env_btn_layout.addWidget(self.btn_add_auth)
        env_btn_layout.addStretch()

        self.env_container = QWidget()
        env_layout = QVBoxLayout(self.env_container)
        env_layout.setContentsMargins(0, 0, 0, 0)
        env_layout.addWidget(self.env_editor)
        env_layout.addLayout(env_btn_layout)

        self.lbl_args = QLabel("启动参数:")
        self.lbl_env = QLabel("环境变量:")

        self.form_layout.addRow("服务器标识:", self.inp_name)
        self.form_layout.addRow("传输协议:", self.combo_type)
        self.form_layout.addRow("启动命令/URL:", self.inp_cmd_url)
        self.form_layout.addRow(self.lbl_args, self.inp_args)
        self.form_layout.addRow(self.lbl_env, self.env_container)

        if server_config:
            type_idx = 0 if server_config.get("type", "stdio") == "stdio" else 1
            self.combo_type.setCurrentIndex(type_idx)

            if type_idx == 0:
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

        self.btn_test = self.add_button("🧪 测试连接", self._on_test_clicked)
        self.footer_layout.removeWidget(self.btn_test)
        self.footer_layout.insertWidget(0, self.btn_test)

        self.add_button("取消", self.reject)
        self.add_button("保存", self.accept, is_primary=True)

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
                border-radius: 4px; padding: 6px; selection-background-color: {tm.color('accent')}; 
            }} 
            QLineEdit:focus, QComboBox:focus {{ border: 1px solid {tm.color('accent')}; }}
            QLineEdit:disabled {{ background-color: {tm.color('bg_main')}; color: {tm.color('text_muted')}; }}
        """)
        self.btn_add_auth.setStyleSheet(
            f"color: {tm.color('warning')}; font-weight: bold; background: transparent; border: none;")
        self.btn_add_env.setStyleSheet(f"color: {tm.color('text_main')}; background: transparent; border: none;")
        self.btn_test.setStyleSheet(
            f"QPushButton {{ background-color: {tm.color('btn_bg')}; color: {tm.color('warning')}; border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 5px 10px; }} QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}")

    def _on_type_changed(self):
        is_stdio = self.combo_type.currentText() == "stdio"
        self.inp_args.setVisible(is_stdio)
        self.lbl_args.setVisible(is_stdio)
        self.btn_add_auth.setVisible(not is_stdio)

        if is_stdio:
            self.inp_cmd_url.setPlaceholderText("例如: python, npx, /usr/bin/node")
            self.lbl_env.setText("环境变量:")
        else:
            self.inp_cmd_url.setPlaceholderText("例如: http://192.168.1.10:8000/sse")
            self.lbl_env.setText("HTTP请求头:")

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
        cfg = {"type": stype, "description": f"Custom {stype} server"}

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
            StandardDialog(self, "信息缺失", "请至少填入服务器标识与命令/URL。").exec()
            return

        self.pd = ProgressDialog(self, "测试连接", f"正在尝试连接至 [{name}]...\n请稍候...")
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
        self.pd.close_safe()
        if success:
            StandardDialog(self, "连接成功", f"✅ {msg}").exec()
        else:
            err_dialog = StandardDialog(self, "连接失败", f"❌ 无法连接到服务器：\n{msg}")
            err_dialog.setFixedWidth(500)
            err_dialog.exec()


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
        self.btn_close.setVisible(False)

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
        self.content_layout.addStretch()

        self.btn_cancel = self.add_button("Cancel Task", self.on_cancel_clicked, is_danger=True)

        self._apply_theme()
        self.adjustSize()

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

        # 处理在 show_success_state 中修改过的按钮样式
        if self.btn_cancel.text() == "OK":
            self.btn_cancel.setStyleSheet(f"""
                QPushButton {{ background-color: {tm.color('accent')}; color: {tm.color('bg_main')}; border-radius: 4px; border: none; font-weight:bold;}}
                QPushButton:hover {{ background-color: {tm.color('accent_hover')}; }}
            """)

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
                stats.append(f"🖥️ CPU: {app_cpu:04.1f}%")

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
        self.lbl_metrics.setVisible(False)
        self.pbar.setVisible(False)
        self.lbl_title.setText(title)
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
        """修复逻辑：点击取消后，给后台发送信号，并设置500ms强制关闭定时器"""
        self.lbl_message.setText("Stopping... forcing termination.")
        self.btn_cancel.setEnabled(False)

        # 1. 停止监控
        if hasattr(self, 'metric_timer'): self.metric_timer.stop()

        # 2. 发送信号给 TaskManager 去杀进程
        self.sig_canceled.emit()

        # 3. 强制关闭窗口（不再无限等待后台）
        QTimer.singleShot(500, self.reject)

    def close_safe(self):
        if hasattr(self, 'metric_timer'): self.metric_timer.stop()
        self.accept()

    def closeEvent(self, event):
        if hasattr(self, 'metric_timer'): self.metric_timer.stop()
        super().closeEvent(event)


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
        self.form_layout.addRow("Domain:", self.inp_domain)

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