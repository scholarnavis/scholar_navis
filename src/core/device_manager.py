import platform
import subprocess
import logging
import sys

from src.core.onnx_provider import (
    AUTO_PRIORITY, HINT_ID_PREFIX, PROVIDER_CPU, list_available_providers,
    probe_provider, resolve_provider, tensorrt_runtime_available,
)


def _no_window_flags() -> int:
    """子进程无控制台窗口标志（仅 Windows 有意义）。"""
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


class DeviceManager:
    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DeviceManager, cls).__new__(cls)
            cls._instance.logger = logging.getLogger("DeviceManager")
            if not cls._initialized:
                cls._instance.log_system_report()
                cls._initialized = True
        return cls._instance

    def get_gpu_info(self):
        gpus = []
        system = platform.system()
        try:
            if system == "Windows":
                cmd = ["powershell", "-NoProfile", "-Command",
                       "Get-CimInstance -ClassName Win32_VideoController | Select-Object Name, AdapterRAM | ConvertTo-Json"]
                output = subprocess.check_output(cmd, text=True, creationflags=subprocess.CREATE_NO_WINDOW).strip()
                if output:
                    import json
                    data = json.loads(output)
                    if isinstance(data, dict): data = [data]
                    for item in data:
                        name = item.get("Name", "Unknown GPU")
                        ram_bytes = item.get("AdapterRAM", 0)

                        if ram_bytes:
                            if ram_bytes in [4294967296, 4293918720]:
                                vram_gb = "≥ 4.0 GB"
                            else:
                                vram_gb = f"{ram_bytes / (1024 ** 3):.1f} GB"
                        else:
                            vram_gb = "Shared / Unknown"
                        gpus.append({"name": name, "vram": vram_gb})

                try:
                    smi_out = subprocess.check_output(
                        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                        text=True, creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    nvidia_vrams = {}
                    for line in smi_out.strip().split('\n'):
                        parts = line.split(',')
                        if len(parts) == 2:
                            n_name = parts[0].strip()
                            n_mib = float(parts[1].replace("MiB", "").strip())
                            nvidia_vrams[n_name] = f"{n_mib / 1024:.1f} GB"

                    # 将真实 VRAM 覆盖回去
                    for gpu in gpus:
                        for nv_name, real_vram in nvidia_vrams.items():
                            if nv_name.lower() in gpu["name"].lower() or gpu["name"].lower() in nv_name.lower():
                                gpu["vram"] = real_vram
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass

            elif system == "Darwin":
                output = subprocess.check_output(["system_profiler", "SPDisplaysDataType"], text=True)
                for line in output.split('\n'):
                    if "Chipset Model:" in line:
                        gpus.append({"name": line.split(":")[1].strip(), "vram": "Unified Memory"})

            elif system == "Linux":
                # 两条信息源互补：驱动接口（nvidia-smi/NVML）能给出显存，
                # PCI 总线（lspci）才能看到 AMD/Intel 集显。只取其一都会漏。
                gpus.extend(self._nvidia_gpus())
                has_nvidia = any("nvidia" in g["name"].lower() for g in gpus)
                for gpu in self._pci_display_gpus():
                    if has_nvidia and "nvidia" in gpu["name"].lower():
                        continue
                    gpus.append(gpu)
        except Exception as e:
            self.logger.warning(f"Failed to fetch GPU info: {e}")

        unique_gpus = []
        seen = set()
        for g in gpus:
            if g['name'] not in seen:
                seen.add(g['name'])
                unique_gpus.append(g)

        return unique_gpus if unique_gpus else [{"name": "Unknown GPU", "vram": "N/A"}]


    def _nvidia_gpus(self) -> list:
        """枚举 NVIDIA GPU：优先 ``nvidia-smi``，缺失时回退 NVML（nvidia-ml-py）。

        Windows 上 nvidia-smi 一般随驱动安装，但不保证在 PATH；NVML 回退可覆盖
        该情况以及精简安装（如部分容器镜像）的场景。
        """
        gpus = []
        try:
            proc = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=10,
                creationflags=_no_window_flags(),
            )
            for line in (proc.stdout or "").strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) != 2:
                    continue
                try:
                    vram = f"{float(parts[1].replace('MiB', '').strip()) / 1024:.1f} GB"
                except ValueError:
                    vram = "Unknown VRAM"
                gpus.append({"name": parts[0], "vram": vram})
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            self.logger.debug(f"nvidia-smi unavailable: {e}")

        if gpus:
            return gpus

        try:
            import pynvml

            pynvml.nvmlInit()
            try:
                for i in range(pynvml.nvmlDeviceGetCount()):
                    handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                    name = pynvml.nvmlDeviceGetName(handle)
                    if isinstance(name, bytes):
                        name = name.decode("utf-8", errors="replace")
                    total = pynvml.nvmlDeviceGetMemoryInfo(handle).total
                    gpus.append({"name": str(name), "vram": f"{total / (1024 ** 3):.1f} GB"})
            finally:
                pynvml.nvmlShutdown()
        except Exception as e:
            self.logger.debug(f"NVML fallback unavailable: {e}")

        return gpus

    def _pci_display_gpus(self) -> list:
        """从 ``lspci`` 解析显示设备（Linux 上唯一能看到集成显卡/其他厂商的途径）。"""
        try:
            proc = subprocess.run(["lspci"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as e:
            self.logger.debug(f"lspci unavailable: {e}")
            return []

        gpus = []
        for line in (proc.stdout or "").splitlines():
            if not any(tag in line for tag in ("VGA", "3D", "Display")):
                continue
            _, _, desc = line.partition(": ")
            gpus.append({"name": desc.strip() or line.strip(), "vram": "Unknown VRAM"})
        return gpus

    def get_onnx_providers(self):
        """构建期支持的 ONNX 执行提供者（不代表运行期可用，见 onnx_provider）。"""
        return list_available_providers()

    def get_functional_providers(self) -> list:
        """运行期**真实可用**的加速提供者（含 CPU 兜底）。

        与 :meth:`get_onnx_providers` 的差异是本应用在 Linux 上的关键修复点：
        ``onnxruntime-gpu`` 已安装但缺少 CUDA/cuDNN 运行库时，构建期列表仍含
        ``CUDAExecutionProvider``，而实际建会话会静默回退 CPU。

        只探测本应用认识的加速提供者（见 ``AUTO_PRIORITY``）：构建里额外携带的
        提供者（如 TensorRT）不在自动选择范围内，逐个建会话探测既无收益，
        又会产生大段无意义的 ORT 报错。
        """
        usable = [p for p in AUTO_PRIORITY if probe_provider(p)]
        return [PROVIDER_CPU] + usable

    def get_available_devices(self):
        """可供用户选择的推理设备（UI 直接消费）。

        只把**运行期真实可用**的加速路径列为可选设备：否则用户选中 `cuda:0`
        后模型加载要么静默回退（速度与预期不符），要么在回退检测处直接报错。

        GPU 存在但当前 ONNX Runtime 用不了时，追加 ``selectable=False`` 的说明项
        （id 前缀 ``unavailable``）。旧实现把这类条目做成普通可选项
        （id ``unsupported_N``），用户既能选中、又能保存，点"Test Compute Device"
        还会得到"不是可识别的加速器"这种无法行动的提示。

        可选设备顺序：Auto → CPU → 各加速器（同款 GPU 的 TensorRT 排在 CUDA 前，
        因为 TRT 更快）；``unavailable`` 说明项统一排在最后，避免混在可选项之间。
        """
        providers = self.get_onnx_providers()
        gpu_info_list = self.get_gpu_info()

        devices = [
            {"id": "auto", "name": "Auto Detect (Recommended)"},
            {"id": "cpu", "name": "CPU (Universal Fallback - Slow but Safe)"}
        ]
        hints = []

        has_cuda = "CUDAExecutionProvider" in providers and probe_provider("CUDAExecutionProvider")
        has_dml = "DmlExecutionProvider" in providers and probe_provider("DmlExecutionProvider")
        has_coreml = "CoreMLExecutionProvider" in providers and probe_provider("CoreMLExecutionProvider")
        has_rocm = "ROCmExecutionProvider" in providers and probe_provider("ROCmExecutionProvider")
        # TensorRT 用轻量判断（构建期 + libnvinfer 存在）：真实探测会构建引擎，
        # 放在设备枚举路径上代价过高。
        has_trt = tensorrt_runtime_available()

        if has_coreml:
            devices.append({"id": "coreml", "name": "Apple Silicon (CoreML)"})

        sys_name = platform.system()

        # 解绑 WMI 索引，修正笔记本 DXGI/CUDA 真实序号映射
        cuda_idx = 0
        trt_idx = 0
        is_hybrid = len(gpu_info_list) > 1  # 判断是否为双显卡环境

        def _hint(gpu_name: str, detail: str, advice: str) -> dict:
            """构造不可选说明项（UI 需渲染为 disabled 条目）。

            id 沿用 "前缀:序号" 形式（``unavailable:0``），与其它设备标识一致：
            :func:`src.core.onnx_provider.resolve_provider` 按冒号切分前缀识别。
            """
            return {
                "id": f"{HINT_ID_PREFIX}:{len(hints)}",
                "name": f"{gpu_name} (unavailable - {detail})",
                "selectable": False,
                "hint": advice,
            }

        for i, gpu_dict in enumerate(gpu_info_list):
            gpu_name = gpu_dict.get("name", "Unknown GPU")
            gpu_lower = gpu_name.lower()

            if "nvidia" in gpu_lower:
                if has_trt or has_cuda:
                    if has_trt:
                        devices.append({
                            "id": f"trt:{trt_idx}",
                            "name": (f"{gpu_name} (TensorRT - fastest; first run "
                                     f"compiles engines, then cached)")})
                    if has_cuda:
                        # CUDA 环境下，NVIDIA 独显永远从 0 开始算
                        devices.append({
                            "id": f"cuda:{cuda_idx}",
                            "name": f"{gpu_name} (CUDA Accelerated)"})
                    cuda_idx += 1
                    trt_idx += 1
                elif sys_name == "Windows" and has_dml:
                    # DirectML 环境下，双显卡笔记本的独显大概率被 DXGI 分配在 Adapter 1
                    target_id = 1 if is_hybrid else 0
                    devices.append({"id": f"dml:{target_id}", "name": f"{gpu_name} (DirectML Fallback)"})
                elif "CUDAExecutionProvider" in providers:
                    # 构建期支持 CUDA 但运行期建会话失败：多为缺少 CUDA/cuDNN 运行库
                    hints.append(_hint(
                        gpu_name, "CUDA runtime missing",
                        "ONNX Runtime ships CUDA support but cannot use this GPU because "
                        "the CUDA 12 + cuDNN 9 runtime libraries were not found. Install "
                        "them to enable acceleration, or select CPU."))
                else:
                    hints.append(_hint(
                        gpu_name, "onnxruntime-gpu not installed",
                        "Install the 'onnxruntime-gpu' package (Linux) to accelerate this GPU."))

            elif "amd" in gpu_lower or "radeon" in gpu_lower:
                if sys_name == "Windows":
                    if has_dml:
                        # 启发式判断：若是双显卡且包含英伟达，AMD就是核显(0)，否则为独显
                        target_id = 0 if is_hybrid and any("nvidia" in g.get("name", "").lower() for g in gpu_info_list) else (1 if is_hybrid else 0)
                        devices.append({"id": f"dml:{target_id}", "name": f"{gpu_name} (DirectML)"})
                    else:
                        hints.append(_hint(
                            gpu_name, "DirectML not installed",
                            "Install 'onnxruntime-directml' to accelerate this GPU on Windows, "
                            "or select CPU."))
                elif sys_name == "Linux":
                    if has_rocm:
                        devices.append({"id": f"rocm:{i}", "name": f"{gpu_name} (ROCm)"})
                    else:
                        hints.append(_hint(
                            gpu_name, "ROCm not installed",
                            "Install an onnxruntime build with ROCm support to accelerate "
                            "this AMD GPU on Linux, or select CPU."))
                else:
                    if not has_coreml:
                        hints.append(_hint(
                            gpu_name, "no accelerator available on this OS",
                            "This GPU has no ONNX Runtime accelerator on this platform. Select CPU."))

            elif "intel" in gpu_lower or "uhd" in gpu_lower or "iris" in gpu_lower:
                if sys_name == "Windows" and has_dml:
                    # Intel 核显在 DXGI 中永远是 Adapter 0
                    devices.append({"id": "dml:0", "name": f"{gpu_name} (DirectML)"})
                else:
                    hints.append(_hint(
                        gpu_name, "needs DirectML or OpenVINO",
                        "This integrated GPU is only accelerated via DirectML (Windows). "
                        "Select CPU on this platform."))

        # 说明项放在最后：它们不可选，只用于解释"为什么这块卡用不了"
        devices.extend(hints)

        seen_ids = set()
        unique_devices = []
        for d in devices:
            if d['id'] not in seen_ids:
                seen_ids.add(d['id'])
                unique_devices.append(d)

        return unique_devices

    def get_sys_info(self):
        import psutil
        info = {}
        info['os'] = platform.platform()
        info['python_ver'] = sys.version.split()[0]
        info['cpu'] = platform.processor() or platform.machine()

        try:
            info['cpu_cores'] = f"{psutil.cpu_count(logical=False)}C / {psutil.cpu_count(logical=True)}T"
        except (psutil.Error, OSError):
            info['cpu_cores'] = "Unknown Cores"

        try:
            mem = psutil.virtual_memory()
            info['ram_total'] = f"{mem.total / (1024 ** 3):.1f} GB"
            info['ram_available'] = f"{mem.available / (1024 ** 3):.1f} GB"
        except (psutil.Error, OSError):
            info['ram_total'] = "Unknown"
            info['ram_available'] = "Unknown"

        gpu_info_list = self.get_gpu_info()

        info['gpu_info'] = gpu_info_list

        info['gpus'] = [g['name'] for g in gpu_info_list]

        info['ort_providers'] = self.get_onnx_providers()
        # 运行期真实可用的加速提供者（探测结果），UI 据此判断是否真的在加速，
        # 避免"构建期列表里有 CUDA 就显示 Hardware Accelerated"的误导。
        info['ort_providers_active'] = self.get_functional_providers()

        try:
            import onnxruntime as ort
            info['ort_version'] = ort.__version__
        except (ImportError, OSError, AttributeError):
            info['ort_version'] = "N/A"

        return info

    def get_optimal_device(self):
        """返回当前机器**实际可用**的最优设备标识。

        与旧实现的差别：不再仅凭构建期提供者列表就返回 ``cuda:0``。Linux 上
        ``onnxruntime-gpu`` 与 CUDA/cuDNN 运行库缺失的组合极常见，此时返回
        ``cuda:0`` 会让模型加载失败或静默降速。
        """
        resolved = resolve_provider("auto")

        if resolved.provider == "CUDAExecutionProvider":
            return "cuda:0"

        if resolved.provider == "DmlExecutionProvider":
            # DirectML 场景保留原有的双显卡序号启发式
            return "dml:1" if len(self.get_gpu_info()) > 1 else "dml:0"

        if resolved.provider == "CoreMLExecutionProvider":
            return "coreml"

        if resolved.provider == "ROCmExecutionProvider":
            return "rocm:0"

        return "cpu"


    def parse_device_string(self, setting_str):
        """把配置里的设备标识解析为可用设备（用于模型加载与状态展示）。"""
        if not setting_str or str(setting_str).lower() == "auto":
            return self.get_optimal_device()

        raw = str(setting_str).strip().lower()
        if raw.startswith(HINT_ID_PREFIX):
            # 历史配置可能残留 UI 说明项（unavailable_N/unsupported_N）：
            # 它们不是设备标识，直接回落到自动选择，别传到模型加载层。
            self.logger.warning(
                f"Saved device '{setting_str}' is a non-selectable hint entry; using auto.")
            return self.get_optimal_device()

        return setting_str

    def log_system_report(self):
        info = self.get_sys_info()
        self.logger.info(f"System: {info['os']} | Python: {info['python_ver']}")
        self.logger.info(f"GPUs: {', '.join(info['gpus'])}")
        self.logger.info(f"ONNX Providers (build): {', '.join(info['ort_providers'])}")
        self.logger.info(f"ONNX Providers (usable): {', '.join(info.get('ort_providers_active', []))}")
        self.logger.info(
            f"TensorRT: build={'yes' if 'TensorrtExecutionProvider' in info['ort_providers'] else 'no'} | "
            f"runtime libs={'yes' if tensorrt_runtime_available() else 'no'}")
        self.logger.info(f"Optimal device resolved to: {self.get_optimal_device()}")