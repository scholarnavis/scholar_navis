"""Settings UI mixin: 付费墙取全文窗口的监听目录。

拆分自 src/tools/settings_tool.py。方法运行于共享的 SettingsTool 实例上，通过 `self`
访问其状态（config / layout / _mark_unsaved）。

目录口径全部来自 :mod:`src.core.fulltext_dirs`：本文件只做"展示 + 增删 + 打开 + 落盘"，
不自己展开 ``~``、不自己判上限，避免与扫描端口径分叉。

打开目录统一走 :func:`src.core.platform_env.open_with_system`（跨平台、无 shell），
不使用 :func:`reveal_in_file_manager`：后者在 Linux 上打开的是**父目录**，而这里要的
是"进到该目录里去看 PDF"。
"""

import html
import logging
import os

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QGroupBox, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton,
                               QVBoxLayout)

from src.core.fulltext_dirs import (MAX_WATCH_DIRS, downloads_dir, inbox_dir,
                                    normalize_dirs)
from src.core.i18n import tr
from src.core.platform_env import open_with_system
from src.core.theme_manager import ThemeManager
from src.ui.components.toast import ToastManager

logger = logging.getLogger("Settings.FulltextSection")


class FulltextSectionMixin:
    """取全文窗口的监听目录：内置收件目录只读展示 + 额外目录可增删。"""

    def init_fulltext_section(self):
        self.group_fulltext = QGroupBox(tr("Full-Text Retrieval"))
        self.group_fulltext.setObjectName("group_fulltext")
        layout = QVBoxLayout(self.group_fulltext)

        self.lbl_fulltext_hint = QLabel(tr(
            "The paywalled full-text picker watches the directories below. Drop the "
            "downloaded PDF into any of them and it is claimed automatically as long "
            "as the file name matches the requested paper. Double-click a watched "
            "directory to open it in the file manager."))
        self.lbl_fulltext_hint.setWordWrap(True)
        layout.addWidget(self.lbl_fulltext_hint)

        # 内置收件目录：路径本身是链接，点一下就在文件管理器里打开（与列表双击同一入口）。
        self.lbl_fulltext_inbox = QLabel()
        self.lbl_fulltext_inbox.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_fulltext_inbox.setOpenExternalLinks(False)  # 自己打开，才能先判存在性
        self.lbl_fulltext_inbox.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)
        self.lbl_fulltext_inbox.setWordWrap(True)
        inbox = inbox_dir()
        self.lbl_fulltext_inbox.setText(
            f"{tr('Built-in inbox (always watched):')} "
            f"<a href=\"{html.escape(QUrl.fromLocalFile(inbox).toString(), quote=True)}\">"
            f"{html.escape(inbox)}</a>")
        self.lbl_fulltext_inbox.setToolTip(tr("Open this directory in the file manager."))
        self.lbl_fulltext_inbox.linkActivated.connect(
            lambda _href: self._open_fulltext_dir(inbox))
        layout.addWidget(self.lbl_fulltext_inbox)

        self.lbl_fulltext_count = QLabel()
        layout.addWidget(self.lbl_fulltext_count)

        self.list_fulltext_dirs = QListWidget()
        self.list_fulltext_dirs.setObjectName("list_fulltext_dirs")
        # 双击留给"打开该目录"；默认的可编辑标记会把双击变成重命名框。
        self.list_fulltext_dirs.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list_fulltext_dirs.itemDoubleClicked.connect(
            self._on_fulltext_dir_double_clicked)
        self.list_fulltext_dirs.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection)
        self.list_fulltext_dirs.setMinimumHeight(96)
        layout.addWidget(self.list_fulltext_dirs)

        row = QHBoxLayout()
        self.btn_add_fulltext_dir = QPushButton(tr("Add Directory..."))
        self.btn_add_fulltext_dir.clicked.connect(self._on_add_fulltext_dir)
        row.addWidget(self.btn_add_fulltext_dir)

        self.btn_del_fulltext_dir = QPushButton(tr("Remove Selected"))
        self.btn_del_fulltext_dir.clicked.connect(self._on_remove_fulltext_dir)
        row.addWidget(self.btn_del_fulltext_dir)
        row.addStretch()
        layout.addLayout(row)

        self.lbl_fulltext_perf = QLabel(tr(
            "Each watched directory is listed on a timer while the picker is open, so "
            "more directories cost more disk scanning; the picker backs off "
            "automatically when a directory is large or quiet. Missing directories are "
            "skipped."))
        self.lbl_fulltext_perf.setWordWrap(True)
        layout.addWidget(self.lbl_fulltext_perf)

        self._set_fulltext_dirs([])
        self.layout.addWidget(self.group_fulltext)

    # ---------- 展示 ----------

    def _set_fulltext_dirs(self, dirs):
        """把目录列表灌进控件（载入配置与增删后都走这里）。"""
        if not hasattr(self, "list_fulltext_dirs"):
            return
        normalized = normalize_dirs(dirs)
        self.list_fulltext_dirs.clear()
        for path in normalized:
            item = QListWidgetItem(path)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            if not os.path.isdir(path):
                # 不删掉它：外接盘/网络盘可能只是暂时不在，删了等于替用户丢配置。
                item.setText(f"{path}  {tr('(missing)')}")
                item.setForeground(QBrush(QColor(ThemeManager().color("text_muted"))))
            self.list_fulltext_dirs.addItem(item)
        self._refresh_fulltext_count()

    def _refresh_fulltext_count(self):
        if not hasattr(self, "lbl_fulltext_count"):
            return
        count = self.list_fulltext_dirs.count()
        self.lbl_fulltext_count.setText(
            f"{tr('Extra watch directories')}: {count}/{MAX_WATCH_DIRS}")
        if hasattr(self, "btn_add_fulltext_dir"):
            self.btn_add_fulltext_dir.setEnabled(count < MAX_WATCH_DIRS)

    def _fulltext_dirs_from_ui(self):
        """当前列表里的目录（去重/上限的口径全交给 fulltext_dirs）。"""
        if not hasattr(self, "list_fulltext_dirs"):
            return []
        return normalize_dirs([
            self.list_fulltext_dirs.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list_fulltext_dirs.count())])

    # ---------- 打开 ----------

    def _on_fulltext_dir_double_clicked(self, item):
        self._open_fulltext_dir(item.data(Qt.ItemDataRole.UserRole))

    def _open_fulltext_dir(self, path):
        """在系统文件管理器里打开监听目录。

        目录不在时给出提示而不是静默失败：外接盘/网络盘掉线后，用户双击只会"没反应"，
        那比报错更难排查。
        """
        path = str(path or "").strip()
        if not path:
            return
        if not os.path.isdir(path):
            logger.info("Full-text watch dir is not there, not opening: %s", path)
            ToastManager().show(tr("This directory does not exist (yet)."), "warning")
            return
        if not open_with_system(path):
            logger.warning("No system file manager opened the directory: %s", path)
            ToastManager().show(
                tr("Could not open the directory with the system file manager."),
                "warning")
            return
        logger.info("Opened full-text watch dir: %s", path)

    # ---------- 增删 ----------

    def _on_add_fulltext_dir(self):
        from src.ui.components.file_dialogs import existing_directory

        current = self._fulltext_dirs_from_ui()
        if len(current) >= MAX_WATCH_DIRS:
            ToastManager().show(
                tr("Up to {n} extra directories can be watched.").format(n=MAX_WATCH_DIRS),
                "info")
            return

        # 起始目录沿用最后一个已配置项；列表为空时落在系统下载目录（默认值来源，
        # 也是用户最常放 PDF 的地方），拿不到才退回家目录。
        start = current[-1] if current else (downloads_dir() or os.path.expanduser("~"))
        path = existing_directory(self.widget, tr("Select a directory to watch"), start)
        if not path:
            return

        merged = normalize_dirs(current + [path])
        if len(merged) == len(current):
            ToastManager().show(tr("This directory is already in the list."), "info")
            return
        logger.info("Full-text watch dir added: %s", path)
        self._set_fulltext_dirs(merged)
        self._mark_unsaved()

    def _on_remove_fulltext_dir(self):
        selected = self.list_fulltext_dirs.selectedItems()
        if not selected:
            ToastManager().show(tr("Select the directories to remove first."), "info")
            return
        removed = {item.data(Qt.ItemDataRole.UserRole) for item in selected}
        logger.info("Full-text watch dir removed: %s", sorted(removed))
        self._set_fulltext_dirs(
            [p for p in self._fulltext_dirs_from_ui() if p not in removed])
        self._mark_unsaved()