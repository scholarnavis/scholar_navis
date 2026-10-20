"""书签相关界面组件：悬停预览卡、滚动条标注条、创建与书签集对话框。

职责边界
--------
* 数据模型、锚点匹配与标注排布在 :mod:`src.core.bookmarks`；本模块只负责"画"与
  "接鼠标"。
* 悬停卡与会话/气泡都是**松耦合**的：调用方把条目字典递进来即可，卡片不反向
  查询历史，因此不会因为消息重建而持有过期引用。
* 滚动条标注条是滚动区的**子控件浮层**，不覆盖滚动条本体：用户仍可直接拖动
  滚动条，标注条只承担"位置提示 + 悬停预览 + 点击跳转"。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import (QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation,
                            QRect, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWidgets import (QAbstractScrollArea, QApplication, QColorDialog, QFrame,
                               QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QScrollArea, QTextEdit, QVBoxLayout, QWidget)

from src.core.bookmarks import (DEFAULT_COLOR, DESC_MAX, PALETTE, TITLE_MAX,
                                contrast_text_color, layout_marks, normalize_color)
from src.core.i18n import tr
from src.core.theme_manager import ThemeManager, hex_to_rgba, strong_weight_css
from src.ui.components.dialogs.base import BaseDialog

logger = logging.getLogger("UI.Bookmarks")

#: 悬停预览卡宽度（px）。
CARD_WIDTH = 340
#: 卡片最大内容高度（px）：组内条目多时由内容区滚动承担。
CARD_MAX_CONTENT_H = 380
#: 卡片四周阴影留白（px）。
CARD_SHADOW = 8
#: 卡片内容边距（px）。
CARD_PAD = 12
#: 落点与锚点的间距（px）。
CARD_GAP = 8
#: 悬停意图延迟（ms）：避免鼠标划过时卡片乱闪。
HOVER_INTENT_MS = 140
#: 光标轮询间隔与离开宽限拍数（可从锚点移进卡片；约 0.5s，足够跨越间隙）。
WATCH_INTERVAL_MS = 130
WATCH_GRACE_TICKS = 4


def _chip_style(color: str) -> str:
    """标题色块：底色=用户所选颜色，文字色按亮度自适应。"""
    fg = contrast_text_color(color)
    return (f"background-color: {color}; color: {fg}; border-radius: 4px; "
            f"padding: 1px 7px; font-weight: {strong_weight_css()};")


def _muted_style() -> str:
    tm = ThemeManager()
    return (f"color: {tm.color('text_muted')}; font-size: 12px; "
            f"background: transparent; border: none;")


class BookmarkHoverCard(QWidget):
    """宿主窗口内的浮层：展示一条或多条书签（多条时上下排列）。

    不使用独立顶层窗口：与引用悬停卡同因——Wayland 下客户端无法摆放自己的
    顶层窗口，只有作为宿主子控件时位置与层级才可控。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setMouseTracking(True)
        self._pos_anim: Optional[QPropertyAnimation] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(CARD_SHADOW, CARD_SHADOW, CARD_SHADOW, CARD_SHADOW)

        self._card = QFrame()
        self._card.setObjectName("BookmarkCard")
        shadow = QGraphicsDropShadowEffect(self._card)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(0, 0, 0, 120))
        self._card.setGraphicsEffect(shadow)
        outer.addWidget(self._card)

        self._lay = QVBoxLayout(self._card)
        self._lay.setContentsMargins(CARD_PAD, CARD_PAD, CARD_PAD, CARD_PAD)
        self._lay.setSpacing(6)

        self._scroll = QScrollArea(self._card)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget "
                                   "{ background: transparent; border: none; }")
        self._lay.addWidget(self._scroll)

        self._host_widget = QWidget()
        self._host_widget.setStyleSheet("background: transparent;")
        self._host_layout = QVBoxLayout(self._host_widget)
        self._host_layout.setContentsMargins(0, 0, 0, 0)
        self._host_layout.setSpacing(8)
        self._scroll.setWidget(self._host_widget)

        self.apply_theme()
        self.hide()

    # ------------------------------------------------------------------ #
    #  内容
    # ------------------------------------------------------------------ #
    def set_entries(self, entries: Sequence[Dict[str, Any]]):
        """写入要展示的条目（每条至少含 ``title`` / ``color``）。"""
        while self._host_layout.count() > 0:
            item = self._host_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for index, entry in enumerate(entries or []):
            if index:
                line = QFrame()
                line.setFixedHeight(1)
                line.setStyleSheet(
                    f"background-color: {hex_to_rgba(ThemeManager().color('border'), 0.7)};")
                self._host_layout.addWidget(line)
            self._host_layout.addWidget(self._entry_widget(entry))
        self._host_layout.addStretch(1)
        self._relayout()

    def _entry_widget(self, entry: Dict[str, Any]) -> QWidget:
        tm = ThemeManager()
        widget = QWidget()
        widget.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        color = normalize_color(entry.get("color"))
        title = QLabel(str(entry.get("title") or ""))
        title.setStyleSheet(_chip_style(color))
        title.setWordWrap(True)
        header.addWidget(title, 0)
        header.addStretch(1)
        layout.addLayout(header)

        desc = str(entry.get("description") or "").strip()
        if desc:
            desc_label = QLabel(desc)
            desc_label.setWordWrap(True)
            desc_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            desc_label.setStyleSheet(
                f"color: {tm.color('text_main')}; font-size: 12px; "
                f"background: transparent; border: none;")
            layout.addWidget(desc_label)
        return widget

    def _relayout(self):
        """按内容收敛卡片高度：内容多时由内部滚动承担，绝不超出宿主窗口。"""
        card_w = CARD_WIDTH - 2 * CARD_SHADOW
        self._card.setFixedWidth(card_w)
        inner_w = max(80, card_w - 2 * CARD_PAD - 8)
        self._host_widget.setFixedWidth(inner_w)
        self._host_widget.layout().activate()
        content_h = self._host_layout.sizeHint().height()
        content_h = max(28, min(content_h, CARD_MAX_CONTENT_H))
        self._scroll.setFixedHeight(content_h)

        self._lay.activate()
        height = self._lay.heightForWidth(card_w)
        if height <= 0:              # 含自动换行 QLabel 的布局可能算不出 heightForWidth
            height = self._lay.sizeHint().height()
        total = max(60, height + 2 * CARD_SHADOW)
        host = self.parentWidget()
        if host is not None:
            total = min(total, max(120, host.height() - 2 * CARD_GAP))
        self.setFixedSize(CARD_WIDTH, total)

    # ------------------------------------------------------------------ #
    #  主题 / 定位 / 显隐
    # ------------------------------------------------------------------ #
    def apply_theme(self):
        tm = ThemeManager()
        self._card.setStyleSheet(f"""
            QFrame#BookmarkCard {{
                background-color: {tm.color('bg_card')};
                border: 1px solid {tm.color('border')};
                border-radius: 8px;
            }}
        """)
        effect = self._card.graphicsEffect()
        if isinstance(effect, QGraphicsDropShadowEffect):
            effect.setColor(QColor(0, 0, 0, 110 if tm.current_theme == "dark" else 55))

    def contains_global(self, point: QPoint) -> bool:
        return self.isVisible() and self.rect().contains(self.mapFromGlobal(point))

    def show_at(self, anchor_rect: QRect, prefer: str = "auto"):
        """在锚点旁展示；``prefer="left"`` 用于滚动条标注（卡片向左展开）。"""
        host = self.parentWidget()
        if host is None:
            return
        self.apply_theme()
        anchor = QRect(host.mapFromGlobal(anchor_rect.topLeft()), anchor_rect.size())
        target = self._target_geometry(anchor, prefer)

        self._stop_anim()
        self.setGeometry(target.translated(0, 6))
        self.show()
        self.raise_()
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(130)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(self.pos())
        anim.setEndValue(target.topLeft())
        anim.start()
        self._pos_anim = anim

    def _target_geometry(self, anchor: QRect, prefer: str) -> QRect:
        host = self.parentWidget()
        bounds = host.rect() if host is not None else QRect(0, 0, 1280, 800)
        w, h = self.width(), self.height()
        ax, ay = anchor.left(), anchor.top()
        if prefer == "left":
            candidates = [
                QPoint(ax - w - CARD_GAP, ay),
                QPoint(ax + CARD_GAP, ay),
                QPoint(ax - w - CARD_GAP, ay - h),
                QPoint(ax + CARD_GAP, ay - h),
            ]
        else:
            candidates = [
                QPoint(ax + CARD_GAP, anchor.bottom() + CARD_GAP),
                QPoint(ax + CARD_GAP, ay - h - CARD_GAP),
                QPoint(ax - w - CARD_GAP, anchor.bottom() + CARD_GAP),
                QPoint(ax - w - CARD_GAP, ay - h - CARD_GAP),
            ]

        pad = 4
        for point in candidates:
            rect = QRect(point, QSize(w, h))
            if bounds.contains(rect):
                return rect
        x = min(max(candidates[0].x(), pad), max(pad, bounds.width() - w - pad))
        y = min(max(candidates[0].y(), pad), max(pad, bounds.height() - h - pad))
        return QRect(int(x), int(y), w, h)

    def _stop_anim(self):
        if self._pos_anim is not None and self._pos_anim.state() == QPropertyAnimation.Running:
            self._pos_anim.stop()

    def hide_card(self):
        self._stop_anim()
        self.hide()


class BookmarkHoverController(QObject):
    """进程级单例：管理悬停意图与预览卡生命周期。

    卡片按需创建、按宿主窗口重绑；不做状态缓存——调用方每次把当前条目递进来，
    因此书签被删除或标题被改动后不会展示过期内容。
    """

    _instance: Optional["BookmarkHoverController"] = None

    def __init__(self):
        super().__init__()
        self._card: Optional[BookmarkHoverCard] = None
        self._anchor_rect = QRect()
        self._pending: Optional[Tuple[List[Dict[str, Any]], QRect, Any, str]] = None
        self._outside_ticks = 0

        self._intent_timer = QTimer(self)
        self._intent_timer.setSingleShot(True)
        self._intent_timer.timeout.connect(self._present)

        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(WATCH_INTERVAL_MS)
        self._watch_timer.timeout.connect(self._watch_cursor)

    @classmethod
    def instance(cls) -> "BookmarkHoverController":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def show_entries(self, entries: Sequence[Dict[str, Any]], anchor_rect: QRect,
                     host=None, prefer: str = "auto"):
        """请求展示一组书签；锚点未变时刷新内容而不重新计时。"""
        entries = [e for e in (entries or []) if isinstance(e, dict) and e.get("title")]
        if not entries or not anchor_rect.isValid():
            self.leave()
            return
        self._anchor_rect = QRect(anchor_rect)
        self._pending = (list(entries), QRect(anchor_rect), host, prefer)
        self._intent_timer.start(HOVER_INTENT_MS)

    def leave(self):
        """离开锚点：取消尚未弹出的意图；已显示的卡片交给光标轮询收尾。"""
        self._intent_timer.stop()
        self._pending = None

    def follow(self, anchor_rect: QRect):
        """鼠标仍在同一条书签文字内移动：只更新锚点，不重排/重弹卡片。

        若不更新，锚点会停留在最初悬停的那一个点上：用户沿着带下划线的文字
        移动时，光标很快离开"锚点 ± 容差"，卡片就会被当成"鼠标已走开"而收起。
        """
        if not anchor_rect.isValid():
            return
        self._anchor_rect = QRect(anchor_rect)
        if self._pending is not None:
            entries, _old, host, prefer = self._pending
            self._pending = (entries, QRect(anchor_rect), host, prefer)
        self._outside_ticks = 0

    def dismiss(self):
        self._intent_timer.stop()
        self._watch_timer.stop()
        self._pending = None
        self._outside_ticks = 0
        if self._card is not None:
            self._card.hide_card()

    # ------------------------------------------------------------------ #
    #  内部
    # ------------------------------------------------------------------ #
    def _present(self):
        if self._pending is None:
            return
        entries, anchor, host, prefer = self._pending
        card = self._ensure_card(host)
        card.set_entries(entries)
        card.show_at(anchor, prefer)
        self._outside_ticks = 0
        if not self._watch_timer.isActive():
            self._watch_timer.start()

    def _ensure_card(self, host) -> BookmarkHoverCard:
        host = host or self._fallback_host()
        card = self._card
        if card is not None:
            try:
                card.isVisible()
            except RuntimeError:
                self._card = card = None
        if card is None:
            self._card = card = BookmarkHoverCard(host)
        elif card.parentWidget() is not host:
            card.hide_card()
            card.setParent(host)
        return card

    @staticmethod
    def _fallback_host():
        app = QApplication.instance()
        if app is None:
            return None
        window = app.activeWindow()
        if window is not None and window.isVisible():
            return window
        for widget in app.topLevelWidgets():
            if widget.isVisible() and widget.isWindow():
                return widget
        return None

    def _watch_cursor(self):
        card = self._card
        if card is None or not card.isVisible():
            self._watch_timer.stop()
            return
        pos = QCursor.pos()
        on_anchor = self._anchor_rect.adjusted(-10, -10, 10, 10).contains(pos)
        if on_anchor or card.contains_global(pos):
            self._outside_ticks = 0
            return
        self._outside_ticks += 1
        if self._outside_ticks >= WATCH_GRACE_TICKS:
            self.dismiss()


# --------------------------------------------------------------------------- #
#  滚动条标注条
# --------------------------------------------------------------------------- #
class BookmarkScrollStrip(QWidget):
    """对话区右侧的彩色标注条：显示书签在整段对话中的位置。

    位置换算：``ratio`` 是"把滚动条拖到这里，书签正好处在视口顶部"的比例，
    因此标注与滚动条滑块在同一坐标系里，拖动滑块即可对准。

    间距不足时由 :func:`src.core.bookmarks.layout_marks` 先摊开、再并组；
    并组后的标注上下排列，悬停一次性预览全部条目。
    """

    sig_jump = Signal(str)

    #: 标注条宽度（px）。
    WIDTH = 12
    #: 单个标注的最小 / 最大高度（px）。
    MARK_MIN_H = 4
    MARK_MAX_H = 6
    #: 同组标注之间的最小空隙（px）。
    GAP = 3
    #: 命中判定的纵向容差（px）：标注本身很细，点击/悬停目标需要放宽。
    HIT_PAD = 6
    #: 动画节拍（ms）与收敛系数。
    TICK_MS = 16
    EASE = 0.28

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent;")
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)

        self._entries: List[Dict[str, Any]] = []
        self._marks: Dict[str, float] = {}
        self._groups: List[Dict[str, Any]] = []
        self._appear: Dict[str, float] = {}
        self._hover_group = -1
        self._hover_p = 0.0

        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(self.TICK_MS)
        self._anim_timer.timeout.connect(self._step_anim)

        ThemeManager().theme_changed.connect(self.update)
        self.hide()

        host = parent
        if host is not None:
            host.installEventFilter(self)
            bar = self._scrollbar()
            if bar is not None:
                bar.installEventFilter(self)

    # ------------------------------------------------------------------ #
    #  数据
    # ------------------------------------------------------------------ #
    def set_entries(self, entries: Sequence[Dict[str, Any]]):
        """写入标注条目：``[{"id","title","description","color","ratio","msg_index"}]``。"""
        clean = []
        for entry in entries or []:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            try:
                ratio = float(entry.get("ratio", 0.0))
            except (TypeError, ValueError):
                continue
            item = dict(entry)
            item["ratio"] = min(max(ratio, 0.0), 1.0)
            item["color"] = normalize_color(entry.get("color"))
            clean.append(item)
        clean.sort(key=lambda e: e["ratio"])
        self._entries = clean

        keep = {e["id"] for e in clean}
        for key in list(self._appear):
            if key not in keep:
                self._appear.pop(key, None)
        for entry in clean:
            self._appear.setdefault(entry["id"], 0.0)

        self._relayout_marks()
        self._sync_geometry()
        if clean and not self._anim_timer.isActive():
            self._anim_timer.start()
        self.update()

    def is_empty(self) -> bool:
        return not self._entries

    def _relayout_marks(self):
        self._marks = {}
        self._groups = []
        if not self._entries or self.height() <= 0:
            return
        mark_h = self.MARK_MAX_H
        positions = [entry["ratio"] * max(0, self.height() - mark_h - 4) + 2
                     for entry in self._entries]
        groups = layout_marks(positions, self.height(), mark_h, self.GAP, 1)
        for group in groups:
            ids = []
            ys = []
            for index, y in group:
                entry = self._entries[index]
                self._marks[entry["id"]] = float(y)
                ids.append(entry["id"])
                ys.append(float(y))
            if ys:
                self._groups.append({
                    "ids": ids,
                    "top": min(ys),
                    "bottom": max(ys) + mark_h,
                })
        if self._hover_group >= len(self._groups):
            self._hover_group = -1

    def _entry_of(self, bookmark_id: str) -> Optional[Dict[str, Any]]:
        for entry in self._entries:
            if entry["id"] == bookmark_id:
                return entry
        return None

    # ------------------------------------------------------------------ #
    #  绘制
    # ------------------------------------------------------------------ #
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)

        for gi, group in enumerate(self._groups):
            hovered = (gi == self._hover_group)
            for bookmark_id in group["ids"]:
                y = self._marks.get(bookmark_id)
                entry = self._entry_of(bookmark_id)
                if y is None or entry is None:
                    continue
                progress = self._appear.get(bookmark_id, 0.0)
                if progress <= 0.01:
                    continue
                grow = self._hover_p if hovered else 0.0
                height = self.MARK_MIN_H + (self.MARK_MAX_H - self.MARK_MIN_H) * progress
                height += grow
                width = min(4 + 4 * progress + 2 * grow, self.WIDTH)
                color = QColor(entry["color"])
                color.setAlpha(int(min(1.0, progress) * 235))
                painter.setBrush(color)
                painter.drawRoundedRect(
                    self.width() - width - 1.0, y, width, height, height / 2.0, height / 2.0)

        painter.end()

    # ------------------------------------------------------------------ #
    #  动画
    # ------------------------------------------------------------------ #
    def _step_anim(self):
        settled = True
        for bookmark_id, value in list(self._appear.items()):
            new_value = value + (1.0 - value) * self.EASE
            if abs(1.0 - new_value) < 0.01:
                new_value = 1.0
            else:
                settled = False
            self._appear[bookmark_id] = new_value

        hover_target = 1.0 if self._hover_group >= 0 else 0.0
        self._hover_p += (hover_target - self._hover_p) * self.EASE
        if abs(hover_target - self._hover_p) < 0.01:
            self._hover_p = hover_target
        else:
            settled = False

        self.update()
        if settled:
            self._anim_timer.stop()

    # ------------------------------------------------------------------ #
    #  鼠标
    # ------------------------------------------------------------------ #
    def _group_at(self, y: float) -> int:
        best = -1
        best_distance = None
        for index, group in enumerate(self._groups):
            if group["top"] - self.HIT_PAD <= y <= group["bottom"] + self.HIT_PAD:
                return index
            center = (group["top"] + group["bottom"]) / 2.0
            distance = abs(y - center)
            if distance <= self.HIT_PAD * 2 and (best_distance is None or distance < best_distance):
                best, best_distance = index, distance
        return best

    def mouseMoveEvent(self, event):
        group_index = self._group_at(event.position().y())
        if group_index != self._hover_group:
            self._hover_group = group_index
            if not self._anim_timer.isActive():
                self._anim_timer.start()
            self._update_hover_card()

    def leaveEvent(self, event):
        self._hover_group = -1
        if not self._anim_timer.isActive():
            self._anim_timer.start()
        BookmarkHoverController.instance().leave()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mouseReleaseEvent(event)
        group_index = self._group_at(event.position().y())
        if group_index < 0:
            return super().mouseReleaseEvent(event)
        group = self._groups[group_index]
        pointer = event.position().y()
        nearest = min(group["ids"],
                      key=lambda bid: abs((self._marks.get(bid, 0.0) + 2) - pointer))
        BookmarkHoverController.instance().dismiss()
        logger.info("Bookmark strip click: jump to %s", nearest)
        self.sig_jump.emit(nearest)
        event.accept()

    def _update_hover_card(self):
        if self._hover_group < 0:
            BookmarkHoverController.instance().leave()
            return
        group = self._groups[self._hover_group]
        entries = [e for e in (self._entry_of(bid) for bid in group["ids"]) if e]
        if not entries:
            return
        anchor = QRect(
            self.mapToGlobal(QPoint(max(0, self.width() - 6), int(group["top"]))),
            QSize(6, max(4, int(group["bottom"] - group["top"]))))
        BookmarkHoverController.instance().show_entries(
            entries, anchor, self.window(), prefer="left")

    # ------------------------------------------------------------------ #
    #  几何
    # ------------------------------------------------------------------ #
    def _scrollbar(self):
        area = self.parentWidget()
        if isinstance(area, QAbstractScrollArea):
            return area.verticalScrollBar()
        return None

    def _sync_geometry(self):
        """贴在竖直滚动条左侧：不覆盖滑块，用户仍可直接拖动滚动条。"""
        area = self.parentWidget()
        bar = self._scrollbar()
        if area is None or bar is None:
            self.hide()
            return
        if not bar.isVisible() or bar.maximum() <= 0 or not self._entries:
            self.hide()
            return
        # 滚动条在 QAbstractScrollArea 里睡在一个内部容器中，bar.x()/y() 是相对
        # 该容器的坐标；必须换算到滚动区坐标系，否则标注条会贴在左上角。
        top_left = bar.mapTo(area, bar.rect().topLeft())
        x = max(0, top_left.x() - self.WIDTH - 1)
        self.setGeometry(x, top_left.y(), self.WIDTH, max(1, bar.height()))
        self._relayout_marks()
        self.show()
        self.raise_()

    def eventFilter(self, obj, event):
        etype = event.type()
        if obj is self.parentWidget() and etype in (QEvent.Resize, QEvent.Show, QEvent.LayoutRequest):
            self._sync_geometry()
        elif obj is self._scrollbar() and etype in (QEvent.Resize, QEvent.Show, QEvent.Hide,
                                                    QEvent.LayoutRequest):
            self._sync_geometry()
        return False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout_marks()


# --------------------------------------------------------------------------- #
#  创建 / 编辑对话框
# --------------------------------------------------------------------------- #
class BookmarkCreateDialog(BaseDialog):
    """新建书签：标题（必填、简短）、介绍（可选）、颜色（预设 + 自定义）。"""

    def __init__(self, parent=None, selected_text: str = ""):
        super().__init__(parent, title=tr("New Bookmark"), width=460)
        self._selected_color = DEFAULT_COLOR
        self._swatches: List[QPushButton] = []

        # 默认标题取选中文字的第一行，用户通常只需微调
        suggestion = " ".join((selected_text or "").split())[:TITLE_MAX]

        self.lbl_title = QLabel(tr("Title (required)"))
        self.edit_title = QLineEdit(suggestion)
        self.edit_title.setMaxLength(TITLE_MAX)
        self.edit_title.setPlaceholderText(tr("A short name for this bookmark"))

        self.lbl_desc = QLabel(tr("Description (optional)"))
        self.edit_desc = QTextEdit()
        self.edit_desc.setFixedHeight(72)
        self.edit_desc.setPlaceholderText(tr("Why is this passage worth keeping?"))

        self.lbl_selection = QLabel(tr("Selected text"))
        self.lbl_preview = QLabel("")
        self.lbl_preview.setWordWrap(True)
        self.lbl_preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        preview = " ".join((selected_text or "").split())
        if len(preview) > 160:
            preview = preview[:160] + "…"
        self.lbl_preview.setText(preview or "—")

        self.lbl_color = QLabel(tr("Color"))
        self.color_row = QHBoxLayout()
        self.color_row.setSpacing(6)
        for color in PALETTE:
            button = QPushButton()
            button.setFixedSize(26, 26)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _=False, c=color: self._select_color(c))
            self._swatches.append(button)
            self.color_row.addWidget(button)

        self.btn_custom = QPushButton(tr("Custom..."))
        self.btn_custom.setCursor(Qt.PointingHandCursor)
        self.btn_custom.clicked.connect(self._pick_custom_color)
        self.color_row.addWidget(self.btn_custom)
        self.color_row.addStretch(1)

        for widget in (self.lbl_title, self.edit_title, self.lbl_desc, self.edit_desc,
                       self.lbl_selection, self.lbl_preview, self.lbl_color):
            self.content_layout.addWidget(widget)
        self.content_layout.addLayout(self.color_row)

        self.btn_cancel = QPushButton(tr("Cancel"))
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_save = QPushButton(tr("Save"))
        self.btn_save.setCursor(Qt.PointingHandCursor)
        self.btn_save.clicked.connect(self.accept)
        self.footer_layout.addWidget(self.btn_cancel)
        self.footer_layout.addWidget(self.btn_save)

        self.edit_title.textChanged.connect(self._update_save_state)
        self._update_save_state()
        self._select_color(DEFAULT_COLOR)
        self.edit_title.setFocus()
        self.edit_title.selectAll()

    # ------------------------------------------------------------------ #
    def values(self) -> Tuple[str, str, str]:
        return (" ".join(self.edit_title.text().split())[:TITLE_MAX],
                " ".join(self.edit_desc.toPlainText().split())[:DESC_MAX],
                self._selected_color)

    def selected_color(self) -> str:
        return self._selected_color

    def _select_color(self, color: str):
        self._selected_color = normalize_color(color)
        tm = ThemeManager()
        for button, swatch in zip(self._swatches, PALETTE):
            active = swatch == self._selected_color
            border = tm.color("text_main") if active else "transparent"
            width = 2 if active else 0
            button.setStyleSheet(
                f"QPushButton {{ background-color: {swatch}; border: {width}px solid {border}; "
                f"border-radius: 13px; }}")
        self._update_save_state()

    def _pick_custom_color(self):
        color = QColorDialog.getColor(
            QColor(self._selected_color), self, tr("Pick a bookmark color"))
        if color.isValid():
            self._select_color(color.name())

    def _update_save_state(self):
        self.btn_save.setEnabled(bool(self.edit_title.text().strip()))

    # ------------------------------------------------------------------ #
    def _apply_theme(self):
        super()._apply_theme()
        tm = ThemeManager()
        for label in (getattr(self, "lbl_title", None), getattr(self, "lbl_desc", None),
                      getattr(self, "lbl_selection", None), getattr(self, "lbl_color", None)):
            if label is not None:
                label.setStyleSheet(
                    f"color: {tm.color('text_main')}; font-weight: {strong_weight_css()}; "
                    f"font-size: 12px;")
        if hasattr(self, "lbl_preview"):
            self.lbl_preview.setStyleSheet(
                f"color: {tm.color('text_muted')}; background-color: "
                f"{hex_to_rgba(tm.color('text_muted'), 0.10)}; border-radius: 6px; "
                f"padding: 8px; font-size: 12px;")
        if hasattr(self, "btn_save"):
            self.btn_save.setStyleSheet(
                f"QPushButton {{ background-color: {tm.color('accent')}; "
                f"color: {tm.color('bg_main')}; border: none; border-radius: 4px; "
                f"padding: 6px 18px; font-weight: {strong_weight_css()}; }}"
                f"QPushButton:disabled {{ background-color: {tm.color('btn_bg')}; "
                f"color: {tm.color('text_muted')}; }}")
            self.btn_cancel.setStyleSheet(
                f"QPushButton {{ background-color: {tm.color('btn_bg')}; "
                f"color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; "
                f"border-radius: 4px; padding: 6px 14px; }}"
                f"QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}")
        if hasattr(self, "btn_custom"):
            self.btn_custom.setStyleSheet(
                f"QPushButton {{ background-color: {tm.color('btn_bg')}; "
                f"color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; "
                f"border-radius: 4px; padding: 4px 10px; font-size: 12px; }}"
                f"QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}")
        # 颜色按钮的选中边框依赖当前主题文字色，主题切换后重刷一遍
        if getattr(self, "_swatches", None):
            self._select_color(self._selected_color)


class BookmarkSetDialog(BaseDialog):
    """书签集：按对话顺序列出全部书签（标题 + 颜色 + 介绍），支持跳转与删除。"""

    sig_jump = Signal(str)
    sig_delete = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, title=tr("Bookmarks"), width=560)
        self._entries: List[Dict[str, Any]] = []

        self.lbl_summary = QLabel("")
        self.content_layout.addWidget(self.lbl_summary)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setMinimumHeight(320)
        self.content_layout.addWidget(self.scroll, 1)

        self.host = QWidget()
        self.host.setStyleSheet("background: transparent;")
        self.host_layout = QVBoxLayout(self.host)
        self.host_layout.setContentsMargins(0, 0, 0, 0)
        self.host_layout.setSpacing(8)
        self.host_layout.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self.host)

        self.btn_close = QPushButton(tr("Close"))
        self.btn_close.clicked.connect(self.close)
        self.footer_layout.addWidget(self.btn_close)
        self.resize(560, 560)

    # ------------------------------------------------------------------ #
    def set_entries(self, entries: Sequence[Dict[str, Any]]):
        self._entries = [dict(e) for e in (entries or []) if isinstance(e, dict)]
        self._render_rows()

    def entries(self) -> List[Dict[str, Any]]:
        return list(self._entries)

    def _render_rows(self):
        if not hasattr(self, "host_layout"):
            return
        while self.host_layout.count() > 0:
            item = self.host_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self.lbl_summary.setText(
            tr("{n} bookmark(s) in this conversation").format(n=len(self._entries)))
        if not self._entries:
            empty = QLabel(tr("No bookmarks yet. Select text in a message, right-click, "
                              "and choose \"Add Bookmark\"."))
            empty.setWordWrap(True)
            empty.setStyleSheet(_muted_style() + " padding: 18px 6px;")
            self.host_layout.addWidget(empty)
            self.host_layout.addStretch(1)
            return

        for entry in self._entries:
            self.host_layout.addWidget(self._row_widget(entry))
        self.host_layout.addStretch(1)

    def _row_widget(self, entry: Dict[str, Any]) -> QWidget:
        tm = ThemeManager()
        frame = QFrame()
        frame.setObjectName("BookmarkRow")
        frame.setStyleSheet(
            f"QFrame#BookmarkRow {{ background-color: {hex_to_rgba(tm.color('text_muted'), 0.08)}; "
            f"border: 1px solid {tm.color('border')}; border-radius: 6px; }}")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 8, 8, 8)
        layout.setSpacing(10)

        bar = QFrame()
        bar.setFixedWidth(4)
        bar.setStyleSheet(
            f"background-color: {normalize_color(entry.get('color'))}; border-radius: 2px;")
        layout.addWidget(bar)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(3)

        title = QLabel(str(entry.get("title") or ""))
        title.setStyleSheet(_chip_style(normalize_color(entry.get("color"))))
        title.setWordWrap(True)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(title, 0)
        row.addStretch(1)
        text_layout.addLayout(row)

        desc = str(entry.get("description") or "").strip()
        if desc:
            desc_label = QLabel(desc)
            desc_label.setWordWrap(True)
            desc_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            desc_label.setStyleSheet(
                f"color: {tm.color('text_main')}; font-size: 12px; background: transparent; "
                f"border: none;")
            text_layout.addWidget(desc_label)
        layout.addLayout(text_layout, 1)

        bookmark_id = str(entry.get("id") or "")
        btn_jump = QPushButton(tr("Jump"))
        btn_jump.setCursor(Qt.PointingHandCursor)
        btn_jump.setStyleSheet(
            f"QPushButton {{ background-color: {tm.color('btn_bg')}; "
            f"color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; "
            f"border-radius: 4px; padding: 3px 10px; font-size: 12px; }}"
            f"QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}")
        btn_jump.clicked.connect(lambda _=False, bid=bookmark_id: self.sig_jump.emit(bid))

        btn_delete = QPushButton(tr("Delete"))
        btn_delete.setCursor(Qt.PointingHandCursor)
        btn_delete.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {tm.color('danger')}; "
            f"border: 1px solid {tm.color('border')}; border-radius: 4px; "
            f"padding: 3px 10px; font-size: 12px; }}"
            f"QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}")
        btn_delete.clicked.connect(lambda _=False, bid=bookmark_id: self.sig_delete.emit(bid))

        layout.addWidget(btn_jump)
        layout.addWidget(btn_delete)
        return frame

    # ------------------------------------------------------------------ #
    def _apply_theme(self):
        super()._apply_theme()
        tm = ThemeManager()
        if hasattr(self, "lbl_summary"):
            self.lbl_summary.setStyleSheet(
                f"color: {tm.color('text_muted')}; font-size: 12px;")
        if hasattr(self, "btn_close"):
            self.btn_close.setStyleSheet(
                f"QPushButton {{ background-color: {tm.color('btn_bg')}; "
                f"color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; "
                f"border-radius: 4px; padding: 6px 16px; }}"
                f"QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}")
        if getattr(self, "_entries", None) is not None:
            self._render_rows()


__all__ = [
    "BookmarkHoverCard", "BookmarkHoverController", "BookmarkScrollStrip",
    "BookmarkCreateDialog", "BookmarkSetDialog",
]
