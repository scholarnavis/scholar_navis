import json
import logging
from typing import Generator, List, Dict, Optional

import httpx
from openai import OpenAI, APIStatusError
from src.core.config_manager import ConfigManager
from src.core.network_worker import _get_explicit_proxy_kwargs


class OpenAICompatibleLLM:
    def __init__(self, config: Optional[Dict] = None):
        self.logger = logging.getLogger("LLM.Provider")
        self._is_cancelled = False
        self.config_data = config or {}

        sys_cfg = ConfigManager().user_settings
        custom_timeout = config.get("timeout", 60.0) if config else 60.0

        httpx_kwargs = {"timeout": custom_timeout}
        proxy_cfg = _get_explicit_proxy_kwargs()
        httpx_kwargs.update(proxy_cfg)

        self.http_client = httpx.Client(**httpx_kwargs)

        if not config:
            self.provider_id = sys_cfg.get("active_llm_id", "custom")
            self.api_key = sys_cfg.get("llm_api_key", "sk-no-key-required")
            self.base_url = sys_cfg.get("llm_base_url", "http://localhost:11434/v1")
            self.model_name = sys_cfg.get("llm_model_name", "llama3")
        else:
            self.provider_id = config.get("id", "custom")
            self.api_key = config.get("api_key", "sk-no-key-required")
            self.base_url = config.get("base_url", "http://localhost:11434/v1")
            self.model_name = config.get("model_name", "llama3")

        if not self.api_key:
            self.api_key = "sk-no-key-required"

        # 全部强制使用标准 OpenAI 客户端
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url, http_client=self.http_client)

        self.logger.info(f"Initialized Pure OpenAI-Compatible LLM: [{self.model_name}] @ {self.base_url}")

        applied_params = self._get_payload_kwargs()
        if applied_params:
            self.logger.info(f"Applied Custom Parameters: {applied_params}")

    def _log_params(self, payload: Dict):
        safe_payload = {}
        for k, v in payload.items():
            if k in ['api_key', 'messages', 'contents', 'input', 'image_url', 'image_base64', 'inline_data']:
                safe_payload[k] = "<Omitted for Log>"
            else:
                safe_payload[k] = v
        self.logger.info(f"[{self.model_name}] Request Parameters: {safe_payload}")

    def cancel(self):
        self._is_cancelled = True
        try:
            if self.client and hasattr(self.client, "close"):
                self.client.close()
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
                    res[name] = json.loads(val_str)
                else:
                    res[name] = val_str
            except Exception as e:
                self.logger.warning(f"Parameter Parse Warning: {e}")
        return res

    def _get_payload_kwargs(self) -> Dict:
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

        return {k: v for k, v in custom_params.items() if k not in ["messages", "model", "stream", "tools"]}

    def _split_openai_payload(self, payload: Dict) -> Dict:
        standard_keys = {
            "temperature", "top_p", "n", "stop", "max_tokens", "presence_penalty",
            "frequency_penalty", "logit_bias", "user", "response_format", "seed",
            "tools", "tool_choice", "parallel_tool_calls", "logprobs", "top_logprobs", "reasoning_effort"
        }

        standard_payload = {}
        extra_payload = {}

        for k, v in payload.items():
            if k in standard_keys:
                standard_payload[k] = v
            else:
                extra_payload[k] = v

        if extra_payload:
            standard_payload["extra_body"] = extra_payload

        return standard_payload

    def _sanitize_messages_for_text_only(self, messages: List[Dict]) -> List[Dict]:
        """
        铁腕清理多模态消息：
        不管上层UI传了什么图片或复杂对象，全部剥离，只提取纯文本发给大模型。
        保证 PDF/文档文本正常发送，同时掐断所有多模态报错的可能性。
        """
        clean_msgs = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")

            if isinstance(content, list):
                # 遍历列表，只提取 type 为 text 的内容
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text" and "text" in part:
                        text_parts.append(part["text"])
                    elif isinstance(part, str):
                        text_parts.append(part)
                clean_msgs.append({"role": role, "content": "\n".join(text_parts)})
            else:
                clean_msgs.append({"role": role, "content": str(content)})

        return clean_msgs

    def chat(self, messages: List[Dict], is_translation=False, **kwargs):
        payload = self._get_payload_kwargs()
        payload.update(kwargs)

        if is_translation:
            for k in ['tools', 'tool_choice', 'response_format', 'image_generation']:
                payload.pop(k, None)

        # 强制格式化为纯文本
        clean_messages = self._sanitize_messages_for_text_only(messages)

        safe_payload = self._split_openai_payload(payload)

        self._log_params(safe_payload)

        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=clean_messages,
            **safe_payload
        )
        choice = response.choices[0]

        if choice.message.tool_calls:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": t.id, "type": "function",
                                "function": {"name": t.function.name, "arguments": t.function.arguments}} for t in
                               choice.message.tool_calls]
            }
        return choice.message.content or ""

    def stream_chat(self, messages: List[Dict], is_translation=False, **kwargs) -> Generator[str, None, None]:
        payload = self._get_payload_kwargs()
        payload.update(kwargs)

        if is_translation:
            for k in ['tools', 'tool_choice', 'response_format', 'image_generation']:
                payload.pop(k, None)

        # 强制格式化为纯文本
        clean_messages = self._sanitize_messages_for_text_only(messages)
        self._log_params(payload)

        is_thinking = False
        stream = payload.pop("stream", True)

        safe_payload = self._split_openai_payload(payload)

        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=clean_messages,
                stream=stream,
                **safe_payload
            )

            if not stream:
                yield response.choices[0].message.content or ""
                return

            for chunk in response:
                if self._is_cancelled:
                    if is_thinking: yield "\n</think>\n"
                    yield "\n\n[⛔ Generation halted by user.]"
                    break

                if not chunk.choices: continue
                delta = chunk.choices[0].delta

                # 安全提取 DeepSeek 等模型兼容的 reasoning_content
                reasoning = getattr(delta, 'reasoning_content', None)
                if not reasoning and hasattr(delta, 'model_extra') and delta.model_extra:
                    reasoning = delta.model_extra.get('reasoning_content')

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

            if is_thinking: yield "\n</think>\n"

        except APIStatusError as e:
            friendly_msg = ""
            if e.status_code == 400:
                friendly_msg = "\n\n💡 <b>System Tip:</b> Request rejected. Make sure context limits aren't exceeded or parameters are valid."
            yield f"\n\n[API Request Error: HTTP {e.status_code}]\n{str(e)}{friendly_msg}\n"
        except Exception as e:
            if self._is_cancelled or "closed" in str(e).lower():
                yield "\n\n[⛔ Generation halted by user.]"
            else:
                yield f"\n\n[System Error: {str(e)}]\n"