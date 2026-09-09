"""Token estimation utilities for context budgeting and usage display.

优先使用 tiktoken（若已安装）做精确估算；未安装或编码器不可用时，按字符
类别加权启发式估算：
- CJK（中日韩）字符约 1 token/字（cl100k 对常见汉字 0.6~1.2）；
- 其他字符约 0.25 token/字符（英文约 4 chars/token）。

估算仅用于上下文预算控制与 UI 用量展示，不作为计费依据；提供商返回的
真实 usage 永远优先于本模块的估算值。
"""
import logging

logger = logging.getLogger(__name__)

try:
    import tiktoken
    _ENCODER = None
    try:
        _ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception as e:  # 离线环境下 get_encoding 可能因下载失败抛错
        logger.warning(f"tiktoken cl100k_base unavailable, fallback to heuristic: {e}")
        _ENCODER = None
except ImportError:
    _ENCODER = None

#: 主要 CJK Unicode 区段（含日文假名、韩文谚文与 CJK 标点）。
_CJK_RANGES = (
    (0x4E00, 0x9FFF),    # CJK 统一表意文字
    (0x3400, 0x4DBF),    # 扩展 A
    (0x3040, 0x30FF),    # 日文假名
    (0xAC00, 0xD7AF),    # 韩文谚文
    (0x3000, 0x303F),    # CJK 符号与标点
)


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(text) -> int:
    """估算文本的 token 数。

    tiktoken 可用时误差约 ±5%（cl100k_base 对非 OpenAI 模型仍是近似）；
    否则按 CJK/拉丁字符加权估算。空文本返回 0。
    """
    if not text:
        return 0
    text = str(text)
    if _ENCODER is not None:
        try:
            # disallowed_special：正文含 "<|...|>" 等特殊标记时不抛错
            return len(_ENCODER.encode(text, disallowed_special=()))
        except Exception:
            pass
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return cjk + max(1, other // 4)


def estimate_message_tokens(messages) -> int:
    """估算 OpenAI 格式消息列表的总 token（含每条约 4 token 的协议开销）。

    支持 content 为字符串或 OpenAI 多模态 list（text 段按文本估算，
    image_url 段按常见视觉编码约 1100 token 计）；assistant 消息中的
    reasoning_content 与 tool_calls 一并计入。
    """
    if not messages:
        return 0
    total = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        total += 4  # role / 结构开销
        content = m.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        total += estimate_tokens(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        total += 1100
        reasoning = m.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            total += estimate_tokens(reasoning)
        tool_calls = m.get("tool_calls")
        if tool_calls:
            import json as _json
            try:
                total += estimate_tokens(_json.dumps(tool_calls, ensure_ascii=False))
            except (TypeError, ValueError):
                total += estimate_tokens(str(tool_calls))
    return total


# ---------------------------------------------------------------------- #
#  模型上下文窗口解析与治理预算推导
# ---------------------------------------------------------------------- #

#: 常见模型家族的输入上下文窗口（token）。litellm.model_cost 未收录或
#: 数据滞后时兜底；与 litellm 解析结果取较大值。命中规则为子串匹配
#: （按表序，先长后短，如 qwen-long 先于 qwen）。
_BUILTIN_CONTEXT_HINTS = [
    ("qwen-long", 10_000_000),
    ("gpt-5", 1_000_000), ("gpt-4.1", 1_000_000),
    ("gpt-4o", 128_000), ("o1", 200_000), ("o3", 200_000), ("o4", 200_000),
    ("gpt-4", 128_000), ("gpt-3.5", 16_384),
    ("claude", 200_000), ("gemini", 1_000_000), ("deepseek", 1_048_576),
    ("qwen", 131_072), ("glm-4.6", 200_000), ("glm", 128_000),
    ("kimi", 256_000), ("moonshot", 131_072), ("minimax", 1_000_000),
    ("doubao", 256_000), ("hunyuan", 256_000), ("grok", 131_072),
    ("llama", 128_000), ("mistral", 128_000),
]

#: 无法识别模型时的保守默认（本地小模型常见量级）。
_DEFAULT_CONTEXT_WINDOW = 32_768


def resolve_context_window(model_name) -> int:
    """解析模型的输入上下文窗口（token）。

    解析顺序（取所有来源的最大值，防任一来源数据滞后）：
    1. ``litellm.model_cost``（litellm 自带的模型元数据库，含
       max_input_tokens / max_tokens，按精确名匹配；带 provider 前缀时
       同时尝试去掉前缀的名字）；
    2. 内置家族提示表（子串匹配）；
    均未命中返回 ``_DEFAULT_CONTEXT_WINDOW``。
    """
    name = (model_name or "").strip().lower()
    if not name:
        return _DEFAULT_CONTEXT_WINDOW
    candidates = {name}
    if "/" in name:
        candidates.add(name.rsplit("/", 1)[-1])

    best = 0
    try:
        import litellm
        for cand in candidates:
            info = litellm.model_cost.get(cand)
            if info:
                v = info.get("max_input_tokens") or info.get("max_tokens") or 0
                best = max(best, int(v or 0))
    except Exception:
        pass

    for key, window in _BUILTIN_CONTEXT_HINTS:
        for cand in candidates:
            if key in cand:
                best = max(best, window)
                break

    return best or _DEFAULT_CONTEXT_WINDOW


def derive_context_budgets(context_window: int) -> dict:
    """按模型上下文窗口推导上下文治理预算（相对比例 + 合理 clamp）。

    - tool_result_chars：单条工具结果字符上限（≈窗口的 1.6%，按 4
      chars/token 即 0.4% 窗口）；
    - tool_token_budget：单轮内全部 role=tool 消息的 token 预算（≈10%）；
    - kb_token_budget：KB 检索注入 system prompt 的 token 预算（≈2.4%）。

    小窗口模型自动收紧（如 8K 窗口 → 工具 2K / KB 2K），大窗口模型放宽
    （1M → 工具 100K / KB 24K / 单条 16K 字符），保证各模型符合自身容量。
    """
    return {
        "tool_result_chars": max(4_000, min(int(context_window * 0.016), 16_000)),
        "tool_token_budget": max(2_000, min(int(context_window * 0.10), 128_000)),
        "kb_token_budget": max(2_000, min(int(context_window * 0.024), 48_000)),
    }
