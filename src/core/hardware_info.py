"""硬件信息采集（OS / CPU / 内存 / GPU），**不含任何 ONNX 运行时探测**。

为什么单独一个模块
------------------
设置页那块"System Hardware Info"原先由 ``device_manager`` 提供，而它是为本地 ONNX
推理服务的：构造时会导入 ``onnxruntime``、枚举执行提供者、跑探针会话，并顺带把这个
结果写进面板（"ONNX Engine / Providers" 两行）。本地推理下线后那些探测全部没有意义，
只留下纯硬件信息这一部分职责，因此搬到这里，``onnxruntime`` 也不再被启动链拉起。

GPU 枚举保留原来的两条互补信息源：驱动接口（``nvidia-smi`` / NVML）能给显存，
PCI 总线（``lspci``）才能看到 AMD / Intel 集显——只取其一都会漏。
"""

import json
import logging
import platform
import subprocess
import sys

from src.core.platform_env import (PLATFORM_LINUX, PLATFORM_MACOS,
                                   PLATFORM_WINDOWS, no_window_flags, os_family)

logger = logging.getLogger("HardwareInfo")

#: Windows 的 WMI 在 4 GB 附近会回绕成 4293918720（4 GiB 减 1 MiB），这两种值
#: 只能当作"至少 4 GB"，照原样换算会显示成 4.0 GB 而误导用户。
_WMI_4GB_SENTINELS = (4294967296, 4293918720)


def sys_info() -> dict:
    """OS / CPU / 内存概况。取不到的项给出可读占位，绝不抛异常。"""
    info = {
        "os": platform.platform(),
        "python_ver": sys.version.split()[0],
        "cpu": platform.processor() or platform.machine(),
    }
    try:
        import psutil

        info["cpu_cores"] = (f"{psutil.cpu_count(logical=False)}C / "
                             f"{psutil.cpu_count(logical=True)}T")
        mem = psutil.virtual_memory()
        info["ram_total"] = f"{mem.total / (1024 ** 3):.1f} GB"
        info["ram_available"] = f"{mem.available / (1024 ** 3):.1f} GB"
    except Exception as e:      # psutil 缺失或平台不支持：面板显示 Unknown 即可
        logger.debug(f"psutil unavailable for hardware info: {e}")
        info.setdefault("cpu_cores", "Unknown Cores")
        info.setdefault("ram_total", "Unknown")
        info.setdefault("ram_available", "Unknown")
    return info


def _nvidia_gpus() -> list:
    """枚举 NVIDIA GPU：优先 ``nvidia-smi``，缺失时回退 NVML。"""
    gpus = []
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
            creationflags=no_window_flags(),
        )
        for line in (proc.stdout or "").strip().splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 2:
                continue
            try:
                vram = f"{float(parts[1].replace('MiB', '').strip()) / 1024:.1f} GB"
            except ValueError:
                vram = "Unknown VRAM"
            gpus.append({"name": parts[0], "vram": vram})
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        logger.debug(f"nvidia-smi unavailable: {e}")

    if gpus:
        return gpus

    try:
        import pynvml

        pynvml.nvmlInit()
        try:
            for index in range(pynvml.nvmlDeviceGetCount()):
                handle = pynvml.nvmlDeviceGetHandleByIndex(index)
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode("utf-8", errors="replace")
                total = pynvml.nvmlDeviceGetMemoryInfo(handle).total
                gpus.append({"name": str(name), "vram": f"{total / (1024 ** 3):.1f} GB"})
        finally:
            pynvml.nvmlShutdown()
    except Exception as e:      # NVML 只是兜底，缺失不影响主路径
        logger.debug(f"NVML fallback unavailable: {e}")
    return gpus


def _pci_display_gpus() -> list:
    """从 ``lspci`` 解析显示设备（Linux 上看得到集显与其他厂商的唯一途径）。"""
    try:
        proc = subprocess.run(["lspci"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug(f"lspci unavailable: {e}")
        return []
    gpus = []
    for line in (proc.stdout or "").splitlines():
        if not any(tag in line for tag in ("VGA", "3D", "Display")):
            continue
        _, _, desc = line.partition(": ")
        gpus.append({"name": desc.strip() or line.strip(), "vram": "Unknown VRAM"})
    return gpus


def _windows_gpus() -> list:
    """Windows：WMI 列全部显示适配器，再用 ``nvidia-smi`` 校正真实显存。"""
    gpus = []
    try:
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance -ClassName Win32_VideoController | "
             "Select-Object Name, AdapterRAM | ConvertTo-Json"],
            text=True, encoding="utf-8", errors="replace",
            creationflags=no_window_flags(),
        ).strip()
        data = json.loads(output) if output else []
        if isinstance(data, dict):
            data = [data]
        for item in data:
            name = item.get("Name", "Unknown GPU")
            ram_bytes = item.get("AdapterRAM", 0) or 0
            if ram_bytes in _WMI_4GB_SENTINELS:
                vram = "≥ 4.0 GB"
            elif ram_bytes:
                vram = f"{ram_bytes / (1024 ** 3):.1f} GB"
            else:
                vram = "Shared / Unknown"
            gpus.append({"name": name, "vram": vram})
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        logger.debug(f"WMI GPU query failed: {e}")

    real = {gpu["name"]: gpu["vram"] for gpu in _nvidia_gpus()}
    for gpu in gpus:
        for name, vram in real.items():
            low_name, low_gpu = name.lower(), gpu["name"].lower()
            if low_name in low_gpu or low_gpu in low_name:
                gpu["vram"] = vram
    return gpus or _nvidia_gpus()


def _macos_gpus() -> list:
    try:
        output = subprocess.check_output(["system_profiler", "SPDisplaysDataType"],
                                         text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug(f"system_profiler unavailable: {e}")
        return []
    return [{"name": line.split(":", 1)[1].strip(), "vram": "Unified Memory"}
            for line in output.splitlines() if "Chipset Model:" in line]


def gpu_info() -> list:
    """显示适配器列表（去重；一个都没有时给出 Unknown 占位）。"""
    family = os_family()
    try:
        if family == PLATFORM_WINDOWS:
            gpus = _windows_gpus()
        elif family == PLATFORM_MACOS:
            gpus = _macos_gpus()
        elif family == PLATFORM_LINUX:
            # 两条信息源互补：驱动接口能给显存，PCI 总线才看得到集显。
            gpus = _nvidia_gpus()
            has_nvidia = any("nvidia" in gpu["name"].lower() for gpu in gpus)
            for gpu in _pci_display_gpus():
                if has_nvidia and "nvidia" in gpu["name"].lower():
                    continue
                gpus.append(gpu)
        else:
            gpus = []
    except Exception as e:
        logger.warning(f"Failed to fetch GPU info: {e}")
        gpus = []

    unique, seen = [], set()
    for gpu in gpus:
        if gpu["name"] in seen:
            continue
        seen.add(gpu["name"])
        unique.append(gpu)
    return unique or [{"name": "Unknown GPU", "vram": "N/A"}]


def collect() -> dict:
    """设置页要的一份完整快照（纯硬件，无推理运行时信息）。"""
    info = sys_info()
    info["gpu_info"] = gpu_info()
    info["gpus"] = [gpu["name"] for gpu in info["gpu_info"]]
    return info
