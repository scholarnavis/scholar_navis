import os
import shutil
import sys
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QListWidget,
                               QStackedWidget, QSplitter)
from PySide6.QtCore import Qt, QSize

from src.core.config_manager import ConfigManager
from src.core.device_manager import DeviceManager
from src.core.logger import get_qt_log_handler
from src.core.models_registry import resolve_auto_model, check_model_exists, get_model_conf
from src.ui.components.dialog import StandardDialog
from src.ui.components.toast import ToastManager

# 引入所有工具
from src.tools.import_tool import ImportTool
from src.tools.settings_tool import SettingsTool
from src.tools.chat_tool import ChatTool
from src.tools.staging_tool import StagingTool
from src.tools.graph_tool import GraphTool
from src.tools.gap_miner import GapMinerTool
from src.tools.radar_tool import RadarTool
from src.tools.log_tool import LogTool


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Scholar Navis Pro - Research Assistant")
        self.resize(1280, 800)

        # Toast
        ToastManager().set_parent(self)

        # 主分割器
        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.setHandleWidth(2)
        main_splitter.setStyleSheet("QSplitter::handle { background-color: #333; }")
        self.setCentralWidget(main_splitter)

        # --- 左侧面板 ---
        left_panel = QWidget()
        left_panel.setMinimumWidth(240)
        left_panel.setMaximumWidth(300)

        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.sidebar = QListWidget()
        self.sidebar.setStyleSheet("""
            QListWidget { 
                border: none; 
                background-color: #252526; 
                font-family: 'Segoe UI', sans-serif;
                font-size: 14px; 
                outline: none; 
            }
            QListWidget::item { 
                padding: 15px 20px; 
                border-left: 3px solid transparent;
                color: #cccccc;
            }
            QListWidget::item:selected { 
                background-color: #37373d; 
                color: white; 
                border-left: 3px solid #007acc;
                font-weight: bold;
            }
            QListWidget::item:hover { 
                background-color: #2a2d2e; 
            }
        """)
        self.sidebar.currentRowChanged.connect(self.switch_tool)

        left_layout.addWidget(self.sidebar)
        main_splitter.addWidget(left_panel)

        # --- 右侧面板 ---
        self.tool_stack = QStackedWidget()
        self.tool_stack.setStyleSheet("background-color: #1e1e1e;")
        main_splitter.addWidget(self.tool_stack)

        # --- 加载工具 ---
        self.tools = []

        self.import_tool = ImportTool()
        self.add_tool(self.import_tool)

        self.add_tool(StagingTool())
        self.add_tool(ChatTool())
        self.add_tool(GraphTool())
        self.add_tool(GapMinerTool())
        self.add_tool(RadarTool())
        self.add_tool(SettingsTool())

        self.log_tool = LogTool()
        self.add_tool(self.log_tool)

        self.sidebar.setCurrentRow(0)
        self.perform_startup_checks()

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
                repo_id = conf.get('hf_repo_id')
                if repo_id and not check_model_exists(repo_id):
                    missing_repos.append((repo_id, conf.get('ui_name', mid)))

        if missing_repos:
            # 🛑 关键修改：即使检测失败，也不执行 shutil.rmtree
            # 仅仅提示用户去设置页面检查
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