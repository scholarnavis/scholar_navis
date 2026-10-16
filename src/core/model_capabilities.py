"""模型能力表：远程模型"上下文窗口 / 输出上限 / 不被接受的请求参数"的单一事实来源。

为什么必须有这张表
------------------
这几项能力 **OpenAI 兼容接口不提供**：``{base_url}/models`` 通常只回 ``id``
（个别网关会额外带 ``context_length``，但不可依赖）；是否支持某个请求参数更是
没有查询入口，只有"发过去被 400 拒绝"才知道。所以能力信息只能来自三处，
且必须按"谁更权威"排序——本模块是唯一的合成点：

1. **用户声明** —— ``llm_config.json`` 里 provider 的 ``context_window`` /
   ``max_output_tokens``（设置页可填）。最权威：用户知道自己接的是哪个模型。
2. **运行时实测** —— 本表落盘内容。provider 报错时才会吐露真值，例如
   ``"This model's maximum context length is 131072 tokens"`` 或
   ``"max_tokens is too large"``。这类信息**只用于收紧或纠正**，不会凭空放大。
3. **静态推断** —— ``litellm.model_cost`` + 内置家族表（见
   :mod:`src.core.token_estimator`）。离线可用、零请求，但可能滞后，也识别不出
   自建网关的自定义模型名（此时只能落到兜底默认：远程端点 300K、本地推理端点 32K）。

调用方（预算推导、请求参数下发、设置页展示）**一律只查本模块**，不要再各自
判断，否则同一件事会出现多处口径。

落盘格式
--------
``config/model_capabilities.json``，键为 ``"<规范化 base_url>|<model>"``::

    {
      "https://api.example.com/v1|my-model": {
        "context_window": 131072,
        "max_output_tokens": 65536,
        "unsupported_params": ["top_p"],
        "source": "provider-error",
        "updated_at": "2026-10-15T12:00:00"
      }
    }

文件可手工编辑（等同于"用户声明"）。损坏时退回空表并告警，绝不因为一个坏文件
让对话起不来；写入用"临时文件 + 原子替换"并加进程内锁（GUI 与 API server 同进程，
多线程写同一文件必须串行）。
"""

import json
import logging
import os
import threading
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Set, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: 能力表落盘位置（与其它 config/*.json 同级）。
CAPABILITY_FILE = os.path.join(os.getcwd(), "config", "model_capabilities.json")

#: 已知可被 provider 拒绝的请求参数（用于从 400 报错里定位"哪个参数不支持"）。
#: 只用于"报错后识别"，不做任何主动猜测——列表外的参数一律不处理。
KNOWN_REQUEST_PARAMS = (
    "temperature", "top_p", "top_k", "presence_penalty", "frequency_penalty",
    "logprobs", "top_logprobs", "seed", "stop", "n", "response_format",
    "tool_choice", "tools", "parallel_tool_calls", "reasoning_effort",
    "thinking", "enable_thinking", "max_tokens", "max_completion_tokens",
    "modalities", "logit_bias", "user",
)

_lock = threading.RLock()
#: 落盘内容缓存：``(mtime, data)``。命中 mtime 直接复用，避免每次预算推导都读盘。
_cache: Tuple[float, Dict[str, Dict[str, Any]]] = (0.0, {})


# ---------------------------------------------------------------------- #
#  键与读写
# ---------------------------------------------------------------------- #
def normalize_base_url(base_url: str) -> str:
    """归一化 base_url：去空白、去尾斜杠、小写，保证同一网关只有一个键。"""
    return str(base_url or "").strip().rstrip("/").lower()


def capability_key(base_url: str, model_name: str) -> str:
    """能力表键：``"<规范化 base_url>|<model>"``（唯一处定义）。"""
    return f"{normalize_base_url(base_url)}|{str(model_name or '').strip().lower()}"


#: 回环地址：一定是本机推理服务。
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]", "host.docker.internal")


def is_local_endpoint(base_url: str) -> bool:
    """base_url 是否指向本机/内网自建的推理服务（唯一判定处）。

    判定为"本地"的两种情况：回环地址（localhost、127.0.0.1、::1、0.0.0.0、
    host.docker.internal），或主机名不含点号（``ollama``、``lmstudio`` 这类容器/
    内网服务名）。用途只有一个：兜底窗口取保守值——本地窗口取决于部署与量化，
    既探测不到也通常不大（见 ``token_estimator._LOCAL_CONTEXT_WINDOW``）。
    """
    url = str(base_url or "").strip()
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    if not host:
        # 缺 scheme 的写法（"127.0.0.1:1234/v1"）在这里补一次解析
        host = url.split("/")[0].split(":")[0].lower()
    return host in _LOOPBACK_HOSTS or ("." not in host and bool(host))


def _load() -> Dict[str, Dict[str, Any]]:
    """读取能力表（带 mtime 缓存）；文件缺失/损坏时返回空表。"""
    global _cache
    path = CAPABILITY_FILE
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    with _lock:
        cached_mtime, cached = _cache
        if mtime == cached_mtime:
            return cached
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Model capability file unreadable, ignoring it: {e}")
            _cache = (mtime, {})
            return {}
        if not isinstance(data, dict):
            logger.warning("Model capability file has unexpected structure, ignoring it.")
            data = {}
        _cache = (mtime, {k: v for k, v in data.items() if isinstance(v, dict)})
        return _cache[1]


def _write(data: Dict[str, Dict[str, Any]]) -> None:
    """原子写盘（临时文件 + replace），失败只告警——能力表丢了不影响对话。"""
    global _cache
    path = CAPABILITY_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
        _cache = (os.path.getmtime(path), data)
    except OSError as e:
        logger.warning(f"Failed to persist model capabilities: {e}")


def _update(base_url: str, model_name: str, fields: Dict[str, Any],
            source: str, only_lower: Iterable[str] = ()) -> bool:
    """写入/合并一条能力记录。

    ``only_lower`` 里的键只会被"更小/更严"的值覆盖——实测得到的上限（provider
    报错给出的真值）只允许收紧，绝不允许把之前记录的更小上限顶回去。
    返回是否发生了实际变化。
    """
    if not model_name:
        return False
    with _lock:
        data = dict(_load())
        key = capability_key(base_url, model_name)
        entry = dict(data.get(key) or {})
        changed = False
        for name, value in fields.items():
            if value in (None, "", []):
                continue
            if name in only_lower:
                old = entry.get(name)
                if isinstance(old, int) and old <= int(value):
                    continue
            if entry.get(name) != value:
                entry[name] = value
                changed = True
        if not changed:
            return False
        entry["source"] = source
        entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
        data[key] = entry
        _write(data)
        return True


def get(base_url: str, model_name: str) -> Dict[str, Any]:
    """取某 (网关, 模型) 的能力记录（不存在返回空 dict，调用方判空即可）。"""
    return dict(_load().get(capability_key(base_url, model_name)) or {})


def snapshot() -> Dict[str, Dict[str, Any]]:
    """全量快照（供日志/设置页展示）。"""
    return dict(_load())


def clear(base_url: str = "", model_name: str = "") -> bool:
    """清除一条能力记录；都不传则清空整表。返回是否有改动。"""
    with _lock:
        data = dict(_load())
        if not base_url and not model_name:
            if not data:
                return False
            _write({})
            return True
        key = capability_key(base_url, model_name)
        if key not in data:
            return False
        data.pop(key)
        _write(data)
        return True


# ---------------------------------------------------------------------- #
#  记录实测结果
# ---------------------------------------------------------------------- #
def record_unsupported_param(base_url: str, model_name: str, param: str) -> bool:
    """记录"该模型不接受某请求参数"（provider 用 400 明确拒绝之后）。"""
    param = str(param or "").strip()
    if not param:
        return False
    with _lock:
        current = list(get(base_url, model_name).get("unsupported_params") or [])
        if param in current:
            return False
        current.append(param)
    changed = _update(base_url, model_name, {"unsupported_params": current},
                      source="provider-error")
    if changed:
        logger.info(f"Model '{model_name}': parameter '{param}' is not accepted; "
                    f"it will be omitted from subsequent requests.")
    return changed


def record_max_output_tokens(base_url: str, model_name: str, value: int) -> bool:
    """记录实测的输出上限（provider 报"max_tokens 太大"时给出的真值）。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    return _update(base_url, model_name, {"max_output_tokens": value},
                   source="provider-error", only_lower=("max_output_tokens",))


def record_context_window(base_url: str, model_name: str, value: int) -> bool:
    """记录实测的上下文窗口（provider 报错文案里给出的真值）。

    来源是"我们超限了"，因此这个值是该端点的**真实上限**，可覆盖静态推断值；
    但仍让位于用户在配置里显式声明（用户可能故意接了不同参数的同名模型）。
    """
    try:
        value = int(value)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    changed = _update(base_url, model_name, {"context_window": value},
                      source="provider-error")
    if changed:
        logger.info(f"Model '{model_name}': context window learned from provider "
                    f"error = {value} tokens.")
    return changed


def unsupported_params(base_url: str, model_name: str) -> Set[str]:
    """该模型已被证实不接受的请求参数集合。"""
    return {str(p) for p in (get(base_url, model_name).get("unsupported_params") or [])}


def cached_max_output_tokens(base_url: str, model_name: str) -> Optional[int]:
    """该模型已实测的输出上限（未实测过返回 None）。"""
    value = get(base_url, model_name).get("max_output_tokens")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------- #
#  窗口合成（唯一入口）
# ---------------------------------------------------------------------- #
def resolve_window(model_name: str, base_url: str = "",
                   explicit: Optional[Any] = None) -> Tuple[int, str]:
    """合成上下文窗口，返回 ``(tokens, 来源标签)``。

    优先级（高 → 低）：

    1. ``explicit`` —— 用户在该 provider 配置里声明的 ``context_window``；
    2. 实测记录 —— 本表里 provider 报错给出的真值；
    3. 静态推断 —— litellm ``model_cost`` 与内置家族表取较大值；
    4. 兜底默认 —— 远程端点 300K（现代主流模型的下限），本地推理端点 32K。

    ``explicit`` 与实测值都只作用于**记名模型**；静态推断不区分 base_url，
    因此仍可能对自建网关估错——这正是 1/2 两级存在的理由。
    """
    # 延迟导入：静态推断在 token_estimator，而它又通过本模块取实测值，
    # 模块级互相 import 会成环；函数内导入既避免环，也不影响调用频次（有缓存）。
    from src.core.token_estimator import (_DEFAULT_CONTEXT_WINDOW, _LOCAL_CONTEXT_WINDOW,
                                          _static_context_window)

    declared = _positive_int(explicit)
    if declared:
        return declared, "user-declared"

    learned = _positive_int(get(base_url, model_name).get("context_window")
                            if model_name else None)
    if learned:
        return learned, "learned"

    static = _static_context_window(model_name)
    if static:
        return static, "static"
    if is_local_endpoint(base_url):
        return _LOCAL_CONTEXT_WINDOW, "default-local"
    return _DEFAULT_CONTEXT_WINDOW, "default"


def _positive_int(value: Any) -> int:
    """宽松取正整数（``"128000"`` / ``128000`` 都认；否则 0）。"""
    if value in (None, ""):
        return 0
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0
