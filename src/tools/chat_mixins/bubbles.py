"""Bubble flow mixin: bubble creation, scrolling, follow-ups, link routing.

拆分自 src/tools/chat_tool.py：负责气泡生命周期、滚动定位与追问组件管理。
"""
import logging

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from src.ui.components.chat_bubble import ChatBubbleWidget
from src.ui.components.pill_button import FollowUpGroupWidget
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)


class ChatBubblesMixin:
    """气泡渲染与视图滚动。"""

    def _ensure_chat_ui(self):
        """确保聊天界面骨架已构建。

        外部路由（如 main_window.route_dev_test）可能早于 UI 构建触发
        气泡方法；此时先补建 UI，避免 chat_layout/scroll_area 未定义。
        """
        if getattr(self, 'chat_layout', None) is None:
            logger.warning("Chat UI not built yet; building lazily before bubble ops.")
            self.get_ui_widget()

    def add_bubble(self, text, is_user, context_html=None):
        self._ensure_chat_ui()
        if is_user:
            self.remove_old_follow_ups()
            for i in range(self.chat_layout.count()):
                item = self.chat_layout.itemAt(i)
                if item and item.widget():
                    w = item.widget()
                    if hasattr(w, 'is_user') and w.is_user:
                        w.disable_edit()

        index = len(self.history)
        bubble = ChatBubbleWidget(text, is_user, index, context_html=context_html)
        bubble.index = index

        # Inject the translator config so plot-plan cards can translate non-English
        # user edits back to English before the plan is confirmed and re-sent.
        if hasattr(self, 'trans_selector'):
            bubble.translator_config = self.trans_selector.get_current_config()

        # Plot-plan confirmation cards may appear in AI bubbles; forward the final
        # English requirement to the chat tool so it re-sends it to the AI to render.
        bubble.sig_plot_plan_confirm.connect(self.handle_plot_plan_confirm)

        if is_user:
            bubble.sig_edit_confirmed.connect(self.handle_edit_resend)
            bubble.sig_link_clicked.connect(self.handle_link_click)
        else:
            bubble.lbl_text.anchorClicked.connect(self.handle_link_click)

        self.chat_layout.addWidget(bubble)

        if not getattr(self, '_is_editing', False):
            if is_user:
                QTimer.singleShot(50, lambda: self.scroll_to_user_message(bubble))
            else:
                QTimer.singleShot(50, lambda: self.scroll_to_bottom(smooth=True))
        return bubble

    def show_dev_note(self, text):
        """Show a display-only note bubble (left-aligned, gray) to the user.

        This bubble is NOT appended to ``self.history`` and therefore never
        reaches the LLM — it is purely a user-visible annotation (used by the
        developer-mode AI tests to label what is being exercised).
        """
        self._ensure_chat_ui()
        index = len(self.history)
        bubble = ChatBubbleWidget(
            text, is_user=False, index=index,
            context_html=None, msg_type=ChatBubbleWidget.MSG_ERROR,
        )
        bubble.index = index
        self.chat_layout.addWidget(bubble)
        QTimer.singleShot(50, lambda: self.scroll_to_bottom(smooth=True))
        return bubble

    def scroll_to_bottom(self, smooth=False):
        sb = self.scroll_area.verticalScrollBar()
        target = sb.maximum()

        if smooth and hasattr(self, 'scroll_anim') and sb.value() != target:
            self.scroll_anim.stop()
            self.scroll_anim.setDuration(250)  # 250毫秒的平滑过渡
            self.scroll_anim.setStartValue(sb.value())
            self.scroll_anim.setEndValue(target)
            self.scroll_anim.start()
        else:
            sb.setValue(target)

    def scroll_to_user_message(self, bubble_widget):
        QApplication.processEvents()

        target_y = max(0, bubble_widget.y() - 10)
        sb = self.scroll_area.verticalScrollBar()

        if hasattr(self, 'scroll_anim'):
            self.scroll_anim.stop()
            self.scroll_anim.setDuration(300)
            self.scroll_anim.setStartValue(sb.value())
            self.scroll_anim.setEndValue(target_y)
            self.scroll_anim.start()
        else:
            sb.setValue(target_y)

    def render_follow_up_buttons(self, questions):
        if not questions:
            return

        self.follow_up_group = FollowUpGroupWidget(
            questions,
            self._trigger_follow_up,
            self._edit_follow_up
        )

        # 将其作为对话流的一个整体插入
        self.chat_layout.addWidget(self.follow_up_group)

        if not getattr(self, '_is_editing', False):
            QTimer.singleShot(50, self.scroll_to_bottom)

    def remove_old_follow_ups(self):
        """清理历史中的追问组件，避免重复堆叠"""
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item and isinstance(item.widget(), FollowUpGroupWidget):
                item.widget().deleteLater()

    def clear_follow_up_shelf(self):
        while self.follow_up_shelf_layout.count() > 0:
            item = self.follow_up_shelf_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.follow_up_shelf.setVisible(False)

    def clear_chat_history(self):
        self.cancel_generation()
        self.current_ai_bubble = None
        self.history.clear()
        self.clear_layout(self.chat_layout)

        self.clear_follow_up_shelf()

        self.input_container.unlock_input()

        self.input_container.clear_text()
        self.clear_attached_context()
        self.is_locked = False
        ToastManager().show("Chat history cleared.", "success")

    def clear_layout(self, layout):
        while layout.count() > 0:
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                if hasattr(widget, 'clean_up_images'):
                    widget.clean_up_images()
                widget.deleteLater()
            elif item.spacerItem():
                pass

    def handle_link_click(self, url):
        """统一代理链接路由"""
        from src.ui.components.text_formatter import TextFormatter

        def trigger_render(idx):
            # 仅寻找被点击的那个气泡进行局部重绘
            for i in range(self.chat_layout.count()):
                item = self.chat_layout.itemAt(i)
                if item and item.widget():
                    w = item.widget()
                    from src.ui.components.chat_bubble import ChatBubbleWidget
                    if isinstance(w, ChatBubbleWidget) and getattr(w, 'index', -1) == idx:
                        raw_text = self.current_ai_text if w == getattr(self, 'current_ai_bubble', None) else (
                            self.history[idx]['content'] if idx < len(self.history) else "")
                        if raw_text:
                            w.set_content(self._format_response(raw_text, idx))
                        break

        if not hasattr(self, 'mermaid_codes'):
            self.mermaid_codes = {}
        if not hasattr(self, 'user_toggled_thinks'):
            self.user_toggled_thinks = set()
        if not hasattr(self, 'expanded_thinks'):
            self.expanded_thinks = set()

        TextFormatter.handle_link_click(
            url=url, parent_widget=self, mermaid_cache=self.mermaid_codes,
            user_toggled_thinks=self.user_toggled_thinks,
            expanded_indices=self.expanded_thinks,
            render_callback=trigger_render
        )
