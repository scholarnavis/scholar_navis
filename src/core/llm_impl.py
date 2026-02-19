import logging
import httpx
from typing import Generator, List, Dict, Optional
from openai import OpenAI
from src.core.config_manager import ConfigManager


class OpenAICompatibleLLM:

    def __init__(self, config: Optional[Dict] = None):
        self.logger = logging.getLogger("LLM.Provider")
        self._is_cancelled = False
        self._httpx_client = httpx.Client(timeout=60.0)

        if not config:
            sys_cfg = ConfigManager().user_settings
            self.api_key = sys_cfg.get("llm_api_key", "sk-placeholder")
            self.base_url = sys_cfg.get("llm_base_url", "http://localhost:11434/v1")
            self.model_name = sys_cfg.get("llm_model_name", "llama3")
        else:
            self.api_key = config.get("api_key", "sk-placeholder")
            self.base_url = config.get("base_url", "http://localhost:11434/v1")
            self.model_name = config.get("model_name", "llama3")

        if not self.api_key:
            self.api_key = "sk-no-key-required"

        self.logger.info(f"Initializing LLM Client: [{self.model_name}] @ {self.base_url}")

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
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                stream=True,
                temperature=0.7,
                max_tokens=4096
            )

            for chunk in response:
                if self._is_cancelled:
                    yield "\n\n[⛔ Generation halted by user.]"
                    break

                if hasattr(chunk.choices[0], 'delta'):
                    delta = chunk.choices[0].delta
                    if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                        yield f"<think>{delta.reasoning_content}</think>"
                    if hasattr(delta, 'content') and delta.content:
                        yield delta.content


        except Exception as e:
            error_msg = str(e).lower()
            if self._is_cancelled or "closed" in error_msg or "cancelled" in error_msg:
                self.logger.info("LLM Socket connection closed by user cancellation.")
                yield "\n\n[⛔ Generation halted by user.]"
            else:
                self.logger.error(f"LLM Stream Error: {error_msg}")
                yield f"\n\n[⚠System Error: {str(e)}]\n"