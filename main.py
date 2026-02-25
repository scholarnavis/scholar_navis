import os
import sys
import time

from PySide6.QtCore import Qt, QThread, Signal, QObject, QTimer
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel, QProgressBar, QGraphicsDropShadowEffect

from src.core.mcp_manager import MCPManager
from src.core.network_worker import setup_global_network_env
from src.core.config_manager import ConfigManager
from src.core.logger import setup_logger
from src.ui.main_window import MainWindow


# 拦截来自 MCPManager 的子进程唤起请求，防止主 UI 被无限循环启动
if len(sys.argv) > 1 and sys.argv[1] == "--run-builtin-mcp":
    os.environ["SCARF_NO_ANALYTICS"] = "true"
    from plugins.academic_mcp_server import mcp
    mcp.run(transport='stdio')
    sys.exit(0)


is_compiled = getattr(sys, 'frozen', False) or '__compiled__' in globals()

if is_compiled:
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
            # Step 1: 基础环境准备
            self.sig_progress.emit(10, "Initializing environment...")
            time.sleep(0.1)

            # Step 2: 统一配置加载
            self.sig_progress.emit(30, "Loading unified system configuration...")
            ConfigManager()
            setup_global_network_env()
            time.sleep(0.1)

            # Step 3: MCP 懒加载准备 (不再这里执行耗时连接)
            self.sig_progress.emit(60, "Preparing MCP Subsystems (Lazy Mode)...")
            time.sleep(0.1)

            # Step 4: 构建主界面
            self.sig_progress.emit(90, "Building User Interface...")
            time.sleep(0.1)

            # 结束
            self.sig_progress.emit(100, "Ready.")
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

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setXOffset(0)
        shadow.setYOffset(4)
        shadow.setColor(QColor(0, 0, 0, 160))
        self.setGraphicsEffect(shadow)

        container = QWidget(self)
        container.setFixedSize(460, 240)
        container.move(10, 10)
        container.setStyleSheet("""
            QWidget { background-color: #1e1e1e; border-radius: 12px; border: 1px solid #333333; }
        """)

        layout = QVBoxLayout(container)
        layout.setContentsMargins(30, 40, 30, 30)

        self.title = QLabel("🧠 Scholar Navis")
        self.title.setStyleSheet("""
            QLabel { color: #05B8CC; font-size: 28px; font-weight: bold; font-family: 'Segoe UI', 'Microsoft YaHei'; border: none; }
        """)
        self.title.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.title)

        self.subtitle = QLabel("AI-Powered Research Assistant")
        self.subtitle.setStyleSheet("color: #888888; font-size: 13px; border: none;")
        self.subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.subtitle)

        layout.addStretch()

        self.lbl_status = QLabel("Initializing engine...")
        self.lbl_status.setStyleSheet("color: #cccccc; font-size: 12px; border: none;")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_status)

        self.progress = QProgressBar()
        self.progress.setFixedHeight(4)
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet("""
            QProgressBar { background-color: #2b2b2b; border: none; border-radius: 2px; }
            QProgressBar::chunk { background-color: #05B8CC; border-radius: 2px; }
        """)
        layout.addWidget(self.progress)


class AppController(QObject):
    def __init__(self):
        super().__init__()

        self.splash = SplashScreen()
        self.splash.show()
        QApplication.processEvents()

        self.logger = setup_logger()
        self.logger.info("System Launching.")

        self.thread = QThread()
        self.worker = StartupWorker(self.logger)
        self.worker.moveToThread(self.thread)

        self.worker.sig_progress.connect(self.update_splash)
        self.worker.sig_finished.connect(self.on_startup_finished)
        self.thread.started.connect(self.worker.run)

        self.thread.start()

    def update_splash(self, val, msg):
        self.splash.progress.setValue(val)
        self.splash.lbl_status.setText(msg)

    def on_startup_finished(self):
        self.splash.progress.setValue(100)
        self.splash.lbl_status.setText("Ready.")

        self.main_window = MainWindow()

        self.thread.quit()
        self.thread.wait()
        self.worker.deleteLater()
        self.thread.deleteLater()

        self.main_window.show()
        self.splash.close()

        QTimer.singleShot(1500, lambda: MCPManager.get_instance().bootstrap_servers())


if __name__ == "__main__":
    app = QApplication(sys.argv)
    controller = AppController()
    sys.exit(app.exec())