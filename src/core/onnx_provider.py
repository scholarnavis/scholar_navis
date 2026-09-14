"""ONNX Runtime 执行提供者（Execution Provider）解析与真实可用性探测。

背景
----
``onnxruntime.get_available_providers()`` 只反映**构建期**支持，不代表运行期
可用。典型反例（Linux 常见）：安装的是 ``onnxruntime-gpu``，但系统缺少
CUDA/cuDNN 运行库，此时 ``CUDAExecutionProvider`` 仍出现在可用列表里，真正
创建推理会话时 ORT 会静默回退到 CPU。上层若据此选择 ``cuda:0``，随后又会因为
"检测到静默回退"而抛错，整条知识库索引链路直接失败。

本模块的职责（单一职责，供 device_manager / kb_tasks / rerank_engine /
settings_tasks 共用，避免各处重复实现同一次判断）：

1. :func:`list_available_providers` —— 构建期支持的提供者；
2. :func:`probe_provider` —— 用内存中的最小 ONNX 模型真实建会话，确认该
   提供者是否**实际生效**（进程内缓存，结果不随运行期变化）；
3. :func:`resolve_provider` —— 把设备标识（``cuda:0`` / ``dml:1`` / ``auto`` …）
   解析为“实际可用”的提供者；不可用时明确降级到 CPU 并给出原因，而不是抛错。

性能
----
探测只在缓存未命中时执行（一次内存内建会话，毫秒级）；调用方在热路径
（模型加载、设备批量枚举）上只会命中字典查找。可通过环境变量
``SCHOLAR_NAVIS_SKIP_PROVIDER_PROBE=1`` 完全跳过真实探测（仅按构建期列表
判断），用于个别平台的应急兜底。
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("Core.ONNXProvider")

PROVIDER_CPU = "CPUExecutionProvider"
PROVIDER_TENSORRT = "TensorrtExecutionProvider"

#: 提示项设备标识前缀（GPU 存在但当前 ONNX Runtime 用不了）。
#: 这类条目只在 UI 中作为"不可选说明"展示，绝不能作为推理设备保存/传递。
HINT_ID_PREFIX = "unavailable"

#: 设备标识前缀 → ORT 执行提供者名称
PROVIDER_BY_PREFIX = {
    "cuda": "CUDAExecutionProvider",
    "trt": PROVIDER_TENSORRT,
    "tensorrt": PROVIDER_TENSORRT,
    "dml": "DmlExecutionProvider",
    "rocm": "ROCmExecutionProvider",
    "coreml": "CoreMLExecutionProvider",
    "cpu": PROVIDER_CPU,
}

#: ``auto`` 模式的探测优先级（CPU 恒为兜底，不列入此表）。
#
# TensorRT 刻意**不在**其中：TRT 首次建会话要构建引擎（秒级到分钟级）并写缓存，
# 让 auto 隐式选中它会让首次索引出现难以解释的长时间等待。需要极致速度的用户
# 在"Compute Device"里显式选择 TensorRT 即可（见 device_manager 的选项）。
AUTO_PRIORITY = (
    "CUDAExecutionProvider",
    "DmlExecutionProvider",
    "ROCmExecutionProvider",
    "CoreMLExecutionProvider",
)

_TRUTHY = {"1", "true", "yes", "on"}

#: 探测结果缓存：key = "Provider[:device_id]"，value = 是否真实生效。
_probe_cache: dict = {}
_probe_lock = threading.Lock()
_probe_model_cache: Optional[bytes] = None

#: 静默窗口嵌套深度（见 :func:`_probe_log_quiet`）。
_probe_quiet_depth = 0
#: 窗口内的 ORT 全局日志级别（4 = FATAL）。
_PROBE_LOG_SEVERITY = 4
#: 窗口结束后的基线级别（3 = ERROR）：保留真实错误，压掉重复警告。
_RUNTIME_LOG_SEVERITY = 3


@dataclass(frozen=True)
class ResolvedProvider:
    """设备标识解析结果。

    :param provider: 实际应传给 ORT / optimum 的提供者名称。
    :param provider_options: 提供者选项（如 ``{"device_id": 0}``），无则 None。
    :param requested: 调用方原始请求（设备标识字符串）。
    :param degraded: True 表示请求的加速设备不可用，已降级到 CPU。
    :param reason: 降级原因（可直接展示给用户），无降级时为空串。
    """

    provider: str
    provider_options: Optional[dict]
    requested: str
    degraded: bool
    reason: str = ""

    @property
    def device_id(self) -> Optional[int]:
        if self.provider_options and "device_id" in self.provider_options:
            return self.provider_options["device_id"]
        return None


def _skip_probe() -> bool:
    return os.environ.get("SCHOLAR_NAVIS_SKIP_PROVIDER_PROBE", "").strip().lower() in _TRUTHY


def _shared_library_present(*names: str) -> bool:
    """系统里是否存在指定共享库（``ctypes.util.find_library`` + Windows PATH 兜底）。"""
    import ctypes.util

    for name in names:
        try:
            if ctypes.util.find_library(name):
                return True
        except (OSError, TypeError):
            pass

    # Windows 上 find_library 依赖注册表/搜索路径，常见情形容错率偏高，补扫 PATH
    prefixes = tuple(n.lower() for n in names)
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        try:
            for entry in os.listdir(directory):
                low = entry.lower()
                if low.endswith(".dll") and any(low.startswith(p) for p in prefixes):
                    return True
        except OSError:
            continue
    return False


def tensorrt_engine_cache_dir() -> str:
    """TensorRT 引擎/计时缓存目录。

    TRT 会把每个（子）图编译成引擎，构建开销极大（秒级到分钟级），必须跨会话
    复用：开启 ``trt_engine_cache_enable`` 并把路径固定到模型缓存目录下。
    可用环境变量 ``SCHOLAR_NAVIS_TRT_CACHE`` 覆盖。
    """
    override = os.environ.get("SCHOLAR_NAVIS_TRT_CACHE", "").strip()
    if override:
        cache_dir = os.path.expanduser(override)
    else:
        try:
            from src.core.models_registry import _get_hf_home

            cache_dir = os.path.join(_get_hf_home(), "tensorrt_cache")
        except Exception:  # 缓存根目录不可知时的静态兜底
            cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "scholar_navis", "tensorrt")

    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError as e:
        logger.warning(f"Cannot create TensorRT cache dir '{cache_dir}': {e}")
    return cache_dir


def _tensorrt_provider_options(device_id: int) -> dict:
    """TensorRT 提供者选项（引擎缓存 + FP16）。

    FP16 是 TRT 路线的主要收益来源，且对嵌入/重排这类模型精度影响可忽略；
    引擎与计时缓存落在 :func:`tensorrt_engine_cache_dir`，避免每次启动重编译。
    """
    return {
        "device_id": device_id,
        "trt_engine_cache_enable": True,
        "trt_engine_cache_path": tensorrt_engine_cache_dir(),
        "trt_timing_cache_enable": True,
        "trt_fp16_enable": True,
    }


def tensorrt_runtime_available() -> bool:
    """轻量判断 TensorRT 是否可用（不建会话）。

    枚举设备时不使用 :func:`probe_provider`：TRT 建会话会触发引擎构建，放在
    设备枚举路径上会明显拖慢设置页与聊天头部。这里只做"构建期含 TRT 提供者 +
    关键运行库存在"的静态判断；真正使用前由 :func:`resolve_provider`
    （``probe=True``）做一次实测。
    """
    if PROVIDER_TENSORRT not in list_available_providers():
        return False
    return _shared_library_present("nvinfer", "nvinfer_10", "nvinfer_plugin")


def list_available_providers() -> list:
    """构建期支持的提供者列表（ORT 不可用时返回 CPU）。

    查询本身就处于静默窗口内：实测（缺 libcublasLt 的机器）``get_available_providers()``
    就会让 ORT 尝试加载 CUDA 提供者库并向 stderr 打印整段报错，因此它和建会话
    一样需要放进 :func:`_probe_log_quiet`。窗口可重入（见该函数的深度计数），
    嵌套调用不会提前把日志级别降回去。
    """
    try:
        with _probe_log_quiet():
            import onnxruntime as ort
            return list(ort.get_available_providers())
    except Exception as e:  # ImportError / OSError（缺运行库）等
        logger.warning(f"onnxruntime unavailable, assuming CPU only: {e}")
        return [PROVIDER_CPU]


def _probe_model_bytes() -> Optional[bytes]:
    """构造最小可推理 ONNX 模型（内存内），用于建会话探测提供者。"""
    global _probe_model_cache
    if _probe_model_cache is not None:
        return _probe_model_cache
    try:
        from onnx import TensorProto, helper

        x = helper.make_tensor_value_info("X", TensorProto.FLOAT, [1, 3])
        y = helper.make_tensor_value_info("Y", TensorProto.FLOAT, [1, 3])
        node = helper.make_node("Identity", inputs=["X"], outputs=["Y"])
        graph = helper.make_graph([node], "sn-provider-probe", [x], [y])
        opset = helper.make_opsetid("", 14)
        model = helper.make_model(
            graph, producer_name="scholar-navis-probe", opset_imports=[opset])
        _probe_model_cache = model.SerializeToString()
    except Exception as e:
        logger.warning(f"Failed to build provider probe model: {e}")
        _probe_model_cache = None
    return _probe_model_cache


def probe_provider(provider: str, provider_options: Optional[dict] = None) -> bool:
    """真实探测某个执行提供者是否生效（进程内缓存）。

    ``CPUExecutionProvider`` 恒为 True（ORT 的必要兜底）。探测失败仅记录日志，
    不影响调用方继续使用 CPU。
    """
    if provider == PROVIDER_CPU:
        return True

    key = provider if not provider_options else f"{provider}@{sorted(provider_options.items())}"
    with _probe_lock:
        if key in _probe_cache:
            return _probe_cache[key]

    result = _probe_impl(provider, provider_options)

    with _probe_lock:
        _probe_cache[key] = result
    return result


def _probe_impl(provider: str, provider_options: Optional[dict]) -> bool:
    # 整个探测（含可用性查询）都在静默窗口内：查询本身就会让 ORT 尝试加载
    # CUDA/TensorRT 提供者库，从而打印整段 C++ 报错与 Python 层 EP Error。
    with _probe_log_quiet():
        available = list_available_providers()
        if provider not in available:
            logger.info(f"Provider '{provider}' not present in this onnxruntime build.")
            return False

        if _skip_probe():
            logger.info(f"Provider probe skipped by env flag; trusting build list for '{provider}'.")
            return True

        model_bytes = _probe_model_bytes()
        if model_bytes is None:
            # 无法构造探测模型（onnx 缺失）：不做判断，避免误杀可用加速。
            logger.warning(f"Cannot probe '{provider}'; assuming available.")
            return True

        candidates = [(provider, provider_options)] if provider_options else [provider]
        candidates.append(PROVIDER_CPU)  # 兜底，避免会话创建直接抛错

        try:
            import onnxruntime as ort

            session = ort.InferenceSession(model_bytes, providers=candidates)
            active = session.get_providers()
            ok = bool(active) and active[0] == provider
            if ok:
                logger.info(f"Provider probe OK: '{provider}' {provider_options or ''}")
            else:
                logger.warning(
                    f"Provider probe: requested '{provider}' {provider_options or ''} "
                    f"but onnxruntime activated {active}. Runtime libraries are likely missing.")
            return ok
        except Exception as e:
            logger.warning(f"Provider probe failed for '{provider}': {e}")
            return False


@contextlib.contextmanager
def _probe_log_quiet():
    """屏蔽 ORT 的整段噪声（C++ 层 stderr + Python 层 stdout），**可重入**。

    两个噪声源（在缺少 CUDA/TensorRT 运行库的机器上每次启动都会出现）：

    1. **C++ 层**：提供者库（或 ``get_available_providers()`` 内部的提供者信息
       查询）加载失败时直接向 stderr 打印整段报错，例如
       ``[E:onnxruntime:Default, provider_bridge_ort.cc:...] Failed to load library
       libonnxruntime_providers_cuda.so with error: libcublasLt.so.12 ...``。
       把 ORT 全局日志级别临时提到 FATAL 即可压下（实测有效，含首次加载）。
    2. **Python 层**：``onnxruntime_inference_collection`` 在建会话失败并自动回退
       时用 ``print()`` 输出 ``EP Error ... Falling back to ...``。通过重定向
       stdout 捕获，并以 DEBUG 级别写入日志（信息不丢失）。

    本模块是这两个噪声源的唯一触发点，结论都会由 :func:`probe_provider` 整理成
    一条可读警告，因此窗口内只需静默；窗口结束后恢复 ERROR 基线，真实会话错误
    依旧可见。

    **可重入**：:func:`list_available_providers` 自身也在窗口内，会被
    :func:`_probe_impl` 嵌套调用。深度计数保证只有最外层设置/恢复日志级别与
    重定向，内层退出不会把级别提前降回去（早期版本正是踩了这个坑：内层把级别
    降回 3，导致后续建会话的报错又冒出来）。

    注意：``redirect_stdout`` 作用于 ``sys.stdout``，窗口极短（毫秒级）；探测通常
    在后台线程执行，该窗口内其它线程的 ``print`` 会一并被收进 DEBUG 日志。
    """
    global _probe_quiet_depth
    try:
        import onnxruntime as ort
    except Exception:  # onnxruntime 不可用时无需处理
        yield
        return

    with _probe_lock:
        _probe_quiet_depth += 1
        outermost = _probe_quiet_depth == 1

    captured = io.StringIO() if outermost else None
    if outermost:
        try:
            ort.set_default_logger_severity(_PROBE_LOG_SEVERITY)
        except Exception as e:
            logger.debug(f"Could not raise ORT log severity for probe: {e}")

    try:
        if outermost:
            with contextlib.redirect_stdout(captured):
                yield
        else:
            yield
    finally:
        with _probe_lock:
            _probe_quiet_depth -= 1
            last = _probe_quiet_depth <= 0
            if last:
                _probe_quiet_depth = 0  # 防御异常路径下的计数漂移
        if last:
            try:
                ort.set_default_logger_severity(_RUNTIME_LOG_SEVERITY)
            except Exception:  # 非关键路径，失败静默
                pass

            noise = captured.getvalue().strip() if captured else ""
            if noise:
                logger.debug(f"Suppressed ORT output during provider probe:\n{noise}")


def resolve_provider(device_str: Optional[str], probe: bool = True) -> ResolvedProvider:
    """把设备标识解析为实际可用的执行提供者，不可用时降级到 CPU。

    :param device_str: ``auto`` / ``cpu`` / ``cuda:0`` / ``trt:0`` / ``dml:1`` /
        ``rocm:0`` / ``coreml`` 等；空值与 ``auto`` 等价。
    :param probe: False 时只做名称映射（用于纯展示场景，避免任何运行时开销）。

    已知前缀但运行期不可用（如 Linux 缺 CUDA 运行库）时返回 CPU 并提供
    ``reason``，**不抛异常**：加速失败不应阻断功能，仅降低速度。
    """
    raw = str(device_str or "auto").strip().lower()

    if raw in ("", "auto"):
        if probe:
            for prov in AUTO_PRIORITY:
                if probe_provider(prov):
                    return ResolvedProvider(prov, None, raw or "auto", False)
        return ResolvedProvider(PROVIDER_CPU, None, raw or "auto", False)

    prefix, _, suffix = raw.partition(":")
    device_id = int(suffix) if suffix.isdigit() else 0

    if raw.startswith(HINT_ID_PREFIX) or prefix == HINT_ID_PREFIX:
        # UI 的"不可用提示项"（GPU 存在但当前 ONNX Runtime 用不了）不是设备标识，
        # 用户若通过旧配置选到它，给出可操作的说明而不是"无法识别的加速器"。
        reason = (
            "This GPU is listed as unavailable: the installed ONNX Runtime cannot "
            "use it yet. Select 'CPU' to keep working, or install the required "
            "runtime (CUDA/cuDNN, DirectML, ROCm...) and re-run the device test.")
        logger.warning(f"Unavailable-device hint '{raw}' resolved to CPU: {reason}")
        return ResolvedProvider(PROVIDER_CPU, None, raw, True, reason)

    provider = PROVIDER_BY_PREFIX.get(prefix)

    if provider is None:
        reason = (
            f"Device '{raw}' is not a recognized ONNX accelerator; using CPU instead. "
            f"Pick one of the devices listed under 'Compute Device'.")
        logger.warning(reason)
        return ResolvedProvider(PROVIDER_CPU, None, raw, True, reason)

    if provider == PROVIDER_CPU:
        return ResolvedProvider(PROVIDER_CPU, None, raw, False)

    # TensorRT 必须携带引擎缓存等选项，否则每次启动都要重新编译引擎
    if provider == PROVIDER_TENSORRT:
        options = _tensorrt_provider_options(device_id)
    else:
        options = {"device_id": device_id} if suffix.isdigit() else None

    if probe and not probe_provider(provider, options):
        if provider == PROVIDER_TENSORRT:
            detail = ("missing TensorRT/CUDA runtime libraries "
                      "(libnvinfer, CUDA, cuDNN)")
        else:
            detail = "missing driver/CUDA or cuDNN libraries"
        reason = (
            f"Requested '{raw}' but {provider} is not usable at runtime "
            f"({detail}). Falling back to CPU; "
            f"results are unaffected, only speed. "
            f"Use Settings -> Hardware Test for details.")
        logger.warning(reason)
        return ResolvedProvider(PROVIDER_CPU, None, raw, True, reason)

    return ResolvedProvider(provider, options, raw, False)


def provider_usable(provider: str) -> bool:
    """供零散场景使用的便捷封装（等价于 ``probe_provider``）。"""
    return probe_provider(provider)


def reset_cache() -> None:
    """清空探测缓存（测试或运行期重新安装 CUDA 运行库后使用）。"""
    with _probe_lock:
        _probe_cache.clear()
    logger.info("ONNX provider probe cache cleared.")
