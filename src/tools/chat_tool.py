"""对话工具（标签页容器）。

一个标签页 = 一个 :class:`~src.tools.chat_conversation.ChatConversation`。本文件只
负责"标签页这件事"，不碰对话内容：

* **标签栏 UI**：新建（SVG 加号）/ 切换 / 改名（双击、F2、右键）/ 关闭
  （标签上的 SVG 关闭按钮、鼠标中键、右键菜单），见
  :class:`~src.ui.components.chat_tabs.ChatTabBar`；
* **懒加载**：标签页的控件在它**第一次被激活**时才构建（建控件 + 回灌历史都不便宜），
  因此开很多标签页不会拖慢启动；未被打开过的标签页，其历史只在导出 / 切换时按需读盘；
* **持久化编排**：标签页清单（id / 名称 / 当前活动）落在
  :mod:`src.core.chat_session` 的 ``tabs.json``；每个标签页的会话由各自的
  ``ChatConversation`` 在轮次边界落盘。关闭标签页 = 丢弃该标签页的快照；
* **对外统一入口**：主窗口（"发送到对话"、开发者测试）只跟容器打交道，
  容器把调用转发给当前活动标签页，见下方 ``handle_external_send`` 等方法。

多标签页并发生成
----------------
每个标签页持有自己的 ``TaskManager`` 与气泡，因此多个标签页**可以同时生成回答**，
互不干扰。对外请求速率仍受进程级单例
:data:`src.core.network_worker.global_rate_limiter` 约束 —— 限速是全局的，
标签页（以及将来新增的功能）都共用同一份配额，不会因为"多了标签页"而翻倍。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from src.core import chat_session
from src.core.chat_archive import (DEFAULT_TAB_NAME, TAB_NAME_MAX,
                                  clean_tab_name)
from src.core.i18n import tr
from src.tools.base_tool import BaseTool
from src.ui.components.chat_tabs import ChatTabBar
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)


class ChatTool(BaseTool):
    """对话标签页容器（"对话助手"面板的根对象）。"""

    def __init__(self):
        super().__init__("Chat Assistant")
        self.widget: Optional[QWidget] = None
        self._tab_bar: Optional[ChatTabBar] = None
        self._stack: Optional[QStackedWidget] = None
        #: [{"id": str, "name": str, "conv": ChatConversation|None, "messages": list|None}]
        self._tabs: List[Dict[str, Any]] = []
        self._active_id: Optional[str] = None
        self._restored = False

        # 退出 / 崩溃前把标签页清单刷一遍（各标签页的会话由它们各自的钩子负责）
        chat_session.register_flush_hook(self.flush_tabs_manifest)

    # ------------------------------------------------------------------ #
    #  UI
    # ------------------------------------------------------------------ #
    def get_ui_widget(self) -> QWidget:
        if self.widget:
            return self.widget

        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tab_bar = ChatTabBar(self.widget)
        self._tab_bar.sig_new_tab.connect(self._on_new_tab_requested)
        self._tab_bar.sig_activated.connect(self.activate_tab)
        self._tab_bar.sig_close_requested.connect(self.close_tab)
        self._tab_bar.sig_renamed.connect(self.rename_tab)
        layout.addWidget(self._tab_bar, 0)

        self._stack = QStackedWidget(self.widget)
        layout.addWidget(self._stack, 1)

        self._restore_tabs_once()
        return self.widget

    def _on_new_tab_requested(self):
        self.add_tab()

    # ------------------------------------------------------------------ #
    #  标签页清单
    # ------------------------------------------------------------------ #
    def _display_tab_name(self, raw) -> str:
        """标签页名的**唯一**显示口径。

        核心层（快照清单 / 归档 / 迁移）用 :data:`DEFAULT_TAB_NAME` 这个英文常量表示
        "用户没命名过"，而界面要按当前语言显示；用户自己起的名字（包括改名后写回
        清单的名字）则原样保留。翻译只在这里发生一次，避免"新标签页显示默认对话、
        迁移来的却显示 Default Conversation"这类同一物两种说法。
        """
        name = clean_tab_name(raw)
        if not name or name == DEFAULT_TAB_NAME:
            return tr(DEFAULT_TAB_NAME)
        return name

    def _unique_tab_name(self, desired) -> str:
        """标签页名的**唯一化口径**：与现有名称重名时追加 `` (n)``（n 从 1 起）。

        新建、导入、改名、以及恢复清单都走这里，因此界面上永远不可能出现两个同名
        标签（导入两份同样叫"默认对话"的归档，得到的是"默认对话 (1)"、
        "默认对话 (2)"）。注意括号前有一个空格，与 ``TAB_NAME_MAX`` 的截断配合：
        先为后缀留出位置再截断，避免长名字被截回原名而重新撞名。
        """
        base = self._display_tab_name(desired)
        taken = {tab["name"] for tab in self._tabs}
        if base not in taken:
            return base

        number = 1
        while True:
            suffix = f" ({number})"
            candidate = clean_tab_name(base[: max(1, TAB_NAME_MAX - len(suffix))] + suffix)
            if candidate not in taken:
                logger.info("Tab name %r is taken; using %r instead.", base, candidate)
                return candidate
            number += 1

    def _restore_tabs_once(self):
        """首次建界面时按磁盘清单恢复标签页（只恢复清单，不做任何回灌）。"""
        if self._restored:
            return
        self._restored = True

        active, entries = chat_session.load_manifest()
        for entry in entries:
            # 唯一化也要覆盖"恢复"：老版本或手工改过的清单里可能已经存在重名
            name = self._unique_tab_name(entry["name"])
            self._tabs.append({"id": entry["id"], "name": name,
                               "conv": None, "messages": None})
            self._tab_bar.add_tab_item(entry["id"], name)

        if not self._tabs:
            # 全新用户：至少有一个标签页，界面永远不处于"没有标签页"的状态
            self.add_tab()
            return
        self.activate_tab(active or self._tabs[0]["id"])

    def _save_manifest(self):
        """把标签页清单（id / 名称 / 活动项）落盘。体积与标签页数相关，可频繁调用。"""
        if not self._tabs:
            return
        chat_session.save_manifest(self._active_id, self._tabs)

    def flush_tabs_manifest(self):
        """退出 / 崩溃前的兜底：保证清单与界面一致（钩子表统一触发）。"""
        self._save_manifest()

    def _find(self, tab_id: Optional[str]) -> Optional[Dict[str, Any]]:
        for tab in self._tabs:
            if tab["id"] == tab_id:
                return tab
        return None

    def tab_count(self) -> int:
        return len(self._tabs)

    # ------------------------------------------------------------------ #
    #  标签页操作
    # ------------------------------------------------------------------ #
    def add_tab(self, name=None, messages=None, activate: bool = True,
                tab_id: Optional[str] = None) -> str:
        """新建标签页并（默认）激活；返回标签页 id。

        :param messages: 非空时表示该标签页自带历史（导入 / 恢复场景），
            首次激活时直接回灌，不再读盘。
        :param name: 期望的名称；与现有标签重名时自动追加 `` (n)`` 编号
            （见 :meth:`_unique_tab_name`），因此新建 / 导入都拿不到重名标签。
        """
        tab_id = tab_id or chat_session.new_tab_id()
        display = self._unique_tab_name(name)
        tab = {
            "id": tab_id,
            "name": display,
            "conv": None,
            # 注意：空列表与"无自带历史"语义不同（前者是导入的空对话），统一收敛为 None
            "messages": list(messages) if messages else None,
        }
        self._tabs.append(tab)
        if self._tab_bar is not None:
            self._tab_bar.add_tab_item(tab_id, display)
        if tab["messages"]:
            # 自带历史的标签页（导入）立刻落盘：用户完全可能不激活它就直接退出，
            # 只留在内存里等于白丢一份刚导入的对话。
            chat_session.save_tab(tab_id, tab["messages"])
        if activate:
            self.activate_tab(tab_id)
        else:
            self._save_manifest()
        return tab_id

    def activate_tab(self, tab_id: str):
        """切换到指定标签页（懒加载：首次激活时构建控件并回灌历史）。"""
        tab = self._find(tab_id)
        if tab is None:
            logger.warning("Ignored activation of unknown tab id: %s", tab_id)
            return
        conv = self._ensure_conversation(tab)
        if conv is None:
            return
        self._stack.setCurrentWidget(conv.get_ui_widget())
        self._active_id = tab_id
        if self._tab_bar is not None:
            self._tab_bar.set_active(tab_id)
        self._save_manifest()

    def close_tab(self, tab_id: str):
        """关闭标签页：终止其生成、释放控件、**丢弃**其磁盘快照。

        始终保留至少一个标签页 —— "一个标签页都没有"的界面没有任何可用操作
        （没有输入框、没有加号的落点语义），因此最后一个标签页不允许关闭。
        """
        if len(self._tabs) <= 1:
            ToastManager().show(tr("At least one tab must remain open."), "warning")
            return
        tab = self._find(tab_id)
        if tab is None:
            return

        index = self._tabs.index(tab)
        self._tabs.remove(tab)

        conv = tab.get("conv")
        if conv is not None:
            try:
                conv.shutdown()
            except Exception as e:      # 关闭标签页失败不应连累其它标签页
                logger.error("Failed to shut down chat tab %s: %s", tab_id, e)
            widget = conv.get_ui_widget()
            if widget is not None:
                self._stack.removeWidget(widget)
                widget.setParent(None)
                widget.deleteLater()

        # 关闭 = 丢弃该标签页的对话：磁盘快照同步删除，下次启动不再恢复
        chat_session.clear_tab(tab_id)
        if self._tab_bar is not None:
            self._tab_bar.remove_tab_item(tab_id)

        if self._active_id == tab_id:
            nxt = self._tabs[min(index, len(self._tabs) - 1)]
            self._active_id = None
            self.activate_tab(nxt["id"])
        self._save_manifest()
        logger.info("Closed chat tab %s (%d tab(s) remaining).", tab_id, len(self._tabs))

    def rename_tab(self, tab_id: str, name: str):
        """重命名标签页。

        输入为空 = 放弃改名（把界面文本拉回当前名称），而不是把标签页改成默认名；
        名称未变化时同样只做界面复位。改成与别的标签重名时同样会拿到编号
        （"不能存在重名"对所有入口一视同仁）。
        """
        tab = self._find(tab_id)
        if tab is None:
            return
        if not clean_tab_name(name):
            if self._tab_bar is not None:
                self._tab_bar.set_name(tab_id, tab["name"])
            return
        # 先判"是否就是本标签页现在的名字"：否则一个本身带编号的名字（如 "对话 (1)"）
        # 会被自己占位而误判成重名，被加上第二层编号（"对话 (1) (1)"）。
        if self._display_tab_name(name) == tab["name"]:
            return
        display = self._unique_tab_name(name)
        tab["name"] = display
        if self._tab_bar is not None:
            self._tab_bar.set_name(tab_id, display)
        self._save_manifest()
        logger.info("Renamed chat tab %s to %r.", tab_id, display)

    def _ensure_conversation(self, tab: Dict[str, Any]):
        """取得（必要时构建）标签页对应的会话对象。"""
        conv = tab.get("conv")
        if conv is not None:
            return conv
        if self._stack is None:
            return None

        from src.tools.chat_conversation import ChatConversation

        conv = ChatConversation(tab["id"], self)
        self._stack.addWidget(conv.get_ui_widget())
        tab["conv"] = conv

        messages = tab.pop("messages", None)
        if messages is None:
            messages = chat_session.load_tab(tab["id"])
        if messages:
            # 延后一拍：先让本标签页完成首帧，再回灌历史（纯渲染，不发任何请求）
            QTimer.singleShot(0, lambda c=conv, m=messages: c.load_messages(m))
        return conv

    # ------------------------------------------------------------------ #
    #  导出 / 导入：供标签页内的 mixin 回调
    # ------------------------------------------------------------------ #
    def _tab_messages(self, tab: Dict[str, Any]) -> List[Dict[str, Any]]:
        """取某标签页的消息（**不**为它构建控件）。

        已打开过的标签页直接用内存里的历史；从未打开过的直接读磁盘快照 ——
        "导出全部"因此不需要先把每个标签页都渲染一遍。
        """
        conv = tab.get("conv")
        if conv is not None:
            return list(getattr(conv, "history", []) or [])
        messages = tab.get("messages")
        if messages is not None:
            return list(messages)
        return chat_session.load_tab(tab["id"])

    def collect_tabs_payload(self) -> List[Dict[str, Any]]:
        """汇总所有标签页的消息，供"导出全部标签页"写出无损归档。"""
        return [{"name": tab["name"], "messages": self._tab_messages(tab)}
                for tab in self._tabs]

    def import_tabs(self, tabs) -> int:
        """把导入的多个标签页各建成一个新标签页，返回实际创建的数量。

        刻意**不覆盖**任何现有对话：多标签页归档往往来自另一台机器 / 另一次会话，
        覆盖当前内容是不可逆的破坏。名称沿用归档里的（缺失时回落"默认对话"，
        解析侧已归一），首个新建标签页自动激活。
        """
        created = 0
        first: Optional[str] = None
        for raw in tabs or []:
            if not isinstance(raw, dict):
                continue
            messages = [m for m in (raw.get("messages") or []) if isinstance(m, dict)]
            if not messages:
                continue
            tab_id = self.add_tab(name=raw.get("name"), messages=messages, activate=False)
            if first is None:
                first = tab_id
            created += 1

        if first is not None:
            self.activate_tab(first)
        logger.info("Imported %d conversation(s) as new chat tab(s).", created)
        return created

    # ------------------------------------------------------------------ #
    #  对外统一入口：一律转发给当前活动标签页
    # ------------------------------------------------------------------ #
    def _active_conversation(self):
        tab = self._find(self._active_id) if self._active_id else None
        if tab is None and self._tabs:
            tab = self._tabs[0]
        if tab is None:
            return None
        return self._ensure_conversation(tab)

    def handle_external_send(self, context_text, prompt_text=""):
        conv = self._active_conversation()
        if conv is not None:
            conv.handle_external_send(context_text, prompt_text)

    def handle_external_send_with_mcp(self, context_text, prompt_text, target_tag):
        conv = self._active_conversation()
        if conv is not None:
            conv.handle_external_send_with_mcp(context_text, prompt_text, target_tag)

    def process_send(self, text, display_text=None):
        conv = self._active_conversation()
        if conv is not None:
            conv.process_send(text, display_text)

    def process_attached_files(self, items):
        conv = self._active_conversation()
        if conv is not None:
            conv.process_attached_files(items)

    def show_dev_note(self, text):
        conv = self._active_conversation()
        return conv.show_dev_note(text) if conv is not None else None

    def inject_dev_demo(self, user_text, ai_text, references=None):
        conv = self._active_conversation()
        if conv is None:
            return None
        return conv.inject_dev_demo(user_text, ai_text, references=references)
