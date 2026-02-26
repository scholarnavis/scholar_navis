import os
import time

import tqdm
from huggingface_hub import snapshot_download, constants
from src.core.core_task import BackgroundTask, TaskState
from src.core.config_manager import ConfigManager
from src.core.network_worker import setup_global_network_env

_global_callback = None
_last_emit_time = 0
_EMIT_INTERVAL = 0.1
_original_display = tqdm.std.tqdm.display


def patched_display(self, msg=None, pos=None):
    global _last_emit_time, _global_callback

    res = _original_display(self, msg, pos)

    # 我们的拦截逻辑
    if _global_callback:
        current_time = time.time()
        is_finished = (self.n >= self.total) if self.total else False


        if is_finished or (current_time - _last_emit_time >= _EMIT_INTERVAL):
            _last_emit_time = current_time

            percent = 0
            if self.total and self.total > 0:
                percent = int((self.n / self.total) * 100)

            desc = self.desc if self.desc else "Processing..."

            if "files" in desc:
                display_msg = f"📦 {desc}: {self.n}/{self.total}"
            else:
                display_msg = f"⬇️ {desc}"

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


        os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "0"
        os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"

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
                    resume_download=True,
                    max_workers=8,
                )
            self.send_log("INFO", f"Download Finished: {repo_id}. Starting ONNX Conversion...")

            # 自动启动 ONNX 永久转换，并在 Dialog 提示
            self.queue.put({
                "state": TaskState.PROCESSING.value,
                "progress": 99,
                "msg": f"[{repo_id}] Converting to ONNX format (First time only)..."
            })
            from src.core.models_registry import ensure_onnx_model
            ensure_onnx_model(repo_id)

            self.send_log("INFO", f"ONNX Complete: {repo_id}")

        except Exception as e:
            self.send_log("ERROR", f"Download Error: {str(e)}")
            raise e