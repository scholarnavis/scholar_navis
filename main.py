import ctypes
import os
import sys
import time

import qdarktheme
from PySide6.QtCore import Qt, QThread, Signal, QObject, QTimer, Slot
from PySide6.QtGui import QIcon
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel, QProgressBar
from src.version import __version__

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


class StartupWorker(QThread):
    sig_progress = Signal(int, str)
    sig_finished = Signal()

    def __init__(self, logger):
        super().__init__()
        self.logger = logger

    def run(self):
        try:
            self.sig_progress.emit(5, "Detecting hardware info...")
            time.sleep(0.1)
            from src.core.device_manager import DeviceManager

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

            self.sig_progress.emit(25, "Scanning local hardware & compute engines...")
            time.sleep(0.1)
            dev_mgr = DeviceManager()
            dev = dev_mgr.get_optimal_device()
            dev_str = dev.get('type', 'cpu') if isinstance(dev, dict) else str(dev)

            self.sig_progress.emit(40, "Mounting theme cache and UI assets...")
            time.sleep(0.1)
            tm = ThemeManager()
            _ = tm.color('bg_main')

            self.sig_progress.emit(60, "Loading MCP Subsystem metadata...")
            time.sleep(0.1)
            cfg_mgr.load_mcp_servers()

            self.sig_progress.emit(80, "Verifying AI Models (ONNX)...")
            time.sleep(0.1)
            missing_models = []

            try:
                from src.task.common_task import VerifyModelsTask
                from src.core.config_manager import ConfigManager

                cfg = ConfigManager()
                embed_id = cfg.user_settings.get("current_model_id", "embed_auto")
                rerank_id = cfg.user_settings.get("rerank_model_id", "rerank_auto")

                # 定义一个空的队列，用来静默吸收 Task 内部发出的进度和日志信息
                class NullQueue:
                    def put(self, item, block=True, timeout=None): pass

                task_kwargs = {
                    "embed_id": embed_id,
                    "rerank_id": rerank_id
                }

                verify_task = VerifyModelsTask(
                    task_id="startup_integrity_check",
                    task_queue=NullQueue(),
                    kwargs=task_kwargs
                )


                result = verify_task._execute()
                missing_models = result.get("to_download", [])

                if missing_models:
                    self.logger.info(f"Models to prepare: {missing_models}")

            except Exception as e:
                self.logger.error(f"Failed to verify ONNX models during startup: {e}")


            self.sig_progress.emit(85, f"Verifying Embedding model ({embed_id})...")
            time.sleep(0.5)
            e_conf = get_model_conf(embed_id, "embedding")
            if e_conf and not e_conf.get('is_network', False):
                repo_id = e_conf.get('hf_repo_id')
                if repo_id:
                    ensure_onnx_model(repo_id, "embedding")

            self.sig_progress.emit(90, f"Verifying Reranker model ({rerank_id})...")
            time.sleep(0.5)
            r_conf = get_model_conf(rerank_id, "reranker")
            if r_conf and not r_conf.get('is_network', False):
                repo_id = r_conf.get('hf_repo_id')
                if repo_id:
                    ensure_onnx_model(repo_id, "reranker")

            self.sig_progress.emit(95, "Building Main User Interface...")
            time.sleep(0.1)

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
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SplashScreen)
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
        self.title.setStyleSheet(f"color: {text_main}; font-size: 26px; font-weight: bold; border: none; letter-spacing: 1px;")
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
        # 1. 设置 100% 状态
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
        self.splash.close()

        QTimer.singleShot(300, self.main_window.perform_startup_checks)
        QTimer.singleShot(1500, lambda: MCPManager.get_instance().bootstrap_servers())

if __name__ == "__main__":

    import multiprocessing

    multiprocessing.freeze_support()
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QMessageBox


    from src.core.logger import setup_logger


    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        try:
            myappid = ctypes.c_wchar_p("scholar.navis.app")
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass

    app = QApplication(sys.argv)



    unique_server_name = "ScholarNavis_SingleInstance_Lock"
    socket = QLocalSocket()
    socket.connectToServer(unique_server_name)

    # 如果能连上服务器，说明已经有一个实例在运行
    if socket.waitForConnected(500):
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Warning)
        msg_box.setWindowTitle("Notice")
        msg_box.setText("Scholar Navis is already running.")
        msg_box.setInformativeText(
            "The application is running in the background. Please do not open multiple instances.")
        msg_box.setStandardButtons(QMessageBox.Ok)
        msg_box.exec()
        sys.exit(0)

    local_server = QLocalServer()
    QLocalServer.removeServer(unique_server_name)
    local_server.listen(unique_server_name)

    from src.core.logger import setup_logger

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