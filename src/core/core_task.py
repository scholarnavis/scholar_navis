import logging
import os
import sys
import time
import datetime
import traceback
import subprocess
import multiprocessing as mp
from enum import Enum
from PySide6.QtCore import QObject, Signal, QThread



class TaskState(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"
    TERMINATED = "terminated"


class BackgroundTask(mp.Process):
    """后台任务基类"""

    def __init__(self, task_id, queue: mp.Queue, kwargs=None):
        super().__init__()
        self.task_id = task_id
        self.queue = queue
        self.kwargs = kwargs or {}
        self.logger = logging.getLogger("BackgroundTask")

    def run(self):
        self.logger.debug(sys.path)

        self.logger.debug(f"[Child Process] Start. PID: {os.getpid()} | Task: {self.task_id}")
        self.queue.put({"state": TaskState.PROCESSING.value, "progress": -1, "msg": "Initializing..."})
        try:
            self._execute()
            self.queue.put({"state": TaskState.SUCCESS.value, "progress": 100, "msg": "Task completed."})
            self.logger.debug(f"[Child Process] Success: {self.task_id}")
        except Exception as e:
            err = traceback.format_exc()
            self.logger.debug(f"[Child Process] CRASHED:\n{err}")
            self.queue.put({"state": TaskState.FAILED.value, "progress": 0, "msg": str(e)})

    def send_log(self, level: str, msg: str):
        self.queue.put({"type": "log", "level": level, "msg": msg})

    def update_progress(self, progress: int, msg: str):
        """发送进度更新给主进程的 UI"""
        self.queue.put({"state": TaskState.PROCESSING.value, "progress": progress, "msg": msg})

    def _execute(self):
        raise NotImplementedError()


class TaskManager(QObject):
    """主进程管理者"""
    sig_progress = Signal(int, str)
    sig_state_changed = Signal(str, str)
    sig_log = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger("TaskManager")
        self.task_queue = mp.Queue()
        self.worker_process = None
        self._listener_thread = None
        self.pre_task_hook = None
        self.post_task_hook = None
        self.terminate_hook = None

    def register_hooks(self, pre=None, post=None, on_terminate=None):
        """【补全缺失的方法】"""
        self.pre_task_hook = pre
        self.post_task_hook = post
        self.terminate_hook = on_terminate

    def start_task(self, task_class, task_id, **kwargs):
        self.logger.info(f"Launching {task_class.__name__}")
        if getattr(self, 'worker_process', None):
            if self.worker_process.is_alive():
                self.worker_process.join(timeout=2.0)
            self.worker_process = None

        if self.pre_task_hook: self.pre_task_hook()

        # 传入统一的 task_queue
        self.worker_process = task_class(task_id, self.task_queue, kwargs)
        self.worker_process.daemon = True

        try:
            self.worker_process.start()
            self.logger.debug(f"Process spawned. PID: {self.worker_process.pid}")
            self._listener_thread = QueueListenerThread(self.task_queue)
            self._listener_thread.sig_data_received.connect(self._handle_queue_msg)
            self._listener_thread.start()
        except Exception as e:
            self.logger.error(f"Spawn FAILED: {str(e)}")

    def _handle_queue_msg(self, data):
        msg_type = data.get("type", "state")
        if msg_type == "log":
            self.sig_log.emit(data.get("level", "INFO"), data.get("msg", ""))
            return
        state, msg, progress = data.get("state"), data.get("msg", ""), data.get("progress", -2)
        if progress != -2: self.sig_progress.emit(progress, msg)
        if state in [TaskState.SUCCESS.value, TaskState.FAILED.value]:
            if state == TaskState.SUCCESS.value and self.post_task_hook: self.post_task_hook()
            self.sig_state_changed.emit(state, msg)
            self.stop_listener()

    def cancel_task(self):
        if self.worker_process and self.worker_process.is_alive():
            pid = self.worker_process.pid
            self.kill_process_tree(pid)
            self.sig_state_changed.emit(TaskState.TERMINATED.value, "Manually halted.")
            self.stop_listener()
            if self.terminate_hook: self.terminate_hook()

    def stop_listener(self):
        if self._listener_thread: self._listener_thread.stop()

    @staticmethod
    def kill_process_tree(pid: int):
        try:
            if sys.platform == "win32":
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            else:
                os.kill(pid, 9)
        except:
            pass


class QueueListenerThread(QThread):
    sig_data_received = Signal(dict)

    def __init__(self, queue):
        super().__init__();
        self.queue = queue;
        self._is_running = True

    def run(self):
        while self._is_running:
            try:
                data = self.queue.get(timeout=0.1)
                self.sig_data_received.emit(data)
            except:
                continue

    def stop(self):
        self._is_running = False