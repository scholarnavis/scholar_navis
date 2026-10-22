"""系统通知（桌面托盘气泡）。

用途
----
需要用户在**应用之外**被提醒的场景：AI 请求登录出版社 / 机构、全文下载完成、
长时间任务结束。应用内的 Toast 只在窗口可见时有用，最小化或切到浏览器后就看不
到了。

实现口径
--------
* 走 Qt 的 ``QSystemTrayIcon.showMessage``：Windows 通知中心、macOS 通知中心、
  Linux（有托盘/NotificationItem 的桌面）都能收到；
* 懒创建托盘图标：没有托盘的环境（无桌面会话、纯 headless、部分精简桌面）直接
  返回 ``False``，**绝不**因此崩溃或阻塞——调用方本来就有应用内提示；
* 不注册菜单、不常驻交互：托盘图标唯一职责是承载通知，避免与用户的托盘管理冲突。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("Core.Notifications")

#: 懒创建的托盘图标（Qt 要求通知必须挂在 QSystemTrayIcon 上）。
_tray = None
_unavailable = False


def _ensure_tray():
    """创建（或在不可用时标记）托盘图标；返回 QSystemTrayIcon 或 None。"""
    global _tray, _unavailable
    if _tray is not None or _unavailable:
        return _tray
    try:
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon

        app = QApplication.instance()
        if app is None or not QSystemTrayIcon.isSystemTrayAvailable():
            _unavailable = True
            logger.info("System tray unavailable; desktop notifications disabled.")
            return None
        tray = QSystemTrayIcon()
        try:
            from src.core.theme_manager import ThemeManager
            icon = ThemeManager().get_app_icon()
            if not icon.isNull():
                tray.setIcon(icon)
        except Exception as e:      # 没有图标也能发通知，只是气泡里没有图
            logger.debug("Tray icon unavailable: %s", e)
        tray.setToolTip("Scholar Navis")
        tray.show()
        _tray = tray
    except Exception as e:
        _unavailable = True
        logger.info("Desktop notifications unavailable: %s", e)
    return _tray


def notify(title: str, body: str, timeout_ms: int = 10000,
           app_icon: Optional[str] = None) -> bool:
    """发一条桌面通知；返回是否真的发出去了。

    :param app_icon: 预留：部分平台支持在气泡里指定图标（当前统一用应用图标）。
    """
    title = str(title or "").strip()
    body = str(body or "").strip()
    if not title and not body:
        return False
    tray = _ensure_tray()
    if tray is None:
        # Linux 桌面常常没有托盘（GNOME 默认不提供）：退回 notify-send，
        # 否则"AI 请你登录"这类提醒在最小化时完全看不到。
        return _notify_send(title, body)
    try:
        from PySide6.QtWidgets import QSystemTrayIcon
        tray.showMessage(title or "Scholar Navis", body,
                         QSystemTrayIcon.MessageIcon.Information, int(timeout_ms))
        return True
    except Exception as e:
        logger.warning("Failed to show desktop notification: %s", e)
        return False


def _icon_path() -> str:
    """应用图标文件路径（通知里要的是路径，不是 QIcon）。"""
    try:
        from src.core import BASE_DIR
        import os
        candidate = os.path.join(BASE_DIR, "Assets", "icon.png")
        if os.path.isfile(candidate):
            return candidate
    except Exception:
        pass
    try:
        from src.core.theme_manager import ThemeManager
        path = ThemeManager().get_resource_path("assets", "icon.png")
        return str(path or "")
    except Exception:
        return ""


def _notify_send(title: str, body: str) -> bool:
    """无托盘时的 Linux 兜底：调用 ``notify-send``（libnotify）。

    不阻塞调用方（``Popen`` 不等待），失败一律当"没发出去"——调用方在应用内还有
    Toast，不会因为通知失败而丢信息。
    """
    try:
        import shutil
        import subprocess
        import sys

        if not sys.platform.startswith("linux"):
            return False
        icon = _icon_path()
        binary = shutil.which("notify-send")
        if binary:
            cmd = [binary, "-a", "Scholar Navis"]
            if icon:
                cmd += ["-i", icon]      # 通知气泡里的应用图标
            cmd += [title or "Scholar Navis", body or ""]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        # 没有 libnotify 时直接走 D-Bus（glib 的 gdbus 基本都装了）
        gdbus = shutil.which("gdbus")
        if gdbus:
            subprocess.Popen(
                [gdbus, "call", "--session", "--dest", "org.freedesktop.Notifications",
                 "--object-path", "/org/freedesktop/Notifications",
                 "--method", "org.freedesktop.Notifications.Notify",
                 "Scholar Navis", "0", icon or "", title or "Scholar Navis",
                 body or "", "[]", "{}", "6000"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        return False
    except Exception as e:
        logger.debug("notify-send fallback failed: %s", e)
        return False


__all__ = ["notify"]
