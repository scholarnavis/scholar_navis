import os
import time
import psutil
import torch
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QWidget, QFrame, QFormLayout,
                               QLineEdit, QTextEdit, QComboBox, QProgressBar,
                               QSizePolicy, QGraphicsDropShadowEffect)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QColor

from src.core.models_registry import EMBEDDING_MODELS



class BaseDialog(QDialog):
    def __init__(self, parent=None, title="Dialog", width=450):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(width)
        self._drag_pos = None

        self.main_frame = QFrame(self)
        self.main_frame.setObjectName("MainFrame")
        self.main_frame.setStyleSheet("""
            QFrame#MainFrame {
                background-color: #1e1e1e;
                border: 1px solid #444;
                border-radius: 6px;
            }
        """)

        # 阴影
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(15)
        shadow.setXOffset(0)
        shadow.setYOffset(4)
        shadow.setColor(QColor(0, 0, 0, 80))
        self.main_frame.setGraphicsEffect(shadow)

        self.v_layout = QVBoxLayout(self.main_frame)
        self.v_layout.setContentsMargins(0, 0, 0, 0)
        self.v_layout.setSpacing(0)

        # --- 标题栏 ---
        self.title_bar = QWidget()
        self.title_bar.setFixedHeight(40)
        self.title_bar.setStyleSheet("""
            background-color: #252526; 
            border-top-left-radius: 6px; 
            border-top-right-radius: 6px; 
            border-bottom: 1px solid #333;
        """)
        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(15, 0, 10, 0)

        self.lbl_title = QLabel(title)
        self.lbl_title.setStyleSheet(
            "color: #e0e0e0; font-weight: bold; font-family: 'Segoe UI'; font-size: 13px; border: none;")
        title_layout.addWidget(self.lbl_title)
        title_layout.addStretch()

        self.btn_close = QPushButton("✕")
        self.btn_close.setFixedSize(30, 30)
        self.btn_close.clicked.connect(self.reject)
        self.btn_close.setStyleSheet("""
            QPushButton { border: none; color: #888; background: transparent; font-weight: bold; font-size: 14px; }
            QPushButton:hover { color: #fff; background-color: #c42b1c; border-radius: 4px; }
        """)
        title_layout.addWidget(self.btn_close)
        self.v_layout.addWidget(self.title_bar)

        # --- 内容区 ---
        self.content_widget = QWidget()
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(20, 20, 20, 20)
        self.content_layout.setSpacing(15)
        self.v_layout.addWidget(self.content_widget, 1)

        # --- 底部按钮区 ---
        self.footer_widget = QWidget()
        self.footer_widget.setFixedHeight(55)
        self.footer_widget.setStyleSheet("""
            background-color: #252526; 
            border-bottom-left-radius: 6px; 
            border-bottom-right-radius: 6px; 
            border-top: 1px solid #333;
        """)
        self.footer_layout = QHBoxLayout(self.footer_widget)
        self.footer_layout.setContentsMargins(15, 0, 15, 0)
        self.footer_layout.addStretch()
        self.v_layout.addWidget(self.footer_widget)

        window_layout = QVBoxLayout(self)
        window_layout.setContentsMargins(10, 10, 10, 10)
        window_layout.addWidget(self.main_frame)

        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.MinimumExpanding)

    def add_button(self, text, callback, is_primary=False, is_danger=False):
        btn = QPushButton(text)
        btn.setFixedSize(90, 32)
        btn.setCursor(Qt.PointingHandCursor)

        if callback:
            btn.clicked.connect(lambda *args, cb=callback: cb())

        base_style = "QPushButton { border-radius: 4px; font-family: 'Segoe UI'; font-size: 13px; font-weight: 500; }"

        if is_primary:
            style = base_style + """
                QPushButton { background-color: #007acc; color: white; border: 1px solid #007acc; }
                QPushButton:hover { background-color: #0062a3; }
                QPushButton:pressed { background-color: #005a9e; }
            """
        elif is_danger:
            style = base_style + """
                QPushButton { background-color: #2d2d30; color: #ff6b6b; border: 1px solid #ff6b6b; }
                QPushButton:hover { background-color: #ff6b6b; color: white; }
            """
        else:
            style = base_style + """
                QPushButton { background-color: #3e3e42; color: #cccccc; border: 1px solid #555; }
                QPushButton:hover { background-color: #4e4e52; }
            """

        btn.setStyleSheet(style)
        self.footer_layout.addWidget(btn)
        return btn

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            global_pos = event.globalPosition().toPoint()
            if self.title_bar.geometry().contains(self.main_frame.mapFromGlobal(global_pos)):
                self._drag_pos = global_pos - self.frameGeometry().topLeft()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self._drag_pos:
            global_pos = event.globalPosition().toPoint()
            self.move(global_pos - self._drag_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)


class StandardDialog(BaseDialog):
    def __init__(self, parent=None, title="Notification", message="", show_cancel=False):
        super().__init__(parent, title=title, width=420)
        msg_label = QLabel(message)
        msg_label.setWordWrap(True)
        msg_label.setStyleSheet("color: #d4d4d4; font-size: 14px; padding: 5px; border: none;")
        self.content_layout.addWidget(msg_label)

        if show_cancel:
            self.add_button("Cancel", self.reject)
        self.add_button("OK", self.accept, is_primary=True)
        self.adjustSize()


# --- 关键修改开始 ---

try:
    import pynvml

    pynvml.nvmlInit()
    HAS_NVML = True
except Exception:
    HAS_NVML = False


class ProgressDialog(BaseDialog):
    sig_canceled = Signal()

    def __init__(self, parent=None, title="Processing", message="Please wait...", telemetry_config=None):
        super().__init__(parent, title=title, width=540)
        self.setWindowModality(Qt.ApplicationModal)
        self.btn_close.setVisible(False)

        if telemetry_config is None:
            self.telemetry = {"cpu": True, "ram": True, "gpu": True, "net": False, "io": True}
        else:
            self.telemetry = telemetry_config

        # --- UI 初始化 ---
        self.lbl_message = QLabel(message)
        self.lbl_message.setWordWrap(True)
        self.lbl_message.setStyleSheet("font-size: 13px; color: #dddddd; margin-bottom: 5px; border: none;")
        self.content_layout.addWidget(self.lbl_message)

        self.pbar = QProgressBar()
        self.pbar.setFixedHeight(18)
        self.pbar.setRange(0, 0)
        self.pbar.setAlignment(Qt.AlignCenter)
        self.pbar.setTextVisible(True)
        self.pbar.setStyleSheet("""
                    QProgressBar { 
                        border: 1px solid #444; 
                        background-color: #1e1e1e; 
                        border-radius: 4px; 
                        color: white; 
                        font-weight: bold; 
                        font-size: 11px; 
                        text-align: center; 
                    }
                    QProgressBar::chunk { background-color: #05B8CC; border-radius: 3px; }
                """)
        self.content_layout.addWidget(self.pbar)

        self.lbl_metrics = QLabel("Initializing App Profiler...")
        self.lbl_metrics.setWordWrap(True)
        self.lbl_metrics.setStyleSheet("""
            QLabel {
                font-family: 'Consolas', 'Courier New', monospace; 
                color: #a5d6a7; font-size: 11px; background-color: #1e1e1e;
                border: 1px solid #333; border-radius: 4px; padding: 6px; margin-top: 5px;
            }
        """)
        self.content_layout.addWidget(self.lbl_metrics)
        self.content_layout.addStretch()

        self.btn_cancel = self.add_button("Cancel Task", self.on_cancel_clicked, is_danger=True)
        self.adjustSize()

        self.main_process = psutil.Process(os.getpid())
        self.main_process.cpu_percent(interval=None)

        if any(self.telemetry.values()):
            self.metric_timer = QTimer(self)
            self.metric_timer.timeout.connect(self._update_metrics)
            self._last_time = time.time()
            if self.telemetry.get("net"):
                try:
                    self._last_net_io = psutil.net_io_counters()
                except:
                    self.telemetry["net"] = False
            if self.telemetry.get("io"):
                try:
                    self._last_disk_io = self._get_process_tree_io()
                except:
                    self.telemetry["io"] = False
            self.metric_timer.start(1000)
        else:
            self.lbl_metrics.setVisible(False)

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

    def _update_metrics(self):
        try:
            stats = []
            curr_time = time.time()
            dt = curr_time - self._last_time
            if dt <= 0: return

            procs = self._get_process_tree()
            pids = [p.pid for p in procs]

            if self.telemetry.get("cpu"):
                app_cpu = 0.0
                for p in procs:
                    try:
                        app_cpu += p.cpu_percent(interval=None)
                    except:
                        pass
                stats.append(f"🖥️ CPU: {app_cpu:04.1f}%")

            if self.telemetry.get("ram"):
                app_ram = 0
                for p in procs:
                    try:
                        app_ram += p.memory_info().rss
                    except:
                        pass
                stats.append(f"RAM: {app_ram / (1024 ** 2):.1f} MB")

            if self.telemetry.get("io"):
                curr_disk_io = self._get_process_tree_io()
                read_spd = (curr_disk_io[0] - self._last_disk_io[0]) / dt
                write_spd = (curr_disk_io[1] - self._last_disk_io[1]) / dt
                stats.append(f"I/O: R:{self._format_speed(read_spd)} W:{self._format_speed(write_spd)}")
                self._last_disk_io = curr_disk_io

            if self.telemetry.get("gpu"):
                if HAS_NVML:
                    try:
                        app_vram_mb = 0
                        sys_used_vram_gb = 0
                        gpu_usage = 0
                        device_count = pynvml.nvmlDeviceGetCount()
                        for i in range(device_count):
                            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                            gpu_usage = max(gpu_usage, util.gpu)
                            mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                            sys_used_vram_gb += mem_info.used / (1024 ** 3)
                            gpu_procs = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
                            for gp in gpu_procs:
                                if gp.pid in pids and gp.usedGpuMemory is not None:
                                    app_vram_mb += gp.usedGpuMemory / (1024 ** 2)
                        if app_vram_mb > 0:
                            stats.append(f"App VRAM: {app_vram_mb:.0f} MB [GPU: {gpu_usage}%]")
                        else:
                            stats.append(f"Sys VRAM: {sys_used_vram_gb:.1f}G [GPU: {gpu_usage}%]")
                    except:
                        stats.append("GPU: Active")
                elif torch.cuda.is_available():
                    try:
                        free, total = torch.cuda.mem_get_info()
                        used_gb = (total - free) / (1024 ** 3)
                        stats.append(f"Sys VRAM: {used_gb:.1f}G")
                    except:
                        stats.append("GPU: Active")
                else:
                    stats.append("GPU: N/A")

            if self.telemetry.get("net"):
                curr_net_io = psutil.net_io_counters()
                recv_spd = (curr_net_io.bytes_recv - self._last_net_io.bytes_recv) / dt
                sent_spd = (curr_net_io.bytes_sent - self._last_net_io.bytes_sent) / dt
                stats.append(f"Sys Net: ↓{self._format_speed(recv_spd)} ↑{self._format_speed(sent_spd)}")
                self._last_net_io = curr_net_io

            self._last_time = curr_time
            if len(stats) > 3:
                self.lbl_metrics.setText(f"{' | '.join(stats[:3])}\n{' | '.join(stats[3:])}")
            else:
                self.lbl_metrics.setText(" | ".join(stats))
        except Exception as e:
            self.lbl_metrics.setText(f"Profiler Error: {str(e)}")

    def update_progress(self, percent, msg=None):
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
        self.lbl_metrics.setVisible(False)
        self.pbar.setVisible(False)
        self.lbl_title.setText(title)
        self.lbl_message.setText(message)
        self.btn_cancel.setText("OK")
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.setStyleSheet("""
            QPushButton { background-color: #007acc; color: white; border-radius: 4px; border: none; font-weight:bold;}
            QPushButton:hover { background-color: #0062a3; }
        """)
        try:
            self.btn_cancel.clicked.disconnect()
        except:
            pass
        self.btn_cancel.clicked.connect(self.accept)

    def on_cancel_clicked(self):
        """修复逻辑：点击取消后，给后台发送信号，并设置500ms强制关闭定时器"""
        self.lbl_message.setText("Stopping... forcing termination.")
        self.btn_cancel.setEnabled(False)

        # 1. 停止监控
        if hasattr(self, 'metric_timer'): self.metric_timer.stop()

        # 2. 发送信号给 TaskManager 去杀进程
        self.sig_canceled.emit()

        # 3. 强制关闭窗口（不再无限等待后台）
        QTimer.singleShot(500, self.reject)

    def close_safe(self):
        if hasattr(self, 'metric_timer'): self.metric_timer.stop()
        self.accept()

    def closeEvent(self, event):
        if hasattr(self, 'metric_timer'): self.metric_timer.stop()
        super().closeEvent(event)


# ... (ProjectEditorDialog 保持不变) ...
class ProjectEditorDialog(BaseDialog):
    def __init__(self, parent=None, is_edit=False, current_data=None):
        title = "Edit Library Info" if is_edit else "Create New Library"
        super().__init__(parent, title=title, width=480)

        form_widget = QWidget()
        self.form_layout = QFormLayout(form_widget)
        self.form_layout.setSpacing(15)
        self.form_layout.setLabelAlignment(Qt.AlignRight)

        form_widget.setStyleSheet("""
            QLabel { color: #aaaaaa; font-size: 13px; border: none; } 
            QLineEdit, QTextEdit, QComboBox { 
                background-color: #2d2d30; border: 1px solid #444; color: #eeeeee; 
                border-radius: 4px; padding: 5px; selection-background-color: #007acc; 
            } 
            QLineEdit:focus, QTextEdit:focus, QComboBox:focus { border: 1px solid #007acc; }
        """)

        self.inp_name = QLineEdit()
        self.inp_name.setPlaceholderText("e.g. Cotton Genomics")
        self.form_layout.addRow("Name:", self.inp_name)

        self.inp_domain = QLineEdit()
        self.inp_domain.setPlaceholderText("e.g. Plant Biology")
        self.form_layout.addRow("Domain:", self.inp_domain)

        self.inp_desc = QTextEdit()
        self.inp_desc.setPlaceholderText("Optional description...")
        self.inp_desc.setMaximumHeight(70)
        self.form_layout.addRow("Desc:", self.inp_desc)

        self.combo_model = QComboBox()
        active_models = EMBEDDING_MODELS
        for m in active_models:
            self.combo_model.addItem(m['ui_name'], m['id'])
        self.form_layout.addRow("AI Model:", self.combo_model)

        self.content_layout.addWidget(form_widget)

        if is_edit and current_data:
            self.inp_name.setText(current_data.get('name', ''))
            self.inp_domain.setText(current_data.get('domain', ''))
            self.inp_desc.setText(current_data.get('description', ''))
            current_mid = current_data.get('model_id')
            idx = self.combo_model.findData(current_mid)
            if idx >= 0: self.combo_model.setCurrentIndex(idx)
            self.model_warn = QLabel(
                "Changing the model invalidates existing vector data. Index rebuild required after saving.")
            self.model_warn.setStyleSheet("color: #e6a23c; font-size: 11px; font-weight: bold; border: none;")
            self.model_warn.setWordWrap(True)
            self.form_layout.addRow("", self.model_warn)

        self.add_button("Cancel", self.reject)
        self.add_button("Save", self.accept, is_primary=True)

    def get_data(self):
        return {
            "name": self.inp_name.text().strip(),
            "domain": self.inp_domain.text().strip(),
            "description": self.inp_desc.toPlainText().strip(),
            "model_id": self.combo_model.currentData()
        }