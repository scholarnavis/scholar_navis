"""Settings mixin: WebDAV 备份源管理与远端备份的上传 / 恢复。

只做界面编排：备份源的增删改落到 :class:`ConfigManager`，协议细节在
:mod:`src.core.webdav_backup`，传输与序列化在 :mod:`src.task.backup_tasks`。
恢复刻意复用既有导入链路（``on_import_clicked``），因此口令解密、结构校验、
技能还原、未保存标记的行为与"从本地文件导入"完全一致。
"""

import logging
from email.utils import parsedate_to_datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from src.core.config_bundle import build_bundle
from src.core.core_task import TaskManager, TaskMode
from src.core.i18n import tr
from src.core.theme_manager import ThemeManager
from src.task.backup_tasks import (ListWebDAVBackupsTask, RestoreWebDAVBackupTask,
                                   UploadWebDAVBackupTask)
from src.ui.components.combo import BaseComboBox
from src.ui.components.dialogs.backup_dialogs import WebDAVSourceDialog
from src.ui.components.dialogs.common import (ExportPasswordDialog, ProgressDialog,
                                              StandardDialog)
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)

#: 备份表格里保存远端文件名的角色（放在文件名单元格上）
_REMOTE_NAME_ROLE = Qt.UserRole

#: 远端备份表格的列
_BACKUP_COLUMNS = ("Filename", "Size", "Modified")

#: 备份表格的高度随行数伸缩：有数据时恰好容纳现有行（空表留 2 行），
#: 超过 8 行才出滚动条，避免"只有一条备份却占掉半个区块"的空白。
_BACKUP_TABLE_EMPTY_ROWS = 2
_BACKUP_TABLE_MAX_ROWS = 8


class BackupSectionMixin:
    """WebDAV 备份区块。"""

    # ---------- 构建 ----------
    def init_backup_section(self):
        self.group_backup = QGroupBox(tr("WebDAV Backup"))
        box = QVBoxLayout(self.group_backup)
        box.setSpacing(10)

        self.lbl_backup_hint = QLabel(tr(
            "Upload the current configuration bundle to a WebDAV folder, and restore "
            "any backup from there. Restoring follows the same import flow as a local "
            "file, so an encrypted backup still asks for its password."))
        self.lbl_backup_hint.setWordWrap(True)
        box.addWidget(self.lbl_backup_hint)

        source_row = QHBoxLayout()
        source_row.addWidget(QLabel(tr("Backup source:")))
        self.combo_backup_source = BaseComboBox()
        self.combo_backup_source.setMinimumWidth(220)
        self.combo_backup_source.currentIndexChanged.connect(self._on_backup_source_changed)
        source_row.addWidget(self.combo_backup_source, 1)

        self.btn_add_webdav = QPushButton(tr("Add"))
        self.btn_add_webdav.clicked.connect(self._on_add_backup_source)
        self.btn_edit_webdav = QPushButton(tr("Edit"))
        self.btn_edit_webdav.clicked.connect(self._on_edit_backup_source)
        self.btn_del_webdav = QPushButton(tr("Delete"))
        self.btn_del_webdav.clicked.connect(self._on_delete_backup_source)
        self.btn_test_webdav = QPushButton(tr("Test Connection"))
        self.btn_test_webdav.clicked.connect(self._on_test_backup_source)
        for btn in (self.btn_add_webdav, self.btn_edit_webdav,
                    self.btn_del_webdav, self.btn_test_webdav):
            source_row.addWidget(btn)
        box.addLayout(source_row)

        action_row = QHBoxLayout()
        action_row.addWidget(QLabel(tr("Remote backups:")))
        action_row.addStretch()
        self.btn_refresh_backups = QPushButton(tr("Refresh List"))
        self.btn_refresh_backups.clicked.connect(self._on_refresh_backups)
        self.btn_upload_backup = QPushButton(tr("Upload Current Config"))
        self.btn_upload_backup.clicked.connect(self._on_upload_backup)
        self.btn_restore_backup = QPushButton(tr("Restore Selected"))
        self.btn_restore_backup.clicked.connect(self._on_restore_backup)
        for btn in (self.btn_refresh_backups, self.btn_upload_backup, self.btn_restore_backup):
            action_row.addWidget(btn)
        box.addLayout(action_row)

        self.table_backups = QTableWidget(0, len(_BACKUP_COLUMNS))
        self.table_backups.setHorizontalHeaderLabels([tr(c) for c in _BACKUP_COLUMNS])
        self.table_backups.verticalHeader().setVisible(False)
        self.table_backups.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table_backups.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_backups.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table_backups.setWordWrap(False)
        header = self.table_backups.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setStretchLastSection(False)
        self.table_backups.itemSelectionChanged.connect(self._on_backup_selection_changed)
        self.table_backups.cellDoubleClicked.connect(lambda *_args: self._on_restore_backup())
        box.addWidget(self.table_backups)
        self._fit_backup_table_height()

        self.lbl_backup_status = QLabel("")
        self.lbl_backup_status.setWordWrap(True)
        box.addWidget(self.lbl_backup_status)

        self.layout.addWidget(self.group_backup)

        # 清单与传输分成两个任务管理器：刷新清单不应取消正在进行的上传/恢复。
        self.webdav_list_mgr = TaskManager()
        self.webdav_list_mgr.sig_progress.connect(self._on_webdav_list_progress)
        self.webdav_list_mgr.sig_result.connect(self._on_webdav_list_result)

        self.webdav_transfer_mgr = TaskManager()
        self.webdav_transfer_mgr.sig_progress.connect(self._on_webdav_transfer_progress)
        self.webdav_transfer_mgr.sig_result.connect(self._on_webdav_transfer_result)

        self._webdav_list_intent = "refresh"
        self._webdav_transfer_intent = ""
        self._backup_busy = False
        self.upload_pd = None
        self.restore_pd = None
        self._reload_backup_sources()

    # ---------- 备份源列表 ----------
    def _reload_backup_sources(self, select_id=None):
        """把 ``config.webdav_sources`` 同步到下拉框（保留当前选择）。"""
        current = select_id or self._current_backup_source_id()
        sources = list(getattr(self.config, "webdav_sources", None) or [])

        self.combo_backup_source.blockSignals(True)
        self.combo_backup_source.clear()
        for source in sources:
            self.combo_backup_source.addItem(str(source.get("name") or source.get("id") or ""),
                                             str(source.get("id") or ""))
        if current:
            index = self.combo_backup_source.findData(current)
            if index >= 0:
                self.combo_backup_source.setCurrentIndex(index)
        self.combo_backup_source.blockSignals(False)

        self._set_backup_busy(False)
        self._update_backup_source_hint()

    def _current_backup_source_id(self) -> str:
        data = self.combo_backup_source.currentData()
        return str(data) if data else ""

    def _current_backup_profile(self):
        source_id = self._current_backup_source_id()
        for source in (getattr(self.config, "webdav_sources", None) or []):
            if str(source.get("id") or "") == source_id:
                return dict(source)
        return None

    def _has_backup_source(self) -> bool:
        return bool(getattr(self.config, "webdav_sources", None))

    def _update_backup_source_hint(self):
        if not self._has_backup_source():
            self.lbl_backup_status.setText(
                tr("No backup source yet. Click 'Add' to configure a WebDAV server."))
            return
        profile = self._current_backup_profile()
        if profile:
            self.lbl_backup_status.setText(
                tr("Folder on server: {url}").format(url=_remote_display_url(profile)))

    def _on_backup_source_changed(self, *_args):
        self._clear_backup_rows()
        self._update_backup_source_hint()
        self._set_backup_busy(getattr(self, "_backup_busy", False))

    def _clear_backup_rows(self):
        self.table_backups.setRowCount(0)
        self._fit_backup_table_height()

    def _on_backup_selection_changed(self):
        self._refresh_restore_button()

    def _selected_backup_name(self) -> str:
        row = self.table_backups.currentRow()
        if row < 0:
            return ""
        item = self.table_backups.item(row, 0)
        return str(item.data(_REMOTE_NAME_ROLE) or "") if item is not None else ""

    def _refresh_restore_button(self):
        busy = getattr(self, "_backup_busy", False)
        self.btn_restore_backup.setEnabled(
            not busy and self._has_backup_source() and bool(self._selected_backup_name()))

    # ---------- 备份源增删改 ----------
    def _on_add_backup_source(self):
        self._open_backup_source_dialog(None)

    def _on_edit_backup_source(self):
        profile = self._current_backup_profile()
        if not profile:
            return
        self._open_backup_source_dialog(profile)

    def _open_backup_source_dialog(self, profile):
        dlg = WebDAVSourceDialog(self.widget, profile=profile)
        if not dlg.exec() or not dlg.result_profile:
            return
        try:
            self.config.upsert_webdav_source(dlg.result_profile)
        except OSError as e:
            logger.exception("Saving the WebDAV backup source failed")
            StandardDialog(self.widget, tr("Save Failed"),
                           tr("Could not save the backup source: {err}").format(err=e)).exec()
            return
        self._reload_backup_sources(select_id=dlg.result_profile.get("id"))
        ToastManager().show(tr("Backup source saved."), "success")

    def _on_delete_backup_source(self):
        profile = self._current_backup_profile()
        if not profile:
            return
        dlg = StandardDialog(
            self.widget, tr("Delete Backup Source"),
            tr("Delete the backup source '{name}'? Files already on the server "
               "are not touched.").format(name=profile.get("name") or ""),
            show_cancel=True)
        if not dlg.exec():
            return
        try:
            self.config.remove_webdav_source(str(profile.get("id") or ""))
        except OSError as e:
            logger.exception("Removing the WebDAV backup source failed")
            StandardDialog(self.widget, tr("Delete Failed"),
                           tr("Could not delete the backup source: {err}").format(err=e)).exec()
            return
        self._clear_backup_rows()
        self._reload_backup_sources()

    # ---------- 远端清单 ----------
    def _on_test_backup_source(self):
        self._start_backup_listing("test")

    def _on_refresh_backups(self):
        self._start_backup_listing("refresh")

    def _start_backup_listing(self, intent: str):
        profile = self._current_backup_profile()
        if not profile:
            return
        self._webdav_list_intent = intent
        self.btn_refresh_backups.setEnabled(False)
        self.btn_test_webdav.setEnabled(False)
        self.lbl_backup_status.setText(tr("Connecting to the WebDAV server..."))
        self.webdav_list_mgr.start_task(
            ListWebDAVBackupsTask, "webdav_list", mode=TaskMode.THREAD, profile=profile)

    def _on_webdav_list_progress(self, percent, msg=None):
        if msg:
            self.lbl_backup_status.setText(str(msg))

    def _on_webdav_list_result(self, result):
        intent = self._webdav_list_intent
        self.btn_refresh_backups.setEnabled(True)
        self.btn_test_webdav.setEnabled(True)

        if not result.get("success"):
            self.lbl_backup_status.setText(tr("Backup list unavailable."))
            StandardDialog(self.widget, tr("WebDAV Error"),
                           str(result.get("msg") or tr("Unknown error"))).exec()
            return

        files = result.get("files") or []
        self._fill_backup_rows(files)
        self._refresh_restore_button()

        if intent == "test":
            message = tr("Connection OK. {count} backup(s) on the server.").format(count=len(files))
            ToastManager().show(message, "success")
            self.lbl_backup_status.setText(message)
        else:
            self.lbl_backup_status.setText(
                tr("{count} backup(s) on the server.").format(count=len(files)))

    # ---------- 上传 / 恢复 ----------
    def _on_upload_backup(self):
        if not self._current_backup_profile():
            return
        if not self.check_unsaved_changes(proceed_callback=self._start_backup_upload):
            return
        self._start_backup_upload()

    def _start_backup_upload(self):
        profile = self._current_backup_profile()
        if not profile:
            return

        pwd_dlg = ExportPasswordDialog(self.widget)
        pwd_dlg.exec()
        if pwd_dlg.is_cancelled:
            return

        # 与本地导出同一构造口径：上传的是已保存的配置，未保存的界面改动不进包。
        bundle = build_bundle(self.config, self.llm_configs)

        self._webdav_transfer_intent = "upload"
        self._set_backup_busy(True)
        self.upload_pd = ProgressDialog(
            self.widget, tr("WebDAV Backup"),
            tr("Compressing and uploading the configuration bundle..."))
        self.upload_pd.sig_canceled.connect(self.webdav_transfer_mgr.cancel_task)
        self.upload_pd.show()
        self.webdav_transfer_mgr.start_task(
            UploadWebDAVBackupTask, "webdav_upload", mode=TaskMode.THREAD,
            profile=profile, password=pwd_dlg.password, bundle=bundle)

    def _on_restore_backup(self):
        name = self._selected_backup_name()
        if not name:
            ToastManager().show(tr("Select a backup in the list first."), "info")
            return
        if not self.check_unsaved_changes(proceed_callback=lambda: self._start_backup_restore(name)):
            return
        self._start_backup_restore(name)

    def _start_backup_restore(self, name: str):
        profile = self._current_backup_profile()
        if not profile:
            return

        self._webdav_transfer_intent = "restore"
        self._set_backup_busy(True)
        self.restore_pd = ProgressDialog(
            self.widget, tr("Restore Backup"),
            tr("Downloading {name}...").format(name=name))
        self.restore_pd.sig_canceled.connect(self.webdav_transfer_mgr.cancel_task)
        self.restore_pd.show()
        self.webdav_transfer_mgr.start_task(
            RestoreWebDAVBackupTask, "webdav_restore", mode=TaskMode.THREAD,
            profile=profile, name=name)

    def _on_webdav_transfer_progress(self, percent, msg=None):
        pd = self.upload_pd or self.restore_pd
        if pd is not None:
            pd.update_progress(percent, msg)

    def _on_webdav_transfer_result(self, result):
        intent = self._webdav_transfer_intent
        self._webdav_transfer_intent = ""
        pd, self.upload_pd, self.restore_pd = (self.upload_pd or self.restore_pd), None, None
        self._set_backup_busy(False)

        if not result.get("success"):
            if pd is not None:
                pd.show_finish_state(False, tr("Operation Failed"),
                                     str(result.get("msg") or tr("Unknown error")))
            return

        if intent == "restore":
            # 关掉传输进度框，把暂存文件交给既有导入流程（它自己会开进度框与口令框）
            if pd is not None:
                pd.close_safe()
            self.on_import_clicked(auto_path=str(result.get("path") or ""))
            return

        if pd is not None:
            pd.show_finish_state(
                True, tr("Backup Uploaded"),
                tr("Uploaded {name} to the WebDAV server.").format(name=result.get("file") or ""))
        self._start_backup_listing("refresh")

    def _set_backup_busy(self, busy: bool):
        self._backup_busy = bool(busy)
        # 新增入口与是否存在备份源无关，始终可用（只要不在传输中）
        self.btn_add_webdav.setEnabled(not busy)
        has_source = self._has_backup_source()
        for btn in (self.btn_edit_webdav, self.btn_del_webdav, self.btn_test_webdav,
                    self.btn_refresh_backups, self.btn_upload_backup):
            btn.setEnabled(not busy and has_source)
        self._refresh_restore_button()

    def _fill_backup_rows(self, files):
        """把远端清单铺进表格：文件名 / 大小 / 修改时间。

        排序用途的数据通过 :class:`_SortableCell` 携带，因此点表头得到的是
        "大小按字节、时间按真实时刻"，而不是显示文本的字典序。
        """
        table = self.table_backups
        table.setSortingEnabled(False)
        # 行内容会随刷新整体更换，旧选中行不能再当作有效选择
        table.clearSelection()
        table.setCurrentCell(-1, -1)
        table.setRowCount(len(files))
        for row, entry in enumerate(files):
            name = str(entry.get("name") or "")
            size_bytes = max(0, int(entry.get("size") or 0))
            stamp, epoch = _remote_timestamp(entry.get("modified"))

            name_item = QTableWidgetItem(name)
            name_item.setData(_REMOTE_NAME_ROLE, name)
            name_item.setToolTip(name)

            size_item = _SortableCell(f"{size_bytes / 1024.0:.1f} KB", size_bytes)
            size_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            time_item = _SortableCell(stamp, epoch)
            time_item.setToolTip(stamp)

            table.setItem(row, 0, name_item)
            table.setItem(row, 1, size_item)
            table.setItem(row, 2, time_item)

        table.setSortingEnabled(True)
        # 文件名前缀是上传时间戳，倒序即最新备份在最上面
        table.sortItems(0, Qt.DescendingOrder)
        self._fit_backup_table_height()

    def _fit_backup_table_height(self):
        """按行数定高：一条备份就一行高，不留一片空白。"""
        table = self.table_backups
        rows = table.rowCount()
        row_height = table.rowHeight(0) if rows else table.verticalHeader().defaultSectionSize()
        visible = min(rows or _BACKUP_TABLE_EMPTY_ROWS, _BACKUP_TABLE_MAX_ROWS)
        chrome = table.horizontalHeader().sizeHint().height() + 2 * table.frameWidth()
        table.setFixedHeight(chrome + visible * row_height)

    # ---------- 主题 ----------
    def _apply_backup_theme(self):
        tm = ThemeManager()
        self.group_backup.setStyleSheet(
            f"QGroupBox {{ border: 1px solid {tm.color('border')}; border-radius: 6px;"
            f" margin-top: 10px; padding: 12px; color: {tm.color('text_main')}; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px;"
            f" color: {tm.color('text_muted')}; }}")
        self.lbl_backup_hint.setStyleSheet(
            f"color: {tm.color('text_muted')}; font-size: 11px;")
        self.lbl_backup_status.setStyleSheet(
            f"color: {tm.color('text_muted')}; font-size: 11px;")
        self.combo_backup_source.setStyleSheet(self._get_input_style())
        self.table_backups.setStyleSheet(
            f"QTableWidget {{ background-color: {tm.color('bg_card')};"
            f" color: {tm.color('text_main')};"
            f" border: 1px solid {tm.color('border')}; border-radius: 4px; }}"
            f"QTableWidget::item:selected {{ background-color: {tm.color('btn_hover')};"
            f" color: {tm.color('text_main')}; }}"
            f"QHeaderView::section {{ background-color: {tm.color('bg_input')};"
            f" color: {tm.color('text_muted')};"
            f" border: 1px solid {tm.color('border')}; padding: 4px; }}")

        self.btn_add_webdav.setStyleSheet(self._get_btn_style(btn_type="success"))
        self.btn_edit_webdav.setStyleSheet(self._get_btn_style())
        self.btn_del_webdav.setStyleSheet(self._get_btn_style(btn_type="danger"))
        self.btn_test_webdav.setStyleSheet(self._get_btn_style())
        self.btn_refresh_backups.setStyleSheet(self._get_btn_style())
        self.btn_upload_backup.setStyleSheet(self._get_btn_style(btn_type="primary"))
        self.btn_restore_backup.setStyleSheet(self._get_btn_style(btn_type="primary"))
        # 字体变化会改变行高，高度按新的行高重算
        self._fit_backup_table_height()


def _remote_display_url(profile: dict) -> str:
    base = str(profile.get("url") or "").rstrip("/")
    folder = str(profile.get("remote_dir") or "").strip("/")
    return f"{base}/{folder}" if folder else base


class _SortableCell(QTableWidgetItem):
    """显示文本自定义、排序按原始数值的单元格。"""

    def __init__(self, text: str, value):
        super().__init__(text)
        self._value = value

    def __lt__(self, other):
        if isinstance(other, _SortableCell):
            return self._value < other._value
        return super().__lt__(other)


def _remote_timestamp(raw) -> tuple:
    """HTTP 日期 → (本地时间显示, 可比较的时间戳)。

    远端给的是 ``Tue, 24 Nov 2026 11:07:25 GMT`` 这类以星期名开头的串，直接按
    文本排序毫无意义，因此解析出时刻供排序；解析失败时退回原串且时间戳为 0。
    """
    text = str(raw or "").strip()
    if not text:
        return "", 0.0
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return text, 0.0
    if moment is None:
        return text, 0.0
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.strftime("%Y-%m-%d %H:%M:%S"), moment.timestamp()
