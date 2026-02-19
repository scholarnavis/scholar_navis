import torch
from sentence_transformers import CrossEncoder
from src.core.config_manager import ConfigManager
from src.core.device_manager import DeviceManager
import logging

from src.core.models_registry import resolve_auto_model, get_model_conf


class RerankEngine:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(RerankEngine, cls).__new__(cls)
            cls._instance.logger = logging.getLogger("RerankEngine")
            cls._instance.model = None
            cls._instance.device = "cpu"
            cls._instance.config = ConfigManager()
            cls._instance.dev_mgr = DeviceManager()
        return cls._instance

    def load_model(self):
        """按需加载重排序模型"""

        # 如果已经加载，直接返回，避免重复加载
        if self.model is not None:
            return

        try:
            user_pref = self.config.user_settings.get("inference_device", "Auto")
            self.device = self.dev_mgr.parse_device_string(user_pref)

            rerank_id = self.config.user_settings.get("rerank_model_id", "rerank_auto")

            if rerank_id == "rerank_auto":
                rerank_id = resolve_auto_model("reranker", self.device)

            #  从 Registry 获取详细配置
            r_conf = get_model_conf(rerank_id, "reranker")
            if not r_conf or 'hf_repo_id' not in r_conf:
                raise ValueError(f"Invalid reranker configuration for {rerank_id}")

            actual_repo_id = r_conf['hf_repo_id']
            trust_remote = r_conf.get('trust_remote_code', True)

            self.logger.info(f"Loading Reranker Model ({actual_repo_id}) on {self.device}...")

            model_kwargs = {
                "torch_dtype": torch.float16 if "cuda" in str(self.device) else torch.float32
            }

            self.model = CrossEncoder(
                model_name_or_path=actual_repo_id,
                device=self.device,
                trust_remote_code=trust_remote,
                model_kwargs=model_kwargs
            )
            self.logger.info("Reranker loaded successfully.")
        except Exception as e:
            self.logger.error(f"Failed to load Reranker: {e}")
            self.model = None

    def rerank(self, query, documents, domain="General", top_k=8):
        self.load_model()

        if not self.model or not documents:
            return documents[:top_k]

        if domain and domain != "General":
            augmented_query = f"[{domain} Context] {query}"
        else:
            augmented_query = query

        # 构造 Pair
        pairs = [[augmented_query, doc.get('content', '')] for doc in documents]

        try:
            scores = self.model.predict(pairs)

            for i, doc in enumerate(documents):
                doc['score'] = float(scores[i])

            ranked_docs = sorted(documents, key=lambda x: x.get('score', 0), reverse=True)

            return ranked_docs[:top_k]
        except Exception as e:
            self.logger.error(f"Reranking failed: {e}")
            # 发生错误时，做降级处理：返回未排序的前 k 个
            return documents[:top_k]