import json
import logging
import os
import threading
import uuid
from typing import Generator, List, Dict, Optional, Tuple

import litellm
from litellm import completion, image_generation
from litellm.exceptions import APIError, APIConnectionError, ContextWindowExceededError, RateLimitError, Timeout, \
    AuthenticationError, ServiceUnavailableError, BadRequestError, NotFoundError

from src.core.config_manager import ConfigManager
from src.core.network_worker import _get_explicit_proxy_kwargs


_translation_lock = threading.Lock()
_TRANSLATION_CACHE = {}

def get_cached_translation(text, direction="to_en", llm_instance=None, **kwargs):
    if not llm_instance: return text

    # 使用方向和文本的哈希作为唯一键，彻底与 llm_instance 实例解耦
    cache_key = f"{direction}_{hash(text)}"

    with _translation_lock:
        if cache_key in _TRANSLATION_CACHE:
            return _TRANSLATION_CACHE[cache_key]

    if direction == "to_en":
        prompt = (
            "You are an expert bioinformatician and translator. "
            "Translate the following user query into precise academic English. "
            "CRITICAL: DO NOT translate or alter any Latin taxonomic names (e.g., Gossypium, Arabidopsis) "
            "or scientific abbreviations (e.g., scRNA-seq, qPCR). "
            "Output ONLY the translated English text, nothing else."
        )
    else:
        prompt = (
            "You are an expert academic translator. Translate the following English text "
            "into the language of the user's original query. \n"
            "CRITICAL RULES:\n"
            "1. KEEP ALL CITATION TAGS INTACT (e.g., [1], [2]).\n"
            "2. DO NOT translate Latin taxonomic names.\n"
            "3. PRESERVE all Markdown formatting.\n"
        )

    response = llm_instance.chat([
        {"role": "system", "content": prompt},
        {"role": "user", "content": text}
    ], **kwargs)

    res_text = response.get("content", "").strip() if isinstance(response, dict) else str(response).strip()

    if res_text:
        _TRANSLATION_CACHE[cache_key] = res_text

    return res_text


def _raw_error_text(e: Exception) -> str:
    """提取异常的原始底层信息（provider 响应体、状态码、请求 URL）。

    litellm 异常的 ``message`` 常为摘要；``body`` / ``response`` 才包含
    provider 返回的原始 JSON。统一结构化输出供日志完整追溯；UI 错误
    面板的 details 与日志同源（协议见 src/core/llm_errors.py）。
    """
    parts = [f"{type(e).__name__}: {e}"]
    status = getattr(e, 'status_code', None)
    if status:
        parts.append(f"status_code: {status}")
    body = getattr(e, 'body', None)
    if body:
        try:
            parts.append(f"body: {json.dumps(body, ensure_ascii=False)}")
        except (TypeError, ValueError):
            parts.append(f"body: {body!r}")
    response = getattr(e, 'response', None)
    if response is not None and not body:
        text = getattr(response, 'text', None)
        if text:
            parts.append(f"response: {str(text)[:2000]}")
    request = getattr(e, 'request', None)
    if request is not None:
        url = getattr(request, 'url', '')
        if url:
            parts.append(f"url: {url}")
    return "\n".join(parts)


#: provider 承载"思考链"的字段名。litellm 对多数 provider 归一化为
#: ``reasoning_content``，但部分本地/兼容网关使用 ``reasoning`` /
#: ``thinking`` / ``thinking_content``。只识别前者会导致思考链落入正文，
#: 因此这里集中做跨字段兼容，避免各处重复判断而口径不一。
_REASONING_FIELD_NAMES = ("reasoning_content", "reasoning", "thinking", "thinking_content")


def _coerce_reasoning_value(value) -> str:
    """把 provider 返回的推理字段值规整为纯文本（恒返回 ``str``）。

    字段形态可能是 str、dict（``{"thinking"|"text"|"content": ...}``）、
    或 list（Anthropic/Gemini 经 litellm 的 ``thinking_blocks``）。未知形态
    一律返回空串，绝不抛出，避免流式过程中因单条脏数据中断整轮对话。
    """
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("thinking", "text", "content", "reasoning_content", "reasoning"):
            inner = value.get(key)
            if isinstance(inner, str) and inner:
                return inner
        return ""
    if isinstance(value, (list, tuple)):
        parts: List[str] = []
        for item in value:
            text = _coerce_reasoning_value(item)
            if text:
                parts.append(text)
        return "".join(parts)
    for attr in ("thinking", "text", "content"):
        inner = getattr(value, attr, None)
        if isinstance(inner, str) and inner:
            return inner
    return ""


def _extract_reasoning(holder) -> str:
    """从 delta / message 对象提取思考链文本（跨 provider 字段名兼容）。

    仅当对象确实携带推理内容时返回非空字符串；调用方据此把文本包进
    `` thinking`` 折叠区，从而保证思考链不会作为正文渲染。
    """
    if holder is None:
        return ""
    for name in _REASONING_FIELD_NAMES:
        text = _coerce_reasoning_value(getattr(holder, name, None))
        if text:
            return text
    extra = getattr(holder, "model_extra", None)
    if isinstance(extra, dict):
        for name in _REASONING_FIELD_NAMES:
            text = _coerce_reasoning_value(extra.get(name))
            if text:
                return text
        text = _coerce_reasoning_value(extra.get("thinking_blocks"))
        if text:
            return text
    return ""


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

    def reset(self):
        """清除取消标志，供实例复用（新一轮生成开始时调用）。

        一旦 cancel() 将 _is_cancelled 置 True，若不复位，后续所有流式
        stream_chat 会立即中断并输出"已停止"。每次新的 Agent 运行/任务启动
        前都应调用 reset() 以保证状态干净。
        """
        self._is_cancelled = False

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

        if "temperature" not in custom_params:
            custom_params["temperature"] = 0.01
        if "top_p" not in custom_params:
            custom_params["top_p"] = 0.1

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
            "stream": stream,
            "messages": messages,
            "timeout": self.custom_timeout,
            **payload
        }

        # 绝对服从用户配置：确保传入配置项里的 API URL
        if self.base_url:
            kwargs["api_base"] = self.base_url

        base = self.base_url.lower() if self.base_url else ""

        # 智能路由 1：处理几个规矩特殊、需要走原生协议的官方 API
        if "api.anthropic.com" in base or "api.minimaxi.com/anthropic" in base:
            kwargs["custom_llm_provider"] = "anthropic"
            if not self.model_name.startswith("anthropic/"):
                pass
        elif "api.deepseek.com" in base:
            kwargs["custom_llm_provider"] = "deepseek"
            if not self.model_name.startswith("deepseek/"):
                kwargs["model"] = f"deepseek/{self.model_name}"
        elif "generativelanguage" in base and "openai" not in base:
            kwargs["custom_llm_provider"] = "gemini"
            if not self.model_name.startswith("gemini/"):
                kwargs["model"] = f"gemini/{self.model_name}"
        elif "xiaomimimo.com" in base:
            kwargs["custom_llm_provider"] = "openai"
            if not self.model_name.startswith("openai/"):
                kwargs["model"] = f"openai/{self.model_name}"
        else:
            kwargs["custom_llm_provider"] = "openai"
            if not self.model_name.startswith("openai/"):
                kwargs["model"] = f"openai/{self.model_name}"

        return kwargs

    def _attach_usage(self, response, result: Dict) -> None:
        """把 provider 返回的真实 usage 以 ``_usage`` 键附加到结果 dict。

        供 AgentRuntime._accumulate_usage 消费（真实 usage 优先、估算兜底）。
        部分 provider 或异常路径下 usage 缺失，此时不写入任何键，让调用方
        自然走 token 估算兜底；提取失败仅告警，不影响正常返回结果。
        """
        try:
            usage = getattr(response, "usage", None)
            if not usage:
                return
            result["_usage"] = {
                "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            }
        except (TypeError, ValueError) as e:
            self.logger.warning(f"Attach usage failed (skip real usage): {e}")

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

            reasoning = _extract_reasoning(choice.message)

            if getattr(choice.message, 'tool_calls', None):
                msg_dump = choice.message.model_dump(exclude_none=True)
                msg_dump["reasoning_content"] = reasoning or ""
                if not msg_dump.get("content"):
                    msg_dump["content"] = ""
                self._attach_usage(response, msg_dump)
                return msg_dump

            result = {
                "content": choice.message.content or "",
                "reasoning_content": reasoning or "",
                "role": "assistant"
            }
            self._attach_usage(response, result)
            return result
        except Exception as e:
            self.logger.error(f"Chat completion error.\n{_raw_error_text(e)}", exc_info=True)
            raise

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
        native_reasoning_mode = False

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

                # 提取思考内容（跨 provider 字段名兼容，务必先于 content 判定，
                # 避免思考链被当成正文输出）
                reasoning = _extract_reasoning(delta)

                if reasoning:
                    if not is_thinking:
                        yield "<think>\n"
                        is_thinking = True
                        native_reasoning_mode = True
                    yield reasoning

                # 提取正文内容
                content = getattr(delta, 'content', None)
                if content:
                    if "<think>" in content and not is_thinking:
                        is_thinking = True
                        native_reasoning_mode = False

                    if "</think>" in content and is_thinking:
                        yield content
                        is_thinking = False
                        continue

                    if is_thinking:
                        if native_reasoning_mode:
                            yield "\n</think>\n\n"
                            is_thinking = False
                            native_reasoning_mode = False
                            yield content
                        else:
                            yield content
                    else:
                        yield content

            if is_thinking:
                yield "\n</think>\n"


        except Exception as e:
            text, label = self._stream_error_map(e)
            if text is not None:
                # provider 原始响应体（含"不支持图片/不支持格式"等具体原因）完整入日志
                self.logger.error(f"{label}\n{_raw_error_text(e)}")
                yield text
            elif self._is_cancelled or "closed" in str(e).lower() or "cancel" in str(e).lower():
                yield "\n\n[⛔ Generation halted by user.]"
            else:
                # 未知错误：完整 traceback + 原始信息入日志，摘要反馈给用户
                self.logger.error(f"Unexpected system error.\n{_raw_error_text(e)}", exc_info=True)
                yield f"\n\n[System Error: {type(e).__name__}: {e}]\n"

    @staticmethod
    def _stream_error_map(e: Exception) -> Tuple[Optional[str], Optional[str]]:
        """litellm 流式异常 → (UI 错误文本, 日志短标签)。

        错误文案与历史版本逐字一致：UI 依据 ``[xxx Error]`` 标记前缀做
        错误面板路由（runtime._is_error_token / chat_tasks 降级流），
        不可改动标记格式。未知异常返回 (None, None) 由调用方兜底。
        """
        if isinstance(e, ContextWindowExceededError):
            return ("\n\n[Context Exceeded Error]\nThe input text or document is too long for this model. "
                    "Please clear history or use a model with a larger context window.\n",
                    "Context window exceeded.")
        if isinstance(e, RateLimitError):
            return ("\n\n[Rate Limit Error]\nToo many requests or insufficient quota. "
                    "Please try again later.\n", "Rate limit hit.")
        if isinstance(e, Timeout):
            return ("\n\n[Timeout Error]\nThe model took too long to respond. "
                    "Please check your network or try a different provider.\n", "Request timeout.")
        if isinstance(e, AuthenticationError):
            return f"\n\n[API Request Error: HTTP 401]\n{getattr(e, 'message', str(e))}\n", "Authentication Error."
        if isinstance(e, NotFoundError):
            return f"\n\n[API Request Error: HTTP 404]\n{getattr(e, 'message', str(e))}\n", "Not Found Error."
        if isinstance(e, BadRequestError):
            return (f"\n\n[API Request Error: HTTP 400]\n{getattr(e, 'message', str(e))}\n"
                    "💡 Tip: This might happen if you sent an image to a text-only model, "
                    "used an unsupported image format, or provided invalid parameters.\n",
                    "Bad Request Error.")
        if isinstance(e, ServiceUnavailableError):
            return (f"\n\n[API Request Error: HTTP 503]\nThe API service is currently overloaded or down. "
                    f"Please try again later.\nDetails: {getattr(e, 'message', str(e))}\n",
                    "Service Unavailable Error.")
        if isinstance(e, APIConnectionError):
            return (f"\n\n[System Error: Connection Failed]\nFailed to connect to the API endpoint. "
                    f"Please check your proxy settings or local network.\nDetails: {getattr(e, 'message', str(e))}\n",
                    "API Connection Error.")
        if isinstance(e, APIError):
            return f"\n\n[API Request Error: HTTP {e.status_code}]\n{e.message}\n", f"API Error ({e.status_code})."
        return None, None

    def stream_chat_events(self, messages: List[Dict], is_translation=False, **kwargs) -> Generator[Dict, None, None]:
        """流式调用并收集完整响应（含 tool_calls 增量累积），供 Agent 循环使用。

        与 stream_chat 的差异：
        - 保留 tools/tool_choice 参数（工具调用轮次必需）；
        - delta.tool_calls 分片按 index 累积，流结束后合成完整调用列表
          （与 chat() 非流式返回的 tool_calls 结构一致）；
        - 事件化输出：{"type": "reasoning"|"text", "text": str} 与收尾的
          {"type": "final", "response": dict}；final.response 结构与
          chat() 返回一致（content/reasoning_content/role/tool_calls/
          _usage），供上层判定工具调用与用量统计。
        内嵌 ``<think>`` 标签的模型其 content 原样进入 text 事件，与
        非流式 chat() 的 content 行为保持一致。
        """
        if getattr(self, '_missing_api_key', False):
            raise ValueError("API Key is missing. Please configure your API key in the settings before proceeding.")

        payload = self._get_payload_kwargs()
        payload.update(kwargs)
        payload.pop("stream", None)  # 本方法强制流式
        processed_messages = self._process_messages(messages)
        self._log_params(payload)

        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        # index -> {"id", "name", "args"}：tool_calls 增量分片累积槽位
        collected_calls: Dict[int, Dict] = {}
        usage_obj = None

        def _build_final() -> Dict:
            resp: Dict = {
                "content": "".join(content_parts),
                "reasoning_content": "".join(reasoning_parts),
                "role": "assistant",
            }
            if collected_calls:
                calls = []
                for idx in sorted(collected_calls):
                    slot = collected_calls[idx]
                    calls.append({
                        "id": slot["id"] or f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": slot["name"] or "unknown",
                            "arguments": slot["args"] or "{}",
                        },
                    })
                resp["tool_calls"] = calls
            if usage_obj is not None:
                try:
                    resp["_usage"] = {
                        "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
                        "completion_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
                        "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
                    }
                except (TypeError, ValueError):
                    pass
            return resp

        try:
            litellm_kwargs = self._build_litellm_kwargs(payload, processed_messages, stream=True)
            response = completion(**litellm_kwargs)

            for chunk in response:
                if self._is_cancelled:
                    content_parts.append("\n\n[⛔ Generation halted by user.]")
                    break
                if getattr(chunk, "usage", None):
                    usage_obj = chunk.usage
                if not getattr(chunk, 'choices', None) or not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                reasoning = _extract_reasoning(delta)
                if reasoning:
                    reasoning_parts.append(reasoning)
                    yield {"type": "reasoning", "text": reasoning}

                content = getattr(delta, 'content', None)
                if content:
                    content_parts.append(content)
                    yield {"type": "text", "text": content}

                for tc in (getattr(delta, 'tool_calls', None) or []):
                    slot = collected_calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn:
                        if fn.name:
                            slot["name"] = fn.name
                        if fn.arguments:
                            slot["args"] += fn.arguments

        except Exception as e:
            text, label = self._stream_error_map(e)
            if text is not None:
                self.logger.error(f"{label}\n{_raw_error_text(e)}")
                content_parts.append(text)
            elif self._is_cancelled or "closed" in str(e).lower() or "cancel" in str(e).lower():
                content_parts.append("\n\n[⛔ Generation halted by user.]")
            else:
                self.logger.error(f"Unexpected system error.\n{_raw_error_text(e)}", exc_info=True)
                content_parts.append(f"\n\n[System Error: {type(e).__name__}: {e}]\n")

        yield {"type": "final", "response": _build_final()}


    def generate_image(self, prompt: str, **kwargs) -> str:
        """
        补全的多模态：统一的图像生成接口。
        支持 DALL-E, Midjourney (需要对应的代理 API), 或兼容的模型。

        返回值统一为可直接嵌入 <img src=...> 的地址：
        - 提供方返回 URL 时原样返回；
        - 仅返回 b64_json 时落盘为本地临时文件并返回 file:// URI
          （部分图像生成专用模型不返回 URL，只返回 base64）。
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

            data = res.data[0] if res and getattr(res, "data", None) else None
            url = str(getattr(data, "url", None) or "").strip()
            if url:
                return url

            # 兜底：仅返回 base64 时写入本地缓存，返回 file:// URI
            b64 = str(getattr(data, "b64_json", None) or "").strip()
            if b64:
                import base64
                import hashlib
                from src.core.output_paths import generated_image_dir
                img_bytes = base64.b64decode(b64)
                file_name = f"scholar_navis_gen_{hashlib.md5(img_bytes).hexdigest()[:12]}.png"
                # 生成图会被聊天记录引用（双击打开内部查看器），因此必须落在
                # 持久化目录：临时目录在 steam-run / 系统清理后会消失，导致
                # 历史消息里的图片全部打不开。
                local_path = os.path.join(generated_image_dir(), file_name)
                if not os.path.exists(local_path):
                    with open(local_path, "wb") as f:
                        f.write(img_bytes)
                local_uri = local_path.replace("\\", "/")
                return f"file:///{local_uri}" if not local_uri.startswith("/") else f"file://{local_uri}"

            raise ValueError("Image generation returned no image data (neither url nor b64_json).")
        except Exception as e:
            self.logger.error(f"Image generation failed.\n{_raw_error_text(e)}", exc_info=True)
            raise