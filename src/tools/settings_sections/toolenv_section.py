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

from src.core.core_task import TaskManager, TaskMode, TaskState
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
        """在后台线程里检测 R / WSL / 生信工具，完成后回主线程渲染。

        检测必须离开界面线程：Windows 上没有本机构建的每个工具都要经 ``wsl.exe``
        探测，发行版冷启动时单次可超过十秒。这里只负责发起任务与置灰按钮，结果由
        :meth:`_render_toolenv` 处理（见 :class:`src.task.settings_tasks.ToolEnvDetectTask`）。
        """
        if not hasattr(self, "table_toolenv"):
            return

        from src.task.settings_tasks import ToolEnvDetectTask

        if getattr(self, "_toolenv_task_mgr", None) is None:
            self._toolenv_task_mgr = TaskManager()
            self._toolenv_task_mgr.sig_result.connect(self._on_toolenv_result)
            self._toolenv_task_mgr.sig_state_changed.connect(self._on_toolenv_state)

        self.btn_toolenv_refresh.setEnabled(False)
        self.btn_toolenv_refresh.setText(tr("Detecting..."))
        self.lbl_toolenv_status.setText(tr("Detecting external tools..."))
        logger.info("External tool detection started (refresh=%s)", refresh)
        self._toolenv_task_mgr.start_task(
            ToolEnvDetectTask, task_id="toolenv_detect",
            mode=TaskMode.THREAD, refresh=refresh)

    def _on_toolenv_state(self, state: str, msg: str):
        """任务终态：恢复按钮；失败时把原因写进状态行（表格保持上一次的结果）。"""
        self.btn_toolenv_refresh.setEnabled(True)
        self.btn_toolenv_refresh.setText(tr("Re-detect"))
        if state != TaskState.SUCCESS.value:
            tm = ThemeManager()
            reason = str(msg or "").strip() or tr("unknown error")
            logger.warning("External tool detection ended as %s: %s", state, reason)
            self.lbl_toolenv_status.setText(
                f"<span style='color:{tm.color('warning')}'>"
                f"{tr('Detection failed:')} {reason}</span>")

    def _on_toolenv_result(self, result):
        """后台检测结果回主线程渲染（结构见 ToolEnvDetectTask._execute 的返回值）。"""
        try:
            self._render_toolenv(result or {})
        except Exception as e:
            logger.exception("Rendering the external tool table failed: %s", e)
            tm = ThemeManager()
            self.lbl_toolenv_status.setText(
                f"<span style='color:{tm.color('warning')}'>"
                f"{tr('Detection failed:')} {e}</span>")

    def _render_toolenv(self, result):
        """把一份检测结果画到状态行与表格上。"""
        tm = ThemeManager()
        platform = str(result.get("platform") or "")
        rows = list(result.get("tools") or [])
        total = int(result.get("total") or len(rows))
        resolved = int(result.get("resolved") or 0)
        missing = [row.get("name", "") for row in rows if not row.get("available")]

        r_info = result.get("r") or {}
        r_ok = bool(r_info.get("available"))
        r_text = (tr("available") + f" {r_info.get('version', '')}"
                  if r_ok else tr("not detected"))
        r_color = tm.color("success") if r_ok else tm.color("warning")

        wsl_usable = bool(result.get("wsl_usable"))
        wsl_color = (tm.color("success") if wsl_usable
                     else (tm.color("text_muted") if not result.get("wsl_applicable")
                           else tm.color("warning")))
        tools_color = tm.color("success") if not missing else tm.color("warning")

        self.lbl_toolenv_status.setText(
            f"<div style='line-height:1.7;'>"
            f"<b>{tr('Platform:')}</b> {platform}<br>"
            f"<b>R:</b> <span style='color:{r_color}'>{r_text}</span>"
            f" &nbsp;<span style='color:{tm.color('text_muted')}'>"
            f"{r_info.get('executable', '')}</span><br>"
            f"<b>WSL:</b> <span style='color:{wsl_color}'>{result.get('wsl', '')}</span><br>"
            f"<b>{tr('Tools:')}</b> <span style='color:{tools_color}'>"
            f"{resolved}/{total}</span> {tr('available')}"
            + (f" &nbsp;<span style='color:{tm.color('text_muted')}'>"
               f"{tr('missing:')} {', '.join(missing)}</span>" if missing else "")
            + "</div>")

        self.table_toolenv.setRowCount(len(rows))
        for index, row in enumerate(rows):
            available = bool(row.get("available"))
            status_text = tr("Available") if available else tr("Unavailable")
            status_color = tm.color("success") if available else tm.color("warning")
            source = str(row.get("source") or "")
            distro = str(row.get("distro") or "")
            help_lines = int(row.get("help_lines") or 0)
            cells = [
                row.get("name", ""),
                status_text,
                # 来源列：WSL 落点附上发行版名，否则"wsl"这一个词无法回答"用的是
                # 哪个 Linux 环境"。
                (f"{source} ({distro})" if distro else (source or "-")),
                str(row.get("version") or "") or "-",
                f"{help_lines} lines" if help_lines else "-",
                str(row.get("path") or "") or str(row.get("reason") or ""),
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if column == 1:
                    item.setForeground(self._toolenv_brush(status_color))
                if column == 5:
                    item.setToolTip(str(text))
                self.table_toolenv.setItem(index, column, item)
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

        两种平台走两条路，判定依据只有一处（
        :meth:`src.core.bioenv.catalog.Catalog.installable_packages`）：

        * 本平台有 conda 构建 → 复制模块入口命令。用 ``python -m`` 而不是
          ``python -c "..."``：后者的引号转义在 cmd / PowerShell / bash 里各不相同，
          复制出去经常跑不起来；
        * 本平台没有（Windows 的常态）→ 复制在 WSL 发行版里装这几包的命令。让用户
          去跑一条注定求解失败的命令，等于把失败留给下一次询问。
        """
        from PySide6.QtGui import QGuiApplication

        from src.core.bioenv import catalog, wsl
        from src.core.toolchain import micromamba as mamba
        from src.ui.components.toast import ToastManager

        cat = catalog.default()
        prefix_name = next(iter(cat.prefix_names()), "core")
        platform = mamba.runtime_platform()
        packages = cat.installable_packages(prefix_name, platform)

        if packages:
            command = f"python -m src.core.bioenv install {prefix_name}"
            toast = (f"{tr('Install command copied:')} "
                     f"{prefix_name} <- {' '.join(packages)}")
            level = "info"
        else:
            declared = " ".join(cat.prefix_packages(prefix_name))
            status = wsl.detect()
            if status.usable:
                command = (f"wsl -d {status.distro} -- bash -lc "
                           f"\"conda install -y -c bioconda -c conda-forge {declared}\"")
                toast = tr("No build for this platform; the command installs the "
                           "packages inside WSL.")
                level = "info"
            else:
                command = ""
                toast = f"{tr('No build for this platform.')} {wsl.describe(status)}"
                level = "warning"

        clipboard = QGuiApplication.clipboard()
        if clipboard is not None and command:
            clipboard.setText(command)
        ToastManager().show(toast, level)
        logger.info("Install command copied for prefix '%s' on %s (platform build: %s)",
                    prefix_name, platform, bool(packages))
