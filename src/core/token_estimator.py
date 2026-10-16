"""Token estimation utilities for context budgeting and usage display.

优先使用 tiktoken（若已安装）做精确估算；未安装、编码器尚未就绪或不可用时，
按字符类别加权启发式估算：
- CJK（中日韩）字符约 1 token/字（cl100k 对常见汉字 0.6~1.2）；
- 其他字符约 0.25 token/字符（英文约 4 chars/token）。

估算仅用于上下文预算控制与 UI 用量展示，不作为计费依据；提供商返回的
真实 usage 永远优先于本模块的估算值。

启动性能（重要）
----------------
``tiktoken.get_encoding("cl100k_base")`` 需要一份约 1.7 MB 的 BPE 词表，由
tiktoken 按需从 ``openaipublic.blob.core.windows.net`` 下载，其内部实现是不带
超时的 ``requests.get``。早期实现把这次加载放在模块导入期，而本模块又位于主窗口
导入链上（main_window → chat_tool → chat_tasks → 本模块），于是**每次启动都要
联网下载**：tiktoken 默认缓存目录取 ``tempfile.gettempdir()``，在打包/沙箱环境
（NixOS steam-run 的 ``--tmpfs /tmp``、容器、被清理的 Windows 临时目录）中每次都
是空的。网络一旦抖动，启动就会被这一段拖到分钟级，而日志上看不出任何线索。

现在改为两点：
1. 缓存目录固定到应用目录 ``<BASE_DIR>/.cache/tiktoken``（可用
   ``SCHOLAR_NAVIS_TIKTOKEN_CACHE`` 覆盖），词表只需下载一次；
2. 编码器在**后台线程**预热（模块导入时自动触发一次，见 :func:`warm_up_async`），
   估算函数在就绪前直接走启发式，导入期与首次调用都不会等待网络。

置 ``SCHOLAR_NAVIS_DISABLE_TIKTOKEN=1`` 可完全关闭 tiktoken（只用启发式）。
"""
import logging
import os
import threading
from functools import lru_cache

from src.core import BASE_DIR

logger = logging.getLogger(__name__)

#: tiktoken 自身的缓存目录环境变量（其 ``load.read_file_cached`` 每次调用都读）。
_CACHE_DIR_ENV = "TIKTOKEN_CACHE_DIR"
#: 覆盖缓存目录（打包/容器场景可指向其它可写位置）。
_CACHE_DIR_OVERRIDE_ENV = "SCHOLAR_NAVIS_TIKTOKEN_CACHE"
#: 置 1/true/yes/on 时禁用 tiktoken，仅使用字符启发式估算。
_DISABLE_ENV = "SCHOLAR_NAVIS_DISABLE_TIKTOKEN"
#: 编码器名：cl100k_base 是主流模型（含非 OpenAI 模型）最接近的通用词表。
_ENCODING_NAME = "cl100k_base"
_TRUTHY = {"1", "true", "yes", "on"}

#: 编码器句柄与状态（仅在 _load_encoder 中写入，读方无锁依赖布尔可见性）。
_encoder = None
_encoder_ready = False
_encoder_unavailable = False
_warmup_lock = threading.Lock()
_warmup_started = False


def tiktoken_cache_dir() -> str:
    """tiktoken 词表缓存目录（不存在时创建）。

    必须是**持久**目录，原因见模块文档「启动性能」一节。
    """
    override = os.environ.get(_CACHE_DIR_OVERRIDE_ENV, "").strip()
    cache_dir = (os.path.expanduser(override) if override
                 else os.path.join(BASE_DIR, ".cache", "tiktoken"))
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError as e:
        logger.warning(f"Cannot create tiktoken cache dir '{cache_dir}': {e}")
    return cache_dir


def tiktoken_disabled() -> bool:
    """是否通过环境变量完全禁用 tiktoken。"""
    return os.environ.get(_DISABLE_ENV, "").strip().lower() in _TRUTHY


def _load_encoder() -> None:
    """（后台线程）加载编码器；任何失败只记录日志，估算自动退回启发式。"""
    global _encoder, _encoder_ready, _encoder_unavailable
    try:
        # 必须在 get_encoding 之前设置：它决定词表落盘位置，指向持久目录才能
        # 避免每次启动重新下载。用户显式设置过则不覆盖。
        os.environ.setdefault(_CACHE_DIR_ENV, tiktoken_cache_dir())
        import tiktoken

        encoder = tiktoken.get_encoding(_ENCODING_NAME)
    except ImportError:
        _encoder_unavailable = True
        logger.info("tiktoken not installed; token estimation uses heuristic fallback.")
        return
    except Exception as e:  # 下载失败/离线/缓存损坏等
        _encoder_unavailable = True
        logger.warning(f"tiktoken '{_ENCODING_NAME}' unavailable, fallback to heuristic: {e}")
        return

    _encoder = encoder
    _encoder_ready = True
    logger.info(
        f"tiktoken '{_ENCODING_NAME}' ready (cache: {os.environ.get(_CACHE_DIR_ENV, '')}).")


def warm_up_async() -> bool:
    """后台预热编码器（幂等、非阻塞），返回本次是否真正发起了线程。

    启动路径调用它即可让词表下载与模型加载并行进行；未就绪期间的估算由
    :func:`estimate_tokens` 走启发式兜底，因此**任何调用方都不会等待网络**。
    """
    global _warmup_started
    if tiktoken_disabled() or _encoder_ready or _encoder_unavailable:
        return False
    with _warmup_lock:
        if _warmup_started:
            return False
        _warmup_started = True
    threading.Thread(target=_load_encoder, name="tiktoken-warmup", daemon=True).start()
    return True


def _get_encoder():
    """返回已就绪的编码器；未就绪（正在预热/不可用）时返回 None。"""
    return _encoder if _encoder_ready else None


# 模块导入即后台预热：本模块处于主窗口导入链上，导入期不得有任何网络等待。
warm_up_async()

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

    tiktoken 就绪时误差约 ±5%（cl100k_base 对非 OpenAI 模型仍是近似）；
    否则按 CJK/拉丁字符加权估算。空文本返回 0。
    """
    if not text:
        return 0
    text = str(text)
    encoder = _get_encoder()
    if encoder is not None:
        try:
            # disallowed_special：正文含 "<|...|>" 等特殊标记时不抛错
            return len(encoder.encode(text, disallowed_special=()))
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

#: 无法识别模型时，**远程**端点使用的默认窗口（token）。
#:
#: 取 300K 而不是早年那种 32K：主流商用模型如今普遍 ≥128K，1M 已不稀奇，"未知 =
#: 小窗口"会让自建网关/新发布的模型被白白闲置。猜高的代价由运行时兜底：真超限时
#: AgentRuntime 会压缩最旧历史重试一次，并把 provider 在报错里给出的真值写入能力表
#: （见 src/core/model_capabilities.py），因此最坏情况只是多一次往返，之后永久正确。
_DEFAULT_CONTEXT_WINDOW = 300_000

#: 本地推理端点（LM Studio / Ollama / llama.cpp 等）的默认窗口（token）。
#:
#: 与本机服务通信时，窗口取决于本地部署与量化设置——既探测不到，也几乎不会是大窗口，
#: 常见量级就是 8K~32K。此处保持保守，避免每次和本地模型对话都先撞一次上下文超限。
_LOCAL_CONTEXT_WINDOW = 32_768


def resolve_context_window(model_name, base_url=None, explicit=None) -> int:
    """解析模型的输入上下文窗口（token）。

    完整优先级与理由见 :mod:`src.core.model_capabilities`（唯一合成点）：
    用户声明 → 运行时实测 → 静态推断（本模块） → 默认值。
    """
    from src.core.model_capabilities import resolve_window

    return resolve_window(model_name, base_url=base_url, explicit=explicit)[0]


@lru_cache(maxsize=512)
def _static_context_window(model_name) -> int:
    """静态推断窗口（``litellm.model_cost`` 与内置家族表取较大值）。

    未命中任何来源时返回 **0**（不是默认值）——调用方据此区分"识别出来了"与
    "完全没线索"，默认值由 :func:`src.core.model_capabilities.resolve_window`
    统一兜底。结果按模型名缓存：litellm 元数据在进程内不会变，而本函数在
    每轮预算推导里都会被调用。
    """
    name = (model_name or "").strip().lower()
    if not name:
        return 0
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

    return best


def derive_context_budgets(context_window: int) -> dict:
    """按模型上下文窗口推导上下文治理预算（比例主导，上下限随窗口缩放）。

    1M 级窗口已是常态，"够用但保守"的比例会把容量白白闲置——那正是"窗口 1M、
    程序只喂一点点"的成因。因此比例按"把窗口用起来"来定：

    ===================  ==========  ==========  ==============
    预算项               占窗口比例  窗口硬上限  1M 窗口实测值
    ===================  ==========  ==========  ==============
    tool_result_chars      3%          8%         31,457 字符
    tool_token_budget     20%         35%        209,715 token
    kb_token_budget        8%         15%         83,886 token
    kb_chunk_chars       0.5%          4%          5,243 字符
    history_token_budget  45%         60%        471,859 token
    ===================  ==========  ==========  ==============

    各项都是**上界**而非预定量，实际占用取决于内容多少；"硬上限"只在窗口大到
    离谱（>2M）时才生效，避免单项吞掉整张上下文。小窗口仍有安全下限。

    代价要说清楚：历史/工具预算越大，Agent 每轮重发的 prompt 越大——首字延迟与
    token 成本随之上升。1M 窗口 + 全部拉满是"用满容量"，不是"必然更快"。若要压
    成本，改这里的比例即可（唯一处定义）。
    """
    window = max(int(context_window or 0), 1_000)
    return {
        "tool_result_chars": _clamp(round(window * 0.03), 4_000, round(window * 0.08)),
        "tool_token_budget": _clamp(round(window * 0.20), 2_000, round(window * 0.35)),
        "kb_token_budget": _clamp(round(window * 0.08), 2_000, round(window * 0.15)),
        "kb_chunk_chars": _clamp(round(window * 0.005), 1_600, round(window * 0.04)),
        "history_token_budget": _clamp(round(window * 0.45), 4_000, round(window * 0.60)),
    }


def _clamp(value: int, low: int, high: int) -> int:
    """把 ``value`` 夹到 ``[low, max(low, high)]``（下限优先，避免比例上限倒挂）。"""
    return max(low, min(value, max(low, high)))
