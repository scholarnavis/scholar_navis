import json
import logging
import re
from typing import Generator, List, Dict, Optional

import httpx
import litellm
from litellm import completion, image_generation
from litellm.exceptions import APIError, APIConnectionError, ContextWindowExceededError, RateLimitError, Timeout

from src.core.config_manager import ConfigManager
from src.core.network_worker import _get_explicit_proxy_kwargs



litellm.drop_params = True


class OpenAICompatibleLLM:
    def __init__(self, config: Optional[Dict] = None):
        self.logger = logging.getLogger("LLM.Provider")
        self._is_cancelled = False
        self.config_data = config or {}

        sys_cfg = ConfigManager().user_settings
        self.custom_timeout = config.get("timeout", 60.0) if config else 60.0

        if not config:
            self.provider_id = sys_cfg.get("active_llm_id", "custom")
            raw_api_key = sys_cfg.get("llm_api_key", "")
            self.base_url = sys_cfg.get("llm_base_url", "http://localhost:11434/v1")
            self.model_name = sys_cfg.get("llm_model_name", "llama3")
        else:
            self.provider_id = config.get("id", "custom")
            raw_api_key = config.get("api_key", "")
            self.base_url = config.get("base_url", "http://localhost:11434/v1")
            self.model_name = config.get("model_name", "llama3")

        self._missing_api_key = False
        if not raw_api_key or str(raw_api_key).strip() == "":
            if "localhost" not in self.base_url and "127.0.0.1" not in self.base_url:
                self._missing_api_key = True
            self.api_key = "sk-no-key-required"
        else:
            self.api_key = str(raw_api_key).strip()

        # 配置代理环境供 LiteLLM 内部的 HTTP 请求使用
        proxy_cfg = _get_explicit_proxy_kwargs()
        if "proxy" in proxy_cfg:
            # LiteLLM 支持环境变量，通常 network_worker 已设置。这里显式配置增加健壮性
            import os
            os.environ["HTTP_PROXY"] = proxy_cfg["proxy"]
            os.environ["HTTPS_PROXY"] = proxy_cfg["proxy"]

        self.logger.info(f"Initialized Unified LLM Provider via LiteLLM: [{self.model_name}] @ {self.base_url}")

        applied_params = self._get_payload_kwargs()
        if applied_params:
            self.logger.info(f"Applied Custom Parameters: {applied_params}")

    def _log_params(self, payload: Dict):
        safe_payload = {}
        for k, v in payload.items():
            if k in ['api_key', 'messages', 'contents', 'input', 'image_url', 'image_base64', 'inline_data']:
                safe_payload[k] = "<Omitted for Log>"
            elif k in ['tools']:
                pass
            else:
                safe_payload[k] = v
        self.logger.info(f"[{self.model_name}] Request Parameters: {safe_payload}")

    def cancel(self):
        # LiteLLM 对流的打断可以通过停止迭代来实现，无需手动 close client
        self._is_cancelled = True

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

    def _process_messages(self, messages: List[Dict]) -> List[Dict]:
        """
        支持多模态消息：LiteLLM 会将符合 OpenAI 规范的 image_url 自动转译给 Anthropic/Gemini 等
        """
        processed_msgs = []
        for m in messages:
            msg_dict = m.copy()
            role = m.get("role", "user")
            content = m.get("content", "")

            msg_dict["role"] = role

            if isinstance(content, list):
                valid_parts = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") in ["text", "image_url"]:
                            valid_parts.append(part)
                    elif isinstance(part, str):
                        valid_parts.append({"type": "text", "text": part})
                msg_dict["content"] = valid_parts
            else:
                msg_dict["content"] = str(content) if content is not None else ""

            processed_msgs.append(msg_dict)

        return processed_msgs

    def _build_litellm_kwargs(self, payload: Dict, messages: List[Dict], stream: bool = False) -> Dict:
        """构建底层路由参数：决定是当做中转站处理，还是按原生厂商协议处理"""
        kwargs = {
            "model": self.model_name,
            "api_key": self.api_key,
            "api_base": self.base_url,
            "stream": stream,
            "messages": messages,
            "timeout": self.custom_timeout,
            **payload
        }

        # 智能路由：如果没指定厂商前缀（如 'anthropic/'），且 base_url 不是官方的，强制按 OpenAI 格式发送给中转站/本地模型
        if "/" not in self.model_name and self.base_url and "api.openai.com" not in self.base_url:
            kwargs["custom_llm_provider"] = "openai"

        return kwargs

    def chat(self, messages: List[Dict], is_translation=False, **kwargs):
        if getattr(self, '_missing_api_key', False):
            raise ValueError("API Key is missing. Please configure your API key in the settings before proceeding.")

        payload = self._get_payload_kwargs()
        payload.update(kwargs)

        if is_translation:
            for k in ['tools', 'tool_choice', 'response_format', 'image_generation']:
                payload.pop(k, None)

        processed_messages = self._process_messages(messages)
        self._log_params(payload)

        try:
            litellm_kwargs = self._build_litellm_kwargs(payload, processed_messages, stream=False)
            response = completion(**litellm_kwargs)
            choice = response.choices[0]

            reasoning = getattr(choice.message, 'reasoning_content', None)
            if not reasoning and hasattr(choice.message, 'model_extra') and choice.message.model_extra:
                reasoning = choice.message.model_extra.get('reasoning_content')

            if getattr(choice.message, 'tool_calls', None):
                msg_dump = choice.message.model_dump(exclude_none=True)
                msg_dump["reasoning_content"] = reasoning or ""
                if not msg_dump.get("content"):
                    msg_dump["content"] = ""
                return msg_dump

            return {
                "content": choice.message.content or "",
                "reasoning_content": reasoning or "",
                "role": "assistant"
            }
        except Exception as e:
            self.logger.error(f"Chat completion error: {str(e)}")
            raise e

    def stream_chat(self, messages: List[Dict], is_translation=False, **kwargs) -> Generator[str, None, None]:
        if getattr(self, '_missing_api_key', False):
            raise ValueError("API Key is missing. Please configure your API key in the settings before proceeding.")

        payload = self._get_payload_kwargs()
        payload.update(kwargs)

        if is_translation:
            for k in ['tools', 'tool_choice', 'response_format', 'image_generation']:
                payload.pop(k, None)

        processed_messages = self._process_messages(messages)
        stream = payload.pop("stream", True)
        self._log_params(payload)

        is_thinking = False

        try:
            litellm_kwargs = self._build_litellm_kwargs(payload, processed_messages, stream=stream)
            response = completion(**litellm_kwargs)

            if not stream:
                yield response.choices[0].message.content or ""
                return

            for chunk in response:
                if self._is_cancelled:
                    if is_thinking:
                        yield "\n</think>\n\n"
                    yield "\n\n[⛔ Generation halted by user.]"
                    break

                if not getattr(chunk, 'choices', None) or not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                # 统一抽象抽象不同模型的“思考”过程输出
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
                    # 兼容 Ollama / VLLM：某些直接在正文里输出 <think> 标签的模型
                    if "<think>" in content and not is_thinking:
                        is_thinking = True
                    if "</think>" in content and is_thinking:
                        yield content  # 包含标签一同输出
                        is_thinking = False
                        continue

                    if is_thinking and not reasoning:
                        # 模型正文里混杂着思考内容
                        yield content
                    elif not is_thinking:
                        # 正常文本
                        yield content
                    elif is_thinking and reasoning:
                        # 异常切换：reasoning 结束突然接 content
                        yield "\n</think>\n\n"
                        is_thinking = False
                        yield content

            if is_thinking:
                yield "\n</think>\n"

        except ContextWindowExceededError as e:
            self.logger.error(f"Context window exceeded: {e}")
            yield f"\n\n[Context Exceeded Error]\nThe input text or document is too long for this model. Please clear history or use a model with a larger context window.\n"

        except RateLimitError as e:
            self.logger.error(f"Rate limit hit: {e}")
            yield f"\n\n[Rate Limit Error]\nToo many requests or insufficient quota. Please try again later.\n"
        except Timeout as e:
            self.logger.error(f"Request timeout: {e}")
            yield f"\n\n[Timeout Error]\nThe model took too long to respond. Please check your network or try a different provider.\n"
        except APIError as e:
            self.logger.error(f"API Error ({e.status_code}): {e.message}")
            friendly_msg = ""
            if e.status_code == 400:
                # 400 错误通常是因为参数不支持，比如传了图片但模型是纯文本的
                friendly_msg = "\n💡 Tip: This might happen if you sent an image to a text-only model. Try selecting a specific Vision Model in the settings."
            yield f"\n\n[API Request Error: HTTP {e.status_code}]\n{e.message}{friendly_msg}\n"
        except Exception as e:
            if self._is_cancelled or "closed" in str(e).lower() or "cancel" in str(e).lower():
                yield "\n\n[⛔ Generation halted by user.]"
            else:
                self.logger.error(f"Unexpected system error: {e}")
                yield f"\n\n[System Error: {str(e)}]\n"


    def generate_image(self, prompt: str, **kwargs) -> str:
        """
        补全的多模态：统一的图像生成接口。
        支持 DALL-E, Midjourney (需要对应的代理 API), 或兼容的模型。
        """
        if getattr(self, '_missing_api_key', False):
            raise ValueError("API Key is missing. Please configure your API key.")

        self.logger.info(f"Generating image with prompt: {prompt[:50]}...")

        try:
            # LiteLLM 的图像生成接口
            res = image_generation(
                prompt=prompt,
                model=self.model_name,
                api_key=self.api_key,
                api_base=self.base_url,
                **kwargs
            )
            # 提取生成的图像 URL
            return res.data[0].url
        except Exception as e:
            self.logger.error(f"Image generation failed: {str(e)}")
            raise e