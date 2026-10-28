"""External tools section: R runtime, bioinformatics tools and WSL.

与 R 环境区块并列放在设置页：三者的共同点是"应用并不自带，取决于用户机器上装了
什么"。分开放在不同页面，用户排查"为什么某个分析跑不了"时要在两处来回跳。

检测本身会真的起子进程读版本（每个工具几十毫秒），因此这里只做一次汇总刷新，不在
每个控件上各探测一遍。
"""

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QGroupBox, QHBoxLayout, QLabel,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from src.core.i18n import tr
from src.core.theme_manager import ThemeManager

logger = logging.getLogger(__name__)

#: 表格列序：名称 / 状态 / 来源 / 版本 / 帮助缓存 / 路径。
_COLUMNS = ("Tool", "Status", "Source", "Version", "Help", "Path")


class ToolEnvSectionMixin:
    """设置页的"外置工具"区块。"""

    def init_toolenv_section(self):
        """Build the external tools block (R + bioinformatics + WSL)."""
        self.group_toolenv = QGroupBox(tr("External Tools (R and Bioinformatics)"))
        self.group_toolenv.setObjectName("group_toolenv")
        layout = QVBoxLayout(self.group_toolenv)

        self.lbl_toolenv_status = QLabel(tr("Detecting external tools..."))
        self.lbl_toolenv_status.setTextFormat(Qt.RichText)
        self.lbl_toolenv_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_toolenv_status.setWordWrap(True)
        layout.addWidget(self.lbl_toolenv_status)

        self.table_toolenv = QTableWidget(0, len(_COLUMNS))
        self.table_toolenv.setHorizontalHeaderLabels([tr(name) for name in _COLUMNS])
        self.table_toolenv.verticalHeader().setVisible(False)
        self.table_toolenv.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table_toolenv.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_toolenv.horizontalHeader().setStretchLastSection(True)
        self.table_toolenv.setMinimumHeight(150)
        layout.addWidget(self.table_toolenv)

        self.lbl_toolenv_hint = QLabel(
            tr("Tools already installed on this machine are used in preference to the "
               "private prefix. A tool's help output is captured once before its first "
               "use, so option names always match the installed version."))
        self.lbl_toolenv_hint.setWordWrap(True)
        layout.addWidget(self.lbl_toolenv_hint)

        action_row = QHBoxLayout()
        self.btn_toolenv_refresh = QPushButton(tr("Re-detect"))
        self.btn_toolenv_refresh.clicked.connect(self._on_toolenv_refresh)
        action_row.addWidget(self.btn_toolenv_refresh)

        self.btn_toolenv_open = QPushButton(tr("Open data folder"))
        self.btn_toolenv_open.clicked.connect(self._on_toolenv_open_folder)
        action_row.addWidget(self.btn_toolenv_open)

        self.btn_toolenv_copy = QPushButton(tr("Copy install command"))
        self.btn_toolenv_copy.clicked.connect(self._on_toolenv_copy_command)
        action_row.addWidget(self.btn_toolenv_copy)
        action_row.addStretch()
        layout.addLayout(action_row)

        self.layout.addWidget(self.group_toolenv)
        self._style_toolenv()
        self._refresh_toolenv_status()

    # ---------------------------------------------------------------- #
    #  样式
    # ---------------------------------------------------------------- #
    def _style_toolenv(self):
        """Apply theme colours (called on build and on every theme switch)."""
        tm = ThemeManager()
        if hasattr(self, "table_toolenv"):
            self.table_toolenv.setStyleSheet(
                f"QTableWidget {{ background-color: {tm.color('bg_card')};"
                f" color: {tm.color('text_main')};"
                f" border: 1px solid {tm.color('border')}; }}"
                f"QHeaderView::section {{ background-color: {tm.color('bg_input')};"
                f" color: {tm.color('text_muted')};"
                f" border: 1px solid {tm.color('border')}; padding: 4px; }}")
        if hasattr(self, "lbl_toolenv_status"):
            self.lbl_toolenv_status.setStyleSheet(
                f"color: {tm.color('text_main')}; font-size: 12px;")
        if hasattr(self, "lbl_toolenv_hint"):
            self.lbl_toolenv_hint.setStyleSheet(
                f"color: {tm.color('text_muted')}; font-size: 11px;")

    # ---------------------------------------------------------------- #
    #  检测与渲染
    # ---------------------------------------------------------------- #
    def _on_toolenv_refresh(self):
        """Button handler: drop caches first, then re-detect."""
        self._refresh_toolenv_status(refresh=True)

    def _refresh_toolenv_status(self, refresh: bool = False):
        """Detect R, WSL and every catalogued tool, then redraw the block."""
        if not hasattr(self, "table_toolenv"):
            return

        from src.core.bioenv import catalog, paths
        from src.core.bioenv import help as help_mod
        from src.core.bioenv import resolver
        from src.core.bioenv import wsl
        from src.core.r_engine import get_r_engine
        from src.core.toolchain import micromamba as mamba

        tm = ThemeManager()
        try:
            if refresh:
                resolver.clear_cache()
                wsl.invalidate()
            cat = catalog.default(refresh=refresh)
            platform = mamba.runtime_platform()
            ws = wsl.detect(refresh=refresh)
            locations = resolver.resolve_all(cat, platform, refresh=refresh)
            r_info = get_r_engine().detect()
        except Exception as e:
            # 检测失败不能让设置页整体炸掉：REST 之外的区块还要能用。
            logger.exception("External tool detection failed: %s", e)
            self.lbl_toolenv_status.setText(
                f"<span style='color:{tm.color('warning')}'>"
                f"{tr('Detection failed:')} {e}</span>")
            return

        reachable = cat.for_reachable(platform)
        missing = [entry.name for entry in reachable if entry.name not in locations]
        help_cached = set(help_mod.cached_tools())

        r_ok = bool(r_info.get("available"))
        r_text = (tr("available") + f" {r_info.get('version', '')}"
                  if r_ok else tr("not detected"))
        r_color = tm.color("success") if r_ok else tm.color("warning")

        wsl_color = tm.color("success") if ws.usable else tm.color("text_muted")
        tools_color = tm.color("success") if not missing else tm.color("warning")

        self.lbl_toolenv_status.setText(
            f"<div style='line-height:1.7;'>"
            f"<b>{tr('Platform:')}</b> {platform}<br>"
            f"<b>R:</b> <span style='color:{r_color}'>{r_text}</span>"
            f" &nbsp;<span style='color:{tm.color('text_muted')}'>"
            f"{r_info.get('executable', '')}</span><br>"
            f"<b>WSL:</b> <span style='color:{wsl_color}'>{wsl.describe(ws)}</span><br>"
            f"<b>{tr('Tools:')}</b> <span style='color:{tools_color}'>"
            f"{len(locations)}/{len(reachable)}</span> {tr('available')}"
            + (f" &nbsp;<span style='color:{tm.color('text_muted')}'>"
               f"{tr('missing:')} {', '.join(missing)}</span>" if missing else "")
            + "</div>")

        self.table_toolenv.setRowCount(len(reachable))
        for row, entry in enumerate(reachable):
            location = locations.get(entry.name)
            status_text = tr("Available") if location else tr("Unavailable")
            status_color = tm.color("success") if location else tm.color("warning")
            help_lines = help_mod.total_lines(entry.name) if entry.name in help_cached else 0
            cells = [
                entry.name,
                status_text,
                location.source if location else "-",
                (location.version if location else "") or "-",
                f"{help_lines} lines" if help_lines else "-",
                (location.path if location else
                 resolver.unavailable_reason(entry, platform)),
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if column == 1:
                    item.setForeground(self._toolenv_brush(status_color))
                if column == 5:
                    item.setToolTip(str(text))
                self.table_toolenv.setItem(row, column, item)
        self.table_toolenv.resizeColumnsToContents()

    @staticmethod
    def _toolenv_brush(color: str):
        """Build a foreground brush from a theme colour (hex or rgba)."""
        from PySide6.QtGui import QBrush, QColor

        brush = QBrush(QColor(color))
        return brush

    # ---------------------------------------------------------------- #
    #  动作
    # ---------------------------------------------------------------- #
    def _on_toolenv_open_folder(self):
        """Reveal the private environment directory in the system file manager."""
        from src.core.bioenv import paths
        from src.core.platform_env import reveal_in_file_manager
        from src.ui.components.toast import ToastManager

        paths.ensure_dirs()
        target = paths.env_root()
        if not reveal_in_file_manager(target):
            ToastManager().show(f"{tr('Cannot open:')} {target}", "error")

    def _on_toolenv_copy_command(self):
        """Copy a ready-to-run install command for the private prefix.

        应用自己不执行下载：这一步会拉取数百 MB 并求解依赖，交给用户在自己的终端里
        跑，既能看清输出，也能中途取消。
        """
        from PySide6.QtGui import QGuiApplication

        from src.core.bioenv import catalog
        from src.ui.components.toast import ToastManager

        cat = catalog.default()
        prefix_name = next(iter(cat.prefix_names()), "core")
        packages = " ".join(cat.prefix_packages(prefix_name))
        command = (
            "python -c \"from src.core.bioenv import catalog, prefix; "
            f"c=catalog.load(); p='{prefix_name}'; "
            "print(prefix.create(p, c.prefix_packages(p), channels=c.channels, "
            "tools=c.tools_of(p), log_cb=print))\""
        )
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(command)
        ToastManager().show(
            f"{tr('Install command copied:')} {prefix_name} <- {packages}", "info")
        logger.info("Install command copied for prefix '%s'", prefix_name)
