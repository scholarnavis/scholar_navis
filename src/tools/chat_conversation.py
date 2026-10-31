"""单个对话标签页（一个标签页 = 一个 ChatConversation 实例）。

历史沿革：本文件即原 ``chat_tool.py`` 中的单会话实现，按功能域拆到
``chat_mixins/`` 之后剩下的外壳。现在外壳之上多了一层标签页容器
（:class:`src.tools.chat_tool.ChatTool`），于是这里改名为 ChatConversation：
**它代表"一个标签页"，每个实例独占自己的历史、气泡、输入框与生成任务**。

因此：

* 多个标签页可以**同时**生成回答（各自的 ``TaskManager`` 与气泡互不干扰）；
* "清空 / 附件 / 输入框 / 对话区"天然只作用于所属标签页，无需额外分支；
* 全局限速（:data:`src.core.network_worker.global_rate_limiter`）是进程级单例，
  所有标签页共享同一份配额，不会因为"多了标签页"而放大对外请求速率。

拆分边界（与 mixin 的分工）：

- chat_input_widgets: ChatDropTargetWidget / AutoResizingTextEdit / ChatInputContainer
- ChatSendFlowMixin: query dispatch & AI response launch
- ChatResponseFlowMixin: streaming render & finish/error handling
- ChatBubblesMixin: bubble creation, scrolling & follow-ups
- ChatAttachmentsMixin: attachments & history export / import
- ChatSessionMixin: per-tab autosave

本文件只保留外壳：构造、UI 骨架、ribbon 状态与知识库绑定。
"""

import logging
import re
import threading

from PySide6.QtCore import (QEasingCurve, QEvent, QPoint, QPropertyAnimation,
                            Qt, QTimer, Signal)
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from src.core import chat_session
from src.core.i18n import tr
from src.core.signals import GlobalSignals
from src.core.theme_manager import ThemeManager, overlay_scrollbar_qss, strong_weight_css
from src.tools.base_tool import BaseTool
from src.tools.chat_input_widgets import (ChatDropTargetWidget,
                                         ChatInputContainer)
from src.tools.chat_mixins import (ChatAttachmentsMixin, ChatBookmarksMixin,
                                   ChatBubblesMixin, ChatResponseFlowMixin,
                                   ChatSearchMixin, ChatSendFlowMixin,
                                   ChatSessionMixin)
from src.ui.components.chat_bubble import ChatBubbleWidget
from src.ui.components.combo import BaseComboBox
from src.ui.components.model_selector import ModelSelectorWidget
from src.ui.components.RotatingSpinner import LoadingOverlay
from src.ui.components.search import SearchBar
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)


class ChatConversation(ChatSearchMixin, ChatSendFlowMixin, ChatResponseFlowMixin,
                       ChatBookmarksMixin, ChatBubblesMixin, ChatAttachmentsMixin,
                       ChatSessionMixin, BaseTool):
    #: 付费墙/手动 PDF 入库完成：(ok, message, path)。由后台线程发出，UI 线程接收。

    # 由 get_ui_widget 与信号槽流程创建，仅作静态检查声明
    top_bar_wrapper: QWidget
    model_selector: ModelSelectorWidget
    collapsed_placeholder: QLabel
    btn_ribbon_state: QPushButton
    ribbon_state: str
    scroll_area: QScrollArea
    chat_container: QWidget
    chat_layout: QVBoxLayout
    btn_scroll_bottom: QPushButton
    opacity_effect: QGraphicsOpacityEffect
    fade_anim: QPropertyAnimation
    btn_jump_msg_top: QPushButton
    jump_opacity_effect: QGraphicsOpacityEffect
    jump_fade_anim: QPropertyAnimation
    scroll_anim: QPropertyAnimation
    follow_up_shelf: QWidget
    follow_up_shelf_layout: QVBoxLayout
    input_container: ChatInputContainer
    btn_toggle_search: QPushButton
    search_bar: SearchBar  # 见 src/ui/components/search.py（共享搜索条）
    _render_timer: QTimer
    _is_rendering_dirty: bool
    kb_id: str
    is_locked: bool

    # ------------------------------------------------------------------ #
    #  流式文本缓冲
    # ------------------------------------------------------------------ #
    @property
    def current_ai_text(self) -> str:
        """当前回答的累计文本（惰性拼接）。

        流式 token 逐个到达时，如果每来一个就做 ``text += token``，Python 字符串
        不可变，整体是 O(N²) 拷贝：长思维链（几万到十几万字符）会把主线程拖死，
        表现为"AI 一思考整个程序就卡"。这里改为分段追加，只有真正需要读取
        （渲染、落盘、收尾）时才一次性 join。
        """
        parts = getattr(self, "_ai_text_parts", None)
        if parts is None:
            return ""
        if len(parts) > 1:
            value = "".join(parts)
            parts.clear()
            parts.append(value)
            self._ai_text_len = len(value)
            return value
        return parts[0] if parts else ""

    @current_ai_text.setter
    def current_ai_text(self, value):
        text = str(value or "")
        self._ai_text_parts = [text]
        self._ai_text_len = len(text)

    def __init__(self, tab_id: str, tabs_container=None):
        """:param tab_id: 本会话所属标签页 id（快照文件名与清单键都用它）。
        :param tabs_container: 所属的标签页容器（导出全部 / 导入多标签页时回调它）。
        """
        super().__init__("Chat Assistant")
        self.tab_id = tab_id
        self.tabs_container = tabs_container
        self.history = []
        self.widget = None
        self.current_ai_text = ""
        self.current_ai_bubble = None
        self.pdf_viewer = None
        #: 本标签页是否已关闭（见 :meth:`shutdown`）。
        #:
        #: 控件由容器 ``deleteLater`` 释放，而挂在 Python 对象（本对象）上的计时器、
        #: 以及 ``QTimer.singleShot`` 排进事件队列的 lambda 都不受控件生命周期约束：
        #: 它们能活到控件被销毁之后。凡是"延迟执行且会碰控件"的回调，入口都以本标志
        #: 为准——否则关标签页后会撞上 "Internal C++ object already deleted"
        #: （历史 bug：关标签页后 ``_check_scroll_position`` 在已删除的 QScrollArea
        #: 上取滚动条，或取全文/追问的 50ms 迟到回调去滚一个已删除的气泡）。
        self._closed = False

        # 滚动范围变化在流式期间非常频繁：合并成 90ms 一次，避免每个 rangeChanged
        # 都全量扫描气泡布局来确定"回顶/到底"按钮。
        self._scroll_check_timer = QTimer(self)
        self._scroll_check_timer.setSingleShot(True)
        self._scroll_check_timer.setInterval(90)
        self._scroll_check_timer.timeout.connect(self._check_scroll_position)
        self.expanded_thinks = set()
        self.user_toggled_thinks = set()
        self.external_context_buffer = ""
        self.external_context_html = ""
        # Human-in-the-loop 等待状态：ask_user / deep-plan 卡等待用户操作时为
        # True，期间锁定通用输入框的发送，直到卡片提交或用户按 Stop 强制终止。
        self._awaiting_user_input = False

        # 本标签页的自动快照：恢复时机由容器决定（见 ChatSessionMixin.load_messages）；
        # 退出 / 崩溃前的兜底落盘由 core 的钩子表统一触发，这里只登记能力。
        chat_session.register_flush_hook(self.flush_session_snapshot)

        # 历史回灌的分片状态（见 ChatAttachmentsMixin._apply_imported_history）：
        # 非 None 表示重建正在进行，此时 self.history 只是"已重建的一部分"，禁止
        # 落盘覆盖磁盘上的完整快照（判定点见 ChatSessionMixin._snapshot_writable）。
        self._history_rebuild_state = None
        # 对话区加载遮罩：惰性创建（见 _show_chat_loading），与悬浮按钮同为浮层子控件。
        self._chat_loading_overlay = None


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

        # 第 1 行包成独立 Widget：Collapsed 状态直接整行隐藏，不再留一条空白带。
        self.ribbon_row = QWidget()
        row1_layout = QHBoxLayout(self.ribbon_row)
        row1_layout.setContentsMargins(0, 0, 0, 0)
        row1_layout.setSpacing(8)
        # enable_vision=True：允许为聊天单独配置 Vision 模型，当主模型不支持
        # 图片时，可由该模型把附件图片转成文字描述（Auto 表示跟随主模型）。
        self.model_selector = ModelSelectorWidget(label_text=tr(" Main Model:"), config_key="chat_llm_id",
                                                  model_key="chat_model_name", enable_vision=True)

        self.collapsed_placeholder = QLabel(" ")
        self.collapsed_placeholder.setVisible(False)

        row1_layout.addWidget(self.model_selector, 1)
        row1_layout.addWidget(self.collapsed_placeholder, 1)

        # 收起按钮放在第 2 行（与 KB 同一行）；Compute Device 状态已按要求移除
        tm = ThemeManager()
        self.btn_ribbon_state = QPushButton(tr(" Pinned"))
        self.btn_ribbon_state.setIcon(tm.icon("keep", "text_muted"))
        self.btn_ribbon_state.setCursor(Qt.PointingHandCursor)
        self.btn_ribbon_state.setFixedWidth(90)
        self._apply_ribbon_button_style()

        # 顶栏第 2 行 = 状态按钮组（搜索 / 书签 / 收起）。知识库选择器已彻底下线：
        # 检索证据只来自学术工具，界面上不再有"选库"这一层。
        row2_layout = QHBoxLayout()
        row2_layout.setSpacing(8)
        row2_layout.setContentsMargins(0, 0, 0, 0)
        row2_layout.addStretch(1)
        row2_layout.addWidget(self.btn_ribbon_state)

        top_bar.addWidget(self.ribbon_row)
        top_bar.addLayout(row2_layout)

        # 会话搜索：按钮插入 row2（收起按钮前），搜索条在顶栏下方整行展开
        self.setup_chat_search(row2_layout, top_bar)
        # 书签集入口：插在搜索按钮之前（同一行的状态按钮组）
        self.setup_bookmark_button(row2_layout)

        main_layout.addWidget(self.top_bar_wrapper)

        self.ribbon_state = self.config.user_settings.get("chat_ribbon_state", "Pinned")

        def set_ribbon_visible(visible, show_row=True):
            # Collapsed：整行收起（不留空白带）；Hover：保留一行占位，鼠标进入
            # 顶栏时再展开模型选择器。
            self.ribbon_row.setVisible(show_row)
            self.model_selector.setVisible(visible)
            self.collapsed_placeholder.setVisible(not visible and show_row)

        def apply_ribbon_state(state):
            tm = ThemeManager()
            self.ribbon_state = state
            self.config.user_settings["chat_ribbon_state"] = state
            self.config.save_settings()

            if state == "Pinned":
                self.btn_ribbon_state.setText(tr(" Pinned"))
                self.btn_ribbon_state.setIcon(tm.icon("keep", "text_muted"))
                set_ribbon_visible(True)
            elif state == "Hover":
                self.btn_ribbon_state.setText(tr(" Hover"))
                self.btn_ribbon_state.setIcon(tm.icon("menu", "text_muted"))
                set_ribbon_visible(False)
            elif state == "Collapsed":
                self.btn_ribbon_state.setText(tr(" Collapsed"))
                self.btn_ribbon_state.setIcon(tm.icon("down", "text_muted"))
                set_ribbon_visible(False, show_row=False)

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
        # 对话区只做纵向滚动：横向滚动条既没有用途，也会被过宽内容带出来
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 滚动条走 overlay 样式：常态为浅色滑块（能看出可滚动），交互时加深
        self.scroll_area.setStyleSheet(
            "QScrollArea { border: none; background-color: transparent; }"
            + overlay_scrollbar_qss())

        self.chat_container = QWidget()
        self.chat_container.setStyleSheet("background-color: transparent;")
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setSpacing(12)
        # 底部留出悬浮按钮的占位：按钮是 scroll_area 的浮动子控件，不留白
        # 就会盖住最后一行内容。
        self.chat_layout.setContentsMargins(10, 10, 10, self._OVERLAY_RESERVED_BOTTOM)
        self.chat_layout.setAlignment(Qt.AlignTop)

        self.scroll_area.setWidget(self.chat_container)
        main_layout.addWidget(self.scroll_area, stretch=1)
        # 书签在滚动条上的彩色标注（滚动区浮层，不遮挡滚动条本体）
        self.setup_bookmark_strip()

        main_layout.addSpacing(5)

        # --- 悬浮导航按钮：滚动到底部 / 回到本条消息顶部 ---
        self.btn_scroll_bottom, self.opacity_effect, self.fade_anim = self._make_overlay_button("down")
        self.btn_scroll_bottom.clicked.connect(lambda: self.scroll_to_bottom(smooth=True))

        # 长回答导航：当前阅读的 AI 回答高过一屏且顶端被滚出视口时显示，
        # 点击平滑滚回该条回答顶部
        self.btn_jump_msg_top, self.jump_opacity_effect, self.jump_fade_anim = self._make_overlay_button("chevron-up")
        self.btn_jump_msg_top.clicked.connect(self._on_jump_msg_top_clicked)

        # 两个按钮统一由 _layout_overlay_buttons 定位（右下角竖排，"到底部"
        # 在下）；_overlay_state 记录逻辑可见性，驱动位置归并动画
        self._overlay_state = {"bottom": False, "jump": False}
        self._overlay_pos_anims = {}
        self.scroll_area.installEventFilter(self)

        sb = self.scroll_area.verticalScrollBar()
        sb.valueChanged.connect(self._check_scroll_position)
        # 折叠思考链、图片加载完成、流式内容增长等布局变化只改 range 而
        # value 未必变化（valueChanged 不发）；延迟一帧重检，保证悬浮按钮
        # 状态最终收敛，修复折叠后"到底部"按钮误残留的问题
        sb.rangeChanged.connect(self._on_scroll_range_changed)

        self.scroll_anim = QPropertyAnimation(sb, b"value")
        self.scroll_anim.setEasingCurve(QEasingCurve.OutCubic)

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
        self.input_container.sig_import_clicked.connect(self.import_chat_history)
        self.input_container.sig_clear_clicked.connect(self.clear_chat_history)
        self.input_container.sig_attach_clicked.connect(self.show_attachment_menu)
        self.input_container.sig_clear_context_clicked.connect(self.clear_attached_context)
        # 剪贴板图片粘贴：位图（截图工具）与本地图片文件两条路径
        self.input_container.sig_paste_image.connect(self.attach_from_clipboard)
        self.input_container.sig_paste_files.connect(self.process_attached_files)
        # 图片附件：输入区缩略图芯片的移除 / 预览动作
        if hasattr(self.input_container, 'sig_remove_image'):
            self.input_container.sig_remove_image.connect(self.remove_attached_image)
        if hasattr(self.input_container, 'sig_open_image'):
            self.input_container.sig_open_image.connect(self.open_attachment_image)
        # 文档附件芯片：打开 / 移除动作
        if hasattr(self.input_container, 'sig_remove_file'):
            self.input_container.sig_remove_file.connect(self.remove_attached_file)
        if hasattr(self.input_container, 'sig_open_file'):
            self.input_container.sig_open_file.connect(self.open_attachment_file)

        main_layout.addWidget(self.input_container)

        self._render_timer = QTimer(self.widget)
        self._render_timer.setInterval(60)
        self._render_timer.timeout.connect(self._throttled_render)
        self._is_rendering_dirty = False

        # 顶栏"收起状态"按钮的配色随主题刷新（本工具无独立 _apply_theme，
        # 子控件各自订阅主题信号，这里补上本文件自有的控件）。
        ThemeManager().theme_changed.connect(self._apply_ribbon_button_style)

        # 历史回灌由容器在"标签页首次激活"时调用 load_messages()，此处不再自恢复。

        return self.widget

    def shutdown(self):
        """关闭本标签页：注销退出钩子、断开全局信号、终止生成、释放画面。

        由容器的 ``close_tab`` 调用；此后本对象不再被引用。快照文件的删除由容器
        统一负责（它同时要改标签页清单），这里只做内存与线程侧的收尾。
        """
        # 先关门再收尾：控件随后被容器 deleteLater 释放，而排队的 singleShot 与挂在
        # 本对象上的计时器都可能在那之后才执行，必须让它们一律提前返回（见 _closed）。
        self._closed = True
        chat_session.unregister_flush_hook(self.flush_session_snapshot)
        self._disconnect_global_signals()

        # 让正在分片推进的历史回灌自然终止（见 _rebuild_history_step 的首个判据）：
        # 否则已排队的下一片会往已被销毁的控件上写，触发 C++ 对象已删除的崩溃。
        self._history_rebuild_state = None
        self._is_editing = False

        if getattr(self, 'input_container', None) is not None:
            try:
                self.cancel_generation()
            except Exception as e:
                logger.warning("Failed to cancel generation while closing tab %s: %s",
                               self._tab_id(), e)
        for name in ('_render_timer', '_scroll_check_timer'):
            timer = getattr(self, name, None)
            if timer is not None:
                timer.stop()

        layout = getattr(self, 'chat_layout', None)
        if layout is not None:
            self.clear_layout(layout)

        # 会话内容已落盘（或已被容器删除），内存不再保留，避免关闭后仍占着大字符串
        self.history = []
        self.current_ai_text = ""
        self.current_ai_bubble = None

    def _disconnect_global_signals(self):
        """断开进程级单例（GlobalSignals / ThemeManager）上的连接。

        这些单例在进程生命周期内一直存在，连接会**持有本对象引用**：不解开的话，
        关闭掉的标签页会被每次知识库刷新 / 主题切换唤醒，既泄漏内存又会在已销毁的
        控件上做事。界面控件（气泡、输入区）之间由 Qt 在控件析构时自动断开。
        """
        signals = GlobalSignals()
        pairs = []
        if hasattr(signals, 'llm_config_changed'):
            pairs.append((signals.llm_config_changed, self.load_llm_configs))
        for signal, slot in pairs:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        try:
            ThemeManager().theme_changed.disconnect(self._apply_ribbon_button_style)
        except (RuntimeError, TypeError):
            pass
        for slot in (self._apply_bookmark_button_theme,):
            try:
                ThemeManager().theme_changed.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _apply_ribbon_button_style(self):
        """按当前主题刷新顶栏 ribbon 状态按钮（Pinned / Hover / Collapsed）。"""
        btn = getattr(self, 'btn_ribbon_state', None)
        if btn is None:
            return
        tm = ThemeManager()
        state_icons = {"Pinned": "keep", "Hover": "menu", "Collapsed": "down"}
        btn.setIcon(tm.icon(state_icons.get(getattr(self, 'ribbon_state', 'Pinned'), "keep"),
                            "text_muted"))
        btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: 1px solid {tm.color('border')};
                           border-radius: 4px; color: {tm.color('text_muted')};
                           font-size: 11px; padding: 2px 6px; text-align: left;
                           font-family: {tm.font_family()}; }}
            QPushButton:hover {{ background: {tm.color('btn_hover')}; color: {tm.color('text_main')}; }}
        """)

        # 悬浮导航圆按钮同样随主题刷新（构造期已按旧主题着色）
        overlay_style = f"""
            QPushButton {{
                background-color: {tm.color('accent')};
                border-radius: 20px; border: 1px solid {tm.color('border')};
            }}
            QPushButton:hover {{ background-color: {tm.color('accent_hover')}; }}
        """
        for key, icon_name in (('btn_scroll_bottom', 'down'),
                               ('btn_jump_msg_top', 'chevron-up')):
            ob = getattr(self, key, None)
            if ob is None:
                continue
            ob.setIcon(tm.icon(icon_name, "bg_main"))
            ob.setStyleSheet(overlay_style)

    def eventFilter(self, obj, event):
        if obj == self.scroll_area and event.type() == QEvent.Resize:
            # 窗口缩放时按钮直接跟手就位（无动画），避免连续 resize 期间
            # 动画反复重启产生抖动
            self._layout_overlay_buttons(animate=False)

        # 标签页被切走：书签集是独立窗口，必须跟着收起，否则会浮在别的标签页上
        if obj == self.widget and event.type() == QEvent.Hide:
            self._hide_bookmark_overlays()

        if obj == self.top_bar_wrapper:
            if self.ribbon_state == "Hover":
                if event.type() == QEvent.Enter:
                    self.model_selector.setVisible(True)
                    self.collapsed_placeholder.setVisible(False)

                elif event.type() == QEvent.Leave:
                    if not self.top_bar_wrapper.geometry().contains(self.widget.mapFromGlobal(QCursor.pos())):
                        self.model_selector.setVisible(False)
                        self.collapsed_placeholder.setVisible(True)

        return super().eventFilter(obj, event)

    def load_llm_configs(self):
        if hasattr(self, 'model_selector'):
            self.model_selector.load_llm_configs()

    def _show_chat_loading(self, text: str):
        """在对话区盖一层加载遮罩并显示 ``text``（惰性创建，可重复调用）。

        遮罩与悬浮导航按钮一样是 ``scroll_area`` 的浮层子控件：不进 ``chat_layout``，
        因此历史回灌前的 ``clear_layout`` 不会把它清掉，定位也由遮罩自己跟随宿主
        尺寸（见 :class:`~src.ui.components.RotatingSpinner.LoadingOverlay`）。
        """
        overlay = self._chat_loading_overlay
        if overlay is None:
            overlay = self._chat_loading_overlay = LoadingOverlay(self.scroll_area)
        overlay.start(text)

    def _hide_chat_loading(self):
        """收起对话区加载遮罩。未创建过（或已收起）时是空操作。"""
        overlay = self._chat_loading_overlay
        if overlay is not None:
            overlay.stop()

    def _make_overlay_button(self, icon_name: str):
        """创建悬浮导航圆按钮（40x40 圆形，透明度渐隐动画）。

        :return: (btn, opacity_effect, fade_anim) 三元组。
        """
        tm = ThemeManager()
        btn = QPushButton("", self.scroll_area)
        btn.setIcon(tm.icon(icon_name, "bg_main"))
        btn.setFixedSize(self._OVERLAY_BTN_SIZE, self._OVERLAY_BTN_SIZE)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {tm.color('accent')};
                border-radius: 20px; border: 1px solid {tm.color('border')};
            }}
            QPushButton:hover {{ background-color: {tm.color('accent_hover')}; }}
        """)
        effect = QGraphicsOpacityEffect(btn)
        btn.setGraphicsEffect(effect)
        effect.setOpacity(0.0)
        btn.setVisible(False)
        anim = QPropertyAnimation(effect, b"opacity")
        anim.setDuration(250)
        anim.finished.connect(lambda b=btn, a=anim: self._on_fade_anim_finished(b, a))
        return btn, effect, anim

    def _on_fade_anim_finished(self, btn, anim):
        """渐隐动画结束时隐藏对应按钮，并让剩余可见按钮归位"""
        if anim.endValue() == 0.0:
            btn.hide()
            # 隐藏完成后重算布局：隐藏按钮直接就位（为下次渐显做准备），
            # 其余可见按钮若有位移则走归并动画
            self._layout_overlay_buttons(animate=False)

    # 悬浮按钮竖排布局参数：距滚动区下边缘 16px，按钮间距 8px。
    # 右侧额外让出 16px：书签标注条紧贴滚动条，按钮不能压在它的命中区上。
    _OVERLAY_MARGIN = 16
    _OVERLAY_SIDE_EXTRA = 16
    _OVERLAY_GAP = 8
    _OVERLAY_BTN_SIZE = 40
    # 对话容器底部留白 = 一颗悬浮按钮 + 其右/下边距 + 间距：保证滚到底时
    # 最后一行内容（如追问建议卡片）不会被右下角悬浮按钮压住。
    _OVERLAY_RESERVED_BOTTOM = _OVERLAY_BTN_SIZE + _OVERLAY_MARGIN + _OVERLAY_GAP

    def _set_overlay_visible(self, key, btn, effect, anim, should_show: bool):
        """悬浮按钮统一显隐切换（渐变动画 + 防重入 + 位置归并）。

        :param key: ``_overlay_state`` 中的逻辑可见性键（"bottom"/"jump"）。
        显示时先按最终布局摆位再渐显——此刻按钮尚不可见，会被直接 move
        到目标位，避免从上次隐藏时的旧位置飞入；隐藏触发时其余可见
        按钮以动画归并到锚位。
        """
        self._overlay_state[key] = should_show
        if should_show and not btn.isVisible():
            self._layout_overlay_buttons(animate=True)
            btn.setVisible(True)
            anim.stop()
            anim.setStartValue(effect.opacity())
            anim.setEndValue(1.0)
            anim.start()
        elif not should_show and btn.isVisible():
            if anim.endValue() != 0.0:
                anim.stop()
                anim.setStartValue(effect.opacity())
                anim.setEndValue(0.0)
                anim.start()
            self._layout_overlay_buttons(animate=True)

    def _layout_overlay_buttons(self, animate=True):
        """计算两个悬浮导航按钮的目标位置并应用。

        统一锚定滚动区右下角竖排（"到底部"在下）；二者都显示时上下
        排列，仅其一显示时占据底部锚位。可见按钮的位置变化走 180ms
        pos 动画（OutCubic），不可见按钮直接就位。
        """
        area = self.scroll_area
        x = (area.width() - self.btn_scroll_bottom.width()
             - self._OVERLAY_MARGIN - self._OVERLAY_SIDE_EXTRA)
        y_bottom = area.height() - self.btn_scroll_bottom.height() - self._OVERLAY_MARGIN
        both = self._overlay_state.get("bottom") and self._overlay_state.get("jump")
        jump_y = (y_bottom - self.btn_jump_msg_top.height() - self._OVERLAY_GAP) if both else y_bottom
        self._move_overlay(self.btn_scroll_bottom, QPoint(x, y_bottom), animate)
        self._move_overlay(self.btn_jump_msg_top, QPoint(x, jump_y), animate)

    def _move_overlay(self, btn, pos: QPoint, animate: bool):
        if btn.pos() == pos:
            return
        if animate and btn.isVisible():
            anim = self._overlay_pos_anim(btn)
            anim.stop()
            anim.setStartValue(btn.pos())
            anim.setEndValue(pos)
            anim.start()
        else:
            anim = self._overlay_pos_anims.get(btn)
            if anim is not None:
                anim.stop()
            btn.move(pos)

    def _overlay_pos_anim(self, btn) -> QPropertyAnimation:
        anim = self._overlay_pos_anims.get(btn)
        if anim is None:
            anim = QPropertyAnimation(btn, b"pos", self)
            anim.setDuration(180)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            self._overlay_pos_anims[btn] = anim
        return anim

    def _check_scroll_position(self):
        # 计时器 / 滚动条信号都可能在控件被销毁后才到（见 _closed）：
        # 本函数一进来就取 verticalScrollBar()，是这一族崩溃最容易撞上的一处。
        if self._closed:
            return
        sb = self.scroll_area.verticalScrollBar()
        self._set_overlay_visible(
            "bottom", self.btn_scroll_bottom, self.opacity_effect, self.fade_anim,
            (sb.maximum() - sb.value()) > 200)
        self._update_jump_top_button()

    def _on_scroll_range_changed(self, min_v, max_v):
        # 关闭后不得再排重检：控件正在被释放，90ms 后这一枪会打在已删除的滚动条上。
        if self._closed:
            return
        # 布局高度变化后延迟重检：合并 90ms 内的连续 rangeChanged，避免流式期间
        # 每个 token 都全量扫描气泡布局来更新悬浮按钮。
        self._scroll_check_timer.start()
        # 布局变化可能意味着新气泡或流式内容增长，搜索展开时节流重扫命中表
        self._schedule_chat_search_refresh()

    def _find_jump_top_target(self):
        """定位当前正在阅读的长 AI 回答。

        取视口中心线之上最近的一条 AI 气泡（含中心线落在气泡内的情况），
        且要求其高度超过一屏（需要翻页才能读完）。中心线落在追问组件或
        气泡间隙时仍能命中其上方最近的 AI 回答。
        """
        vh = self.scroll_area.viewport().height()
        center_y = self.scroll_area.verticalScrollBar().value() + vh // 2
        layout = self.chat_layout
        candidate = None
        for i in range(layout.count()):
            item = layout.itemAt(i)
            w = item.widget() if item else None
            if not isinstance(w, ChatBubbleWidget) or w.is_user or not w.isVisible():
                continue
            # chat_layout 自上而下按 y 递增排布，越过中心线即可停止
            if w.y() <= center_y:
                candidate = w
            else:
                break
        if candidate is not None and candidate.height() > vh:
            return candidate
        return None

    def _update_jump_top_button(self):
        """长回答导航按钮显隐：仅当目标回答的顶端已滚出视口上方时显示，
        此时点击才有"回到该条回答开头"的意义。"""
        should_show = False
        target = self._find_jump_top_target()
        if target is not None:
            should_show = target.y() < self.scroll_area.verticalScrollBar().value()
        self._set_overlay_visible(
            "jump", self.btn_jump_msg_top, self.jump_opacity_effect, self.jump_fade_anim, should_show)

    def _on_jump_msg_top_clicked(self):
        target = self._find_jump_top_target()
        if target is not None:
            self.scroll_to_message_top(target)

    def _save_setting(self, key, value):
        self.config.user_settings[key] = value
        self.config.save_settings()

    def execute_task(self):
        pass
