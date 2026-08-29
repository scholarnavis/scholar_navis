"""Common dialogs: message, progress, unsaved-changes and password prompts."""
import os
import re
import time

import psutil
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QLabel, QFrame, QHBoxLayout, QLineEdit, QProgressBar,
                               QScrollArea, QSizePolicy, QWidget)

from src.ui.components.dialogs.base import BaseDialog
from src.ui.components.toast import ToastManager

__all__ = [
    "StandardDialog", "ProgressDialog", "UnsavedChangesDialog",
    "ExportPasswordDialog", "ImportPasswordDialog", "AddModelDialog",
]


class StandardDialog(BaseDialog):
    def __init__(self, parent=None, title="Notification", message="", show_cancel=False, width=420):
        super().__init__(parent, title=title, width=width)

        self.is_long_text = len(message) > 300 or message.count('\n') > 8

        self.msg_label = QLabel(message)
        self.msg_label.setWordWrap(True)
        self.msg_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.msg_label.setTextInteractionFlags(Qt.TextBrowserInteraction)

        if self.is_long_text:
            self.scroll_area = QScrollArea()
            self.scroll_area.setWidgetResizable(True)
            self.scroll_area.setFrameShape(QFrame.NoFrame)
            self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

            self.scroll_area.setWidget(self.msg_label)
            self.scroll_area.setMaximumHeight(350)
            self.scroll_area.setMinimumHeight(200)
            self.content_layout.addWidget(self.scroll_area)
        else:
            self.content_layout.addWidget(self.msg_label)

        if show_cancel:
            self.add_button("Cancel", self.reject)
        self.add_button("OK", self.accept, is_primary=True)

        self._apply_theme()

    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm
        self.msg_label.setStyleSheet(
            f"color: {tm.color('text_main')}; background-color: transparent; font-size: 14px; padding: 5px; border: none;"
        )
        if self.is_long_text:
            self.scroll_area.viewport().setStyleSheet("background-color: transparent;")


class ProgressDialog(BaseDialog):
    sig_canceled = Signal()

    def __init__(self, parent=None, title="Processing", message="Please wait...", telemetry_config=None):
        super().__init__(parent, title=title, width=540)
        self.setWindowModality(Qt.ApplicationModal)

        self.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)

        # --- UI 初始化 ---
        self.lbl_message = QLabel(message)
        self.lbl_message.setWordWrap(True)
        self.content_layout.addWidget(self.lbl_message)

        self.pbar = QProgressBar()
        self.pbar.setFixedHeight(18)
        self.pbar.setRange(0, 0)
        self.pbar.setAlignment(Qt.AlignCenter)
        self.pbar.setTextVisible(True)
        self.content_layout.addWidget(self.pbar)

        self.stalled_warning_widget = QWidget()
        warn_layout = QHBoxLayout(self.stalled_warning_widget)
        warn_layout.setContentsMargins(5, 5, 5, 5)
        self.lbl_warn_icon = QLabel()
        self.lbl_warn_icon.setFixedSize(16, 16)
        self.lbl_warn_text = QLabel(
            "Task is still in progress. Large models or network latency may take extra time, please wait...")
        self.lbl_warn_text.setWordWrap(True)
        self.lbl_warn_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.lbl_warn_text.setMinimumHeight(32)
        warn_layout.addWidget(self.lbl_warn_icon, 0, Qt.AlignTop)
        warn_layout.addWidget(self.lbl_warn_text, 1)

        self.content_layout.addStretch()

        self.btn_cancel = self.add_button("Cancel Task", self.on_cancel_clicked, is_danger=True)

        self._apply_theme()
        self.adjustSize()

        self._last_progress = -1
        self._last_progress_time = time.time()
        self.stall_timer = QTimer(self)
        self.stall_timer.timeout.connect(self._check_stalled_progress)
        self.stall_timer.start(2000)

        self.main_process = psutil.Process(os.getpid())

        self.main_process.cpu_percent(interval=None)

    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm
        self.lbl_message.setStyleSheet(
            f"font-size: 13px; color: {tm.color('text_main')}; margin-bottom: 5px; border: none;")

        self.pbar.setStyleSheet(f"""
            QProgressBar {{
                border: 1px solid {tm.color('border')};
                background-color: {tm.color('bg_input')};
                border-radius: 4px;
                color: {tm.color('text_main')};
                font-weight: bold;
                font-size: 11px;
                text-align: center;
            }}
            QProgressBar::chunk {{ background-color: {tm.color('accent')}; border-radius: 3px; }}
        """)

        self.lbl_warn_icon.setPixmap(tm.icon("info", "warning").pixmap(16, 16))

        self.lbl_warn_icon.setStyleSheet("border: none; background: transparent;")

        self.lbl_warn_text.setStyleSheet(
            f"color: {tm.color('warning')}; font-size: 12px; font-weight: bold; border: none; background: transparent;")

        self.stalled_warning_widget.setObjectName("StallWarningBox")
        self.stalled_warning_widget.setStyleSheet(
            f"QWidget#StallWarningBox {{ background-color: {tm.color('bg_input')}; border: 1px dashed {tm.color('warning')}; border-radius: 4px; }}")


    def _format_speed(self, bytes_per_sec):
        if bytes_per_sec >= 1024 * 1024:
            return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
        elif bytes_per_sec >= 1024:
            return f"{bytes_per_sec / 1024:.0f} KB/s"
        else:
            return f"{bytes_per_sec:.0f} B/s"

    def _get_process_tree(self):
        try:
            return [self.main_process] + self.main_process.children(recursive=True)
        except psutil.NoSuchProcess:
            return [self.main_process]

    def _check_stalled_progress(self):
        if time.time() - self._last_progress_time > 120:
            if not self.stalled_warning_widget.isVisible() and self.pbar.isVisible():
                self.stalled_warning_widget.setVisible(True)

    def _get_process_tree_io(self):
        read_bytes, write_bytes = 0, 0
        for p in self._get_process_tree():
            try:
                io = p.io_counters()
                read_bytes += io.read_bytes
                write_bytes += io.write_bytes
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                pass
        return read_bytes, write_bytes

    def update_progress(self, percent, msg=None):
        if percent != self._last_progress:
            self._last_progress = percent
            self._last_progress_time = time.time()
            if self.stalled_warning_widget.isVisible():
                self.stalled_warning_widget.setVisible(False)

        if percent < 0:
            if self.pbar.maximum() != 0:
                self.pbar.setRange(0, 0)
                self.pbar.setTextVisible(False)
        else:
            if self.pbar.maximum() == 0:
                self.pbar.setRange(0, 100)
                self.pbar.setTextVisible(True)
            self.pbar.setValue(percent)
        if msg: self.lbl_message.setText(msg)

    def show_success_state(self, title="Success", message="Task completed successfully."):
        if hasattr(self, 'metric_timer'): self.metric_timer.stop()
        if hasattr(self, 'stall_timer'): self.stall_timer.stop()
        self.stalled_warning_widget.setVisible(False)

        self.pbar.setVisible(False)

        self.setWindowTitle(title)

        self.setWindowFlags(self.windowFlags() | Qt.WindowCloseButtonHint)
        self.show()

        self.lbl_message.setText(message)
        self.btn_cancel.setText("OK")
        self.btn_cancel.setEnabled(True)

        tm = self.tm
        self.btn_cancel.setStyleSheet(f"""
            QPushButton {{ background-color: {tm.color('accent')}; color: {tm.color('bg_main')}; border-radius: 4px; border: none; font-weight:bold;}}
            QPushButton:hover {{ background-color: {tm.color('accent_hover')}; }}
        """)

        try:
            self.btn_cancel.clicked.disconnect()
        except:
            pass
        self.btn_cancel.clicked.connect(self.accept)

    def on_cancel_clicked(self):
        # 1. 更新 UI 状态，隐藏取消按钮，提示用户等待
        self.lbl_message.setText("Cancelling... waiting for background task to safely terminate.")
        self.btn_cancel.setVisible(False)
        self.stalled_warning_widget.setVisible(False)

        # 2. 停止本地的性能监控器
        if hasattr(self, 'metric_timer'): self.metric_timer.stop()
        if hasattr(self, 'stall_timer'): self.stall_timer.stop()

        # 3. 发送取消请求给 TaskManager
        self.sig_canceled.emit()

    def close_safe(self):
        if hasattr(self, 'metric_timer'): self.metric_timer.stop()
        if hasattr(self, 'stall_timer'): self.stall_timer.stop()
        self.accept()

    def closeEvent(self, event):
        if hasattr(self, 'metric_timer'): self.metric_timer.stop()
        if hasattr(self, 'stall_timer'): self.stall_timer.stop()
        super().closeEvent(event)

    def show_finish_state(self, success: bool, title: str, message: str):
        # 1. 清理监控器与隐藏旧 UI
        if hasattr(self, 'metric_timer'): self.metric_timer.stop()
        if hasattr(self, 'stall_timer'): self.stall_timer.stop()

        self.stalled_warning_widget.setVisible(False)
        self.pbar.setVisible(False)

        # 2. 隐藏自身的弹窗界面，转而使用 StandardDialog 告知最终结果
        self.hide()

        # 3. 弹出最终结果对话框
        result_dialog = StandardDialog(
            self.parent(),
            title=title,
            message=message,
            show_cancel=False
        )
        result_dialog.exec()

        self.accept()


class UnsavedChangesDialog(BaseDialog):
    def __init__(self, parent):
        super().__init__(parent, title="Unsaved Modifications", width=460)
        self.user_choice = "close"  # Default fallback action

        # Configure the message label
        msg_label = QLabel(
            "You have unsaved configuration changes.\n"
            "Please specify how you would like to proceed before navigating away:"
        )
        msg_label.setWordWrap(True)
        msg_label.setStyleSheet(
            f"color: {self.tm.color('text_main')}; font-size: 14px; border: none; background: transparent;"
        )
        self.content_layout.addWidget(msg_label)

        # Callback generator for buttons
        def set_choice(action, accept=False):
            self.user_choice = action
            self.accept() if accept else self.reject()

        # Construct Footer Buttons
        btn_close = self.add_button("Close", lambda: set_choice("close"))
        btn_revert = self.add_button("Revert Changes", lambda: set_choice("revert"), is_danger=True)
        btn_save = self.add_button("Save Settings", lambda: set_choice("save", True), is_primary=True)

        btn_close.setFixedWidth(80)
        btn_revert.setFixedWidth(130)
        btn_save.setFixedWidth(130)


class ExportPasswordDialog(BaseDialog):
    def __init__(self, parent):
        super().__init__(parent, title="Export Security", width=420)
        self.password = None
        self.is_cancelled = True
        self.regex = re.compile(r'^[a-zA-Z0-9@_\-+=!#$&^*]+$')

        lbl = QLabel(
            "Set a password to encrypt the exported configuration.\nLeave completely blank for an unencrypted JSON export.")
        lbl.setWordWrap(True)
        lbl.setStyleSheet(
            f"color: {self.tm.color('text_main')}; font-size: 13px; border: none; background: transparent;")

        self.inp_pass = QLineEdit()
        self.inp_pass.setEchoMode(QLineEdit.Password)
        self.inp_pass.setPlaceholderText("Min 6 chars (a-zA-Z0-9@_-+=!#$&^*)")

        self.content_layout.addWidget(lbl)
        self.content_layout.addWidget(self.inp_pass)

        btn_cancel = self.add_button("Cancel", self.reject)
        btn_confirm = self.add_button("Confirm", self._validate, is_primary=True)
        btn_cancel.setFixedWidth(100)
        btn_confirm.setFixedWidth(100)

    def _validate(self):
        pwd = self.inp_pass.text()
        if not pwd:
            self.is_cancelled = False
            self.accept()
            return

        if len(pwd) < 6 or not self.regex.match(pwd):
            ToastManager().show("Invalid password! Min 6 chars. Allowed: a-zA-Z0-9@_-+=!#$&^*", "error")
            return

        self.password = pwd
        self.is_cancelled = False
        self.accept()


class ImportPasswordDialog(BaseDialog):
    def __init__(self, parent):
        super().__init__(parent, title="Encrypted Bundle Detected", width=420)
        self.password = None
        self.is_cancelled = True

        lbl = QLabel("This configuration is encrypted.\nPlease enter the password to unlock:")
        lbl.setWordWrap(True)
        lbl.setStyleSheet(
            f"color: {self.tm.color('text_main')}; font-size: 13px; border: none; background: transparent;")

        self.inp_pass = QLineEdit()
        self.inp_pass.setEchoMode(QLineEdit.Password)
        self.inp_pass.setPlaceholderText("Enter decryption password...")

        self.content_layout.addWidget(lbl)
        self.content_layout.addWidget(self.inp_pass)

        btn_cancel = self.add_button("Cancel", self.reject)
        btn_confirm = self.add_button("Confirm", self._validate, is_primary=True)
        btn_cancel.setFixedWidth(100)
        btn_confirm.setFixedWidth(100)

    def _validate(self):
        pwd = self.inp_pass.text()
        if not pwd:
            from src.ui.components.toast import ToastManager
            ToastManager().show("Password cannot be empty.", "error")
            return
        self.password = pwd
        self.is_cancelled = False
        self.accept()


class AddModelDialog(BaseDialog):
    def __init__(self, parent=None):
        super().__init__(parent, title="Add Custom Model", width=350)
        self.inp_name = QLineEdit()
        self.inp_name.setPlaceholderText("Enter model ID/name...")
        self.content_layout.addWidget(self.inp_name)

        self.add_button("Cancel", self.reject)
        self.add_button("Add", self.accept, is_primary=True)

        self._apply_theme()

    def get_name(self):
        return self.inp_name.text().strip()
