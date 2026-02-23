import logging
import httpx
from typing import Generator, List, Dict, Optional
from openai import OpenAI
from src.core.config_manager import ConfigManager
from src.core.network_worker import _get_explicit_proxy_kwargs


class OpenAICompatibleLLM:

    def __init__(self, config: Optional[Dict] = None):
        self.logger = logging.getLogger("LLM.Provider")
        self._is_cancelled = False

        sys_cfg = ConfigManager().user_settings
        proxy_mode = sys_cfg.get("proxy_mode", "system")
        custom_timeout = config.get("timeout", 60.0) if config else 60.0
        httpx_kwargs = {"timeout": custom_timeout}
        httpx_kwargs = {"timeout": 60.0}
        proxy_cfg = _get_explicit_proxy_kwargs()
        httpx_kwargs.update(proxy_cfg)

        try:
            self._httpx_client = httpx.Client(**httpx_kwargs)
        except TypeError:
            # 兼容低版本/不同系统的 httpx 代理参数名差异
            if "proxy" in httpx_kwargs:
                httpx_kwargs["proxies"] = httpx_kwargs.pop("proxy")
            self._httpx_client = httpx.Client(**httpx_kwargs)

        if not config:
            self.api_key = sys_cfg.get("llm_api_key", "sk-placeholder")
            self.base_url = sys_cfg.get("llm_base_url", "http://localhost:11434/v1")
            self.model_name = sys_cfg.get("llm_model_name", "llama3")
        else:
            self.api_key = config.get("api_key", "sk-placeholder")
            self.base_url = config.get("base_url", "http://localhost:11434/v1")
            self.model_name = config.get("model_name", "llama3")

        if not self.api_key:
            self.api_key = "sk-no-key-required"

        self.logger.info(f"Initializing LLM Client: [{self.model_name}] @ {self.base_url} (Proxy Mode: {proxy_mode})")

        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            http_client=self._httpx_client
        )

    def cancel(self):
        self._is_cancelled = True
        try:
            self._httpx_client.close()
        except Exception:
            pass

    def stream_chat(self, messages: List[Dict[str, str]]) -> Generator[str, None, None]:
        try:
            # 1. 基础参数
            kwargs = {
                "model": self.model_name,
                "messages": messages,
                "stream": True,
                "temperature": 0.7,
                "max_tokens": 16384
            }

            # 2. 动态注入 Nvidia 专属思考激活参数
            # 通过检测 base_url 判断是否为 Nvidia 服务商
            if "nvidia.com" in self.base_url.lower():
                kwargs["extra_body"] = {
                    "chat_template_kwargs": {
                        "enable_thinking": True,
                        "clear_thinking": False
                    }
                }

            self.logger.debug(f"Calling LLM API with kwargs: {kwargs.keys()}")
            response = self.client.chat.completions.create(**kwargs)

            is_thinking = False

            for chunk in response:
                if self._is_cancelled:
                    if is_thinking: yield "\n</think>\n"
                    yield "\n\n[⛔ Generation halted by user.]"
                    break

                if not getattr(chunk, "choices", None) or len(chunk.choices) == 0:
                    continue

                delta = getattr(chunk.choices[0], "delta", None)
                if not delta: continue

                # 1. 处理思考内容 (Reasoning)
                reasoning = getattr(delta, 'reasoning_content', None)
                if reasoning:
                    if not is_thinking:
                        yield "<think>\n"
                        is_thinking = True
                    yield reasoning

                # 2. 处理正式内容 (Content)
                content = getattr(delta, 'content', None)
                if content:
                    if is_thinking:
                        # 只有当正式内容出现时，才闭合思考标签
                        yield "\n</think>\n"
                        is_thinking = False
                    yield content

                # 异常中断补全标签
            if is_thinking:
                yield "\n</think>\n"

        except Exception as e:
            error_msg = str(e).lower()
            if self._is_cancelled or "closed" in error_msg or "cancelled" in error_msg:
                self.logger.info("LLM Socket connection closed by user cancellation.")
                if 'is_thinking' in locals() and is_thinking:
                    yield "\n</think>\n"
                yield "\n\n[⛔ Generation halted by user.]"
            else:
                self.logger.error(f"LLM Stream Error: {error_msg}")
                if 'is_thinking' in locals() and is_thinking:
                    yield "\n</think>\n"
                yield f"\n\n[⚠System Error: {str(e)}]\n"