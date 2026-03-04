from PySide6.QtWidgets import (QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
                               QHeaderView, QAbstractItemView, QComboBox, QPushButton, QPlainTextEdit)
from PySide6.QtCore import Qt, Signal
from src.core.theme_manager import ThemeManager


class ScrollInterceptTableWidget(QTableWidget):
    def wheelEvent(self, event):
        super().wheelEvent(event)
        event.accept()


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
        self.table.setMinimumHeight(160)

        self.layout.addWidget(self.table)

        # Connect cell changes to signal
        self.table.itemChanged.connect(lambda _: self.sig_data_changed.emit())

        # Wire up dynamic theming
        self._apply_theme()
        ThemeManager().theme_changed.connect(self._apply_theme)

    def _apply_theme(self):
        tm = ThemeManager()
        self.table.setStyleSheet(f"""
            QTableWidget {{ 
                background-color: {tm.color('bg_input')}; 
                color: {tm.color('text_main')}; 
                border: 1px solid {tm.color('border')}; 
            }}
            QHeaderView::section {{ 
                background-color: {tm.color('bg_card')}; 
                color: {tm.color('text_muted')}; 
                border: 1px solid {tm.color('border')}; 
                padding: 4px; 
            }}
            QComboBox {{ 
                background: transparent; 
                color: {tm.color('text_main')}; 
                border: none; 
            }}
            QPlainTextEdit {{ 
                background: {tm.color('bg_main')}; 
                color: {tm.color('text_main')}; 
                border: 1px solid {tm.color('border')}; 
                border-radius: 2px; 
            }}
            QPushButton {{ 
                background: transparent; 
                color: {tm.color('danger')}; 
                border: none; 
                font-weight: bold;
            }}
        """)

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
        combo_type.currentTextChanged.connect(lambda _: self.sig_data_changed.emit())
        self.table.setCellWidget(row, 1, combo_type)

        # Value
        val_edit = QPlainTextEdit(str(val))
        val_edit.setPlaceholderText("Enter value or valid JSON...")
        val_edit.textChanged.connect(lambda: self.sig_data_changed.emit())
        self.table.setCellWidget(row, 2, val_edit)

        self.table.setRowHeight(row, 60)

        # Delete Button
        btn_del = QPushButton()
        btn_del.setIcon(ThemeManager().icon("delete", "danger"))  # Assuming 'delete.svg' exists, otherwise use 'close'
        btn_del.setCursor(Qt.PointingHandCursor)
        btn_del.setToolTip("Delete Parameter")
        btn_del.clicked.connect(lambda *args, r=row: self._remove_row(btn_del))
        self.table.setCellWidget(row, 3, btn_del)

    def _remove_row(self, button):
        for row in range(self.table.rowCount()):
            if self.table.cellWidget(row, 3) == button:
                self.table.removeRow(row)
                self.sig_data_changed.emit()
                break

    def extract_data(self):
        self.table.viewport().clearFocus()
        self.table.setCurrentItem(None)

        data = []
        for r in range(self.table.rowCount()):
            name_item = self.table.item(r, 0)
            val_widget = self.table.cellWidget(r, 2)
            type_combo = self.table.cellWidget(r, 1)

            name = name_item.text().strip() if name_item else ""
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