import os
import sys

from PySide6.QtWidgets import QApplication

from src.core.mcp_manager import MCPManager
from src.core.network_worker import setup_global_network_env
from src.core.config_manager import ConfigManager
from src.core.logger import setup_logger
from src.ui.main_window import MainWindow

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Disable telemetry for academic privacy
os.environ["ANONYMIZED_TELEMETRY"] = "False"
os.environ["SCARF_NO_ANALYTICS"] = "true"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"



def init_mcp(logger):
    """
    Initialize MCP servers: One Core Internal + One External Bridge
    """
    mcp_mgr = MCPManager.get_instance()
    config = ConfigManager().user_settings

    os.environ["NCBI_API_EMAIL"] = config.get("ncbi_email", "scholar.navis@example.com")
    os.environ["NCBI_API_KEY"] = config.get("ncbi_api_key", "")
    os.environ["S2_API_KEY"] = config.get("s2_api_key", "")

    logger.info("Starting MCP subsystem initialization.")

    # 内置核心 MCP (Core Academic)
    try:
        logger.info("Attempting to load internal academic MCP server.")
        mcp_mgr.connect_sync(
            python_path=sys.executable,
            args=["-c", "from plugins.academic_mcp_server import mcp; mcp.run(transport='stdio')"]
        )
        logger.info("Internal academic MCP server initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to start internal academic MCP server: {e}")

    # 外挂扩展 MCP
    ext_python = config.get("external_python_path", "python")
    ext_plugins_dir = os.path.join(BASE_DIR, "plugins_ext")

    if not os.path.exists(ext_plugins_dir):
        try:
            os.makedirs(ext_plugins_dir)
        except Exception as e:
            logger.error(f"Failed to create external plugins directory: {e}")
            return

    # 严格约定外挂桥接器的文件名为 external_bridge.py
    bridge_script = os.path.join(ext_plugins_dir, "external_bridge.py")
    if os.path.exists(bridge_script):
        try:
            logger.info(f"Attempting to load external MCP bridge: {bridge_script}")
            mcp_mgr.connect_sync(script_path=bridge_script, python_path=ext_python)
            logger.info("External MCP bridge loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to load external bridge. Error: {e}")
    else:
        logger.info("No external_bridge.py found. Running with core tools only.")


if __name__ == "__main__":
    logger = setup_logger()
    logger.info(f"System Launching.")

    # Apply global network proxies and mirrors before initiating core components
    setup_global_network_env()

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()

    init_mcp(logger)

    sys.exit(app.exec())