import atexit
import logging
import logging.handlers
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime
from PySide6.QtCore import QObject, Signal

from src.core import BASE_DIR


log_dir = os.path.join(BASE_DIR,"logs")

#: 单个日志文件上限；超出后由 RotatingFileHandler 轮转为 ``*.log.1`` 等。
#: 此前用普通 FileHandler，一次长时间运行加 DEBUG 级别就能产出 GB 级单文件，
#: 而 ``clean_old_logs`` 只按文件个数清理，对总量毫无约束。
LOG_MAX_BYTES = 32 * 1024 * 1024
#: 同一次运行内保留的轮转份数
LOG_BACKUP_COUNT = 5

#: 日志写盘队列与后台写入器（进程级单例，setup_logger 重入时替换）
_log_queue = None
_queue_listener = None

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

def _drain_log_queue(timeout: float = 1.0) -> None:
    """等待写盘队列排空。队列空不等于最后一条已写完，故再留一个极短窗口。"""
    pending = _log_queue
    if pending is None:
        return
    deadline = time.monotonic() + timeout
    while pending.qsize() > 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.02)


def _flush_root_handlers():
    """把根 logger 的所有 handler 立即刷盘（致命路径上必须先把证据落盘）。

    写盘已移到后台线程，因此这里要先把队列排空，再对真实的文件 handler 调
    ``flush``；只对簿记用的 QueueHandler 调 flush 是空操作。
    """
    _drain_log_queue()

    targets = list(logging.getLogger().handlers)
    listener = _queue_listener
    if listener is not None:
        targets.extend(listener.handlers)
    for handler in targets:
        try:
            handler.flush()
        except Exception:          # noqa: BLE001 - 刷盘失败不能反过来影响崩溃路径
            pass


def _shutdown_logging() -> None:
    """停掉上一次安装的写盘线程并关闭其 handler。

    ``handlers.clear()`` 只从列表里移除、不关闭底层文件对象：重复调用
    ``setup_logger`` 会泄漏句柄，Windows 上旧日志文件还会一直被占用，
    使 ``clean_old_logs`` 的删除静默失败。
    """
    global _log_queue, _queue_listener

    if _queue_listener is not None:
        try:
            _queue_listener.stop()
        except Exception:          # noqa: BLE001 - 关闭失败不影响后续初始化
            pass
        _queue_listener = None
    _log_queue = None

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:          # noqa: BLE001
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
            # 只有 Critical / Fatal 才值得同步落盘：qFatal 之后 Qt 会直接 abort，
            # 其余级别交给后台写线程，否则每条 Qt 消息都是一次全 handler flush。
            if mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
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
    """Configure global logging.

    文件与控制台输出经 ``QueueHandler`` 交给后台线程写：UI 线程只做一次入队，
    不再承担 ``write + flush`` 的系统调用。日志量大时（DEBUG、MCP 输出、
    逐行解析进度）这是界面卡顿的直接来源之一。

    写入侧用 ``RotatingFileHandler``：文件名仍按启动时间生成，但单个文件超过
    :data:`LOG_MAX_BYTES` 时轮转，避免长时间运行产出无上限的单文件。
    """
    global _log_queue, _queue_listener

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

    _shutdown_logging()

    formatter = logging.Formatter('%(asctime)s | %(name)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S')

    # Qt 面板信号必须发在主线程（信号接收方是 UI），因此不进队列。
    _qt_handler.setFormatter(formatter)
    root_logger.addHandler(_qt_handler)

    os.makedirs(log_dir, exist_ok=True)
    log_filename = f"scholar_navis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_path = os.path.join(log_dir, log_filename)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, mode='a', encoding='utf-8',
        maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    _log_queue = queue.Queue()
    _queue_listener = logging.handlers.QueueListener(
        _log_queue, file_handler, console_handler, respect_handler_level=True)
    _queue_listener.start()
    root_logger.addHandler(logging.handlers.QueueHandler(_log_queue))



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

def flush_logs():
    """把待写日志立即落盘。

    写盘异步化之后，进程退出前必须显式排空队列，否则最后一段（往往正是
    崩溃 traceback）会随进程一起消失。崩溃钩子与优雅退出路径都应调用本函数。
    """
    _flush_root_handlers()


def get_qt_log_handler():
    return _qt_handler


# 正常退出时把队列里剩余的日志写完；崩溃路径由 _flush_root_handlers 主动触发。
atexit.register(_flush_root_handlers)