import ctypes
import os
import sys
import time

import qdarktheme
from PySide6.QtCore import Qt, QThread, Signal, QObject, QTimer, Slot
from PySide6.QtGui import QIcon
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel, QProgressBar

from src.core.device_manager import DeviceManager
from src.core.mcp_manager import MCPManager
from src.core.models_registry import resolve_auto_model, check_model_exists, get_model_conf, ensure_onnx_model
from src.core.network_worker import setup_global_network_env
from src.ui.main_window import MainWindow
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

            self.sig_progress.emit(55, "Connecting to local knowledge base & logs...")
            time.sleep(0.1)

            self.sig_progress.emit(70, "Loading MCP Subsystem metadata...")
            time.sleep(0.1)
            cfg_mgr.load_mcp_servers()

            self.sig_progress.emit(85, "Checking AI models & local cache integrity...")
            time.sleep(0.1)
            embed_id = cfg_mgr.user_settings.get("current_model_id", "embed_auto")
            rerank_id = cfg_mgr.user_settings.get("rerank_model_id", "rerank_auto")

            if embed_id == "embed_auto":
                embed_id = resolve_auto_model("embedding", dev_str)
            if rerank_id == "rerank_auto":
                rerank_id = resolve_auto_model("reranker", dev_str)

            for mid, mtype in [(embed_id, "embedding"), (rerank_id, "reranker")]:
                conf = get_model_conf(mid, mtype)
                if conf and not conf.get('is_network', False):
                    repo_id = conf.get('hf_repo_id')
                    if repo_id and check_model_exists(repo_id):
                        ensure_onnx_model(repo_id, mtype)

            self.sig_progress.emit(95, "Building Main User Interface...")
            time.sleep(0.1)

            self.sig_progress.emit(100, "Ready.")
            time.sleep(0.1)

            self.sig_finished.emit()

        except Exception as e:
            self.logger.error(f"Startup error: {e}")
            self.sig_finished.emit()


class SplashScreen(QWidget):
    """Elegant academic startup screen fully integrated with ThemeManager"""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.SplashScreen  # <-- 加上这个
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(500, 280)

        tm = ThemeManager()

        icon_path = ThemeManager.get_resource_path("Assets", "ico.svg")
        self.setWindowIcon(QIcon(icon_path))

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
        self.splash.progress.setValue(100)
        self.splash.lbl_status.setText("Ready.")

        self.main_window = MainWindow()

        self.worker.deleteLater()

        self.main_window.show()
        self.splash.close()

        QTimer.singleShot(1500, lambda: MCPManager.get_instance().bootstrap_servers())


if __name__ == "__main__":
    from src.core.logger import setup_logger

    global_logger = setup_logger()

    if sys.platform == "win32":
        myappid = f"scholarnavis.studio.navigator.{__version__}222222222222222222222222222222"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(myappid))

    app = QApplication(sys.argv)

    from src.core.theme_manager import ThemeManager
    from src.core.config_manager import ConfigManager
    import qdarktheme

    tm = ThemeManager()
    ico_path = os.path.abspath(tm.get_resource_path("Assets", "ico.ico"))

    if sys.platform == "win32" and os.path.exists(ico_path):
        global_icon = QIcon(ico_path)
    else:
        global_logger.warning(f"Could not find valid .ico at {ico_path}, taskbar icon may fail.")
        global_icon = tm.get_app_icon()

    app.setWindowIcon(global_icon)

    app.processEvents()

    saved_theme = ConfigManager().user_settings.get("theme", "dark").lower()
    qdarktheme.setup_theme(saved_theme)

    controller = AppController(global_logger)
    controller.splash.setWindowIcon(global_icon)

    sys.exit(app.exec())