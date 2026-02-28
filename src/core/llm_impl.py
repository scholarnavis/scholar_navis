import base64
import json
import logging
from typing import Generator, List, Dict, Optional

import dashscope
import httpx
from openai import OpenAI, APIStatusError
from anthropic import Anthropic, APIStatusError as AnthropicAPIError
from zai import ZhipuAiClient

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

        self.client = None
        self.sdk_type = "openai"

        if self.provider_id == "anthropic" or "minimax" in self.base_url.lower():
            self.sdk_type = "anthropic"
            self.client = Anthropic(api_key=self.api_key, base_url=self.base_url, http_client=self.http_client)

        elif self.provider_id == "zhipu":
            self.sdk_type = "zhipu"
            try:
                self.client = ZhipuAiClient(api_key=self.api_key)
            except ImportError:
                self.logger.warning("zai SDK missing. Falling back to OpenAI compatible mode.")
                self.sdk_type = "openai"
                self.client = OpenAI(api_key=self.api_key, base_url=self.base_url, http_client=self.http_client)

        elif self.provider_id == "qwen":
            self.sdk_type = "qwen"
            try:
                if "dashscope" not in self.base_url and self.base_url.strip():
                    dashscope.base_http_api_url = self.base_url
                self.client = dashscope
            except ImportError:
                self.logger.warning("Dashscope SDK missing. Falling back to OpenAI compatible mode.")
                self.sdk_type = "openai"
                self.client = OpenAI(api_key=self.api_key, base_url=self.base_url, http_client=self.http_client)

        elif self.provider_id == "gemini":
            self.sdk_type = "gemini"
            try:
                from google import genai
                self.client = genai.Client(api_key=self.api_key)
            except ImportError:
                self.logger.warning("google-genai SDK missing. Falling back to OpenAI compatible mode.")
                self.sdk_type = "openai"
                self.client = OpenAI(api_key=self.api_key, base_url=self.base_url, http_client=self.http_client)

        else:
            self.sdk_type = "openai"
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url, http_client=self.http_client)

        self.logger.info(f"Initialized LLM Config: [{self.model_name}] SDK: {self.sdk_type.upper()} @ {self.base_url}")

        applied_params = self._get_payload_kwargs()
        if applied_params:
            self.logger.info(f"Applied Custom Parameters: {applied_params}")

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
                    res[name] = val_str.lower() in['true', '1', 'yes', 'on']
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
            model_params = current_model_conf.get("params",[])
        else:
            param_mode = self.config_data.get("model_params_mode", "inherit")
            model_params = self.config_data.get("model_params",[])

        provider_params = self.config_data.get("provider_params",[])
        custom_params = {}

        if param_mode == "inherit":
            custom_params = self._parse_custom_params(provider_params)
        elif param_mode == "custom":
            custom_params = self._parse_custom_params(model_params)

        return {k: v for k, v in custom_params.items() if k not in["messages", "model", "stream", "tools"]}

    def _split_openai_payload(self, payload: Dict) -> Dict:
        standard_keys = {
            "temperature", "top_p", "n", "stop", "max_tokens", "presence_penalty",
            "frequency_penalty", "logit_bias", "user", "response_format", "seed",
            "tools", "tool_choice", "parallel_tool_calls", "logprobs", "top_logprobs", "thinking"
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

    def chat(self, messages: List[Dict], **kwargs):
        payload = self._get_payload_kwargs()
        payload.update(kwargs)

        if self.sdk_type == "anthropic":
            return self._chat_anthropic(messages, **payload)
        elif self.sdk_type == "zhipu":
            return self._chat_zhipu(messages, **payload)
        elif self.sdk_type == "qwen":
            return self._chat_qwen(messages, **payload)
        elif self.sdk_type == "gemini":
            return self._chat_gemini(messages, **payload)
        else:
            return self._chat_openai(messages, **payload)

    def _chat_openai(self, messages: List[Dict], **payload):
        safe_payload = self._split_openai_payload(payload)
        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            **safe_payload
        )
        choice = response.choices[0]

        if choice.message.tool_calls:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls":[{"id": t.id, "type": "function",
                                "function": {"name": t.function.name, "arguments": t.function.arguments}} for t in
                               choice.message.tool_calls]
            }
        return choice.message.content or ""

    def _chat_zhipu(self, messages: List[Dict], **payload):
        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            **payload
        )
        choice = response.choices[0]
        if choice.message.tool_calls:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls":[{"id": t.id, "type": "function",
                                "function": {"name": t.function.name, "arguments": t.function.arguments}} for t in
                               choice.message.tool_calls]
            }
        return choice.message.content or ""

    def _chat_qwen(self, messages: List[Dict], **payload):
        is_vl_payload = any(isinstance(m["content"], list) for m in messages)
        vl_keywords =['vl', 'image', 'audio', 'video', 'vision', 'qwen3.5-plus']
        is_vl_model = any(kw in self.model_name.lower() for kw in vl_keywords)

        use_multimodal = is_vl_payload or is_vl_model

        qwen_msgs = self._convert_to_qwen_messages(messages)

        if use_multimodal:
            response = self.client.MultiModalConversation.call(
                api_key=self.api_key,
                model=self.model_name,
                messages=qwen_msgs,
                result_format='message',
                **payload
            )
        else:
            response = self.client.Generation.call(
                api_key=self.api_key,
                model=self.model_name,
                messages=qwen_msgs,
                result_format='message',
                **payload
            )

        if response.status_code == 200:
            return response.output.choices[0].message.content
        else:
            raise Exception(f"Dashscope Error[{response.code}]: {response.message}")

    def _chat_gemini(self, messages: List[Dict], **payload):
        sys_prompt, gemini_msgs = self._convert_to_gemini_messages(messages)

        tools = payload.pop("tools", None)
        if "tool_choice" in payload: payload.pop("tool_choice")

        gemini_config = self._clean_gemini_payload(payload)
        if sys_prompt:
            gemini_config["system_instruction"] = sys_prompt
        if tools:
            gemini_config["tools"] = tools

        response = self.client.models.generate_content(
            model=self.model_name,
            contents=gemini_msgs,
            config=gemini_config
        )
        return response.text

    def _chat_anthropic(self, messages: List[Dict], **payload):
        system_prompt, anthropic_msgs = self._convert_to_anthropic_messages(messages)
        anthropic_payload = self._clean_anthropic_payload(payload)

        if system_prompt:
            anthropic_payload["system"] = system_prompt

        response = self.client.messages.create(
            model=self.model_name,
            messages=anthropic_msgs,
            **anthropic_payload
        )

        text_content = ""
        tool_calls =[]
        for block in response.content:
            if block.type == "text":
                text_content += block.text
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "type": "function",
                    "function": {"name": block.name, "arguments": json.dumps(block.input)}
                })

        if tool_calls:
            return {"role": "assistant", "content": text_content, "tool_calls": tool_calls}
        return text_content

    def stream_chat(self, messages: List[Dict]) -> Generator[str, None, None]:
        payload = self._get_payload_kwargs()

        # 1. 识别并拦截图像生成模型 (Text-to-Image)
        gen_keywords =['image', 'dall', 'mj', 'picture', 'cogview']
        is_vision_understanding = any(kw in self.model_name.lower() for kw in ['vl', 'vision'])
        is_image_gen = any(kw in self.model_name.lower() for kw in gen_keywords) and not is_vision_understanding

        if is_image_gen:
            yield from self._generate_image_pseudo_stream(messages, **payload)
            return

        # 2. 正常的流式文本 / 视觉理解对话
        try:
            if self.sdk_type == "anthropic":
                yield from self._stream_anthropic(messages, **payload)
            elif self.sdk_type == "zhipu":
                yield from self._stream_zhipu(messages, **payload)
            elif self.sdk_type == "qwen":
                yield from self._stream_qwen(messages, **payload)
            elif self.sdk_type == "gemini":
                yield from self._stream_gemini(messages, **payload)
            else:
                yield from self._stream_openai(messages, **payload)

        except (APIStatusError, AnthropicAPIError) as e:
            friendly_msg = ""
            status_code = getattr(e.response, "status_code", 0) if hasattr(e, "response") else 0
            if status_code == 400:
                friendly_msg = "\n\n💡 <b>System Tip:</b> Request rejected. Make sure multi-modal features are supported by this model, or context limits aren't exceeded."
            yield f"\n\n[API Request Error: HTTP {status_code}]\n{str(e)}{friendly_msg}\n"
        except Exception as e:
            if self._is_cancelled or "closed" in str(e).lower():
                yield "\n\n[⛔ Generation halted by user.]"
            else:
                yield f"\n\n[System Error: {str(e)}]\n"

    def _stream_openai(self, messages: List[Dict], **payload) -> Generator[str, None, None]:
        is_thinking = False
        stream = payload.pop("stream", True)
        safe_payload = self._split_openai_payload(payload)

        response = self.client.chat.completions.create(model=self.model_name, messages=messages, stream=stream,
                                                       **safe_payload)

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

            reasoning = getattr(delta, 'reasoning_content', None)
            if not reasoning and hasattr(delta, 'model_extra') and delta.model_extra:
                reasoning = delta.model_extra.get('reasoning_content')

            if reasoning:
                if not is_thinking:
                    yield "<think>\n"
                    is_thinking = True
                yield reasoning

            content = delta.content
            if content:
                if is_thinking:
                    yield "\n</think>\n"
                    is_thinking = False
                yield content

        if is_thinking: yield "\n</think>\n"

    def _generate_image_pseudo_stream(self, messages: List[Dict], **payload) -> Generator[str, None, None]:
        self.logger.info(f"Routing to Image Generation endpoint for model: {self.model_name}")
        yield "🎨 *正在挥洒创意，绘制图像中，请稍候...*\n\n"

        last_msg = messages[-1]["content"]
        if isinstance(last_msg, list):
            prompt = " ".join([p.get("text", "") for p in last_msg if p.get("type") == "text"])
        else:
            prompt = last_msg

        try:
            if self.sdk_type == "zhipu":
                res = self.client.images.generations(
                    model=self.model_name,
                    prompt=prompt,
                    **self._split_openai_payload(payload)
                )
                img_url = res.data[0].url
                yield f"![Generated Image]({img_url})"

            elif self.sdk_type == "qwen":
                qwen_msgs =[
                    {
                        "role": "user",
                        "content": [{"text": prompt}]
                    }
                ]
                payload["stream"] = False
                res = self.client.MultiModalConversation.call(
                    api_key=self.api_key,
                    model=self.model_name,
                    messages=qwen_msgs,
                    result_format='message',
                    **payload
                )

                if res.status_code == 200:
                    content_data = res.output.choices[0].message.content
                    if isinstance(content_data, list):
                        for item in content_data:
                            if "image" in item:
                                yield f"![Generated Image]({item['image']})\n\n"
                            elif "text" in item:
                                yield f"{item['text']}\n"
                    else:
                        yield str(content_data)
                else:
                    yield f"\n\n❌ 生图失败：[{res.code}] {res.message}"

            elif self.sdk_type == "openai" and "dall" in self.model_name.lower():
                res = self.client.images.generate(
                    model=self.model_name,
                    prompt=prompt,
                    **self._split_openai_payload(payload)
                )
                img_url = res.data[0].url
                yield f"![Generated Image]({img_url})"

            elif self.sdk_type == "gemini":
                valid_keys =["number_of_images", "aspect_ratio", "output_mime_type", "person_generation",
                              "safety_settings"]
                gemini_config = {k: v for k, v in payload.items() if k in valid_keys}

                res = self.client.models.generate_images(
                    model=self.model_name,
                    prompt=prompt,
                    config=gemini_config if gemini_config else None
                )

                for generated_image in res.generated_images:
                    img = generated_image.image
                    if hasattr(img, "image_bytes") and img.image_bytes:
                        b64_data = base64.b64encode(img.image_bytes).decode("utf-8")
                        mime_type = payload.get("output_mime_type", "image/jpeg")
                        yield f"![Generated Image](data:{mime_type};base64,{b64_data})\n\n"
                    else:
                        yield f"\n\n❌ Gemini 图像生成成功，但无法提取图像字节数据。"
            else:
                yield "\n\n❌ 当前服务商暂不支持该图像生成模型的直接调用。"

        except Exception as e:
            if self._is_cancelled:
                yield "\n\n[⛔ 生图已取消]"
            else:
                yield f"\n\n[❌ 生图异常: {str(e)}]"

    def _stream_zhipu(self, messages: List[Dict], **payload) -> Generator[str, None, None]:
        is_thinking = False
        stream = payload.pop("stream", True)

        response = self.client.chat.completions.create(model=self.model_name, messages=messages, stream=stream,
                                                       **payload)

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

            reasoning = getattr(delta, 'reasoning_content', None)
            if not reasoning and hasattr(delta, 'model_extra') and delta.model_extra:
                reasoning = delta.model_extra.get('reasoning_content')

            if reasoning:
                if not is_thinking:
                    yield "<think>\n"
                    is_thinking = True
                yield reasoning

            content = delta.content
            if content:
                if is_thinking:
                    yield "\n</think>\n"
                    is_thinking = False
                yield content

        if is_thinking: yield "\n</think>\n"

    def _stream_qwen(self, messages: List[Dict], **payload) -> Generator[str, None, None]:
        is_vl_payload = any(isinstance(m["content"], list) for m in messages)
        vl_keywords =['vl', 'image', 'audio', 'video', 'vision', 'qwen3.5-plus']
        is_vl_model = any(kw in self.model_name.lower() for kw in vl_keywords)

        use_multimodal = is_vl_payload or is_vl_model
        qwen_msgs = self._convert_to_qwen_messages(messages)
        is_thinking = False

        if use_multimodal:
            responses = self.client.MultiModalConversation.call(
                api_key=self.api_key,
                model=self.model_name,
                messages=qwen_msgs,
                stream=True,
                incremental_output=True,
                result_format='message',
                **payload
            )
        else:
            responses = self.client.Generation.call(
                api_key=self.api_key,
                model=self.model_name,
                messages=qwen_msgs,
                stream=True,
                incremental_output=True,
                result_format='message',
                **payload
            )

        for chunk in responses:
            if self._is_cancelled:
                if is_thinking: yield "\n</think>\n"
                yield "\n\n[⛔ Generation halted by user.]"
                break

            if chunk.status_code == 200:
                choice = chunk.output.choices[0]
                msg = choice.message

                reasoning = getattr(msg, 'reasoning_content', '')
                content = getattr(msg, 'content', '')

                if reasoning:
                    if not is_thinking:
                        yield "<think>\n"
                        is_thinking = True
                    yield reasoning

                if content:
                    if is_thinking and not reasoning:
                        yield "\n</think>\n"
                        is_thinking = False
                    yield content
            else:
                yield f"\n\n[Dashscope Error: {chunk.message}]"
                break

        if is_thinking: yield "\n</think>\n"

    def _stream_gemini(self, messages: List[Dict], **payload) -> Generator[str, None, None]:
        sys_prompt, gemini_msgs = self._convert_to_gemini_messages(messages)

        gemini_config = self._clean_gemini_payload(payload)
        if sys_prompt:
            gemini_config["system_instruction"] = sys_prompt

        responses = self.client.models.generate_content_stream(
            model=self.model_name,
            contents=gemini_msgs,
            config=gemini_config
        )

        for chunk in responses:
            if self._is_cancelled:
                yield "\n\n[⛔ Generation halted by user.]"
                break
            if chunk.text:
                yield chunk.text

    def _stream_anthropic(self, messages: List[Dict], **payload) -> Generator[str, None, None]:
        system_prompt, anthropic_msgs = self._convert_to_anthropic_messages(messages)
        anthropic_payload = self._clean_anthropic_payload(payload)

        if system_prompt:
            anthropic_payload["system"] = system_prompt

        is_thinking = False

        with self.client.messages.stream(
                model=self.model_name,
                messages=anthropic_msgs,
                **anthropic_payload
        ) as stream:
            for event in stream:
                if self._is_cancelled:
                    if is_thinking: yield "\n</think>\n"
                    yield "\n\n[⛔ Generation halted by user.]"
                    break

                if event.type == "content_block_start" and event.content_block.type == "thinking":
                    if not is_thinking:
                        yield "<think>\n"
                        is_thinking = True
                elif event.type == "content_block_delta":
                    if event.delta.type == "thinking_delta":
                        yield event.delta.thinking
                    elif event.delta.type == "text_delta":
                        if is_thinking:
                            yield "\n</think>\n"
                            is_thinking = False
                        yield event.delta.text

            if is_thinking:
                yield "\n</think>\n"

    def _convert_to_qwen_messages(self, messages: List[Dict]) -> List[Dict]:
        qwen_msgs = []
        for m in messages:
            if isinstance(m["content"], list):
                content = []
                for part in m["content"]:
                    if part.get("type") == "text":
                        content.append({"text": part["text"]})
                    elif part.get("type") == "image_url":
                        content.append({"image": part["image_url"]["url"]})
                qwen_msgs.append({"role": m["role"], "content": content})
            else:
                qwen_msgs.append(m)
        return qwen_msgs

    def _clean_gemini_payload(self, payload: Dict) -> Dict:
        gemini_config = {}
        if "max_tokens" in payload:
            gemini_config["max_output_tokens"] = payload.pop("max_tokens")
        if "temperature" in payload:
            gemini_config["temperature"] = payload.pop("temperature")
        if "top_p" in payload:
            gemini_config["top_p"] = payload.pop("top_p")
        if "top_k" in payload:
            gemini_config["top_k"] = payload.pop("top_k")
        if "stop" in payload:
            gemini_config["stop_sequences"] = payload.pop("stop")
        if "response_format" in payload:
            fmt = payload.pop("response_format")
            if isinstance(fmt, dict) and fmt.get("type") == "json_object":
                gemini_config["response_mime_type"] = "application/json"

        exclude_keys = {"stream", "model", "messages", "tools", "tool_choice", "parallel_tool_calls"}
        for k, v in payload.items():
            if k not in exclude_keys:
                gemini_config[k] = v

        return gemini_config

    def _convert_to_gemini_messages(self, messages: List[Dict]):
        sys_prompt = ""
        gemini_msgs =[]
        import base64

        try:
            from google.genai import types
            has_types = True
        except ImportError:
            has_types = False

        for m in messages:
            if m["role"] == "system":
                sys_prompt += m["content"] + "\n"
            else:
                role = "user" if m["role"] == "user" else "model"
                parts = []
                if isinstance(m["content"], list):
                    for part in m["content"]:
                        if part.get("type") == "text":
                            if has_types:
                                parts.append(types.Part.from_text(text=part["text"]))
                            else:
                                parts.append({"text": part["text"]})
                        elif part.get("type") == "image_url":
                            url = part["image_url"]["url"]
                            if url.startswith("data:"):
                                mime = url.split(";")[0].split(":")[1]
                                b64_data = url.split(",")[1]
                                decoded_data = base64.b64decode(b64_data)
                                if has_types:
                                    parts.append(types.Part.from_bytes(data=decoded_data, mime_type=mime))
                                else:
                                    parts.append({"inline_data": {"mime_type": mime, "data": decoded_data}})
                else:
                    if has_types:
                        parts.append(types.Part.from_text(text=m["content"]))
                    else:
                        parts.append({"text": m["content"]})

                if has_types:
                    gemini_msgs.append(types.Content(role=role, parts=parts))
                else:
                    gemini_msgs.append({"role": role, "parts": parts})

        return sys_prompt.strip(), gemini_msgs

    def _clean_anthropic_payload(self, payload: Dict) -> Dict:
        anthropic_payload = {}

        if "max_tokens" in payload:
            anthropic_payload["max_tokens"] = payload.pop("max_tokens")
        else:
            anthropic_payload["max_tokens"] = 4096

        if "tools" in payload:
            anthropic_tools =[
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"]["parameters"]
                } for t in payload.pop("tools")
            ]
            anthropic_payload["tools"] = anthropic_tools

            if "tool_choice" in payload:
                tc = payload.pop("tool_choice")
                if tc == "auto" or tc == "required":
                    anthropic_payload["tool_choice"] = {"type": "auto"} if tc == "auto" else {"type": "any"}
                elif isinstance(tc, dict) and "function" in tc:
                    anthropic_payload["tool_choice"] = {"type": "tool", "name": tc["function"]["name"]}

        if "stop" in payload:
            stop_val = payload.pop("stop")
            if isinstance(stop_val, str):
                anthropic_payload["stop_sequences"] = [stop_val]
            elif isinstance(stop_val, list):
                anthropic_payload["stop_sequences"] = stop_val

        exclude_keys = {"presence_penalty", "frequency_penalty", "logit_bias", "user", "response_format", "seed",
                        "parallel_tool_calls", "logprobs", "top_logprobs", "stream"}
        for k, v in payload.items():
            if k not in exclude_keys:
                anthropic_payload[k] = v

        return anthropic_payload

    def _convert_to_anthropic_messages(self, messages: List[Dict]):
        system_prompt = ""
        raw_msgs = []

        for msg in messages:
            if msg["role"] == "system":
                system_prompt += msg["content"] + "\n"
            elif msg["role"] == "tool":
                tool_content = str(msg.get("content", ""))
                if not tool_content.strip():
                    tool_content = "Executed successfully."
                raw_msgs.append({
                    "role": "user",
                    "content":[{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": tool_content
                    }]
                })
            else:
                new_content = []
                content = msg["content"]

                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "text":
                            new_content.append({"type": "text", "text": block["text"]})
                        elif block.get("type") == "image_url":
                            url = block["image_url"]["url"]
                            if url.startswith("data:"):
                                try:
                                    mime_data = url.split(";")[0]
                                    mime = mime_data.split(":")[1] if ":" in mime_data else "image/jpeg"
                                    b64_data = url.split(",")[1]
                                    new_content.append({
                                        "type": "image",
                                        "source": {"type": "base64", "media_type": mime, "data": b64_data}
                                    })
                                except Exception as e:
                                    new_content.append({"type": "text", "text": f"[Failed to process image: {e}]"})
                            else:
                                # 对于普通 URL, 如果未能转换为 base64, 则降级以文本呈现供模型自行抓取或阅读
                                new_content.append({"type": "text", "text": f"[Image URL: {url}]"})
                else:
                    if content:
                        new_content.append({"type": "text", "text": str(content)})

                # 提取模型侧调用的工具 (需回传给上下文保证连贯性)
                if msg["role"] == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        fn = tc.get("function", {})
                        args = fn.get("arguments", "{}")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except:
                                args = {}
                        new_content.append({
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": fn.get("name", ""),
                            "input": args
                        })

                if not new_content:
                    new_content.append({"type": "text", "text": " "})

                raw_msgs.append({"role": msg["role"], "content": new_content})

        merged_msgs =[]
        for rm in raw_msgs:
            role = rm["role"]
            content = rm["content"]
            if merged_msgs and merged_msgs[-1]["role"] == role:
                merged_msgs[-1]["content"].extend(content)
            else:
                merged_msgs.append({"role": role, "content": content})

        return system_prompt.strip(), merged_msgs