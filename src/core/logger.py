import logging
import os
import signal
import sys
import threading
from datetime import datetime
from PySide6.QtCore import QObject, Signal

from src.core import BASE_DIR


log_dir = os.path.join(BASE_DIR,"logs")

#: 保留引用：Qt 侧对传入的消息处理器只持弱引用，被 GC 后崩溃现场会静默消失。
_qt_message_handler_ref = None

#: 致命信号转储文件句柄，必须活到进程结束（提前关闭会让 faulthandler 失效）。
_faulthandler_file = None

class QtLogHandler(QObject, logging.Handler):
    new_log_signal = Signal(str, str, str, int)

    def __init__(self):
        QObject.__init__(self)
        logging.Handler.__init__(self)
        self.log_history = []

    def emit(self, record):
        # 日志管道不允许向调用方抛出异常（与 stdlib logging.Handler.emit 行为一致），
        # format() 失败域不可穷举，此处豁免静态检查。
        # noinspection PyBroadException
        try:
            msg = self.format(record)
            self.log_history.append((record.levelname, msg, record.pathname, record.lineno))
            if len(self.log_history) > 2000:
                self.log_history.pop(0)
            self.new_log_signal.emit(record.levelname, msg, record.pathname, record.lineno)
        except Exception:
            self.handleError(record)

# 全局单例
_qt_handler = QtLogHandler()

_early_formatter = logging.Formatter('%(asctime)s | %(name)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S')
_qt_handler.setFormatter(_early_formatter)
logging.getLogger().addHandler(_qt_handler)

def _flush_root_handlers():
    """把根 logger 的所有 handler 立即刷盘（致命路径上必须先把证据落盘）。"""
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:          # noqa: BLE001 - 刷盘失败不能反过来影响崩溃路径
            pass


def _install_qt_message_handler():
    """把 Qt 自身的消息并入应用日志。

    Qt 的 ``qDebug`` / ``qWarning`` / ``qCritical`` / ``qFatal`` 默认只写 stderr，
    从不进日志文件。于是 QtWebEngine 的原生崩溃（例如
    ``Release of profile requested but WebEnginePage still not deleted``、
    ``Failed to create shared context``）在事后复盘时**完全不存在**——这正是
    "打开 Mermaid 阅读器后闪退却没有任何日志"的直接原因。

    ``qFatal`` 之后 Qt 会直接 abort，因此这里对每条消息都强制刷盘。
    """
    global _qt_message_handler_ref
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    level_map = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    qt_logger = logging.getLogger("Qt")
    state = threading.local()

    def _handler(mode, context, message):
        # 递归保护：日志 handler 自身若再触发 Qt 消息，会无限套娃。
        if getattr(state, "active", False):
            return
        state.active = True
        try:
            where = ""
            if context is not None and getattr(context, "file", None):
                where = f" ({context.file}:{context.line})"
            qt_logger.log(level_map.get(mode, logging.INFO), "%s%s", message, where)
            _flush_root_handlers()
        finally:
            state.active = False

    _qt_message_handler_ref = _handler
    qInstallMessageHandler(_handler)


def _enable_faulthandler():
    """开启致命信号转储，返回转储文件路径。

    原生崩溃（SIGSEGV / SIGABRT / SIGBUS …）不会经过 ``sys.excepthook``，日志里
    只会毫无预兆地断掉。``faulthandler`` 在信号处理函数里把各线程的 Python 栈写
    进 ``logs/crash_*.log``，是这类"闪退无痕"唯一的取证手段。
    """
    global _faulthandler_file
    if _faulthandler_file is not None:
        return getattr(_faulthandler_file, "name", "")

    path = os.path.join(log_dir, f"crash_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    try:
        import faulthandler

        os.makedirs(log_dir, exist_ok=True)
        _faulthandler_file = open(path, "a", encoding="utf-8")   # noqa: SIM115 - 需活到进程结束
        faulthandler.enable(file=_faulthandler_file, all_threads=True)
        # chain=True：转储后仍执行原处理器，保留系统的 core dump / 退出码语义。
        for name in ("SIGSEGV", "SIGABRT", "SIGBUS", "SIGFPE", "SIGILL"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                faulthandler.register(sig, file=_faulthandler_file,
                                      all_threads=True, chain=True)
            except (ValueError, OSError):
                pass
        return path
    except Exception as e:          # noqa: BLE001 - 取证失败绝不能阻断启动
        _faulthandler_file = None
        return f"<failed: {e}>"


def setup_logger():
    """Configure global logging"""
    root_logger = logging.getLogger()

    log_level = logging.INFO
    try:
        from src.core.config_manager import ConfigManager
        config_mgr = ConfigManager()
        level_str = config_mgr.user_settings.get("log_level", "INFO")
        log_level = getattr(logging, level_str.upper(), logging.INFO)
    except Exception as e:
        # 配置读取失败不得阻断日志初始化，回退 INFO 并记录原因
        logging.getLogger().debug(f"Failed to load log level setting, fallback to INFO: {e}")

    root_logger.setLevel(log_level)

    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    formatter = logging.Formatter('%(asctime)s | %(name)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S')

    _qt_handler.setFormatter(formatter)
    root_logger.addHandler(_qt_handler)

    os.makedirs(log_dir, exist_ok=True)
    log_filename = f"scholar_navis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_path = os.path.join(log_dir, log_filename)

    file_handler = logging.FileHandler(log_path, mode='a', encoding='utf-8')
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)



    def global_exception_handler(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        root_logger.critical("UNCAUGHT FATAL EXCEPTION", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = global_exception_handler
    root_logger.info(f"Logger initialized. Log file: {log_path}")

    # 取证能力必须在任何 GUI / QtWebEngine 代码之前就位，否则原生闪退不留痕迹。
    _install_qt_message_handler()
    root_logger.info(f"Crash diagnostics enabled. Signal dump: {_enable_faulthandler()}")
    return root_logger

def get_qt_log_handler():
    return _qt_handler