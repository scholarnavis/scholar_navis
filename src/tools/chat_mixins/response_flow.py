"""Response flow mixin: streaming render, task events, finish/error handling.

拆分自 src/tools/chat_tool.py：负责 AI 输出的流式渲染、任务事件分发与收尾状态恢复。
"""
import logging
import re

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from src.core.core_task import TaskState
from src.ui.components.dialog import StandardDialog
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)


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

            if hasattr(self.input_container, 'btn_mcp_tags'):
                self.input_container.btn_mcp_tags.setEnabled(enabled)

            if hasattr(self.input_container, 'btn_clear'):
                self.input_container.btn_clear.setEnabled(enabled)
            if hasattr(self.input_container, 'btn_attach'):
                self.input_container.btn_attach.setEnabled(enabled)

    def _throttled_render(self):
        if getattr(self, '_is_rendering_dirty', False) and self.current_ai_bubble:
            self._is_rendering_dirty = False
            idx = getattr(self.current_ai_bubble, 'index', -1)
            self.current_ai_bubble.set_content(self._format_response(self.current_ai_text.lstrip(), idx))

            sb = self.scroll_area.verticalScrollBar()
            if (sb.maximum() - sb.value()) <= 50:
                self.scroll_to_bottom()

    def update_ai_bubble(self, token):
        if not self.current_ai_bubble:
            return
        sb = self.scroll_area.verticalScrollBar()
        is_at_bottom = (sb.maximum() - sb.value()) <= 15
        idx = getattr(self.current_ai_bubble, 'index', -1)

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
            base_html = self._format_response(self.current_ai_text.lstrip(), idx)
            self.current_ai_bubble.set_content(
                base_html +
                "<br><div style='color:#05B8CC;'><i>Connecting to LLM provider, please wait...</i></div>"
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

        self.current_ai_text += token
        self._is_rendering_dirty = True

    def _format_response(self, text, index):
        """统一代理给 TextFormatter，保持内部调用无需修改"""
        from src.ui.components.text_formatter import TextFormatter
        if not hasattr(self, 'mermaid_codes'):
            self.mermaid_codes = {}

        return TextFormatter.format_response(
            text, index,
            getattr(self, 'expanded_thinks', set()),
            getattr(self, 'user_toggled_thinks', set()),
            self.mermaid_codes
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

    def on_chat_finished(self, is_cancelled=False):
        if hasattr(self, '_render_timer'):
            self._render_timer.stop()
        self.set_controls_enabled(True)

        if getattr(self, '_is_rendering_dirty', False):
            self._throttled_render()

        if not self.current_ai_bubble:
            return

        self.input_container.btn_stop.setText("Stop")
        self.input_container.btn_stop.setEnabled(True)
        self.input_container.btn_stop.setVisible(False)
        self.input_container.btn_send.setVisible(True)

        if is_cancelled:
            if self.current_ai_bubble:
                self.current_ai_bubble.is_interrupted = True
            StandardDialog(self.widget, "Task Cancelled", "The AI generation has been stopped by the user.",
                           show_cancel=False).exec()
            if hasattr(self, '_restore_last_input'):
                self._restore_last_input()

            self.history.append({"role": "assistant", "content": self.current_ai_text, "status": "interrupted"})
            self.current_ai_bubble = None
            self.scroll_to_bottom()
            return

        try:
            self.input_container.btn_stop.clicked.disconnect()
        except Exception:
            pass

        if self.current_ai_bubble and self.current_ai_bubble.is_loading:
            self.current_ai_bubble.set_loading(False)

        full_text = self.current_ai_text
        cites_html = ""

        cite_match = re.search(
            r'<br><hr style=\'border:0; height:1px; background:#444; margin:15px 0;\'><b>.*?Cited Sources:</b><br>',
            full_text)
        if cite_match:
            cites_html = full_text[cite_match.start():]
            full_text = full_text[:cite_match.start()]

        pattern = r'(?:\[\s*FOLLOW[_-]?\s*UPS?\s*\]|(?:^|\n|<br>|<br/>)\s*\*?\*?(?:💡\s*)?Suggested\s*Follow[- ]?ups?(?:\s*questions?)?:?\*?\*?)\s*'
        matches = list(re.finditer(pattern, full_text, flags=re.IGNORECASE))
        questions = []

        if matches:
            last_match = matches[-1]
            follow_up_block = full_text[last_match.end():].replace('<br>', '\n').replace('<br/>', '\n')

            if len(follow_up_block) < 1500:
                clean_text = full_text[:last_match.start()].strip()
                self.current_ai_text = clean_text + cites_html

                for line in follow_up_block.split('\n'):
                    line = line.strip()
                    line = re.sub(r'^>\s*', '', line)
                    if re.match(r'^([-*]|\d+\.)', line):
                        q = re.sub(r'^([-*\s]+|\d+\.\s*)', '', line).strip()
                        q = q.replace('**', '').strip()
                        if q and len(q) > 4:  # 防止空行或者过短的字符
                            tag_match = re.match(r'^\[(.*?)\]\s*(.*)', q)
                            if tag_match:
                                tag, text = tag_match.groups()
                                questions.append({"tag": tag.strip(), "text": text.strip()})
                            else:
                                questions.append({"tag": "General", "text": q})

                # 更新气泡内容
                idx = getattr(self.current_ai_bubble, 'index', -1)
                final_html = self._format_response(self.current_ai_text, idx)
                self.current_ai_bubble.set_content(final_html)

                # 渲染追问按钮
                if questions:
                    self.render_follow_up_buttons(questions)
            else:
                self.current_ai_text = full_text + cites_html
                idx = getattr(self.current_ai_bubble, 'index', -1)
                final_html = self._format_response(self.current_ai_text,
                                                   idx) if self.current_ai_text else "No response."
                self.current_ai_bubble.set_content(final_html)
        else:
            self.current_ai_text = full_text + cites_html
            idx = getattr(self.current_ai_bubble, 'index', -1)
            final_html = self._format_response(self.current_ai_text, idx) if self.current_ai_text else "No response."
            self.current_ai_bubble.set_content(final_html)

        self.history.append({"role": "assistant", "content": self.current_ai_text})
        self.current_ai_bubble = None
        self.logger.info("AI response generation finished and UI updated.")

    def on_chat_error(self, msg):
        """处理对话任务抛出的异常，恢复 UI 状态并展示错误"""
        if hasattr(self, '_render_timer'):
            self._render_timer.stop()
        self.set_controls_enabled(True)

        self.input_container.btn_stop.setText("Stop")
        self.input_container.btn_stop.setEnabled(True)
        self.input_container.btn_stop.setVisible(False)
        self.input_container.btn_send.setVisible(True)

        if self.current_ai_bubble:
            self.current_ai_bubble.set_loading(False)
            self.current_ai_bubble.is_interrupted = True

        # 格式化错误信息以在聊天气泡中醒目展示
        error_html = f"<div style='color: #ff6b6b; margin-top: 10px;'><b>⚠️ Generation Error:</b><br>{msg}</div>"

        if self.current_ai_text.strip():
            self.current_ai_text += error_html
        else:
            self.current_ai_text = error_html

        # 渲染到气泡
        if self.current_ai_bubble:
            idx = getattr(self.current_ai_bubble, 'index', -1)
            final_html = self._format_response(self.current_ai_text, idx)
            self.current_ai_bubble.set_content(final_html)

        # 记录到历史避免上下文结构断裂
        self.history.append({
            "role": "assistant",
            "content": self.current_ai_text,
            "status": "error"
        })

        self.current_ai_bubble = None
        self.logger.error(f"Chat task failed: {msg}")
        ToastManager().show("Generation failed due to an error.", "error")
        self.scroll_to_bottom()
