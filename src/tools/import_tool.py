import hashlib
import logging
import os
import shutil
import tempfile


from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                               QLabel, QFileDialog, QGroupBox, QTableWidget,
                               QHeaderView, QAbstractItemView, QMenu, QLineEdit, QTableWidgetItem, QApplication)
from PySide6.QtGui import QAction, QCursor, QColor, QIcon
from PySide6.QtCore import Qt
from src.core.core_task import TaskState, TaskManager
from src.core.models_registry import get_model_conf, check_model_exists
from src.core.theme_manager import ThemeManager
from src.tools.base_tool import BaseTool
from src.core.kb_manager import KBManager
from src.core.signals import GlobalSignals
from src.services.file_service import FileService
from src.task.kb_tasks import ImportFilesTask, DeleteFilesTask, SwitchKBTask, RenameFilesTask
from src.ui.components.combo import BaseComboBox
from src.ui.components.dialog import ProjectEditorDialog, ProgressDialog, StandardDialog, BaseDialog


class ImportTool(BaseTool):
    def __init__(self):
        super().__init__("Library Manager")
        self.widget = None
        self.kb_manager = KBManager()
        self.task_mgr = TaskManager()
        self.staged_add = []
        self.staged_del = []
        self.staged_rename = {}
        self.staged_meta = None
        self.rebuild_required = False

        self.current_kb_id = None
        self.pd = None

        GlobalSignals().kb_list_changed.connect(self.refresh_kb_list)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

    def get_ui_widget(self) -> QWidget:
        if hasattr(self, 'widget') and self.widget: return self.widget
        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)



        # 2项目管理
        kb_group = QGroupBox("Project / Library Management")
        kb_layout = QHBoxLayout(kb_group)
        self.combo_kb = BaseComboBox(min_height=55)
        self.combo_kb.currentIndexChanged.connect(self.on_kb_switched)

        btn_col = QVBoxLayout()
        row1 = QHBoxLayout()
        self.btn_new = QPushButton(" New")
        self.btn_new.clicked.connect(self.create_new_kb)
        self.btn_snp = QPushButton(" Import .snp")
        self.btn_snp.clicked.connect(self.import_external_kb)
        row1.addWidget(self.btn_new)
        row1.addWidget(self.btn_snp)

        row2 = QHBoxLayout()
        self.btn_edit = QPushButton(" Edit")
        self.btn_edit.clicked.connect(self.edit_current_kb)
        self.btn_del_kb = QPushButton(" Delete")
        self.btn_del_kb.clicked.connect(self.delete_current_kb)
        row2.addWidget(self.btn_edit)
        row2.addWidget(self.btn_del_kb)
        btn_col.addLayout(row1)
        btn_col.addLayout(row2)

        kb_layout.addWidget(self.combo_kb, stretch=7)
        kb_layout.addLayout(btn_col, stretch=3)
        layout.addWidget(kb_group)

        # 3. 详情与操作
        action_bar = QHBoxLayout()
        self.lbl_kb_info = QLabel("Select a library...")

        ctrl_col = QVBoxLayout()
        self.btn_add_files = QPushButton(" Add Files")
        self.btn_add_files.clicked.connect(self.select_files)
        self.btn_export = QPushButton(" Export Project")
        self.btn_export.clicked.connect(self.export_current_kb)
        ctrl_col.addWidget(self.btn_add_files)
        ctrl_col.addWidget(self.btn_export)

        action_bar.addWidget(self.lbl_kb_info, stretch=1)
        action_bar.addLayout(ctrl_col)
        layout.addLayout(action_bar)

        # 4. 文件列表
        self.file_table = QTableWidget(0, 3)
        self.file_table.cellDoubleClicked.connect(self._on_table_double_click)
        self.file_table.setHorizontalHeaderLabels(["Filename", "Size", "Status"])
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.file_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.file_table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.file_table.verticalHeader().setDefaultSectionSize(36)
        self.file_table.setTextElideMode(Qt.ElideMiddle)
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.file_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.file_table.customContextMenuRequested.connect(self.show_context_menu)

        layout.addWidget(self.file_table)

        # 5. 底部保存区
        save_group = QGroupBox("Changes Staging")
        save_layout = QVBoxLayout(save_group)
        self.lbl_staged_status = QLabel("Ready.")
        self.btn_save = QPushButton(" Save & Apply All Changes")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self.commit_changes)
        save_layout.addWidget(self.lbl_staged_status)
        save_layout.addWidget(self.btn_save)
        layout.addWidget(save_group)

        self._toggle_kb_actions(False)
        self.refresh_kb_list()

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

        return self.widget

    def _apply_theme(self):
        if not hasattr(self, 'widget') or not self.widget:
            return

        tm = ThemeManager()
        bg_main = tm.color('bg_main')
        bg_card = tm.color('bg_card')
        border = tm.color('border')
        text_main = tm.color('text_main')
        text_muted = tm.color('text_muted')
        accent = tm.color('accent')
        accent_hover = tm.color('accent_hover')
        danger = tm.color('danger')
        success = tm.color('success')
        btn_bg = tm.color('btn_bg')
        btn_hover = tm.color('btn_hover')

        self.widget.setStyleSheet(f"""
            QWidget {{ background-color: {bg_main}; color: {text_main}; border: none; }}
            QGroupBox {{ border: 1px solid {border}; border-radius: 6px; margin-top: 12px; padding-top: 25px; background-color: {bg_card}; }}
            QGroupBox::title {{ subcontrol-origin: margin; left: 10px; color: {text_muted}; font-weight: bold; }}
            QPushButton {{ background-color: {btn_bg}; border: 1px solid {border}; border-radius: 4px; padding: 6px 12px; color: {text_main}; }}
            QPushButton:hover {{ background-color: {btn_hover}; }}
            QPushButton:disabled {{ color: {text_muted}; background-color: {bg_main}; border: 1px dashed {border}; }}
        """)

        self.file_table.setStyleSheet(f"""
            QTableWidget {{ background-color: {bg_main}; border: 1px solid {border}; gridline-color: {bg_card}; color: {text_main}; }}
            QHeaderView::section {{ background-color: {bg_card}; color: {text_muted}; border: none; padding: 4px; border-right: 1px solid {border}; border-bottom: 1px solid {border}; }}
            QTableWidget::item:selected {{ background-color: {btn_hover}; color: {text_main}; }}
        """)

        if hasattr(self, 'lbl_kb_info'): self.lbl_kb_info.setStyleSheet(f"color: {text_muted}; font-size: 12px;")
        if hasattr(self, 'lbl_staged_status'):
            self.lbl_staged_status.setStyleSheet(f"color: {text_muted}; border: 1px dashed {border}; padding: 10px;")

        # --- Functional Button Colors & Icons ---
        # 1. Neutral / Standard Actions
        if hasattr(self, 'btn_snp'): self.btn_snp.setIcon(tm.icon("archive", "text_main"))
        if hasattr(self, 'btn_edit'): self.btn_edit.setIcon(tm.icon("edit", "text_main"))
        if hasattr(self, 'btn_export'): self.btn_export.setIcon(tm.icon("download", "text_main"))

        # 2. Positive / Creation Actions (Accent / Success)
        if hasattr(self, 'btn_new'):
            self.btn_new.setIcon(tm.icon("add", "accent"))
            self.btn_new.setStyleSheet(
                f"QPushButton {{ background-color: transparent; border: 1px solid {accent}; color: {accent}; border-radius: 4px; padding: 6px 12px; }} QPushButton:hover {{ background-color: {accent}; color: {bg_main}; }}")

        if hasattr(self, 'btn_add_files'):
            self.btn_add_files.setIcon(tm.icon("attach", "success"))
            self.btn_add_files.setStyleSheet(
                f"QPushButton {{ background-color: transparent; border: 1px solid {success}; color: {success}; border-radius: 4px; padding: 6px 12px; }} QPushButton:hover {{ background-color: {success}; color: {bg_main}; }}")

        # 3. Destructive Actions (Danger)
        if hasattr(self, 'btn_del_kb'):
            self.btn_del_kb.setIcon(tm.icon("delete", "danger"))
            self.btn_del_kb.setStyleSheet(
                f"QPushButton {{ background-color: transparent; border: 1px solid {danger}; color: {danger}; border-radius: 4px; padding: 6px 12px; }} QPushButton:hover {{ background-color: {danger}; color: {bg_main}; }}")

        # 4. Primary Save Action
        if hasattr(self, 'btn_save'):
            self.btn_save.setIcon(tm.icon("save", "bg_main"))
            self.btn_save.setStyleSheet(
                f"QPushButton:enabled {{ background-color: {accent}; font-weight: bold; color: {bg_main}; height: 35px; border: none; }} QPushButton:hover:enabled {{ background-color: {accent_hover}; }}")

        self.update_file_list()

    def _render_table(self):
        self.file_table.setRowCount(0)
        if not self.current_kb_id: return

        tm = ThemeManager()
        success_color = QColor(tm.color('success'))
        danger_color = QColor(tm.color('danger'))
        warning_color = QColor(tm.color('warning'))
        text_color = QColor(tm.color('text_main'))

        kb_data = self.kb_manager.get_kb_by_id(self.current_kb_id)
        if not kb_data: return

        existing_files = list(kb_data.get('file_map', {}).values())

        # Include Icon objects
        display_items = []
        for f in existing_files:
            if f in self.staged_del:
                display_items.append((f, "Deleted", danger_color, tm.icon("delete", "danger")))
            elif f in self.staged_rename:
                display_items.append((self.staged_rename[f], f"Renamed (from {f})", warning_color, tm.icon("edit-2", "warning")))
            else:
                display_items.append((f, "Synced", text_color, tm.icon("check-circle", "success")))

        for f in self.staged_add:
            display_items.append((os.path.basename(f), "Added", success_color, tm.icon("plus-circle", "success")))

        self.file_table.setRowCount(len(display_items))
        for row, (name, status, color, icon) in enumerate(display_items):
            item_name = QTableWidgetItem(name)
            item_status = QTableWidgetItem(icon, status)
            item_size = QTableWidgetItem("--")

            item_name.setForeground(color)
            item_status.setForeground(color)
            item_size.setForeground(color)

            self.file_table.setItem(row, 0, item_name)
            self.file_table.setItem(row, 1, item_status)
            self.file_table.setItem(row, 2, item_size)



    def _toggle_kb_actions(self, enabled: bool, status: str = "ready"):
        if not enabled:
            self.btn_edit.setEnabled(False)
            self.btn_del_kb.setEnabled(False)
            self.btn_add_files.setEnabled(False)
            self.btn_export.setEnabled(False)
            self.file_table.setEnabled(False)
            return

        # 如果选中了，且状态正常 (ready)
        if status == "ready":
            self.btn_edit.setEnabled(True)
            self.btn_del_kb.setEnabled(True)
            self.btn_add_files.setEnabled(True)
            self.btn_export.setEnabled(True)
            self.file_table.setEnabled(True)  # 允许操作文件表格
        else:
            # 状态异常 (corrupted 或中断残留的 building)
            # 开启焦土政策：只允许编辑(重建)或删除，其他全部锁死
            self.btn_edit.setEnabled(True)
            self.btn_del_kb.setEnabled(True)
            self.btn_add_files.setEnabled(False)
            self.btn_export.setEnabled(False)
            self.file_table.setEnabled(False)    # 锁死表格，禁止右键和双击重命名

    def _on_table_double_click(self, row, col):
        if not self.current_kb_id: return

        if col == 0:
            status_item = self.file_table.item(row, 2)
            if status_item and "Indexed" in status_item.text():
                self._handle_open([row])

    def _adjust_table_height(self):
        self.file_table.setMinimumHeight(150)
        self.file_table.setMaximumHeight(16777215)

    def refresh_kb_list(self):
        self.combo_kb.blockSignals(True)
        self.combo_kb.clear()
        self.combo_kb.setPlaceholderText("Select a library...")

        kbs = self.kb_manager.get_all_kbs()
        from src.core.models_registry import get_model_conf, check_model_exists
        target_idx = -1
        tm = ThemeManager()

        for i, kb in enumerate(kbs):
            m = get_model_conf(kb.get('model_id'), "embedding")

            if m:
                is_downloaded = check_model_exists(m.get('hf_repo_id'))
                status_marker = "" if is_downloaded else " (Not Downloaded)"
                m_ui = f"{m['ui_name']}{status_marker}"
            else:
                m_ui = f"{kb.get('model_id', '?')} (Unknown/External)"

            status = kb.get('status', 'ready')

            icon = QIcon()
            display_text = f"{kb['name']}   [Model: {m_ui} | Docs: {kb.get('doc_count', 0)}]"

            if status == "corrupted":
                icon = tm.icon("alert", "danger")
                display_text = f"[CORRUPTED] {display_text}"
            elif status == "building":
                icon = tm.icon("loader", "warning")
                display_text = f"[BUILDING] {display_text}"
            else:
                icon = tm.icon("database", "accent")

            self.combo_kb.addItem(icon, display_text, kb)

            if kb['id'] == getattr(self, 'current_kb_id', None):
                target_idx = i

        if target_idx >= 0:
            self.combo_kb.setCurrentIndex(target_idx)
            self.on_kb_switched(target_idx)
        else:
            self.combo_kb.setCurrentIndex(-1)
            self.on_kb_switched(-1)

        self.combo_kb.blockSignals(False)

    def update_file_list(self):
        try:
            if not getattr(self, 'current_kb_id', None):
                self.file_table.setRowCount(0)
                if hasattr(self, 'lbl_kb_info'):
                    self.lbl_kb_info.setText("Select a library...")
                if hasattr(self, '_adjust_table_height'):
                    self._adjust_table_height()
                return

            self.file_table.setRowCount(0)
            files = self.kb_manager.get_kb_files(self.current_kb_id)

            tm = ThemeManager()
            success_color = QColor(tm.color('success'))
            warning_color = QColor(tm.color('warning'))
            danger_color = QColor(tm.color('danger'))
            text_color = QColor(tm.color('text_main'))

            # 1. 渲染已经存在于知识库中的文件
            for f in files:
                name = f['name']
                if name in self.staged_del: continue
                row = self.file_table.rowCount()
                self.file_table.insertRow(row)
                display_name = self.staged_rename.get(name, name)

                item_name = QTableWidgetItem(display_name)
                item_size = QTableWidgetItem(str(f.get('size', '-')))

                # Replace Emoji with Icon
                if name in self.staged_rename:
                    status_text = "Renaming..."
                    color = warning_color
                    icon = tm.icon("edit-2", "warning")
                else:
                    status_text = "Indexed"
                    color = text_color
                    icon = tm.icon("check-circle", "success")

                item_name.setForeground(color)
                item_size.setForeground(color)

                item_status = QTableWidgetItem(icon, status_text)
                item_status.setForeground(color)

                self.file_table.setItem(row, 0, item_name)
                self.file_table.setItem(row, 1, item_size)
                self.file_table.setItem(row, 2, item_status)

            # 2. 渲染即将导入（Staged Add）的文件
            for f_path in self.staged_add:
                row = self.file_table.rowCount()
                self.file_table.insertRow(row)

                item_name = QTableWidgetItem(os.path.basename(f_path))
                item_size = QTableWidgetItem("-")

                if f_path.lower().endswith('.doc'):
                    item_status = QTableWidgetItem(tm.icon("alert", "danger"), "Unsupported (.docx required)")
                    item_name.setForeground(danger_color)
                    item_size.setForeground(danger_color)
                    item_status.setForeground(danger_color)
                else:
                    item_status = QTableWidgetItem(tm.icon("clock", "warning"), "Pending Save")
                    item_name.setForeground(success_color)
                    item_size.setForeground(success_color)
                    item_status.setForeground(success_color)

                self.file_table.setItem(row, 0, item_name)
                self.file_table.setItem(row, 1, item_size)
                self.file_table.setItem(row, 2, item_status)

            self._update_details_html()

            if hasattr(self, '_adjust_table_height'):
                self._adjust_table_height()

        except Exception as e:
            import traceback
            self.logger.error(f"GUI Error in update_file_list: {e}\n{traceback.format_exc()}")

    def _update_details_html(self):
        try:
            data = self.combo_kb.currentData()
            if not data: return

            display_data = data.copy()
            if getattr(self, 'staged_meta', None):
                display_data.update(self.staged_meta)

            m_conf = get_model_conf(display_data.get('model_id'), "embedding")

            if m_conf:
                is_downloaded = check_model_exists(m_conf.get('hf_repo_id'))
                dl_tag = "" if is_downloaded else " <span style='color:#ffb86c; font-weight:bold;'>(Not Downloaded)</span>"
                m_ui = f"{m_conf['ui_name']}{dl_tag}"
            else:
                m_ui = f"{display_data.get('model_id', 'Unknown')} <span style='color:#ff6b6b; font-weight:bold;'>(Unknown/External)</span>"

            status = display_data.get('status', 'ready')
            status_color = "#ff6b6b" if status == "corrupted" else ("#f1c40f" if status == "building" else "#05B8CC")

            info = (
                f"<b>Project:</b> {display_data.get('name', 'Unknown')}<br>"
                f"<b>Domain:</b> <span style='color:#05B8CC'>{display_data.get('domain', 'Gen')}</span><br>"
                f"<b>Status:</b> <span style='color:{status_color}; font-weight:bold;'>{status.upper()}</span><br>"
                f"<b>Model:</b> {m_ui}<br>"
                f"<b>Storage:</b> {display_data.get('doc_count', 0)} files ({display_data.get('size_mb', 0)} MB)"
            )

            if hasattr(self, 'lbl_kb_info'):
                self.lbl_kb_info.setText(info)

        except Exception as e:
            import traceback
            self.logger.error(f"GUI Error in _update_details_html: {e}\n{traceback.format_exc()}")

    def show_context_menu(self, pos):
        indexes = self.file_table.selectedIndexes()
        if not indexes: return
        rows = sorted(set(idx.row() for idx in indexes))

        tm = ThemeManager()
        menu = QMenu()
        menu.setStyleSheet(f"""
            QMenu {{ background-color: {tm.color('bg_card')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 4px; }} 
            QMenu::item {{ padding: 6px 25px 6px 20px; border-radius: 2px; }}
            QMenu::item:selected {{ background-color: {tm.color('btn_hover')}; }}
            QMenu::separator {{ height: 1px; background: {tm.color('border')}; margin: 4px 0px; }}
        """)

        # 挂载 SVG 图标
        act_open = QAction(tm.icon("link", "text_main"), "Open Source File", self.widget)
        act_rename = QAction(tm.icon("edit", "text_main"), "Rename (Stage)", self.widget)
        act_del = QAction(tm.icon("delete", "danger"), f"Delete {len(rows)} items (Stage)", self.widget)

        act_open.triggered.connect(lambda: self._handle_open(rows))
        act_rename.triggered.connect(lambda: self._stage_rename_dialog(rows[0]))
        act_del.triggered.connect(lambda: self.batch_delete(rows))

        menu.addAction(act_open)
        menu.addAction(act_rename)
        menu.addSeparator()
        menu.addAction(act_del)
        menu.exec(QCursor.pos())


    def _stage_rename_dialog(self, row):
        """重命名暂存逻辑：自动补充后缀"""
        # 获取当前显示的名称（可能是已经暂存过一次的名字）
        old_display_name = self.file_table.item(row, 0).text()


        original_name = None
        for k, v in self.staged_rename.items():
            if v == old_display_name:
                original_name = k
                break
        if not original_name:
            original_name = old_display_name

        base_name, ext = os.path.splitext(original_name)

        # 使用你提供的 BaseDialog
        dlg = BaseDialog(self.widget, title="Rename File", width=400)
        inp = QLineEdit(os.path.splitext(old_display_name)[0])
        tm = ThemeManager()
        inp.setStyleSheet(
            f"background-color: {tm.color('bg_input')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; padding: 5px; border-radius: 4px;")

        dlg.content_layout.addWidget(QLabel(f"New name (Extension '{ext}' will be auto-added):"))
        dlg.content_layout.addWidget(inp)
        dlg.add_button("Cancel", dlg.reject)
        dlg.add_button("Confirm", dlg.accept, is_primary=True)

        if dlg.exec():
            new_name = inp.text().strip()
            if new_name:
                # 自动补充后缀逻辑
                if not new_name.lower().endswith(ext.lower()):
                    new_name += ext

                if new_name != old_display_name:
                    self.staged_rename[original_name] = new_name
                    # 立马刷新界面显示和暂存状态
                    self.update_file_list()
                    self.mark_dirty()

    def mark_dirty(self):
        """实时刷新暂存信息状态，并联动按钮可见性"""
        has_changes = any([self.staged_add, self.staged_del, self.staged_rename, self.staged_meta])
        self.btn_save.setEnabled(has_changes)

        #获取当前状态，保持按钮的强制锁定
        data = self.combo_kb.currentData()
        kb_selected = self.current_kb_id is not None
        kb_status = data.get('status', 'ready') if data else 'ready'
        self._toggle_kb_actions(kb_selected, status=kb_status)

        msg = f"Staged: {len(self.staged_add)} add, {len(self.staged_del)} del, {len(self.staged_rename)} rename"
        if self.staged_meta:
            msg += " | Info Edited"
        if self.rebuild_required:
            msg += " | FULL REBUILD"

        # 覆写提示信息：如果是损坏状态，强制提示用户该怎么做
        is_abnormal = (kb_status != "ready")
        if is_abnormal:
            msg = f"KB IS {kb_status.upper()}. Locked. Please click 'Edit' -> 'Save' to rebuild, or 'Del'."

        self.lbl_staged_status.setText(msg)

        # 样式调整：有改动或者是损坏状态，都高亮显示
        if has_changes or is_abnormal:
            color = "#ff6b6b" if is_abnormal else "#ffb86c"
            self.lbl_staged_status.setStyleSheet(
                f"color: {color}; font-weight: bold; border: 1px solid {color}; padding: 5px;")
        else:
            self.lbl_staged_status.setStyleSheet("color: #888; border: 1px dashed #444; padding: 10px;")

    def commit_changes(self):
        """Commit and apply all staged changes"""
        needs_model = any([self.staged_add, self.staged_del, self.staged_rename, self.rebuild_required])

        data = self.combo_kb.currentData()
        m_id = self.staged_meta['model_id'] if self.staged_meta else (data['model_id'] if data else 'embed_auto')

        if needs_model:
            from src.core.models_registry import get_model_conf, check_model_exists
            conf = get_model_conf(m_id, "embedding")

            if conf and not check_model_exists(conf.get('hf_repo_id')):
                dlg = StandardDialog(
                    self.widget,
                    "Action Required",
                    "The selected AI model weights are missing or incomplete. Please go to 'Global Settings' to download the model first.",
                    show_cancel=True
                )
                if dlg.exec():
                    GlobalSignals().navigate_to_tool.emit("Global Settings")
                    GlobalSignals().request_model_download.emit(m_id, "embedding")
                return

        self.pd = ProgressDialog(
            self.widget, "Synchronizing", "Synchronizing database and file index...",
            telemetry_config={"cpu": True, "ram": True, "gpu": True, "net": False}
        )
        self.pd.sig_canceled.connect(self.task_mgr.cancel_task)
        self.task_mgr.sig_progress.connect(self.pd.update_progress)
        self.pd.show()

        if self.staged_meta:
            m = self.staged_meta
            self.kb_manager.update_kb_info(self.current_kb_id, m['name'], m['description'], m['domain'])
            if self.rebuild_required:
                self.kb_manager._update_meta_field(self.current_kb_id, "model_id", m['model_id'])

        needs_process = any([self.staged_rename, self.staged_del, self.staged_add, self.rebuild_required])

        # 如果只有元数据改动，没有耗时任务
        if self.staged_meta and not needs_process:
            m = self.staged_meta
            self.kb_manager.update_kb_info(self.current_kb_id, m['name'], m['description'], m['domain'])
            self.kb_manager.set_kb_status(self.current_kb_id, "ready")

            self.pd.show_success_state("Info Updated", "Project metadata saved successfully.")
            self._clear_staging_state()
            GlobalSignals().kb_modified.emit(self.current_kb_id)
            return

        if needs_process:
            if self.staged_meta:
                m = self.staged_meta
                self.kb_manager.update_kb_info(self.current_kb_id, m['name'], m['description'], m['domain'])
                if self.rebuild_required:
                    self.kb_manager._update_meta_field(self.current_kb_id, "model_id", m['model_id'])


            self.pd.sig_canceled.connect(self.task_mgr.cancel_task)
            self.task_mgr.sig_progress.connect(self.pd.update_progress)


            self._run_task_chain()

    def _clear_staging_state(self):
        """内部辅助：清理暂存状态并刷新界面"""
        self.staged_add, self.staged_del, self.staged_rename = [], [], {}
        self.staged_meta, self.rebuild_required = None, False
        self.btn_save.setEnabled(False)
        self.refresh_kb_list()
        self.mark_dirty()





    def _on_task_terminated(self):
        self.kb_manager.set_kb_status(self.current_kb_id, "corrupted")
        if self.pd:
            self.pd.close_safe()
            try: self.task_mgr.sig_progress.disconnect(self.pd.update_progress)
            except Exception: pass

        self.staged_add, self.staged_del, self.staged_rename = [], [], {}
        self.staged_meta, self.rebuild_required = None, False

        self.mark_dirty()
        self.refresh_kb_list()
        StandardDialog(self.widget, "Task Terminated",
                       "The process was interrupted. The library may be corrupted and require a full rebuild.").exec()

    def _run_task_chain(self):
        # 必须要先连接信号，再启动任务
        if self.staged_rename:
            self.task_mgr.sig_state_changed.connect(self._on_rename_done)
            self.task_mgr.start_task(RenameFilesTask, "ren", kb_id=self.current_kb_id, renames=self.staged_rename)
        elif self.staged_del:
            self._on_rename_done(TaskState.SUCCESS.value, "")  # 直接跳到删除环节
        elif self.staged_add or self.rebuild_required:
            self._on_del_done(TaskState.SUCCESS.value, "")  # 直接跳到导入/重建环节

    def _on_rename_done(self, state, msg):

        try:
            self.task_mgr.sig_state_changed.disconnect()
        except Exception:
            pass

        if state == TaskState.FAILED.value: return self._on_error(msg)
        if state == TaskState.SUCCESS.value:
            if self.staged_del:
                try:
                    self.task_mgr.sig_state_changed.disconnect(self._on_rename_done)
                except Exception:
                    pass
                self.task_mgr.sig_state_changed.connect(self._on_del_done)
                self.task_mgr.start_task(DeleteFilesTask, "del", kb_id=self.current_kb_id, file_names=self.staged_del)
            else:
                self._on_del_done(TaskState.SUCCESS.value, "")

    def _on_del_done(self, state, msg):

        try:
            self.task_mgr.sig_state_changed.disconnect()
        except Exception:
            pass


        if state == TaskState.FAILED.value: return self._on_error(msg)
        if state == TaskState.SUCCESS.value:
            try:
                self.task_mgr.sig_state_changed.disconnect(self._on_del_done)
            except Exception:
                pass
            self.task_mgr.sig_state_changed.connect(self._on_final_done)

            if self.rebuild_required:
                self.task_mgr.start_task(ImportFilesTask, "reb", kb_id=self.current_kb_id, is_rebuild=True)
            elif self.staged_add:
                self.task_mgr.start_task(ImportFilesTask, "add", kb_id=self.current_kb_id, files=self.staged_add)
            else:
                self._on_final_done(TaskState.SUCCESS.value, "")

    def _on_final_done(self, state, msg):
        try:
            self.task_mgr.sig_progress.disconnect(self.pd.update_progress)
        except:
            pass

        if state == TaskState.SUCCESS.value:
            if self.pd:
                self.pd.show_finish_state(True, "Success", "The library has been fully synchronized and indexed.")
                self.pd = None

            if self.current_kb_id:
                self.kb_manager.set_kb_status(self.current_kb_id, "ready")

            self._clear_staging_state()
            if self.current_kb_id:
                GlobalSignals().kb_modified.emit(self.current_kb_id)

        elif state in [TaskState.FAILED.value, TaskState.TERMINATED.value]:
            self._on_error(msg)

        try:
            self.task_mgr.sig_state_changed.disconnect(self._on_final_done)
        except:
            pass

    def _on_error(self, msg):
        self.kb_manager.set_kb_status(self.current_kb_id, "corrupted")
        self.logger.error(f"Knowledge Base task failed. Error: {msg}")

        msg_lower = msg.lower()
        is_model_error = (
            "oserror" in msg_lower or
            "no file named" in msg_lower or
            "model weights" in msg_lower or
            "model load failed" in msg_lower or
            "onnx" in msg_lower
        )

        if self.pd:
            self.pd.show_finish_state(False, "Process Halted", f"Operation ended: {msg}")
            self.pd = None
        else:
            StandardDialog(self.widget, "Error", f"Operation failed: {msg}").exec()

        if is_model_error:
            dlg = StandardDialog(
                self.widget,
                title="Model Incomplete",
                message="The required AI model files are missing or corrupted. Would you like to go to Settings to download them now?",
                show_cancel=True
            )
            if dlg.exec():
                GlobalSignals().navigate_to_tool.emit("Global Settings")
                data = self.combo_kb.currentData()
                if data:
                    GlobalSignals().request_model_download.emit(data['model_id'], "embedding")

        for slot in [self._on_rename_done, self._on_del_done, self._on_final_done]:
            try:
                self.task_mgr.sig_state_changed.disconnect(slot)
            except:
                pass

        self._clear_staging_state()
        self.refresh_kb_list()

    def create_new_kb(self):
        dlg = ProjectEditorDialog(self.widget, is_edit=False)
        if dlg.exec():
            d = dlg.get_data()
            if d['name']:
                new_id = self.kb_manager.create_kb(d['name'], d['description'], d['domain'], d['model_id'])
                self.logger.info(f"Created new Knowledge Base: '{d['name']}' (ID: {new_id})")  # 🆕
                self.current_kb_id = new_id
                self.refresh_kb_list()
                GlobalSignals().kb_list_changed.emit()


    def edit_current_kb(self):
        curr = self.combo_kb.currentData()
        if not curr: return
        dlg = ProjectEditorDialog(self.widget, is_edit=True, current_data=curr)
        if dlg.exec():
            self.staged_meta = dlg.get_data()

            is_corrupted = curr.get('status') == 'corrupted'
            if self.staged_meta['model_id'] != curr['model_id'] or is_corrupted:
                self.rebuild_required = True

            self.update_file_list()
            self.mark_dirty()

    def delete_current_kb(self):
        data = self.combo_kb.currentData()
        if not data: return
        if StandardDialog(self.widget, "DANGER", f"Confirm deletion of '{data['name']}'?", show_cancel=True).exec():
            self.logger.warning(f"Deleting Knowledge Base: '{data['name']}' (ID: {data['id']})")
            self.kb_manager.delete_kb(data['id'])
            self.current_kb_id = None
            self.refresh_kb_list()
            GlobalSignals().kb_list_changed.emit()


    def _get_file_hash(self, file_path):
        """计算文件的 SHA-256 唯一指纹"""
        hasher = hashlib.sha256()
        try:
            with open(file_path, 'rb') as f:
                while chunk := f.read(1024 * 1024):  # 每次读 1MB，防内存溢出
                    hasher.update(chunk)
            return hasher.hexdigest()
        except:
            return None

    def select_files(self):
        # 允许选择 PDF 和 Markdown
        files, _ = QFileDialog.getOpenFileNames(self.widget, "Select Documents", "", "Documents (*.pdf *.md *.txt *.doc *.docx)")
        if not files: return

        if any(f.lower().endswith('.doc') for f in files):
            from src.ui.components.toast import ToastManager
            ToastManager().show("Legacy .doc format detected. It will be skipped. Please convert to .docx", "warning")

        # 阈值警告
        if len(files) > 100:
            if not StandardDialog(self.widget, "Large Batch",
                                  f"You are importing {len(files)} files. This might take a while. Continue?",
                                  show_cancel=True).exec():
                return

        kb_root = os.path.join(self.kb_manager.WORKSPACE_DIR, self.current_kb_id)
        doc_dir = os.path.join(kb_root, "documents")

        # 构建现有文件指纹库 (Size -> [Path])
        # 这种两级索引比直接算所有 Hash 快得多
        existing_map = {}

        # 1. 扫描磁盘上的文件
        if os.path.exists(doc_dir):
            for f in os.listdir(doc_dir):
                fp = os.path.join(doc_dir, f)
                if os.path.isfile(fp):
                    sz = os.path.getsize(fp)
                    if sz not in existing_map: existing_map[sz] = []
                    existing_map[sz].append(fp)

        # 2. 扫描暂存区文件
        for f in self.staged_add:
            sz = os.path.getsize(f)
            if sz not in existing_map: existing_map[sz] = []
            existing_map[sz].append(f)

        valid_files = []
        duplicate_count = 0

        pd = None
        cancel_flag = [False]

        if len(files) > 50:
            pd = ProgressDialog(self.widget, "Checking Duplicates", "Scanning file signatures...", telemetry_config={})
            pd.sig_canceled.connect(lambda: cancel_flag.__setitem__(0, True))  # 绑定取消事件
            pd.show()

        for i, incoming_path in enumerate(files):
            if cancel_flag[0]:
                break

            QApplication.processEvents()
            if pd: pd.update_progress(int((i / len(files)) * 100), f"Checking {os.path.basename(incoming_path)}...")

            incoming_size = os.path.getsize(incoming_path)

            # 第一道防线：文件大小不同，肯定是新文件
            if incoming_size not in existing_map:
                valid_files.append(incoming_path)
                existing_map[incoming_size] = [incoming_path]
                continue

            # 第二道防线：大小相同，计算 SHA256
            incoming_hash = self._get_file_hash(incoming_path)
            is_dup = False
            for suspect in existing_map[incoming_size]:
                if self._get_file_hash(suspect) == incoming_hash:
                    is_dup = True
                    break

            if is_dup:
                duplicate_count += 1
            else:
                valid_files.append(incoming_path)
                existing_map[incoming_size].append(incoming_path)

        if pd:
            if cancel_flag[0]:
                pd.show_finish_state(False, "Cancelled", "File scanning was cancelled by user.")
                return
            else:
                pd.close_safe()

        if duplicate_count > 0:
            from src.ui.components.toast import ToastManager
            ToastManager().show(f"Skipped {duplicate_count} duplicate files.", "warning")

        if valid_files:
            self.staged_add.extend(valid_files)
            self.update_file_list()
            self.mark_dirty()


    def batch_delete(self, rows):
        for r in reversed(rows):
            name = self.file_table.item(r, 0).text()
            status_item = self.file_table.item(r, 2)
            status_text = status_item.text() if status_item else ""

            if "Indexed" in status_text or "Renaming..." in status_text:
                original_name = name
                # 如果这个文件被重命名过，需要顺藤摸瓜找到它真正的原始名字
                for k, v in self.staged_rename.items():
                    if v == name:
                        original_name = k
                        break

                self.staged_del.append(original_name)

                if original_name in self.staged_rename:
                    del self.staged_rename[original_name]

            elif "Pending Save" in status_text:
                self.staged_add = [f for f in self.staged_add if os.path.basename(f) != name]

            self.file_table.removeRow(r)

        self.mark_dirty()


    def on_kb_switched(self, index):
        if index < 0:
            if hasattr(self, '_toggle_kb_actions'):
                self._toggle_kb_actions(False, status="ready")
            self.current_kb_id = None
            self.update_file_list()
            if hasattr(self, 'mark_dirty'):
                self.mark_dirty()
            return

        data = self.combo_kb.itemData(index)
        if not data: return

        new_kb_id = data.get('id')

        if getattr(self, 'current_kb_id', None) != new_kb_id:
            self.current_kb_id = new_kb_id
            self.staged_add, self.staged_del, self.staged_rename = [], [], {}
            self.staged_meta, self.rebuild_required = None, False

        kb_status = data.get('status', 'ready')
        if hasattr(self, '_toggle_kb_actions'):
            self._toggle_kb_actions(True, status=kb_status)


        self.update_file_list()

        if hasattr(self, 'mark_dirty'):
            self.mark_dirty()


    def export_current_kb(self):
        data = self.combo_kb.currentData()
        if not data: return

        # 默认名称为知识库名字，后缀为 .snp
        default_name = f"{data.get('name', 'Project')}.snp"

        path, _ = QFileDialog.getSaveFileName(
            self.widget,
            "Export Project",
            default_name,
            "Scholar Navis Project (*.snp);;Zip Archive (*.zip)"
        )

        if path:
            # 开启硬件监控进度条 (只需 CPU, RAM, IO，不需要网络和 GPU)
            self.pd = ProgressDialog(
                self.widget, "Exporting Project", "Preparing to pack...",
                telemetry_config={"cpu": True, "ram": True, "gpu": False, "net": False, "io": True}
            )
            self.pd.show()

            # 绑定任务回调
            self.task_mgr.sig_progress.connect(self.pd.update_progress)
            self.task_mgr.sig_state_changed.connect(self._on_export_done)
            self.pd.sig_canceled.connect(self.task_mgr.cancel_task)

            from src.task.kb_tasks import ExportKBTask
            self.task_mgr.start_task(ExportKBTask, "export", kb_id=self.current_kb_id, dest_path=path)


    def _on_export_done(self, state, msg):
        if state == TaskState.SUCCESS.value:
            self.pd.show_finish_state(True, "Export Success", "Project has been successfully exported.")
        elif state in [TaskState.FAILED.value, TaskState.TERMINATED.value]:
            self.pd.show_finish_state(False, "Export Halted", f"Task ended: {msg}")

        try:
            self.task_mgr.sig_state_changed.disconnect(self._on_export_done)
        except Exception:
            pass
        try:
            self.task_mgr.sig_progress.disconnect(self.pd.update_progress)
        except Exception:
            pass


    def import_external_kb(self):
        path, _ = QFileDialog.getOpenFileName(
            self.widget,
            "Import Project",
            "",
            "Project Bundle (*.snp *.zip)"
        )

        if path:
            # 开启硬件监控进度条
            self.pd = ProgressDialog(
                self.widget, "Importing Project", "Reading archive...",
                telemetry_config={"cpu": True, "ram": True, "gpu": False, "net": False, "io": True}
            )
            self.pd.show()

            # 绑定任务回调
            self.task_mgr.sig_progress.connect(self.pd.update_progress)
            self.task_mgr.sig_state_changed.connect(self._on_import_done)
            self.pd.sig_canceled.connect(self.task_mgr.cancel_task)

            from src.task.kb_tasks import ImportExternalKBTask
            self.task_mgr.start_task(ImportExternalKBTask, "import", bundle_path=path)


    def _on_import_done(self, state, msg):
        if state == TaskState.SUCCESS.value:
            self.pd.show_finish_state(True, "Import Success", "Project has been successfully imported.")
            self.refresh_kb_list()
        elif state in [TaskState.FAILED.value, TaskState.TERMINATED.value]:
            self.pd.show_finish_state(False, "Import Halted", f"Task ended: {msg}")

        try:
            self.task_mgr.sig_state_changed.disconnect(self._on_import_done)
        except Exception:
            pass
        try:
            self.task_mgr.sig_progress.disconnect(self.pd.update_progress)
        except Exception:
            pass
        GlobalSignals().kb_list_changed.emit()


    def download_required_model(self):
        self.pd = ProgressDialog(
            self.widget, "Downloader", "Connecting...",
            telemetry_config={"cpu": False, "ram": False, "gpu": False, "net": True}
        )
        self.pd.show()

        self.task_mgr.sig_progress.connect(self.pd.update_progress)

        def on_download_state_changed(state, msg):
            if state == TaskState.SUCCESS.value:
                self.pd.show_success_state("Complete", "Model downloaded successfully.")
                self.refresh_kb_list()  # 下载完刷新一下状态
            elif state == TaskState.FAILED.value:
                self.pd.close_safe()
                StandardDialog(self.widget, "Download Failed", f"Network error: {msg}").exec()

            try:
                self.task_mgr.sig_state_changed.disconnect(on_download_state_changed)
            except Exception:
                pass

        self.task_mgr.sig_state_changed.connect(on_download_state_changed)
        self.pd.sig_canceled.connect(self.task_mgr.cancel_task)
        self.task_mgr.start_task(SwitchKBTask, "dl", kb_id=self.current_kb_id)


    def _handle_open(self, rows):
        """修复混淆后的文件打开逻辑"""
        kb_data = self.kb_manager.get_kb_by_id(self.current_kb_id)
        file_map = kb_data.get('file_map', {})
        # 反向映射：真名 -> UUID
        reverse_map = {v: k for k, v in file_map.items()}

        for r in rows:
            real_name = self.file_table.item(r, 0).text()
            obfuscated_name = reverse_map.get(real_name)

            if obfuscated_name:
                source_path = os.path.join(self.kb_manager.WORKSPACE_DIR, self.current_kb_id, "documents",
                                           obfuscated_name)
                if os.path.exists(source_path):
                    temp_dir = os.path.join(tempfile.gettempdir(), "ScholarNavis_View")
                    os.makedirs(temp_dir, exist_ok=True)
                    temp_file_path = os.path.join(temp_dir, real_name)
                    try:
                        shutil.copy2(source_path, temp_file_path)
                        FileService.open_file(temp_file_path)
                    except Exception as e:
                        StandardDialog(self.widget, "Error", f"Failed to open file: {e}").exec()
