import glob
import io
import logging
import multiprocessing
import os
import sys
import threading
import time
import psutil
import tqdm

os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "15")
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "15")
os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")
# Xet 通道会绕过普通 requests 的进度/读超时：网络监控上有流量，但 tqdm
# 停在 0% 且无法超时。强制走标准 HTTP 下载，进度和超时都可控。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import huggingface_hub.utils.logging as hf_logging
from huggingface_hub import HfApi, hf_hub_download

from src.core.core_task import BackgroundTask, TaskState
from src.core.network_worker import setup_global_network_env

_tqdm_lock = threading.Lock()

# 注意：请勿在此处全局 import huggingface_hub 或 torch！
# 必须等待网络环境和线程环境变量设置完毕后再进行局部 import。


_global_callback = None
_last_emit_time = 0
_EMIT_INTERVAL = 0.1
_original_display = tqdm.std.tqdm.display
#: 最近一次 tqdm 进度快照，供 watchdog 判断“界面还在 27% 但网络已无数据”。
_last_progress_wall = 0.0
_last_progress_snapshot = (0, "", "")
_download_active = False


def _format_rate(rate, unit=""):
    """把 tqdm 的 rate 格式化成人类可读的下载速度。"""
    try:
        rate = float(rate or 0)
    except (TypeError, ValueError):
        return ""
    if rate <= 0:
        return ""
    unit = str(unit or "").lower()
    if unit in ("b", "byte", "bytes"):
        if rate >= 1024 ** 3:
            return f"{rate / (1024 ** 3):.2f} GB/s"
        if rate >= 1024 ** 2:
            return f"{rate / (1024 ** 2):.1f} MB/s"
        if rate >= 1024:
            return f"{rate / 1024:.0f} KB/s"
        return f"{rate:.0f} B/s"
    return f"{rate:.1f} it/s"


def patched_display(self, msg=None, pos=None):
    global _last_emit_time, _global_callback
    global _last_progress_wall, _last_progress_snapshot
    res = _original_display(self, msg, pos)

    if _global_callback:
        with _tqdm_lock:
            current_time = time.time()
            is_finished = (self.n >= self.total) if self.total else False

            if is_finished or (current_time - _last_emit_time >= 0.1):
                _last_emit_time = current_time

                # total 未知时用 -1 让进度条走不确定动画；不要伪装成 0%。
                percent = (int((self.n / self.total) * 100)
                           if (self.total and self.total > 0) else -1)
                desc = str(self.desc) if self.desc else "Processing"

                # tqdm 自带的 rate 是“从开始到现在的平均速度”，断网时仍会
                # 停在 4.5 MB/s。这里改为按两次 display 的增量计算瞬时速度，
                # 无数据时明确显示 0 B/s，而不是保留旧值。
                n = float(self.n or 0)
                prev_n = getattr(self, "_navis_prev_n", None)
                prev_t = getattr(self, "_navis_prev_t", None)
                rate = 0.0
                if prev_n is not None and prev_t is not None:
                    dt = max(current_time - prev_t, 1e-6)
                    delta = n - prev_n
                    if delta > 0:
                        instant = delta / dt
                        old = float(getattr(self, "_navis_rate", 0.0) or 0.0)
                        # 轻量平滑，避免单次 display 抖动；但如果距上次
                        # display 已超过 2 秒，说明中间断流过，不能拿旧平滑值
                        # 冒充当前速度，直接用本次区间的平均速度。
                        if old <= 0 or dt > 2.0:
                            rate = instant
                        else:
                            rate = old * 0.55 + instant * 0.45
                self._navis_prev_n = n
                self._navis_prev_t = current_time
                self._navis_rate = rate
                if rate <= 0:
                    idle = current_time - float(getattr(self, "_navis_last_data", 0.0) or 0.0)
                    speed = "0 B/s" if idle >= 1.5 else ""
                else:
                    self._navis_last_data = current_time
                    speed = _format_rate(rate, getattr(self, "unit", ""))

                if "Fetching" in desc or "files" in desc.lower():
                    display_msg = f"- {desc} ({self.n}/{self.total})"
                else:
                    clean_name = desc.replace("Downloading ", "")
                    display_msg = f"- {clean_name}"
                    if speed:
                        display_msg += f" · {speed}"

                _last_progress_wall = current_time
                _last_progress_snapshot = (percent, display_msg, speed)
                _global_callback(percent, display_msg, speed)

    return res


class DownloadCapture:
    def __init__(self, callback):
        self.callback = callback

    def __enter__(self):
        global _global_callback, _download_active, _last_progress_wall
        global _last_progress_snapshot
        _global_callback = self.callback
        _download_active = True
        _last_progress_wall = time.time()
        _last_progress_snapshot = (0, "", "")

        tqdm.std.tqdm.display = patched_display
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        tqdm.std.tqdm.display = _original_display
        global _global_callback, _download_active
        _global_callback = None
        _download_active = False


class RealTimeHFDownloadTask(BackgroundTask):
    def _on_download_progress(self, percent, message, speed_text=""):
        """把 tqdm 的下载百分比/速度桥接到设置页 ProgressDialog。"""
        try:
            msg = str(message or "").lstrip("- ").strip()
            speed = str(speed_text or "").strip()
            if speed and speed not in msg:
                msg = f"{msg} · {speed}" if msg else speed
            raw_progress = int(percent if percent is not None else -1)
            progress = -1 if raw_progress < 0 else max(0, min(100, raw_progress))
            self.queue.put({
                "state": TaskState.PROCESSING.value,
                "progress": progress,
                "msg": f"Downloading {msg}" if msg else "Downloading model files...",
            })
        except (TypeError, ValueError, OSError):
            pass

    def _scan_download_bytes(self):
        total = 0
        for root in getattr(self, "_scan_dirs", []):
            if not root or not os.path.isdir(root):
                continue
            for base, _dirs, files in os.walk(root):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(base, name))
                    except OSError:
                        pass
        return total

    def _watch_download_progress(self):
        """tqdm 不刷新时，用缓存目录的实际字节增长兜底测速。"""
        while not self._watch_stop.wait(1.0):
            if not _download_active:
                continue
            now = time.time()
            idle = now - float(_last_progress_wall or 0.0)
            if idle < 2.5:
                continue
            percent, message, _speed = _last_progress_snapshot
            clean = str(message or "").lstrip("- ").strip()
            if not clean:
                clean = "model files"

            # tqdm/Xet 不报告进度时，文件仍可能正在增长；扫描缓存目录，
            # 用真实字节增量恢复速度显示，避免“有流量但 UI 一直 0%”。
            current = self._scan_download_bytes()
            last = getattr(self, "_scan_last_bytes", current)
            last_t = getattr(self, "_scan_last_time", now)
            delta = current - last
            if delta > 0:
                dt = max(now - last_t, 1e-6)
                speed = _format_rate(delta / dt, "B")
                self._on_download_progress(
                    percent if percent is not None else -1,
                    f"{clean} · {speed}", speed)
                _last_progress_wall = now
                self._scan_last_bytes = current
                self._scan_last_time = now
                continue
            self._scan_last_bytes = current
            self._scan_last_time = now

            if (now - self._last_stall_notify) >= 2.0:
                self._last_stall_notify = now
                self._on_download_progress(percent, f"{clean} · 0 B/s", "0 B/s")

    def _execute(self):
        if sys.stdout is None:
            sys.stdout = io.StringIO()
        if sys.stderr is None:
            sys.stderr = io.StringIO()
        setup_global_network_env()
        # 网络卡住时 requests 的默认读超时可能非常长；15 秒没有数据就
        # 让 huggingface_hub 抛错，由上层显示失败/允许重试，而不是无限 27%。
        os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "15")
        os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "15")
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        repo_id = self.kwargs.get("repo_id")
        self._watch_stop = threading.Event()
        self._last_stall_notify = 0.0
        threading.Thread(target=self._watch_download_progress,
                         daemon=True, name="hf-progress-watchdog").start()
        import huggingface_hub.constants
        if "HF_ENDPOINT" in os.environ:
            huggingface_hub.constants.ENDPOINT = os.environ["HF_ENDPOINT"]
        actual_endpoint = huggingface_hub.constants.ENDPOINT
        http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "None"
        https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "None"
        network_msg = (
            f"Network Config -> Endpoint: {actual_endpoint} | "
            f"HTTP_PROXY: {http_proxy} | HTTPS_PROXY: {https_proxy}"
        )
        if hasattr(self, "logger") and self.logger:
            self.logger.info(network_msg)
        self.send_log("INFO", network_msg)
        cache_dir = os.path.join(huggingface_hub.constants.default_cache_path, "models--" + repo_id.replace("/", "--"))
        try:
            from src.core.models_registry import _get_hf_home
            local_dir = os.path.join(_get_hf_home(), "models--" + repo_id.replace("/", "--"))
        except Exception:
            local_dir = cache_dir
        scan_dirs = [cache_dir, local_dir]
        try:
            scan_dirs.append(os.path.join(
                huggingface_hub.constants.HF_HUB_CACHE,
                "models--" + repo_id.replace("/", "--")))
        except Exception:
            pass
        try:
            hf_home = os.environ.get("HF_HOME")
            if hf_home:
                scan_dirs.append(os.path.join(
                    hf_home, "hub", "models--" + repo_id.replace("/", "--")))
        except Exception:
            pass
        self._scan_dirs = list(dict.fromkeys(d for d in scan_dirs if d))
        self._scan_last_bytes = self._scan_download_bytes()
        self._scan_last_time = time.time()
        # hf_hub_download/snapshot_download 会等待 .lock；上一次被强制关闭的
        # 下载进程可能留下锁定文件，使新任务永远停在 “Still waiting to acquire
        # lock”。在真正下载前清理所有候选缓存目录中的残留锁。
        for scan_dir in self._scan_dirs:
            if not os.path.isdir(scan_dir):
                continue
            for lock_file in glob.glob(
                    os.path.join(scan_dir, "**", "*.lock"), recursive=True):
                try:
                    os.remove(lock_file)
                    self.send_log(
                        "WARNING",
                        f"Removed leftover lock file to prevent deadlock: {lock_file}")
                except OSError as e:
                    self.send_log(
                        "WARNING",
                        f"Cannot remove lock file {lock_file}: {e}")
        hf_logger = hf_logging.get_logger()
        hf_logging.set_verbosity_info()

        class HFLogHandler(logging.Handler):
            def __init__(self, task):
                super().__init__()
                self.task = task
                self.setFormatter(logging.Formatter('%(message)s'))

            def emit(self, record):
                msg = self.format(record)
                if hasattr(self.task, "logger") and self.task.logger:
                    self.task.logger.log(record.levelno, f"{msg}")
                if "acquire lock" in msg or "lock" in msg.lower():
                    self.task._on_download_progress(
                        -1, "Waiting for another download process to release the model lock...")
                if record.levelno >= logging.INFO:
                    self.task.send_log(record.levelname, f"{msg}")

        for h in hf_logger.handlers[:]:
            if isinstance(h, HFLogHandler):
                hf_logger.removeHandler(h)
        hf_logger.addHandler(HFLogHandler(self))
        os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "0"
        # hf_transfer 更快，但读超时不可控；网络半开时会永久停在 27%。
        # 改用 requests 下载路径，配合 HF_HUB_DOWNLOAD_TIMEOUT 才能失败/重试。
        os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
        # 预先初始化变量，防止作用域错误
        has_remote_onnx = False
        target_files = []
        total_bytes = 0
        try:
            self.send_log("INFO", f"[{repo_id}] Fetching repository metadata...")
            self.queue.put({"state": TaskState.PROCESSING.value, "progress": -1, "msg": "Fetching model metadata..."})
            api = HfApi(endpoint=actual_endpoint)
            repo_info = api.repo_info(repo_id=repo_id, files_metadata=True,
                                      timeout=15)
            has_remote_onnx = any(f.rfilename.endswith(".onnx") for f in repo_info.siblings)
            if has_remote_onnx:
                # --- 场景 A：远程仓库包含 ONNX，直接下载 ONNX，跳过权重下载和转换 ---
                self.send_log("INFO",
                              f"[{repo_id}] Detected ONNX format in repository. Skipping large model weights download.")
                self.queue.put({
                    "state": TaskState.PROCESSING.value,
                    "progress": -1,
                    "msg": f"Detected remote ONNX. Downloading optimized model directly..."
                })
            else:
                # --- 场景 B：远程仓库无 ONNX，走原有的 safetensors 下载逻辑 ---
                has_safetensors = any(f.rfilename.endswith(".safetensors") for f in repo_info.siblings)
                for f in repo_info.siblings:
                    fn = f.rfilename
                    if fn.endswith(".onnx") or fn.endswith(".h5") or fn.endswith(".msgpack"):
                        continue
                    if has_safetensors and fn.endswith(".bin") and not fn.startswith("openvino"):
                        continue
                    target_files.append(f)
                total_bytes = sum(f.size for f in target_files if f.size)
                self.send_log("INFO",
                              f"[{repo_id}] Target files: {len(target_files)}, Size: {total_bytes / (1024 ** 2):.2f} MB")
                for idx, f in enumerate(target_files):
                    file_name = f.rfilename
                    self.queue.put({
                        "state": TaskState.PROCESSING.value,
                        "progress": -1,
                        "msg": f"Downloading: {file_name} ({idx + 1}/{len(target_files)})..."
                    })
                    with DownloadCapture(self._on_download_progress):
                        last_error = None
                        for attempt in range(5):
                            try:
                                hf_hub_download(
                                    repo_id=repo_id,
                                    filename=file_name,
                                    etag_timeout=15,
                                )
                                last_error = None
                                break
                            except Exception as e:
                                last_error = e
                                self.send_log(
                                    "WARNING",
                                    f"Download attempt {attempt + 1}/5 failed for "
                                    f"{file_name}: {e}")
                                if attempt < 4:
                                    delay = min(2 ** attempt, 10)
                                    self._on_download_progress(
                                        -1,
                                        f"- {file_name} · retrying in {delay}s")
                                    time.sleep(delay)
                        if last_error is not None:
                            raise last_error
            # === 统一处理 ONNX 确保逻辑 ===
            self.send_log("INFO", f"Download Finished: {repo_id}. Checking ONNX status...")
            if self.is_cancelled():
                raise InterruptedError("Download was cancelled by user before ONNX conversion.")
            try:
                physical_cores = psutil.cpu_count(logical=False) or multiprocessing.cpu_count() - 1
                os.environ["OMP_NUM_THREADS"] = str(physical_cores)
                os.environ["MKL_NUM_THREADS"] = str(physical_cores)
                os.environ["OPENBLAS_NUM_THREADS"] = str(physical_cores)
                self.send_log("INFO",
                              f"CPU Engine Optimizer: Using {physical_cores} physical cores.")
            except Exception as e:
                self.send_log("WARNING", f"Could not optimize CPU threads: {e}")
            from src.core.models_registry import ensure_onnx_model
            # 将 has_remote_onnx 传递给 registry，告知其无需转换（如果本地缺失的话）
            with DownloadCapture(self._on_download_progress):
                ensure_onnx_model(repo_id, onnx_files_available=has_remote_onnx)
            self.send_log("INFO", f"ONNX Complete: {repo_id}")
        except Exception as e:
            self.send_log("ERROR", f"Download Error: {str(e)}")
            raise e
        finally:
            try:
                self._watch_stop.set()
            except Exception:
                pass