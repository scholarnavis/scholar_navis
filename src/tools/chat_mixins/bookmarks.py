"""书签 mixin：对话内的书签增删、滚动条标注、书签集与跳转。

职责边界
--------
* 书签数据存在**消息条目**里（``history[i]["bookmarks"]``），因此会话快照与
  归档导出天然带上书签，不需要第二套持久化通道（口径见 :mod:`src.core.bookmarks`）。
* 气泡只负责"选区 → 锚点载荷"与"画下划线"；本 mixin 负责对话框、历史回写、
  落盘、滚动条标注与跳转。
* 还原历史时由 :meth:`_restore_message_bookmarks` 把书签交回气泡，与引用/组件
  的回灌时机相同（正文渲染完成之后）。
"""

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QPushButton, QSizePolicy

from src.core.bookmarks import make_bookmark, normalize_bookmarks
from src.core.i18n import tr
from src.core.theme_manager import ThemeManager, strong_weight_css
from src.ui.components.bookmark_ui import (BookmarkCreateDialog,
                                           BookmarkHoverController,
                                           BookmarkScrollStrip,
                                           BookmarkSetDialog)
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)


class ChatBookmarksMixin:
    """对话标签页内的书签功能。"""

    #: 布局变化后重算滚动条标注的节流间隔（ms）。
    _BOOKMARK_STRIP_REFRESH_MS = 80

    # ------------------------------------------------------------------ #
    #  初始化
    # ------------------------------------------------------------------ #
    def setup_bookmark_button(self, row2_layout):
        """在顶栏第 2 行插入"书签集"按钮（位于搜索按钮之前）。"""
        self.btn_bookmarks = QPushButton(tr(" Bookmarks"))
        self.btn_bookmarks.setToolTip(tr("Show all bookmarks in this conversation"))
        self.btn_bookmarks.setCursor(Qt.PointingHandCursor)
        # 顶栏里没有可拉伸控件（KB 下拉被隐藏）时，按钮默认会被 QBoxLayout 拉宽
        self.btn_bookmarks.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.btn_bookmarks.clicked.connect(self.open_bookmark_set)

        anchor = getattr(self, "btn_toggle_search", None)
        index = row2_layout.indexOf(anchor) if anchor is not None else -1
        row2_layout.insertWidget(index if index >= 0 else row2_layout.count() - 1,
                                 self.btn_bookmarks)
        ThemeManager().theme_changed.connect(self._apply_bookmark_button_theme)
        self._apply_bookmark_button_theme()

    def setup_bookmark_strip(self):
        """创建滚动条标注条并订阅滚动范围变化。"""
        self._bookmark_strip = BookmarkScrollStrip(self.scroll_area)
        self._bookmark_strip.sig_jump.connect(self.jump_to_bookmark)

        self._bookmark_strip_refresh = QTimer(self.widget)
        self._bookmark_strip_refresh.setSingleShot(True)
        self._bookmark_strip_refresh.setInterval(self._BOOKMARK_STRIP_REFRESH_MS)
        self._bookmark_strip_refresh.timeout.connect(self._refresh_bookmark_strip)

        self.scroll_area.verticalScrollBar().rangeChanged.connect(
            lambda *_: self._schedule_bookmark_strip_refresh())
        logger.debug("Bookmark strip initialized for tab %s", getattr(self, "tab_id", "?"))

    def _apply_bookmark_button_theme(self):
        btn = getattr(self, "btn_bookmarks", None)
        if btn is None:
            return
        tm = ThemeManager()
        btn.setIcon(tm.icon("bookmark", "text_main"))
        btn.setStyleSheet(f"""
            QPushButton {{ background-color: {tm.color('btn_bg')}; color: {tm.color('text_main')};
                           border: 1px solid {tm.color('border')}; padding: 4px 10px;
                           border-radius: 4px; font-weight: {strong_weight_css()}; }}
            QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}
        """)

    # ------------------------------------------------------------------ #
    #  查询
    # ------------------------------------------------------------------ #
    def _iter_bookmark_bubbles(self):
        layout = getattr(self, "chat_layout", None)
        if layout is None:
            return
        for i in range(layout.count()):
            item = layout.itemAt(i)
            widget = item.widget() if item is not None else None
            if widget is not None and hasattr(widget, "bookmark_anchor_point"):
                yield widget

    def _bubble_for_message(self, message_index):
        for bubble in self._iter_bookmark_bubbles():
            if getattr(bubble, "index", -1) == message_index:
                return bubble
        return None

    @staticmethod
    def _bookmark_entries_of(message):
        if not isinstance(message, dict):
            return []
        return normalize_bookmarks(message.get("bookmarks"))

    def _find_bookmark(self, bookmark_id):
        """按 id 找书签，返回 ``(消息编号, 书签)``；找不到返回 ``None``。"""
        for msg_index, message in enumerate(getattr(self, "history", []) or []):
            for bookmark in self._bookmark_entries_of(message):
                if bookmark["id"] == bookmark_id:
                    return msg_index, bookmark
        return None

    def _panel_entries(self):
        """书签集的数据视图：按对话顺序排列，条目内注入消息编号。"""
        entries = []
        for msg_index, message in enumerate(getattr(self, "history", []) or []):
            for bookmark in self._bookmark_entries_of(message):
                entry = dict(bookmark)
                entry["msg_index"] = msg_index
                entries.append(entry)
        return entries

    # ------------------------------------------------------------------ #
    #  增删
    # ------------------------------------------------------------------ #
    def _on_bubble_bookmark_requested(self, bubble, payload):
        payload = payload if isinstance(payload, dict) else {}
        msg_index = int(payload.get("message_index", -1))
        history = getattr(self, "history", [])
        if not (0 <= msg_index < len(history)):
            ToastManager().show(tr("This message cannot be bookmarked."), "warning")
            return
        # 请求来自右键菜单的 triggered：让菜单先完成收起，再进入模态对话框，
        # 避免在菜单的事件循环里再套一层模态循环（部分平台会丢焦点）。
        QTimer.singleShot(0, self, lambda: self._prompt_new_bookmark(bubble, payload))

    def _prompt_new_bookmark(self, bubble, payload):
        msg_index = int(payload.get("message_index", -1))
        history = getattr(self, "history", [])
        if not (0 <= msg_index < len(history)):
            return

        dialog = BookmarkCreateDialog(self.widget, selected_text=payload.get("text", ""))
        try:
            if not dialog.exec():
                return
            title, description, color = dialog.values()
        finally:
            dialog.deleteLater()

        bookmark = make_bookmark(
            title, description, color,
            payload.get("text", ""), payload.get("prefix", ""),
            payload.get("suffix", ""), payload.get("occurrence", 0),
            payload.get("block", 0), payload.get("block_kind", "text"))
        if not bookmark:
            ToastManager().show(tr("A bookmark needs a title."), "warning")
            return

        bookmarks = self._bookmark_entries_of(history[msg_index])
        bookmarks.append(bookmark)
        history[msg_index]["bookmarks"] = bookmarks
        bubble.apply_bookmarks(bookmarks, animate_ids=[bookmark["id"]])

        self._autosave_session()
        self._schedule_bookmark_strip_refresh()
        self._sync_bookmark_panel()
        ToastManager().show(tr("Bookmark added."), "success")
        logger.info("Bookmark %s added to message #%s (title=%r)",
                    bookmark["id"], msg_index, bookmark["title"])

    def _on_bubble_bookmark_remove(self, bubble, bookmark_id):
        msg_index = int(getattr(bubble, "index", -1))
        history = getattr(self, "history", [])
        if not (0 <= msg_index < len(history)):
            return
        current = self._bookmark_entries_of(history[msg_index])
        bookmarks = [b for b in current if b["id"] != bookmark_id]
        if len(bookmarks) == len(current):
            return

        history[msg_index]["bookmarks"] = bookmarks
        bubble.refresh_bookmarks(bookmarks)

        self._autosave_session()
        self._schedule_bookmark_strip_refresh()
        self._sync_bookmark_panel()
        ToastManager().show(tr("Bookmark removed."), "info")
        logger.info("Bookmark %s removed from message #%s", bookmark_id, msg_index)

    def _on_bookmark_panel_delete(self, bookmark_id):
        located = self._find_bookmark(bookmark_id)
        if located is None:
            return
        msg_index, _bookmark = located
        bubble = self._bubble_for_message(msg_index)
        if bubble is None:
            # 气泡尚未渲染（理论上不会发生）：直接从历史移除并落盘
            history = getattr(self, "history", [])
            history[msg_index]["bookmarks"] = [
                b for b in self._bookmark_entries_of(history[msg_index])
                if b["id"] != bookmark_id]
            self._autosave_session()
            self._schedule_bookmark_strip_refresh()
            self._sync_bookmark_panel()
            return
        self._on_bubble_bookmark_remove(bubble, bookmark_id)

    # ------------------------------------------------------------------ #
    #  悬停预览
    # ------------------------------------------------------------------ #
    def _on_bubble_bookmark_hover(self, bubble, bookmark_id, anchor_rect):
        bookmark = next((b for b in bubble.bookmarks() if b["id"] == bookmark_id), None)
        if bookmark is None:
            return
        # ChatConversation 是 QObject（BaseTool），没有 window()；浮层必须挂到
        # 对话页面所在的顶层窗口上（Wayland 下只有宿主子控件的位置才可控）。
        host = getattr(self, "widget", None)
        host = host.window() if host is not None else None
        BookmarkHoverController.instance().show_entries(
            [bookmark], anchor_rect, host, prefer="auto")

    def _on_bubble_bookmark_move(self, anchor_rect):
        """鼠标仍沿着同一条书签文字移动：让已弹出的预览卡跟随锚点。"""
        BookmarkHoverController.instance().follow(anchor_rect)

    def _on_bubble_bookmark_leave(self):
        BookmarkHoverController.instance().leave()

    # ------------------------------------------------------------------ #
    #  历史回灌
    # ------------------------------------------------------------------ #
    def _restore_message_bookmarks(self, bubble, message):
        """把历史条目里的书签画回气泡（正文渲染完成之后调用）。"""
        bookmarks = self._bookmark_entries_of(message)
        if not bookmarks:
            return
        bubble.apply_bookmarks(bookmarks)
        logger.debug("Restored %d bookmark(s) for message #%s",
                     len(bookmarks), getattr(bubble, "index", -1))

    # ------------------------------------------------------------------ #
    #  滚动条标注
    # ------------------------------------------------------------------ #
    def _schedule_bookmark_strip_refresh(self):
        timer = getattr(self, "_bookmark_strip_refresh", None)
        if timer is not None:
            timer.start()

    def _refresh_bookmark_strip(self):
        strip = getattr(self, "_bookmark_strip", None)
        if strip is None:
            return
        scrollbar = self.scroll_area.verticalScrollBar()
        maximum = scrollbar.maximum()
        if maximum <= 0:
            strip.set_entries([])
            return

        entries = []
        container = self.chat_container
        for bubble in self._iter_bookmark_bubbles():
            for bookmark in bubble.bookmarks():
                point = bubble.bookmark_anchor_point(bookmark["id"])
                if point is None:
                    continue
                y = bubble.mapTo(container, point).y()
                target = min(max(y - 8, 0), maximum)
                entry = dict(bookmark)
                entry["ratio"] = target / float(maximum)
                entry["msg_index"] = getattr(bubble, "index", -1)
                entries.append(entry)

        strip.set_entries(entries)
        logger.debug("Bookmark strip refreshed: %d mark(s), range=0..%d",
                     len(entries), maximum)

    # ------------------------------------------------------------------ #
    #  跳转
    # ------------------------------------------------------------------ #
    def jump_to_bookmark(self, bookmark_id):
        located = self._find_bookmark(bookmark_id)
        if located is None:
            return
        msg_index, _bookmark = located
        bubble = self._bubble_for_message(msg_index)
        if bubble is None:
            return

        point = bubble.bookmark_anchor_point(bookmark_id)
        if point is not None:
            target = bubble.mapTo(self.chat_container, point).y() - 24
        else:
            # 锚点暂时定位不到（正在流式/重排）：至少滚到该条消息顶部
            target = bubble.y() - 10

        scrollbar = self.scroll_area.verticalScrollBar()
        target = min(max(int(target), 0), scrollbar.maximum())
        if hasattr(self, "scroll_anim") and scrollbar.value() != target:
            self.scroll_anim.stop()
            self.scroll_anim.setDuration(300)
            self.scroll_anim.setStartValue(scrollbar.value())
            self.scroll_anim.setEndValue(target)
            self.scroll_anim.start()
        else:
            scrollbar.setValue(target)

        BookmarkHoverController.instance().dismiss()
        QTimer.singleShot(330, bubble,
                          lambda: bubble.pulse_bookmark(bookmark_id))
        logger.info("Jumped to bookmark %s (message #%s)", bookmark_id, msg_index)

    # ------------------------------------------------------------------ #
    #  书签集
    # ------------------------------------------------------------------ #
    def open_bookmark_set(self):
        panel = getattr(self, "_bookmark_panel", None)
        if panel is None:
            panel = BookmarkSetDialog(self.widget)
            panel.sig_jump.connect(self.jump_to_bookmark)
            panel.sig_delete.connect(self._on_bookmark_panel_delete)
            self._bookmark_panel = panel
        panel.set_entries(self._panel_entries())
        panel.show()
        panel.raise_()
        panel.activateWindow()

    def _sync_bookmark_panel(self):
        panel = getattr(self, "_bookmark_panel", None)
        if panel is not None and panel.isVisible():
            panel.set_entries(self._panel_entries())

    # ------------------------------------------------------------------ #
    #  清理
    # ------------------------------------------------------------------ #
    def _hide_bookmark_overlays(self):
        """标签页被切走时收起悬停卡与书签集窗口（它们本身是独立窗口）。"""
        BookmarkHoverController.instance().dismiss()
        panel = getattr(self, "_bookmark_panel", None)
        if panel is not None and panel.isVisible():
            panel.hide()
        for name in ("_literature_panel", "_evidence_panel"):
            panel = getattr(self, name, None)
            if panel is not None and panel.isVisible():
                panel.hide()

    def _reset_bookmark_views(self):
        strip = getattr(self, "_bookmark_strip", None)
        if strip is not None:
            strip.set_entries([])
        panel = getattr(self, "_bookmark_panel", None)
        if panel is not None:
            panel.set_entries([])
            panel.hide()

    def clear_chat_history(self):
        self._reset_bookmark_views()
        super().clear_chat_history()

    def _apply_imported_history(self, *args, **kwargs):
        self._reset_bookmark_views()
        return super()._apply_imported_history(*args, **kwargs)
