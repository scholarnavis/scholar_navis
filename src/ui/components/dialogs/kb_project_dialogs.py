"""Knowledge base file selection and project (library) editor dialogs.

拆分自 src/ui/components/dialog.py。
"""

import os

from PySide6.QtCore import Qt, QRegularExpression
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (QAbstractItemView, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QTextEdit, QWidget)

from src.core.models_registry import EMBEDDING_MODELS
from src.ui.components.combo import BaseComboBox
from src.ui.components.dialogs.base import BaseDialog


class SelectKBFileDialog(BaseDialog):
    def __init__(self, parent=None, files=None):
        super().__init__(parent, title="Select Files from Knowledge Base", width=580)
        self.setMinimumHeight(500)
        self._all_files = files or []

        # --- KB 选择栏 ---
        kb_layout = QHBoxLayout()
        lbl_kb = QLabel("Knowledge Base:")
        kb_layout.addWidget(lbl_kb)

        self.combo_kb = BaseComboBox()

        kb_layout.addWidget(self.combo_kb, stretch=1)
        self.content_layout.addLayout(kb_layout)

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

        # --- 底部状态提示 ---
        self.lbl_status = QLabel()
        self.footer_layout.insertWidget(0, self.lbl_status)

        self.add_button("Cancel", self.reject)
        self.btn_attach = self.add_button("Attach", self.accept, is_primary=True)

        self.combo_kb.currentIndexChanged.connect(self._on_kb_changed)

        # 初始化数据
        self._load_kbs()
        self._update_status()
        self._apply_theme()

    def _load_kbs(self):
        from src.core.kb_manager import KBManager
        self.kb_manager = KBManager()
        kbs = self.kb_manager.get_all_kbs()

        self.combo_kb.blockSignals(True)
        self.combo_kb.clear()
        self.combo_kb.addItem("Please select a Knowledge Base...", "none")

        for kb in kbs:
            if kb.get('status') == 'ready':
                self.combo_kb.addItem(kb['name'], kb)

        self.combo_kb.blockSignals(False)

    def _on_kb_changed(self):
        kb_data = self.combo_kb.currentData()
        kb_id = kb_data.get("id") if isinstance(kb_data, dict) else kb_data

        if not kb_id or kb_id == "none":
            self._all_files = []
        else:
            self._all_files = self.kb_manager.get_kb_files(kb_id)

        self.inp_search.clear()
        self._populate_list(self._all_files)
        self._update_status()

    def _populate_list(self, files):
        self.list_widget.clear()
        for f in files:
            item = QListWidgetItem(f"  {f['name']}")
            item.setData(Qt.UserRole, f)
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
            self.btn_attach.setEnabled(True)
        else:
            self.lbl_status.setText(f"{total} file(s) available")
            self.btn_attach.setEnabled(False)

    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm
        self.lbl_status.setStyleSheet(
            f"color: {tm.color('text_muted')}; font-size: 12px; font-weight: bold;")
        if hasattr(self, 'combo_kb') and hasattr(self.combo_kb, 'setStyleSheet'):
            self.combo_kb.setStyleSheet(
                f"QComboBox {{ border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 4px; background: {tm.color('bg_input')}; color: {tm.color('text_main')}; }}")

    def get_selected_file_infos(self):
        infos = []
        for item in self.list_widget.selectedItems():
            data = item.data(Qt.UserRole)
            if isinstance(data, dict):
                infos.append(data)
            else:
                infos.append({"path": data, "name": os.path.basename(data)})
        return infos

    def get_selected_paths(self):
        return [item['path'] for item in self.get_selected_file_infos()]


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

        self.combo_model = BaseComboBox()
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
