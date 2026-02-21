# 修改 src/core/model_manager.py
import logging
import os
from src.core.models_registry import get_model_conf, check_model_exists, resolve_auto_model
from src.core.signals import GlobalSignals
from src.core.device_manager import DeviceManager
from src.core.config_manager import ConfigManager


class ModelManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ModelManager, cls).__new__(cls)
            cls._instance.logger = logging.getLogger("ModelManager")
            cls._instance.dev_mgr = DeviceManager()
        return cls._instance

    def verify_chat_models(self, kb_id):
        """
        在对话开始前执行强力自检：
        1. 检查该知识库绑定的 Embedding 模型是否已下载。
        2. 检查全局设置中选用的 Reranker 模型是否已下载。
        """
        from src.core.kb_manager import KBManager
        from src.core.config_manager import ConfigManager

        config = ConfigManager()
        dev = self.dev_mgr.get_optimal_device()

        # --- 校验 A: Embedding 模型 ---
        kb_info = KBManager().get_kb_by_id(kb_id)
        embed_id = kb_info.get('model_id', 'embed_auto') if kb_info else 'embed_auto'
        if embed_id == "embed_auto":
            embed_id = resolve_auto_model("embedding", dev)

        e_conf = get_model_conf(embed_id, "embedding")
        if not e_conf or not check_model_exists(e_conf.get('hf_repo_id')):
            ui_name = e_conf.get('ui_name', embed_id) if e_conf else embed_id
            return False, f"Embedding Model ({ui_name})", embed_id, "embedding"

        # --- 校验 B: Reranker 模型 ---
        rerank_id = config.user_settings.get("rerank_model_id", "rerank_auto")
        if rerank_id == "rerank_auto":
            rerank_id = resolve_auto_model("reranker", dev)

        r_conf = get_model_conf(rerank_id, "reranker")
        # Reranker 如果没选或者选了 Auto 但解析失败，通常可以跳过，但如果显式选了却没下载，则拦截
        if r_conf and not check_model_exists(r_conf.get('hf_repo_id')):
            ui_name = r_conf.get('ui_name', rerank_id)
            return False, f"Reranker Model ({ui_name})", rerank_id, "reranker"

        return True, None, None, None