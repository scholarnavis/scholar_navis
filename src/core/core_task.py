import json
import logging
import os
import queue
import signal
import subprocess
import sys
import time
import traceback
import multiprocessing as mp
from enum import Enum
from typing import Any, Dict, Optional

import psutil
from PySide6.QtCore import QObject, Signal, QThread, QTimer, QEventLoop


class TaskState(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"
    TERMINATED = "terminated"


class TaskMode(Enum):
    PROCESS = "process"
    THREAD = "thread"


class BackgroundTask:
    """后台任务逻辑基类：只负责任务执行、进度和日志发送，不包含具体业务"""

    def __init__(self, task_id: str, task_queue: mp.Queue, kwargs: Optional[Dict] = None):
        self.task_id = task_id
        self.queue = task_queue
        self.kwargs = kwargs or {}
        self.logger = logging.getLogger(f"Task-{self.task_id}")
        self._cancel_event = mp.Event()

        self._last_progress_time = 0.0
        self._last_log_time = 0.0
        self._throttle_interval = 0.05

    def run(self):
        self.logger.debug(f"Start. PID: {os.getpid()} | Task: {self.task_id}")
        self._emit_state(TaskState.PROCESSING, -1, "Initializing...")
        try:
            result_payload = self._execute()
            self._emit_state(TaskState.SUCCESS, 100, "Task completed.", result_payload)
        except Exception as e:
            err = traceback.format_exc()
            self.logger.error(f"CRASHED:\n{err}")
            self._emit_state(TaskState.FAILED, 0, str(e))

    def _execute(self) -> Any:
        raise NotImplementedError()

    def _emit_state(self, state: TaskState, progress: int, msg: str, payload: Any = None):
        self.queue.put({
            "type": "state",
            "state": state.value,
            "progress": progress,
            "msg": msg,
            "payload": payload
        })

    def send_log(self, level: str, msg: str):
        current_time = time.time()
        if level in ["ERROR", "WARNING"] or (current_time - self._last_log_time >= self._throttle_interval):
            self._last_log_time = current_time
            self.queue.put({"type": "log", "level": level, "msg": msg})

    def update_progress(self, progress: int, msg: str):
        current_time = time.time()
        if progress in (-1, 0, 100) or (current_time - self._last_progress_time >= self._throttle_interval):
            self._last_progress_time = current_time
            self._emit_state(TaskState.PROCESSING, progress, msg)

    def cancel(self):
        self._cancel_event.set()

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def wait_for_cancel(self, timeout: float):
        if self._cancel_event.wait(timeout):
            raise InterruptedError("Task was cancelled during wait.")


class RunnerProcess(mp.Process):
    def __init__(self, task_cls, task_id, queue, kwargs):
        super().__init__(daemon=True)
        self.task = task_cls(task_id, queue, kwargs)

    def run(self):
        self.task.run()


class RunnerThread(QThread):
    def __init__(self, task_cls, task_id, queue, kwargs):
        super().__init__()
        self.task = task_cls(task_id, queue, kwargs)

    def run(self):
        self.task.run()


class TaskManager(QObject):
    sig_progress = Signal(int, str)
    sig_state_changed = Signal(str, str)
    sig_log = Signal(str, str)
    sig_result = Signal(object)

    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger("TaskManager")
        self.task_queue = None
        self.worker = None
        self.current_mode = TaskMode.PROCESS

        self.hooks = {"pre": None, "post": None, "terminate": None}

        # 轮询队列的定时器 (替代原生线程)
        self._queue_timer = QTimer(self)
        self._queue_timer.timeout.connect(self._poll_queue)
        self._queue_timer.setInterval(100)

        # 用于延迟启动任务的定时器
        self._delay_start_timer = QTimer(self)
        self._delay_start_timer.setSingleShot(True)

    def register_hooks(self, pre=None, post=None, on_terminate=None):
        self.hooks["pre"] = pre
        self.hooks["post"] = post
        self.hooks["terminate"] = on_terminate

    def start_task(self, task_class, task_id: str, mode: TaskMode = TaskMode.PROCESS, delay_ms: int = 0, **kwargs):
        """
        启动任务
        :param delay_ms: 延迟多少毫秒后真正执行任务 (满足“等待一段时间再执行”需求)
        """
        self.cancel_task()  # 清理旧任务

        if delay_ms > 0:
            self.logger.info(f"Task {task_id} scheduled to start in {delay_ms}ms")
            # 绑定实际启动逻辑
            self._delay_start_timer.timeout.disconnect() if self._delay_start_timer.receivers(
                self._delay_start_timer.timeout) > 0 else None
            self._delay_start_timer.timeout.connect(lambda: self._real_start(task_class, task_id, mode, kwargs))
            self._delay_start_timer.start(delay_ms)
        else:
            self._real_start(task_class, task_id, mode, kwargs)

    def _real_start(self, task_class, task_id: str, mode: TaskMode, kwargs: Dict):

        # 强制将已知会引发 GIL 锁死的重型任务转移到独立进程，无视工具组件的原始请求
        if task_class.__name__ in ["VerifyModelsTask", "VersionCheckTask"]:
            if mode == TaskMode.THREAD:
                self.logger.warning(f"Intercepted {task_class.__name__}: Forcing PROCESS mode to prevent UI freeze.")
                mode = TaskMode.PROCESS

        self.logger.info(f"Launching {task_class.__name__} in {mode.value} mode")
        if mode == TaskMode.PROCESS:
            self.task_queue = mp.Queue()
        else:
            self.task_queue = queue.Queue()

        self.current_mode = mode

        if self.hooks["pre"]:
            self.hooks["pre"]()

        WorkerClass = RunnerProcess if mode == TaskMode.PROCESS else RunnerThread
        self.worker = WorkerClass(task_class, task_id, self.task_queue, kwargs)

        try:
            self.worker.start()
            self._queue_timer.start()
        except Exception as e:
            self.logger.error(f"Spawn FAILED: {e}")
            self.sig_state_changed.emit(TaskState.FAILED.value, f"Spawn FAILED: {e}")

    # --- 消息分发 ---

    def _poll_queue(self):
        if not self.task_queue: return
        while True:
            try:
                data = self.task_queue.get_nowait()
                self._dispatch_message(data)
            except queue.Empty:
                break
            except Exception as e:
                self.logger.error(f"Error reading queue: {e}")
                break

    def _dispatch_message(self, data: Dict):
        msg_type = data.get("type", "state")
        if msg_type in ["log", "system_log"]:
            lvl_str = data.get("level", "INFO")
            msg = data.get("msg", "")
            self.sig_log.emit(lvl_str, msg)
            logging.getLogger("TaskWorker").log(getattr(logging, lvl_str.upper(), logging.INFO), msg)
        elif msg_type == "state":
            self._handle_state(data)

    def _handle_state(self, data: Dict):
        if "payload" in data and data["payload"] is not None:
            resolved_payload = self._resolve_temp_file_payload(data["payload"])
            self.sig_result.emit(resolved_payload)

        progress = data.get("progress", -2)
        if progress != -2:
            self.sig_progress.emit(progress, data.get("msg", ""))

        state = data.get("state")
        if state in [TaskState.SUCCESS.value, TaskState.FAILED.value]:
            if state == TaskState.SUCCESS.value and self.hooks["post"]:
                self.hooks["post"]()

            self.sig_state_changed.emit(state, data.get("msg", ""))
            self._cleanup_worker()

    def _resolve_temp_file_payload(self, payload: Any) -> Any:
        if not (isinstance(payload, dict) and payload.get("_is_temp_file")):
            return payload
        temp_path = payload["path"]
        try:
            with open(temp_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            self.logger.error(f"Failed to load temp payload file: {e}")
            return {}
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    # --- 控制逻辑 ---

    def wait(self, timeout_sec: float = None):
        """
        等待任务完成（成功、失败或终止）。
        阻塞当前代码向下执行，但使用 QEventLoop 保证 UI 不会卡顿！
        """
        # 如果任务还没生成（比如在 delay 阶段），或者是空的，直接返回
        if not self.worker or (self.current_mode == TaskMode.PROCESS and not self.worker.is_alive()) or (
                self.current_mode == TaskMode.THREAD and not self.worker.isRunning()):
            return

        loop = QEventLoop(self)

        # 定义收到结束信号时退出局部事件循环
        def on_state_changed(state, msg):
            if state in [TaskState.SUCCESS.value, TaskState.FAILED.value, TaskState.TERMINATED.value]:
                loop.quit()

        self.sig_state_changed.connect(on_state_changed)

        # 超时强行退出阻塞
        if timeout_sec:
            QTimer.singleShot(int(timeout_sec * 1000), loop.quit)

        # 启动局部事件循环 (代码会停在这里，但 UI 事件依然响应)
        loop.exec()

        # 断开连接，防止内存泄漏
        self.sig_state_changed.disconnect(on_state_changed)

    def cancel_task(self):
        # 1. 如果还在延迟启动阶段，直接取消定时器
        if self._delay_start_timer.isActive():
            self._delay_start_timer.stop()
            self.sig_state_changed.emit(TaskState.TERMINATED.value, "Cancelled before start.")
            return

        if not self.worker:
            return

        # 2. 尝试优雅取消
        if hasattr(self.worker, 'task'):
            self.worker.task.cancel()

        # 3. 暴力终止
        if self.current_mode == TaskMode.PROCESS and self.worker.is_alive():
            self._kill_process_tree(self.worker.pid)
        elif self.current_mode == TaskMode.THREAD and self.worker.isRunning():
            self.worker.deleteLater()

            # 4. 发出信号与清理
        self.sig_state_changed.emit(TaskState.TERMINATED.value, "Manually halted.")
        if self.hooks["terminate"]:
            self.hooks["terminate"]()

        self._cleanup_worker()

    def _cleanup_worker(self):
        self._queue_timer.stop()
        if self.current_mode == TaskMode.THREAD and self.worker:
            self.worker.deleteLater()
        self.worker = None

    @staticmethod
    def _kill_process_tree(pid: int):
        try:
            if sys.platform == "win32":
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
            else:
                try:
                    parent = psutil.Process(pid)
                    for child in parent.children(recursive=True): child.kill()
                    parent.kill()
                except psutil.NoSuchProcess:
                    pass
        except Exception:
            pass