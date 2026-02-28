import logging
import os
import sys
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


class TaskMode(Enum):
    PROCESS = "process"
    THREAD = "thread"


class BackgroundTask:
    """后台任务逻辑基类，不再直接继承 Process/Thread"""

    def __init__(self, task_id, queue: mp.Queue, kwargs=None):
        self.task_id = task_id
        self.queue = queue
        self.kwargs = kwargs or {}
        self.logger = logging.getLogger("BackgroundTask")
        self._is_cancelled = False

    def run(self):
        # 局部导入！
        from src.core.config_manager import ConfigManager
        self.config = ConfigManager()

        self.logger.debug(f"Start. PID: {os.getpid()} | Task: {self.task_id}")
        self.queue.put({"state": TaskState.PROCESSING.value, "progress": -1, "msg": "Initializing..."})
        try:
            result_payload = self._execute()
            self.queue.put({
                "state": TaskState.SUCCESS.value,
                "progress": 100,
                "msg": "Task completed.",
                "payload": result_payload
            })
        except Exception as e:
            err = traceback.format_exc()
            self.logger.error(f"CRASHED:\n{err}")
            self.queue.put({"state": TaskState.FAILED.value, "progress": 0, "msg": str(e), "payload": None})

    def send_log(self, level: str, msg: str):
        self.queue.put({"type": "log", "level": level, "msg": msg})

    def update_progress(self, progress: int, msg: str):
        self.queue.put({"state": TaskState.PROCESSING.value, "progress": progress, "msg": msg})

    def _execute(self):
        raise NotImplementedError()

    def cancel(self):
        self._is_cancelled = True

# --- 物理包装器 ---
class RunnerProcess(mp.Process):
    def __init__(self, task_cls, task_id, queue, kwargs):
        super().__init__()
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
        self.task_queue = mp.Queue()
        self.worker = None
        self._listener_thread = None
        self.current_mode = TaskMode.PROCESS
        self.pre_task_hook = None
        self.post_task_hook = None
        self.terminate_hook = None

    def register_hooks(self, pre=None, post=None, on_terminate=None):
        self.pre_task_hook = pre
        self.post_task_hook = post
        self.terminate_hook = on_terminate

    def start_task(self, task_class, task_id, mode: TaskMode = TaskMode.PROCESS, **kwargs):
        self.logger.info(f"Launching {task_class.__name__} in {mode.value} mode")
        self.cancel_task()  # 清理旧任务

        self.current_mode = mode
        if self.pre_task_hook: self.pre_task_hook()

        if mode == TaskMode.PROCESS:
            self.worker = RunnerProcess(task_class, task_id, self.task_queue, kwargs)
            self.worker.daemon = True
        else:
            self.worker = RunnerThread(task_class, task_id, self.task_queue, kwargs)

        try:
            self.worker.start()
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

        if "payload" in data and data["payload"] is not None:
            self.sig_result.emit(data["payload"])

        state, msg, progress = data.get("state"), data.get("msg", ""), data.get("progress", -2)
        if progress != -2:
            self.sig_progress.emit(progress, msg)

        if state in [TaskState.SUCCESS.value, TaskState.FAILED.value]:
            if state == TaskState.SUCCESS.value and self.post_task_hook:
                self.post_task_hook()
            self.sig_state_changed.emit(state, msg)
            self.stop_listener()

            if self.worker:
                if self.current_mode == TaskMode.THREAD:
                    self.worker.deleteLater()
                self.worker = None

    def cancel_task(self):
        if self.worker:
            if hasattr(self.worker, 'task'):
                self.worker.task.cancel()

            if self.current_mode == TaskMode.PROCESS and self.worker.is_alive():
                self.kill_process_tree(self.worker.pid)
            elif self.current_mode == TaskMode.THREAD and self.worker.isRunning():
                if not hasattr(self, '_orphaned_threads'):
                    self._orphaned_threads = []

                old_worker = self.worker
                old_worker.quit()
                self._orphaned_threads.append(old_worker)

                old_worker.finished.connect(
                    lambda w=old_worker: self._orphaned_threads.remove(w) if w in getattr(self, '_orphaned_threads',
                                                                                          []) else None
                )

            self.sig_state_changed.emit(TaskState.TERMINATED.value, "Manually halted.")
            self.stop_listener()
            if self.terminate_hook: self.terminate_hook()

            self.worker = None

    def stop_listener(self):
        if self._listener_thread:
            self._listener_thread.stop()
            self._listener_thread.wait(200)
            self._listener_thread.deleteLater()
            self._listener_thread = None

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
        super().__init__()
        self.queue = queue
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