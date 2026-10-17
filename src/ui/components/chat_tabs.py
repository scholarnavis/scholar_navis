"""对话标签页栏（浏览器式）：新建 / 切换 / 改名 / 关闭。

职责边界
--------
* 本模块只负责**标签栏自身的交互与外观**：它不认识对话、历史或生成任务，
  一切状态变化都通过信号交给持有者（:class:`src.tools.chat_tool.ChatTool`）。
* 因此"标签页里装的是什么"由容器决定，标签栏可以独立测试（构造一个
  :class:`ChatTabBar`，连上信号断言即可）。

交互
----
* 新建：右侧的 **SVG 加号**（``Assets/Icons/add.svg``，按主题着色）；
* 切换：左键单击标签；
* 改名：双击标签、或按 **F2**、或右键菜单 → Rename（就地编辑，Esc 取消）；
* 关闭：标签上的 **SVG 关闭按钮**、鼠标 **中键**、或右键菜单 → Close Tab。

外观
----
配色全部取自 :class:`~src.core.theme_manager.ThemeManager` 的角色色，并订阅
``theme_changed``，深浅色主题都能正确适配；标签过多时标签栏横向滚动
（滚轮可直接横向翻阅），加号按钮固定在右侧不随滚动移动。
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
                               QScrollArea, QSizePolicy, QToolButton, QWidget)

from src.core.i18n import tr
from src.core.theme_manager import ThemeManager, overlay_scrollbar_qss

logger = logging.getLogger(__name__)


class ChatTabItem(QWidget):
    """单个标签：名称 + 关闭按钮，附带改名与关闭的全部触发方式。"""

    sig_activated = Signal(str)
    sig_close_requested = Signal(str)
    sig_renamed = Signal(str, str)
    #: (tab_id, 全局坐标) —— 由标签栏弹出上下文菜单
    sig_context_menu = Signal(str, QPoint)

    #: 名称显示的最大像素宽度（超出中间省略），避免个别长名把标签栏撑爆
    LABEL_MAX_WIDTH = 150
    #: 标签高度（与标签栏一致，仅作视觉基线）
    HEIGHT = 30

    def __init__(self, tab_id: str, name: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.tab_id = tab_id
        self._name = name
        self._active = False
        self._edit: Optional[QLineEdit] = None

        self.setObjectName("ChatTabItem")
        # QSS 背景只对设置了 WA_StyledBackground 的 QWidget 生效
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setCursor(Qt.PointingHandCursor)

        self._layout = QHBoxLayout(self)
        layout = self._layout
        layout.setContentsMargins(10, 0, 4, 0)
        layout.setSpacing(4)

        self._label = QLabel()
        # 名称不参与鼠标事件：点击落到标签自身（激活/右键菜单统一在标签上处理）
        self._label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._label.setObjectName("ChatTabLabel")

        self._btn_close = QToolButton(self)
        self._btn_close.setObjectName("ChatTabClose")
        self._btn_close.setCursor(Qt.PointingHandCursor)
        self._btn_close.setAutoRaise(True)
        self._btn_close.setFixedSize(18, 18)
        self._btn_close.setIconSize(QSize(12, 12))
        self._btn_close.setToolTip(tr("Close Tab"))
        self._btn_close.clicked.connect(lambda: self.sig_close_requested.emit(self.tab_id))

        layout.addWidget(self._label)
        layout.addWidget(self._btn_close)

        self._refresh_label()
        self.apply_theme()

    # ---------- 状态 ----------
    def set_name(self, name: str) -> None:
        self._name = name
        self._refresh_label()

    def name(self) -> str:
        return self._name

    def set_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        self.apply_theme()

    def _refresh_label(self) -> None:
        elided = self._label.fontMetrics().elidedText(
            self._name, Qt.ElideMiddle, self.LABEL_MAX_WIDTH)
        self._label.setText(elided)
        self.setToolTip(self._name)

    # ---------- 外观 ----------
    def apply_theme(self) -> None:
        tm = ThemeManager()
        if self._active:
            bg = tm.color("bg_card")
            fg = tm.color("text_main")
            underline = tm.color("accent")
        else:
            bg = "transparent"
            fg = tm.color("text_muted")
            underline = "transparent"

        self.setStyleSheet(f"""
            QWidget#ChatTabItem {{
                background-color: {bg};
                border-bottom: 2px solid {underline};
            }}
            QWidget#ChatTabItem:hover {{
                background-color: {tm.color('btn_hover')};
            }}
            QLabel#ChatTabLabel {{
                color: {fg};
                background: transparent;
                font-size: 12px;
                font-family: {tm.font_family()};
                border: none;
            }}
            QToolButton#ChatTabClose {{
                background: transparent;
                border: none;
                border-radius: 4px;
                padding: 0px;
            }}
            QToolButton#ChatTabClose:hover {{
                background-color: {tm.color('bg_input')};
            }}
        """)
        # 关闭图标是 SVG（Assets/Icons/close.svg），按主题角色色着色后交给 QToolButton
        self._btn_close.setIcon(tm.icon("close", "text_muted"))

    # ---------- 就地改名 ----------
    def start_rename(self) -> None:
        """进入改名态（双击 / F2 / 右键菜单共用这一处实现）。"""
        if self._edit is not None:
            return
        logger.debug("Inline rename started for tab %s.", self.tab_id)
        edit = QLineEdit(self._name, self)
        edit.setObjectName("ChatTabRename")
        edit.setMaxLength(80)
        edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        tm = ThemeManager()
        edit.setStyleSheet(f"""
            QLineEdit#ChatTabRename {{
                background-color: {tm.color('bg_input')};
                color: {tm.color('text_main')};
                border: 1px solid {tm.color('accent')};
                border-radius: 3px;
                padding: 1px 4px;
                font-size: 12px;
                font-family: {tm.font_family()};
            }}
        """)
        self._layout.insertWidget(0, edit)
        self._label.setVisible(False)
        self._btn_close.setVisible(False)
        self._edit = edit

        edit.selectAll()
        edit.setFocus(Qt.OtherFocusReason)
        # returnPressed 与 editingFinished 会在同一次回车中先后到达，两者都指向
        # _commit_rename：第一次提交后 _edit 已被清空，第二次天然变成空操作。
        edit.returnPressed.connect(self._commit_rename)
        edit.editingFinished.connect(self._commit_rename)
        edit.installEventFilter(self)

    def _finish_rename(self) -> None:
        """退出改名态并销毁输入框（提交与取消的公共部分）。"""
        edit = self._edit
        self._edit = None
        if edit is not None:
            edit.removeEventFilter(self)
            self._layout.removeWidget(edit)
            edit.hide()
            edit.deleteLater()
        self._label.setVisible(True)
        self._btn_close.setVisible(True)

    def _commit_rename(self) -> None:
        edit = self._edit
        if edit is None:
            return
        text = edit.text().strip()
        self._finish_rename()
        if text and text != self._name:
            self.sig_renamed.emit(self.tab_id, text)

    def _cancel_rename(self) -> None:
        self._finish_rename()

    def eventFilter(self, obj, event):
        # Esc 取消改名（QLineEdit 自身不处理 Esc）
        if obj is self._edit and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key_Escape:
                self._cancel_rename()
                return True
        return super().eventFilter(obj, event)

    # ---------- 鼠标 / 键盘 ----------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.sig_activated.emit(self.tab_id)
        elif event.button() == Qt.MiddleButton:
            # 中键关闭（浏览器习惯）
            self.sig_close_requested.emit(self.tab_id)
            event.accept()
            return
        elif event.button() == Qt.RightButton:
            self.sig_context_menu.emit(self.tab_id, self.mapToGlobal(event.position().toPoint()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.start_rename()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F2:
            self.start_rename()
            event.accept()
            return
        super().keyPressEvent(event)


class ChatTabBar(QWidget):
    """标签栏：横向排列标签 + 右侧固定 SVG 加号。

    持有者只需连三类信号：``sig_new_tab`` / ``sig_activated`` / ``sig_close_requested``，
    并在改名成功时接收 ``sig_renamed``。
    """

    sig_new_tab = Signal()
    sig_activated = Signal(str)
    sig_close_requested = Signal(str)
    sig_renamed = Signal(str, str)

    #: 标签栏总高：标签 30px + 溢出时那条 6px 横向滚动条，二者相加仍有余量，
    #: 保证标签在需要横向翻阅时不会被滚动条压扁。
    BAR_HEIGHT = 38
    #: 溢出时横向滚动条的粗细（px），用极简外观（无轨道底）避免喧宾夺主。
    SCROLLBAR_THICKNESS = 6

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ChatTabBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(self.BAR_HEIGHT)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(6, 0, 6, 0)
        outer.setSpacing(4)

        # 标签区放在滚动容器里：标签多到超宽时可横向滚动，加号仍固定在右侧
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self._inner = QWidget()
        self._inner.setObjectName("ChatTabStrip")
        self._strip = QHBoxLayout(self._inner)
        self._strip.setContentsMargins(0, 0, 0, 0)
        self._strip.setSpacing(2)
        self._strip.addStretch(1)
        self._scroll.setWidget(self._inner)

        self._btn_new = QToolButton(self)
        self._btn_new.setObjectName("ChatTabNew")
        self._btn_new.setCursor(Qt.PointingHandCursor)
        self._btn_new.setFixedSize(26, 26)
        self._btn_new.setIconSize(QSize(16, 16))
        self._btn_new.setAutoRaise(True)
        self._btn_new.setToolTip(tr("New Tab"))
        self._btn_new.clicked.connect(self.sig_new_tab.emit)

        outer.addWidget(self._scroll, 1)
        outer.addWidget(self._btn_new, 0)

        # 标签顺序的唯一来源就是布局本身（见 add_tab_item 的插入位置），
        # 不再另存一份 id 序列 —— 两份顺序迟早会不一致。
        self._items: Dict[str, ChatTabItem] = {}

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

    # ---------- 标签增删改 ----------
    def add_tab_item(self, tab_id: str, name: str) -> None:
        if tab_id in self._items:
            self.set_name(tab_id, name)
            return
        item = ChatTabItem(tab_id, name, self._inner)
        item.sig_activated.connect(self.sig_activated.emit)
        item.sig_close_requested.connect(self.sig_close_requested.emit)
        item.sig_renamed.connect(self._on_item_renamed)
        item.sig_context_menu.connect(self._show_context_menu)
        # 插到伸缩项之前：标签始终左对齐
        self._strip.insertWidget(self._strip.count() - 1, item)
        self._items[tab_id] = item

    def remove_tab_item(self, tab_id: str) -> None:
        item = self._items.pop(tab_id, None)
        if item is not None:
            item.setParent(None)
            item.deleteLater()

    def set_name(self, tab_id: str, name: str) -> None:
        item = self._items.get(tab_id)
        if item is not None:
            item.set_name(name)

    def set_active(self, tab_id: str) -> None:
        for tid, item in self._items.items():
            item.set_active(tid == tab_id)
        item = self._items.get(tab_id)
        if item is not None:
            # 激活的标签滚入视野（标签多时切换不会"切到视口外"）
            self._scroll.ensureWidgetVisible(item, 20, 0)

    def count(self) -> int:
        return len(self._items)

    def _on_item_renamed(self, tab_id: str, name: str) -> None:
        self.sig_renamed.emit(tab_id, name)

    # ---------- 上下文菜单 ----------
    def _show_context_menu(self, tab_id: str, global_pos: QPoint) -> None:
        tm = ThemeManager()
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{ background-color: {tm.color('bg_card')}; color: {tm.color('text_main')};
                     border: 1px solid {tm.color('border')}; border-radius: 6px; padding: 4px; }}
            QMenu::item {{ padding: 6px 20px; border-radius: 4px; }}
            QMenu::item:selected {{ background-color: {tm.color('accent')}; color: #fff; }}
        """)
        act_rename = menu.addAction(tm.icon("edit", "text_main"), tr("Rename Tab"))
        act_close = menu.addAction(tm.icon("close", "text_main"), tr("Close Tab"))

        action = menu.exec(global_pos or QCursor.pos())
        if action is act_rename:
            item = self._items.get(tab_id)
            if item is not None:
                item.start_rename()
        elif action is act_close:
            self.sig_close_requested.emit(tab_id)

    # ---------- 滚轮横向翻阅 ----------
    def wheelEvent(self, event):
        bar = self._scroll.horizontalScrollBar()
        if bar.maximum() <= 0:
            super().wheelEvent(event)
            return
        delta = event.angleDelta().y() or event.angleDelta().x()
        bar.setValue(bar.value() - delta)
        event.accept()

    # ---------- 外观 ----------
    def _apply_theme(self) -> None:
        tm = ThemeManager()
        self.setStyleSheet(f"""
            QWidget#ChatTabBar {{
                background-color: {tm.color('bg_main')};
                border-bottom: 1px solid {tm.color('border')};
            }}
            QWidget#ChatTabStrip {{ background: transparent; }}
            QToolButton#ChatTabNew {{
                background: transparent;
                border: 1px solid transparent;
                border-radius: 4px;
            }}
            QToolButton#ChatTabNew:hover {{
                background-color: {tm.color('btn_hover')};
                border: 1px solid {tm.color('border')};
            }}
        """)
        # 加号图标是 SVG（Assets/Icons/add.svg），按主题角色色着色
        self._btn_new.setIcon(tm.icon("add", "text_main"))
        self._scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            + overlay_scrollbar_qss(thickness=self.SCROLLBAR_THICKNESS, groove_alpha=0.0))
        for item in self._items.values():
            item.apply_theme()
