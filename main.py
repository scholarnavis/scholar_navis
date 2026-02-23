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
    Initialize MCP servers:
    """
    mcp_mgr = MCPManager.get_instance()
    config = ConfigManager().user_settings

    os.environ["NCBI_API_EMAIL"] = config.get("ncbi_email", "")
    os.environ["NCBI_API_KEY"] = config.get("ncbi_api_key", "")
    os.environ["S2_API_KEY"] = config.get("s2_api_key", "")

    logger.info("Starting MCP subsystem initialization.")

    # --- Phase 1: Load Internal Compiled MCP Server ---
    # Using sys.executable with '-c' to run the module compiled inside the Nuitka binary
    try:
        logger.info("Attempting to load internal academic MCP server.")
        mcp_mgr.connect_sync(
            python_path=sys.executable,
            args=["-c", "from plugins.academic_mcp_server import mcp; mcp.run(transport='stdio')"]
        )
        logger.info("Internal academic MCP server initialized successfully.")

    except Exception as e:
        logger.error(f"Failed to start internal academic MCP server: {e}")

    # --- Phase 2: Load External CLI MCP Servers ---
    # Retrieve the external Python path defined by the user in Settings
    ext_python = config.get("external_python_path", "python")
    ext_plugins_dir = os.path.join(BASE_DIR, "plugins_ext")

    if not os.path.exists(ext_plugins_dir):
        try:
            os.makedirs(ext_plugins_dir)
            logger.info(f"Created external plugins directory at: {ext_plugins_dir}")
        except Exception as e:
            logger.error(f"Failed to create external plugins directory: {e}")
            return

    logger.info(f"Scanning for external MCP plugins in: {ext_plugins_dir}")

    for file in os.listdir(ext_plugins_dir):
        if file.endswith(".py"):
            plugin_path = os.path.join(ext_plugins_dir, file)
            try:
                logger.info(f"Attempting to load external plugin: {file} using python env: {ext_python}")
                mcp_mgr.connect_sync(script_path=plugin_path, python_path=ext_python)
                logger.info(f"External MCP plugin loaded successfully: {file}")
            except Exception as e:
                logger.error(f"Failed to load external plugin {file}. Error: {e}")


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