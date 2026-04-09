import os
import platform

from src.core.config_manager import ConfigManager
from src.core.core_task import BackgroundTask
from src.core.device_manager import DeviceManager
from src.core.models_registry import resolve_auto_model, get_model_conf
from src.core.network_worker import create_robust_session
from src.core.version import __latest__


class VersionCheckTask(BackgroundTask):
    def _execute(self):
        try:
            session = create_robust_session()

            os_name = platform.system().lower()

            url = f"{__latest__}?os={os_name}"

            self.logger.debug(f"Checking for updates at: {url}")

            response = session.get(url, timeout=15)

            if response.status_code == 200:
                latest_version = response.text.strip()
                self.logger.info(f"Successfully fetched latest version: {latest_version}")
                return {"latest_version": latest_version}
            else:
                self.logger.warning(
                    f"Update check failed with status code: {response.status_code}")
        except Exception as e:
            self.logger.error(f"Failed to check for updates: {e}")


class VerifyModelsTask(BackgroundTask):
    def _check_local_onnx_exists(self, repo_id):
        """核心探测逻辑：严格检查 /models 下的对应目录是否有 .onnx 文件"""
        if not repo_id:
            self.logger.warning("VerifyModelsTask: Model check skipped - no repo_id provided.")
            return False

        repo_folder = f"models--{repo_id.replace('/', '--')}"
        model_dir = os.path.join(ConfigManager().BASE_DIR, "models", repo_folder)

        self.logger.info(f"VerifyModelsTask: Searching for ONNX model '{repo_id}' at expected path '{model_dir}'")

        if not os.path.exists(model_dir):
            self.logger.error(f"VerifyModelsTask: ONNX check failed. Directory missing: '{model_dir}'")
            return False

        for root, dirs, files in os.walk(model_dir):
            if self.is_cancelled():
                raise InterruptedError("Verification safely terminated by user.")

            if any(f.endswith('.onnx') for f in files):
                self.logger.info(f"VerifyModelsTask: ONNX files successfully verified for '{repo_id}' at '{root}'")
                return True

        self.logger.warning(
            f"VerifyModelsTask: ONNX check failed. Directory exists but no .onnx files found for '{repo_id}'")
        return False

    def _execute(self):
        self.update_progress(10, "Verifying hardware and AI model files (ONNX)...")

        embed_id = self.kwargs.get('embed_id')
        rerank_id = self.kwargs.get('rerank_id')

        dev = DeviceManager().get_optimal_device()

        real_embed = embed_id
        if real_embed == "embed_auto": real_embed = resolve_auto_model("embedding", dev)

        real_rerank = rerank_id
        if real_rerank == "rerank_auto": real_rerank = resolve_auto_model("reranker", dev)

        to_download = []

        # 修改点：在检查嵌入模型前更新进度条状态
        self.update_progress(30, f"Checking Embedding model: {real_embed}...")
        e_conf = get_model_conf(real_embed, "embedding")
        if e_conf and not e_conf.get('is_network', False):
            if not self._check_local_onnx_exists(e_conf.get('hf_repo_id')):
                to_download.append(e_conf['hf_repo_id'])

        # 修改点：在检查重排序模型前更新进度条状态
        self.update_progress(60, f"Checking Reranker model: {real_rerank}...")
        r_conf = get_model_conf(real_rerank, "reranker")
        if r_conf and not r_conf.get('is_network', False):
            if not self._check_local_onnx_exists(r_conf.get('hf_repo_id')):
                to_download.append(r_conf['hf_repo_id'])

        self.update_progress(100, "Verification complete.")

        return {
            "to_download": to_download,
            "embed": {"id": real_embed, "repo_id": e_conf.get('hf_repo_id') if e_conf else None,
                      "is_network": e_conf.get('is_network', False) if e_conf else False},
            "rerank": {"id": real_rerank, "repo_id": r_conf.get('hf_repo_id') if r_conf else None,
                       "is_network": r_conf.get('is_network', False) if r_conf else False}
        }