import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Generator, List, Dict, Optional, Tuple

import litellm
from litellm import completion, image_generation
from litellm.exceptions import APIError, APIConnectionError, ContextWindowExceededError, RateLimitError, Timeout, \
    AuthenticationError, ServiceUnavailableError, BadRequestError, NotFoundError

from src.core.config_manager import ConfigManager
from src.core.model_capabilities import (KNOWN_REQUEST_PARAMS, cached_max_output_tokens,
                                         record_context_window, record_max_output_tokens,
                                         record_unsupported_param, unsupported_params)
from src.core.think_tags import (THINK_CLOSE_TAG, THINK_OPEN_TAG,
                                 ThinkStreamSplitter, strip as strip_think_blocks)


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
#: ``thinking`` / ``thinking_content``，聚合网关（OpenRouter 等）用
#: ``reasoning_details``（结构化分片列表）。只识别前者会导致思考链落入正文，
#: 因此这里集中做跨字段兼容，避免各处重复判断而口径不一。
_REASONING_FIELD_NAMES = ("reasoning_content", "reasoning", "thinking", "thinking_content",
                          "reasoning_details")


def _coerce_reasoning_value(value) -> str:
    """把 provider 返回的推理字段值规整为纯文本（恒返回 ``str``）。

    字段形态可能是 str、dict（``{"thinking"|"text"|"content": ...}``）、
    或 list（Anthropic/Gemini 经 litellm 的 ``thinking_blocks``；聚合网关的
    ``reasoning_details`` 元素用 ``{"type": "reasoning.summary", "summary": ...}``）。
    未知形态一律返回空串，绝不抛出，避免流式过程中因单条脏数据中断整轮对话。
    """
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("thinking", "text", "content", "summary",
                    "reasoning_content", "reasoning"):
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


def _usage_tokens(usage) -> Tuple[int, int]:
    """把 litellm Usage 对象或 ``_usage`` dict 归一化为 ``(prompt, completion)``。

    ``usage`` 缺失（流式接口常不回传）时返回 ``(0, 0)``——日志里显示 0 就是
    "provider 没给用量"，与"用量真的是 0"在耗时分析里无需区分。
    """
    if usage is None:
        return 0, 0
    if isinstance(usage, dict):
        getter = usage.get
    else:
        def getter(name, default=0):
            return getattr(usage, name, default)
    try:
        return (int(getter("prompt_tokens", 0) or 0),
                int(getter("completion_tokens", 0) or 0))
    except (TypeError, ValueError):
        return 0, 0


class _CallTimer:
    """单次 LLM 调用的耗时统计（三个调用入口共用的唯一实现）。

    回答"这一轮为什么慢"需要两类时间，缺一不可：

    * ``ttfb`` —— 请求发出到**首个** token 到达。它包含连接、排队与大上下文的
      prefill；首字迟通常意味着 prompt 太大、provider 在排队或被限速；
    * ``total`` —— 到响应结束（流式读完最后一个分片）的墙钟耗时。首字正常而
      total 很长，说明慢在**生成**（输出长 / 思维链长）。

    使用约定：发起请求前构造，首个 token 处调 :meth:`first_token`，响应结束时调
    :meth:`done`；**异常路径也必须调** ``done(note="failed")``——否则"卡到超时的那次
    调用"恰恰成了唯一不留痕的调用。日志级别与 ``Request Parameters`` 一致（INFO）。
    """

    __slots__ = ("_label", "_logger", "_model", "_t0", "_ttfb", "_done")

    def __init__(self, kind: str, logger: logging.Logger, model: str,
                 is_translation: bool = False):
        self._label = f"{kind}[translation]" if is_translation else kind
        self._logger = logger
        self._model = model
        self._t0 = time.perf_counter()
        self._ttfb: Optional[float] = None
        self._done = False

    def first_token(self) -> None:
        """记录首个 token 到达时刻（重复调用只认第一次）。"""
        if self._ttfb is None:
            self._ttfb = time.perf_counter()

    def done(self, usage=None, chars: int = 0, note: str = "") -> None:
        """输出本次调用的耗时与用量（``usage`` 可为 litellm Usage 或 dict）。

        幂等：一次调用只留一行。异常分支已记过 `failed` 后，收尾处的常规调用
        因为同一个 timer 会再记一次——那第二行只会有"耗时正常"的假象，必须挡掉。
        """
        if self._done:
            return
        self._done = True
        total = time.perf_counter() - self._t0
        prompt_tokens, completion_tokens = _usage_tokens(usage)
        ttfb = f"{self._ttfb - self._t0:.2f}s" if self._ttfb is not None else "n/a"
        self._logger.info(
            f"[{self._model}] {self._label} finished in {total:.2f}s "
            f"(ttfb={ttfb}, prompt={prompt_tokens}, completion={completion_tokens}, "
            f"chars={chars}{', ' + note if note else ''})")


# ---------------------------------------------------------------------- #
#  请求自适应（纯解析部分）
#
#  provider 对"不接受的参数 / 超限的输出长度 / 超限的上下文"都只会在报错文案里
#  给出线索，且措辞各家不同。这里把"从文案里读出事实"的解析集中定义，全部是
#  纯函数（可脱机测试）；"怎么用这些事实"由 OpenAICompatibleLLM 的适配方法负责。
# ---------------------------------------------------------------------- #
#: 400 文案里"该参数不被接受"的常见措辞（与参数名同句出现才算数）。
_PARAM_REJECT_HINTS = (
    "unsupported", "not supported", "does not support", "unrecognized",
    "unknown parameter", "unexpected", "invalid", "not allowed", "extra field",
)
#: 允许出现在"拒绝"句式里的参数名（白名单，避免误伤 content 之类的普通词）。
_HINT_ALT = "|".join(re.escape(h) for h in _PARAM_REJECT_HINTS)

#: "输出上限"文案里数字的常见位置（仅当文案提到 max_tokens 时才启用，见下）。
_MAX_TOKENS_PATTERNS = (
    # "Invalid 'max_tokens': maximum value is 65536." / "max_tokens is too large ..., limit is 8192"
    r"max_tokens[^.\n]{0,80}?(?:maximum|limit|at most|no more than|must be|"
    r"less than or equal to|exceeds)[^0-9]{0,24}(\d[\d,_]*)",
    # "This model supports at most 65536 completion tokens." / "... supports up to 8192 tokens"
    r"(?:supports?(?: up to| at most| at least)|at most|no more than|limited to|"
    r"maximum(?: value)?(?: of| is| for)?)[^0-9]{0,24}(\d[\d,_]*)\s*(?:completion )?tokens",
    # "max_tokens 999999 exceeds the maximum 65536"
    r"max_tokens[^.\n]{0,40}?(\d[\d,_]*)[^.\n]{0,40}?(?:is the maximum|maximum)",
)


def detect_max_tokens_limit(message: str) -> Optional[int]:
    """从报错文案里解析输出上限（``max_tokens`` 超限时 provider 会给出真值）。

    只在文案确实提到 ``max_tokens`` 时才解析：通用的"at most N tokens"句式也出现在
    上下文超限文案里，不加这个门槛会把上下文窗口误当成输出上限。
    """
    text = str(message or "").lower()
    if "max_tokens" not in text and "max_completion_tokens" not in text:
        return None
    return _find_count(_MAX_TOKENS_PATTERNS, message)

#: "上下文窗口"文案里数字的常见位置。
_CONTEXT_LIMIT_PATTERNS = (
    r"maximum context length is[^0-9]{0,16}(\d[\d,_]*)",
    r"context (?:window|length)[^.\n]{0,60}?(\d[\d,_]*)\s*tokens",
    r"maximum (?:allowed )?(?:number of )?tokens[^0-9]{0,24}(\d[\d,_]*)",
)


def _parse_count(raw: str) -> int:
    """把 ``"131,072"`` / ``"65_536"`` 这类计数还原成 int。"""
    return int(str(raw).replace(",", "").replace("_", "").replace(" ", ""))


def _find_count(patterns, message: str) -> Optional[int]:
    """按给定句式列表在文案里找第一个计数（找不到返回 None）。"""
    text = str(message or "").lower()
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                return _parse_count(match.group(1))
            except ValueError:
                continue
    return None


def detect_rejected_param(message: str, payload: Dict) -> Optional[str]:
    """从报错文案里识别"哪个已发送的参数不被接受"（识别不出返回 None）。

    只认"报错方明确点名"的句式，不做启发式猜测：误删用户本来可用的参数
    比继续报错更糟。
    """
    text = str(message or "").lower()
    if not text:
        return None
    for name in payload:
        if name not in KNOWN_REQUEST_PARAMS:
            continue
        token = re.escape(str(name).lower())
        if (re.search(rf"(?:{_HINT_ALT})[^.\n]{{0,60}}{token}\b", text)
                or re.search(rf"\b{token}\b[^.\n]{{0,60}}(?:{_HINT_ALT})", text)):
            return name
    return None


def detect_context_limit(message: str) -> Optional[int]:
    """从报错文案里解析上下文窗口（超限时 provider 会给出真值）。"""
    return _find_count(_CONTEXT_LIMIT_PATTERNS, message)


#: 中转站常把 400 原样抛出（不发 ContextWindowExceededError），文案是唯一线索。
_CONTEXT_OVERFLOW_HINTS = (
    "maximum context length", "context length exceeded", "context window exceeded",
    "reduce the length of the messages", "too many tokens", "input is too long",
    "exceeds the maximum number of tokens",
)


def is_context_overflow_error(error: Exception) -> bool:
    """异常是否表示"上下文超限"（类型优先，文案兜底）。唯一判定处。

    调用方据此决定"压缩历史后重试"，因此判定必须宽：漏判会把本可自动恢复的
    请求变成用户可见的报错。
    """
    if isinstance(error, ContextWindowExceededError):
        return True
    text = _raw_error_text(error).lower()
    return any(hint in text for hint in _CONTEXT_OVERFLOW_HINTS)


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

        # 代理由 LiteLLM 内部的 HTTP 客户端从进程环境变量读取；环境变量统一由
        # network_worker.setup_global_network_env 按 system/off/custom 三态写入，
        # 这里不再单独判断（旧代码取了 proxy_cfg 却没有使用，属于死赋值）。
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

        payload = {k: v for k, v in custom_params.items()
                   if k not in ["messages", "model", "stream", "tools"]}
        # 用户声明的输出上限（provider 配置 max_output_tokens）：只在用户明确填了、
        # 且没有自定义 max_tokens 时下发——把"我声明这个模型能出多长"与"这次要它出多长"
        # 分开，避免悄悄改变默认行为。
        declared_output = self.config_data.get("max_output_tokens")
        if declared_output and not any(k in payload for k in ("max_tokens", "max_completion_tokens")):
            try:
                payload["max_tokens"] = int(declared_output)
            except (TypeError, ValueError):
                pass
        # 所有调用入口都经过本方法，因此"不再发送已被实测拒绝的参数"只需在这一处
        # 定义（证据来自能力表，见 src.core.model_capabilities）。
        return self._strip_unsupported_params(payload)

    def _context_char_budget(self) -> int:
        """按当前模型窗口推导 `### CONTEXT` 的字符预算，而不是固定 14k。

        2026 年主流窗口是 128K–1M；固定 14k 字符（约 4k token）会把检索
        文献和附件切得只剩骨架。这里复用 token 预算：KB 预算 × 约 4 字符/
        token，并保留 50% 窗口的硬上限。
        """
        cached = getattr(self, "_context_char_budget_cache", None)
        if cached is not None:
            return cached
        try:
            from src.core.token_estimator import (derive_context_budgets,
                                                  resolve_context_window)
            declared = (getattr(self, "config_data", None) or {}).get("context_window")
            window = resolve_context_window(self.model_name, self.base_url, declared)
            budgets = derive_context_budgets(window)
            value = max(14_000, int(budgets["kb_token_budget"] * 4.0))
            value = min(value, max(14_000, int(window * 0.50 * 4.0)))
        except Exception:
            value = 48_000
        self._context_char_budget_cache = value
        return value

    def _tool_message_char_budget(self) -> int:
        try:
            from src.core.token_estimator import (derive_context_budgets,
                                                  resolve_context_window)
            window = resolve_context_window(self.model_name, self.base_url)
            return max(6_000, int(derive_context_budgets(window)["tool_result_chars"]))
        except Exception:
            return 12_000

    def _trim_context_block(self, text: str) -> str:
        marker = "### CONTEXT:"
        idx = text.find(marker)
        if idx < 0:
            return text
        limit = self._context_char_budget()
        body_start = idx + len(marker)
        if len(text) - body_start <= limit:
            return text
        return (text[:body_start] + "\n"
                + text[body_start:body_start + limit]
                + "\n...[context truncated by client to fit provider limits]")

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
                text = str(content) if content is not None else ""
                # 历史里的思考/工具过程不是用户内容：发给 provider 前剥掉，
                # 既省 token，也避免工具参数/搜索结果里的词触发内容审查。
                if role != "user":
                    text = strip_think_blocks(text)
                    text = re.sub(r"\[Status:\s*Executing\][^\n]*", "", text,
                                  flags=re.IGNORECASE)
                if role == "tool":
                    text = text[:self._tool_message_char_budget()]
                elif role == "system":
                    text = self._trim_context_block(text)
                msg_dict["content"] = text

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

    # ------------------------------------------------------------------ #
    #  请求自适应：把 provider 的明确拒绝变成"下次不再犯错"
    # ------------------------------------------------------------------ #
    def _strip_unsupported_params(self, payload: Dict) -> Dict:
        """剔除已被该模型明确拒绝的请求参数，并收紧已实测的输出上限。

        只在"曾经被明确拒绝过"时才剔除——绝不预先猜测能力：猜错会让用户本来
        可用的参数失效，而 provider 的拒绝是权威证据。记录来源见
        :mod:`src.core.model_capabilities`。
        """
        rejected = unsupported_params(self.base_url, self.model_name)
        if rejected:
            dropped = [k for k in list(payload) if k in rejected]
            for key in dropped:
                payload.pop(key, None)
            if dropped:
                self.logger.info(f"[{self.model_name}] Omitting parameters previously "
                                 f"rejected by the provider: {dropped}")
        limit = cached_max_output_tokens(self.base_url, self.model_name)
        if limit:
            for key in ("max_tokens", "max_completion_tokens"):
                try:
                    if int(payload.get(key) or 0) > limit:
                        self.logger.info(f"[{self.model_name}] Clamping '{key}' to the "
                                         f"measured output limit {limit}.")
                        payload[key] = limit
                except (TypeError, ValueError):
                    continue
        return payload

    def _adapt_after_error(self, payload: Dict, error: Exception) -> Optional[Dict]:
        """依 provider 的明确报错修正请求参数；无法修正返回 None。

        只处理有权威证据的两类（各重试一次即止，不循环）：

        1. 参数不被接受 —— 剔除该参数，并记入能力表（后续请求不再发送）；
        2. ``max_tokens`` 超上限 —— 下调到 provider 给出的上限，同样记入能力表。

        上下文超限需要"砍历史"，属于上层（AgentRuntime）的职责，这里只把 provider
        透露的真值记入能力表后原样抛出。
        """
        message = _raw_error_text(error)
        new_payload = dict(payload)
        changed = False

        rejected = detect_rejected_param(message, new_payload)
        if rejected:
            new_payload.pop(rejected, None)
            record_unsupported_param(self.base_url, self.model_name, rejected)
            self.logger.warning(f"[{self.model_name}] Provider rejected parameter "
                                f"'{rejected}'; dropping it and retrying once.")
            changed = True

        limit = detect_max_tokens_limit(message)
        if limit:
            for key in ("max_tokens", "max_completion_tokens"):
                if key in new_payload:
                    new_payload[key] = min(int(new_payload.get(key) or limit), limit)
                    changed = True
            record_max_output_tokens(self.base_url, self.model_name, limit)
            self.logger.warning(f"[{self.model_name}] max_tokens exceeds the provider "
                                f"limit {limit}; lowering it and retrying once.")

        return new_payload if changed else None

    @staticmethod
    def _is_content_inspection_error(error: Exception) -> bool:
        text = _raw_error_text(error).lower()
        return any(token in text for token in (
            "datainspectionfailed", "inappropriate content",
            "content filter", "content_policy", "content policy",
            "safety system", "moderation",
        ))

    def _minimal_retry_messages(self, messages: List[Dict]) -> List[Dict]:
        """内容审查 400 的最后退路：只保留系统规则 + 最后一条用户消息。

        自动证据/附件全文里可能夹着触发审查的词；把 `### CONTEXT:` 整段去掉、
        丢掉历史与工具消息，通常能换回一次可用回答。失败仍按原错误抛出。
        """
        system_text = ""
        last_user = ""
        for msg in messages or []:
            role = msg.get("role")
            content = msg.get("content")
            if not isinstance(content, str):
                continue
            if role == "system" and not system_text:
                system_text = content
            elif role == "user":
                last_user = content
        if system_text:
            idx = system_text.find("### CONTEXT:")
            if idx >= 0:
                system_text = system_text[:idx].rstrip()
            system_text = system_text[:24000]
        minimal = []
        if system_text:
            minimal.append({"role": "system", "content": system_text})
        if last_user:
            minimal.append({"role": "user", "content": last_user[:24000]})
        return minimal

    def _completion_with_adaptation(self, kwargs: Dict, payload: Dict):
        """发起一次 completion；参数被拒/超限时修正参数并**重试一次**。

        只对"参数层面"的错误重试——这类错误必然发生在产生任何输出之前，重试
        不会造成重复输出或重复副作用。其余异常原样抛出，交由上层决定
        （网络重试、上下文压缩等）。
        """
        try:
            return completion(**kwargs)
        except Exception as e:
            if is_context_overflow_error(e):
                # 上下文超限要在"砍历史"那一层解决（AgentRuntime），这里只把 provider
                # 给出的真值记入能力表，让下一轮的预算直接按它收紧。
                self._remember_context_limit(e)
                raise
            if self._is_content_inspection_error(e):
                minimal = self._minimal_retry_messages(kwargs.get("messages") or [])
                if minimal:
                    self.logger.warning(
                        "Provider content inspection rejected the input; retrying once "
                        "with minimal context (system rules + last user message).")
                    retry_kwargs = dict(kwargs)
                    retry_kwargs["messages"] = minimal
                    return completion(**retry_kwargs)
            adapted = self._adapt_after_error(payload, e)
            if adapted is None:
                raise
            rebuilt = self._build_litellm_kwargs(adapted, kwargs.get("messages") or [],
                                                 stream=bool(kwargs.get("stream")))
            return completion(**rebuilt)

    def _remember_context_limit(self, error: Exception) -> None:
        """把 provider 报错文案里的上下文窗口真值记入能力表（下次预算直接用它）。"""
        limit = detect_context_limit(_raw_error_text(error))
        if limit:
            record_context_window(self.base_url, self.model_name, limit)

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
        timer = _CallTimer("chat", self.logger, self.model_name,
                           is_translation=is_translation)

        try:
            litellm_kwargs = self._build_litellm_kwargs(payload, processed_messages, stream=False)
            response = self._completion_with_adaptation(litellm_kwargs, payload)
            choice = response.choices[0]

            reasoning = _extract_reasoning(choice.message)

            if getattr(choice.message, 'tool_calls', None):
                msg_dump = choice.message.model_dump(exclude_none=True)
                msg_dump["reasoning_content"] = reasoning or ""
                if not msg_dump.get("content"):
                    msg_dump["content"] = ""
                self._attach_usage(response, msg_dump)
                timer.done(usage=getattr(response, "usage", None),
                           chars=len(msg_dump.get("content") or ""),
                           note=f"tool_calls={len(choice.message.tool_calls)}")
                return msg_dump

            result = {
                "content": choice.message.content or "",
                "reasoning_content": reasoning or "",
                "role": "assistant"
            }
            self._attach_usage(response, result)
            timer.done(usage=getattr(response, "usage", None),
                       chars=len(result["content"]) + len(result["reasoning_content"]))
            return result
        except Exception as e:
            timer.done(note=f"failed: {type(e).__name__}")
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
        timer = _CallTimer("stream", self.logger, self.model_name,
                           is_translation=is_translation)
        emitted = 0

        # 思考/正文的分流交给统一状态机（src/core/think_tags.py）：它识别全部
        # provider 写法（thinking/reasoning/管道/Harmony/Kimi），允许正文之后
        # 重新进入思考态，并暂扣被切开的标签。旧的"只认字面量 <think>、一次
        # 闭合后永久失效"的状态机正是思考链漏进正文的根因。
        splitter = ThinkStreamSplitter()
        emitted_think_open = False

        def _render(reasoning: str, content: str):
            """把切分出的两路文本还原成带规范标签的单流（UI 按标签折叠）。"""
            nonlocal emitted_think_open
            out = []
            if reasoning:
                if not emitted_think_open:
                    out.append(THINK_OPEN_TAG + "\n")
                    emitted_think_open = True
                out.append(reasoning)
            if content:
                if emitted_think_open:
                    out.append("\n" + THINK_CLOSE_TAG + "\n\n")
                    emitted_think_open = False
                out.append(content)
            return out

        try:
            litellm_kwargs = self._build_litellm_kwargs(payload, processed_messages, stream=stream)
            response = self._completion_with_adaptation(litellm_kwargs, payload)

            if not stream:
                text = response.choices[0].message.content or ""
                timer.done(usage=getattr(response, "usage", None), chars=len(text))
                yield text
                return

            for chunk in response:
                if self._is_cancelled:
                    if emitted_think_open:
                        yield "\n" + THINK_CLOSE_TAG + "\n\n"
                        emitted_think_open = False
                    yield "\n\n[⛔ Generation halted by user.]"
                    break

                if not getattr(chunk, 'choices', None) or not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                # 提取思考内容（跨 provider 字段名兼容，务必先于 content 判定，
                # 避免思考链被当成正文输出）
                reasoning = _extract_reasoning(delta)
                # 提取正文内容
                content = getattr(delta, 'content', None)

                if not reasoning and not content:
                    continue

                timer.first_token()
                for r_part, c_part in splitter.feed_chunk(reasoning or "", content or ""):
                    for piece in _render(r_part, c_part):
                        emitted += len(piece)
                        yield piece

            # 流结束：放出暂扣片段，并闭合仍开着的思考块
            r_tail, c_tail = splitter.flush()
            for piece in _render(r_tail, c_tail):
                emitted += len(piece)
                yield piece
            if emitted_think_open:
                piece = "\n" + THINK_CLOSE_TAG + "\n"
                emitted += len(piece)
                yield piece
            timer.done(chars=emitted)


        except Exception as e:
            timer.done(chars=emitted, note=f"failed: {type(e).__name__}")
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
            if OpenAICompatibleLLM._is_content_inspection_error(e):
                return ("\n\n[Content Filter Error]\nThe provider rejected the input during "
                        "content inspection. The app retried once with the retrieved context "
                        "removed; if this still fails, shorten the conversation or switch "
                        "provider.\n", "Content inspection rejected input.")
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
        # Agent 循环的每一轮都走这里：耗时日志直接反映"第几轮慢、慢在首字还是生成"
        timer = _CallTimer("stream_events", self.logger, self.model_name,
                           is_translation=is_translation)

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
            response = self._completion_with_adaptation(litellm_kwargs, payload)

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
                content = getattr(delta, 'content', None)
                if reasoning or content or getattr(delta, "tool_calls", None):
                    # 首个模型输出（正文/思考/工具调用分片都算）：TTFB 到此为止
                    timer.first_token()
                if reasoning:
                    reasoning_parts.append(reasoning)
                    yield {"type": "reasoning", "text": reasoning}

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
            timer.done(chars=len("".join(content_parts)) + len("".join(reasoning_parts)),
                       note=f"failed: {type(e).__name__}")
            text, label = self._stream_error_map(e)
            if text is not None:
                self.logger.error(f"{label}\n{_raw_error_text(e)}")
                content_parts.append(text)
            elif self._is_cancelled or "closed" in str(e).lower() or "cancel" in str(e).lower():
                content_parts.append("\n\n[⛔ Generation halted by user.]")
            else:
                self.logger.error(f"Unexpected system error.\n{_raw_error_text(e)}", exc_info=True)
                content_parts.append(f"\n\n[System Error: {type(e).__name__}: {e}]\n")

        final = _build_final()
        timer.done(usage=final.get("_usage"),
                   chars=len(final["content"]) + len(final["reasoning_content"]),
                   note=(f"tool_calls={len(final['tool_calls'])}"
                         if final.get("tool_calls") else ""))
        yield {"type": "final", "response": final}


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