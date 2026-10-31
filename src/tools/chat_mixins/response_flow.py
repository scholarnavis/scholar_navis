"""Response flow mixin: streaming render, task events, finish/error handling.

拆分自 src/tools/chat_tool.py：负责 AI 输出的流式渲染、任务事件分发与收尾状态恢复。
"""
import logging
import re

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from src.core.core_task import TaskState
from src.core.follow_ups import split_follow_ups
from src.core.i18n import tr
from src.core.theme_manager import ThemeManager
from src.ui.components.dialog import StandardDialog
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)

#: 任务框架下发的通用引导文案（见 core_task.BackgroundTask.run）。它们只表示
#: "任务已开始"，并非模型正文；若写进正文会把动态加载指示器顶成静态文字，
#: 让人误以为卡死。这里直接忽略，交由气泡的动态加载指示器按阶段轮换提示词。
_BOOTSTRAP_PLACEHOLDERS = {
    "Initializing...",
    "Initializing",
}


class ChatResponseFlowMixin:
    """流式输出：token -> bubble -> finish / error 状态机。"""

    def set_controls_enabled(self, enabled: bool):
        """锁定或解锁对话控制区的关键配置"""
        if hasattr(self, 'model_selector'):
            self.model_selector.setEnabled(enabled)
        if hasattr(self, 'trans_selector'):
            self.trans_selector.setEnabled(enabled)
        if hasattr(self, 'combo_kb'):
            self.combo_kb.setEnabled(enabled)

        if hasattr(self, 'input_container'):
            if hasattr(self.input_container, 'chk_external_tools'):
                self.input_container.chk_external_tools.setEnabled(enabled)
            elif hasattr(self.input_container, 'chk_mcp_enable'):
                self.input_container.chk_mcp_enable.setEnabled(enabled)

            if hasattr(self.input_container, 'chk_academic_agent'):
                self.input_container.chk_academic_agent.setEnabled(enabled)

            # Deep Mode 与上面两个开关同属"轮次级配置"：三者必须一起禁用，
            # 否则界面会出现"同类控件两个灰、一个可点"的不一致，用户还会以为
            # 中途切换能改变正在跑的这轮（实际只对下一轮生效）。
            if hasattr(self.input_container, 'chk_deep_mode'):
                self.input_container.chk_deep_mode.setEnabled(enabled)

            if hasattr(self.input_container, 'btn_mcp_tags'):
                self.input_container.btn_mcp_tags.setEnabled(enabled)

            if hasattr(self.input_container, 'btn_clear'):
                self.input_container.btn_clear.setEnabled(enabled)
            if hasattr(self.input_container, 'btn_attach'):
                self.input_container.btn_attach.setEnabled(enabled)
            if hasattr(self.input_container, 'btn_export'):
                self.input_container.btn_export.setEnabled(enabled)
            if hasattr(self.input_container, 'btn_import'):
                self.input_container.btn_import.setEnabled(enabled)

    def _throttled_render(self):
        # 渲染节拍器也挂在会话对象上：关闭标签页后不得再往已释放的气泡里写（见 _closed）。
        if self._closed:
            return
        if getattr(self, '_is_rendering_dirty', False) and self.current_ai_bubble:
            self._is_rendering_dirty = False
            idx = getattr(self.current_ai_bubble, 'index', -1)
            # 渲染前先分离追问块：流式期间 suggestions 也不进入正文，避免闪烁
            # log_success=False：流式期间同一文本会被反复拆分，成功日志仅由最终渲染打印
            split = split_follow_ups(
                self.current_ai_text.lstrip(), log_success=False
            )
            self._pending_follow_ups = split.questions
            self.current_ai_bubble.set_content(
                self._format_response(split.main_text + split.cites_html, idx)
            )
            # 半条落盘：与渲染同节拍（10~16 Hz）调用，写盘频率由 core 侧节流控制。
            # 崩溃时这一份让"正在生成的那段"也能回来，而不是只剩上一轮。
            self._autosave_inflight()

            # "是否还在底部"由 is_at_bottom 一处判定（默认阈值即此处原值），
            # 不再就地重算一遍公式。
            if self.is_at_bottom():
                self.scroll_to_bottom()

    def update_ai_bubble(self, token):
        if not self.current_ai_bubble or token == "":
            return
        # 阈值比默认更紧（原值 15）：只判断"用户是否还贴着底"来决定要不要把视图拽下来，
        # 公式本身仍由 is_at_bottom 定义，避免同一件事出现第二处算法。
        is_at_bottom = self.is_at_bottom(threshold=15)
        idx = getattr(self.current_ai_bubble, 'index', -1)

        # 0. 任务框架的通用引导文案（"Initializing..." 等）不是模型正文：写入
        #    正文会把动态加载指示器顶成静态文字，而这句固定文案本身正是"看着
        #    像卡死"的来源。直接忽略，让指示器继续按阶段轮换提示词。
        if token.strip() in _BOOTSTRAP_PLACEHOLDERS:
            return

        if token == "[CLEAR_SEARCH]":
            self.current_ai_text = re.sub(
                r"<div class=['\"]status-msg['\"].*?>.*?</div>\s*(?:<br>\s*)*(?:\n)*",
                '',
                self.current_ai_text,
                flags=re.DOTALL | re.IGNORECASE
            )
            self.current_ai_text = re.sub(
                r'(?:<br>\s*)*<i>(?:🌐\s*|📚\s*)?(?:Translating|Loading|Filtering|Extracting|\[Low VRAM).*?</i>\s*(?:<br>\s*)*(?:\n)*',
                '',
                self.current_ai_text,
                flags=re.DOTALL | re.IGNORECASE
            )
            self.current_ai_text = self.current_ai_text.lstrip()
            self._is_rendering_dirty = True
            return

        # 2. Handle LLM connection start
        if token == "[START_LLM_NETWORK]":
            self._is_waiting_llm = True
            # 用动态指示器的阶段文案代替静态的"连接中"提示：等待 provider 建连
            # 期间仍保留跳动圆点，不会出现"卡住的静态文字"；该文案在首个真实
            # token 到达时随 set_loading(False) 自动清除，不污染正文。
            setter = getattr(self.current_ai_bubble, "set_loading_caption", None)
            if setter is not None:
                setter("Contacting the model")
            else:
                self.current_ai_bubble.set_content(
                    self._format_response(self.current_ai_text.lstrip(), idx) +
                    f"<br><div style='color:{ThemeManager().color('accent')};'>"
                    f"<i>Connecting to LLM provider, please wait...</i></div>"
                )
            self.slow_conn_timer = QTimer(self)
            self.slow_conn_timer.setSingleShot(True)
            self.slow_conn_timer.timeout.connect(self._show_slow_connection_warning)
            self.slow_conn_timer.start(8000)
            if is_at_bottom:
                self.scroll_to_bottom()
            return

        # 3. Stop waiting and clear timer once real content arrives
        if getattr(self, '_is_waiting_llm', False):
            self._is_waiting_llm = False
            if hasattr(self, 'slow_conn_timer'):
                self.slow_conn_timer.stop()
            if self.current_ai_bubble.is_loading:
                self.current_ai_bubble.set_loading(False)

        # 只追加到分段缓冲，不在这里做 O(N) 的字符串拼接；真正 join 发生在
        # 下一次渲染/落盘/收尾读取 current_ai_text 时（见 ChatConversation 属性）。
        parts = getattr(self, "_ai_text_parts", None)
        if parts is None:
            self.current_ai_text = token
        else:
            parts.append(token)
            self._ai_text_len = getattr(self, "_ai_text_len", 0) + len(token)
        self._is_rendering_dirty = True

        # 长文本渲染降频：markdown 全量重排（split_follow_ups + format_response
        # + QTextBrowser 重排）随文本长度线性变贵，固定 60ms 间隔在长回答
        # 后期会产生可感知卡顿；按已累计长度自适应放宽节流间隔。
        n = getattr(self, "_ai_text_len", 0)
        if n > 96000:
            interval = 800
        elif n > 48000:
            interval = 400
        elif n > 24000:
            interval = 240
        elif n > 12000:
            interval = 160
        elif n > 6000:
            interval = 100
        else:
            interval = 60
        if hasattr(self, '_render_timer') and self._render_timer.interval() != interval:
            self._render_timer.setInterval(interval)

    def _format_response(self, text, index, hide_citations=None):
        """统一代理给 TextFormatter，保持内部调用无需修改。

        :param hide_citations: ``None`` 表示按当前阶段自动判定——生成中隐藏引用
            标记（编号尚未定稿），收尾换文后展示；历史重渲染则按"这条消息有没有
            引用列表"显式传入，避免出现有编号却没有列表可对的状态。
        """
        from src.ui.components.text_formatter import TextFormatter
        if not hasattr(self, 'mermaid_codes'):
            self.mermaid_codes = {}
        if hide_citations is None:
            hide_citations = bool(getattr(self, "_citations_hidden", False))

        return TextFormatter.format_response(
            text, index,
            getattr(self, 'expanded_thinks', set()),
            getattr(self, 'user_toggled_thinks', set()),
            self.mermaid_codes,
            hide_citations=hide_citations,
        )

    def _on_chat_progress(self, progress, msg):
        if progress == -1:
            self.update_ai_bubble(msg)

    def _on_chat_state_changed(self, state, msg):
        if state == TaskState.SUCCESS.value:
            self.on_chat_finished(is_cancelled=False)
        elif state == TaskState.FAILED.value:
            self.on_chat_error(msg)
        elif state == TaskState.TERMINATED.value:
            self.on_chat_finished(is_cancelled=True)

    def _on_chat_result(self, payload):
        if isinstance(payload, dict) and payload.get("event") == "translated":
            self._on_query_translated(payload.get("text"))
        elif isinstance(payload, dict) and payload.get("event") == "usage":
            # 本次生成任务的 token 用量 + 本轮耗时：显示在当前 AI 气泡下方。
            # 同时暂存到 _current_usage，收尾时随历史条目一起落盘——否则重启、
            # 切换标签页或导入归档后，计数器会整片消失。
            stats = {
                "prompt_tokens": int(payload.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(payload.get("completion_tokens", 0) or 0),
                "estimated": bool(payload.get("estimated", False)),
                "elapsed_ms": payload.get("elapsed_ms"),
            }
            self._current_usage = stats
            bubble = getattr(self, "current_ai_bubble", None)
            if bubble is not None and hasattr(bubble, "set_token_stats"):
                bubble.set_token_stats(
                    stats["prompt_tokens"], stats["completion_tokens"],
                    estimated=stats["estimated"], elapsed_ms=stats["elapsed_ms"])
        elif isinstance(payload, dict) and payload.get("event") == "follow_ups":
            # 结构化追问建议（suggest_follow_ups 工具产出）：缓存到收尾时统一渲染，
            # 优先于 on_chat_finished 里的文本解析兜底。
            self._structured_follow_ups = payload.get("data") or []
        elif isinstance(payload, dict) and payload.get("event") == "ask_user":
            # Human-in-the-loop 提问卡：结构化通道直达渲染，绕过文本管线。
            # 同时进入等待作答状态：锁定通用发送直到卡片提交或强制终止。
            self._awaiting_user_input = True
            bubble = getattr(self, "current_ai_bubble", None)
            if bubble is not None and hasattr(bubble, "attach_ask_user_card"):
                bubble.attach_ask_user_card(payload.get("data") or {})
        elif isinstance(payload, dict) and payload.get("event") == "references":
            # 参考文献结构化数据（cite_references 工具产出）：同步进引用信息存储，
            # 供正文 [n] 的悬停卡 / 详情面板查询著录与支撑原文。
            data = payload.get("data") or []
            bubble = getattr(self, "current_ai_bubble", None)
            msg_index = getattr(bubble, "index", -1) if bubble is not None else -1
            from src.ui.components.citation_popup import CitationPopupController
            CitationPopupController.instance().merge_references(data, msg_index)
            logger.debug("Reference data synced to citation popup store: %d item(s) for message #%s.",
                         len(data), msg_index)
        elif isinstance(payload, dict) and payload.get("event") == "answer_final":
            # 正文收口：任务端已把编号按出现顺序定稿、并渲染好参考列表。这一刻之前
            # 上屏的是"隐去引用标记"的中间态，从这里开始才显示编号与列表。
            text = payload.get("text") or ""
            if text:
                self.current_ai_text = text
                self._citations_hidden = False
                self._is_rendering_dirty = True
                self._throttled_render()
            data = payload.get("references") or []
            bubble = getattr(self, "current_ai_bubble", None)
            msg_index = getattr(bubble, "index", -1) if bubble is not None else -1
            from src.ui.components.citation_popup import CitationPopupController
            # 定稿条目是权威集合：整条替换，避免生成期按内部号合并的旧键残留，
            # 进而被 _attach_references 一起写进历史。
            CitationPopupController.instance().replace_references(data, msg_index)
            logger.debug("Final answer applied: %d chars, %d cited reference(s) "
                         "for message #%s.", len(text), len(data), msg_index)
        elif isinstance(payload, dict) and payload.get("event") == "await_user":
            # deep-plan 等待确认：同样进入等待状态锁定通用发送。
            self._awaiting_user_input = True
        elif isinstance(payload, dict) and payload.get("event") == "fulltext_request":
            # 取全文：任务线程正挂起等这份 PDF（本轮尚未结束）。
            self._open_fulltext_dialog(dict(payload.get("data") or {}))

    # ------------------------------------------------------------------ 取全文

    def _open_fulltext_dialog(self, data: dict):
        """准备取全文窗口并发系统通知；**不抢焦点、不切换当前窗口**。

        任务线程已经等在 :mod:`src.core.fulltext_bridge` 上，用户交回 PDF 后本轮从
        原处继续，已检索到的来源、计划与引用注册表都不丢。窗口以
        ``WA_ShowWithoutActivating`` 在后台打开（用户在任务栏能看到它），同时发一条
        带应用名与图标的系统通知；只有用户点通知时，才由
        :meth:`_on_fulltext_notification_clicked` 把它拉到前台。
        """
        conversation_id = str(self._tab_id() or "")
        if not conversation_id:
            logger.warning("fulltext_request event carries no conversation id; picker skipped.")
            return
        from PySide6.QtCore import Qt

        from src.ui.components.fulltext_request import FullTextRequestDialog

        self._close_fulltext_dialog()
        logger.info("Preparing the full-text picker in the background (conversation=%s).",
                    conversation_id)
        # 本类（ChatConversation）是 QObject（BaseTool），不能当 QDialog 的父窗口；
        # 与其它对话内弹窗一致，父窗口取对话页面控件（事件到达时界面早已建好）。
        dialog = FullTextRequestDialog(getattr(self, "widget", None), data=data,
                                       conversation_id=conversation_id)
        dialog.setAttribute(Qt.WA_ShowWithoutActivating, True)
        dialog.finished.connect(
            lambda *_, d=dialog: self._on_fulltext_dialog_finished(d))
        self._fulltext_dialog = dialog
        self._fulltext_conversation_id = conversation_id
        dialog.show()
        self._notify_fulltext_request(data, conversation_id)

    def _notify_fulltext_request(self, data: dict, conversation_id: str):
        """发一条"需要全文"的系统通知（文案全部走 i18n，见 locales.py）。"""
        from src.core.version import __app_name__
        from src.ui.components.system_notify import system_notifier

        notifier = system_notifier()
        if not getattr(self, "_fulltext_notify_hooked", False):
            notifier.activated.connect(self._on_fulltext_notification_clicked)
            self._fulltext_notify_hooked = True

        title = str(data.get("title") or "").strip()
        doi = str(data.get("doi") or "").strip()
        url = str(data.get("url") or "").strip()
        reason = str(data.get("reason") or "").strip()

        lines = [tr("Paper: {paper}").format(
            paper=title or doi or url or tr("(no title provided)"))]
        if doi:
            lines.append(f"DOI: {doi}")
        if url:
            # 没有 DOI 的样例（部分 RSS 条目）只有落地页：把链接写进通知，用户才知道
            # 该去哪儿下载它。
            lines.append(tr("Link: {url}").format(url=url))
        # 与窗口同一口径：模型写的 reason 优先，其次是自动升级带来的 note
        # （如"自动下载没有成功（出版社 403）"）。
        detail = (tr("Needed for: {reason}").format(reason=reason) if reason else
                  str(data.get("note") or "").strip())
        if detail:
            lines.append(detail)
        lines.append(tr("Download the PDF, then hand it over in the Scholar Navis window."))
        body = "\n".join(lines)

        notified = notifier.notify(f"{__app_name__} · {tr('Full text needed')}", body,
                                   notification_id=conversation_id, timeout_ms=20000)
        if not notified:
            # 没有系统通知通道时至少给一条应用内提示：否则窗口静静等在后台，
            # 用户既没看到通知、也不知道这一轮正卡在等他选文件。
            logger.info("No system notification channel; the picker waits in the background.")
            ToastManager().show(tr("The full-text picker is waiting for your PDF."), "info")
        logger.debug("Full-text notification for conversation %s delivered=%s.",
                     conversation_id, notified)

    def _on_fulltext_notification_clicked(self, notification_id: str):
        """用户点了系统通知：这时才把取全文窗口拉到前台（焦点由用户主动触发）。"""
        dialog = getattr(self, "_fulltext_dialog", None)
        if dialog is None:
            return
        current = str(getattr(self, "_fulltext_conversation_id", "") or "")
        if notification_id and current and notification_id != current:
            logger.debug("Notification click ignored: belongs to another conversation.")
            return
        logger.info("Bringing the full-text picker to the front on user request.")
        dialog.showNormal()
        dialog.raise_()
        dialog.activateWindow()

    def _on_fulltext_dialog_finished(self, dialog):
        """``finished`` 回调：只清掉"这一个"窗口（旧窗口迟到不应误关新窗口）。"""
        if getattr(self, "_fulltext_dialog", None) is dialog:
            self._close_fulltext_dialog()

    def _close_fulltext_dialog(self):
        """窗口已交付/取消：清掉引用与通知状态，避免再次被拉到前台。"""
        dialog = getattr(self, "_fulltext_dialog", None)
        self._fulltext_dialog = None
        self._fulltext_conversation_id = ""
        if dialog is not None:
            try:
                dialog.deleteLater()
            except Exception:      # 窗口可能已随父控件销毁
                pass
        try:
            from src.ui.components.system_notify import system_notifier

            system_notifier().dismiss()
        except Exception as e:
            logger.debug("Notification dismiss skipped: %s", e)

    def on_chat_finished(self, is_cancelled=False):
        # 本轮已结束：取全文窗口不再有等待方，留着只会让用户选完才发现没人接。
        if getattr(self, "_fulltext_dialog", None) is not None:
            self._close_fulltext_dialog()
        if hasattr(self, '_render_timer'):
            self._render_timer.stop()
        self.set_controls_enabled(True)

        if getattr(self, '_is_rendering_dirty', False):
            self._throttled_render()

        if not self.current_ai_bubble:
            return

        self.input_container.btn_stop.setText(tr("Stop"))
        self.input_container.btn_stop.setEnabled(True)
        awaiting = getattr(self, '_awaiting_user_input', False)

        # Stop 按钮旧连接统一断开；等待作答分支将其重连为强制终止出口。
        try:
            self.input_container.btn_stop.clicked.disconnect()
        except Exception:
            pass

        if is_cancelled:
            self._awaiting_user_input = False
            self.input_container.btn_stop.setVisible(False)
            self.input_container.btn_send.setVisible(True)
            # 发送按钮在生成期间被真正禁用（防止回车重入），取消路径必须解锁。
            self.input_container.set_send_locked(False)

            if self.current_ai_bubble:
                self.current_ai_bubble.is_interrupted = True
            StandardDialog(self.widget, tr("Task Cancelled"),
                           tr("The AI generation has been stopped by the user."),
                           show_cancel=False).exec()
            # 刻意不回填输入框：被停止的问题本身已作为用户气泡留在对话里，
            # 再塞回输入框会让用户以为"没发出去"，还会连带恢复上一轮附件预览，
            # 导致同一问题被重复发送。需要重发时用用户气泡上的编辑/重发入口
            # （ChatSendFlowMixin.handle_edit_resend）。

            # 中途停止没有参考文献列表可渲染：正文里的引用标记一并抹掉，历史与
            # 屏幕保持一致（不留下"有编号、点开却是空卡"的残迹）。
            entry = {"role": "assistant",
                     "content": self._without_citation_markers(self.current_ai_text),
                     "status": "interrupted"}
            self._attach_usage(entry)
            self._attach_ui_cards(entry)
            self.history.append(entry)
            # 轮次边界落盘（取消也算一轮结束，半条已并入历史）
            self._autosave_session()
            self.current_ai_bubble = None
            # 用户停止也是一轮的出口：到这一刻"重新发起"入口才该出现
            self._sync_regenerate_available()
            self.scroll_to_bottom(force=False)
            return

        if awaiting:
            # Human-in-the-loop 暂停（ask_user / deep-plan 卡等待操作）：
            # 本轮对话尚未真正结束，通用输入框保持锁定防止重复发送；
            # Stop 保留为强制终止出口（cancel_generation 解除等待状态）。
            self.input_container.btn_send.setVisible(False)
            self.input_container.btn_stop.setVisible(True)
            self.input_container.set_send_locked(True)
            self.input_container.btn_stop.clicked.connect(self.cancel_generation)
            self.logger.info(
                "Turn paused for user input; general send locked until the pending card is answered or dismissed.")
        else:
            self.input_container.btn_stop.setVisible(False)
            self.input_container.btn_send.setVisible(True)
            self.input_container.set_send_locked(False)

        if awaiting and not self.current_ai_text.strip():
            # Human-in-the-loop 暂停：提问卡 / 深度计划卡由**结构化事件**直接写进
            # 气泡，正文本身就是空的。这里若继续走下面的收尾，会把那张卡改写成
            # "No response."——用户既看不到问题，也没有入口作答，表现为"点了重新
            # 生成立刻变成 No response"（自动深度模式会走到这条路径）。
            self.logger.info(
                "Turn paused for user input with an empty text body; keeping the "
                "rendered card untouched.")
            if self.current_ai_bubble and self.current_ai_bubble.is_loading:
                self.current_ai_bubble.set_loading(False)
            self.current_ai_bubble = None
            self._sync_regenerate_available()
            self.scroll_to_bottom(force=False)
            return

        if self.current_ai_bubble and self.current_ai_bubble.is_loading:
            self.current_ai_bubble.set_loading(False)

        full_text = self.current_ai_text

        # 统一分离：正文 / 追问块 / 引用块（与流式渲染共用同一逻辑）
        split = split_follow_ups(full_text)
        self.current_ai_text = split.main_text + split.cites_html
        self.logger.debug(
            "Follow-up split finished: main=%d chars, questions=%d, cites=%d chars.",
            len(split.main_text), len(split.questions), len(split.cites_html),
        )

        idx = getattr(self.current_ai_bubble, 'index', -1)
        if self.current_ai_text.strip():
            final_html = self._format_response(self.current_ai_text, idx)
            if not str(final_html or "").strip():
                # 有原文却渲染出空 HTML（典型是整轮只产出被展示侧剥离的工具调用文本）：
                # 退回纯文本，至少让用户看到这一轮到底输出了什么，而不是一个空气泡。
                self.logger.warning(
                    "Formatted answer is empty though %d chars of raw text exist "
                    "(index=%s); rendering the raw text instead.",
                    len(self.current_ai_text), idx)
                from html import escape as _escape
                final_html = f"<pre>{_escape(self.current_ai_text)}</pre>"
        else:
            # 空回答：这是程序侧的问题（模型没产出 / 被展示侧过滤干净），必须留日志
            # 并给出可读提示，不能只画一个空白气泡。
            self.logger.warning(
                "Turn finished with an empty answer (history=%d turns, raw=0 chars).",
                len(getattr(self, 'history', []) or []))
            final_html = tr("No response.")
        self.current_ai_bubble.set_content(final_html)
        # 流式已结束：强制收敛气泡高度（中途态可能把高度写成偏大值且因幂等缓存不回落）
        if hasattr(self.current_ai_bubble, 'force_resync_height'):
            self.current_ai_bubble.force_resync_height()

        # 追问建议：优先使用结构化产出（suggest_follow_ups 工具）；缺失时回退文本解析
        # （历史消息重渲染、非 Agent 模式等兼容路径）。
        structured_questions = getattr(self, '_structured_follow_ups', None)
        self._structured_follow_ups = None
        questions = structured_questions or split.questions
        if questions:
            self.render_follow_up_buttons(questions)

        entry = {"role": "assistant", "content": self.current_ai_text}
        self._attach_references(entry)
        self._attach_usage(entry)
        self._attach_ui_cards(entry)
        self.history.append(entry)
        # 轮次边界落盘：回答完整收尾，半条文件随之作废
        self._autosave_session()
        self.current_ai_bubble = None
        self._sync_regenerate_available()   # 回答结束：本轮的重新发起入口此刻出现
        self.logger.info("AI response generation finished and UI updated.")

    @staticmethod
    def _citations_hidden_for(msg) -> bool:
        """历史消息是否应隐藏引用标记：没有引用列表就没有可点的编号。

        生成期的隐藏由 :attr:`_citations_hidden` 控制；重放历史消息时改用这条
        判据——旧记录里可能只剩 ``[n]`` 而条目早已丢失，显示出来只会是空卡。
        """
        return not bool((msg or {}).get("references"))

    @staticmethod
    def _without_citation_markers(text):
        """抹掉正文里的引用标记（取消 / 报错出口用）。

        这两种出口不会生成参考文献列表，编号也就没有落点；留着标记只会让用户
        点到一张空卡。抹除口径与生成期隐藏一致，见
        :func:`src.core.references.strip_citation_markers`。
        """
        from src.core.references import strip_citation_markers
        return strip_citation_markers(text or "")

    def _attach_references(self, entry: dict):
        """把当前 AI 气泡的引用条目写入历史条目（导出无损留存的唯一写入点）。

        引用条目此前只存在于 :class:`CitationPopupController` 的内存缓存里，
        导出再导入后便会全部丢失（正文 ``[n]`` 仍在，悬停却只剩占位文案）。
        故在收尾落历史时按气泡编号取出并挂到该条 ``references`` 字段上：
        ``ExportChatTask`` 的无损导出会原样序列化，导入 / 编辑重发再整批回灌。
        """
        bubble = getattr(self, "current_ai_bubble", None)
        idx = getattr(bubble, "index", -1) if bubble is not None else -1
        try:
            from src.ui.components.citation_popup import CitationPopupController
            refs = CitationPopupController.instance().references_for(idx)
        except Exception as e:  # pragma: no cover - 纯防御
            self.logger.debug("Reference snapshot for message #%s skipped: %s", idx, e)
            return
        if refs:
            entry["references"] = refs
            self.logger.debug("Attached %d reference(s) to history message #%s.",
                              len(refs), idx)

    def _attach_usage(self, entry: dict):
        """把本轮 token 用量写入历史条目（读数持久化的唯一写入点）。

        计数器原本只活在气泡的按钮行里：重启应用、切换标签页、导出再导入之后
        一条都对不上。用量是这一轮回答的属性，应与正文、引用一样随历史保存。
        """
        usage = getattr(self, "_current_usage", None)
        if isinstance(usage, dict) and usage:
            entry["usage"] = dict(usage)

    def _restore_message_usage(self, bubble, msg):
        """把历史条目里的 token 用量回灌到重建后的气泡（:meth:`_attach_usage` 的读取侧）。"""
        usage = (msg or {}).get("usage") if isinstance(msg, dict) else None
        if bubble is None or not isinstance(usage, dict):
            return
        setter = getattr(bubble, "set_token_stats", None)
        if not callable(setter):
            return
        try:
            setter(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                   estimated=bool(usage.get("estimated", False)),
                   elapsed_ms=usage.get("elapsed_ms"))
        except Exception as e:      # 纯展示，失败不得影响历史回灌
            logger.debug("Token stats restore skipped for message #%s: %s",
                         getattr(bubble, "index", -1), e)

    def _attach_ui_cards(self, entry: dict):
        """把当前 AI 气泡内组件的交互结果写入历史条目（自动保存 / 导出共用的写入点）。

        正文标记只记录"生成时的事实"，用户在这些卡片里做了什么（选了哪些选项、
        是否确认绘图方案、计划被改成什么）只存在于内存里。这里在收尾落历史时导出，
        启动恢复与归档导入再整批回灌并冻结为只读组件。
        """
        bubble = getattr(self, "current_ai_bubble", None)
        snapshots_of = getattr(bubble, "card_snapshots", None)
        if bubble is None or not callable(snapshots_of):
            return
        try:
            snapshots = snapshots_of()
        except Exception as e:      # pragma: no cover - 纯防御
            self.logger.warning("Card snapshot skipped for message #%s: %s",
                                getattr(bubble, "index", -1), e)
            return
        if snapshots:
            entry["ui_components"] = snapshots
            self.logger.debug("Attached %d UI card state(s) to history message #%s.",
                              len(snapshots), getattr(bubble, "index", -1))

    @staticmethod
    def _build_error_marker(msg):
        """把任务端传回的错误转换为统一错误面板标记。

        - 任务端结构化 JSON（{"title","body","details"}）原样转为面板
          payload（details 缺省时回退为 title+body）；
        - 非结构化文本（程序自身异常）包装为通用 payload：首行摘要进入
          正文，完整原文进入折叠详情。

        所有报错最终都渲染为同一样式的 ErrorPanelWidget（含可折叠的
        Technical Details 技术详情栏），保证美术样式全局一致。

        :return: ``<error_panel data="...">`` 标记字符串。
        """
        import json as _json

        from src.core.llm_errors import error_marker, friendly_payload

        data = None
        if isinstance(msg, str) and msg.strip().startswith("{"):
            try:
                parsed = _json.loads(msg)
                if isinstance(parsed, dict) and "body" in parsed:
                    data = {
                        "title": str(parsed.get("title", "Generation Error")),
                        "body": str(parsed.get("body", "")),
                        "details": str(parsed.get("details", "") or
                                       f"{parsed.get('title', '')}\n{parsed.get('body', '')}"),
                    }
            except ValueError:
                data = None
        if data is None:
            raw = str(msg or "").strip()
            data = friendly_payload("Generation Error", raw.split("\n", 1)[0][:300], details=raw)
        return error_marker(data)

    def on_chat_error(self, msg):
        """处理对话任务抛出的异常，恢复 UI 状态并展示错误"""
        if hasattr(self, '_render_timer'):
            self._render_timer.stop()
        self.set_controls_enabled(True)

        self.input_container.btn_stop.setText(tr("Stop"))
        self.input_container.btn_stop.setEnabled(True)
        if getattr(self, '_awaiting_user_input', False):
            # 提问卡已渲染但本轮后续流程失败：保持等待锁定，
            # 用户仍可在卡片上作答，或点 Stop 解除等待状态。
            self.input_container.btn_send.setVisible(False)
            self.input_container.btn_stop.setVisible(True)
            self.input_container.set_send_locked(True)
        else:
            self.input_container.btn_stop.setVisible(False)
            self.input_container.btn_send.setVisible(True)
            # 报错路径同样要解锁发送（生成期间按钮被真正禁用）。
            self.input_container.set_send_locked(False)

        if self.current_ai_bubble:
            self.current_ai_bubble.set_loading(False)
            self.current_ai_bubble.is_interrupted = True

        # 统一错误面板：任务端结构化 JSON 与程序自身异常都转换为
        # <error_panel> 标记，由气泡渲染为同一样式的错误组件（含折叠详情）。
        error_marker = self._build_error_marker(msg)

        if self.current_ai_text.strip():
            self.current_ai_text += "\n\n" + error_marker
        else:
            self.current_ai_text = error_marker

        # 渲染到气泡
        if self.current_ai_bubble:
            idx = getattr(self.current_ai_bubble, 'index', -1)
            final_html = self._format_response(self.current_ai_text, idx)
            self.current_ai_bubble.set_content(final_html)

        # 记录到历史避免上下文结构断裂。报错同样没有参考文献列表，引用标记一并
        # 抹掉，避免历史里留下查不到著的编号。
        entry = {
            "role": "assistant",
            "content": self._without_citation_markers(self.current_ai_text),
            "status": "error"
        }
        self._attach_usage(entry)
        self._attach_ui_cards(entry)
        self.history.append(entry)
        # 轮次边界落盘：报错同样是一轮结束，历史与快照保持一致
        self._autosave_session()

        self.current_ai_bubble = None
        self._sync_regenerate_available()   # 意外结束同样是一轮的出口
        # 完整原始错误（含 JSON payload 中的 details）写入日志
        self.logger.error("Chat task failed.\n%s", msg)
        ToastManager().show(tr("Generation failed due to an error."), "error")
        # 报错同样不强制拉回底部（错误已在气泡内呈现）
        self.scroll_to_bottom(force=False)
