import os
import multiprocessing
import os
import sys
import threading
import time

from PySide6.QtCore import Qt, QThread, Signal, QObject, QTimer, Slot
from PySide6.QtGui import QIcon
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QProgressBar

from src.core.core_task import TaskManager, TaskMode
from src.task.startup_tasks import HardwareInitTask

is_compiled = getattr(sys, 'frozen', False) or '__compiled__' in globals()

if is_compiled:
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

os.environ["ANONYMIZED_TELEMETRY"] = "False"
os.environ["SCARF_NO_ANALYTICS"] = "true"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"


class StartupWorker(QThread):
    sig_progress = Signal(int, str)
    sig_finished = Signal()

    def __init__(self, logger):
        super().__init__()
        self.logger = logger
        self.hw_task_mgr = TaskManager()

    def run(self):
        try:
            self.sig_progress.emit(5, "Detecting hardware info...")
            time.sleep(0.1)

            self.sig_progress.emit(6, "Loading model registry framework...")
            time.sleep(0.1)
            from src.core.models_registry import resolve_auto_model, check_model_exists, get_model_conf, \
                ensure_onnx_model

            self.sig_progress.emit(7, "Loading user settings...")
            time.sleep(0.1)
            from src.core.network_worker import setup_global_network_env
            from src.core.config_manager import ConfigManager
            from src.core.theme_manager import ThemeManager

            self.sig_progress.emit(10, "Loading system configuration & network profiles...")
            time.sleep(0.1)
            cfg_mgr = ConfigManager()
            _ = cfg_mgr.user_settings
            setup_global_network_env()

            self.sig_progress.emit(25, "Scanning local hardware & compute engines (Background)...")
            time.sleep(0.1)
            self.hw_task_mgr.start_task(HardwareInitTask, task_id="hw_warmup", mode=TaskMode.THREAD)

            self.sig_progress.emit(40, "Mounting theme cache and UI assets...")
            time.sleep(0.1)
            tm = ThemeManager()
            _ = tm.color('bg_main')

            self.sig_progress.emit(60, "Loading MCP Subsystem metadata...")
            time.sleep(0.1)
            cfg_mgr.load_mcp_servers()

            self.sig_progress.emit(80, "Pre-loading UI components & ML libraries...")
            time.sleep(0.1)
            from src.ui.main_window import MainWindow
            from src.core.mcp_manager import MCPManager

            self.sig_progress.emit(100, "Ready. Building workspace...")
            time.sleep(0.1)
            self.sig_finished.emit()

        except Exception as e:
            self.logger.error(f"Startup error: {e}")
            self.sig_finished.emit()


class SplashScreen(QWidget):
    """Elegant academic startup screen fully integrated with ThemeManager"""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.SplashScreen)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(500, 280)

        tm = ThemeManager()

        ico_path = ThemeManager.get_resource_path("Assets", "icon.ico")
        png_path = ThemeManager.get_resource_path("Assets", "icon.png")
        if sys.platform == "win32" and os.path.exists(ico_path):
            self.setWindowIcon(QIcon(ico_path))
        else:
            self.setWindowIcon(QIcon(png_path))

        bg_color = tm.color("bg_main")
        bg_card = tm.color("bg_card")
        text_main = tm.color("title_blue")
        text_sub = tm.color("text_muted")
        border_color = tm.color("border")
        accent = tm.color("accent")
        font_family = tm.font_family()

        container = QWidget(self)
        container.setFixedSize(480, 260)
        container.move(10, 10)
        container.setStyleSheet(f"""
            QWidget {{ background-color: {bg_card}; border-radius: 12px; border: 1px solid {border_color}; font-family: {font_family}; }}
        """)

        layout = QVBoxLayout(container)
        layout.setContentsMargins(30, 30, 30, 30)

        self.logo = QSvgWidget(ThemeManager.get_resource_path("Assets", "ico.svg"))
        self.logo.setFixedSize(64, 64)
        layout.addWidget(self.logo, alignment=Qt.AlignCenter)

        self.title = QLabel("Scholar Navis")
        self.title.setStyleSheet(
            f"color: {text_main}; font-size: 26px; font-weight: bold; border: none; letter-spacing: 1px;")
        self.title.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.title)

        self.subtitle = QLabel("AI-Powered Research Assistant")
        self.subtitle.setStyleSheet(f"color: {text_sub}; font-size: 14px; border: none; font-style: italic;")
        self.subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.subtitle)

        layout.addStretch()

        self.lbl_status = QLabel("Initializing engine...")
        self.lbl_status.setStyleSheet(f"color: {text_sub}; font-size: 12px; border: none;")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_status)

        self.progress = QProgressBar()
        self.progress.setFixedHeight(4)
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet(f"""
            QProgressBar {{ background-color: {bg_color}; border: none; border-radius: 2px; }}
            QProgressBar::chunk {{ background-color: {accent}; border-radius: 2px; }}
        """)
        layout.addWidget(self.progress)


class AppController(QObject):
    def __init__(self, logger):
        super().__init__()
        self.logger = logger

        from src.core.config_manager import ConfigManager
        from src.core.theme_manager import ThemeManager
        import qdarktheme

        cfg = ConfigManager().user_settings
        theme_setting = cfg.get("theme", "Dark").lower()
        qdarktheme.setup_theme(theme_setting)
        ThemeManager().set_theme(theme_setting)

        self.splash = SplashScreen()
        self.splash.show()
        QApplication.processEvents()

        self.logger.info("System Launching.")

        self.worker = StartupWorker(self.logger)
        self.worker.sig_progress.connect(self.update_splash)
        self.worker.sig_finished.connect(self.on_startup_finished)
        self.worker.start()

    @Slot(int, str)
    def update_splash(self, val, msg):
        self.splash.progress.setValue(val)
        self.splash.lbl_status.setText(msg)

    @Slot()
    def on_startup_finished(self):
        self.splash.progress.setValue(100)
        self.splash.lbl_status.setText("Ready. Initializing workspace...")
        QApplication.processEvents()
        QTimer.singleShot(50, self._build_and_show_main_window)

    def _build_and_show_main_window(self):
        from src.ui.main_window import MainWindow
        from src.core.mcp_manager import MCPManager

        self.main_window = MainWindow()
        self.worker.deleteLater()

        self.main_window.show()
        self.main_window.raise_()
        self.main_window.activateWindow()

        self.splash.close()

        QTimer.singleShot(500, self.main_window.check_first_run)
        QTimer.singleShot(1500, lambda: MCPManager.get_instance().bootstrap_servers())

if __name__ == "__main__":

    multiprocessing.freeze_support()

    # 1. 判断启动模式
    is_admin = False
    try:
        if os.name == 'nt':
            import ctypes

            is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
        else:
            is_admin = os.geteuid() == 0
    except Exception:
        pass

    if is_admin:
        from PySide6.QtWidgets import QApplication, QMessageBox
        import sys

        temp_app = QApplication(sys.argv)
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Critical)
        msg_box.setWindowTitle("Security Alert: Elevated Privileges")
        msg_box.setText("Scholar Navis cannot be run with Administrator / Root privileges.")
        msg_box.setInformativeText(
            "For security reasons and to prevent sandbox escapes, please restart the application as a standard user.")
        msg_box.setStandardButtons(QMessageBox.Ok)
        msg_box.exec()
        sys.exit(1)

    # 1. 判断启动模式
    is_api_mode = len(sys.argv) > 1 and sys.argv[1] == "--api-server"


    # 2. 统一在最开始创建 Qt 应用实例，以支持 QLocalSocket 机制
    if is_api_mode:
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication(sys.argv)
    else:
        from PySide6.QtWidgets import QApplication
        app = QApplication(sys.argv)

    from PySide6.QtNetwork import QLocalServer, QLocalSocket

    # 3. 全局单例锁检测
    unique_server_name = "ScholarNavis_SingleInstance_Lock"
    socket = QLocalSocket()
    socket.connectToServer(unique_server_name)

    # 如果能连上服务器，说明已经有一个实例（GUI 或 API）在运行
    if socket.waitForConnected(500):
        if is_api_mode:
            print("Initialization Failed: Scholar Navis (GUI or API) is currently executing.")
        else:
            from PySide6.QtWidgets import QMessageBox

            msg_box = QMessageBox()
            msg_box.setIcon(QMessageBox.Warning)
            msg_box.setWindowTitle("Execution Alert")
            msg_box.setText("Scholar Navis is currently executing.")
            msg_box.setInformativeText(
                "The application (GUI or API) is already operating in the background. Concurrent instantiation is prohibited.")
            msg_box.setStandardButtons(QMessageBox.Ok)
            msg_box.exec()
        sys.exit(0)

    # 4. 当前无其他实例，抢占互斥锁
    local_server = QLocalServer()
    QLocalServer.removeServer(unique_server_name)
    local_server.listen(unique_server_name)

    # 5. 根据模式进入相应的启动流程
    if is_api_mode:
        os.environ["SCARF_NO_ANALYTICS"] = "true"

        from src.core.logger import setup_logger
        global_logger = setup_logger()

        from src.core.config_manager import ConfigManager
        from src.core.network_worker import setup_global_network_env
        from src.core.mcp_manager import MCPManager

        # 初始化基础环境并读取配置
        ConfigManager()
        setup_global_network_env()

        # 强制拉起 MCP 服务器
        global_logger.info("Bootstrapping MCP Servers for API mode...")
        MCPManager.get_instance().bootstrap_servers(force_all=True)

        # 启动 API 服务器
        from src.api.api_server import run_server

        run_server()
        sys.exit(0)

    else:
        import ctypes
        from src.core.logger import setup_logger

        if sys.platform == "win32":
            try:
                myappid = ctypes.c_wchar_p("scholar.navis.app")
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
            except Exception:
                pass

        global_logger = setup_logger()

        from src.core.theme_manager import ThemeManager
        from src.core.config_manager import ConfigManager
        import qdarktheme

        tm = ThemeManager()
        global_icon = tm.get_app_icon()
        app.setWindowIcon(global_icon)

        app.processEvents()

        saved_theme = ConfigManager().user_settings.get("theme", "dark").lower()
        qdarktheme.setup_theme(saved_theme)

        controller = AppController(global_logger)
        controller.splash.setWindowIcon(global_icon)

        sys.exit(app.exec())