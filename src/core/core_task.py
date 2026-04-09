import json
import logging
import os
import queue
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

class IPCLogHandler(logging.Handler):
    def __init__(self, task_queue):
        super().__init__()
        self.task_queue = task_queue

    def emit(self, record):
        try:
            msg = self.format(record)
            self.task_queue.put({
                "type": "log",
                "level": record.levelname,
                "msg": f"[{record.name}] {msg}"
            })
        except Exception:
            pass


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
        if mp.current_process().name != 'MainProcess':
            root_logger = logging.getLogger()
            root_logger.handlers.clear()

            ipc_handler = IPCLogHandler(self.queue)
            ipc_handler.setFormatter(logging.Formatter('%(message)s'))
            root_logger.addHandler(ipc_handler)
            root_logger.setLevel(logging.INFO)

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
        self.logger = logging.getLogger(f"RunnerProcess-{task_id} ({self.pid})")
        self.task = task_cls(task_id, queue, kwargs)

    def run(self):
        try:
            self.task.run()
        except Exception as e:
            self.logger.error(f"Process Crash: {traceback.format_exc()}")
            raise


# 全局孤儿线程池：保存被取消但仍在后台进行收尾的线程，防止被 Python GC 杀掉导致 0xC0000409
_active_threads = set()


class RunnerThread(QThread):
    def __init__(self, task_cls, task_id, queue, kwargs):
        # 绝不传递 parent，防止主进程垃圾回收时误杀底层 C++ 线程
        super().__init__(parent=None)
        self.task = task_cls(task_id, queue, kwargs)

        # 线程启动前，把自己注册到全局集合保命
        _active_threads.add(self)
        self.finished.connect(self._on_finish)

    def _on_finish(self):
        # 线程自然死透后，自动从集合中移除，并安全释放 C++ 内存
        _active_threads.discard(self)
        self.deleteLater()

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

        heavy_tasks = []
        if task_class.__name__ in heavy_tasks:
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

        if mode == TaskMode.THREAD:
            self.worker = RunnerThread(task_class, task_id, self.task_queue, kwargs)
        else:
            self.worker = RunnerProcess(task_class, task_id, self.task_queue, kwargs)

        try:
            if mode == TaskMode.PROCESS:
                import threading
                threading.Thread(target=self.worker.start, daemon=True).start()
            else:
                self.worker.start()

            self._queue_timer.start()
        except Exception as e:
            self.logger.error(f"Spawn FAILED: {e}")
            self.sig_state_changed.emit(TaskState.FAILED.value, f"Spawn FAILED: {e}")
        # --- 修改结束 ---


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


    def wait(self, timeout_sec: float = None):

        if not self.worker or (self.current_mode == TaskMode.PROCESS and not self.worker.is_alive()) or (
                self.current_mode == TaskMode.THREAD and not self.worker.isRunning()):
            return

        loop = QEventLoop(self)

        def on_state_changed(state, msg):
            if state in [TaskState.SUCCESS.value, TaskState.FAILED.value, TaskState.TERMINATED.value]:
                loop.quit()

        self.sig_state_changed.connect(on_state_changed)

        if timeout_sec:
            QTimer.singleShot(int(timeout_sec * 1000), loop.quit)

        loop.exec()

        self.sig_state_changed.disconnect(on_state_changed)

    def cancel_task(self):
        if self._delay_start_timer.isActive():
            self._delay_start_timer.stop()
            self.sig_state_changed.emit(TaskState.TERMINATED.value, "Cancelled before start.")
            return

        if not self.worker:
            return

        worker_to_stop = self.worker
        self.worker = None
        self._queue_timer.stop()

        if hasattr(worker_to_stop, 'task'):
            worker_to_stop.task.cancel()

        if self.current_mode == TaskMode.PROCESS and worker_to_stop.is_alive():
            self._kill_process_tree(worker_to_stop.pid)
            worker_to_stop.join(timeout=1.0)
        elif self.current_mode == TaskMode.THREAD and worker_to_stop.isRunning():
            worker_to_stop.requestInterruption()

        self.sig_state_changed.emit(TaskState.TERMINATED.value, "Task has been terminated.")

        if self.hooks.get("terminate"):
            self.hooks["terminate"]()

    def _cleanup_worker(self):
        self._queue_timer.stop()
        if self.worker:
            if self.current_mode == TaskMode.PROCESS and self.worker.is_alive():
                self.worker.terminate()

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