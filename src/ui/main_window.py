import ctypes
import os
import sys

from PySide6.QtCore import Qt, QSize, QTimer, QEvent
from PySide6.QtGui import QShortcut, QKeySequence, QIcon
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QListWidget,
                               QStackedWidget, QSplitter, QPushButton, QLabel, QHBoxLayout, QListWidgetItem,
                               QApplication)

from src.core.config_manager import ConfigManager
from src.core.core_task import TaskManager, TaskMode
from src.core.mcp_manager import MCPManager
from src.core.theme_manager import ThemeManager
from src.task.common_task import VerifyModelsTask
from src.tools.about_tool import AboutTool
from src.tools.chat_tool import ChatTool
from src.tools.import_tool import ImportTool
from src.tools.log_tool import LogTool
from src.tools.rss_tool import RSSTool
from src.tools.settings_tool import SettingsTool
from src.ui.components.dialog import StandardDialog, BaseDialog
from src.ui.components.quick_translator import QuickTranslatorWindow
from src.ui.components.toast import ToastManager


def force_windows_taskbar_icon(hwnd, icon_path):
    if sys.platform != "win32":
        return
    if not os.path.exists(icon_path):
        return
    try:

        hIcon_small = ctypes.windll.user32.LoadImageW(
            0, os.path.abspath(icon_path), 1, 16, 16, 0x0010
        )
        if hIcon_small:
            ctypes.windll.user32.SendMessageW(int(hwnd), 0x0080, 0, hIcon_small)

        hIcon_big = ctypes.windll.user32.LoadImageW(
            0, os.path.abspath(icon_path), 1, 256, 256, 0x0010
        )
        if not hIcon_big:
            hIcon_big = ctypes.windll.user32.LoadImageW(
                0, os.path.abspath(icon_path), 1, 128, 128, 0x0010
            )
        if not hIcon_big:
            hIcon_big = ctypes.windll.user32.LoadImageW(
                0, os.path.abspath(icon_path), 1, 64, 64, 0x0010
            )

        if hIcon_big:
            ctypes.windll.user32.SendMessageW(int(hwnd), 0x0080, 1, hIcon_big)

    except Exception:
        pass


def set_window_titlebar_theme(hwnd, is_dark: bool):
    if sys.platform == "win32":
        try:
            hwnd_int = int(hwnd)
            value = ctypes.c_int(1 if is_dark else 0)

            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd_int, 20, ctypes.byref(value), 4)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd_int, 19, ctypes.byref(value), 4)

            ctypes.windll.user32.SetWindowPos(hwnd_int, 0, 0, 0, 0, 0, 0x0037)

            ctypes.windll.user32.SendMessageW(hwnd_int, 0x0086, 0, 0)
            ctypes.windll.user32.SendMessageW(hwnd_int, 0x0086, 1, 0)

            ctypes.windll.user32.RedrawWindow(hwnd_int, None, None, 0x0400 | 0x0100 | 0x0001)
        except Exception:
            pass


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Scholar Navis - Research Assistant")
        self.resize(1280, 800)

        self.setWindowIcon(ThemeManager().get_app_icon())

        ToastManager().set_parent(self)

        # 主分割器
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setHandleWidth(1)
        self.setCentralWidget(self.main_splitter)

        # --- 左侧面板 ---
        self.left_panel = QWidget()
        self.left_panel.setFixedWidth(220)

        left_layout = QVBoxLayout(self.left_panel)
        left_layout.setContentsMargins(10, 15, 10, 15)
        left_layout.setSpacing(10)

        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(5, 0, 5, 0)

        self.logo_widget = QSvgWidget(ThemeManager.get_resource_path("Assets", "ico.svg"))
        self.logo_widget.setFixedSize(36, 36)

        self.lbl_app_name = QLabel("Scholar Navis")
        self.lbl_app_name.setStyleSheet("font-weight: bold; font-size: 16px;")

        top_bar.addWidget(self.logo_widget)
        top_bar.addWidget(self.lbl_app_name, stretch=1)
        left_layout.addLayout(top_bar)

        # 导航栏
        self.sidebar = QListWidget()
        self.sidebar.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sidebar.currentRowChanged.connect(self.switch_tool)
        left_layout.addWidget(self.sidebar)

        # 左下角翻译按钮 (要求：圆形底纹，学术蓝)
        self.btn_quick_trans = QPushButton()
        self.btn_quick_trans.setToolTip("Quick Translate (Ctrl+Shift+T)")
        self.btn_quick_trans.setCursor(Qt.PointingHandCursor)
        self.btn_quick_trans.setFixedSize(48, 48)  # 完美的圆形尺寸
        self.btn_quick_trans.clicked.connect(self.toggle_quick_translator)

        # 包裹在一个布局里使其居中或靠左不拉伸
        trans_layout = QHBoxLayout()
        trans_layout.addWidget(self.btn_quick_trans)
        trans_layout.addStretch()
        left_layout.addLayout(trans_layout)

        self.main_splitter.addWidget(self.left_panel)

        # --- 右侧主面板 ---
        self.tool_stack = QStackedWidget()
        self.main_splitter.addWidget(self.tool_stack)

        # 锁定分割线不可拖拽，纯靠代码控制折叠（更符合现代UI逻辑）
        self.main_splitter.setCollapsible(0, False)
        self.main_splitter.setCollapsible(1, False)
        handle = self.main_splitter.handle(1)
        handle.setDisabled(True)

        self.tools =[]

        self.tm = ThemeManager()
        self.icon_map = {
            "Library Manager": "folder",
            "Chat Assistant": "send",
            "Literature Tracker": "rss",
            "System Logs": "list",
            "Global Settings": "settings",
            "About": "info"
        }

        # 注册所有工具
        self.add_tool(ImportTool())
        QApplication.processEvents()

        self.add_tool(ChatTool())
        QApplication.processEvents()

        self.add_tool(RSSTool())
        QApplication.processEvents()

        self.add_tool(SettingsTool())
        QApplication.processEvents()

        self.add_tool(LogTool())
        QApplication.processEvents()

        self.add_tool(AboutTool())
        self.sidebar.setCurrentRow(0)
        self.clean_old_logs()
        self._setup_mcp_status_bar()
        QTimer.singleShot(300, self.perform_startup_checks)

        self.translator_dialog = QuickTranslatorWindow(None)
        self.shortcut_translate = QShortcut(QKeySequence("Ctrl+Shift+T"), self)
        self.shortcut_translate.activated.connect(self.toggle_quick_translator)

        self.tm.theme_changed.connect(self._apply_theme)
        self._apply_theme()

        if sys.platform == "win32":
            ico_path = ThemeManager.get_resource_path("Assets", "icon.ico")
            hwnd = int(self.winId())
            QTimer.singleShot(100, lambda: force_windows_taskbar_icon(hwnd, ico_path))

    def toggle_quick_translator(self):
        if self.translator_dialog.isHidden() or self.translator_dialog.windowOpacity() == 0.0:
            self.translator_dialog.setWindowOpacity(1.0)
            self.translator_dialog.show()
            self.translator_dialog.activateWindow()
            self.translator_dialog.input_box.setFocus()
        else:
            self.translator_dialog.hide_with_fade()

    def _update_logo_theme(self):
        theme = ConfigManager().user_settings.get("theme", "Dark").lower()
        filename = "logo_light.svg" if theme == "light" else "logo_dark.svg"

        logo_path = ThemeManager.get_resource_path("Assets", filename)

        if os.path.exists(logo_path):
            self.logo_widget.load(logo_path)

    def _apply_theme(self):
        tm = self.tm
        is_dark = tm.current_theme == "dark"

        hwnd = int(self.winId())
        QTimer.singleShot(100, lambda: set_window_titlebar_theme(hwnd, is_dark))

        self.setStyleSheet(f"QMainWindow {{ background-color: {tm.color('bg_main')}; }}")
        self.main_splitter.setStyleSheet(f"QSplitter::handle {{ background-color: {tm.color('border')}; }}")
        self.tool_stack.setStyleSheet(f"background-color: {tm.color('bg_main')};")

        for i in range(self.sidebar.count()):
            item = self.sidebar.item(i)
            tool_name = self.tools[i].tool_name
            icon_name = self.icon_map.get(tool_name, "tag")
            item.setIcon(tm.icon(icon_name, "text_main"))

        self.sidebar.setStyleSheet(f"""
            QListWidget {{ 
                border: none; 
                background-color: transparent; 
                font-family: 'Segoe UI', sans-serif;
                font-size: 14px; 
                outline: none; 
            }}
            QListWidget::item {{ 
                padding: 10px 8px; 
                color: {tm.color('text_muted')};
                border-radius: 6px;
                margin-bottom: 4px;
            }}
            QListWidget::item:selected {{ 
                background-color: {tm.color('btn_bg')}; 
                color: {tm.color('text_main')}; 
                font-weight: bold;
            }}
            QListWidget::item:hover:!selected {{ 
                background-color: {tm.color('btn_hover')}; 
            }}
        """)

        self.lbl_app_name.setStyleSheet(f"color: {tm.color('title_blue')}; font-weight: bold; font-size: 16px;")

        self.btn_quick_trans.setIcon(tm.icon("translate", "bg_main"))
        self.btn_quick_trans.setIconSize(QSize(22, 22))
        self.btn_quick_trans.setStyleSheet(f"""
            QPushButton {{ 
                background-color: {tm.color('accent')}; 
                border: 2px solid {tm.color('border')};
                border-radius: 24px;
            }}
            QPushButton:hover {{ 
                background-color: {tm.color('accent_hover')}; 
            }}
            QPushButton:pressed {{ 
                background-color: {tm.color('title_blue')}; 
            }}
        """)

    def _setup_mcp_status_bar(self):
        status_widget = QWidget()
        status_layout = QHBoxLayout(status_widget)
        status_layout.setContentsMargins(5, 2, 15, 2)
        status_layout.addStretch()

        status_layout.addWidget(QLabel("MCP Servers:"))

        # 内置MCP状态
        builtin_status = QLabel("Built-in: Running")
        builtin_status.setStyleSheet("color: #4caf50; font-weight: bold;")
        status_layout.addWidget(builtin_status)

        status_layout.addSpacing(10)

        # 网络/自定义 MCP 状态
        self.custom_status_label = QLabel("Custom: 0 Active")
        self.custom_status_label.setStyleSheet("color: #888;")
        status_layout.addWidget(self.custom_status_label)

        self.statusBar().addPermanentWidget(status_widget)

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._update_mcp_status)
        self.status_timer.start(5000)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            is_dark = self.tm.current_theme == "dark"
            QTimer.singleShot(10, lambda: set_window_titlebar_theme(self.winId(), is_dark))


    def _update_mcp_status(self):
        mcp_mgr = MCPManager.get_instance()
        config_mgr = ConfigManager()
        mcp_config = config_mgr.mcp_servers.get("mcpServers", {})

        connected_count = 0
        starting_count = 0
        error_count = 0

        for name, cfg in mcp_config.items():
            if name != "builtin":
                if cfg.get("enabled", False) or cfg.get("always_on", False):
                    status = mcp_mgr.get_server_status(name)
                    if status == "connected":
                        connected_count += 1
                    elif status in ["starting", "connecting"]:
                        starting_count += 1
                    elif "error" in status:
                        error_count += 1

        if starting_count > 0:
            self.custom_status_label.setText(f"Custom: {starting_count} Starting...")
            self.custom_status_label.setStyleSheet("color: #ffb86c; font-weight: bold;")
        elif error_count > 0:
            self.custom_status_label.setText(f"Custom: {connected_count} Active, {error_count} Error")
            self.custom_status_label.setStyleSheet("color: #ff6b6b; font-weight: bold;")
        elif connected_count > 0:
            self.custom_status_label.setText(f"Custom: {connected_count} Active")
            self.custom_status_label.setStyleSheet("color: #4caf50; font-weight: bold;")
        else:
            self.custom_status_label.setText("Custom: 0 Active")
            self.custom_status_label.setStyleSheet("color: #888;")

    def clean_old_logs(self):
        base_dir = ThemeManager.get_resource_path()
        log_dirs =[
            os.path.join(base_dir, "logs"),
            os.path.join(base_dir, "logs", "mcp", "academic"),
            os.path.join(base_dir, "logs", "mcp", "common")
        ]

        for d in log_dirs:
            if not os.path.exists(d):
                continue

            logs =[os.path.join(d, f) for f in os.listdir(d) if ".log" in f]
            logs.sort(key=os.path.getmtime)

            if len(logs) > 30:
                for old_log in logs[:-30]:
                    try:
                        os.remove(old_log)
                    except Exception:
                        pass

    def perform_startup_checks(self):
        is_first = self.check_first_run()
        if not is_first:
            self.check_model_integrity()

    def add_tool(self, tool):
        self.tools.append(tool)
        widget = tool.get_ui_widget()
        self.tool_stack.addWidget(widget)

        icon_name = self.icon_map.get(tool.tool_name, "tag")
        item = QListWidgetItem(self.tm.icon(icon_name, "text_muted"), f"  {tool.tool_name}")
        self.sidebar.addItem(item)

    def switch_tool(self, index):
        self.tool_stack.setCurrentIndex(index)

    def check_model_integrity(self):
        cfg = ConfigManager()

        embed_id = cfg.user_settings.get("current_model_id", "embed_auto")
        rerank_id = cfg.user_settings.get("rerank_model_id", "rerank_auto")

        self.integrity_task_mgr = TaskManager()
        self.integrity_task_mgr.sig_result.connect(self._on_integrity_check_result)

        self.integrity_task_mgr.start_task(
            VerifyModelsTask,
            task_id="startup_integrity_check",
            mode=TaskMode.THREAD,
            embed_id=embed_id,
            rerank_id=rerank_id
        )

    def _on_integrity_check_result(self, result):
        to_download = result.get("to_download",[])

        if to_download:
            names = "<br>".join([f"• {repo}" for repo in to_download])
            msg = (
                f"<b>Model Check</b><br><br>"
                f"The system cannot verify the following models:<br>{names}<br><br>"
                f"Please go to <b>Global Settings</b> to verify the path or download them."
            )
            dlg = StandardDialog(self, "System Check", msg)
            dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowStaysOnTopHint)
            dlg.exec()
            self._jump_to_settings()

    def _jump_to_settings(self):
        self.switch_tool(3)
        self.sidebar.setCurrentRow(3)

    def check_first_run(self):
        cfg = ConfigManager()
        if cfg.user_settings.get("is_first_run", True):

            tm = ThemeManager()

            dlg = BaseDialog(self, title="Welcome to Scholar Navis", width=580)
            dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowStaysOnTopHint)
            dlg.footer_widget.setVisible(False)

            welcome_html = f"""
                        <div style="font-family: {tm.font_family()}; font-size: 14px; color: {tm.color('text_main')}; line-height: 1.6;">
                            <h2 style="color: {tm.color('title_blue')}; margin-top: 5px; margin-bottom: 12px; font-weight: bold; letter-spacing: 0.5px;">
                                Your AI-Powered Research Assistant
                            </h2>
                            <p style="margin-top: 0; color: {tm.color('text_main')};">
                                Welcome! Scholar Navis is designed to streamline your literature review, data analysis, and academic writing processes.
                            </p>

                            <div style="background-color: {tm.color('bg_input')}; border-left: 4px solid {tm.color('warning')}; padding: 12px 16px; margin: 15px 0; border-radius: 4px;">
                                <b style="color: {tm.color('warning')}; font-size: 14px;">🌐 Global Network Connectivity</b><br>
                                <span style="font-size: 13px; color: {tm.color('text_muted')}; display: inline-block; margin-top: 4px;">
                                To fully utilize AI models, fetch literature, and track global preprints, please ensure your device has unrestricted access to the global internet for the best possible experience.
                                </span>
                            </div>

                            <div style="background-color: {tm.color('bg_input')}; border-left: 4px solid {tm.color('danger')}; padding: 12px 16px; margin: 15px 0; border-radius: 4px;">
                                <b style="color: {tm.color('danger')}; font-size: 14px;">🛡️ Security Notice</b><br>
                                <span style="font-size: 13px; color: {tm.color('text_muted')}; display: inline-block; margin-top: 4px;">
                                To ensure the absolute security of your academic data and system integrity, please make sure you are using a version downloaded directly from our official website. We are not responsible for any security breaches caused by unauthorized third-party distributions.
                                </span>
                            </div>

                            <p style="font-size: 13px; color: {tm.color('text_muted')}; text-align: center; margin-top: 20px;">
                                <i style="color: {tm.color('accent')};">Please proceed to <b>Global Settings</b> to configure your AI models, API keys, and network proxy.</i>
                            </p>
                        </div>
                        """

            lbl_desc = QLabel(welcome_html)
            lbl_desc.setWordWrap(True)
            lbl_desc.setTextFormat(Qt.RichText)
            dlg.content_layout.addWidget(lbl_desc)

            btn_save = QPushButton("Acknowledge & Go to Settings")
            btn_save.setFixedSize(280, 42)
            btn_save.setCursor(Qt.PointingHandCursor)
            btn_save.setStyleSheet(f"""
                QPushButton {{ 
                    background-color: {tm.color('accent')}; 
                    color: {tm.color('bg_main')}; 
                    border-radius: 6px; 
                    font-weight: bold; 
                    font-size: 14px; 
                    border: none;
                }} 
                QPushButton:hover {{ 
                    background-color: {tm.color('accent_hover')}; 
                }}
            """)
            btn_save.clicked.connect(dlg.accept)

            btn_layout = QHBoxLayout()
            btn_layout.addStretch()
            btn_layout.addWidget(btn_save)
            btn_layout.addStretch()
            dlg.content_layout.addLayout(btn_layout)

            if dlg.exec():
                cfg.user_settings.update({
                    "is_first_run": False
                })
                cfg.save_settings()
                self._jump_to_settings()
            return True
        return False
