from src.core.core_task import BackgroundTask
from src.core.device_manager import DeviceManager
from src.core.models_registry import resolve_auto_model, get_model_conf, check_model_exists


class VerifySettingsTask(BackgroundTask):
    """标准的系统校验 Task，通过 TaskManager 统一调度"""
    def _execute(self):
        self.update_progress(10, "Verifying hardware and AI model files...")

        embed_id = self.kwargs.get('embed_id')
        rerank_id = self.kwargs.get('rerank_id')

        dev = DeviceManager().get_optimal_device()

        real_embed = embed_id
        if real_embed == "embed_auto": real_embed = resolve_auto_model("embedding", dev)

        real_rerank = rerank_id
        if real_rerank == "rerank_auto": real_rerank = resolve_auto_model("reranker", dev)

        to_download = []
        e_conf = get_model_conf(real_embed, "embedding")
        if e_conf and not e_conf.get('is_network', False) and not check_model_exists(e_conf.get('hf_repo_id')):
            to_download.append(e_conf['hf_repo_id'])

        r_conf = get_model_conf(real_rerank, "reranker")
        if r_conf and not r_conf.get('is_network', False) and not check_model_exists(r_conf.get('hf_repo_id')):
            to_download.append(r_conf['hf_repo_id'])

        self.update_progress(90, "Verification complete.")

        return {
            "to_download": to_download
        }
