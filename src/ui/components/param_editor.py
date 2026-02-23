from PySide6.QtWidgets import (QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
                               QHeaderView, QAbstractItemView, QComboBox, QPushButton, QPlainTextEdit)
from PySide6.QtCore import Qt, Signal


class ParamEditorWidget(QWidget):
    sig_data_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Parameter Name", "Type", "Value", "Action"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)

        # 增加表格整体高度，以适应多行文本框
        self.table.setMinimumHeight(160)
        self.table.setStyleSheet("QTableWidget { background-color: #2b2b2b; color: white; border: 1px solid #444; }")

        self.layout.addWidget(self.table)

        # Connect cell changes to signal
        self.table.itemChanged.connect(lambda _: self.sig_data_changed.emit())

    def add_param_row(self, name="", ptype="str", val=""):
        row = self.table.rowCount()
        self.table.insertRow(row)

        # Name
        item_name = QTableWidgetItem(name)
        self.table.setItem(row, 0, item_name)

        # Type
        combo_type = QComboBox()
        combo_type.addItems(["str", "int", "float", "bool", "json"])
        combo_type.setCurrentText(ptype)
        combo_type.setStyleSheet("background: #333; color: white; border: none;")
        combo_type.currentTextChanged.connect(lambda _: self.sig_data_changed.emit())
        self.table.setCellWidget(row, 1, combo_type)

        # Value
        val_edit = QPlainTextEdit(str(val))
        val_edit.setPlaceholderText("Enter value or valid JSON...")
        val_edit.setStyleSheet("background: #222; color: #eee; border: 1px solid #444; border-radius: 2px;")
        val_edit.textChanged.connect(lambda: self.sig_data_changed.emit())
        self.table.setCellWidget(row, 2, val_edit)

        self.table.setRowHeight(row, 60)

        # Delete Button
        btn_del = QPushButton("❌")
        btn_del.setCursor(Qt.PointingHandCursor)
        btn_del.setStyleSheet("background: transparent; color: #ff6b6b; border: none;")
        btn_del.clicked.connect(lambda *args, r=row: self._remove_row(btn_del))
        self.table.setCellWidget(row, 3, btn_del)

    def _remove_row(self, button):
        index = self.table.indexAt(button.pos())
        if index.isValid():
            self.table.removeRow(index.row())
            self.sig_data_changed.emit()

    def extract_data(self):
        """Returns a list of dictionaries containing valid parameters."""

        self.table.viewport().clearFocus()
        self.table.setCurrentItem(None)

        data = []
        for r in range(self.table.rowCount()):
            name_item = self.table.item(r, 0)
            val_widget = self.table.cellWidget(r, 2)
            type_combo = self.table.cellWidget(r, 1)

            name = name_item.text().strip() if name_item else ""

            # 从 QPlainTextEdit 提取文本，而不是 QTableWidgetItem
            val = val_widget.toPlainText().strip() if val_widget else ""

            if not name or not val: continue

            data.append({
                "name": name,
                "type": type_combo.currentText(),
                "value": val
            })
        return data

    def load_data(self, param_list, append=False):
        """Populates the table from a list of dictionaries."""
        if not append:
            self.table.setRowCount(0)

        for p in param_list:
            val = p.get("value", "")
            if p.get("type") == "json" and isinstance(val, dict):
                import json
                val = json.dumps(val, ensure_ascii=False, indent=2)

            self.add_param_row(p.get("name", ""), p.get("type", "str"), val)