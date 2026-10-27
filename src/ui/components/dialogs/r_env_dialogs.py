"""
R Environment Dialogs
=====================
承载"备好 R 环境"这件事的模态弹窗：装包装到私有库，或下载私有 R 运行时。

分工刻意分开——:class:`~src.task.r_env_task.REnvSetupTask` 只负责干活并回传
日志 / 进度 / 结果，本模块只负责显示与取消，两边都不承担对方的职责：

* 装包与下载都要几分钟，且**编译输出是判断卡住与否的唯一线索**，因此逐行日志
  必须可见（并且完整日志另存文件，弹窗里给出路径）；
* 长任务必须能取消，取消后由任务侧清理残留的 ``00LOCK-*``；
* 弹窗是模态的：环境正在被改写时不允许并发出图，避免读到装了一半的包
  （R 装包期间同名目录被替换，此时启动的绘图进程会直接报包损坏）。
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QLabel, QPlainTextEdit, QProgressBar, QSizePolicy,
)

from src.core.core_task import TaskManager, TaskMode, TaskState
from src.ui.components.dialogs.base import BaseDialog

logger = logging.getLogger("UI.REnvDialog")

#: 日志视图保留的最大行数：编译输出可能上万行，超出部分留在日志文件里。
_LOG_BLOCK_LIMIT = 5000


class REnvSetupDialog(BaseDialog):
    """R 环境设置的进度与日志弹窗。

    :param action:   ``"packages"`` 或 ``"runtime"``，透传给任务。
    :param packages: 目标包清单。
    :param repo:     用户指定的包源（可为空，此时按可达性自动选择）。
    """

    #: 任务结束（无论成败）后发出，携带任务结果 payload；调用方据此刷新界面。
    sig_finished = Signal(dict)

    def __init__(self, parent=None, *, action: str = "packages",
                 packages: Optional[List[str]] = None, repo: str = ""):
        is_runtime = action == "runtime"
        title = "Download private R runtime" if is_runtime else "Install R packages"
        super().__init__(parent, title=title, width=760)
        self.setWindowModality(Qt.ApplicationModal)

        self._action = action
        self._packages = list(packages or [])
        self._repo = repo
        self._manager = TaskManager()
        self._running = False
        self._finished = False
        self._cancelling = False
        self._result: Dict = {}

        # --- 说明 ---
        self.lbl_hint = QLabel(self._hint_text())
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.content_layout.addWidget(self.lbl_hint)

        # --- 状态行 ---
        self.lbl_status = QLabel("Starting...")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.content_layout.addWidget(self.lbl_status)

        # --- 进度 ---
        self.pbar = QProgressBar()
        self.pbar.setFixedHeight(18)
        self.pbar.setRange(0, 100)
        self.pbar.setValue(0)
        self.pbar.setAlignment(Qt.AlignCenter)
        self.content_layout.addWidget(self.pbar)

        # --- R 日志 ---
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log_view.setMaximumBlockCount(_LOG_BLOCK_LIMIT)
        self.log_view.setMinimumHeight(280)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.content_layout.addWidget(self.log_view, 1)

        self.btn_cancel = self.add_button("Cancel Task", self._on_cancel, is_danger=True)
        self.btn_close = self.add_button("Close", self._on_close, is_primary=True)
        self.btn_close.setEnabled(False)

        self._manager.sig_log.connect(self._on_log)
        self._manager.sig_progress.connect(self._on_progress)
        self._manager.sig_state_changed.connect(self._on_state)
        self._manager.sig_result.connect(self._on_result)

        self._apply_theme()

    # ------------------------------------------------------------------ #
    def _hint_text(self) -> str:
        from src.core import r_env

        if self._action == "runtime":
            text = ("A self-contained R runtime will be downloaded into the application's "
                    "private directory. It ships prebuilt packages, so no compiler and no "
                    "administrator rights are required.")
            # 私有运行时是**可选**动作：本机已有可用 R 时，装包走私有库就够，下载只是
            # 为了让绘图与本机 R 完全解耦。不说明这一点，用户会以为自己点错了。
            try:
                from src.core.r_engine import get_r_engine

                info = get_r_engine().detect()
                if info.get("available"):
                    text += (f"\n\nSystem R is already available "
                             f"({info.get('version')}); downloading is optional. Use it when "
                             f"you want plotting to stay independent of that installation.")
                else:
                    text += ("\n\nNo usable R was found on this system, so this is how the "
                             "visualization engine gets an interpreter.")
            except Exception as e:
                logger.debug(f"System R status unavailable for the runtime hint: {e}")
            return text
        return (f"Missing packages will be installed into the application's private library "
                f"({r_env.library_dir()}). Your system R is not modified; removing that "
                f"directory reverts everything.")

    def showEvent(self, event):
        super().showEvent(event)
        if not self._running and not self._result:
            self._start_task()

    def _start_task(self):
        self._running = True
        self.btn_close.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.pbar.setValue(0)
        logger.info(f"R environment dialog starting task: action={self._action} "
                    f"packages={len(self._packages)}")
        self._manager.start_task(
            task_class=_task_class(),
            task_id=f"r_env_{self._action}",
            mode=TaskMode.THREAD,
            action=self._action,
            packages=self._packages,
            repo=self._repo,
        )

    # ------------------------------------------------------------------ #
    #  任务信号
    # ------------------------------------------------------------------ #
    def _on_log(self, level: str, message: str):
        stamp = time.strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{stamp}] {level:<7} {message}")
        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum())

    def _on_progress(self, percent: int, message: str):
        if percent > 0:
            self.pbar.setValue(max(self.pbar.value(), min(100, int(percent))))
        if message:
            self.lbl_status.setText(message)

    def _on_state(self, state: str, message: str):
        logger.info(f"R environment task state: {state} {message}")
        if state == TaskState.FAILED.value:
            self.lbl_status.setText(message or "Task failed")
            self._finish()

    def _on_result(self, payload):
        self._result = payload if isinstance(payload, dict) else {"ok": False}
        summary = str(self._result.get("summary") or "")
        if summary:
            self.lbl_status.setText(summary)
        self.pbar.setValue(100 if self._result.get("ok") else self.pbar.value())
        self._finish()

    def _finish(self):
        """收敛到终态：只生效一次。

        ``sig_result`` 与 ``sig_state_changed`` 会先后到达（失败路径两者都有、
        正常路径结果先到），不去重会让 :attr:`sig_finished` 发两遍、调用方连着
        刷新两次界面。
        """
        if self._finished:
            return
        self._finished = True
        self._running = False
        self.btn_cancel.setEnabled(False)
        self.btn_close.setEnabled(True)
        self.sig_finished.emit(dict(self._result))

    # ------------------------------------------------------------------ #
    #  用户操作
    # ------------------------------------------------------------------ #
    def _on_cancel(self):
        """请求取消，并允许用户在确认任务停下后关闭窗口。

        :meth:`TaskManager.cancel_task` 会停掉队列轮询，因此取消之后不会再收到
        任务的终态消息；若此时仍禁用关闭按钮，用户会被困在一个模态窗口里。
        取消后放行关闭按钮，并说明关闭时机——环境改写由任务侧负责收尾。
        """
        if not self._running:
            return
        self._cancelling = True
        self.lbl_status.setText(
            "Cancelling... the R task stops within a few seconds; close this window "
            "once the log stops.")
        self.btn_cancel.setEnabled(False)
        self.btn_close.setEnabled(True)
        self._manager.cancel_task()
        logger.info("R environment task cancelled by user")

    def _on_close(self):
        self.accept()

    def closeEvent(self, event):
        if self._running and not self._cancelling:
            # 任务仍在改写私有库：关掉窗口会让用户误以为已经结束。
            event.ignore()
            self.lbl_status.setText("Cancel the task before closing this window.")
            return
        super().closeEvent(event)

    # ------------------------------------------------------------------ #
    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm
        # BaseDialog 的样式表覆盖 QTextEdit 但未覆盖 QPlainTextEdit，日志区单独上色。
        self.log_view.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {tm.color('bg_input')};
                color: {tm.color('text_main')};
                border: 1px solid {tm.color('border')};
                border-radius: 4px;
                padding: 6px;
                selection-background-color: {tm.color('accent')};
                selection-color: {tm.color('selection_fg')};
            }}
        """)
        self.lbl_hint.setStyleSheet(f"color: {tm.color('text_muted')};")


def _task_class():
    """延迟取任务类：避免 UI 模块在导入期就把装包相关依赖全拉进来。"""
    from src.task.r_env_task import REnvSetupTask

    return REnvSetupTask