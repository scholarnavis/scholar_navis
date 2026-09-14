import os
import multiprocessing
import sys
import time
import traceback

# Qt 运行库预检必须发生在**任何 PySide6 导入之前**。Linux 上缺 X11/xkbcommon 等
# 系统库时，import 抛出的原始 ImportError 无法指导用户修复；本模块把它整理成
# 可照做的安装指引，并按运行环境注入 Chromium（QtWebEngine）启动参数。
from src.core.platform_env import (
    configure_qt_environment, format_qt_import_error, gui_display_available,
    is_shared_library_error, log_environment, maybe_relaunch_in_fhs,
    no_display_message, shared_library_hint,
)

configure_qt_environment()

try:
    from PySide6.QtCore import Qt, QThread, Signal, QObject, QTimer, Slot, QCoreApplication
    from PySide6.QtGui import QIcon
    from PySide6.QtSvgWidgets import QSvgWidget
    from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QProgressBar, QApplication, QMessageBox
    from src.core.logger import setup_logger
    from src.core.core_task import TaskManager, TaskMode
except ImportError as e:
    # NixOS：PyPI 的 PySide6 依赖标准路径下的系统库，先用 steam-run 自动接管；
    # 接管成功时进程已被替换，下面不会执行。
    if maybe_relaunch_in_fhs(e):
        sys.exit(0)
    # 退出码 3：环境不满足（区别于业务异常退出码 1）
    print(format_qt_import_error(e), file=sys.stderr)
    sys.exit(3)

is_compiled = getattr(sys, 'frozen', False) or '__compiled__' in globals()

if is_compiled:
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

os.environ["ANONYMIZED_TELEMETRY"] = "False"
os.environ["SCARF_NO_ANALYTICS"] = "true"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"


def global_exception_handler(exc_type, exc_value, exc_traceback):
    """拦截全局未捕获异常并弹窗显示"""
    # 忽略键盘中断
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    tb_str = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))

    if 'global_logger' in globals():
        try:
            global_logger.error(f"Uncaught Exception:\n{tb_str}")
        except Exception as e:
            # 崩溃处理器内日志失败时降级输出到 stderr
            print(f"CRITICAL: Failed to log uncaught exception: {e}", file=sys.stderr)

    try:
        # 检查是否为 API 模式（非 GUI），如果是则只打印终端
        app = QCoreApplication.instance()
        if app and not isinstance(app, QApplication):
            print(f"CRITICAL ERROR (API Mode): {exc_value}\n{tb_str}", file=sys.stderr)
            sys.exit(1)

        # 确保存在 QApplication 以便弹窗
        if not app:
            app = QApplication(sys.argv)

        msg = QMessageBox()
        msg.setIcon(QMessageBox.Critical)
        msg.setWindowTitle("Application Crash")
        msg.setText("Scholar Navis encountered a fatal error and must close.")

        # 针对动态库缺失的专门提示（Windows 为 DLL，Linux 为 .so，macOS 为 dyld）
        if is_shared_library_error(exc_value):
            msg.setInformativeText(
                f"{shared_library_hint(exc_value)}\n\nError: {exc_value}")
        else:
            msg.setInformativeText(str(exc_value))

        msg.setDetailedText(tb_str)
        msg.exec()
    except Exception as e:
        # 崩溃弹窗失败时记录原因并回退到系统默认处理器
        print(f"CRITICAL: Crash dialog failed: {e}", file=sys.stderr)
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    sys.exit(1)


sys.excepthook = global_exception_handler



class StartupWorker(QThread):
    sig_progress = Signal(int, str)
    sig_finished = Signal()
    sig_error = Signal(str, str)
    # 硬件预热任务必须由主线程发起：TaskManager 内部使用 QTimer，在工作线程里
    # start_task 会触发 "QObject::startTimer: Timers cannot be started from
    # another thread"，定时器不启动则任务结果无法回传。
    sig_start_hw_warmup = Signal()

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
            # 交给主线程发起（见 sig_start_hw_warmup 的说明）
            self.sig_start_hw_warmup.emit()

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
            tb_str = traceback.format_exc()
            self.logger.error(f"Startup error: {e}\n{tb_str}")
            self.sig_error.emit(str(e), tb_str)


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

        cfg = ConfigManager().user_settings
        theme_setting = cfg.get("theme", "Dark").lower()
        # 统一入口：基础样式(Fusion) + qdarktheme 样式表 + 同源调色板
        ThemeManager().apply_application_theme(theme_setting)

        self.splash = SplashScreen()
        self.splash.show()
        QApplication.processEvents()

        self.logger.info("System Launching.")

        self.worker = StartupWorker(self.logger)
        self.worker.sig_progress.connect(self.update_splash)
        self.worker.sig_finished.connect(self.on_startup_finished)
        self.worker.sig_error.connect(self.on_startup_error)  # 绑定错误信号
        self.worker.sig_start_hw_warmup.connect(self.start_hw_warmup)
        self.worker.start()

    @Slot()
    def start_hw_warmup(self):
        """在主线程发起硬件预热任务（TaskManager 的 QTimer 有线程亲和性要求）。"""
        from src.task.startup_tasks import HardwareInitTask

        self.worker.hw_task_mgr.start_task(
            HardwareInitTask, task_id="hw_warmup", mode=TaskMode.THREAD)

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

    @Slot(str, str)
    def on_startup_error(self, err_msg, tb_str):
        """处理启动线程中的异常错误"""
        self.splash.hide()
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Critical)
        msg.setWindowTitle("Startup Error")

        if is_shared_library_error(err_msg):
            msg.setText("A required system library is missing.")
            msg.setInformativeText(f"{shared_library_hint(err_msg)}\n\n{err_msg}")
        else:
            msg.setText("Application failed to initialize.")
            msg.setInformativeText(err_msg)
        msg.setDetailedText(tb_str)
        msg.exec()
        sys.exit(1)

    def _build_and_show_main_window(self):
        # 这里的异常会被全局钩子 global_exception_handler 自动接管
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

    try:
        multiprocessing.set_start_method('spawn', force=True)
    except RuntimeError:
        pass

    # 1. 判断启动模式
    is_api_mode = len(sys.argv) > 1 and sys.argv[1] == "--api-server"

    # 2. 图形会话预检（仅 GUI 模式）：无 X11/Wayland 会话时给出可操作提示，
    #    而不是让 Qt 以原生错误码中断在用户读不到的位置（SSH、纯终端、容器）。
    has_display = gui_display_available()
    if not is_api_mode and not has_display:
        print(no_display_message(), file=sys.stderr)
        sys.exit(3)

    def _fatal_message(title, text, detail=""):
        """致命提示：可用界面则弹窗，否则写 stderr（API 模式/无显示会话）。"""
        print(f"{title}: {text}" + (f"\n{detail}" if detail else ""), file=sys.stderr)
        if is_api_mode or not has_display:
            return
        try:
            QApplication.instance() or QApplication(sys.argv)
            box = QMessageBox()
            box.setIcon(QMessageBox.Critical)
            box.setWindowTitle(title)
            box.setText(text)
            if detail:
                box.setInformativeText(detail)
            box.setStandardButtons(QMessageBox.Ok)
            box.exec()
        except Exception as e:
            print(f"CRITICAL: failed to display message box: {e}", file=sys.stderr)

    # 3. 提权检测
    is_admin = False
    try:
        if os.name == 'nt':
            import ctypes

            is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
        else:
            is_admin = os.geteuid() == 0
    except (ImportError, OSError, AttributeError):
        pass

    if is_admin:
        _fatal_message(
            "Security Alert: Elevated Privileges",
            "Scholar Navis cannot be run with Administrator / Root privileges.",
            "For security reasons and to prevent sandbox escapes, "
            "please restart the application as a standard user.")
        sys.exit(1)

    # 4. 统一在最开始创建 Qt 应用实例
    app = QCoreApplication(sys.argv) if is_api_mode else QApplication(sys.argv)

    from PySide6.QtNetwork import QLocalServer, QLocalSocket

    # 5. 全局单例锁检测
    unique_server_name = "ScholarNavis_SingleInstance_Lock"
    socket = QLocalSocket()
    socket.connectToServer(unique_server_name)

    if socket.waitForConnected(500):
        if is_api_mode:
            print("Initialization Failed: Scholar Navis (GUI or API) is currently executing.")
        else:
            msg_box = QMessageBox()
            msg_box.setIcon(QMessageBox.Warning)
            msg_box.setWindowTitle("Execution Alert")
            msg_box.setText("Scholar Navis is currently executing.")
            msg_box.setInformativeText(
                "The application (GUI or API) is already operating in the background. Concurrent instantiation is prohibited.")
            msg_box.setStandardButtons(QMessageBox.Ok)
            msg_box.exec()
        sys.exit(0)

    # 6. 当前无其他实例，抢占互斥锁
    local_server = QLocalServer()
    QLocalServer.removeServer(unique_server_name)
    local_server.listen(unique_server_name)

    # 7. 根据模式进入相应的启动流程
    if is_api_mode:
        os.environ["SCARF_NO_ANALYTICS"] = "true"


        global_logger = setup_logger()
        log_environment()

        from src.core.config_manager import ConfigManager
        from src.core.network_worker import setup_global_network_env
        from src.core.mcp_manager import MCPManager

        ConfigManager()
        setup_global_network_env()

        global_logger.info("Bootstrapping MCP Servers for API mode...")
        MCPManager.get_instance().bootstrap_servers(force_all=True)

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
            except (OSError, AttributeError):
                pass

        global_logger = setup_logger()
        log_environment()

        from src.core.theme_manager import ThemeManager
        from src.core.config_manager import ConfigManager

        tm = ThemeManager()
        global_icon = tm.get_app_icon()
        app.setWindowIcon(global_icon)

        app.processEvents()

        saved_theme = ConfigManager().user_settings.get("theme", "dark").lower()
        tm.apply_application_theme(saved_theme)

        controller = AppController(global_logger)
        controller.splash.setWindowIcon(global_icon)

        sys.exit(app.exec())