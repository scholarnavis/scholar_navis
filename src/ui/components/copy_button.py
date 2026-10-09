"""可复用的"复制到剪贴板"按钮（单一实现）。

背景
----
项目里"复制到剪贴板 + 提示成功"的按钮散落在多处（聊天气泡的 Copy / Copy MD、
源码查看器的 Copy、引用卡片详情面板的 Copy citation 等），各自实现一遍，反馈
方式还不一致（有的弹 Toast、有的改文字、有的什么都不提示）。本模块把这件事收敛
为唯一实现：

* 复制文本到系统剪贴板；
* 复制成功后给出**可见反馈**：按钮文字短暂变为 `Copied`，随后自动还原；
* 可选串一个 Toast（用于需要更醒目的场景）。

文本来源用 ``provider`` 回调注入（**每次点击时求值**），因此调用方无需持有文本
快照——引用卡片切换条目、聊天内容流式增长时都能拿到最新内容。

用法::

    btn = CopyButton("Copy", copied_text="Copied",
                     provider=lambda: some_widget.toPlainText(),
                     toast="Copied to clipboard.")
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QPushButton

logger = logging.getLogger("UI.CopyButton")

#: 复制成功反馈的默认停留时长（ms）。
COPIED_FLASH_MS = 1200


class CopyButton(QPushButton):
    """复制按钮：点击后调用 ``provider()``，把其返回文本写入剪贴板并闪现反馈。"""

    def __init__(self, text: str = "Copy", *, copied_text: str = "Copied",
                 provider: Optional[Callable[[], str]] = None,
                 toast: Optional[str] = None,
                 restore_ms: int = COPIED_FLASH_MS,
                 parent=None):
        super().__init__(text, parent)
        self._idle_text = text
        self._copied_text = copied_text
        self._provider = provider
        self._toast = toast
        self._restore_ms = max(200, int(restore_ms))
        self._restore_timer = QTimer(self)
        self._restore_timer.setSingleShot(True)
        self._restore_timer.timeout.connect(self._restore)
        self.setCursor(Qt.PointingHandCursor)
        # 不抢键盘焦点：复制动作不应打断宿主控件的焦点（如浮层的 Esc 关闭）。
        self.setFocusPolicy(Qt.NoFocus)
        self.clicked.connect(self.copy_now)

    # ------------------------------------------------------------------ #
    #  配置
    # ------------------------------------------------------------------ #
    def set_provider(self, provider: Optional[Callable[[], str]]) -> None:
        """设置/更新文本来源回调（每次点击时求值）。"""
        self._provider = provider

    def set_idle_text(self, text: str) -> None:
        """更新静止态文案（反馈闪现期间不打断当前显示）。"""
        self._idle_text = text
        if not self._restore_timer.isActive():
            self.setText(text)

    # ------------------------------------------------------------------ #
    #  动作
    # ------------------------------------------------------------------ #
    def copy_now(self) -> bool:
        """复制 ``provider()`` 返回的文本；成功复制返回 True（空文本返回 False）。"""
        text = ""
        if callable(self._provider):
            try:
                text = self._provider() or ""
            except Exception as e:  # provider 是外部回调，异常不得打断 UI
                logger.warning("CopyButton provider failed: %s", e)
                text = ""
        if not text:
            return False
        QGuiApplication.clipboard().setText(text)
        self._flash()
        if self._toast:
            try:
                from src.ui.components.toast import ToastManager
                ToastManager().show(self._toast, "success")
            except Exception as e:  # pragma: no cover - Toast 不可用不影响复制
                logger.debug("CopyButton toast skipped: %s", e)
        logger.debug("CopyButton copied %d char(s).", len(text))
        return True

    # ------------------------------------------------------------------ #
    #  内部
    # ------------------------------------------------------------------ #
    def _flash(self):
        """可见反馈：文字临时变为"已复制"，到点自动还原。"""
        if self.text() != self._copied_text:
            self.setText(self._copied_text)
        self._restore_timer.start(self._restore_ms)

    def _restore(self):
        self.setText(self._idle_text)


__all__ = ["CopyButton", "COPIED_FLASH_MS"]
