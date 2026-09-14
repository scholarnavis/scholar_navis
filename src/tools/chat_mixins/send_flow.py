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
from src.core.mcp_manager import MCPManager
from src.core.signals import GlobalSignals
from src.core.theme_manager import ThemeManager
from src.task.chat_tasks import ChatGenerationTask
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
                "Please answer the pending question card above first "
                "(or press Stop to dismiss it).", "warning")
            self.logger.debug("Send blocked: turn is waiting for an interactive-card answer.")
            return

        # 1. 获取并格式化 KB ID
        kb_data = self.combo_kb.currentData()
        kb_id = kb_data.get("id") if isinstance(kb_data, dict) else kb_data
        if not kb_id:
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

        self.input_container.hide_context_preview()
        self.external_files = current_files
        self.start_ai_response(kb_id)

    def _restore_last_input(self):
        last_user_msg = None
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i]['role'] == 'user':
                last_user_msg = self.history[i]
                break

        if last_user_msg:
            self.input_container.set_text(last_user_msg.get('display_text', ''))

            files = last_user_msg.get('external_files', [])
            html = last_user_msg.get('context_html', '')

            self.external_files = list(files) if files else []
            self.external_context_html = html if html else ""

            if self.external_files:
                names = []
                for c in self.external_files:
                    if c['name'] not in names:
                        names.append(c['name'])
                display_text = f"{names[0]}, {names[1]} and {len(names) - 2} more" if len(names) > 2 else ", ".join(
                    names)
                self.input_container.show_context_preview(display_text)
                self._sync_image_thumbs()
            else:
                self.input_container.hide_context_preview()
                self._sync_image_thumbs()

    def _sync_image_thumbs(self):
        """将当前待发送附件中的图片同步到输入区预览条。"""
        image_files = [c for c in getattr(self, 'external_files', []) if c.get("type") == "image"]
        if hasattr(self.input_container, 'set_image_thumbs'):
            self.input_container.set_image_thumbs(image_files)

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

    def start_ai_response(self, kb_id, requires_translation=False):
        main_config = self.model_selector.get_current_config()
        trans_config = self._resolve_trans_config()

        use_academic_agent = self.input_container.chk_academic_agent.isChecked() if hasattr(self.input_container,
                                                                                            'chk_academic_agent') else True
        use_external_tools = self.input_container.chk_external_tools.isChecked() if hasattr(self.input_container,
                                                                                            'chk_external_tools') else False
        deep_mode = self.input_container.chk_deep_mode.isChecked() if hasattr(self.input_container,
                                                                              'chk_deep_mode') else False

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
        self.current_ai_bubble = self.add_bubble("", is_user=False)
        self.current_ai_bubble.set_loading(True)

        self.input_container.btn_send.setVisible(False)
        self.input_container.btn_stop.setVisible(True)
        self.input_container.btn_stop.setEnabled(True)
        self.input_container.btn_stop.setText("Stop")
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
        self.input_container.hide_context_preview()
        if hasattr(self.input_container, 'set_image_thumbs'):
            self.input_container.set_image_thumbs([])

    def handle_edit_resend(self, index, new_text):
        if getattr(self, 'is_locked', False):
            ToastManager().show("Cannot edit: The current library has been modified. Please clear chat.", "warning")
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
            ToastManager().show("You can only edit your most recent message.", "warning")
            return

        old_msg = self.history[index]
        old_context_html = old_msg.get('context_html')
        old_files = old_msg.get('external_files', [])

        self.history = self.history[:index]

        v_bar = self.scroll_area.verticalScrollBar()
        current_scroll = v_bar.value()
        self._is_editing = True

        self.clear_layout(self.chat_layout)
        temp_history = list(self.history)
        self.history = []

        for msg in temp_history:
            display_text = msg.get('display_text', msg['content'])
            ctx_html = msg.get('context_html')
            msg_images = [c for c in msg.get('external_files', []) if c.get("type") == "image"]
            self.add_bubble(display_text, is_user=(msg['role'] == 'user'), context_html=ctx_html,
                            image_files=msg_images)
            self.history.append(msg)

        kb_data = self.combo_kb.currentData()
        kb_id = kb_data.get("id") if isinstance(kb_data, dict) else kb_data

        old_images = [c for c in old_files if c.get("type") == "image"]
        self.add_bubble(new_text, is_user=True, context_html=old_context_html, image_files=old_images)

        QApplication.processEvents()
        v_bar.setValue(current_scroll)
        self._is_editing = False

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

        self.history.append({
            "role": "user",
            "content": llm_text,
            "display_text": new_text,
            "context_html": old_context_html,
            "external_files": old_files
        })

        self.external_files = old_files
        # 附件保持挂载（供后续追问继续引用）：同步输入区预览条与图片芯片
        if old_files:
            names = []
            for c in old_files:
                if c.get('type') != 'image' and c['name'] not in names:
                    names.append(c['name'])
            display_text = (f"{names[0]}, {names[1]} and {len(names) - 2} more"
                            if len(names) > 2 else (", ".join(names) if names else f"{len(old_files)} attachment(s)"))
            self.input_container.show_context_preview(display_text)
        self._sync_image_thumbs()
        self.start_ai_response(kb_id)

    def handle_plot_plan_confirm(self, final_requirement: str):
        """Re-send the user-confirmed (English) plotting requirement to the AI.

        Triggered when a plot-plan confirmation card's "Confirm & Draw" button is
        clicked. The text is already English (translated by the card if needed);
        we inject a light framing hint so the model clearly renders the chart, then
        feed it through the normal send path.
        """
        if getattr(self, 'is_locked', False):
            ToastManager().show("Cannot send: the current library has been modified. Please clear chat.", "warning")
            return
        if not final_requirement or not final_requirement.strip():
            ToastManager().show("Empty plotting requirement.", "warning")
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
            ToastManager().show("Cannot send: the current library has been modified. Please clear chat.", "warning")
            return
        answer = (answer or "").strip()
        if not answer:
            ToastManager().show("Empty answer.", "warning")
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
            ToastManager().show("Cannot send: the current library has been modified. Please clear chat.", "warning")
            return
        plan_text = (plan_text or "").strip()
        if not plan_text:
            ToastManager().show("The plan is empty.", "warning")
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
            ToastManager().show("Cannot send: the current library has been modified. Please clear chat.", "warning")
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
            self.logger.info("Waiting state dismissed by user; normal chat restored.")
            ToastManager().show("Waiting dismissed. You can chat normally now.", "info")
            return

        self.input_container.btn_stop.setEnabled(False)
        self.input_container.btn_stop.setText("Stopping...")

        if hasattr(self, '_render_timer'):
            self._render_timer.stop()
            self._is_rendering_dirty = False

        if getattr(self, 'chat_task_mgr', None):
            self.chat_task_mgr.cancel_task()

        if self.current_ai_bubble:
            self.current_ai_bubble.set_loading(False)

        self.logger.info("AI generation cancellation requested by user. Task manager is gracefully terminating.")
        self.scroll_to_bottom()

    def _trigger_follow_up(self, text):
        if getattr(self, 'is_locked', False):
            ToastManager().show("Cannot send: The current library has been modified. Please clear chat.", "warning")
            return
        self.process_send(text)

    def _edit_follow_up(self, text):
        if getattr(self, 'is_locked', False):
            ToastManager().show("Cannot edit: The current library has been modified. Please clear chat.", "warning")
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
            self.scroll_to_bottom()

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
