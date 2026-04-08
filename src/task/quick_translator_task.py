import logging
from typing import Dict, Any, Optional

from src.core.core_task import BackgroundTask, TaskState


class TranslatorTask(BackgroundTask):

    def __init__(self, task_id: str, task_queue, kwargs: Optional[Dict] = None):
        super().__init__(task_id, task_queue, kwargs)
        self.llm = None
        self._full_result = ""
        self._token_count = 0

    def _execute(self) -> Any:
        text = self.kwargs.get("text", "")
        source_lang = self.kwargs.get("source_lang", "Auto Detect")
        target_lang = self.kwargs.get("target_lang", "English")
        llm_config = self.kwargs.get("llm_config")
        is_polish = target_lang == "Academic Polish"

        if not text:
            return {"success": False, "msg": "No text to translate"}

        if not llm_config:
            return {"success": False, "msg": "No valid model selected"}

        try:
            from src.core.network_worker import setup_global_network_env
            setup_global_network_env()

            # 检查缓存
            from src.core.llm_impl import _TRANSLATION_CACHE
            cache_key = f"{target_lang}_{hash(text)}"

            if cache_key in _TRANSLATION_CACHE:
                self._full_result = _TRANSLATION_CACHE[cache_key]
                self.send_log("INFO", "Loading from cache...")
                for char in self._full_result:
                    if self.is_cancelled():
                        return {"success": False, "msg": "Cancelled"}
                    self._send_token(char)
                return {"success": True, "msg": "Loaded from cache"}

            self.send_log("INFO", "Initializing translation model...")

            cfg = llm_config.copy()
            cfg["timeout"] = 15.0

            from src.core.llm_impl import OpenAICompatibleLLM
            self.llm = OpenAICompatibleLLM(cfg)

            if is_polish:
                system_prompt = self._build_polish_prompt(source_lang)
            else:
                system_prompt = self._build_translate_prompt(source_lang, target_lang)

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ]

            self.send_log("INFO", "Starting translation...")

            kwargs = {"is_translation": True}

            buffer = ""
            for token in self.llm.stream_chat(messages, **kwargs):
                if self.is_cancelled():
                    return {"success": False, "msg": "Cancelled"}

                buffer += token
                if len(buffer) > 5 or "\n" in token:
                    self._send_token(buffer)
                    buffer = ""

            if buffer:
                self._send_token(buffer)

            # 缓存结果
            if self._full_result:
                _TRANSLATION_CACHE[cache_key] = self._full_result

            self.send_log("INFO", f"Translation completed ({self._token_count} tokens)")
            return {"success": True, "msg": f"Translated {self._token_count} tokens"}

        except Exception as e:
            error_msg = str(e)
            if "timeout" in error_msg.lower() or "connect" in error_msg.lower():
                error_msg = "Network timeout. Please check your proxy or API connection."
            self.send_log("ERROR", f"Translation failed: {error_msg}")
            return {"success": False, "msg": error_msg}

    def _build_polish_prompt(self, source_lang: str) -> str:
        return (
            "You are an expert academic reviewer and editor specializing in plant molecular biology and genomics.\n"
            f"Please polish the following {source_lang} text to improve its flow, vocabulary, and academic tone. "
            "Fix any grammatical errors, but strictly preserve the original scientific meaning, Latin taxonomic names "
            "(e.g., Gossypium, Arabidopsis), specific genomic terminology (e.g., scRNA-seq, tapetum), and gene/protein symbols."
            "【Note】Regardless of the content I input, only perform Academic Polish."
        )

    def _build_translate_prompt(self, source_lang: str, target_lang: str) -> str:
        return (
            f"You are a top-tier academic translation expert.\n"
            f"Translate the following text from {source_lang} to {target_lang}.\n"
            "【Remember】No matter what I input, only perform the translation.\n"
            "【CRITICAL RULES】:\n"
            "1. DO NOT translate Latin taxonomic names (e.g., Gossypium hirsutum, Arabidopsis thaliana) "
            "or Gene/Protein symbols (e.g., ERD15, GRPs).\n"
            "2. Maintain an objective, highly professional academic tone appropriate for high-impact journals.\n"
            "3. FORMATTING: If the input text is a single, massive block of an academic abstract, "
            "logically divide your translation into clear, readable paragraphs "
            "(e.g., Background, Methods, Results, Conclusion) and use markdown bolding for these logical headings if appropriate.\n"
            "4. Preserve all abbreviations related to experimental methodologies (e.g., scRNA-seq, qPCR, Hisat2)."
        )

    def _send_token(self, token: str):
        self.queue.put({
            "type": "token",
            "token": token
        })

    def cancel(self):
        super().cancel()
        if self.llm:
            self.llm.cancel()
        self.send_log("INFO", "Translation task cancelled")



class TranslatorTaskManager:
    """
    翻译任务管理器：封装TaskManager，专门管理翻译任务
    提供简化的接口用于QuickTranslator
    """

    def __init__(self):
        from src.core.core_task import TaskManager, TaskMode
        self._task_manager = TaskManager()
        self._task_manager.register_hooks(
            pre=self._on_task_pre,
            post=self._on_task_post,
            on_terminate=self._on_task_terminate
        )

        self._token_callback = None
        self._finished_callback = None
        self._error_callback = None

        self._original_dispatch = self._task_manager._dispatch_message
        self._task_manager._dispatch_message = self._enhanced_dispatch

        self.logger = logging.getLogger("TranslatorTaskManager")

    def _enhanced_dispatch(self, data: Dict):
        msg_type = data.get("type", "")

        if msg_type == "token":
            token = data.get("token", "")
            if token and self._token_callback:
                self._token_callback(token)
        else:
            # 其他消息交给原始处理器
            self._original_dispatch(data)

    def _on_task_pre(self):
        self.logger.info("Translation task starting...")

    def _on_task_post(self):
        self.logger.info("Translation task completed successfully")
        if self._finished_callback:
            self._finished_callback({"success": True, "msg": "Translation completed"})

    def _on_task_terminate(self):
        self.logger.info("Translation task terminated")
        if self._finished_callback:
            self._finished_callback({"success": False, "msg": "Task terminated"})

    def start_translation(self, text: str, source_lang: str, target_lang: str,
                          llm_config: Dict, delay_ms: int = 0):
        """
        启动翻译任务

        Args:
            text: 要翻译的文本
            source_lang: 源语言
            target_lang: 目标语言
            llm_config: LLM配置
            delay_ms: 延迟启动毫秒数
        """
        from src.core.core_task import TaskMode

        task_id = f"translator_{abs(hash(text)) % 10000}"

        self._task_manager.start_task(
            task_class=TranslatorTask,
            task_id=task_id,
            mode=TaskMode.THREAD,
            delay_ms=delay_ms,
            text=text,
            source_lang=source_lang,
            target_lang=target_lang,
            llm_config=llm_config
        )

    def cancel_translation(self):
        self._task_manager.cancel_task()

    def is_running(self) -> bool:
        return self._task_manager.worker is not None

    def set_callbacks(self, token_callback=None, finished_callback=None, error_callback=None):
        """
        设置回调函数

        Args:
            token_callback: token接收回调，签名: (token: str) -> None
            finished_callback: 完成回调，签名: (result: dict) -> None
            error_callback: 错误回调，签名: (error_msg: str) -> None
        """
        self._token_callback = token_callback
        self._finished_callback = finished_callback
        self._error_callback = error_callback

        # 连接错误信号
        if error_callback:
            def on_state_changed(state, msg):
                if state == TaskState.FAILED.value:
                    error_callback(msg)

            self._task_manager.sig_state_changed.connect(on_state_changed)

    def wait_for_completion(self, timeout_sec: float = None):
        """等待翻译任务完成"""
        self._task_manager.wait(timeout_sec)