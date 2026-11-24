"""Send flow mixin: query dispatch, AI response launch, edit-resend, external send.

拆分自 src/tools/chat_tool.py：负责把用户输入送入生成任务并管理生成生命周期。
"""
import logging
import os
import tempfile
from urllib.parse import quote

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from src.core.config_manager import ConfigManager
from src.core.core_task import TaskManager, TaskMode
from src.core.i18n import tr
from src.core.mcp_manager import MCPManager
from src.core.signals import GlobalSignals
from src.core.theme_manager import ThemeManager
from src.task.chat_tasks import ChatGenerationTask
from src.tools.chat_mixins.session import HISTORY_LOADING_TEXT
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)


class ChatSendFlowMixin:
    """发送链路：process_send -> start_ai_response -> (task events handled elsewhere)。"""

    def process_send(self, text, display_text=None):
        """发送用户消息 → 现代异步管线（线程执行，UI 不冻结）。

        Args:
            text: 送入 LLM 管线的完整文本。
            display_text: 气泡与历史展示用文本；协议哨兵消息（如 deep-plan
                确认）传纯文本以避免 UI 显示协议前缀。
        """
        # 0. 取消待刷新的附件预览定时器：同步链路（如开发者测试）在同一调用栈内
        #    attach -> send，若不取消，延迟回调会在发送完成后残留输入区预览。
        timer = getattr(self, '_attach_preview_timer', None)
        if timer is not None:
            timer.stop()

        # 0.5 Human-in-the-loop 等待作答阶段拦截一切通用发送（含追问、编辑重发等
        #     全部入口），防止与交互卡的作答流程互相干扰造成重复发送。
        if getattr(self, '_awaiting_user_input', False):
            ToastManager().show(
                tr("Please answer the pending question card above first "
                   "(or press Stop to dismiss it)."), "warning")
            self.logger.debug("Send blocked: turn is waiting for an interactive-card answer.")
            return

        # 0.6 历史回灌分片进行中拦一切发送（同 0.5 的口径：在这一处卡住所有入口）。
        #     回灌期间 self.history 只是"已重建的一部分"，此刻发送会把新消息挂在半截
        #     上下文上，后续分片还会继续往后追加，两边互相插队；桌面端此时盖着加载
        #     遮罩，等待本身就是预期行为。
        if getattr(self, '_history_rebuild_state', None) is not None:
            ToastManager().show(tr("The conversation is still loading. Please wait a moment."), "warning")
            self.logger.debug("Send blocked: history rebuild is still in progress.")
            return

        # 1. 自动文献库常开：不再使用用户手选的 KB。旧 KB 选择器已下线，
        #    这里固定 none，避免隐藏控件里残留的选择偷偷参与检索。
        kb_id = "none"

        # 4. 获取当前附件数据
        current_html = getattr(self, 'external_context_html', "")
        current_files = getattr(self, 'external_files', [])
        self.external_context_html = ""
        self.external_files = []

        image_files = [c for c in current_files if c.get("type") == "image"]

        # 仅附件（如只拖入图片）发送时补全默认提示语
        if not text.strip() and current_files:
            text = ("Please analyze the attached image(s): describe their content, "
                    "extract key information, and answer any implied questions.") \
                if image_files else "Please analyze the attached file(s)."

        if not text.strip():
            return

        # 5. UI 切换与历史记录管理
        self.input_container.btn_send.setVisible(False)
        self.input_container.btn_stop.setVisible(True)

        self.logger.info(f"User asked: {text[:50]}... (KB: {kb_id}) | Attached Files: {len(current_files)}")
        self.input_container.clear_text()

        # 将上下文的 HTML 链接渲染在气泡上方；图片以缩略图形式展示
        shown_text = display_text or text
        self.add_bubble(shown_text, is_user=True, context_html=current_html if current_html else None,
                        image_files=image_files)

        llm_text = text
        if current_files:
            context_block = "\n".join(
                [f"--- Attached {'Image' if c.get('type') == 'image' else 'File'}: {c['name']} ---"
                 for c in current_files]
            )
            llm_text = f"Context Info:\n{context_block}\n\nQuestion:\n{text}"

        self.history.append({
            "role": "user",
            "content": llm_text,
            "display_text": shown_text,
            "context_html": current_html if current_html else None,
            "external_files": current_files
        })
        # 轮次边界落盘：问题先写进快照，即使随后立刻崩溃，下次启动也能看到它
        self._autosave_session()

        self.input_container.hide_context_preview()
        self.external_files = current_files
        self.start_ai_response(kb_id)

    def _on_query_translated(self, translated_text):
        for i in range(self.chat_layout.count() - 1, -1, -1):
            item = self.chat_layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                if getattr(w, 'is_user', False):
                    if hasattr(w, 'add_translation_widget'):
                        w.add_translation_widget(translated_text)
                    else:
                        self.logger.warning("ChatBubbleWidget is missing 'add_translation_widget' method.")
                    break

    def _resolve_trans_config(self):
        """从配置读取翻译模型配置（Translator UI 已移除，改用存储的 provider/model）。

        与原先 ModelSelectorWidget.get_current_config 等价：读取 chat_trans_llm_id
        对应的 provider，并以 chat_trans_model_name_<provider> 记录的具体模型作为
        model_name。独立翻译往返默认关闭，仅当 chat_skip_translation_roundtrip=False
        时才真正需要此配置。
        """
        try:
            from src.core.config_manager import ConfigManager
            cm = ConfigManager()
            pid = cm.user_settings.get("chat_trans_llm_id")
            if not pid:
                return None
            for cfg in cm.load_llm_configs():
                if cfg.get("id") == pid:
                    out = dict(cfg)
                    m = cm.user_settings.get(f"chat_trans_model_name_{pid}")
                    if m:
                        out["model_name"] = str(m).strip()
                    return out
        except Exception:
            return None
        return None

    def _auto_deep_mode(self) -> bool:
        """自动判断是否需要深度研究；不再依赖用户勾选 Deep Mode。

        触发条件是"明确的综述/对比/多子问题"信号，普通单问单答仍走单 Agent。
        深度研究仍会先给计划卡确认，避免自动分解后跑偏。
        """
        try:
            msg = self.history[-1] if self.history else {}
            text = str(msg.get("display_text") or msg.get("content") or "")
        except Exception:
            return False
        low = text.lower()
        explicit = (
            "深度研究", "deep research", "系统综述", "systematic review",
            "literature review", "meta-analysis", "研究进展", "领域综述",
            "全面梳理", "多篇文献", "对比分析", "compare ", "comparison of",
        )
        if any(key in low for key in explicit):
            return True
        question_marks = text.count("?") + text.count("？")
        if len(text) >= 180 and (question_marks >= 2 or "；" in text or "\n-" in text):
            return True
        return False

    def start_ai_response(self, kb_id, requires_translation=False):
        main_config = self.model_selector.get_current_config()
        if not main_config:
            # 先拦住，而不是先建一个注定空的白气泡：没有 provider 时任务必然
            # 立刻以空正文结束，用户看到的就是"点了重新生成立刻 No response"。
            self.logger.error("Refused to start a turn: no main model provider configured.")
            ToastManager().show(
                tr("No model provider is configured. Pick one in Global Settings first."),
                "warning")
            return
        trans_config = self._resolve_trans_config()

        use_academic_agent = self.input_container.chk_academic_agent.isChecked() if hasattr(self.input_container,
                                                                                            'chk_academic_agent') else True
        use_external_tools = self.input_container.chk_external_tools.isChecked() if hasattr(self.input_container,
                                                                                            'chk_external_tools') else False
        # 不再读用户可见的 Deep Mode 开关：复杂/综述型问题自动升级为深度研究。
        deep_mode = self._auto_deep_mode()

        selected_tags = self.input_container.get_selected_tags()
        academic_tags = [t.replace("[ACADEMIC]", "").strip() for t in selected_tags if t.startswith("[ACADEMIC]")]
        external_names = [t.replace("[External]", "").strip() for t in selected_tags if t.startswith("[External]")]

        if use_academic_agent and not academic_tags:
            use_academic_agent = False

        if use_external_tools and not external_names:
            use_external_tools = False

        if main_config:
            actual_model = main_config.get("model_name", "").strip()
            self.logger.info(
                f" Starting AI response | Model: [{actual_model}] | Provider: [{main_config.get('name', 'Unknown')}]")

        # 初始化聊天气泡与 UI 状态
        self.current_ai_text = ""
        # 生成期间一律不展示引用标记：编号要等收尾按出现顺序定稿（见
        # ReferenceRegistry.renumber_by_appearance），中途露出的只是内部号。
        self._citations_hidden = True
        # 本轮 token 用量由 usage 事件回填；上一轮的值必须清掉，否则新气泡会带着
        # 上一轮的数字（收尾落历史时就会张冠李戴）。
        self._current_usage = None
        # 用户开始等待回答的这一刻，顺手把文献浏览器所需的 QtWebEngine 渲染进程
        # 预热起来：首次点 "Full text" 就不会再干等 3–6 秒初始化（与网速无关）。
        # 预热是幂等的，且不占启动内存；耗时放在用户本来就在等的时间段里。
        self.current_ai_bubble = self.add_bubble("", is_user=False)
        self.current_ai_bubble.set_loading(True)

        self.input_container.btn_send.setVisible(False)
        # 必须"真正禁用"而不只是隐藏：_emit_send() 唯一的拦截条件就是
        # btn_send.isEnabled()，只隐藏不禁用时生成过程中按回车会再次触发发送，
        # 新任务会 disconnect 掉正在运行任务的信号——旧任务继续消耗 token 却
        # 不再上屏，成为不可见也不可控的孤儿任务。
        self.input_container.set_send_locked(True, "Generating a response, please wait...")
        self.input_container.btn_stop.setVisible(True)
        self.input_container.btn_stop.setEnabled(True)
        self.input_container.btn_stop.setText(tr("Stop"))
        self.input_container.btn_stop.setToolTip("")
        self.set_controls_enabled(False)

        self._is_rendering_dirty = False
        self._render_timer.start()

        # Cleanly abort previous tasks if any exist
        if getattr(self, 'chat_task_mgr', None):
            try:
                self.chat_task_mgr.sig_progress.disconnect()
                self.chat_task_mgr.sig_state_changed.disconnect()
                self.chat_task_mgr.sig_result.disconnect()
            except Exception:
                pass
            self.chat_task_mgr.cancel_task()

        self.chat_task_mgr = TaskManager()
        self.chat_task_mgr.sig_progress.connect(self._on_chat_progress)
        self.chat_task_mgr.sig_state_changed.connect(self._on_chat_state_changed)
        self.chat_task_mgr.sig_result.connect(self._on_chat_result)

        try:
            self.input_container.btn_stop.clicked.disconnect()
        except Exception:
            pass
        self.input_container.btn_stop.clicked.connect(self.cancel_generation)

        GlobalSignals().sig_toast.connect(lambda msg, lvl: ToastManager().show(msg, lvl))

        current_external_files = getattr(self, 'external_files', [])

        QApplication.processEvents()

        def _launch_task():
            self.chat_task_mgr.start_task(
                ChatGenerationTask,
                task_id="chat_generation",
                # 长驻 GUI 进程内的 QThread：避免每轮对话在 Windows 上 spawn 新进程
                # 冷启动 + 全量 reimport（约 7s 固定开销）。ChatGenerationTask 以网络
                # IO 为主，与 api_server 同进程跑同一任务的方式一致，改动风险小。
                mode=TaskMode.THREAD,
                main_config=main_config,
                trans_config=trans_config,
                messages=list(self.history),
                kb_id=kb_id,
                conversation_id=self._tab_id(),
                requires_translation=requires_translation,
                external_files=current_external_files,
                use_academic_agent=use_academic_agent,
                academic_tags=academic_tags if use_academic_agent else [],
                use_external_tools=use_external_tools,
                external_tool_names=external_names if use_external_tools else [],
                deep_mode=deep_mode
            )

        QTimer.singleShot(100, _launch_task)

        self.external_files = []
        self.external_context_html = ""
        # hide_context_preview 会一并清空图片 / 文档芯片
        self.input_container.hide_context_preview()

    def handle_regenerate_last_turn(self, index: int = -1):
        """重新发起最后一次对话：撤销该轮已有回答，把同一问题再送一次。

        与"编辑 → 确认"的区别只在文本未变：前面的对话原封不动，因此**不重放历史**。
        走 :meth:`_retract_turn_and_resend` 只删掉该轮回答与它的气泡；若复用
        :meth:`handle_edit_resend`，用户会先看到"正在恢复上一次对话"的遮罩，整段
        历史被清掉重渲染一遍——那是编辑截断才需要的动作。

        :param index: 点击的那条用户消息在 ``history`` 里的下标（=-1 表示不校验来源）。
            入口只在最新一条用户消息上出现（与"编辑"同一条判据），但点击与新一轮开始
            之间可能夹着状态变化，故仍按下标核对一次，避免把当前轮次之外的内容重跑。
        """
        # 守卫顺序与 process_send 的闸口一致：库被改动 / 等待作答 / 历史回灌中是三条
        # 既有闸口，外加"正在生成"——这四态下重发要么破坏上下文，要么与新任务抢管线
        # （现有"编辑后再发送"没有最后一道，正是"旧任务变成孤儿继续烧 token"的来路）。
        # 四种忙碌态都不会显示入口（见 _sync_regenerate_available），这里的守卫是**竞态
        # 兜底**：点击已入队、随后用户又发了新问题，本次点击不该落在那条新任务上。
        if getattr(self, 'is_locked', False):
            ToastManager().show(
                tr("Cannot regenerate: the current library has been modified. "
                   "Please clear chat."), "warning")
            return
        if getattr(self, '_awaiting_user_input', False):
            ToastManager().show(
                tr("Please answer the pending question card above first "
                   "(or press Stop to dismiss it)."), "warning")
            return
        if getattr(self, '_history_rebuild_state', None) is not None:
            ToastManager().show(
                tr("The conversation is still loading. Please wait a moment."), "warning")
            return
        if getattr(self, 'current_ai_bubble', None) is not None:
            ToastManager().show(
                tr("Please wait for the current answer to finish first."), "warning")
            return

        last_user_idx = -1
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i].get('role') == 'user':
                last_user_idx = i
                break
        if index >= 0 and index != last_user_idx:
            self.logger.info("Regenerate ignored: message #%d is no longer the last question "
                             "(last is #%d).", index, last_user_idx)
            return
        text = self._user_display_text(self.history[last_user_idx]) if last_user_idx >= 0 else ""
        if not text:
            ToastManager().show(
                tr("Nothing to regenerate: the conversation has no question to re-send."),
                "warning")
            return

        self.logger.info("Regenerate requested: re-sending question of message #%d.",
                         last_user_idx)
        self._retract_turn_and_resend(last_user_idx)

    def _retract_turn_and_resend(self, last_user_idx: int) -> None:
        """撤销 ``last_user_idx`` 之后的回答并重发该问题。

        只动"最后一轮"：删掉该轮回答的历史条目与气泡，保留前面的对话与其引用缓存。
        新回答会复用被撤销气泡的编号，因此必须先清掉该编号下的引用条目，否则新回答
        的悬停卡会带上一轮已作废的来源。

        :param last_user_idx: 最后一轮用户消息在 ``history`` 中的下标。
        """
        self.remove_old_follow_ups()
        self.clear_follow_up_shelf()
        self.remove_bubbles_after(last_user_idx)
        dropped = len(self.history) - last_user_idx - 1
        del self.history[last_user_idx + 1:]

        from src.ui.components.citation_popup import CitationPopupController
        CitationPopupController.instance().replace_references([], last_user_idx + 1)

        self._structured_follow_ups = None
        self.current_ai_bubble = None
        # 附件仍属这一轮：任务侧按 external_files 取图，与"编辑后重发"共用同一口径。
        old_files = list(self.history[last_user_idx].get('external_files') or [])
        self.external_files = old_files
        if old_files:
            self._refresh_attachment_preview()

        self._sync_regenerate_available()
        # 先把"已回答到第几轮"落盘：本次撤销后若进程退出，快照不该还把旧回答带回来。
        self._autosave_session()
        self.logger.info("Regenerate: retracted %d message(s) after #%d.",
                         dropped, last_user_idx)
        self.start_ai_response("none")

    @staticmethod
    def _user_display_text(msg) -> str:
        """用户消息里"用户真正输入的那段文字"（气泡展示与重新发起都用它）。

        历史条目的 ``content`` 是送给模型的完整文本（附件上下文包在
        ``Context Info: …\\n\\nQuestion:\\n`` 里），``display_text`` 才是用户看到的原文。
        旧快照可能没有该字段，此时按同一分隔规则从 ``content`` 取回问题段——分隔符与
        :meth:`handle_edit_resend` 复用上下文前缀时用的完全一致。
        """
        if not isinstance(msg, dict):
            return ""
        text = str(msg.get('display_text') or '').strip()
        if text:
            return text
        content = str(msg.get('content') or '')
        marker = "\n\nQuestion:\n"
        return content.split(marker, 1)[1].strip() if marker in content else content.strip()

    def handle_edit_resend(self, index, new_text):
        if getattr(self, 'is_locked', False):
            ToastManager().show(tr("Cannot edit: The current library has been modified. Please clear chat."), "warning")
            old_msg = self.history[index]
            for i in range(self.chat_layout.count()):
                item = self.chat_layout.itemAt(i)
                if item and item.widget() and hasattr(item.widget(), 'index'):
                    if item.widget().index == index:
                        item.widget().set_content(
                            self._format_response(old_msg.get('display_text', old_msg['content']), index))
            return

        last_user_idx = -1
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i]['role'] == 'user':
                last_user_idx = i
                break

        if index != last_user_idx:
            ToastManager().show(tr("You can only edit your most recent message."), "warning")
            return

        old_msg = self.history[index]
        old_context_html = old_msg.get('context_html')
        old_files = old_msg.get('external_files', [])
        temp_history = list(self.history[:index])

        # 编辑重发同样走自动文献库，不再读已下线的 KB 选择器。
        kb_id = "none"
        old_images = [c for c in old_files if c.get("type") == "image"]

        llm_text = new_text
        if old_files:
            context_block = "\n".join(
                [f"--- Attached {'Image' if c.get('type') == 'image' else 'File'}: {c['name']} ---"
                 for c in old_files]
            )
            llm_text = f"Context Info:\n{context_block}\n\nQuestion:\n{new_text}"
        elif "Context Info:\n" in old_msg['content'] and "\n\nQuestion:\n" in old_msg['content']:
            context_part = old_msg['content'].split("\n\nQuestion:\n")[0]
            llm_text = f"{context_part}\n\nQuestion:\n{new_text}"

        def _resume_after_replay(_complete):
            # 重放收尾（_finish_history_rebuild）已经复位 _is_editing 并贴底一次；
            # 新气泡走"用户发送"同一条路径（add_bubble 延迟定位到该气泡顶端），
            # 与普通发送行为一致。
            self.add_bubble(new_text, is_user=True, context_html=old_context_html,
                            image_files=old_images)
            self.history.append({
                "role": "user",
                "content": llm_text,
                "display_text": new_text,
                "context_html": old_context_html,
                "external_files": old_files,
            })
            self.external_files = old_files
            # 附件保持挂载（供后续追问继续引用）：预览条由 attachments mixin 重建，
            # 避免此处重复展示逻辑。
            self._refresh_attachment_preview()
            self.start_ai_response(kb_id)

        # 重放改走分片回灌：同一条重放循环同步跑时，十几条气泡会连续冻结窗口约两秒
        # （日志里是一长串 "Bubble font applied"），期间动画停住、窗口无法重绘。
        # _apply_imported_history 用的正是"编辑重发同款"的重建步骤，只是按时间预算
        # 在片与片之间让出事件循环。
        self._apply_imported_history(
            temp_history, announce=False,
            progress_text=tr(HISTORY_LOADING_TEXT),
            on_done=_resume_after_replay)

    def handle_plot_plan_confirm(self, final_requirement: str):
        """Re-send the user-confirmed (English) plotting requirement to the AI.

        Triggered when a plot-plan confirmation card's "Confirm & Draw" button is
        clicked. The text is already English (translated by the card if needed);
        we inject a light framing hint so the model clearly renders the chart, then
        feed it through the normal send path.
        """
        if getattr(self, 'is_locked', False):
            ToastManager().show(tr("Cannot send: the current library has been modified. Please clear chat."), "warning")
            return
        if not final_requirement or not final_requirement.strip():
            ToastManager().show(tr("Empty plotting requirement."), "warning")
            return

        text = final_requirement.strip()
        self.logger.info(f"Plot plan confirmed -> sending render request: {text[:80]}")
        self.process_send(text)

    def handle_ask_user_submit(self, answer: str):
        """把用户对 ask_user 澄清卡的作答回灌为下一条用户消息。

        Agent 上一轮已通过 ask_user 工具暂停等待；此处走正常发送管线，
        模型凭历史中的工具调用即可将答案与其问题对上，继续任务。
        """
        if getattr(self, 'is_locked', False):
            ToastManager().show(tr("Cannot send: the current library has been modified. Please clear chat."), "warning")
            return
        answer = (answer or "").strip()
        if not answer:
            ToastManager().show(tr("Empty answer."), "warning")
            return
        # 卡片作答即本轮继续：解除等待锁定，走正常发送管线。
        self._awaiting_user_input = False
        self.input_container.set_send_locked(False)
        self.logger.info(f"Ask-user answer -> resuming agent: {answer[:80]}")
        self.process_send(answer)

    def handle_deep_plan_confirm(self, plan_text: str):
        """用户确认深度研究计划：以哨兵文本重进管线，跳过分解直接执行。

        Args:
            plan_text: 卡片中编辑后的编号计划文本（一行一个子问题）。
        """
        if getattr(self, 'is_locked', False):
            ToastManager().show(tr("Cannot send: the current library has been modified. Please clear chat."), "warning")
            return
        plan_text = (plan_text or "").strip()
        if not plan_text:
            ToastManager().show(tr("The plan is empty."), "warning")
            return
        # 计划确认即本轮继续：解除等待锁定。
        self._awaiting_user_input = False
        self.input_container.set_send_locked(False)
        self.logger.info(
            f"Deep plan confirmed -> executing ({len(plan_text.splitlines())} sub-task line(s))")
        # 协议前缀 [DEEP_PLAN_CONFIRMED] 仅在 LLM 文本中传递；气泡展示纯计划。
        self.process_send(f"[DEEP_PLAN_CONFIRMED]\n{plan_text}", display_text=plan_text)

    def handle_deep_plan_skip(self, original_query: str):
        """用户跳过深度拆解：本轮按普通单 Agent 直接作答。"""
        if getattr(self, 'is_locked', False):
            ToastManager().show(tr("Cannot send: the current library has been modified. Please clear chat."), "warning")
            return
        original_query = (original_query or "").strip()
        if not original_query:
            return
        # 跳过拆解即本轮继续：解除等待锁定。
        self._awaiting_user_input = False
        self.input_container.set_send_locked(False)
        self.logger.info("Deep plan skipped -> answering directly without decomposition.")
        self.process_send(f"[DEEP_PLAN_SKIPPED]\n{original_query}", display_text=original_query)

    def cancel_generation(self):
        if not self.input_container.btn_stop.isEnabled():
            return

        # Human-in-the-loop 等待作答阶段的强制终止：任务早已结束，
        # 此处仅解除等待锁定并恢复常规对话状态，不弹取消确认框。
        if getattr(self, '_awaiting_user_input', False):
            self._awaiting_user_input = False
            self.input_container.set_send_locked(False)
            self.input_container.btn_stop.setVisible(False)
            self.input_container.btn_send.setVisible(True)
            # 解除等待即本轮被用户终止：与其余出口一样重算"重新发起"入口
            self._sync_regenerate_available()
            self.logger.info("Waiting state dismissed by user; normal chat restored.")
            ToastManager().show(tr("Waiting dismissed. You can chat normally now."), "info")
            return

        self.input_container.btn_stop.setEnabled(False)
        self.input_container.btn_stop.setText(tr("Stopping..."))

        if hasattr(self, '_render_timer'):
            self._render_timer.stop()
            self._is_rendering_dirty = False

        if getattr(self, 'chat_task_mgr', None):
            self.chat_task_mgr.cancel_task()

        if self.current_ai_bubble:
            self.current_ai_bubble.set_loading(False)

        self.logger.info("AI generation cancellation requested by user. Task manager is gracefully terminating.")
        # 取消同样属于收尾动作：停在用户当前阅读位置，不强制拉到底部
        self.scroll_to_bottom(force=False)

    def _trigger_follow_up(self, text):
        if getattr(self, 'is_locked', False):
            # 与上面四处统一为同一译文键（原大小写不一致会造成键分裂）
            ToastManager().show(tr("Cannot send: the current library has been modified. Please clear chat."), "warning")
            return
        self.process_send(text)

    def _edit_follow_up(self, text):
        if getattr(self, 'is_locked', False):
            ToastManager().show(tr("Cannot edit: The current library has been modified. Please clear chat."), "warning")
            return
        self.input_container.set_text(text)

    def _show_slow_connection_warning(self):
        if self.current_ai_bubble and getattr(self, '_is_waiting_llm', False):
            idx = getattr(self.current_ai_bubble, 'index', -1)
            tm = ThemeManager()
            base_html = self._format_response(self.current_ai_text.lstrip(), idx)
            self.current_ai_bubble.set_content(
                base_html +
                f"<br><div style='color:{tm.color('accent')};'><i>Still connecting...</i></div>"
                f"<div style='color:{tm.color('warning')}; font-size:12px; margin-top:5px; "
                f"padding:8px; border:1px solid {tm.color('warning')}; border-radius:4px;'>"
                "Warning: The connection is taking longer than expected. Please check your <b>Network Proxy</b> or <b>API Endpoint (URL)</b>."
                "</div>"
            )
            # 提示同样只在用户已位于底部时跟随，避免打断上翻阅读
            self.scroll_to_bottom(force=False)

    def handle_external_send_with_mcp(self, context_text, prompt_text, target_tag):
        self.get_ui_widget()

        if hasattr(self, 'input_container'):
            if hasattr(self.input_container, 'chk_external_tools'):
                if not self.input_container.chk_external_tools.isChecked():
                    self.input_container.chk_external_tools.setChecked(True)
            elif hasattr(self.input_container, 'chk_mcp_enable'):
                if not self.input_container.chk_mcp_enable.isChecked():
                    self.input_container.chk_mcp_enable.setChecked(True)

        config_mgr = ConfigManager()
        available_tags = MCPManager.get_instance().get_available_mcp()

        deselected = set(self.config.mcp_servers.get("deselected_mcp_tags", []))
        for tag in available_tags:
            if tag.lower() == target_tag.lower():
                deselected.discard(tag)
            else:
                deselected.add(tag)

        self.config.mcp_servers["deselected_mcp_tags"] = list(deselected)
        self.config.save_mcp_servers()

        if hasattr(self, 'input_container') and hasattr(self.input_container, 'refresh_mcp_tags'):
            self.input_container.refresh_mcp()

        self.handle_external_send(context_text, prompt_text)

    def handle_external_send(self, context_text, prompt_text=""):
        self.get_ui_widget()

        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, "scholar_navis_external_context.txt")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(context_text)
        except Exception as e:
            self.logger.error(f"Failed to write temp external context: {e}")

        self.external_files = [{
            "path": temp_path,
            "name": "External Context.txt",
            "page": 1,
            "content": context_text
        }]

        safe_path = quote(temp_path)
        safe_name = quote("External Context.txt")
        link = f"cite://view?path={safe_path}&page=1&name={safe_name}"

        preview_text = context_text[:80].replace('\n', ' ') + "..."
        self.external_context_html = (
            f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' "
            f"style='color:{ThemeManager().color('accent')}; text-decoration:none;'>"
            f"📄 {preview_text} (Click to read more)</a></div>")

        if hasattr(self, 'input_container'):
            if hasattr(self.input_container, 'chk_external_tools'):
                if not self.input_container.chk_external_tools.isChecked():
                    self.input_container.chk_external_tools.setChecked(True)
            elif hasattr(self.input_container, 'chk_mcp_enable'):
                if not self.input_container.chk_mcp_enable.isChecked():
                    self.input_container.chk_mcp_enable.setChecked(True)

        if prompt_text:
            self.process_send(prompt_text)
        else:
            self.input_container.set_text("Please summarize this content and extract key insights.")
            self.input_container.show_context_preview("External Information (RSS/Web)")
