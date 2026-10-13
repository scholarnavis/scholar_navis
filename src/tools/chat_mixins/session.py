"""Chat session mixin: 关闭自动保存 + 启动自动恢复。

持久化口径（路径、格式、节流、半条合并、清理语义）集中在
:mod:`src.core.chat_session`；本文件只回答两个时序问题：**什么时候存**、**什么时候恢复**。
"""

import logging

from src.core.i18n import tr
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)


class ChatSessionMixin:
    """把当前对话持续落盘，并在下次构建面板时回灌（口径见 src/core/chat_session.py）。"""

    # ---------- 保存 ----------
    def _autosave_session(self):
        """轮次边界：覆盖落盘完整会话，并丢弃流式半条（它已并入 history）。"""
        from src.core import chat_session
        history = getattr(self, "history", []) or []
        if not history:
            return
        self._sync_all_bubble_cards()
        if chat_session.save_session(history):
            chat_session.clear_inflight()

    def _autosave_inflight(self):
        """流式期间刷新半条回答。

        调用点是渲染节拍（约 10~16 Hz），因此这里不做任何判断——节流与截断都在
        :func:`src.core.chat_session.save_inflight` 内完成，避免两处各定一套频率。
        """
        text = getattr(self, "current_ai_text", "") or ""
        if not text.strip():
            return
        from src.core import chat_session
        chat_session.save_inflight(text)

    def _forget_autosaved_session(self):
        """用户清空对话：快照一并删除，下次启动不再恢复（"没清理过才恢复"的实现点）。"""
        from src.core import chat_session
        chat_session.clear_session()

    def _on_bubble_cards_changed(self, bubble):
        """组件交互落定：把该气泡的组件状态回写所属历史条目并立即落盘。

        卡片常常在本轮回答**已经落历史之后**才被作答（ask_user / deep_plan 的典型
        时序：本轮先结束并等待用户），所以不能只在收尾时快照一次，必须在状态变化时
        同步覆盖，否则"用户选了什么"永远进不了快照。
        """
        idx = getattr(bubble, "index", -1)
        history = getattr(self, "history", None) or []
        if not (0 <= idx < len(history)):
            return
        snapshot_of = getattr(bubble, "card_snapshots", None)
        if not callable(snapshot_of):
            return
        try:
            snapshots = snapshot_of()
        except Exception as e:      # pragma: no cover - 纯防御
            logger.warning("Card snapshot failed for message #%s: %s", idx, e)
            return

        if snapshots:
            history[idx]["ui_components"] = snapshots
        else:
            history[idx].pop("ui_components", None)
        self._autosave_session()

    def _sync_all_bubble_cards(self):
        """按布局对账：把所有气泡的组件状态同步回各自的 ``history`` 条目。

        卡片交互一般会即时回写（见 :meth:`_on_bubble_cards_changed`），但并非每张卡
        都带状态信号（例如错误面板的折叠状态），且界面上的组件才是"当前真实状态"。
        因此每次落盘前对账一遍：退出兜底与轮次边界都拿到当前状态，而不是上次同步的旧值。
        """
        layout = getattr(self, "chat_layout", None)
        history = getattr(self, "history", None) or []
        if layout is None:
            return
        for i in range(layout.count()):
            item = layout.itemAt(i)
            bubble = item.widget() if item is not None else None
            idx = getattr(bubble, "index", -1)
            snapshot_of = getattr(bubble, "card_snapshots", None)
            if not callable(snapshot_of) or not (0 <= idx < len(history)):
                continue
            try:
                snapshots = snapshot_of()
            except Exception as e:      # pragma: no cover - 纯防御
                logger.warning("Card sync failed for message #%s: %s", idx, e)
                continue
            if snapshots:
                history[idx]["ui_components"] = snapshots
            else:
                history[idx].pop("ui_components", None)

    def flush_session_snapshot(self):
        """退出 / 崩溃前的兜底落盘（由 ``MainWindow.closeEvent`` 与全局异常处理器触发）。

        与轮次边界保存的差别在于：流式进行中时历史里还没有那条 AI 消息，必须把
        ``current_ai_text`` 一并写进半条文件，否则崩溃会丢掉整段正在生成的回答。
        """
        from src.core import chat_session
        if getattr(self, "widget", None) is None:
            return

        history = getattr(self, "history", []) or []
        if history:
            # 先对账再落盘：界面上的组件状态才是"当前真实状态"
            self._sync_all_bubble_cards()
            chat_session.save_session(history)

        text = getattr(self, "current_ai_text", "") or ""
        if text.strip() and getattr(self, "current_ai_bubble", None) is not None:
            chat_session.save_inflight(text, force=True)

    # ---------- 恢复 ----------
    def _restore_autosaved_session(self):
        """启动恢复：把上次对话回灌进面板。

        只在"面板刚构建、当前对话为空"时执行一次，且**只渲染不发送**——重放复用
        导入历史同款逻辑（``_apply_imported_history``），全程不触碰
        ``start_ai_response``，因此不会重复请求或重复计费。
        """
        if getattr(self, "_session_restore_done", False):
            return
        self._session_restore_done = True
        if getattr(self, "history", None):
            return      # 面板上已有内容（例如用户已抢先发问），不覆盖

        from src.core import chat_session
        messages = chat_session.load_session()
        if not messages:
            return

        try:
            # ask_user / deep-plan 的交互卡是 UI 组件，无法随消息恢复；若不复位这个
            # 标志，输入框会被永久锁在"等待作答"状态。
            self._awaiting_user_input = False
            self._apply_imported_history(messages, announce=False)
            logger.info("Restored last chat session: %d message(s)", len(messages))
            ToastManager().show(
                tr("Restored the last conversation ({n} messages).").format(n=len(messages)),
                "info")
        except Exception:
            logger.exception("Failed to restore last chat session.")
