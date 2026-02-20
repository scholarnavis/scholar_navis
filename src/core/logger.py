import logging
import os
import sys
from datetime import datetime
from PySide6.QtCore import QObject, Signal

class QtLogHandler(logging.Handler, QObject):
    new_log_signal = Signal(str, str)

    def __init__(self):
        logging.Handler.__init__(self)
        QObject.__init__(self)
        self.log_history = []

    def emit(self, record):
        try:
            msg = self.format(record)
            self.log_history.append((record.levelname, msg))
            # 限制缓冲区防止内存泄漏
            if len(self.log_history) > 2000:
                self.log_history.pop(0)
            self.new_log_signal.emit(record.levelname, msg)
        except Exception:
            self.handleError(record)

# 全局单例
_qt_handler = QtLogHandler()


def setup_logger():
    """Configure global logging"""
    root_logger = logging.getLogger()

    log_level = logging.INFO
    try:
        from src.core.config_manager import ConfigManager
        config_mgr = ConfigManager()
        level_str = config_mgr.user_settings.get("log_level", "INFO")
        log_level = getattr(logging, level_str.upper(), logging.INFO)
    except Exception:
        pass

    root_logger.setLevel(log_level)

    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    formatter = logging.Formatter('%(asctime)s | %(name)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S')

    _qt_handler.setFormatter(formatter)
    root_logger.addHandler(_qt_handler)

    log_dir = os.path.join(os.getcwd(), "logs")
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
    return root_logger

    # 全局异常捕获
    def global_exception_handler(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        root_logger.critical("🔥 UNCAUGHT FATAL EXCEPTION", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = global_exception_handler

    root_logger.info(f"Logger initialized. Log file: {log_path}")
    return root_logger

def get_qt_log_handler():
    return _qt_handler