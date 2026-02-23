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

        self.config_data = config or {}

        sys_cfg = ConfigManager().user_settings
        proxy_mode = sys_cfg.get("proxy_mode", "system")
        custom_timeout = config.get("timeout", 60.0) if config else 60.0
        httpx_kwargs = {"timeout": custom_timeout}
        proxy_cfg = _get_explicit_proxy_kwargs()
        httpx_kwargs.update(proxy_cfg)

        try:
            self._httpx_client = httpx.Client(**httpx_kwargs)
        except TypeError:
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

    def _parse_custom_params(self, params_list: List[Dict]) -> Dict:
        res = {}
        if not params_list: return res

        for p in params_list:
            name = p.get("name", "").strip()
            if not name: continue

            val_str = str(p.get("value", ""))
            ptype = p.get("type", "str")

            try:
                if ptype == "int":
                    res[name] = int(val_str)
                elif ptype == "float":
                    res[name] = float(val_str)
                elif ptype == "bool":
                    res[name] = val_str.lower() in ['true', '1', 'yes', 'on']
                else:
                    res[name] = val_str
            except ValueError:
                self.logger.warning(f"Parameter Parse Warning: Cannot cast '{name}' ({val_str}) to {ptype}. Skipping.")

        return res

    def stream_chat(self, messages: List[Dict[str, str]]) -> Generator[str, None, None]:
        try:
            kwargs = {
                "model": self.model_name,
                "messages": messages,
                "stream": True,
            }

            # Strategy Resolution: Isolate parameters per specific model
            models_config = self.config_data.get("models_config", {})
            current_model_conf = models_config.get(self.model_name, {})

            if current_model_conf:
                param_mode = current_model_conf.get("mode", "inherit")
                model_params = current_model_conf.get("params", [])
            else:
                # Fallback for backward compatibility
                param_mode = self.config_data.get("model_params_mode", "inherit")
                model_params = self.config_data.get("model_params", [])

            provider_params = self.config_data.get("provider_params", [])
            custom_params = {}

            if param_mode == "inherit":
                custom_params = self._parse_custom_params(provider_params)
            elif param_mode == "custom":
                custom_params = self._parse_custom_params(model_params)
            # If mode == "closed", custom_params remains empty

            safe_custom_params = {
                k: v for k, v in custom_params.items()
                if k not in ["messages", "model", "stream"]
            }

            kwargs.update(safe_custom_params)

            log_kwargs = {k: v for k, v in kwargs.items() if k != 'messages'}
            self.logger.info(f"Stream generation requested. Config applied [Mode: {param_mode}]: {log_kwargs}")

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

                reasoning = getattr(delta, 'reasoning_content', None)
                if reasoning:
                    if not is_thinking:
                        yield "<think>\n"
                        is_thinking = True
                    yield reasoning

                content = getattr(delta, 'content', None)
                if content:
                    if is_thinking:
                        yield "\n</think>\n"
                        is_thinking = False
                    yield content

            if is_thinking:
                yield "\n</think>\n"

        except Exception as e:
            error_msg = str(e).lower()
            if self._is_cancelled or "closed" in error_msg or "cancelled" in error_msg:
                self.logger.info("LLM Socket connection closed by user cancellation.")
                yield "\n\n[⛔ Generation halted by user.]"
            else:
                self.logger.error(f"LLM API Stream Error: {str(e)}")
                yield f"\n\n[System Error: {str(e)}]\n"