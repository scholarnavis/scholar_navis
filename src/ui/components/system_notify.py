"""系统通知的**唯一实现**：把一条消息交给操作系统的通知中心。

为什么要它
----------
付费墙取全文这类"程序需要人做一件事"的场景，若直接把窗口弹出来并抢焦点，用户
正在浏览器里找论文的手会被打断两次（先被切走，再切回来）。本模块只做一件事：
发一条带应用名与图标的通知，**不改变任何窗口焦点**；用户主动点击通知时，才由
调用方决定是否把对应窗口拉到前台（:attr:`SystemNotifier.activated`）。

通道选择
--------
* 优先 Qt 托盘通知（Windows 原生 toast、KDE 等有托盘的主机）：能带应用图标，
  并且支持点击回调；
* 托盘不可用（GNOME 默认无托盘）时退到各平台的原生命令（``notify-send`` /
  ``osascript``），保证"通知一定能看到"，代价是没有点击回调——因此调用方不能
  把"用户点了通知"当作唯一入口；
* 二者都不行时返回 ``False``，由调用方自行降级（例如只在应用内提示）。

生命周期
--------
托盘图标只在有未处理通知期间显示（:meth:`notify` 时 show，:meth:`dismiss` 时
hide），不做常驻托盘，避免给不需要它的用户多出一个图标。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

__all__ = ["SystemNotifier", "system_notifier"]

#: 通知图标（PNG，系统通知栏普遍不认 SVG）。
_ICON_RELATIVE = ("Assets", "icon.png")


def _app_icon():
    """应用图标（QIcon）；取不到时返回 None。"""
    try:
        from PySide6.QtGui import QIcon

        from src.core.theme_manager import ThemeManager

        path = ThemeManager.get_resource_path(*_ICON_RELATIVE)
        if not path or not os.path.isfile(path):
            logger.debug("Notification icon not found: %s", path)
            return None
        icon = QIcon(path)
        return icon if not icon.isNull() else None
    except Exception as e:  # Qt 不可用 / 资源缺失都不该让通知链路崩掉
        logger.debug("Cannot build the notification icon: %s", e)
        return None


def _icon_path() -> str:
    try:
        from src.core.theme_manager import ThemeManager

        path = ThemeManager.get_resource_path(*_ICON_RELATIVE)
        return path if path and os.path.isfile(path) else ""
    except Exception:
        return ""


def _applescript_string(text: str) -> str:
    """AppleScript 字符串字面量：转义反斜杠与双引号。"""
    return '"' + str(text or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


class SystemNotifier(QObject):
    """向系统通知中心发消息（进程级单例，见 :func:`system_notifier`）。"""

    #: 用户点击了某条通知（参数为 ``notification_id``）。仅托盘通道会触发。
    activated = Signal(str)

    def __init__(self):
        super().__init__()
        self._tray = None
        self._icon = None
        self._tray_failed = False
        self._pending_id = ""

    # ------------------------------------------------------------------ 发送

    def notify(self, title: str, body: str, notification_id: str = "",
               timeout_ms: int = 12000) -> bool:
        """发一条系统通知；返回是否至少有一个通道投递成功。

        :param title: 通知标题（一般含应用名）。
        :param body: 正文（论文信息与"需要用户做什么"）。
        :param notification_id: 点击回调时回传的标识，用于定位对应窗口。
        :param timeout_ms: 通知展示时长（毫秒，托盘通道生效）。
        """
        title = str(title or "").strip()
        body = str(body or "").strip()
        if not body:
            logger.debug("Empty notification body ignored.")
            return False

        self._pending_id = str(notification_id or "")
        if self._notify_via_tray(title, body, timeout_ms):
            logger.info("System notification sent via tray: %s", title)
            return True
        if self._notify_via_command(title, body):
            logger.info("System notification sent via platform command: %s", title)
            return True
        logger.warning("No notification channel available; message dropped: %s", title)
        return False

    def dismiss(self) -> None:
        """清除"有未处理通知"状态（交付完成、取消或超时后调用）。"""
        self._pending_id = ""
        if self._tray is not None:
            self._tray.hide()

    # ------------------------------------------------------------ 托盘通道

    def _ensure_tray(self):
        """惰性创建托盘图标；不可用时返回 None（结果进程内缓存）。"""
        if self._tray is not None:
            return self._tray
        if self._tray_failed:
            return None
        try:
            from PySide6.QtWidgets import QApplication, QSystemTrayIcon

            if QApplication.instance() is None:
                self._tray_failed = True
                return None
            if not QSystemTrayIcon.isSystemTrayAvailable():
                logger.info("No system tray on this desktop; falling back to the "
                            "platform notification command.")
                self._tray_failed = True
                return None
            icon = _app_icon()
            if icon is None:
                self._tray_failed = True
                return None

            tray = QSystemTrayIcon(icon, self)
            tray.setToolTip(_app_name())
            tray.messageClicked.connect(self._on_message_clicked)
            self._tray = tray
            self._icon = icon
            return tray
        except Exception as e:
            logger.warning("System tray notification unavailable: %s", e)
            self._tray_failed = True
            return None

    def _notify_via_tray(self, title: str, body: str, timeout_ms: int) -> bool:
        tray = self._ensure_tray()
        if tray is None:
            return False
        try:
            tray.show()
            tray.showMessage(title, body, self._icon, int(timeout_ms))
            return True
        except Exception as e:
            logger.warning("Tray message failed: %s", e)
            return False

    def _on_message_clicked(self):
        """用户点了通知：只在这里才允许调用方抢焦点。"""
        notification_id = self._pending_id
        logger.info("System notification clicked (id=%s).", notification_id or "-")
        self.activated.emit(notification_id)

    # -------------------------------------------------------- 平台命令通道

    def _notify_via_command(self, title: str, body: str) -> bool:
        """Linux/macOS 的原生命令行通道（无点击回调，仅保证"能看到"）。"""
        try:
            if sys.platform.startswith("linux"):
                return self._notify_send(title, body)
            if sys.platform == "darwin":
                return self._notify_osascript(title, body)
        except Exception as e:
            logger.warning("Platform notification command failed: %s", e)
        return False

    @staticmethod
    def _notify_send(title: str, body: str) -> bool:
        if not shutil.which("notify-send"):
            return False
        cmd = ["notify-send", "--app-name", _app_name(), "--urgency", "normal"]
        icon = _icon_path()
        if icon:
            cmd += ["--icon", icon]
        cmd += [title, body]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
        return True

    @staticmethod
    def _notify_osascript(title: str, body: str) -> bool:
        if not shutil.which("osascript"):
            return False
        script = (f"display notification {_applescript_string(body)} "
                  f"with title {_applescript_string(_app_name())} "
                  f"subtitle {_applescript_string(title)}")
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
        return True


def _app_name() -> str:
    from src.core.version import __app_name__

    return __app_name__


_INSTANCE = None


def system_notifier() -> SystemNotifier:
    """进程级单例（首次调用时才创建，导入本模块无副作用）。"""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = SystemNotifier()
    return _INSTANCE
