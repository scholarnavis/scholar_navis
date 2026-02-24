import os
import sys
import time

from PySide6.QtCore import Qt, QThread, Signal, QObject
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel, QProgressBar, QGraphicsDropShadowEffect

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


class StartupWorker(QObject):
    """后台启动任务，防止卡死主 UI 线程"""
    sig_progress = Signal(int, str)
    sig_finished = Signal()

    def __init__(self, logger):
        super().__init__()
        self.logger = logger

    def run(self):
        try:
            self.sig_progress.emit(10, "Applying network configurations...")
            setup_global_network_env()
            time.sleep(0.3)  # 仅为视觉过渡顺滑

            self.sig_progress.emit(30, "Initializing core MCP subsystem...")
            mcp_mgr = MCPManager.get_instance()
            config = ConfigManager().user_settings

            os.environ["NCBI_API_EMAIL"] = config.get("ncbi_email", "scholar.navis@example.com")
            os.environ["NCBI_API_KEY"] = config.get("ncbi_api_key", "")
            os.environ["S2_API_KEY"] = config.get("s2_api_key", "")

            self.logger.info("Attempting to load internal academic MCP server.")
            try:
                mcp_mgr.connect_sync(
                    python_path=sys.executable,
                    args=["-c", "from plugins.academic_mcp_server import mcp; mcp.run(transport='stdio')"]
                )
                self.logger.info("Internal academic MCP server initialized successfully.")
            except Exception as e:
                self.logger.error(f"Failed to start internal academic MCP server: {e}")

            self.sig_progress.emit(70, "Loading external MCP bridge...")
            ext_python = config.get("external_python_path", "python")
            ext_plugins_dir = os.path.join(BASE_DIR, "plugins_ext")

            if not os.path.exists(ext_plugins_dir):
                os.makedirs(ext_plugins_dir, exist_ok=True)

            bridge_script = os.path.join(ext_plugins_dir, "external_bridge.py")
            if os.path.exists(bridge_script):
                try:
                    mcp_mgr.connect_sync(script_path=bridge_script, python_path=ext_python)
                    self.logger.info("External MCP bridge loaded successfully.")
                except Exception as e:
                    self.logger.error(f"Failed to load external bridge: {e}")
            else:
                self.logger.info("No external_bridge.py found. Running with core tools only.")

            self.sig_progress.emit(95, "Building User Interface...")
            time.sleep(0.3)

            self.sig_finished.emit()
        except Exception as e:
            self.logger.error(f"Startup error: {e}")
            self.sig_finished.emit()


class SplashScreen(QWidget):
    """精美的学术风启动界面"""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(480, 260)

        # 阴影效果
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setXOffset(0)
        shadow.setYOffset(4)
        shadow.setColor(QColor(0, 0, 0, 160))
        self.setGraphicsEffect(shadow)

        # 主容器
        container = QWidget(self)
        container.setFixedSize(460, 240)
        container.move(10, 10)
        container.setStyleSheet("""
            QWidget {
                background-color: #1e1e1e;
                border-radius: 12px;
                border: 1px solid #333333;
            }
        """)

        layout = QVBoxLayout(container)
        layout.setContentsMargins(30, 40, 30, 30)

        # Logo / Title
        self.title = QLabel("🧠 Scholar Navis")
        self.title.setStyleSheet("""
            QLabel {
                color: #05B8CC;
                font-size: 28px;
                font-weight: bold;
                font-family: 'Segoe UI', 'Microsoft YaHei';
                border: none;
            }
        """)
        self.title.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.title)

        self.subtitle = QLabel("AI-Powered Research Assistant")
        self.subtitle.setStyleSheet("color: #888888; font-size: 13px; border: none;")
        self.subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.subtitle)

        layout.addStretch()

        # 状态文本
        self.lbl_status = QLabel("Initializing engine...")
        self.lbl_status.setStyleSheet("color: #cccccc; font-size: 12px; border: none;")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_status)

        # 进度条
        self.progress = QProgressBar()
        self.progress.setFixedHeight(4)
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet("""
            QProgressBar {
                background-color: #2b2b2b;
                border: none;
                border-radius: 2px;
            }
            QProgressBar::chunk {
                background-color: #05B8CC;
                border-radius: 2px;
            }
        """)
        layout.addWidget(self.progress)


class AppController(QObject):
    def __init__(self):
        super().__init__()
        self.logger = setup_logger()
        self.logger.info("System Launching.")

        # 1. 显示启动屏
        self.splash = SplashScreen()
        self.splash.show()

        # 2. 准备后台工作线程
        self.thread = QThread()
        self.worker = StartupWorker(self.logger)
        self.worker.moveToThread(self.thread)

        self.worker.sig_progress.connect(self.update_splash)
        self.worker.sig_finished.connect(self.on_startup_finished)
        self.thread.started.connect(self.worker.run)

        # 开始后台加载
        self.thread.start()

    def update_splash(self, val, msg):
        self.splash.progress.setValue(val)
        self.splash.lbl_status.setText(msg)

    def on_startup_finished(self):
        self.splash.progress.setValue(100)
        self.splash.lbl_status.setText("Ready.")

        # 3. 后台加载完毕，初始化主窗口 (主窗口必须在主线程创建)
        self.main_window = MainWindow()

        # 清理线程
        self.thread.quit()
        self.thread.wait()
        self.worker.deleteLater()
        self.thread.deleteLater()

        # 4. 切换窗口
        self.main_window.show()
        self.splash.close()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    controller = AppController()
    sys.exit(app.exec())