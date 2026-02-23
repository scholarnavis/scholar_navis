import json
import logging
import httpx
from typing import Generator, List, Dict, Optional
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

        self.logger.info(f"Initialized LLM Config: [{self.model_name}] @ {self.base_url} (Proxy: {proxy_mode})")

    def cancel(self):
        self._is_cancelled = True
        try:
            if self._httpx_client:
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
                elif ptype == "json":
                    import json
                    res[name] = json.loads(val_str)
                else:
                    res[name] = val_str
            except Exception as e:
                self.logger.warning(f"Parameter Parse Warning: Cannot cast '{name}' ({val_str}) to {ptype}. Skipping. Error: {e}")

        return res

    def chat(self, messages: List[Dict[str, str]], **kwargs):
        """非流式请求，用于翻译和工具调用 (Tool Calling)"""
        payload = {
            "model": self.model_name,
            "messages": messages,
        }
        payload.update(kwargs)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        url = f"{self.base_url.rstrip('/')}/chat/completions"

        response = self._httpx_client.post(url, headers=headers, json=payload)
        response.raise_for_status()

        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            return "" if "tools" not in kwargs else {}

        message = choices[0].get("message", {})
        if "tool_calls" in message and message["tool_calls"]:
            message["content"] = ""  # 确保 content 不为 None
            return message

        # 如果是正常的文本请求（如翻译、润色），只返回 content 字符串
        content = message.get("content", "") or ""

        # 如果外部明确要求返回工具格式（即使没触发），也返回字典
        if "tools" in kwargs:
            return message

        return content  # 返回字符串，这样 .strip() 就能正常工作了

    def stream_chat(self, messages: List[Dict[str, str]]) -> Generator[str, None, None]:
        try:
            payload = {
                "model": self.model_name,
                "messages": messages,
                "stream": True,
            }

            models_config = self.config_data.get("models_config", {})
            current_model_conf = models_config.get(self.model_name, {})

            if current_model_conf:
                param_mode = current_model_conf.get("mode", "inherit")
                model_params = current_model_conf.get("params", [])
            else:
                param_mode = self.config_data.get("model_params_mode", "inherit")
                model_params = self.config_data.get("model_params", [])

            provider_params = self.config_data.get("provider_params", [])
            custom_params = {}

            if param_mode == "inherit":
                custom_params = self._parse_custom_params(provider_params)
            elif param_mode == "custom":
                custom_params = self._parse_custom_params(model_params)

            safe_custom_params = {
                k: v for k, v in custom_params.items()
                if k not in ["messages", "model", "stream"]
            }

            payload.update(safe_custom_params)

            log_kwargs = {k: v for k, v in payload.items() if k != 'messages'}
            self.logger.info(f"Stream generation requested. Config applied [Mode: {param_mode}]: {log_kwargs}")

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            url = f"{self.base_url.rstrip('/')}/chat/completions"

            is_thinking = False

            with self._httpx_client.stream("POST", url, headers=headers, json=payload) as response:
                # 修复核心：如果报错(例如 400参数错误)，强制提前读取 error body，防止 raise 时抛出 ResponseNotRead
                if response.status_code >= 400:
                    response.read()
                response.raise_for_status()

                for line in response.iter_lines():
                    if self._is_cancelled:
                        if is_thinking: yield "\n</think>\n"
                        yield "\n\n[⛔ Generation halted by user.]"
                        break

                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue

                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                        choices = chunk.get("choices", [])
                        if not choices:
                            continue

                        delta = choices[0].get("delta", {})

                        reasoning = delta.get("reasoning_content")
                        if reasoning:
                            if not is_thinking:
                                yield "<think>\n"
                                is_thinking = True
                            yield reasoning

                        content = delta.get("content")
                        if content:
                            if is_thinking:
                                yield "\n</think>\n"
                                is_thinking = False
                            yield content
                    except json.JSONDecodeError:
                        pass

            if is_thinking:
                yield "\n</think>\n"

        except httpx.HTTPStatusError as e:
            err_text = e.response.text
            self.logger.error(f"HTTP Status Error: {e.response.status_code} - {err_text}")
            yield f"\n\n[API Request Error: HTTP {e.response.status_code}]\n{err_text}\n"
        except Exception as e:
            error_msg = str(e).lower()
            if self._is_cancelled or "closed" in error_msg or "cancelled" in error_msg:
                self.logger.info("LLM Socket connection closed by user cancellation.")
                yield "\n\n[⛔ Generation halted by user.]"
            else:
                self.logger.error(f"LLM API Stream Error: {str(e)}")
                yield f"\n\n[System Error: {str(e)}]\n"