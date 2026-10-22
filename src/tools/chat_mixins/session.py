"""Chat session mixin: 每个标签页的自动保存。

持久化口径（路径、格式、节流、半条合并、清理语义、标签页清单）集中在
:mod:`src.core.chat_session`；本文件只回答两个时序问题：**什么时候存**。

"什么时候恢复"归标签页容器（:class:`src.tools.chat_tool.ChatTool`）：一个标签页的
会话要等它第一次被激活、控件真正建出来之后才回灌，因此这里只提供
:meth:`ChatSessionMixin.load_messages` 这一处"把消息灌进面板"的入口。
"""

import logging

from src.core.i18n import tr
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)


class ChatSessionMixin:
    """把当前标签页的对话持续落盘（口径见 src/core/chat_session.py）。"""

    # ---------- 保存 ----------
    def _tab_id(self) -> str:
        """本会话所属标签页 id（由容器在构造时注入）。"""
        return str(getattr(self, "tab_id", "") or "")

    def _snapshot_writable(self) -> bool:
        """当前是否允许把 ``self.history`` 写回磁盘快照。

        历史回灌（:meth:`~src.tools.chat_mixins.attachments.ChatAttachmentsMixin.
        _apply_imported_history`）是分片推进的，期间 ``self.history`` 只是"已重建的
        一部分"；此刻落盘会用截断的会话覆盖磁盘上那份完整快照——而回灌的数据正是从
        它读出来的。宁可这次不写，也不要写坏。
        """
        if getattr(self, "_history_rebuild_state", None) is not None:
            logger.info("Skip session snapshot: history rebuild is still in progress.")
            return False
        return True

    def _autosave_session(self):
        """轮次边界：覆盖落盘本标签页的完整会话，并丢弃流式半条（它已并入 history）。"""
        from src.core import chat_session
        history = getattr(self, "history", []) or []
        if not history or not self._snapshot_writable():
            return
        self._sync_all_bubble_cards()
        # 远程图片可能在本轮结束之后才下载完成：落盘前统一对账一次，归档 /
        # 快照才能把它们一起内嵌。
        sync_images = getattr(self, "_sync_all_bubble_remote_images", None)
        if callable(sync_images):
            sync_images()
        if chat_session.save_tab(self._tab_id(), history):
            chat_session.clear_inflight(self._tab_id())

    def _autosave_inflight(self):
        """流式期间刷新半条回答。

        调用点是渲染节拍（约 10~16 Hz），因此这里不做任何判断——节流与截断都在
        :func:`src.core.chat_session.save_inflight` 内完成，避免两处各定一套频率。
        """
        text = getattr(self, "current_ai_text", "") or ""
        if not text.strip():
            return
        from src.core import chat_session
        chat_session.save_inflight(self._tab_id(), text)

    def _forget_autosaved_session(self):
        """用户清空本标签页对话：落盘快照一并删除，下次启动不再恢复。"""
        from src.core import chat_session
        tab_id = self._tab_id()
        chat_session.clear_tab(tab_id)

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
        # 空快照**不**清除既有值：气泡里没有卡片并不能证明历史上没有过组件
        # （正文标记可能缺失、渲染可能失败、此刻也可能尚未重建完成），而
        # ui_components 是"用户当初选了什么"的唯一载体——删掉就再也找不回来。
        # 多留一条对不上号的快照是无害的（回灌按 (kind, key) 匹配，配不上就忽略）。
        self._autosave_session()

    def _sync_all_bubble_cards(self):
        """按布局对账：把所有气泡的组件状态同步回各自的 ``history`` 条目。

        卡片交互一般会即时回写（见 :meth:`_on_bubble_cards_changed`），但并非每张卡
        都带状态信号（例如错误面板的折叠状态），且界面上的组件才是"当前真实状态"。
        因此每次落盘前对账一遍：退出兜底与轮次边界都拿到当前状态，而不是上次同步的旧值。

        **只覆盖、不清除**（与 :meth:`_on_bubble_cards_changed` 同一条规则）：空快照
        不构成"历史上没有组件"的证据，据此删除会让用户当初的作答永久消失。
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

    def flush_session_snapshot(self):
        """退出 / 崩溃前的兜底落盘（由 ``MainWindow.closeEvent`` 与全局异常处理器触发）。

        与轮次边界保存的差别在于：流式进行中时历史里还没有那条 AI 消息，必须把
        ``current_ai_text`` 一并写进半条文件，否则崩溃会丢掉整段正在生成的回答。
        """
        from src.core import chat_session
        if getattr(self, "widget", None) is None:
            return
        if not self._snapshot_writable():
            return

        history = getattr(self, "history", []) or []
        if history:
            # 先对账再落盘：界面上的组件状态才是"当前真实状态"
            self._sync_all_bubble_cards()
            sync_images = getattr(self, "_sync_all_bubble_remote_images", None)
            if callable(sync_images):
                sync_images()
            chat_session.save_tab(self._tab_id(), history)

        text = getattr(self, "current_ai_text", "") or ""
        if text.strip() and getattr(self, "current_ai_bubble", None) is not None:
            chat_session.save_inflight(self._tab_id(), text, force=True)

    # ---------- 恢复 ----------
    def load_messages(self, messages):
        """把已保存 / 导入的消息回灌进本标签页（由容器在控件就绪后调用）。

        **只渲染不发送**：重放复用导入历史同款逻辑（``_apply_imported_history``），
        全程不触碰 ``start_ai_response``，因此不会重复请求或重复计费。
        """
        messages = list(messages or [])
        if not messages or getattr(self, "history", None):
            return
        try:
            # ask_user / deep-plan 的交互卡是 UI 组件，无法随消息恢复；若不复位这个
            # 标志，输入框会被永久锁在"等待作答"状态。
            self._awaiting_user_input = False
            # 回灌是分片推进的（大历史可达数秒），期间盖一层加载遮罩：否则用户看到的
            # 就是"切过来就卡住"。
            self._apply_imported_history(
                messages, announce=False,
                progress_text=tr("Restoring the last conversation..."),
                on_done=lambda success: self._on_session_restored(messages, success))
        except Exception:
            logger.exception("Failed to restore chat session for tab %s.", self._tab_id())

    def _on_session_restored(self, messages, success):
        """启动恢复的收尾（``_apply_imported_history`` 的 ``on_done`` 回调）。

        单条消息渲染失败不影响整轮恢复，但必须让用户知道"看到的不完整"——静默降级
        会让人以为对话本身就只有这些内容。
        """
        logger.info("Restored chat tab %s: %d message(s) (all_rendered=%s)",
                    self._tab_id(), len(messages), success)
        if not success:
            ToastManager().show(tr("Some messages could not be rendered."), "warning")
            return
        ToastManager().show(
            tr("Restored the last conversation ({n} messages).").format(n=len(messages)),
            "info")
