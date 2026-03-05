import platform
import subprocess
import logging
import psutil
import sys


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
                except Exception:
                    pass

            elif system == "Darwin":
                output = subprocess.check_output(["system_profiler", "SPDisplaysDataType"], text=True)
                for line in output.split('\n'):
                    if "Chipset Model:" in line:
                        gpus.append({"name": line.split(":")[1].strip(), "vram": "Unified Memory"})

            elif system == "Linux":
                try:
                    smi_out = subprocess.check_output(
                        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], text=True)
                    for line in smi_out.strip().split('\n'):
                        parts = line.split(',')
                        if len(parts) == 2:
                            mb_val = float(parts[1].replace("MiB", "").strip())
                            gpus.append({"name": parts[0].strip(), "vram": f"{mb_val / 1024:.1f} GB"})
                except:
                    output = subprocess.check_output(["lspci"], text=True)
                    names = [line.split(': ')[1].strip() for line in output.split('\n') if
                             "VGA" in line or "3D" in line]
                    for name in names: gpus.append({"name": name, "vram": "Unknown VRAM"})
        except Exception as e:
            self.logger.warning(f"Failed to fetch GPU info: {e}")

        unique_gpus = []
        seen = set()
        for g in gpus:
            if g['name'] not in seen:
                seen.add(g['name'])
                unique_gpus.append(g)

        return unique_gpus if unique_gpus else [{"name": "Unknown GPU", "vram": "N/A"}]


    def get_onnx_providers(self):
        try:
            import onnxruntime as ort
            return ort.get_available_providers()
        except ImportError:
            return ["CPUExecutionProvider"]

    def get_available_devices(self):
        providers = self.get_onnx_providers()
        gpu_info_list = self.get_gpu_info()

        devices = [
            {"id": "cpu", "name": "CPU (Universal Fallback - Slow but Safe)"}
        ]

        has_cuda = "CUDAExecutionProvider" in providers
        has_dml = "DmlExecutionProvider" in providers
        has_coreml = "CoreMLExecutionProvider" in providers
        has_rocm = "ROCmExecutionProvider" in providers

        if has_coreml:
            devices.append({"id": "coreml", "name": "Apple Silicon (CoreML)"})

        sys_name = platform.system()

        for i, gpu_dict in enumerate(gpu_info_list):
            gpu_name = gpu_dict.get("name", "Unknown GPU")
            gpu_lower = gpu_name.lower()

            if "nvidia" in gpu_lower:
                if has_cuda:
                    devices.append({"id": f"cuda:{i}", "name": f"{gpu_name} (CUDA Accelerated)"})
                elif sys_name == "Windows" and has_dml:
                    devices.append({"id": f"dml:{i}", "name": f"{gpu_name} (DirectML Fallback)"})
                else:
                    devices.append({"id": f"unsupported_{i}", "name": f"{gpu_name} (Needs 'onnxruntime-gpu')"})

            elif "amd" in gpu_lower or "radeon" in gpu_lower:
                if sys_name == "Windows":
                    if has_dml:
                        devices.append({"id": f"dml:{i}", "name": f"{gpu_name} (DirectML)"})
                    else:
                        devices.append({"id": f"unsupported_{i}", "name": f"{gpu_name} (Needs 'onnxruntime-directml')"})
                elif sys_name == "Linux":
                    if has_rocm:
                        devices.append({"id": f"rocm:{i}", "name": f"{gpu_name} (ROCm)"})
                    else:
                        devices.append(
                            {"id": f"unsupported_{i}", "name": f"{gpu_name} (Needs 'onnxruntime-rocm' on Linux)"})
                else:
                    if not has_coreml:
                        devices.append({"id": f"unsupported_{i}", "name": f"{gpu_name} (OS unsupported for AMD AI)"})

            elif "intel" in gpu_lower or "uhd" in gpu_lower or "iris" in gpu_lower:
                if sys_name == "Windows" and has_dml:
                    devices.append({"id": f"dml:{i}", "name": f"{gpu_name} (DirectML)"})
                else:
                    devices.append({"id": f"unsupported_{i}", "name": f"{gpu_name} (Needs DirectML or OpenVINO)"})

        seen_ids = set()
        unique_devices = []
        for d in devices:
            if d['id'] not in seen_ids:
                seen_ids.add(d['id'])
                unique_devices.append(d)

        return unique_devices

    def get_sys_info(self):
        info = {}
        info['os'] = platform.platform()
        info['python_ver'] = sys.version.split()[0]
        info['cpu'] = platform.processor() or platform.machine()

        try:
            info['cpu_cores'] = f"{psutil.cpu_count(logical=False)}C / {psutil.cpu_count(logical=True)}T"
        except:
            info['cpu_cores'] = "Unknown Cores"

        try:
            mem = psutil.virtual_memory()
            info['ram_total'] = f"{mem.total / (1024 ** 3):.1f} GB"
            info['ram_available'] = f"{mem.available / (1024 ** 3):.1f} GB"
        except:
            info['ram_total'] = "Unknown"
            info['ram_available'] = "Unknown"

        gpu_info_list = self.get_gpu_info()

        info['gpu_info'] = gpu_info_list

        info['gpus'] = [g['name'] for g in gpu_info_list]

        info['ort_providers'] = self.get_onnx_providers()

        try:
            import onnxruntime as ort
            info['ort_version'] = ort.__version__
        except:
            info['ort_version'] = "N/A"

        return info

    def get_optimal_device(self):
        providers = self.get_onnx_providers()
        if "CUDAExecutionProvider" in providers: return "cuda:0"
        if "DmlExecutionProvider" in providers: return "dml:0"
        if "CoreMLExecutionProvider" in providers: return "coreml"
        return "cpu"

    def parse_device_string(self, setting_str):
        if not setting_str or setting_str.lower() == "auto":
            return self.get_optimal_device()
        return setting_str

    def log_system_report(self):
        info = self.get_sys_info()
        self.logger.info(f"System: {info['os']} | Python: {info['python_ver']}")
        self.logger.info(f"GPUs: {', '.join(info['gpus'])}")
        self.logger.info(f"ONNX Providers: {', '.join(info['ort_providers'])}")