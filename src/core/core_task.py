import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import multiprocessing as mp
from enum import Enum
from typing import Any, Dict, Optional
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
    """子进程日志 IPC 桥：按条数/时间窗批量聚合后一次入队。

    高频日志（逐行解析、循环进度）若逐条 put 会产生大量 pickle 序列化
    与 pipe 写入；批量后单条 IPC 开销摊薄一个数量级以上。ERROR/WARNING
    绕过批量窗口立即送达，保证错误反馈的实时性。
    """

    BATCH_SIZE = 32          # 缓冲满 N 条立即 flush
    FLUSH_INTERVAL = 0.1     # 或距上次 flush 超过 N 秒 flush

    def __init__(self, task_queue):
        super().__init__()
        self.task_queue = task_queue
        self._buffer = []
        self._last_flush = time.time()

    def emit(self, record):
        # 子进程日志管道不允许向调用方抛出异常（与 stdlib logging.Handler.emit 行为一致）；
        # format() 失败域不可穷举，此处豁免静态检查。
        # noinspection PyBroadException
        try:
            msg = f"[{record.name}] {self.format(record)}"
            level = record.levelname
            if level in ("ERROR", "WARNING"):
                self.task_queue.put({"type": "log_batch", "records": [{"level": level, "msg": msg}]})
                return
            self._buffer.append({"level": level, "msg": msg})
            if len(self._buffer) >= self.BATCH_SIZE or (time.time() - self._last_flush) >= self.FLUSH_INTERVAL:
                self.flush()
        except Exception:
            pass

    def flush(self):
        """把缓冲中的日志一次性入队；供任务退出前调用，防尾部日志丢失。"""
        # noinspection PyBroadException
        try:
            if self._buffer:
                self.task_queue.put({"type": "log_batch", "records": self._buffer})
                self._buffer = []
                self._last_flush = time.time()
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
        self._ipc_handler: Optional[IPCLogHandler] = None

        self._last_progress_time = 0.0
        self._last_log_time = 0.0
        self._throttle_interval = 0.05

        # 流式 token IPC 攒批缓冲：高频 token 逐条 put 的 pickle/pipe 开销
        # 显著；按字符量/时间窗聚合后批量入队。
        self._token_buffer: list = []
        self._last_token_flush = 0.0

    def run(self):
        if mp.current_process().name != 'MainProcess':
            root_logger = logging.getLogger()
            root_logger.handlers.clear()

            self._ipc_handler = IPCLogHandler(self.queue)
            self._ipc_handler.setFormatter(logging.Formatter('%(message)s'))
            root_logger.addHandler(self._ipc_handler)
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
        finally:
            # 进程退出前冲刷批量日志与 token 缓冲，避免尾部数据滞留丢失
            self._flush_stream_buffer()
            if self._ipc_handler is not None:
                self._ipc_handler.flush()

    def _execute(self) -> Any:
        raise NotImplementedError()

    def _emit_state(self, state: TaskState, progress: int, msg: str, payload: Any = None):
        # 顺序保证：state 消息（进度/终态/控制 payload）必须晚于此前已缓冲
        # 的 token 送达，避免 UI 端状态切换先于正文出现
        self._flush_stream_buffer()
        self.queue.put({
            "type": "state",
            "state": state.value,
            "progress": progress,
            "msg": msg,
            "payload": payload
        })

    # 流式 token 攒批：普通文本按字符量/时间窗聚合；控制标记（UI 按
    # 整 token 精确匹配路由，如 [CLEAR_SEARCH]）穿透缓冲立即单发。
    _CONTROL_TOKENS = {"[CLEAR_SEARCH]", "[START_LLM_NETWORK]"}
    _TOKEN_BATCH_CHARS = 512
    _TOKEN_BATCH_INTERVAL = 0.05

    def stream_token(self, token: str):
        """流式 token 发送入口：攒批后经 update_progress(-1) 送达 UI。"""
        if token.strip() in self._CONTROL_TOKENS:
            self._flush_stream_buffer()
            self.update_progress(-1, token)
            return
        self._token_buffer.append(token)
        buf_chars = sum(len(t) for t in self._token_buffer)
        if (buf_chars >= self._TOKEN_BATCH_CHARS
                or time.time() - self._last_token_flush >= self._TOKEN_BATCH_INTERVAL):
            self._flush_stream_buffer()

    def _flush_stream_buffer(self):
        if self._token_buffer:
            batch = "".join(self._token_buffer)
            self._token_buffer.clear()
            self.update_progress(-1, batch)
        self._last_token_flush = time.time()

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
        # start() 之前 pid 为 None，logger 名只带 task 标识；真实 PID 由
        # BackgroundTask.run 的 Start 日志补记
        self.logger = logging.getLogger(f"RunnerProcess-{task_id}")
        self.task = task_cls(task_id, queue, kwargs)

    def run(self):
        try:
            self.task.run()
        except Exception:
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
    # 大 payload 后台反序列化完成后的跨线程回传（queued 自动切回主线程）
    sig_payload_ready = Signal(object, object)

    # 队列轮询自适应间隔与单次处理上限：空闲 100ms 降低空转唤醒；有消息
    # 20ms 快速跟进；积压未清完 10ms 全速消化。单 tick 最多 200 条，防止
    # 海量积压时一次 tick 长时间占用 UI 线程。
    _POLL_IDLE_MS = 100
    _POLL_ACTIVE_MS = 20
    _POLL_FLOODED_MS = 10
    _POLL_BATCH_CAP = 200

    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger("TaskManager")
        self.task_queue = None
        self.worker = None
        self.current_mode = TaskMode.PROCESS

        self.hooks = {"pre": None, "post": None, "terminate": None}

        # wait() 重入保护标记
        self._waiting = False

        # 轮询队列的定时器 (替代原生线程)
        self._queue_timer = QTimer(self)
        self._queue_timer.timeout.connect(self._poll_queue)
        self._queue_timer.setInterval(self._POLL_IDLE_MS)

        # 用于延迟启动任务的定时器
        self._delay_start_timer = QTimer(self)
        self._delay_start_timer.setSingleShot(True)

        # 大 payload 反序列化在后台线程完成，经 queued connection 回主线程分发
        self.sig_payload_ready.connect(self._on_payload_resolved)

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
                # Windows spawn 启动较重（需重新 import 主模块），放后台
                # 线程拉起，避免阻塞 UI
                threading.Thread(target=self.worker.start, daemon=True).start()
            else:
                self.worker.start()

            self._queue_timer.start()
        except Exception as e:
            self.logger.error(f"Spawn FAILED: {e}")
            self.sig_state_changed.emit(TaskState.FAILED.value, f"Spawn FAILED: {e}")


    def _poll_queue(self):
        if not self.task_queue:
            return
        processed = 0
        while processed < self._POLL_BATCH_CAP:
            try:
                data = self.task_queue.get_nowait()
            except queue.Empty:
                break
            except Exception as e:
                self.logger.error(f"Error reading queue: {e}")
                break
            self._dispatch_message(data)
            processed += 1

        # 自适应轮询节奏：按本 tick 的消化情况动态调整下次唤醒间隔
        if processed >= self._POLL_BATCH_CAP:
            self._queue_timer.setInterval(self._POLL_FLOODED_MS)
        elif processed > 0:
            self._queue_timer.setInterval(self._POLL_ACTIVE_MS)
        else:
            self._queue_timer.setInterval(self._POLL_IDLE_MS)

    def _dispatch_message(self, data: Dict):
        msg_type = data.get("type", "state")
        if msg_type == "log_batch":
            # 子进程批量聚合的日志，逐条还原分发
            for rec in data.get("records") or []:
                self._emit_log(rec.get("level", "INFO"), rec.get("msg", ""))
        elif msg_type in ["log", "system_log"]:
            self._emit_log(data.get("level", "INFO"), data.get("msg", ""))
        elif msg_type == "state":
            self._handle_state(data)

    def _emit_log(self, lvl_str: str, msg: str):
        self.sig_log.emit(lvl_str, msg)
        logging.getLogger("TaskWorker").log(getattr(logging, lvl_str.upper(), logging.INFO), msg)

    def _handle_state(self, data: Dict):
        payload = data.get("payload")
        if isinstance(payload, dict) and payload.get("_is_temp_file"):
            # 大 payload 文件的反序列化挪到后台线程，避免 json.load 阻塞
            # UI；完成后再按原顺序（result → progress → state）补发
            threading.Thread(target=self._resolve_payload_worker, args=(data,), daemon=True).start()
            return
        self._dispatch_state_message(
            data, self._resolve_temp_file_payload(payload) if payload is not None else None)

    def _resolve_payload_worker(self, data: Dict):
        resolved = self._resolve_temp_file_payload(data.get("payload"))
        try:
            self.sig_payload_ready.emit(resolved, data)
        except RuntimeError:
            # 管理器已在关闭中销毁，丢弃迟到结果即可
            pass

    def _on_payload_resolved(self, resolved_payload, data: Dict):
        self._dispatch_state_message(data, resolved_payload)

    def _dispatch_state_message(self, data: Dict, resolved_payload):
        if resolved_payload is not None:
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
        if not self._is_worker_running():
            return
        if self._waiting:
            # 重入保护：QEventLoop 嵌套会导致信号重复分发，已有等待循环
            # 在跑时直接复用它
            self.logger.warning("wait() re-entered while a wait loop is active; ignored.")
            return

        self._waiting = True
        loop = QEventLoop(self)

        def on_state_changed(state, msg):
            if state in [TaskState.SUCCESS.value, TaskState.FAILED.value, TaskState.TERMINATED.value]:
                loop.quit()

        self.sig_state_changed.connect(on_state_changed)
        try:
            if timeout_sec:
                # 定时器挂在 loop 下，loop 结束时随之销毁，不会向已退出
                # 的循环再发 quit
                timeout_timer = QTimer(loop)
                timeout_timer.setSingleShot(True)
                timeout_timer.timeout.connect(loop.quit)
                timeout_timer.start(int(timeout_sec * 1000))
            loop.exec()
        finally:
            # 即使 loop.exec 抛异常也保证摘除槽函数，避免悬挂引用
            self.sig_state_changed.disconnect(on_state_changed)
            self._waiting = False

    def _is_worker_running(self) -> bool:
        if not self.worker:
            return False
        if self.current_mode == TaskMode.PROCESS:
            return self.worker.is_alive()
        return self.worker.isRunning()

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
            # 杀树只做系统调用（psutil），不等待进程退出；join 挪到后台
            # 线程，消除原实现 join(1.0) 同步等待造成的 UI 冻结
            self._kill_process_tree(worker_to_stop.pid)
            threading.Thread(target=self._reap_process, args=(worker_to_stop,), daemon=True).start()
        elif self.current_mode == TaskMode.THREAD and worker_to_stop.isRunning():
            worker_to_stop.requestInterruption()

        self.sig_state_changed.emit(TaskState.TERMINATED.value, "Task has been terminated.")

        if self.hooks.get("terminate"):
            self.hooks["terminate"]()

    def _reap_process(self, worker):
        """后台等待被杀进程退出；超时仅告警（daemon 进程随主程序退出兜底）。"""
        try:
            worker.join(timeout=2.0)
            if worker.is_alive():
                self.logger.warning(
                    f"Process {worker.pid} still alive 2s after kill; left to daemon cleanup.")
        except Exception as e:
            self.logger.warning(f"Process reap error: {e}")

    def _cleanup_worker(self):
        self._queue_timer.stop()
        if self.worker:
            if self.current_mode == TaskMode.PROCESS and self.worker.is_alive():
                self.worker.terminate()

            self.worker = None


    @staticmethod
    def _kill_process_tree(pid: int):
        """终止任务进程树（主进程 + 全部子孙进程）。

        psutil 的 kill 为纯系统调用（Windows 下即 TerminateProcess），微秒
        级完成；替代原先 taskkill 子进程 spawn + 同步等待，消除主线程
        阻塞。psutil 不可用或异常时，Windows 降级为 Popen 异步 taskkill，
        不等待其退出。
        """
        try:
            import psutil
            parent = psutil.Process(pid)
            # 先杀子孙再杀父，防止父进程退出后子进程被重新挂靠而失联
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            parent.kill()
        except psutil.NoSuchProcess:
            pass
        except Exception as e:
            logging.getLogger("TaskManager").warning(f"psutil kill failed, fallback to system tool: {e}")
            if sys.platform == "win32":
                # CREATE_NO_WINDOW：GUI 进程不闪控制台窗口；Popen 不等待
                subprocess.Popen(
                    ['taskkill', '/F', '/T', '/PID', str(pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                import signal
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass