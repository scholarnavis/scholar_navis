import logging
from src.core.models_registry import get_model_conf, check_model_exists, resolve_auto_model
from src.core.signals import GlobalSignals
from src.core.device_manager import DeviceManager

class ModelManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ModelManager, cls).__new__(cls)
            cls._instance.logger = logging.getLogger("ModelManager")
            cls._instance.dev_mgr = DeviceManager()
        return cls._instance

    def ensure_model_ready(self, model_id, model_type="embedding", device_str=None):
        if not device_str:
            device_str = self.dev_mgr.get_optimal_device()

        real_id = resolve_auto_model(model_type, device_str) if model_id.endswith("_auto") else model_id

        conf = get_model_conf(real_id, model_type)
        if not conf:
            self.logger.error(f"Access Denied: Unknown Model '{real_id}'.")
            return False

        repo_id = conf.get('hf_repo_id', '')

        # 2. 检查本地是否已安装
        if check_model_exists(repo_id):
            # 💡 正常使用，并在日志显示
            self.logger.info(f"Model Activated [{model_type.upper()}]: {conf.get('ui_name', real_id)}")
            return True
        else:
            # 未安装，发送信号跳转到 Settings 提示下载
            self.logger.warning(f"Missing Model [{model_type.upper()}]: '{real_id}'. Redirecting to Settings...")
            GlobalSignals().navigate_to_tool.emit("Global Settings")
            GlobalSignals().request_model_download.emit(real_id, model_type)
            return False