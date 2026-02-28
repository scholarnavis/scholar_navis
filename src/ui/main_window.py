import os
import sys

from PySide6.QtCore import Qt, QSize, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QShortcut, QKeySequence, QIcon
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QListWidget,
                               QStackedWidget, QSplitter, QPushButton, QLabel, QHBoxLayout, QListWidgetItem, QComboBox)

from src.core.config_manager import ConfigManager
from src.core.device_manager import DeviceManager
from src.core.mcp_manager import MCPManager
from src.core.models_registry import resolve_auto_model, check_model_exists, get_model_conf
from src.core.theme_manager import ThemeManager
from src.tools.about_tool import AboutTool
from src.tools.chat_tool import ChatTool

from src.tools.import_tool import ImportTool
from src.tools.log_tool import LogTool
from src.tools.rss_tool import RSSTool
from src.tools.settings_tool import SettingsTool
from src.ui.components.dialog import StandardDialog, BaseDialog
from src.ui.components.quick_translator import QuickTranslatorWindow
from src.ui.components.toast import ToastManager

def set_window_titlebar_theme(hwnd, is_dark: bool):
    if sys.platform == "win32":
        import ctypes
        try:
            # 20 是 DWMWA_USE_IMMERSIVE_DARK_MODE 的常量值
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                int(hwnd), 20, ctypes.byref(ctypes.c_int(1 if is_dark else 0)), 4)
        except Exception:
            pass

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Scholar Navis - Research Assistant")
        self.resize(1280, 800)

        # 应用全局窗体图标
        self.setWindowIcon(QIcon(ThemeManager.get_resource_path("Assets", "ico.svg")))

        ToastManager().set_parent(self)

        # 主分割器
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setHandleWidth(1)  # 更现代的细线分割
        self.setCentralWidget(self.main_splitter)

        # --- 左侧面板 ---
        self.left_panel = QWidget()
        self.left_panel.setFixedWidth(220)

        left_layout = QVBoxLayout(self.left_panel)
        left_layout.setContentsMargins(10, 15, 10, 15)
        left_layout.setSpacing(10)

        # 顶部：汉堡菜单(折叠按钮) + Logo
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

        self.tools = []

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
        self.add_tool(ChatTool())
        self.add_tool(RSSTool())
        self.add_tool(SettingsTool())
        self.add_tool(LogTool())
        self.add_tool(AboutTool())

        self.sidebar.setCurrentRow(0)
        self.perform_startup_checks()
        self.clean_old_logs()
        self._setup_mcp_status_bar()

        self.translator_dialog = QuickTranslatorWindow(None)
        self.shortcut_translate = QShortcut(QKeySequence("Ctrl+Shift+T"), self)
        self.shortcut_translate.activated.connect(self.toggle_quick_translator)

        self.tm.theme_changed.connect(self._apply_theme)
        self._apply_theme()

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
        logo_path = os.path.join(os.getcwd(), "Assets", filename)

        if os.path.exists(logo_path):
            self.logo_widget.load(logo_path)
        else:
            pass

    def _apply_theme(self):
        tm = self.tm
        is_dark = tm.current_theme == "dark"

        set_window_titlebar_theme(self.winId(), is_dark)

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

        status_layout.addSpacing(10)  # 增加一些间距

        # 外部MCP状态
        self.external_status_label = QLabel("External: Disabled")
        self.external_status_label.setStyleSheet("color: #888;")
        status_layout.addWidget(self.external_status_label)

        status_layout.addSpacing(10)

        # 网络MCP状态
        self.custom_status_label = QLabel("Custom: 0 Active")
        self.custom_status_label.setStyleSheet("color: #888;")
        status_layout.addWidget(self.custom_status_label)

        # 添加到主界面底部的状态栏
        self.statusBar().addPermanentWidget(status_widget)

        # 定期刷新状态
        from PySide6.QtCore import QTimer
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._update_mcp_status)
        self.status_timer.start(5000)  # 每5秒刷新一次

    def _update_mcp_status(self):

        mcp_mgr = MCPManager.get_instance()
        config_mgr = ConfigManager()
        mcp_config = config_mgr.mcp_servers.get("mcpServers", {})

        # 1. 更新外部 MCP 状态 (Legacy External)
        ext_cfg = mcp_config.get("external", {})
        if ext_cfg.get("enabled", False):
            status = mcp_mgr.get_server_status("external")
            if status == "connected":
                self.external_status_label.setText("External: Connected")
                self.external_status_label.setStyleSheet("color: #4caf50; font-weight: bold;")
            elif "error" in status:
                self.external_status_label.setText("External: Error")
                self.external_status_label.setStyleSheet("color: #ff6b6b;")
                self.external_status_label.setToolTip(status)
            else:
                self.external_status_label.setText(f"External: {status.capitalize()}")
                self.external_status_label.setStyleSheet("color: #ffb86c; font-weight: bold;")
        else:
            self.external_status_label.setText("External: Disabled")
            self.external_status_label.setStyleSheet("color: #888;")

        # 全面更新自定义 MCP 状态综合数量
        connected_count = 0
        starting_count = 0
        error_count = 0

        for name, cfg in mcp_config.items():
            # 过滤掉两个内置的，剩下的全算 Custom
            if name not in ["builtin", "external"]:
                if cfg.get("enabled", False) or cfg.get("always_on", False):
                    status = mcp_mgr.get_server_status(name)
                    if status == "connected":
                        connected_count += 1
                    elif status in ["starting", "connecting"]:
                        starting_count += 1
                    elif "error" in status:
                        error_count += 1

        # 优先级：正在启动/排队中 > 报错 > 全部连上 > 未启用
        if starting_count > 0:
            self.custom_status_label.setText(f"Custom: {starting_count} Starting...")
            self.custom_status_label.setStyleSheet("color: #ffb86c; font-weight: bold;") # 橘黄色提示正在加载
        elif error_count > 0:
            self.custom_status_label.setText(f"Custom: {connected_count} Active, {error_count} Error")
            self.custom_status_label.setStyleSheet("color: #ff6b6b; font-weight: bold;") # 红色警告
        elif connected_count > 0:
            self.custom_status_label.setText(f"Custom: {connected_count} Active")
            self.custom_status_label.setStyleSheet("color: #4caf50; font-weight: bold;") # 绿色就绪
        else:
            self.custom_status_label.setText("Custom: 0 Active")
            self.custom_status_label.setStyleSheet("color: #888;")


    def clean_old_logs(self):
        """保留最近的 30 个日志文件，删除更早的"""
        log_dir = os.path.join(os.getcwd(), "logs")
        if not os.path.exists(log_dir):
            return

        logs = [os.path.join(log_dir, f) for f in os.listdir(log_dir) if f.endswith(".log")]
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
        """
        启动自检
        """
        cfg = ConfigManager()
        dev = DeviceManager().get_optimal_device()

        embed_id = cfg.user_settings.get("current_model_id", "embed_auto")
        if embed_id == "embed_auto":
            embed_id = resolve_auto_model("embedding", dev)

        rerank_id = cfg.user_settings.get("rerank_model_id", "rerank_auto")
        if rerank_id == "rerank_auto":
            rerank_id = resolve_auto_model("reranker", dev)

        missing_repos = []

        for mid, mtype in [(embed_id, "embedding"), (rerank_id, "reranker")]:
            conf = get_model_conf(mid, mtype)
            if conf:
                if conf.get('is_network', False):
                    continue

                repo_id = conf.get('hf_repo_id')
                if repo_id and not check_model_exists(repo_id):
                    missing_repos.append((repo_id, conf.get('ui_name', mid)))

        if missing_repos:
            names = "\n".join([f"• {m[1]}" for m in missing_repos])
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
        self.switch_tool(6)
        self.sidebar.setCurrentRow(6)

    def check_first_run(self):
        cfg = ConfigManager()
        if cfg.user_settings.get("is_first_run", True):

            dlg = BaseDialog(self, "Welcome to Scholar Navis", width=550)
            dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowStaysOnTopHint)
            layout = QVBoxLayout(dlg.main_frame)

            lbl_desc = QLabel(
                "<b>First Time Setup</b><br><br>"
                "Before we begin, please select your computation device and core models based on your hardware capabilities."
            )
            lbl_desc.setWordWrap(True)
            layout.addWidget(lbl_desc)

            # 1. Device
            combo_dev = QComboBox()
            from src.core.device_manager import DeviceManager
            for dev in DeviceManager().get_available_devices():
                combo_dev.addItem(dev["name"], dev["id"])
            layout.addWidget(QLabel("<b>Compute Engine:</b> (Select DirectML/CUDA if you have a GPU)"))
            layout.addWidget(combo_dev)

            # 2. Embed
            combo_embed = QComboBox()
            from src.core.models_registry import EMBEDDING_MODELS
            for m in EMBEDDING_MODELS:
                combo_embed.addItem(m['ui_name'], m['id'])
            layout.addWidget(QLabel("<br><b>Text Embedding Model:</b>"))
            layout.addWidget(combo_embed)

            btn_save = QPushButton("Save & Download Models")
            btn_save.clicked.connect(dlg.accept)
            layout.addWidget(btn_save)

            if dlg.exec():
                cfg.user_settings.update({
                    "is_first_run": False,
                    "inference_device": combo_dev.currentData(),
                    "current_model_id": combo_embed.currentData()
                })
                cfg.save_settings()
                self._jump_to_settings()

