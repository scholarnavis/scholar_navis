import logging
import threading

from src.core.config_manager import ConfigManager
from src.core.device_manager import DeviceManager
from src.core.models_registry import resolve_auto_model, get_model_conf, ensure_onnx_model, \
    check_model_exists


class RerankEngine:
    """本地 ONNX 交叉编码器重排引擎（进程级单例）。

    关键设计：
    - 重量级第三方库（transformers / optimum.onnxruntime / onnxruntime）只在
      真正需要加载模型时才导入，避免拖慢纯 LLM 对话的进程启动。
    - 每个进程内最多尝试加载一次（成功或失败均记录状态），避免反复触发
      transformers/optimum 这类耗时的导入。
    - 加载互斥由 _model_lock 保证，防止预加载线程与查询线程并发重复加载。
    """

    _instance = None

    # 加载状态机：idle(未尝试) -> loading(进行中) -> ready / failed(终态)
    LOAD_IDLE = "idle"
    LOAD_LOADING = "loading"
    LOAD_READY = "ready"
    LOAD_FAILED = "failed"

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(RerankEngine, cls).__new__(cls)
            cls._instance.logger = logging.getLogger("RerankEngine")
            cls._instance.model = None
            cls._instance.tokenizer = None
            cls._instance.device = "cpu"
            cls._instance.config = ConfigManager()
            cls._instance.dev_mgr = DeviceManager()
            cls._instance._inference_lock = threading.Lock()
            cls._instance._model_lock = threading.Lock()
            cls._instance._load_cond = threading.Condition(cls._instance._model_lock)
            cls._instance._load_state = RerankEngine.LOAD_IDLE
            cls._instance._last_error = None
        return cls._instance

    # ---------- 加载 ----------

    def preload(self):
        """后台线程预加载重排模型，避免首次查询阻塞 UI。失败留待惰性重试（不反复导入）。"""
        try:
            self.load_model()
        except Exception as e:  # load_model 内部已吞错并置 failed；此处仅兜底
            self.logger.warning(f"Preload reranker failed (will retry lazily): {e}")
        return self.model is not None

    def is_ready(self) -> bool:
        return self.model is not None and self.tokenizer is not None

    def load_model(self):
        """确保模型已加载。线程安全；同进程内失败后不会再次触发重量级加载。"""
        with self._load_cond:
            if self.is_ready():
                return
            if self._load_state == self.LOAD_LOADING:
                # 已有线程正在加载，等它结束即可（避免重复导入）
                self._load_cond.wait()
                return
            if self._load_state == self.LOAD_FAILED:
                return
            self._load_state = self.LOAD_LOADING

        # 单飞加载：LOADING 标记保证同一进程内只有一个线程执行重量级导入 + 建
        # session，其余线程在此等待或直接复用结果，绝不并发双份导入。
        self._load_model_locked()

    def _resolve_device_and_model(self):
        import onnxruntime as ort

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

        if not check_model_exists(actual_repo_id):
            ui_name = r_conf.get('ui_name', actual_repo_id)
            raise FileNotFoundError(
                f"Reranker model '{ui_name}' is not downloaded. "
                f"Please download it manually from Settings → AI Models.")

        # allow_download=False：即便缓存不完整也绝不自动联网下载，
        # 缺失/损坏都抛错引导用户去设置手动下载。
        onnx_dir = ensure_onnx_model(actual_repo_id, "reranker", allow_download=False)

        import os
        if not onnx_dir or not os.path.exists(onnx_dir):
            raise FileNotFoundError("Reranker model directory not found.")

        available_providers = ort.get_available_providers()
        provider = "CPUExecutionProvider"
        provider_options = None
        device_str = str(self.device).lower()

        if device_str.startswith("cuda") and "CUDAExecutionProvider" in available_providers:
            provider = "CUDAExecutionProvider"
            if ":" in device_str:
                provider_options = {"device_id": int(device_str.split(":")[1])}
        elif device_str.startswith("dml") and "DmlExecutionProvider" in available_providers:
            provider = "DmlExecutionProvider"
            if ":" in device_str:
                provider_options = {"device_id": int(device_str.split(":")[1])}
        elif device_str.startswith("rocm") and "ROCmExecutionProvider" in available_providers:
            provider = "ROCmExecutionProvider"
            if ":" in device_str:
                provider_options = {"device_id": int(device_str.split(":")[1])}
        elif device_str.startswith("coreml") and "CoreMLExecutionProvider" in available_providers:
            provider = "CoreMLExecutionProvider"

        return onnx_dir, provider, provider_options

    def _load_model_locked(self):
        try:
            from optimum.onnxruntime import ORTModelForSequenceClassification
            from transformers import AutoTokenizer

            onnx_dir, provider, provider_options = self._resolve_device_and_model()

            kwargs = {"provider": provider}
            if provider_options:
                kwargs["provider_options"] = provider_options

            self.tokenizer = AutoTokenizer.from_pretrained(onnx_dir)
            self.model = ORTModelForSequenceClassification.from_pretrained(
                onnx_dir,
                export=False,
                **kwargs
            )

            actual_providers = self.model.providers
            if provider != "CPUExecutionProvider" and actual_providers and actual_providers[
                0] == "CPUExecutionProvider":
                fallback_msg = f"Hardware acceleration failed! Requested '{provider}' but ONNX Runtime silently fell back to 'CPUExecutionProvider'. Please check your GPU drivers."
                self.logger.error(fallback_msg)
            else:
                self.logger.info(f"ONNX Reranker loaded successfully on {provider}.")

            self._last_error = None
            with self._load_cond:
                self._load_state = self.LOAD_READY
                self._load_cond.notify_all()

        except Exception as e:
            self.logger.error(f"Failed to load ONNX Reranker: {e}")
            self._last_error = str(e)
            self.model = None
            self.tokenizer = None
            with self._load_cond:
                self._load_state = self.LOAD_FAILED
                self._load_cond.notify_all()

    # ---------- 推理 ----------

    def rerank(self, query, documents, domain="General", top_k=8):
        if not documents:
            return []
        # 已加载则直接用；未加载则触发一次（失败后同进程内不再重复加载）。
        self.load_model()
        if not self.is_ready():
            return documents[:top_k]

        augmented_query = f"[{domain} Context] {query}" if domain and domain != "General" else query
        pairs = [[augmented_query, doc.get('content', '')] for doc in documents]

        try:
            inputs = self.tokenizer(pairs, padding=True, truncation=True, return_tensors='pt', max_length=512)
            with self._inference_lock:
                logits = self.model(**inputs).logits

            # (N,1) 二分类交叉编码器得分；.squeeze(-1) 比 .view(-1) 对 batch 语义更明确
            if logits.shape[1] == 1:
                scores = logits.squeeze(-1).detach().numpy()
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
