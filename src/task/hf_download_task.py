import glob
import logging
import multiprocessing
import os
import threading
import time
import psutil
import tqdm
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


def patched_display(self, msg=None, pos=None):
    global _last_emit_time, _global_callback
    res = _original_display(self, msg, pos)

    if _global_callback:
        with _tqdm_lock:
            current_time = time.time()
            is_finished = (self.n >= self.total) if self.total else False

            if is_finished or (current_time - _last_emit_time >= 0.1):
                _last_emit_time = current_time

                percent = int((self.n / self.total) * 100) if (self.total and self.total > 0) else 0

                desc = str(self.desc) if self.desc else "Processing"

                if "Fetching" in desc or "files" in desc.lower():
                    display_msg = f"📦 {desc} ({self.n}/{self.total})"
                else:
                    clean_name = desc.replace("Downloading ", "")
                    display_msg = f"⬇ {clean_name}"

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
        if os.path.exists(cache_dir):
            for lock_file in glob.glob(os.path.join(cache_dir, "**", "*.lock"), recursive=True):
                try:
                    os.remove(lock_file)
                    self.send_log("WARNING", f"Removed leftover lock file to prevent deadlock: {lock_file}")
                except Exception as e:
                    pass

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
                if record.levelno >= logging.INFO:
                    self.task.send_log(record.levelname, f"{msg}")

        for h in hf_logger.handlers[:]:
            if isinstance(h, HFLogHandler):
                hf_logger.removeHandler(h)
        hf_logger.addHandler(HFLogHandler(self))

        os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "0"
        os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"

        try:
            self.send_log("INFO", f"[{repo_id}] Fetching repository metadata...")
            self.queue.put({"state": TaskState.PROCESSING.value, "progress": -1, "msg": "Fetching model metadata..."})

            api = HfApi(endpoint=actual_endpoint)

            repo_info = api.repo_info(repo_id=repo_id, files_metadata=True)
            has_safetensors = any(f.rfilename.endswith(".safetensors") for f in repo_info.siblings)

            target_files = []
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

            completed_bytes = 0
            _original_update = tqdm.tqdm.update
            tqdm_lock = threading.Lock()
            last_emit_time = [0.0]
            start_time = time.time()
            total_downloaded_this_session = [0]

            for idx, f in enumerate(target_files):
                file_name = f.rfilename
                file_size = f.size or 0

                self.queue.put({
                    "state": TaskState.PROCESSING.value,
                    "progress": -1,
                    "msg": f"🔍 Checking or Queuing: {file_name} ({idx + 1}/{len(target_files)})"
                })

                def patched_update(tqdm_instance, n=1):
                    res = _original_update(tqdm_instance, n)
                    unit = getattr(tqdm_instance, 'unit', '').lower()

                    if 'b' in unit:
                        with tqdm_lock:
                            total_downloaded_this_session[0] += n
                            current_time = time.time()

                            if current_time - last_emit_time[0] >= 0.1:
                                last_emit_time[0] = current_time

                                current_file_bytes = getattr(tqdm_instance, 'n', 0)
                                total_progress_bytes = completed_bytes + current_file_bytes

                                percent = int((total_progress_bytes / total_bytes) * 100) if total_bytes > 0 else 0
                                percent = min(100, max(0, percent))

                                if percent <= 0:
                                    percent = -1

                                elapsed = current_time - start_time
                                speed_bps = total_downloaded_this_session[0] / elapsed if elapsed > 0 else 0
                                speed_mbps = speed_bps / (1024 * 1024)

                                desc = str(getattr(tqdm_instance, 'desc', file_name)).replace("Downloading ", "")
                                display_msg = f"⬇ {desc} ({idx + 1}/{len(target_files)}) | {speed_mbps:.1f} MB/s"

                                self.queue.put({
                                    "state": TaskState.PROCESSING.value,
                                    "progress": percent,
                                    "msg": display_msg
                                })
                    return res

                tqdm.tqdm.update = patched_update
                try:
                    hf_hub_download(
                        repo_id=repo_id,
                        filename=file_name,
                        resume_download=True,
                    )
                finally:
                    tqdm.tqdm.update = _original_update

                completed_bytes += file_size

            self.send_log("INFO", f"Download Finished: {repo_id}. Starting ONNX Conversion...")

            self.queue.put({
                "state": TaskState.PROCESSING.value,
                "progress": -1,
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