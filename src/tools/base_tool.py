from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel
from PySide6.QtCore import QObject, Signal, QThread
import logging

from src.ui.components.toast import ToastManager


class BaseTool(QObject):
    """
    所有工具的基类。
    规范：
    1. 必须实现 get_ui_widget() 返回界面
    2. 必须实现 execute_task() 执行后台任务
    3. 使用 self.log() 打印日志
    """
    # 通用信号
    sig_log = Signal(str)  # 发送日志
    sig_status = Signal(str)  # 更新状态栏
    sig_finished = Signal(object)  # 任务完成，返回数据
    sig_error = Signal(str)  # 任务出错

    def __init__(self, tool_name: str):
        super().__init__()
        self.tool_name = tool_name
        self.logger = logging.getLogger(f"Tool.{tool_name}")

    def get_ui_widget(self) -> QWidget:
        """返回该工具的主界面 Widget"""
        raise NotImplementedError("Tools must implement the get_ui_widget method.")


    def on_task_log(self, level: str, msg: str):
        """
        处理后台 TaskManager 发来的日志信号。
        自动分发到全局 Logger（显示在 LogTool），并在发生警告/错误时自动弹 Toast！
        """
        if level == "INFO":
            self.logger.info(msg)
        elif level == "WARNING":
            self.logger.warning(msg)
            # 局部引入，避免循环依赖
            ToastManager().show(f"{msg}", "warning")
        elif level == "ERROR":
            self.logger.error(msg)
            ToastManager().show(f"{msg}", "error")
        elif level == "DEBUG":
            self.logger.debug(msg)
        else:
            self.logger.info(msg)


class Worker(QObject):
    """通用的工作线程类"""
    finished = Signal()
    sig_result = Signal(object)
    sig_error = Signal(str)

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            result = self.func(*self.args, **self.kwargs)
            self.sig_result.emit(result)
        except Exception as e:
            import traceback
            self.sig_error.emit(str(e))
            # 打印堆栈以便调试
            self.logger.error(traceback.format_exc())
        finally:
            self.finished.emit()