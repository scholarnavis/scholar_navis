# main.py
import os
import sys

from src.core.network_worker import setup_global_network_env

# 1. 确定运行根目录
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 2. 定义本地 models 目录
LOCAL_MODEL_DIR = os.path.join(BASE_DIR, "models")
if not os.path.exists(LOCAL_MODEL_DIR):
    os.makedirs(LOCAL_MODEL_DIR)

# 3. 强行注入环境变量 (覆盖所有默认值)
os.environ["HF_HOME"] = LOCAL_MODEL_DIR
os.environ["SENTENCE_TRANSFORMERS_HOME"] = LOCAL_MODEL_DIR

# 遥测禁用
os.environ["ANONYMIZED_TELEMETRY"] = "False"
os.environ["SCARF_NO_ANALYTICS"] = "true"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"


from PySide6.QtWidgets import QApplication
from src.ui.main_window import MainWindow
from src.core.logger import setup_logger

if __name__ == "__main__":
    logger = setup_logger()
    logger.info(f"System Launching. HF_HOME forced to: {LOCAL_MODEL_DIR}")

    # 🌟 在启动任何功能前，先行打入全局网络代理与镜像配置
    setup_global_network_env()

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()

    sys.exit(app.exec())