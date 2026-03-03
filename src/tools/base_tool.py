import logging
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import QObject

from src.core.config_manager import ConfigManager
from src.ui.components.toast import ToastManager


class BaseTool(QObject):


    def __init__(self, tool_name: str):
        super().__init__()
        self.tool_name = tool_name
        self.logger = logging.getLogger(f"Tool.{tool_name}")
        self.config = ConfigManager()

    def get_ui_widget(self) -> QWidget:
        """返回该工具的主界面 Widget"""
        raise NotImplementedError(f"{self.tool_name} must implement the get_ui_widget method.")

    def on_task_log(self, level: str, msg: str):
        """
        处理后台 TaskManager 发来的日志信号，并在发生警告/错误时自动弹 Toast。
        """
        if level == "INFO":
            self.logger.info(msg)
        elif level == "WARNING":
            self.logger.warning(msg)
            ToastManager().show(f"{msg}", "warning")
        elif level == "ERROR":
            self.logger.error(msg)
            ToastManager().show(f"{msg}", "error")
        elif level == "DEBUG":
            self.logger.debug(msg)
        else:
            self.logger.info(msg)

