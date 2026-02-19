import os
import shutil
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
                               QPushButton, QHBoxLayout, QHeaderView, QGroupBox,
                               QLabel, QAbstractItemView)
from PySide6.QtCore import QFileSystemWatcher, Qt, QUrl, QThread, QObject, Signal
from PySide6.QtGui import QDesktopServices

from src.core.models_registry import get_model_conf
from src.core.signals import GlobalSignals
from src.tools.base_tool import BaseTool
from src.core.kb_manager import KBManager
from src.core.database import DatabaseManager
from src.core.config_manager import ConfigManager
from src.core.pdf_engine import PDFEngine
from src.ui.components.toast import ToastManager
from src.ui.components.combo import BaseComboBox  # 使用新组件
from src.ui.components.dialog import ProgressDialog, StandardDialog


class StagingSwitchWorker(QObject):
    sig_finished = Signal(bool, str)

    def __init__(self, kb_id):
        super().__init__()
        self.kb_id = kb_id

    def run(self):
        try:
            success = DatabaseManager().switch_kb(self.kb_id)
            self.sig_finished.emit(success, "Loaded" if success else "Failed to load database.")
        except Exception as e:
            self.sig_finished.emit(False, str(e))


class StagingImportWorker(QObject):
    sig_progress = Signal(int, str)
    sig_finished = Signal(str)

    def __init__(self, kb_id, file_paths):
        super().__init__()
        self.kb_id = kb_id
        self.file_paths = file_paths
        self.is_running = True
        self.kb_manager = KBManager()
        self.pdf_engine = PDFEngine()
        self.archive_dir = os.path.join(os.getcwd(), "archived_pdfs")
        if not os.path.exists(self.archive_dir): os.makedirs(self.archive_dir)

    def stop(self):
        self.is_running = False

    def run(self):
        processed = 0
        total = len(self.file_paths)
        try:
            for idx, path in enumerate(self.file_paths):
                if not self.is_running:
                    self.sig_finished.emit("Import Cancelled.")
                    return
                filename = os.path.basename(path)
                self.sig_progress.emit(int((idx / total) * 100), f"Processing {idx + 1}/{total}: {filename}")
                new_path = self.kb_manager.import_file_to_kb(self.kb_id, path)
                if not new_path: continue

                def engine_cb(pct, msg):
                    if not self.is_running: raise InterruptedError()

                try:
                    self.pdf_engine.process_pdf(new_path, progress_callback=engine_cb)
                    archive_path = os.path.join(self.archive_dir, filename)
                    if os.path.exists(archive_path):
                        base, ext = os.path.splitext(filename)
                        archive_path = os.path.join(self.archive_dir, f"{base}_archived{ext}")
                    shutil.move(path, archive_path)
                    processed += 1
                except InterruptedError:
                    self.sig_finished.emit("Import Cancelled.")
                    return
                except Exception as e:
                    print(f"Error importing {filename}: {e}")
            self.sig_finished.emit(f"Successfully imported {processed}/{total} files.")
        except Exception as e:
            self.sig_finished.emit(f"Error: {str(e)}")


class StagingTool(BaseTool):
    def __init__(self):
        super().__init__("📥 Staging Area")
        self.kb_manager = KBManager()
        self.widget = None
        self.current_kb_id = None
        self.watch_dir = os.path.join(os.getcwd(), "downloads_staging")
        if not os.path.exists(self.watch_dir): os.makedirs(self.watch_dir)
        self.watcher = QFileSystemWatcher()
        self.watcher.addPath(self.watch_dir)
        self.watcher.directoryChanged.connect(self.reload_files)
        GlobalSignals().kb_list_changed.connect(self.refresh_kb_list)

    def get_ui_widget(self) -> QWidget:
        if self.widget: return self.widget

        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)
        layout.setSpacing(15)

        target_group = QGroupBox("Target Library")
        target_group.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #444; margin-top: 10px; padding-top: 15px; }")
        target_layout = QHBoxLayout(target_group)

        # 传参控制高度，不使用 ugly CSS
        self.combo_kb = BaseComboBox(min_height=40)

        self.combo_kb.currentIndexChanged.connect(self.on_kb_combo_changed)
        target_layout.addWidget(QLabel("Import to:"))
        target_layout.addWidget(self.combo_kb, stretch=1)
        layout.addWidget(target_group)

        info_box = QGroupBox("How to use")
        info_layout = QVBoxLayout(info_box)
        lbl_info = QLabel(
            f"<html>1. Save PDFs to: <span style='color:#05B8CC; font-family:Consolas;'>{self.watch_dir}</span><br>"
            f"2. Select files below and click 'Import Selected'.</html>"
        )
        lbl_info.setTextFormat(Qt.RichText)
        btn_open = QPushButton("Open Folder")
        btn_open.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.watch_dir)))
        info_layout.addWidget(lbl_info)
        info_layout.addWidget(btn_open)
        layout.addWidget(info_box)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Filename", "Size", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setStyleSheet("QTableWidget { background-color: #1e1e1e; border: 1px solid #333; }")
        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        self.btn_import = QPushButton("Import Selected")
        self.btn_import.setStyleSheet("background-color: #007acc; color: white; font-weight: bold; padding: 10px;")
        self.btn_import.setEnabled(False)
        self.btn_import.clicked.connect(self.import_selected)
        self.btn_del = QPushButton("🗑️ Delete")
        self.btn_del.clicked.connect(self.delete_selected)
        btn_layout.addWidget(self.btn_import)
        btn_layout.addWidget(self.btn_del)
        layout.addLayout(btn_layout)

        self.reload_files()
        self.refresh_kb_list()
        return self.widget

    def refresh_kb_list(self):
        self.combo_kb.blockSignals(True)
        self.combo_kb.clear()
        kbs = self.kb_manager.get_all_kbs()

        for kb in kbs:
            m = get_model_conf(kb.get('model_id'), model_type="embedding")

            m_name = m['ui_name'] if m else kb.get('model_id')
            self.combo_kb.addItem(f"{kb['name']} [{m_name}]", kb)

        self.combo_kb.setCurrentIndex(-1)
        self.combo_kb.setPlaceholderText("Select Library...")
        self.combo_kb.blockSignals(False)

    def on_kb_combo_changed(self, index):
        if index < 0: return
        data = self.combo_kb.itemData(index)
        if self.current_kb_id == data['id']: return

        self.pd = ProgressDialog(self.widget, "Activating", f"Loading {data['name']}...")
        self.pd.show()
        self.sw_thread = QThread()
        self.sw_worker = StagingSwitchWorker(data['id'])
        self.sw_worker.moveToThread(self.sw_thread)
        self.sw_thread.started.connect(self.sw_worker.run)
        self.sw_worker.sig_finished.connect(self.handle_switch_done)
        self.sw_worker.sig_finished.connect(self.sw_thread.quit)
        self.sw_thread.finished.connect(self.sw_thread.deleteLater)
        self._pending_id = data['id']
        self.sw_thread.start()

    def handle_switch_done(self, success, msg):
        if self.pd: self.pd.close_safe(); self.pd = None
        if success:
            self.current_kb_id = self._pending_id
            self.btn_import.setEnabled(True)
            GlobalSignals().kb_switched.emit(self.current_kb_id)
        else:
            self.combo_kb.setCurrentIndex(-1)
            StandardDialog(self.widget, "Error", msg).exec()

    def import_selected(self):
        rows = sorted(set(i.row() for i in self.table.selectedIndexes()))
        if not rows: return
        paths = []
        for r in rows:
            p = self.table.item(r, 0).data(Qt.UserRole)
            if os.path.exists(p): paths.append(p)
        self.pd = ProgressDialog(self.widget, "Importing", "Starting batch import...")
        self.pd.show()
        self.imp_thread = QThread()
        self.imp_worker = StagingImportWorker(self.current_kb_id, paths)
        self.imp_worker.moveToThread(self.imp_thread)
        self.pd.sig_canceled.connect(self.imp_worker.stop)
        self.imp_thread.started.connect(self.imp_worker.run)
        self.imp_worker.sig_progress.connect(self.pd.update_progress)
        self.imp_worker.sig_finished.connect(self.handle_import_done)
        self.imp_worker.sig_finished.connect(self.imp_thread.quit)
        self.imp_thread.finished.connect(self.imp_thread.deleteLater)
        self.imp_thread.finished.connect(self.imp_worker.deleteLater)
        self.imp_thread.start()

    def handle_import_done(self, result):
        if self.pd: self.pd.close_safe(); self.pd = None
        ToastManager().show(result, "info")
        self.reload_files()
        GlobalSignals().kb_list_changed.emit()

    def reload_files(self):
        self.table.setRowCount(0)
        if not os.path.exists(self.watch_dir): return
        files = [f for f in os.listdir(self.watch_dir) if f.endswith('.pdf')]
        for f in files:
            path = os.path.join(self.watch_dir, f)
            row = self.table.rowCount()
            self.table.insertRow(row)
            item = QTableWidgetItem(f)
            item.setData(Qt.UserRole, path)
            self.table.setItem(row, 0, item)
            size = os.path.getsize(path) / 1024 / 1024
            self.table.setItem(row, 1, QTableWidgetItem(f"{size:.2f} MB"))
            self.table.setItem(row, 2, QTableWidgetItem("Ready"))

    def delete_selected(self):
        rows = sorted(set(i.row() for i in self.table.selectedIndexes()))
        for r in rows:
            path = self.table.item(r, 0).data(Qt.UserRole)
            try:
                os.remove(path)
            except:
                pass
        self.reload_files()