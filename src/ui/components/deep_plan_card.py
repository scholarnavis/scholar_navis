"""Deep-research plan confirmation card.

深度研究计划确认卡：Agent 分解出并行子任务后不再立即执行，而是流出
``<deep_plan data="...">`` 标记渲染本卡片并结束本轮。用户可查看各子调查及
其理由、编辑子问题清单（编号行文本），随后：
- 点击 "Confirm & Execute" → ``sig_confirm`` 交回编辑后的计划文本，
  以 ``[DEEP_PLAN_CONFIRMED]`` 哨兵重进发送管线，任务端跳过分解直接并行执行；
- 点击 "Answer Directly" → ``sig_skip`` 交回原始问题，以
  ``[DEEP_PLAN_SKIPPED]`` 哨兵跳过拆解、本轮直接单 Agent 作答。
"""
import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from src.core.theme_manager import ThemeManager
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)


def _numbered_lines(sub_tasks) -> str:
    """将子任务列表渲染为可编辑的编号行文本（一行一个子问题）。"""
    lines = []
    idx = 0
    for st in (sub_tasks or []):
        q = str((st or {}).get("query", "") or "").strip()
        if not q:
            continue
        idx += 1
        lines.append(f"{idx}. {q}")
    return "\n".join(lines)


class DeepPlanCardWidget(QFrame):
    """深度研究计划卡：查看/编辑子问题 → 确认并行执行，或跳过直接作答。"""

    #: 用户编辑后的编号计划文本（一行一个子问题，去空行）。
    sig_confirm = Signal(str)
    #: 跳过拆解：交回原始问题文本。
    sig_skip = Signal(str)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self.data = data or {}
        self.setObjectName("deepPlanCard")

        self._query = str(self.data.get("query", "") or "").strip()
        self._sub_tasks = list(self.data.get("sub_tasks") or [])
        self._resolved = False  # 确认/跳过均只允许一次，之后整卡锁定

        self._build_ui()
        self._apply_theme()
        ThemeManager().theme_changed.connect(self._apply_theme)
        logger.debug(f"DeepPlanCard created: {len(self._sub_tasks)} sub-tasks")

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        # 头部：徽标 + 标题 + 提示
        header = QHBoxLayout()
        header.setSpacing(10)
        self._badge = QLabel("DEEP")
        self._badge.setObjectName("deepPlanBadge")
        self._badge.setAlignment(Qt.AlignCenter)
        self._badge.setFixedSize(40, 40)
        header.addWidget(self._badge)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("Proposed Deep-Research Plan")
        title.setObjectName("deepPlanTitle")
        title_box.addWidget(title)
        # 预估执行成本：确认后还需 N 路并行子 Agent + 1 次综合（分解已在
        # 出卡前完成）。让用户在确认闸门上有明确的 token/时间预期。
        n_subs = len([st for st in self._sub_tasks
                      if str((st or {}).get("query", "") or "").strip()])
        cost_hint = (f" Estimated on confirm: {n_subs} parallel agents + 1 synthesis "
                     f"({n_subs + 1} model runs)." if n_subs else "")
        hint = QLabel(
            "Review or edit the sub-investigations below, then confirm to run them "
            "in parallel; or answer directly without decomposition." + cost_hint
        )
        hint.setObjectName("deepPlanHint")
        hint.setWordWrap(True)
        title_box.addWidget(hint)
        header.addLayout(title_box, 1)
        layout.addLayout(header)

        # 原始问题（muted）
        if self._query:
            src = QLabel(f"Query: {self._query}")
            src.setObjectName("deepPlanQuery")
            src.setWordWrap(True)
            src.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(src)

        # 只读概览：子问题 + 分解理由
        overview = self._build_overview_text()
        if overview:
            ov = QLabel(overview)
            ov.setObjectName("deepPlanOverview")
            ov.setWordWrap(True)
            ov.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(ov)

        # 可编辑的编号子问题清单（Confirm 提交后以此为准）
        self._edit = QPlainTextEdit()
        self._edit.setObjectName("deepPlanEdit")
        self._edit.setPlainText(_numbered_lines(self._sub_tasks))
        self._edit.setMinimumHeight(84)
        self._edit.setMaximumHeight(200)
        self._edit.setPlaceholderText("1. sub-investigation A\n2. sub-investigation B")
        layout.addWidget(self._edit)

        # 操作区：恢复 | 跳过 | 确认执行
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._btn_restore = QPushButton("Restore")
        self._btn_restore.setObjectName("deepPlanRestore")
        self._btn_restore.setCursor(Qt.PointingHandCursor)
        self._btn_restore.clicked.connect(self._on_restore)
        actions.addWidget(self._btn_restore)
        actions.addStretch(1)
        self._btn_skip = QPushButton("Answer Directly")
        self._btn_skip.setObjectName("deepPlanSkip")
        self._btn_skip.setCursor(Qt.PointingHandCursor)
        self._btn_skip.clicked.connect(self._on_skip)
        actions.addWidget(self._btn_skip)
        self._btn_confirm = QPushButton("Confirm & Execute")
        self._btn_confirm.setObjectName("deepPlanConfirm")
        self._btn_confirm.setCursor(Qt.PointingHandCursor)
        self._btn_confirm.clicked.connect(self._on_confirm)
        actions.addWidget(self._btn_confirm)
        layout.addLayout(actions)

    def _build_overview_text(self) -> str:
        parts = []
        idx = 0
        for st in self._sub_tasks:
            q = str((st or {}).get("query", "") or "").strip()
            if not q:
                continue
            idx += 1
            line = f"{idx}. {q}"
            rationale = str((st or {}).get("rationale", "") or "").strip()
            if rationale:
                line += f"\n    {rationale}"
            parts.append(line)
        return "\n\n".join(parts)

    # ------------------------------------------------------------- Actions

    def _on_restore(self):
        """把可编辑清单还原为 Agent 最初提出的计划。"""
        self._edit.setPlainText(_numbered_lines(self._sub_tasks))
        logger.debug("DeepPlanCard plan text restored")

    def _on_confirm(self):
        if self._resolved:
            logger.debug("DeepPlanCard confirm ignored: card already resolved.")
            return
        text = "\n".join(
            ln.strip() for ln in self._edit.toPlainText().splitlines() if ln.strip()
        )
        if not text:
            ToastManager().show("The plan is empty; restore or edit it first.", "warning")
            logger.debug("DeepPlanCard confirm rejected: empty plan")
            return
        self._resolved = True
        self._lock_after_decision("Executing...")
        logger.info(f"DeepPlanCard confirmed: {len(text.splitlines())} sub-task line(s); card locked.")
        self.sig_confirm.emit(text)

    def _on_skip(self):
        if self._resolved:
            logger.debug("DeepPlanCard skip ignored: card already resolved.")
            return
        if not self._query:
            ToastManager().show("Original query missing; please restate your question.", "warning")
            logger.debug("DeepPlanCard skip rejected: missing original query")
            return
        self._resolved = True
        self._lock_after_decision("Answering directly...")
        logger.info("DeepPlanCard skipped by user; answering directly; card locked.")
        self.sig_skip.emit(self._query)

    def _lock_after_decision(self, action_text: str):
        """决策后锁定整卡：禁用按钮与编辑框，防止重复触发重复发送。"""
        self._btn_confirm.setText(action_text)
        for btn in (self._btn_confirm, self._btn_skip, self._btn_restore):
            btn.setEnabled(False)
        self._edit.setEnabled(False)

    # -------------------------------------------------------------- Theme

    def _apply_theme(self):
        tm = ThemeManager()
        bg = tm.color("bg_input")
        border = tm.color("border")
        text_main = tm.color("text_main")
        text_sub = tm.color("text_muted")
        accent = tm.color("accent")
        academic_blue = tm.color("academic_blue")
        card_bg = tm.color("bg_card")
        btn_hover = tm.color("btn_hover")
        font_family = tm.font_family()
        self.setStyleSheet(f"""
            QFrame#deepPlanCard {{
                background: {card_bg};
                border: 1px solid {border};
                border-left: 3px solid {academic_blue};
                border-radius: 8px;
                font-family: '{font_family}';
            }}
            QLabel#deepPlanBadge {{
                background: {academic_blue};
                color: #FFFFFF;
                font-size: 10px;
                font-weight: bold;
                border-radius: 4px;
                letter-spacing: 0.5px;
            }}
            QLabel#deepPlanTitle {{
                color: {text_main};
                font-size: 13px;
                font-weight: bold;
            }}
            QLabel#deepPlanHint {{
                color: {text_sub};
                font-size: 11px;
            }}
            QLabel#deepPlanQuery {{
                color: {text_sub};
                font-size: 11px;
                font-style: italic;
            }}
            QLabel#deepPlanOverview {{
                color: {text_main};
                font-size: 12px;
                background: {bg};
                border: 1px dashed {border};
                border-radius: 6px;
                padding: 8px 10px;
            }}
            QPlainTextEdit#deepPlanEdit {{
                background: {bg};
                color: {text_main};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 6px 8px;
                font-size: 12px;
            }}
            QPushButton#deepPlanRestore {{
                background: transparent;
                color: {text_sub};
                font-size: 12px;
                border: 1px solid {border};
                border-radius: 6px;
                padding: 7px 14px;
            }}
            QPushButton#deepPlanRestore:hover {{
                color: {text_main};
                border-color: {text_sub};
            }}
            QPushButton#deepPlanSkip {{
                background: transparent;
                color: {academic_blue};
                font-size: 12px;
                font-weight: bold;
                border: 1px solid {academic_blue};
                border-radius: 6px;
                padding: 7px 14px;
            }}
            QPushButton#deepPlanSkip:hover {{
                background: {bg};
            }}
            QPushButton#deepPlanConfirm {{
                background: {academic_blue};
                color: #FFFFFF;
                font-size: 12px;
                font-weight: bold;
                border: none;
                border-radius: 6px;
                padding: 7px 16px;
            }}
            QPushButton#deepPlanConfirm:hover {{
                background: {btn_hover};
            }}
        """)

    def closeEvent(self, event):
        try:
            ThemeManager().theme_changed.disconnect(self._apply_theme)
        except (TypeError, RuntimeError):
            pass
        super().closeEvent(event)
