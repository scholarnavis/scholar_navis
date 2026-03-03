import logging
from optimum.onnxruntime import ORTModelForSequenceClassification
from transformers import AutoTokenizer

from src.core.config_manager import ConfigManager
from src.core.device_manager import DeviceManager
from src.core.models_registry import resolve_auto_model, get_model_conf, ensure_onnx_model


class RerankEngine:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(RerankEngine, cls).__new__(cls)
            cls._instance.logger = logging.getLogger("RerankEngine")
            cls._instance.model = None
            cls._instance.tokenizer = None
            cls._instance.device = "cpu"
            cls._instance.config = ConfigManager()
            cls._instance.dev_mgr = DeviceManager()
        return cls._instance

    def load_model(self):
        if self.model is not None:
            return

        try:
            user_pref = self.config.user_settings.get("inference_device", "Auto")
            self.device = self.dev_mgr.parse_device_string(user_pref)

            rerank_id = self.config.user_settings.get("rerank_model_id", "rerank_auto")
            if rerank_id == "rerank_auto":
                rerank_id = resolve_auto_model("reranker", self.device)

            r_conf = get_model_conf(rerank_id, "reranker")
            if not r_conf or 'hf_repo_id' not in r_conf:
                raise ValueError(f"Invalid reranker configuration for {rerank_id}")

            actual_repo_id = r_conf['hf_repo_id']
            self.logger.info(f"Loading local ONNX Reranker ({actual_repo_id})...")

            # 保险措施，获取已转换好的文件夹
            onnx_dir = ensure_onnx_model(actual_repo_id, "reranker")
            provider = "CUDAExecutionProvider" if "cuda" in str(self.device) else "CPUExecutionProvider"

            self.tokenizer = AutoTokenizer.from_pretrained(onnx_dir)
            self.model = ORTModelForSequenceClassification.from_pretrained(
                onnx_dir,
                export=False,
                provider=provider
            )
            self.logger.info("ONNX Reranker loaded instantly from disk.")
        except Exception as e:
            self.logger.error(f"Failed to load ONNX Reranker: {e}")
            self.model = None

    def rerank(self, query, documents, domain="General", top_k=8):
        self.load_model()
        if not self.model or not documents: return documents[:top_k]

        augmented_query = f"[{domain} Context] {query}" if domain and domain != "General" else query
        pairs = [[augmented_query, doc.get('content', '')] for doc in documents]

        try:
            inputs = self.tokenizer(pairs, padding=True, truncation=True, return_tensors='pt', max_length=512)
            logits = self.model(**inputs).logits

            if logits.shape[1] == 1:
                scores = logits.view(-1).detach().numpy()
            else:
                import torch.nn.functional as F
                scores = F.softmax(logits, dim=1)[:, 1].detach().numpy()

            for i, doc in enumerate(documents):
                doc['score'] = float(scores[i])

            ranked_docs = sorted(documents, key=lambda x: x.get('score', 0), reverse=True)
            return ranked_docs[:top_k]
        except Exception as e:
            self.logger.error(f"ONNX Reranking failed: {e}")
            return documents[:top_k]