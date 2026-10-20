import logging
import os
import threading

from src.core.config_manager import ConfigManager
from src.core.device_manager import DeviceManager
from src.core.models_registry import resolve_auto_model, get_model_conf, ensure_onnx_model, \
    check_model_exists
from src.core.onnx_provider import resolve_provider


class RerankEngine:
    """本地 ONNX 交叉编码器重排引擎（进程级单例）。

    关键设计：
    - 重量级第三方库（tokenizers / onnxruntime）只在真正需要加载模型时
      才导入，避免拖慢纯 LLM 对话的进程启动。
    - 每个进程内最多尝试加载一次（成功或失败均记录状态），避免反复触发
      这类耗时的导入。
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
            cls._instance._raw_session = None
            cls._instance._raw_inputs = set()
            cls._instance._raw_input_shapes = {}
            cls._instance.scoring = "classifier"
            cls._instance.tokenizer_max_length = 512
            cls._instance.query_instruction = ""
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
        return self.model is not None or self._raw_session is not None

    def is_ready(self) -> bool:
        return self.tokenizer is not None and (
            self.model is not None or self._raw_session is not None)

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
        device_str = str(self.device).lower()

        # 统一走 onnx_provider 的真实可用性解析（详见该模块说明）。
        resolved = resolve_provider(device_str)
        provider, provider_options = resolved.provider, resolved.provider_options
        if resolved.degraded:
            self.logger.warning(f"Reranker device degraded to CPU: {resolved.reason}")
        self.scoring = str(r_conf.get("scoring") or "classifier")
        self.tokenizer_max_length = int(r_conf.get("tokenizer_max_length") or 512)
        self.query_instruction = str(r_conf.get("query_instruction") or "")
        self.logger.info(
            f"Reranker provider resolved: requested={device_str} -> {provider} "
            f"(scoring={self.scoring}, build-time providers: {available_providers})")

        return onnx_dir, provider, provider_options

    @staticmethod
    def _find_onnx_file(root):
        # 优先量化文件：onnx-community 的 v2-m3 仓库默认模型带 2GB 外部权重，
        # int8/quantized 版本约 570MB，CPU/GPU 都够用。
        for preferred in ("model_quantized.onnx", "model_int8.onnx",
                          "model_q4f16.onnx", "model.onnx"):
            for base, _dirs, files in os.walk(root):
                if preferred in files:
                    return os.path.join(base, preferred)
        for base, _dirs, files in os.walk(root):
            for name in files:
                if name.endswith(".onnx"):
                    return os.path.join(base, name)
        return ""

    def _load_model_locked(self):
        try:
            import onnxruntime as ort

            from src.core.onnx_tokenizer import OnnxTokenizer

            onnx_dir, provider, _provider_options = self._resolve_device_and_model()
            self.tokenizer = OnnxTokenizer(
                onnx_dir, max_length=self.tokenizer_max_length)
            model_path = self._find_onnx_file(onnx_dir)
            if not model_path:
                raise FileNotFoundError(
                    f"No ONNX reranker found under {onnx_dir}; download an ONNX-ready model.")

            providers = [provider]
            if "CPUExecutionProvider" not in providers:
                providers.append("CPUExecutionProvider")
            try:
                self._raw_session = ort.InferenceSession(model_path, providers=providers)
            except Exception as e:
                if providers[0] != "CPUExecutionProvider":
                    self.logger.warning(
                        "ONNX GPU reranker failed (%s); retrying on CPU.", e)
                    self._raw_session = ort.InferenceSession(
                        model_path, providers=["CPUExecutionProvider"])
                else:
                    raise
            self._raw_inputs = {i.name for i in self._raw_session.get_inputs()}
            self._raw_input_shapes = {i.name: i.shape
                                      for i in self._raw_session.get_inputs()}
            self.logger.info("Reranker uses ONNX Runtime on %s (%s).",
                             self._raw_session.get_providers()[0], model_path)

            self._last_error = None
            with self._load_cond:
                self._load_state = self.LOAD_READY
                self._load_cond.notify_all()

        except Exception as e:
            self.logger.error(f"Failed to load ONNX Reranker: {e}")
            self._last_error = str(e)
            self.model = None
            self.tokenizer = None
            self._raw_session = None
            with self._load_cond:
                self._load_state = self.LOAD_FAILED
                self._load_cond.notify_all()

    # ---------- 推理 ----------

    _QWEN3_SYSTEM = (
        "Judge whether the Document meets the requirements based on the Query "
        'and the Instruct provided. Note that the answer can only be "yes" or "no".')

    def _qwen3_prompt(self, query, content):
        instruction = self.query_instruction or (
            "Given a scientific question, retrieve passages that answer it")
        return (
            f"<|im_start|>system\n{self._QWEN3_SYSTEM}<|im_end|>\n"
            f"<|im_start|>user\n<Instruct>: {instruction}\n\n"
            f"<Query>: {query}\n\n<Document>: {content}<|im_end|>\n"
            "<|im_start|>assistant\n<think>\n\n</think>\n"
        )

    def _rerank_qwen3(self, query, documents, top_k):
        """Qwen3-Reranker：比较最后位置 yes/no logits，而不是分类头。"""
        try:
            import numpy as np

            yes_id = self.tokenizer.token_to_id("yes")
            no_id = self.tokenizer.token_to_id("no")
            if yes_id < 0 or no_id < 0:
                self.logger.error("Qwen3 reranker tokenizer has no yes/no token")
                return documents[:top_k]

            prompts = [self._qwen3_prompt(query, str(d.get("content") or ""))
                       for d in documents]
            enc = self.tokenizer.encode(prompts, padding_side="left")
            ids = np.asarray(enc["input_ids"])
            mask = np.asarray(enc["attention_mask"])
            batch, seq = ids.shape
            feed = {}
            for name in self._raw_inputs:
                if name in enc:
                    feed[name] = np.asarray(enc[name])
                elif name == "position_ids":
                    # 左 padding：前导 pad 的位置设 0，真实 token 从 0 递增。
                    feed[name] = np.maximum(np.cumsum(mask, axis=1) - 1, 0).astype(np.int64)
                elif name.startswith("past_key_values."):
                    shape = self._raw_input_shapes.get(name) or []
                    dims = [d if isinstance(d, int) else (batch if i == 0 else 0)
                            for i, d in enumerate(shape)]
                    if len(dims) >= 3:
                        dims[-2:] = [dims[-2] if isinstance(dims[-2], int) else 8,
                                     dims[-1] if isinstance(dims[-1], int) else 128]
                    feed[name] = np.zeros(tuple(dims), dtype=np.float32)

            with self._inference_lock:
                logits = np.asarray(self._raw_session.run(None, feed)[0])
            if logits.ndim != 3:
                self.logger.error("Qwen3 reranker expected [B,L,V] logits, got %s",
                                  logits.shape)
                return documents[:top_k]
            last = logits[:, -1, :]
            yes = last[:, yes_id].astype(np.float64)
            no = last[:, no_id].astype(np.float64)
            exp_yes = np.exp(yes - np.maximum(yes, no))
            exp_no = np.exp(no - np.maximum(yes, no))
            scores = exp_yes / (exp_yes + exp_no + 1e-12)
            for i, doc in enumerate(documents):
                doc["score"] = float(scores[i])
            return sorted(documents, key=lambda x: x.get("score", 0),
                          reverse=True)[:top_k]
        except Exception as e:
            self.logger.error("Qwen3 reranking failed: %s", e)
            return documents[:top_k]

    def rerank(self, query, documents, domain="General", top_k=8):
        if not documents:
            return []
        # 已加载则直接用；未加载则触发一次（失败后同进程内不再重复加载）。
        self.load_model()
        if not self.is_ready():
            return documents[:top_k]
        if self.scoring == "qwen3_yes_no":
            return self._rerank_qwen3(query, documents, top_k)

        augmented_query = f"[{domain} Context] {query}" if domain and domain != "General" else query
        pairs = [[augmented_query, doc.get('content', '')] for doc in documents]

        try:
            import numpy as np
            enc = self.tokenizer.encode(pairs)
            ort_inputs = {k: np.asarray(v)
                          for k, v in enc.items() if k in self._raw_inputs}
            with self._inference_lock:
                logits = self._raw_session.run(None, ort_inputs)[0]
            logits = np.asarray(logits)
            if logits.ndim == 2 and logits.shape[1] == 1:
                scores = logits[:, 0]
            elif logits.ndim == 2 and logits.shape[1] >= 2:
                exp = np.exp(logits - logits.max(axis=1, keepdims=True))
                scores = (exp / exp.sum(axis=1, keepdims=True))[:, 1]
            else:
                scores = logits.reshape(-1)

            for i, doc in enumerate(documents):
                doc['score'] = float(scores[i])

            ranked_docs = sorted(documents, key=lambda x: x.get('score', 0), reverse=True)
            return ranked_docs[:top_k]
        except Exception as e:
            self.logger.error(f"ONNX Reranking failed: {e}")
            return documents[:top_k]
