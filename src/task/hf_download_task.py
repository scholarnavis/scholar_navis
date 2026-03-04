import glob
import multiprocessing
import os
import threading
import time
import psutil
import tqdm
from huggingface_hub.constants import default_cache_path

from src.core.core_task import BackgroundTask, TaskState
from src.core.network_worker import setup_global_network_env

_tqdm_lock = threading.Lock()

# 注意：请勿在此处全局 import huggingface_hub 或 torch！
# 必须等待网络环境和线程环境变量设置完毕后再进行局部 import。


_global_callback = None
_last_emit_time = 0
_EMIT_INTERVAL = 0.1
_original_display = tqdm.std.tqdm.display


def patched_display(self, msg=None, pos=None):
    global _last_emit_time, _global_callback
    res = _original_display(self, msg, pos)

    if _global_callback:
        with _tqdm_lock:
            current_time = time.time()
            is_finished = (self.n >= self.total) if self.total else False
            if is_finished or (current_time - _last_emit_time >= _EMIT_INTERVAL):
                _last_emit_time = current_time

            percent = 0
            if self.total and self.total > 0:
                percent = int((self.n / self.total) * 100)

            desc = self.desc if self.desc else "Processing..."

            if "files" in desc:
                display_msg = f"{desc}: {self.n}/{self.total}"
            else:
                display_msg = f"⬇{desc}"

            _global_callback(percent, display_msg)

    return res


class DownloadCapture:
    def __init__(self, callback):
        self.callback = callback

    def __enter__(self):
        global _global_callback
        _global_callback = self.callback

        tqdm.std.tqdm.display = patched_display
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        tqdm.std.tqdm.display = _original_display
        global _global_callback
        _global_callback = None


class RealTimeHFDownloadTask(BackgroundTask):
    def _execute(self):
        setup_global_network_env()
        repo_id = self.kwargs.get("repo_id")

        cache_dir = os.path.join(default_cache_path, "models--" + repo_id.replace("/", "--"))
        if os.path.exists(cache_dir):
            for lock_file in glob.glob(os.path.join(cache_dir, "**", "*.lock"), recursive=True):
                try:
                    os.remove(lock_file)
                except:
                    pass


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

        from huggingface_hub import snapshot_download
        import huggingface_hub.utils.logging as hf_logging
        import logging

        # --- 接管 HuggingFace 的内部日志 ---
        hf_logger = hf_logging.get_logger()
        hf_logging.set_verbosity_info()  # 开启 INFO 级别以记录网络请求和重试

        class HFLogHandler(logging.Handler):
            def __init__(self, task):
                super().__init__()
                self.task = task
                self.setFormatter(logging.Formatter('%(message)s'))

            def emit(self, record):
                msg = self.format(record)
                if hasattr(self.task, "queue"):
                    self.task.queue.put({
                        "type": "system_log",
                        "level": record.levelname,
                        "msg": f"[HF Hub] {msg}"
                    })


                self.task.send_log(record.levelname, f"HF: {msg}")

        for h in hf_logger.handlers[:]:
            if isinstance(h, HFLogHandler):
                hf_logger.removeHandler(h)

        hf_logger.addHandler(HFLogHandler(self))

        os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "0"
        os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"

        def tqdm_callback(percent, msg):
            self.queue.put({
                "state": TaskState.PROCESSING.value,
                "progress": percent,
                "msg": f"[{repo_id}] {msg}"
            })

        try:
            self.send_log("INFO", f"Downloading {repo_id}...")

            with DownloadCapture(tqdm_callback):
                snapshot_download(
                    repo_id=repo_id,
                    max_workers=8,
                )
            self.send_log("INFO", f"Download Finished: {repo_id}. Starting ONNX Conversion...")

            self.queue.put({
                "state": TaskState.PROCESSING.value,
                "progress": 99,
                "msg": f"[{repo_id}] Converting to ONNX format (First time only)..."
            })

            try:
                physical_cores = psutil.cpu_count(logical=False) or multiprocessing.cpu_count() - 1

                os.environ["OMP_NUM_THREADS"] = str(physical_cores)
                os.environ["MKL_NUM_THREADS"] = str(physical_cores)
                os.environ["OPENBLAS_NUM_THREADS"] = str(physical_cores)

                import torch
                torch.set_num_threads(physical_cores)
                torch.set_num_interop_threads(physical_cores)

                self.send_log("INFO",
                              f"CPU Engine Optimizer: Using {physical_cores} physical cores for maximum conversion speed.")
            except Exception as e:
                self.send_log("WARNING", f"Could not optimize CPU threads: {e}")

            from src.core.models_registry import ensure_onnx_model
            ensure_onnx_model(repo_id)

            self.send_log("INFO", f"ONNX Complete: {repo_id}")

        except Exception as e:
            self.send_log("ERROR", f"Download Error: {str(e)}")
            raise e