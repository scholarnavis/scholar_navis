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
import json
import logging
import os
import threading
import time
from functools import lru_cache
from typing import Any, Dict

# LiteLLM 导入期会尝试拉远程 cost map（无超时/网络差时卡 10s+）。模型窗口
# 数据必须离线可用，默认强制走随包携带的本地 backup；最新表由后台刷新合并。
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

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

# 不再维护手写模型窗口表：所有模型信息以 LiteLLM 的 model map 为准，
# 未收录时由调用方显示 Unknown，只有内部预算保留一个安全默认值。

#: 无法识别模型时，**远程**端点使用的默认窗口（token）。
#:
#: 远程未知模型按 1M 兜底：2026 年主流已普遍 ≥128K，1M 已是多家旗舰的默认。
#: "未知 = 小窗口"会让自建网关/新模型被白白闲置。猜高的代价由运行时兜底：真超限时
#: AgentRuntime 会压缩最旧历史重试一次，并把 provider 在报错里给出的真值写入能力表
#: （见 src/core/model_capabilities.py），因此最坏情况只是多一次往返，之后永久正确。
_DEFAULT_CONTEXT_WINDOW = 1_048_576

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


def _litellm_context_window(candidates, resolve_aliases: bool = True) -> int:
    """优先用 LiteLLM 的模型元数据解析输入窗口。

    ``get_model_info`` 会处理 provider 前缀/别名，比自己翻 ``model_cost`` 准；
    只取 ``max_input_tokens``，绝不把 ``max_tokens``（输出上限）当输入窗口。
    """
    try:
        import litellm
    except Exception:
        return 0
    # 先查直接表：已知模型不触发 get_model_info 的 provider 解析/告警。
    for cand in candidates:
        info = litellm.model_cost.get(cand)
        if not isinstance(info, dict):
            continue
        try:
            value = int(info.get("max_input_tokens") or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    if not resolve_aliases:
        return 0
    # 再让 get_model_info 处理 provider 前缀、别名和带日期的模型名。
    for cand in candidates:
        try:
            info = litellm.get_model_info(cand)
        except Exception:
            info = None
        if isinstance(info, dict) or hasattr(info, "get"):
            try:
                value = int(info.get("max_input_tokens") or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
    return 0


_LITELLM_MODEL_MAP_URLS = (
    # 官方源：非中国大陆通常最快。
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json",
    # jsDelivr 的 GitHub CDN：大陆网络通常比 raw.githubusercontent 更容易连通。
    "https://gcore.jsdelivr.net/gh/BerriAI/litellm@main/"
    "model_prices_and_context_window.json",
    "https://testingcf.jsdelivr.net/gh/BerriAI/litellm@main/"
    "model_prices_and_context_window.json",
    "https://cdn.jsdelivr.net/gh/BerriAI/litellm@main/"
    "model_prices_and_context_window.json",
)
_LITELLM_MODEL_MAP_CACHE = os.path.join(
    BASE_DIR, "scholar_workspace", "litellm_model_cost.json")
_MODEL_MAP_LOADED = False
_MODEL_MAP_LOCK = threading.Lock()


def _seed_model_map_path() -> str:
    """PyInstaller 打包时随包携带的 LiteLLM 元数据种子。

    冻结产物里可写数据在 exe 同级目录，而 --add-data 落在 ``_MEIPASS``；
    两者不是同一个 scholar_workspace，所以单独查资源根目录。
    """
    try:
        from src.core.platform_env import app_resource_root
        path = os.path.join(app_resource_root(), "scholar_workspace",
                            "litellm_model_cost.json")
        if os.path.abspath(path) == os.path.abspath(_LITELLM_MODEL_MAP_CACHE):
            return ""
        return path
    except Exception:
        return ""


def _load_cached_litellm_model_map() -> bool:
    """加载上次刷新的 LiteLLM 元数据缓存（7 天内有效），只做一次。

    返回是否真的加载到有效数据；缓存过期/损坏返回 False，交给启动刷新。
    """
    global _MODEL_MAP_LOADED
    if _MODEL_MAP_LOADED:
        return True
    with _MODEL_MAP_LOCK:
        if _MODEL_MAP_LOADED:
            return True
        _MODEL_MAP_LOADED = True
        seed = _seed_model_map_path()
        # 先用户缓存（7 天），再打包种子（保底，不限制年龄；过期后由刷新覆盖）。
        for path, max_age in ((_LITELLM_MODEL_MAP_CACHE, 7 * 86400),
                              (seed, None)):
            if not path:
                continue
            try:
                if max_age is not None and time.time() - os.path.getmtime(path) > max_age:
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict) or not data:
                    continue
                import litellm
                litellm.model_cost.update(data)
                logger.info("Loaded LiteLLM model map from %s: %d entries",
                            "bundled seed" if path == seed else "cache", len(data))
                return True
            except (OSError, ValueError):
                continue
    return False


def ensure_litellm_model_map_fresh(max_age_days: int = 7) -> int:
    """启动时调用：7 天缓存内直接加载；过期/缺失则拉官方表并更新。

    返回值：本次实际刷新的条目数；缓存新鲜或刷新失败返回 0。
    """
    try:
        age = time.time() - os.path.getmtime(_LITELLM_MODEL_MAP_CACHE)
        if age < max_age_days * 86400 and _load_cached_litellm_model_map():
            logger.info("LiteLLM model map cache is fresh (%.1f days); skip refresh.",
                        age / 86400)
            return 0
        logger.info("LiteLLM model map cache is stale/invalid (%.1f days); refreshing...",
                    age / 86400)
    except OSError:
        logger.info("LiteLLM model map cache missing; refreshing...")
    return refresh_litellm_model_map()


def fetch_litellm_model_map(timeout: int = 20) -> Dict[str, Any]:
    """从官方源/大陆可用 CDN 拉取 model map；返回带输入窗口的条目。

    ``timeout`` 是**整次回退链的总预算**，不是每个 URL 给一份：官方源
    超时会消耗预算，后面的 jsDelivr 只能用剩余时间，避免 4 个源串行拖到
    一分钟。单个源再限制为最多 8 秒，防止一个半开连接吃光全部预算。
    """
    try:
        from src.core.network_worker import create_robust_session
        session = create_robust_session()
    except Exception as e:
        logger.warning("LiteLLM model map session failed: %s", e)
        return {}

    total_deadline = time.time() + max(5, int(timeout))
    per_source = max(4, min(8, int(timeout) // 2))
    for url in _LITELLM_MODEL_MAP_URLS:
        remaining = total_deadline - time.time()
        if remaining <= 0:
            break
        try:
            res = session.get(url, timeout=min(per_source, remaining))
            if res.status_code != 200:
                continue
            data = res.json()
            if not isinstance(data, dict) or not data:
                continue
            useful = {k: v for k, v in data.items()
                      if isinstance(v, dict) and v.get("max_input_tokens")}
            if useful:
                logger.info("Fetched LiteLLM model map from %s: %d entries",
                            url.split("/")[2], len(useful))
                return useful
        except Exception as e:
            logger.debug("LiteLLM model map source failed (%s): %s",
                         url.split("/")[2], e)
    return {}


def refresh_litellm_model_map(timeout: int = 20) -> int:
    """拉取 LiteLLM 官方 model_prices_and_context_window.json 并合并。

    本地 LiteLLM 备份可能滞后（例如不包含 GPT-6 的 provider 变体）；这个
    函数用应用自己的网络栈拉最新表，成功后写缓存并清空静态窗口缓存。
    ``timeout`` 为整次回退链的总预算（默认 20 秒）。返回合并的条目数（失败 0）。
    """
    useful = fetch_litellm_model_map(timeout=timeout)
    if not useful:
        logger.warning("Refresh LiteLLM model map failed: no source reachable.")
        return 0
    try:
        import litellm
        litellm.model_cost.update(useful)
        try:
            os.makedirs(os.path.dirname(_LITELLM_MODEL_MAP_CACHE), exist_ok=True)
            tmp = _LITELLM_MODEL_MAP_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(useful, f)
            os.replace(tmp, _LITELLM_MODEL_MAP_CACHE)
        except OSError as e:
            logger.warning("Cache LiteLLM model map failed: %s", e)
        _static_context_window.cache_clear()
        logger.info("Refreshed LiteLLM model map: %d entries", len(useful))
        return len(useful)
    except Exception as e:
        logger.warning("Refresh LiteLLM model map failed: %s", e)
        return 0


@lru_cache(maxsize=1024)
def _static_context_window(model_name, resolve_aliases: bool = True) -> int:
    """只用 LiteLLM 解析输入窗口；不维护手写模型表。

    先查直接 model_cost，再按 provider 变体后缀匹配，最后让 get_model_info
    处理别名。仍未知返回 **0**（UI 显示 Unknown）；内部预算的安全默认值由
    :func:`src.core.model_capabilities.resolve_window` 单独处理。
    """
    name = (model_name or "").strip().lower()
    if not name:
        return 0
    _load_cached_litellm_model_map()
    candidates = [name]
    if "/" in name:
        stripped = name.rsplit("/", 1)[-1]
        if stripped and stripped not in candidates:
            candidates.append(stripped)

    litellm_value = _litellm_context_window(candidates, resolve_aliases=False)
    if litellm_value > 0:
        return litellm_value

    # 官方 LiteLLM 表里新模型常以 provider 变体存在（azure/gpt-6-sol、
    # dashscope/qwen3-max）。按最后一段做前缀匹配，取同族最大输入窗口。
    try:
        import litellm
        best = 0
        for key, info in litellm.model_cost.items():
            if not isinstance(info, dict):
                continue
            last = str(key).rsplit("/", 1)[-1].lower()
            if not any(last == c or last.startswith(c + "-")
                       or last.startswith(c + ".") for c in candidates):
                continue
            try:
                value = int(info.get("max_input_tokens") or 0)
            except (TypeError, ValueError):
                value = 0
            best = max(best, value)
        if best > 0:
            return best
    except Exception:
        pass

    if not resolve_aliases:
        return 0
    # 最后让 get_model_info 处理 provider 前缀、别名和日期后缀；仍未知返回 0。
    return _litellm_context_window(candidates, resolve_aliases=True)


def litellm_context_window(model_name: str, resolve_aliases: bool = False) -> int:
    """只查 LiteLLM 的输入窗口；未知返回 0（不猜、不兜底）。

    供 UI 展示与“严格按 LiteLLM”场景使用。UI 默认 ``resolve_aliases=False``：
    直接表/ provider 变体已经覆盖绝大多数模型，避免对每个未知自定义模型触发
    LiteLLM 的 provider 解析告警。内部预算仍走 :func:`resolve_window`
    （保留用户声明/provider 实测/安全默认）。
    """
    return _static_context_window(model_name, resolve_aliases)


def derive_context_budgets(context_window: int) -> dict:
    """按模型上下文窗口推导上下文治理预算（比例主导，上下限随窗口缩放）。

    这些值是**每轮都要重发的 prompt 体积**，直接决定 token 成本与首字延迟。
    旧比例把窗口当成"必须填满的容器"（工具 26% + KB 10% + 历史 50% = 86%），
    1M 窗口下拉满接近 90 万 token/轮：既昂贵，又必然撞上 provider 的硬限后
    被动降级。这里改成"按需给足、不追求填满"，三项合计控制在窗口的 20% 左右，
    余下的容量留给固定系统提示、输出长度与 provider 侧的计费差异。

    ===================  ==========  ==========  ==============
    预算项               占窗口比例  窗口硬上限  1M 窗口实测值
    ===================  ==========  ==========  ==============
    tool_result_chars      4%         24,000 字符   24,000 字符
    tool_token_budget     12%         48,000 token  48,000 token
    kb_token_budget        4%         16,000 token  16,000 token
    kb_chunk_chars       0.8%         50,000 字符    8,000 字符
    history_token_budget  32%        144,000 token 144,000 token
    ===================  ==========  ==========  ==============

    各项都是**上界**而非预定量，实际占用取决于内容多少；小窗口仍有安全下限，
    避免把单条工具结果切得读不出结论。三项合计在 32K 窗口约占 51%、128K 与
    200K 约 48%、1M 约 21%——余量留给固定系统提示与输出长度。

    ``kb_token_budget`` 已从 8% 降到 4%：Library Manager 从主界面下线后，本地
    知识库检索实际不再产出上下文，原先属于它的份额转给历史。该预算现在只服务
    ``### CONTEXT:`` 段里的附件正文；若将来恢复知识库入口，把这两个比例改回来
    即可（这里是唯一处定义）。
    """
    window = max(int(context_window or 0), 1_000)
    return {
        "tool_result_chars": _clamp(round(window * 0.04), 8_000, 24_000),
        "tool_token_budget": _clamp(round(window * 0.12), 4_000, 48_000),
        "kb_token_budget": _clamp(round(window * 0.04), 2_000, 16_000),
        "kb_chunk_chars": _clamp(round(window * 0.008), 2_000, round(window * 0.05)),
        "history_token_budget": _clamp(round(window * 0.32), 6_000, 144_000),
    }


def _clamp(value: int, low: int, high: int) -> int:
    """把 ``value`` 夹到 ``[low, max(low, high)]``（下限优先，避免比例上限倒挂）。"""
    return max(low, min(value, max(low, high)))
