"""行内引用（``[n]``）的悬停预览卡与溯源详情面板。

交互设计
--------
* **悬停**：鼠标停在正文的 ``[n]`` 上（悬停意图约 220ms）弹出**概览卡**：
  编号、来源类型、标题、作者/年份/期刊、DOI/链接，以及"复制引用"等操作。
  鼠标可以从锚点移入卡片继续操作，离开两者后才自动收起。
* **点击**：在概览卡基础上**原地长大**为**详情面板**（几何连续变形，不闪烁、
  不重建），额外展示该引用对应的**支撑原文片段**（按文本实际高度自适应，
  过长才滚动）与引用理由，便于溯源。
* **详情面板**只在卡片自身持有键盘焦点时响应 Esc，或点击标题栏关闭按钮；点击
  程序本体不会关闭它。面板可通过标题栏或卡片空白处拖动（限制在宿主窗口内）。

它是"窗口内的浮层"，不是独立窗口
--------------------------------
卡片是**宿主窗口（主窗口）的子控件**，而非 ``Qt.Tool`` 顶层窗口。这是被
KDE/Wayland 逼出来的唯一可行做法：Wayland 协议不允许客户端摆放自己的顶层
窗口（``move()`` / ``setGeometry()`` 会被合成器静默忽略），于是顶层窗口方案
必然出现"位置不听话、拖不动、还跑到主窗口下面"这三连；作为子控件时，位置、
层级与拖动全部由程序掌控，且不会脱离主窗口单独漂浮。

视觉规格
--------
卡片为左右偏长的矩形：宽度固定且设有上限（概览 340px / 详情 480px），文字自动
换行；高度按内容自适应并以宿主窗口高度为上限，超出部分由原文区自身滚动承担。

落点位置
--------
自鼠标（引用锚点）**右下方**弹出为默认；该方向空间不足时按"右/左、下/上"四个
方位择优选位，最后仍越界则夹取到宿主窗口内，保证始终完整可见且紧贴锚点。

缓存与数据来源
--------------
``CitationPopupController`` 是一个进程级单例，持有 ``(消息编号, 引用编号) -> 条目``
的映射，由任务层通过结构化事件（``references``）增量同步（见 response_flow）。
正文里的 ``[n]`` 只携带编号，因此渲染/重渲染都不依赖气泡对象的生命周期。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from PySide6.QtCore import (QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation,
                            QRect, Qt, QTimer)
from PySide6.QtGui import QCursor, QDesktopServices, QGuiApplication, QKeyEvent
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QApplication, QFrame, QGraphicsDropShadowEffect,
                               QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QTextBrowser, QVBoxLayout, QWidget)

from src.core.theme_manager import ThemeManager, strong_weight_css
from src.ui.components.toast import ToastManager

logger = logging.getLogger("UI.CitationPopup")

#: 概览卡宽度（px）：偏长矩形，容纳一行标题 + 一两行著录。
COMPACT_WIDTH = 340
#: 详情面板宽度上限（px）：比概览更宽，但不至于横跨半屏。
FULL_WIDTH = 480
#: 阴影留白（px）：卡片四周留给投影的透明边距。
SHADOW_MARGIN = 10
#: 支撑原文区高度边界（px）：按文本实际高度取，超过上限才由其自身滚动。
SNIPPET_MIN_HEIGHT = 30
SNIPPET_MAX_HEIGHT = 200
#: 支撑原文区 QSS 内边距 / 边框宽度（px）：与样式串共用，保证高度测算同源。
SNIPPET_PADDING = 6
SNIPPET_BORDER = 1
#: 卡片内容边距 / 行距（px）：排版取紧凑值，避免"大而空"。
CARD_MARGIN_H = 14
CARD_MARGIN_V = 12
CARD_SPACING = 8
#: 落屏时与鼠标（锚点）的呼吸间距（px）。
POSITION_GAP = 10
#: 标题栏高度（px）：徽标与类型 chip 固定在此高度，保持"信息位"应有的紧凑尺寸。
TITLE_BAR_HEIGHT = 22
#: 落点夹取到宿主窗口内时保留的边缘呼吸（px）。
HOST_EDGE_PAD = 4
#: 悬停意图延迟（ms）：避免鼠标划过时卡片乱闪。
HOVER_INTENT_MS = 220
#: 光标位置轮询间隔（ms）：用于判断"是否已离开锚点与卡片"。
WATCH_INTERVAL_MS = 140
#: 概览态允许的"在外"轮询次数（约 300ms 宽限，足够从锚点移到卡片）。
COMPACT_GRACE_TICKS = 2

#: 来源类型 -> 英文短标签（与 references.kind 对应）。
#: 用英文而非中文：该 chip 是"信息位"上的分类徽标，长度必须短且稳定，
#: 中文字符串在不同字号下宽度差异大，容易把标题栏顶高。
_KIND_LABELS = {
    "reference": "Reference",
    "article": "Journal Article",
    "preprint": "Preprint",
    "dataset": "Dataset",
    "web": "Web Source",
    "book": "Book",
    "local_document": "Local Document",
}


def _kind_label(kind: str) -> str:
    return _KIND_LABELS.get(str(kind or "").lower(), "Reference")


class CitationPopup(QWidget):
    """宿主窗口内的浮层卡片：同时承载"悬停概览"与"点击详情"两种形态。

    它是宿主窗口（主窗口）的**子控件**而非独立窗口，理由见模块 docstring：
    Wayland 下顶层窗口的位置/层级/拖动都不由客户端决定，只有子控件能完全掌控。
    """

    MODE_COMPACT = "compact"
    MODE_FULL = "full"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode = self.MODE_COMPACT
        self._data: Dict[str, Any] = {}
        #: 锚点矩形（宿主窗口坐标系；:meth:`set_anchor` 会把全局坐标换算进来）
        self._anchor_rect = QRect()
        self._geo_anim: Optional[QPropertyAnimation] = None
        #: 拖动：按下时的全局光标点 + 卡片在宿主内的左上角（差值法最稳）
        self._drag_origin: Optional[QPoint] = None
        self._press_global: Optional[QPoint] = None

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        # 浮层不参与布局（绝对定位），也不接受外部样式表背景
        self.setAttribute(Qt.WA_StyledBackground, False)

        self._build_ui()
        self.apply_theme()
        self.hide()
        self._install_host_filter()

    # ------------------------------------------------------------------ #
    #  构建
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN)

        self._card = QFrame()
        self._card.setObjectName("CitationCard")
        self._card.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        shadow = QGraphicsDropShadowEffect(self._card)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 5)
        shadow.setColor(Qt.black)
        self._card.setGraphicsEffect(shadow)
        outer.addWidget(self._card)

        lay = QVBoxLayout(self._card)
        lay.setContentsMargins(CARD_MARGIN_H, CARD_MARGIN_V, CARD_MARGIN_H, CARD_MARGIN_V)
        lay.setSpacing(CARD_SPACING)
        self._lay = lay

        # --- 标题栏：编号徽标 + 来源类型 + 关闭按钮（详情态可见） ---
        #     独立成 widget 既便于统一样式（下边框），也作为拖动抓手，
        #     语义上就是"标题栏"：点标题栏的关闭按钮才是唯一的手动关闭入口。
        #     高度固定：信息位上的徽标/chip 必须适配一行文字的高度，不能被布局
        #     拉伸成大方块（否则一旦上层分配了多余空间，整条标题栏会被撑高）。
        self._title_bar = QFrame()
        self._title_bar.setObjectName("CitationTitleBar")
        self._title_bar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._title_bar.setFixedHeight(TITLE_BAR_HEIGHT)
        header = QHBoxLayout(self._title_bar)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)

        self._lbl_index = QLabel("[?]")
        self._lbl_index.setObjectName("CitationIndexBadge")
        self._lbl_index.setAlignment(Qt.AlignCenter)
        self._lbl_index.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._lbl_index.setFixedHeight(TITLE_BAR_HEIGHT)
        header.addWidget(self._lbl_index)

        self._lbl_kind = QLabel("")
        self._lbl_kind.setObjectName("CitationKindChip")
        self._lbl_kind.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._lbl_kind.setFixedHeight(TITLE_BAR_HEIGHT)
        header.addWidget(self._lbl_kind)
        header.addStretch(1)

        self._btn_close = QPushButton("✕")
        self._btn_close.setObjectName("CitationClose")
        self._btn_close.setCursor(Qt.PointingHandCursor)
        self._btn_close.setFocusPolicy(Qt.NoFocus)
        self._btn_close.setFixedSize(20, 20)
        self._btn_close.clicked.connect(lambda: CitationPopupController.instance().dismiss())
        self._btn_close.setVisible(False)
        header.addWidget(self._btn_close)
        lay.addWidget(self._title_bar)

        # --- 标题 ---
        self._lbl_title = QLabel("")
        self._lbl_title.setObjectName("CitationTitle")
        self._lbl_title.setWordWrap(True)
        self._lbl_title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self._lbl_title)

        # --- 著录信息：作者 · 年份 · 期刊 ---
        self._lbl_meta = QLabel("")
        self._lbl_meta.setObjectName("CitationMeta")
        self._lbl_meta.setWordWrap(True)
        self._lbl_meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self._lbl_meta)

        # --- 链接行：DOI / URL（点击交系统浏览器打开） ---
        self._lbl_links = QLabel("")
        self._lbl_links.setObjectName("CitationLinks")
        self._lbl_links.setWordWrap(True)
        self._lbl_links.setTextFormat(Qt.RichText)
        self._lbl_links.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self._lbl_links.setOpenExternalLinks(True)
        lay.addWidget(self._lbl_links)

        # --- 操作按钮（NoFocus：避免抢走焦点导致 Esc 收不到） ---
        self._btn_row = QHBoxLayout()
        self._btn_row.setSpacing(6)
        self._btn_copy = QPushButton("Copy citation")
        self._btn_copy.setCursor(Qt.PointingHandCursor)
        self._btn_copy.setFocusPolicy(Qt.NoFocus)
        self._btn_copy.clicked.connect(self._copy_citation)
        self._btn_open = QPushButton("Open source")
        self._btn_open.setCursor(Qt.PointingHandCursor)
        self._btn_open.setFocusPolicy(Qt.NoFocus)
        self._btn_open.clicked.connect(self._open_source)
        self._btn_row.addWidget(self._btn_copy)
        self._btn_row.addWidget(self._btn_open)
        self._btn_row.addStretch(1)
        lay.addLayout(self._btn_row)

        # --- 支撑原文（仅详情态可见；高度按文本实测，过长才滚动） ---
        self._snippet_title = QLabel("Cited passage")
        self._snippet_title.setObjectName("CitationSectionTitle")
        self._snippet_title.setVisible(False)
        lay.addWidget(self._snippet_title)

        self._snippet = QTextBrowser()
        self._snippet.setObjectName("CitationSnippet")
        self._snippet.setOpenExternalLinks(True)
        self._snippet.setFrameShape(QFrame.NoFrame)
        self._snippet.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._snippet.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._snippet.setVisible(False)
        lay.addWidget(self._snippet)

        self._lbl_note = QLabel("")
        self._lbl_note.setObjectName("CitationNote")
        self._lbl_note.setWordWrap(True)
        self._lbl_note.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._lbl_note.setVisible(False)
        lay.addWidget(self._lbl_note)

        # 尾部弹性占位：若因任何原因（动画中途、平台度量差异）卡片被分配了
        # 超出内容的高度，多余空间由它吸收，绝不把标题栏等组件拉伸变形。
        lay.addStretch(1)

        # 子控件统一挂事件过滤器：在任意子控件上按 Esc 都能关闭
        # （子控件会抢键盘焦点，只在窗口本体处理会漏掉这些情况）。
        for child in self.findChildren(QWidget):
            child.installEventFilter(self)

    # ------------------------------------------------------------------ #
    #  主题
    # ------------------------------------------------------------------ #
    def apply_theme(self):
        tm = ThemeManager()
        fam = tm.font_family()
        accent = tm.color("accent")
        card_bg = tm.color("bg_card")
        border = tm.color("border")
        text_main = tm.color("text_main")
        muted = tm.color("text_muted")
        hover = tm.color("btn_hover")
        strong = strong_weight_css()

        self._card.setStyleSheet(f"""
            QFrame#CitationCard {{
                background-color: {card_bg};
                border: 1px solid {border};
                border-radius: 10px;
            }}
            QFrame#CitationTitleBar {{
                border: none;
                border-bottom: 1px solid {border};
            }}
            QLabel#CitationIndexBadge {{
                color: #ffffff; background-color: {accent};
                border-radius: 5px; padding: 1px 7px;
                font-family: {fam}; font-size: 11px; font-weight: {strong};
            }}
            QLabel#CitationKindChip {{
                color: {accent}; background-color: rgba(127,127,127,0.14);
                border-radius: 5px; padding: 1px 7px;
                font-family: {fam}; font-size: 10px;
            }}
            QLabel#CitationTitle {{
                color: {text_main}; font-family: {fam};
                font-size: 13px; font-weight: {strong};
            }}
            QLabel#CitationMeta {{
                color: {muted}; font-family: {fam}; font-size: 11px;
            }}
            QLabel#CitationLinks {{
                color: {muted}; font-family: {fam}; font-size: 11px;
            }}
            QLabel#CitationSectionTitle {{
                color: {muted}; font-family: {fam}; font-size: 10px;
                font-weight: {strong};
            }}
            QLabel#CitationNote {{
                color: {muted}; font-family: {fam}; font-size: 11px;
                font-style: italic;
            }}
            QTextBrowser#CitationSnippet {{
                color: {text_main}; background-color: rgba(127,127,127,0.10);
                border: {SNIPPET_BORDER}px solid {border}; border-radius: 6px;
                padding: {SNIPPET_PADDING}px;
                font-family: {fam}; font-size: 11px;
            }}
            QPushButton {{
                color: {text_main}; background-color: rgba(127,127,127,0.10);
                border: 1px solid {border}; border-radius: 5px;
                padding: 3px 10px; font-family: {fam}; font-size: 11px;
            }}
            QPushButton:hover {{ background-color: {hover}; }}
            QPushButton#CitationClose {{
                border: none; background: transparent; color: {muted};
                font-size: 12px; padding: 0px;
            }}
            QPushButton#CitationClose:hover {{ color: {text_main}; }}
        """)
        # 阴影颜色跟随主题取深色，保证浅色模式下也有层次。
        effect = self._card.graphicsEffect()
        if isinstance(effect, QGraphicsDropShadowEffect):
            from PySide6.QtGui import QColor
            effect.setColor(QColor(0, 0, 0, 110 if tm.current_theme == "dark" else 60))

    # ------------------------------------------------------------------ #
    #  内容
    # ------------------------------------------------------------------ #
    def set_anchor(self, anchor_rect: QRect):
        """记录锚点矩形（入参为**全局**坐标，内部换算到宿主窗口坐标系）。

        卡片是宿主的子控件，所以后续定位/夹取一律在宿主坐标系里做，只有锚点
        是从气泡传来的全局矩形，需要在这里换算一次。
        """
        if not anchor_rect.isValid():
            self._anchor_rect = QRect()
            return
        host = self.parentWidget()
        if host is not None:
            self._anchor_rect = QRect(host.mapFromGlobal(anchor_rect.topLeft()),
                                      anchor_rect.size())
        else:
            self._anchor_rect = QRect(anchor_rect)

    # ------------------------------------------------------------------ #
    #  宿主窗口（浮层坐标系）
    # ------------------------------------------------------------------ #
    def _host_bounds(self) -> QRect:
        """浮层可用的落点范围：宿主窗口客户区（子控件坐标系）。

        宿主缺失时（理论上不会发生）退回主屏可用区域，此时卡片本身即顶层窗口，
        局部坐标与全局坐标一致，逻辑仍然成立。
        """
        host = self.parentWidget()
        if host is not None:
            return host.rect()
        screen = QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)

    def _install_host_filter(self):
        """监听宿主尺寸变化：窗口缩小时把浮层重新夹回可见区域内。"""
        host = self.parentWidget()
        if host is not None:
            host.installEventFilter(self)

    def rebind_host(self, host) -> None:
        """换宿主窗口（卡片创建时取的窗口与气泡所在窗口不一致的兜底）。"""
        if host is None or host is self.parentWidget():
            return
        old = self.parentWidget()
        if old is not None:
            old.removeEventFilter(self)
        self.hide_popup()
        self.setParent(host)
        host.installEventFilter(self)

    def contains_global(self, point) -> bool:
        """全局坐标点是否落在卡片内（浮层不是窗口，frameGeometry 不可用于此判断）。"""
        return self.isVisible() and self.rect().contains(self.mapFromGlobal(point))

    def set_reference(self, data: Dict[str, Any], mode: str):
        """写入条目并切换形态（内容变化会触发一次尺寸重算）。"""
        self._data = data or {}
        self._mode = mode
        index = self._data.get("index")
        self._lbl_index.setText(f"[{index}]" if index not in (None, "") else "[?]")
        self._lbl_kind.setText(_kind_label(self._data.get("kind", "")))

        title = str(self._data.get("title") or "").strip()
        self._lbl_title.setText(title or "Untitled")
        self._lbl_title.setVisible(True)

        self._lbl_meta.setText(self._meta_text())
        self._lbl_links.setText(self._links_html())

        full = (mode == self.MODE_FULL)
        self._btn_close.setVisible(full)
        self._snippet_title.setVisible(full)
        self._snippet.setVisible(full)
        note = str(self._data.get("note") or "").strip()
        self._lbl_note.setVisible(full and bool(note))
        self._lbl_note.setText(f"Why cited: {note}" if note else "")

        if full:
            snippet = str(self._data.get("snippet") or "").strip()
            if snippet:
                self._snippet.setHtml(self._snippet_html(snippet))
            else:
                self._snippet.setHtml(
                    "<span style='opacity:0.7;'>No cited passage was recorded "
                    "for this entry.</span>")

        self._relayout()

    def refresh(self, data: Dict[str, Any]):
        """数据更新时按当前形态刷新内容（不改变形态）。"""
        self.set_reference(data, self._mode)

    def _meta_text(self) -> str:
        parts = []
        authors = str(self._data.get("authors") or "").strip()
        year = str(self._data.get("year") or "").strip()
        journal = str(self._data.get("journal") or "").strip()
        if authors:
            parts.append(authors)
        if year:
            parts.append(f"({year})")
        if journal:
            parts.append(f"· {journal}")
        if not parts:
            # 本地文档退化为文件名提示。
            path = str(self._data.get("path") or "").strip()
            if path:
                from os.path import basename
                parts.append(f"Local document · {basename(path)}")
        return " ".join(parts)

    def _links_html(self) -> str:
        from html import escape
        tm = ThemeManager()
        accent = tm.color("accent")
        rows = []
        doi = str(self._data.get("doi") or "").strip()
        url = str(self._data.get("url") or "").strip()
        path = str(self._data.get("path") or "").strip()
        if doi:
            rows.append(f"DOI: <a href='https://doi.org/{escape(doi)}' "
                        f"style='color:{accent}; text-decoration:none;'>{escape(doi)}</a>")
        if url:
            rows.append(f"<a href='{escape(url)}' "
                        f"style='color:{accent}; text-decoration:none;'>{escape(url)}</a>")
        if not doi and not url and path:
            from html import escape as _e
            rows.append(f"<span style='opacity:0.75;'>{_e(path)}</span>")
        return "<br>".join(rows)

    @staticmethod
    def _snippet_html(snippet: str) -> str:
        from html import escape
        tm = ThemeManager()
        text = escape(snippet).replace("\n", "<br>")
        return f"<div style='color:{tm.color('text_main')};'>{text}</div>"

    # ------------------------------------------------------------------ #
    #  尺寸与定位
    # ------------------------------------------------------------------ #
    def _relayout(self):
        """按当前形态与内容把卡片收敛到确定尺寸（宽度固定、高度自适应）。

        原文区高度按文档实测高度取：QTextBrowser 的默认 sizeHint 与内容无关
        （两行文字也会占满一个大空框，正是"大而空"的来源），必须显式按文本
        高度收敛，超过上限时才交给其自身滚动条。
        """
        width = FULL_WIDTH if self._mode == self.MODE_FULL else COMPACT_WIDTH
        card_w = width - 2 * SHADOW_MARGIN
        self._card.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._card.setFixedWidth(card_w)
        # 恢复原文区的固定尺寸策略（变形动画期间会被临时放松）。
        self._snippet.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        # 1) 先按固定宽度跑一遍布局，再**按该宽度**求所需高度。
        #    不能只用 sizeHint()：含自动换行 QLabel 的布局，sizeHint() 与
        #    heightForWidth() 结果不同，前者偏大时卡片就高于实际内容——多余空间
        #    会被布局分给标题栏等组件，正是"顶部徽标被撑成大块、整体大而空"
        #    的来源。
        self._lay.activate()
        if self._snippet.isVisible():
            self._fit_snippet_height(card_w)
            self._lay.activate()
        height = self._lay.heightForWidth(card_w)
        if height <= 0:
            height = self._lay.sizeHint().height()

        # 2) 宿主窗口高度上限：先收缩原文区，仍超出才夹取总高度。
        #    浮层是宿主窗口的子控件，超出部分根本画不出来，故上限取宿主客户区高度。
        bounds = self._host_bounds()
        max_total = max(SNIPPET_MIN_HEIGHT + 160,
                        int(bounds.height() - 2 * HOST_EDGE_PAD))
        total = height + 2 * SHADOW_MARGIN
        if total > max_total and self._snippet.isVisible():
            new_h = max(SNIPPET_MIN_HEIGHT, self._snippet.height() - (total - max_total))
            self._snippet.setFixedHeight(new_h)
            self._lay.activate()
            height = self._lay.heightForWidth(card_w)
            if height <= 0:
                height = self._lay.sizeHint().height()
            total = height + 2 * SHADOW_MARGIN
        if total > max_total:
            total = max_total

        self._card.setFixedHeight(max(1, total - 2 * SHADOW_MARGIN))
        self.setFixedWidth(width)
        self.setFixedHeight(total)

    def _fit_snippet_height(self, card_w: int):
        """把支撑原文区收敛到文本实际高度（上限 ``SNIPPET_MAX_HEIGHT``）。

        浏览器内容宽度由本函数显式固定，再用同一宽度测量文档高度，避免
        "按估算宽度排版、按实际宽度显示"导致的多一行/少一行误差。
        """
        content_w = max(120, card_w - 2 * CARD_MARGIN_H)
        viewport_w = max(80, content_w - 2 * (SNIPPET_PADDING + SNIPPET_BORDER) - 1)
        self._snippet.setFixedWidth(content_w)
        doc = self._snippet.document()
        doc.setTextWidth(viewport_w)
        # 文档高度 + 上下内边距 + 边框 + 取整余量
        needed = int(doc.size().height()) + 2 * (SNIPPET_PADDING + SNIPPET_BORDER) + 2
        self._snippet.setFixedHeight(max(SNIPPET_MIN_HEIGHT, min(needed, SNIPPET_MAX_HEIGHT)))

    @staticmethod
    def _overflow(rect: QRect, avail: QRect):
        """矩形相对可用区域的越界量 ``(水平px, 垂直px)``；``(0, 0)`` 表示完整容纳。"""
        over_x = max(0, avail.left() - rect.left()) + max(0, rect.right() - avail.right())
        over_y = max(0, avail.top() - rect.top()) + max(0, rect.bottom() - avail.bottom())
        return int(over_x), int(over_y)

    def target_geometry(self) -> QRect:
        """在宿主窗口内选点：按"右下 → 右上 → 左下 → 左上"择优选位。

        以鼠标（引用锚点）为参照点，依次尝试四种"卡片锚角 → 落点方位"组合：

        1. 卡片**左上角**贴鼠标 → 卡片落在鼠标**右下方**（首选，最自然）；
        2. 卡片**左下角**贴鼠标 → 卡片落在鼠标**右上方**（下方空间不够时）；
        3. 卡片**右上角**贴鼠标 → 卡片落在鼠标**左下方**（右方空间不够时）；
        4. 卡片**右下角**贴鼠标 → 卡片落在鼠标**左上方**（右下均不够时）。

        任一方位能完整落在宿主客户区内即采用；四个方位都放不下时，取越界量最小
        者再夹取，保证卡片始终完整可见、且尽量贴近鼠标。

        注意：这里全部是**宿主坐标系**（卡片是宿主的子控件）。Wayland 下若按
        顶层窗口计算，合成器会忽略我们的位置，"离着老远"就是这么来的。
        """
        avail = self._host_bounds().adjusted(HOST_EDGE_PAD, HOST_EDGE_PAD,
                                             -HOST_EDGE_PAD, -HOST_EDGE_PAD)
        w, h = self.width(), self.height()
        if self._anchor_rect.isValid():
            anchor = self._anchor_rect
        else:
            anchor = QRect(self.mapFromGlobal(QCursor.pos()), QRect().size())
        gap = POSITION_GAP
        # 以"鼠标点"为参照：锚点字符矩形的右下角，视觉上就是光标本体位置。
        mx, my = anchor.right(), anchor.bottom()

        candidates = (
            QPoint(mx + gap, my + gap),            # 左上角为锚 -> 右下方（首选）
            QPoint(mx + gap, my - gap - h),        # 左下角为锚 -> 右上方
            QPoint(mx - gap - w, my + gap),        # 右上角为锚 -> 左下方
            QPoint(mx - gap - w, my - gap - h),    # 右下角为锚 -> 左上方
        )

        best, best_score = None, None
        for point in candidates:
            rect = QRect(point.x(), point.y(), w, h)
            over_x, over_y = self._overflow(rect, avail)
            if over_x == 0 and over_y == 0:
                return rect                       # 完整可见：直接采用
            score = over_x + over_y
            if best_score is None or score < best_score:
                best, best_score = rect, score

        # 四个方位都放不下：取越界最少者并夹取到宿主客户区内（保底可见）。
        max_x = max(avail.left(), avail.left() + avail.width() - w)
        max_y = max(avail.top(), avail.top() + avail.height() - h)
        x = min(max(best.x(), avail.left()), max_x)
        y = min(max(best.y(), avail.top()), max_y)
        return QRect(int(x), int(y), w, h)

    # ------------------------------------------------------------------ #
    #  展示 / 隐藏（含过渡动画）
    # ------------------------------------------------------------------ #
    def show_with_animation(self, anchor_rect: QRect, mode: str, from_rect: Optional[QRect] = None):
        """显示并播放过渡动画。

        全程不使用 ``setWindowOpacity``：部分平台插件不支持窗口透明度，会刷出
        "This plugin does not support setting window opacity" 告警，改用纯几何
        动画表达入场与形态变化。

        * 概览 -> 详情（传入 ``from_rect``）：从概览几何**连续变形**到详情几何
          （位置 + 尺寸一起插值），是"原地长大"，没有关闭再打开的重建感；
        * 首次出现：自落点附近轻微位移入场。

        变形期间临时解除固定尺寸约束，结束后由 :meth:`_relayout` 重新锁定，
        保证静止态尺寸精确。
        """
        self.set_anchor(anchor_rect)
        target = self.target_geometry()
        morph = (QRect(from_rect)
                 if from_rect is not None and from_rect.isValid()
                 and from_rect.size() != target.size() else None)

        self._stop_animations()
        if morph is not None:
            self._release_fixed_size()
            self.setGeometry(morph)
        else:
            start = QRect(target.topLeft() + QPoint(0, 8), target.size())
            self.setGeometry(start)
        self.show()
        # 浮层要在宿主窗口的其它子控件之上（对子控件 raise_ 即置顶，无需窗口管理器）
        self.raise_()

        if morph is not None:
            self._geo_anim = QPropertyAnimation(self, b"geometry", self)
            self._geo_anim.setDuration(190)
            self._geo_anim.setStartValue(morph)
            self._geo_anim.setEndValue(target)
            self._geo_anim.setEasingCurve(QEasingCurve.OutCubic)
            self._geo_anim.finished.connect(self._relayout)
            self._geo_anim.start()
        elif target.topLeft() != start.topLeft():
            self._geo_anim = QPropertyAnimation(self, b"geometry", self)
            self._geo_anim.setDuration(140)
            self._geo_anim.setStartValue(start)
            self._geo_anim.setEndValue(target)
            self._geo_anim.setEasingCurve(QEasingCurve.OutCubic)
            self._geo_anim.start()

    def _release_fixed_size(self):
        """解除窗口与卡片的固定尺寸，允许几何动画期间连续缩放。

        卡片改为 Expanding：变形过程中随窗口宽度连续变化（文字实时重排），
        动画结束后由 :meth:`_relayout` 恢复 Fixed 并锁定精确尺寸。
        """
        max_side = 16777215  # QWIDGETSIZE_MAX
        for widget in (self, self._card):
            widget.setMinimumWidth(0)
            widget.setMinimumHeight(0)
            widget.setMaximumWidth(max_side)
            widget.setMaximumHeight(max_side)
        self._card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # 原文区宽度也要放松：否则变形途中它仍按"详情宽度"固定，会横向溢出被裁剪。
        if self._snippet.isVisible():
            self._snippet.setMinimumWidth(0)
            self._snippet.setMaximumWidth(max_side)
            self._snippet.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def hide_popup(self):
        """立即隐藏（不做透明度淡出，见 :meth:`show_with_animation` 说明）。"""
        self._stop_animations()
        self._drag_origin = None
        self._press_global = None
        self.hide()

    def _stop_animations(self):
        if self._geo_anim is not None and self._geo_anim.state() == QPropertyAnimation.Running:
            self._geo_anim.stop()

    # ------------------------------------------------------------------ #
    #  拖动 / 键盘
    # ------------------------------------------------------------------ #
    #: 拖动 = 直接移动**子控件**（父坐标），这在 Wayland 下同样有效；换成顶层窗口
    #: 就必须走 startSystemMove()，而容器本身的位置我们已无权决定。事件处理放在本
    #: 体而非子控件的事件过滤器里：过滤器 return True 只是"吃掉"事件，抓手控件拿
    #: 不到隐式鼠标抓取，光标一离开抓手拖动就会中断。子控件默认忽略鼠标事件并逐级
    #: 冒泡到这里，因此标题栏、卡片留白、各处间距都能作为抓手。
    def mousePressEvent(self, event):
        if self._mode == self.MODE_FULL and event.button() == Qt.LeftButton:
            # 用户主动点卡片时才夺取键盘焦点（展开时不抢），Esc 因此只在
            # "卡片确实持有焦点"时生效，与约定一致。
            self.setFocus(Qt.MouseFocusReason)
            self._drag_origin = self.pos()
            self._press_global = event.globalPosition().toPoint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_origin is not None and (event.buttons() & Qt.LeftButton):
            delta = event.globalPosition().toPoint() - self._press_global
            target = self._drag_origin + delta
            # 限制在宿主客户区内：浮层是子控件，拖出窗口就看不见了。
            bounds = self._host_bounds()
            target.setX(min(max(target.x(), 0), max(0, bounds.width() - self.width())))
            target.setY(min(max(target.y(), 0), max(0, bounds.height() - self.height())))
            self.move(target)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_origin is not None:
            self._drag_origin = None
            self._press_global = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def eventFilter(self, obj, event):
        """事件过滤：宿主窗口缩放时把浮层夹回可见区；子控件上按 Esc 关闭。

        子控件会抢键盘焦点（可选中文本的 QLabel、QTextBrowser 等），只在浮层
        本体处理 ``keyPressEvent`` 会漏掉这些情况，故统一在过滤器里拦截 Esc。
        """
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key_Escape:
            CitationPopupController.instance().dismiss()
            return True
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize \
                and self.isVisible():
            # 宿主变小后卡片可能落到窗口外（子控件不会溢出父窗口显示），重新夹回。
            self.move(self.target_geometry().topLeft())
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event: QKeyEvent):
        """浮层持有键盘焦点时，Esc 关闭；其它按键交回默认处理。"""
        if event.key() == Qt.Key_Escape:
            CitationPopupController.instance().dismiss()
            event.accept()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------ #
    #  操作
    # ------------------------------------------------------------------ #
    def _copy_citation(self):
        from src.core.references import ReferenceItem
        try:
            item = ReferenceItem.from_dict(self._data)
            text = f"[{item.index}] {item.citation_text()}"
        except Exception:  # pragma: no cover - 纯防御
            text = str(self._data.get("title") or "")
        QGuiApplication.clipboard().setText(text)
        ToastManager().show("Citation copied to clipboard", "success")

    def _open_source(self):
        data = self._data
        url = str(data.get("url") or "").strip()
        doi = str(data.get("doi") or "").strip()
        path = str(data.get("path") or "").strip()
        if url:
            QDesktopServices.openUrl(QUrl(url))
        elif doi:
            QDesktopServices.openUrl(QUrl(f"https://doi.org/{doi}"))
        elif path:
            import os
            if os.path.exists(path):
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            else:
                ToastManager().show("Local file not found", "error")
        else:
            ToastManager().show("No openable link for this entry", "warning")


class CitationPopupController(QObject):
    """进程级单例：管理引用信息缓存、悬停意图与卡片的显示生命周期。"""

    _instance: Optional["CitationPopupController"] = None

    def __init__(self):
        super().__init__()
        #: 缓存键 = (消息气泡编号, 该消息内的引用编号)。每轮回答的引用编号都从
        #: 1 重新开始，必须带上消息编号，否则历史气泡的 [1] 会被新回答的数据覆盖。
        self._store: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self._popup: Optional[CitationPopup] = None
        self._active_key: Optional[Tuple[int, int]] = None
        self._mode = CitationPopup.MODE_COMPACT
        self._anchor_rect = QRect()
        self._outside_ticks = 0
        #: 浮层宿主窗口（气泡在 hover/expand 时传入，见 _ensure_popup 的兜底逻辑）。
        self._host = None
        #: 展开详情前持有焦点的控件：卡片被点走后归还，用户不必再点一次输入框。
        self._prev_focus = None

        # 悬停意图：鼠标停在锚点上一小段时间才弹出，避免划过高频闪动。
        self._intent_timer = QTimer(self)
        self._intent_timer.setSingleShot(True)
        self._intent_timer.timeout.connect(self._show_compact)
        self._pending_key: Optional[Tuple[int, int]] = None

        # 光标监视：卡片可见时轮询光标位置，判断是否已离开锚点与卡片。
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(WATCH_INTERVAL_MS)
        self._watch_timer.timeout.connect(self._watch_cursor)

        try:
            ThemeManager().theme_changed.connect(self._on_theme_changed)
        except Exception as e:  # pragma: no cover - 主题连接失败不影响功能
            logger.debug("Theme hook for citation popup skipped: %s", e)

    @classmethod
    def instance(cls) -> "CitationPopupController":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------ #
    #  数据同步
    # ------------------------------------------------------------------ #
    def merge_references(self, items, msg_index: int):
        """增量合并结构化引用条目（键为 ``(消息编号, 引用编号)``，新数据为准）。"""
        try:
            mid = int(msg_index)
        except (TypeError, ValueError):
            mid = -1
        count = 0
        for entry in items or []:
            if not isinstance(entry, dict):
                continue
            try:
                idx = int(entry.get("index") or 0)
            except (TypeError, ValueError):
                continue
            if idx <= 0:
                continue
            self._store[(mid, idx)] = entry
            count += 1
        logger.debug("Citation store merged for message #%s: %d item(s), total=%d.",
                     mid, count, len(self._store))
        # 若卡片正展示同一条目，实时刷新其内容。
        if self._popup is not None and self._popup.isVisible() and self._active_key in self._store:
            self._popup.refresh(self._store[self._active_key])

    def lookup(self, msg_index: int, index: int) -> Optional[Dict[str, Any]]:
        try:
            return self._store.get((int(msg_index), int(index)))
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ #
    #  悬停
    # ------------------------------------------------------------------ #
    def hover(self, msg_index: int, index: int, anchor_rect: QRect, host=None):
        """锚点悬停：启动悬停意图计时（详情态下忽略，避免抢走用户操作）。

        :param host: 气泡所在的顶层窗口，作为浮层宿主（浮层是它的子控件）。
        """
        key = self._key(msg_index, index)
        if key is None:
            return
        if host is not None:
            self._host = host
        if self._mode == CitationPopup.MODE_FULL and self._popup is not None \
                and self._popup.isVisible():
            # 详情已是同一条目时只更新锚点；不同条目由点击切换。
            if self._active_key == key:
                self._anchor_rect = QRect(anchor_rect)
            return
        self._anchor_rect = QRect(anchor_rect)
        self._pending_key = key
        self._intent_timer.start(HOVER_INTENT_MS)

    def leave_hover(self):
        """离开锚点：取消尚未触发的悬停意图（已显示的卡片交给光标监视收尾）。"""
        self._intent_timer.stop()
        self._pending_key = None

    def _show_compact(self):
        if self._pending_key is None:
            return
        self._present(self._pending_key, CitationPopup.MODE_COMPACT)

    # ------------------------------------------------------------------ #
    #  点击（展开详情）
    # ------------------------------------------------------------------ #
    def expand(self, msg_index: int, index: int, anchor_rect: QRect, host=None):
        """锚点被点击：展开详情面板；已在概览态时从概览几何平滑过渡。"""
        key = self._key(msg_index, index)
        if key is None:
            return
        if host is not None:
            self._host = host
        self._intent_timer.stop()
        self._pending_key = None
        self._anchor_rect = QRect(anchor_rect)
        from_rect = None
        if self._popup is not None and self._popup.isVisible() and self._active_key == key:
            from_rect = self._popup.geometry()
        self._present(key, CitationPopup.MODE_FULL, from_rect=from_rect)

    @staticmethod
    def _key(msg_index, index) -> Optional[Tuple[int, int]]:
        try:
            mid, idx = int(msg_index), int(index)
        except (TypeError, ValueError):
            return None
        if idx <= 0:
            return None
        return (mid, idx)

    # ------------------------------------------------------------------ #
    #  内部
    # ------------------------------------------------------------------ #
    def _present(self, key: Tuple[int, int], mode: str, from_rect: Optional[QRect] = None):
        msg_index, index = key
        data = self._store.get(key)
        if data is None:
            # 老会话/未同步时给出可用占位，避免点击无反馈。
            data = {"index": index, "title": f"Reference [{index}]",
                    "snippet": "Details are unavailable (this reference may come from "
                               "an earlier session)."}
        popup = self._ensure_popup()
        self._active_key = key
        self._mode = mode
        anchor = self._anchor_rect if self._anchor_rect.isValid() else QRect(QCursor.pos(), QRect().size())
        # 先写入锚点再算内容尺寸：落点选位与高度上限都基于锚点位置。
        popup.set_anchor(anchor)
        # 展示/展开都不动键盘焦点：浮层是主窗口的子控件，本来就与窗口激活状态无关，
        # 不会出现"抢活动窗口""窗口跑到主窗口下面"这类顶层窗口才有的问题。
        if self._prev_focus is None:
            self._prev_focus = QApplication.focusWidget()
        popup.set_reference(data, mode)
        popup.show_with_animation(anchor, mode, from_rect=from_rect)
        self._outside_ticks = 0
        if mode == CitationPopup.MODE_FULL:
            # 详情面板不参与"离开即收起"的轮询：点击程序本体不会关闭它，
            # 只能由 Esc（卡片持有焦点时）或标题栏关闭按钮结束。
            self._watch_timer.stop()
        elif not self._watch_timer.isActive():
            self._watch_timer.start()
        logger.debug("Citation popup shown: message=#%d ref=[%d] mode=%s", msg_index, index, mode)

    def _ensure_popup(self) -> CitationPopup:
        host = self._host or self._overlay_host()
        popup = self._popup
        if popup is not None:
            try:
                popup.isVisible()          # 宿主被关闭时浮层会一并销毁，这里探活
            except RuntimeError:
                self._popup = popup = None
        if popup is None:
            self._popup = popup = CitationPopup(host)
        else:
            popup.rebind_host(host)
        return popup

    @staticmethod
    def _overlay_host():
        """浮层宿主兜底：当前活动窗口，取不到时退回任一可见顶层窗口。

        宿主必须是真正的窗口（顶层 QWidget），浮层才能作为它的子控件绘制在其内容
        之上；这也是 Wayland 下唯一能让位置/层级/拖动都受控的形态。正常路径由气泡
        把 ``self.window()`` 传进来（见 :meth:`hover`），这里只是兜底。
        """
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
        """轮询光标：概览卡在鼠标离开锚点与卡片后自动收起。

        详情面板不参与该轮询（只能由 Esc / 标题栏关闭按钮结束），因此这里直接
        返回，避免"用户去看别的窗口时面板被抢走"。
        """
        popup = self._popup
        if popup is None or not popup.isVisible() or self._mode == CitationPopup.MODE_FULL:
            self._watch_timer.stop()
            return
        pos = QCursor.pos()
        # 锚点区域外扩一点，容忍鼠标在字符边缘抖动。
        on_anchor = self._anchor_rect.adjusted(-6, -6, 6, 6).contains(pos)
        # 浮层不是窗口：frameGeometry 不可用于命中判断，改用全局点反查。
        if on_anchor or popup.contains_global(pos):
            self._outside_ticks = 0
            return
        self._outside_ticks += 1
        if self._outside_ticks >= COMPACT_GRACE_TICKS:
            self.dismiss()

    def dismiss(self):
        self._watch_timer.stop()
        self._intent_timer.stop()
        self._pending_key = None
        self._active_key = None
        self._mode = CitationPopup.MODE_COMPACT
        prev_focus, self._prev_focus = self._prev_focus, None
        if self._popup is not None and self._popup.isVisible():
            # 仅当"卡片自己拿走了键盘焦点"时才归还：否则会把焦点从用户当下
            # 点击的控件上抢走（浮层与窗口激活状态无关，不能用 isActiveWindow）。
            had_focus = self._popup.hasFocus()
            self._popup.hide_popup()
            if had_focus and prev_focus is not None:
                try:
                    prev_focus.setFocus(Qt.OtherFocusReason)
                except RuntimeError:  # 控件可能已随消息重建而销毁
                    pass

    def _on_theme_changed(self, *_args):
        """主题切换（含 auto 跟随系统）：刷新卡片配色并重算内联著录/原文配色。

        QSS 负责整体底色与文字色；但链接行与支撑原文的内联样式在
        :meth:`CitationPopup.set_reference` 时按当时主题固化，必须随主题一起
        重建，否则深/浅模式切换后会出现"底色变了、文字仍是旧主题色"。
        """
        popup = self._popup
        if popup is None:
            return
        popup.apply_theme()
        if popup.isVisible() and self._active_key in self._store:
            popup.refresh(self._store[self._active_key])


__all__ = ["CitationPopup", "CitationPopupController"]
