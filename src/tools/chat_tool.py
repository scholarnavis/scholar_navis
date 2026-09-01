"""Chat tool (aggregate shell).

The original ~1900-line implementation has been split by functional domain
into the mixins under ``chat_mixins/`` plus the input widgets module:

- chat_input_widgets: ChatDropTargetWidget / AutoResizingTextEdit / ChatInputContainer
- ChatSendFlowMixin: query dispatch & AI response launch
- ChatResponseFlowMixin: streaming render & finish/error handling
- ChatBubblesMixin: bubble creation, scrolling & follow-ups
- ChatAttachmentsMixin: attachments & history export

This file keeps only the shell: construction, UI skeleton, ribbon state,
hardware status and knowledge-base bindings.
"""

import logging
import os

from PySide6.QtCore import (QEasingCurve, QEvent, QPropertyAnimation,
                            Qt, QTimer)
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from src.core.kb_manager import DatabaseManager, KBManager
from src.core.core_task import TaskManager, TaskMode
from src.core.models_registry import get_model_conf
from src.core.signals import GlobalSignals
from src.core.theme_manager import ThemeManager
from src.tools.base_tool import BaseTool
from src.tools.chat_input_widgets import (ChatDropTargetWidget,
                                          ChatInputContainer)
from src.tools.chat_mixins import (ChatAttachmentsMixin, ChatBubblesMixin,
                                   ChatResponseFlowMixin, ChatSendFlowMixin)
from src.tools.settings_tool import FloatingOverlayFilter
from src.ui.components.combo import BaseComboBox
from src.ui.components.model_selector import ModelSelectorWidget
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)


class ChatTool(ChatSendFlowMixin, ChatResponseFlowMixin,
               ChatBubblesMixin, ChatAttachmentsMixin, BaseTool):
    # 由 get_ui_widget 与信号槽流程创建，仅作静态检查声明
    top_bar_wrapper: QWidget
    model_selector: ModelSelectorWidget
    collapsed_placeholder: QLabel
    btn_ribbon_state: QPushButton
    trans_selector: ModelSelectorWidget
    lbl_kb: QLabel
    combo_kb: BaseComboBox
    lbl_hardware_status: QLabel
    ribbon_state: str
    scroll_area: QScrollArea
    chat_container: QWidget
    chat_layout: QVBoxLayout
    btn_scroll_bottom: QPushButton
    opacity_effect: QGraphicsOpacityEffect
    fade_anim: QPropertyAnimation
    scroll_anim: QPropertyAnimation
    overlay_filter: FloatingOverlayFilter
    follow_up_shelf: QWidget
    follow_up_shelf_layout: QVBoxLayout
    input_container: ChatInputContainer
    _render_timer: QTimer
    _is_rendering_dirty: bool
    kb_id: str
    is_locked: bool

    def __init__(self):
        super().__init__("Chat Assistant")
        self.history = []
        self.widget = None
        self.kb_manager = KBManager()
        self.current_ai_text = ""
        self.current_ai_bubble = None
        self.pdf_viewer = None
        self.expanded_thinks = set()
        self.user_toggled_thinks = set()
        self.external_context_buffer = ""
        self.external_context_html = ""

        GlobalSignals().kb_list_changed.connect(self.refresh_kb_list)
        GlobalSignals().kb_switched.connect(self.on_global_kb_switched)
        GlobalSignals().kb_modified.connect(self.on_kb_modified)

        if hasattr(GlobalSignals(), 'llm_config_changed'):
            GlobalSignals().llm_config_changed.connect(self.load_llm_configs)

    def get_ui_widget(self) -> QWidget:
        if self.widget: return self.widget

        # 1. Main Container & Global Layout
        self.widget = ChatDropTargetWidget()
        self.widget.sig_files_dropped.connect(self.process_attached_files)

        main_layout = QVBoxLayout(self.widget)
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(10, 10, 10, 10)

        # --- Ribbon UI Implementation ---
        self.top_bar_wrapper = QWidget()
        self.top_bar_wrapper.setObjectName("TopBarWrapper")
        top_bar = QVBoxLayout(self.top_bar_wrapper)
        top_bar.setSpacing(8)
        top_bar.setContentsMargins(0, 0, 0, 10)

        row1_layout = QHBoxLayout()
        # enable_vision=True：允许为聊天单独配置 Vision 模型，当主模型不支持
        # 图片时，可由该模型把附件图片转成文字描述（Auto 表示跟随主模型）。
        self.model_selector = ModelSelectorWidget(label_text=" Main Model:", config_key="chat_llm_id",
                                                  model_key="chat_model_name", enable_vision=True)

        self.collapsed_placeholder = QLabel(" ")
        self.collapsed_placeholder.setVisible(False)

        row1_layout.addWidget(self.model_selector, 1)
        row1_layout.addWidget(self.collapsed_placeholder, 1)

        # 硬件状态显示标签
        self.lbl_hardware_status = QLabel()
        self._update_hardware_status()
        row1_layout.addWidget(self.lbl_hardware_status)

        # Pin/Toggle Button for Ribbon State
        tm = ThemeManager()
        self.btn_ribbon_state = QPushButton(" Pinned")
        self.btn_ribbon_state.setIcon(tm.icon("keep", "text_muted"))
        self.btn_ribbon_state.setCursor(Qt.PointingHandCursor)
        self.btn_ribbon_state.setFixedWidth(90)
        self.btn_ribbon_state.setStyleSheet("""
                            QPushButton { background: transparent; border: 1px solid #555; border-radius: 4px; color: #aaa; font-size: 11px; padding: 2px 6px; text-align: left;}
                            QPushButton:hover { background: #333; color: #fff; }
                        """)
        row1_layout.addWidget(self.btn_ribbon_state)

        row2_layout = QHBoxLayout()
        self.trans_selector = ModelSelectorWidget(label_text=" Translator:", config_key="chat_trans_llm_id",
                                                  model_key="chat_trans_model_name", enable_vision=False)

        self.lbl_kb = QLabel(" Knowledge Base:")
        self.combo_kb = BaseComboBox(max_width=400)
        self.refresh_kb_list()

        row2_layout.addWidget(self.trans_selector)
        row2_layout.addSpacing(15)
        row2_layout.addWidget(self.lbl_kb)
        row2_layout.addWidget(self.combo_kb, 1)

        top_bar.addLayout(row1_layout)
        top_bar.addLayout(row2_layout)

        self.lbl_hardware_status = QLabel("Compute Device: Detecting...")
        top_bar.addWidget(self.lbl_hardware_status)
        self._update_hardware_status()

        main_layout.addWidget(self.top_bar_wrapper)

        self.ribbon_state = self.config.user_settings.get("chat_ribbon_state", "Pinned")

        def set_ribbon_visible(visible):
            self.model_selector.setVisible(visible)
            self.collapsed_placeholder.setVisible(not visible)
            self.trans_selector.setVisible(visible)
            self.lbl_kb.setVisible(visible)
            self.combo_kb.setVisible(visible)

            if hasattr(self, 'lbl_hardware_status'):
                self.lbl_hardware_status.setVisible(visible)

        def apply_ribbon_state(state):
            tm = ThemeManager()
            self.ribbon_state = state
            self.config.user_settings["chat_ribbon_state"] = state
            self.config.save_settings()

            if state == "Pinned":
                self.btn_ribbon_state.setText(" Pinned")
                self.btn_ribbon_state.setIcon(tm.icon("keep", "text_muted"))
                set_ribbon_visible(True)
            elif state == "Hover":
                self.btn_ribbon_state.setText(" Hover")
                self.btn_ribbon_state.setIcon(tm.icon("menu", "text_muted"))
                set_ribbon_visible(False)
            elif state == "Collapsed":
                self.btn_ribbon_state.setText(" Collapsed")
                self.btn_ribbon_state.setIcon(tm.icon("down", "text_muted"))
                set_ribbon_visible(False)

        def toggle_ribbon_state():
            if self.ribbon_state == "Pinned":
                apply_ribbon_state("Hover")
            elif self.ribbon_state == "Hover":
                apply_ribbon_state("Collapsed")
            else:
                apply_ribbon_state("Pinned")

        self.btn_ribbon_state.clicked.connect(toggle_ribbon_state)

        apply_ribbon_state(self.ribbon_state)

        # Install event filter for Hover mechanics
        self.top_bar_wrapper.installEventFilter(self)

        # Load configurations
        self.load_llm_configs()

        # 3. 对话展示滚动区 (仅存放消息气泡)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("QScrollArea { border: none; background-color: transparent; }")

        self.chat_container = QWidget()
        self.chat_container.setStyleSheet("background-color: transparent;")
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setSpacing(12)
        self.chat_layout.setContentsMargins(10, 10, 10, 0)
        self.chat_layout.setAlignment(Qt.AlignTop)

        self.scroll_area.setWidget(self.chat_container)
        main_layout.addWidget(self.scroll_area, stretch=1)

        main_layout.addSpacing(5)

        # --- 悬浮滚动到底部按钮 ---
        self.btn_scroll_bottom = QPushButton("", self.scroll_area)
        self.btn_scroll_bottom.setIcon(ThemeManager().icon("down", "bg_main"))
        self.btn_scroll_bottom.setFixedSize(40, 40)
        self.btn_scroll_bottom.setCursor(Qt.PointingHandCursor)
        self.btn_scroll_bottom.setStyleSheet(f"""
            QPushButton {{
                background-color: {ThemeManager().color('accent')};
                border-radius: 20px; border: 1px solid {ThemeManager().color('border')};
            }}
            QPushButton:hover {{ background-color: {ThemeManager().color('accent_hover')}; }}
        """)
        self.opacity_effect = QGraphicsOpacityEffect(self.btn_scroll_bottom)
        self.btn_scroll_bottom.setGraphicsEffect(self.opacity_effect)
        self.opacity_effect.setOpacity(0.0)
        self.btn_scroll_bottom.setVisible(False)
        self.fade_anim = QPropertyAnimation(self.opacity_effect, b"opacity")
        self.fade_anim.setDuration(250)
        self.fade_anim.finished.connect(self._on_fade_anim_finished)
        self.scroll_anim = QPropertyAnimation(self.scroll_area.verticalScrollBar(), b"value")
        self.scroll_anim.setEasingCurve(QEasingCurve.OutCubic)
        self.btn_scroll_bottom.clicked.connect(lambda: self.scroll_to_bottom(smooth=True))

        self.overlay_filter = FloatingOverlayFilter(self.scroll_area, self.btn_scroll_bottom)
        self.scroll_area.installEventFilter(self.overlay_filter)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._check_scroll_position)

        self.follow_up_shelf = QWidget()
        self.follow_up_shelf.setObjectName("FollowUpShelf")
        self.follow_up_shelf.setVisible(False)  # 初始隐藏
        self.follow_up_shelf_layout = QVBoxLayout(self.follow_up_shelf)
        self.follow_up_shelf_layout.setContentsMargins(10, 5, 10, 5)
        self.follow_up_shelf_layout.setSpacing(5)

        # 将 shelf 添加到主布局
        main_layout.addWidget(self.follow_up_shelf)

        # 5. 底部输入区
        self.input_container = ChatInputContainer()
        self.input_container.sig_send_clicked.connect(self.process_send)
        self.input_container.sig_export_clicked.connect(self.export_chat_history)
        self.input_container.sig_clear_clicked.connect(self.clear_chat_history)
        self.input_container.sig_attach_clicked.connect(self.show_attachment_menu)
        self.input_container.sig_clear_context_clicked.connect(self.clear_attached_context)
        # 图片附件：输入区缩略图芯片的移除 / 预览动作
        if hasattr(self.input_container, 'sig_remove_image'):
            self.input_container.sig_remove_image.connect(self.remove_attached_image)
        if hasattr(self.input_container, 'sig_open_image'):
            self.input_container.sig_open_image.connect(self.open_attachment_image)

        main_layout.addWidget(self.input_container)

        self._render_timer = QTimer(self.widget)
        self._render_timer.setInterval(60)
        self._render_timer.timeout.connect(self._throttled_render)
        self._is_rendering_dirty = False

        return self.widget

    def _update_hardware_status(self):
        """异步获取当前推理设备，防止阻塞主界面"""
        from src.task.chat_tasks import FetchHardwareStatusTask
        self.hw_task_mgr = TaskManager()
        self.hw_task_mgr.sig_result.connect(self._on_hw_status_result)
        self.hw_task_mgr.start_task(FetchHardwareStatusTask, task_id="fetch_hw_chat", mode=TaskMode.THREAD)

    def _on_hw_status_result(self, result):
        if result and "dev_name" in result:
            self.lbl_hardware_status.setText(f"Compute Device: {result['dev_name']}")

    def eventFilter(self, obj, event):
        if obj == self.top_bar_wrapper:
            if self.ribbon_state == "Hover":
                if event.type() == QEvent.Enter:
                    self.model_selector.setVisible(True)
                    self.collapsed_placeholder.setVisible(False)
                    self.trans_selector.setVisible(True)
                    self.lbl_kb.setVisible(True)
                    self.combo_kb.setVisible(True)
                    if hasattr(self, 'lbl_hardware_status'):
                        self.lbl_hardware_status.setVisible(True)

                elif event.type() == QEvent.Leave:
                    if not self.top_bar_wrapper.geometry().contains(self.widget.mapFromGlobal(QCursor.pos())):
                        self.model_selector.setVisible(False)
                        self.collapsed_placeholder.setVisible(True)
                        self.trans_selector.setVisible(False)
                        self.lbl_kb.setVisible(False)
                        self.combo_kb.setVisible(False)
                        if hasattr(self, 'lbl_hardware_status'):
                            self.lbl_hardware_status.setVisible(False)

        return super().eventFilter(obj, event)

    def load_llm_configs(self):
        if hasattr(self, 'model_selector'):
            self.model_selector.load_llm_configs()
        if hasattr(self, 'trans_selector'):
            self.trans_selector.load_llm_configs()

    def refresh_kb_list(self):
        self.load_llm_configs()
        if not hasattr(self, 'combo_kb'): return
        curr_data = self.combo_kb.currentData()
        curr_id = curr_data['id'] if isinstance(curr_data, dict) else curr_data

        self.combo_kb.blockSignals(True)
        self.combo_kb.clear()

        self.combo_kb.addItem("No Knowledge Base (Direct Chat)", "none")

        kbs = self.kb_manager.get_all_kbs()
        target_idx = 0  # 默认选中 "none"

        for i, kb in enumerate(kbs):
            if kb.get('status') == 'ready':
                m = get_model_conf(kb.get('model_id'), "embedding")
                m_ui = m['ui_name'] if m else kb.get('model_id', '?')
                display_text = f"{kb['name']}   [Model: {m_ui} | Docs: {kb.get('doc_count', 0)}]"
                self.combo_kb.addItem(display_text, kb)
                if kb['id'] == curr_id:
                    target_idx = self.combo_kb.count() - 1

        if self.combo_kb.count() > 0:
            self.combo_kb.setCurrentIndex(target_idx)

        self.combo_kb.blockSignals(False)

    def on_global_kb_switched(self, kb_id):
        if not hasattr(self, 'combo_kb') or not kb_id: return
        for i in range(self.combo_kb.count()):
            data = self.combo_kb.itemData(i)
            if data and data.get('id') == kb_id:
                self.combo_kb.blockSignals(True)
                self.combo_kb.setCurrentIndex(i)
                self.combo_kb.blockSignals(False)
                self.kb_id = kb_id
                if hasattr(self, 'db'): self.db.switch_kb(kb_id)
                break

    def on_kb_modified(self, kb_id):
        """当当前关联的知识库在后台发生变更时触发，锁定对话防止上下文错乱"""

        DatabaseManager().reload()

        self.refresh_kb_list()

        if not self.history:
            return

        curr_data = self.combo_kb.currentData()
        curr_id = curr_data.get("id") if isinstance(curr_data, dict) else curr_data

        if curr_id == kb_id:
            self.is_locked = True
            if hasattr(self, 'input_container'):
                self.input_container.lock_input()
            ToastManager().show("The knowledge base was modified. Chat is currently locked.", "warning")

    def _on_fade_anim_finished(self):
        """动画结束时的统一处理逻辑，避免反复 connect/disconnect 产生警告"""
        if self.fade_anim.endValue() == 0.0:
            self.btn_scroll_bottom.hide()

    def _check_scroll_position(self):
        sb = self.scroll_area.verticalScrollBar()
        should_show = (sb.maximum() - sb.value() > 200)

        # 防止动画重复触发
        if should_show and not self.btn_scroll_bottom.isVisible():
            self.btn_scroll_bottom.setVisible(True)
            self.fade_anim.stop()
            self.fade_anim.setStartValue(self.opacity_effect.opacity())
            self.fade_anim.setEndValue(1.0)
            self.fade_anim.start()

        elif not should_show and self.btn_scroll_bottom.isVisible():
            if self.fade_anim.endValue() != 0.0:
                self.fade_anim.stop()
                self.fade_anim.setStartValue(self.opacity_effect.opacity())
                self.fade_anim.setEndValue(0.0)
                self.fade_anim.start()

    def _save_setting(self, key, value):
        self.config.user_settings[key] = value
        self.config.save_settings()

    def execute_task(self):
        pass
