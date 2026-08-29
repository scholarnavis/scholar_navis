"""RSS source editor and subscription manager dialogs."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QWidget)

from src.ui.components.combo import BaseComboBox
from src.ui.components.dialogs.base import BaseDialog

__all__ = ["FeedEditorDialog", "FeedLibraryDialog"]


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

        self.inp_category = BaseComboBox()
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
        lbl_cat = QLabel("Category / Journal:")
        self.combo_category = BaseComboBox()
        self.combo_category.addItems(list(self.display_dict.keys()))
        self.combo_category.currentTextChanged.connect(self._render_table)

        self.inp_search_lib = QLineEdit()
        self.inp_search_lib.setPlaceholderText("Search journal names...")
        self.inp_search_lib.textChanged.connect(self._filter_library_table)

        self.btn_add_custom = QPushButton(" Add Custom Source")
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
                action_layout.addStretch()  # 内置源占位，保持排版对其

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
