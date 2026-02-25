import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QListWidget,
                               QStackedWidget, QSplitter, QPushButton, QLabel, QHBoxLayout)

from src.core.config_manager import ConfigManager
from src.core.device_manager import DeviceManager
from src.core.mcp_manager import MCPManager
from src.core.models_registry import resolve_auto_model, check_model_exists, get_model_conf
from src.tools.chat_tool import ChatTool
# 引入所有工具
from src.tools.import_tool import ImportTool
from src.tools.log_tool import LogTool
from src.tools.rss_tool import RSSTool
from src.tools.settings_tool import SettingsTool
from src.ui.components.dialog import StandardDialog
from src.ui.components.quick_translator import QuickTranslatorWindow
from src.ui.components.toast import ToastManager


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Scholar Navis - Research Assistant")
        self.resize(1280, 800)

        # Toast
        ToastManager().set_parent(self)

        # 主分割器
        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.setHandleWidth(2)
        main_splitter.setStyleSheet("QSplitter::handle { background-color: #333; }")
        self.setCentralWidget(main_splitter)

        # 左侧面板构建 (收窄宽度、更紧凑的边距)
        left_panel = QWidget()
        left_panel.setMaximumWidth(220)

        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(5, 10, 5, 10)
        left_layout.setSpacing(10)

        # 1. Logo
        logo_label = QLabel("🧠 Scholar Navis")
        logo_label.setStyleSheet("""
            QLabel {
                color: #05B8CC;
                font-size: 18px;
                font-weight: bold;
                font-family: 'Segoe UI', 'Microsoft YaHei';
                padding: 10px 5px;
            }
        """)
        left_layout.addWidget(logo_label)

        # 2. 导航栏
        self.sidebar = QListWidget()
        self.sidebar.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sidebar.setStyleSheet("""
            QListWidget { 
                border: none; 
                background-color: transparent; 
                font-family: 'Segoe UI', sans-serif;
                font-size: 14px; 
                outline: none; 
            }
            QListWidget::item { 
                padding: 12px 15px; 
                border-left: 3px solid transparent;
                color: #cccccc;
                border-radius: 6px;
                margin-bottom: 2px;
            }
            QListWidget::item:selected { 
                background-color: #37373d; 
                color: white; 
                border-left: 3px solid #05B8CC;
                font-weight: bold;
            }
            QListWidget::item:hover:!selected { 
                background-color: #2a2d2e; 
            }
        """)
        self.sidebar.currentRowChanged.connect(self.switch_tool)
        left_layout.addWidget(self.sidebar)


        # 灵动翻译入口
        self.btn_quick_trans = QPushButton("🌐")
        self.btn_quick_trans.setToolTip("Quick Translate (Ctrl+Shift+T)")
        self.btn_quick_trans.setCursor(Qt.PointingHandCursor)
        self.btn_quick_trans.setStyleSheet("""
                    QPushButton {
                        background-color: transparent;
                        border: none;
                        color: #555555; 
                        font-size: 26px; 
                        padding: 6px;
                        margin-left: 5px;
                        border-radius: 8px; 
                    }
                    QPushButton:hover {
                        color: #05B8CC; 
                        background-color: rgba(5, 184, 204, 0.1);
                    }
                    QPushButton:pressed {
                        color: #0497A7;
                        background-color: rgba(5, 184, 204, 0.2);
                    }
                """)
        self.btn_quick_trans.clicked.connect(self.toggle_quick_translator)

        # 靠左下角对齐
        left_layout.addWidget(self.btn_quick_trans, alignment=Qt.AlignLeft | Qt.AlignBottom)

        main_splitter.addWidget(left_panel)

        # 右侧主面板构建
        self.tool_stack = QStackedWidget()
        self.tool_stack.setStyleSheet("background-color: #1e1e1e;")
        main_splitter.addWidget(self.tool_stack)

        # --- 加载工具 ---
        self.tools = []

        self.import_tool = ImportTool()
        self.add_tool(self.import_tool)


        self.add_tool(ChatTool())
        self.rss_tool = RSSTool()
        self.add_tool(self.rss_tool)

        self.add_tool(SettingsTool())

        self.log_tool = LogTool()
        self.add_tool(self.log_tool)

        self.sidebar.setCurrentRow(0)
        self.perform_startup_checks()
        self.clean_old_logs()

        self._setup_mcp_status_bar()

        # 注册全局翻译快捷键
        self.translator_dialog = QuickTranslatorWindow(None)
        self.shortcut_translate = QShortcut(QKeySequence("Ctrl+Shift+T"), self)
        self.shortcut_translate.activated.connect(self.toggle_quick_translator)

    # 新增唤醒翻译窗口的方法
    def toggle_quick_translator(self):
        if self.translator_dialog.isHidden() or self.translator_dialog.windowOpacity() == 0.0:
            self.translator_dialog.setWindowOpacity(1.0)
            self.translator_dialog.show()
            self.translator_dialog.activateWindow()
            self.translator_dialog.input_box.setFocus()
        else:
            self.translator_dialog.hide_with_fade()

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
        self.sidebar.addItem(tool.tool_name)

        if hasattr(tool, 'sig_log'):
            tool.sig_log.connect(lambda msg: self.dispatch_log("INFO", msg))

    def switch_tool(self, index):
        self.tool_stack.setCurrentIndex(index)

    def check_model_integrity(self):
        """
        启动自检
        🌟 修复：不再自动删除文件，只提示。
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
            # 只有在确实找不到的时候才弹窗，且不乱动文件
            StandardDialog(self, "System Check", msg).exec()
            self._jump_to_settings()

    def _jump_to_settings(self):
        self.switch_tool(6)
        self.sidebar.setCurrentRow(6)

    def check_first_run(self):
        cfg = ConfigManager()
        if cfg.user_settings.get("is_first_run", True):
            welcome_msg = (
                "Welcome to Scholar Navis!\n\n"
                "Please go to the 'Global Settings' tab to configure your AI models."
            )
            StandardDialog(self, "Welcome", welcome_msg).exec()
            self.switch_tool(6)
            self.sidebar.setCurrentRow(6)
            cfg.user_settings['is_first_run'] = False
            cfg.save_settings()
            return True
        return False

