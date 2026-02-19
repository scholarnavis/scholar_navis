import torch
import platform
import logging
import psutil
import sys
import subprocess
import shutil


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

    def _get_gpu_driver_version(self):
        """尝试通过 nvidia-smi 获取显卡驱动版本"""
        if not torch.cuda.is_available():
            return "N/A"

        # 尝试查找 nvidia-smi
        smi_path = shutil.which("nvidia-smi")
        if not smi_path:
            return "Unknown (nvidia-smi not found)"

        try:
            # nvidia-smi --query-gpu=driver_version --format=csv,noheader
            output = subprocess.check_output(
                [smi_path, "--query-gpu=driver_version", "--format=csv,noheader"],
                encoding="utf-8"
            )
            return output.strip().split('\n')[0]
        except Exception as e:
            return f"Unknown (Error: {str(e)})"

    def get_sys_info(self):
        """获取详尽的系统硬件信息"""
        info = {}


        info['os'] = platform.platform()
        info['python_ver'] = sys.version.split()[0]


        info['cpu'] = platform.processor() or platform.machine()
        info['cpu_cores'] = f"{psutil.cpu_count(logical=False)}C / {psutil.cpu_count(logical=True)}T"

        try:
            mem = psutil.virtual_memory()
            info['ram_total'] = f"{mem.total / (1024 ** 3):.1f} GB"
            info['ram_available'] = f"{mem.available / (1024 ** 3):.1f} GB"
            info['ram_percent'] = f"{mem.percent}%"
        except:
            info['ram_total'] = "Unknown"
            info['ram_available'] = "Unknown"
            info['ram_percent'] = "Unknown"

        info['cuda_support'] = torch.cuda.is_available()
        info['torch_cuda_ver'] = torch.version.cuda if info['cuda_support'] else "N/A"
        info['gpu_driver_ver'] = self._get_gpu_driver_version()

        gpu_details = []
        if info['cuda_support']:
            try:
                cnt = torch.cuda.device_count()
                for i in range(cnt):
                    name = torch.cuda.get_device_name(i)
                    props = torch.cuda.get_device_properties(i)
                    vram_total = f"{props.total_memory / (1024 ** 3):.1f} GB"
                    # 计算能力
                    cap = f"{props.major}.{props.minor}"
                    gpu_details.append(f"[{i}] {name} (VRAM: {vram_total}, Compute: {cap})")
            except Exception as e:
                gpu_details.append(f"Error reading GPU: {e}")
        elif torch.backends.mps.is_available():
            gpu_details.append("Apple Silicon GPU (Shared Memory via Metal)")
            info['os'] += " (MacOS Metal Enabled)"
        else:
            gpu_details.append("None (CPU Only)")

        info['gpus'] = gpu_details
        return info

    def get_optimal_device(self):
        """自动选择最佳设备"""
        if torch.cuda.is_available():
            return "cuda"
        elif torch.backends.mps.is_available():
            return "mps"
        else:
            return "cpu"

    def parse_device_string(self, setting_str):
        if not setting_str or setting_str == "Auto":
            return self.get_optimal_device()
        return setting_str

    def log_system_report(self):
        """向日志系统输出一份完整的硬件自检报告"""
        info = self.get_sys_info()

        log_lines = []
        log_lines.append("=" * 40)
        log_lines.append("🖥️  SYSTEM HARDWARE & ENVIRONMENT REPORT")
        log_lines.append("=" * 40)
        log_lines.append(f"OS             : {info['os']}")
        log_lines.append(f"Python         : {info['python_ver']}")
        log_lines.append(f"CPU            : {info['cpu']} ({info['cpu_cores']})")
        log_lines.append(f"RAM            : {info['ram_available']} Available / {info['ram_total']} Total")
        log_lines.append("-" * 40)

        if info['cuda_support']:
            log_lines.append(f"GPU Support    : ✅ CUDA Available")
            log_lines.append(f"CUDA Version   : {info['torch_cuda_ver']} (PyTorch Built-in)")
            log_lines.append(f"Driver Version : {info['gpu_driver_ver']}")
            for gpu in info['gpus']:
                log_lines.append(f"Device         : {gpu}")
        elif torch.backends.mps.is_available():
            log_lines.append(f"GPU Support    : ✅ Apple MPS (Metal Performance Shaders)")
            log_lines.append(f"Device         : {info['gpus'][0]}")
        else:
            log_lines.append(f"GPU Support    : ❌ CPU Only")

        log_lines.append("=" * 40)

        for line in log_lines:
            self.logger.info(line)

    def check_hardware_requirements(self, recommended_config):
        """检查硬件是否满足要求"""
        warnings = []
        passed = True

        current_dev = self.get_optimal_device()
        priority = recommended_config.get("device_priority", "CPU")
        sys_info = self.get_sys_info()

        # 1. 检查 GPU 需求
        if "GPU Required" in priority:
            if current_dev == "cpu":
                passed = False
                warnings.append("This model requires a dedicated graphics card (GPU), but currently only CPU is detected.")

        # 2. 检查 VRAM
        req_vram_str = recommended_config.get("min_vram", "")
        if req_vram_str and not sys_info['cuda_support']:
            warnings.append(f"This model recommends VRAM: {req_vram_str} (No CUDA support currently).")

        return passed, warnings