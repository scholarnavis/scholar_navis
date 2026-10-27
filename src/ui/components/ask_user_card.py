"""Ask-user clarification card for the human-in-the-loop chat flow.

Rendering-only widget: it validates and locks itself, and emits exactly one
``sig_submit`` in its lifetime. ChatTool owns the send pipeline.

Interaction contract (prevents duplicate sends):
- "My own answer" is mutually exclusive with the preset options;
- the free-text edit is enabled only while "My own answer" is selected;
- the submit button stays disabled until either a preset option is chosen,
  or the own answer is selected with non-empty text;
- the whole card locks right after the first successful submit.
"""
import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.core.i18n import tr
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)

_MAX_OPTIONS = 8
_OWN_ANSWER_LABEL = "My own answer (type below)"
_HINT_IDLE = "Select an option, or choose \u201cMy own answer\u201d to type a reply, then submit."
_HINT_DONE = "Answer submitted; waiting for the agent to continue."


class AskUserCardWidget(QFrame):
    """澄清卡：自行完成答案校验与提交后锁定，仅发出一次 sig_submit。

    组件约定（冻结 / 快照 / 恢复）见 :mod:`src.ui.components.card_registry`：提交后
    经 :meth:`freeze` 变为只读组件（文字仍可框选），交互结果由 :meth:`card_state`
    导出，历史重放时经 :meth:`restore_state` 回灌并冻结。
    """

    CARD_KIND = "ask_user"

    sig_submit = Signal(str)
    #: 交互落定（提交）后发出，供上层把 ``ui_components`` 回写历史条目并落盘
    sig_state_changed = Signal()

    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.data = data or {}
        self.setObjectName("askUserCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

        self._question = str(self.data.get("question") or "").strip()
        self._context = str(self.data.get("context") or "").strip()
        self._multi = bool(self.data.get("multi_select"))
        raw_options = self.data.get("options") or []
        if not isinstance(raw_options, list):
            raw_options = []
        self._options = [str(o).strip() for o in raw_options if str(o).strip()][:_MAX_OPTIONS]

        self._option_widgets = []
        self._own_widget = None
        self._submitted = False
        #: 冻结后展示"你的回答"的只读标签（延迟创建，避免空闲态多一行空白）
        self._answer_label = None

        self._build_ui()
        self._update_submit_state()
        logger.debug(f"AskUserCardWidget created: options={len(self._options)}, multi={self._multi}")

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)
        self._root_layout = root

        q_label = QLabel(tr("Question: {question}").format(question=self._question))
        q_label.setObjectName("askUserQuestion")
        q_label.setWordWrap(True)
        # 冻结后整卡只读，文本一律允许框选复制
        q_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(q_label)

        if self._context:
            c_label = QLabel(self._context)
            c_label.setObjectName("askUserContext")
            c_label.setWordWrap(True)
            c_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            root.addWidget(c_label)

        # 预设选项与 "My own answer" 放入同一容器：
        # 单选时 QRadioButton 同父自动互斥，多选时由信号槽手动互斥。
        opts_widget = QWidget(self)
        opts_layout = QVBoxLayout(opts_widget)
        opts_layout.setContentsMargins(0, 0, 0, 0)
        opts_layout.setSpacing(4)

        for opt in self._options:
            w = QCheckBox(opt) if self._multi else QRadioButton(opt)
            w.setObjectName("askUserOption")
            w.toggled.connect(self._on_option_toggled)
            opts_layout.addWidget(w)
            self._option_widgets.append(w)

        own_text = tr(_OWN_ANSWER_LABEL)
        self._own_widget = (QCheckBox(own_text) if self._multi
                            else QRadioButton(own_text))
        self._own_widget.setObjectName("askUserOption")
        self._own_widget.toggled.connect(self._on_own_toggled)
        opts_layout.addWidget(self._own_widget)

        root.addWidget(opts_widget)

        self._edit = QPlainTextEdit()
        self._edit.setObjectName("askUserEdit")
        self._edit.setPlaceholderText(tr("Type your own answer..."))
        self._edit.setFixedHeight(54)
        self._edit.setEnabled(False)  # 仅选中 "My own answer" 后允许输入
        self._edit.textChanged.connect(self._update_submit_state)
        root.addWidget(self._edit)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)

        self._hint_label = QLabel(tr(_HINT_IDLE))
        self._hint_label.setObjectName("askUserHint")
        self._hint_label.setWordWrap(True)
        self._hint_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bottom.addWidget(self._hint_label, 1)

        self._btn_submit = QPushButton(" " + tr("Submit Answer"))
        self._btn_submit.setObjectName("askUserSubmit")
        self._btn_submit.setCursor(Qt.PointingHandCursor)
        self._btn_submit.setEnabled(False)  # 无有效作答前禁止提交
        self._btn_submit.clicked.connect(self._on_submit)
        bottom.addWidget(self._btn_submit)

        root.addLayout(bottom)

    # ------------------------------------------------------------------
    # 互斥与提交校验
    # ------------------------------------------------------------------
    def _on_option_toggled(self, checked: bool):
        """预设选项切换；多选模式下与 "My own answer" 互斥。"""
        if checked and self._multi and self._own_widget is not None and self._own_widget.isChecked():
            self._own_widget.setChecked(False)
        self._update_submit_state()

    def _on_own_toggled(self, checked: bool):
        """"My own answer" 切换；多选模式下清空预设选项，输入框随之启停。"""
        if checked and self._multi:
            for w in self._option_widgets:
                if w.isChecked():
                    w.setChecked(False)
        self._edit.setEnabled(checked)
        if not checked:
            self._edit.clear()
        self._update_submit_state()

    def _update_submit_state(self):
        """提交按钮仅在有有效作答时可用：选中预设项，或选中自答且文本非空。"""
        if self._submitted:
            self._btn_submit.setEnabled(False)
            return
        if self._own_widget.isChecked():
            ok = bool(self._edit.toPlainText().strip())
        else:
            ok = any(w.isChecked() for w in self._option_widgets)
        self._btn_submit.setEnabled(ok)

    # ------------------------------------------------------------------
    # 提交
    # ------------------------------------------------------------------
    def _on_submit(self):
        if self._submitted:
            logger.debug("AskUserCard submit ignored: card already submitted.")
            return
        own_checked = self._own_widget.isChecked()
        chosen = [w.text().strip() for w in self._option_widgets if w.isChecked()]
        extra = self._edit.toPlainText().strip() if own_checked else ""
        if not chosen and not extra:
            ToastManager().show(
                tr("Select an option or type your own answer first."), "warning")
            logger.debug("AskUserCard submit rejected: empty answer.")
            return

        answer = "\n".join([("; ".join(chosen)) if chosen else "", extra]).strip()
        self._submitted = True
        self._lock_after_submit()
        logger.info(f"AskUserCard answer submitted ({len(answer)} chars); card locked against resubmit.")
        # 先冻结再发出信号：上层收到信号时读到的一定是"已落定"的状态
        self.sig_state_changed.emit()
        self.sig_submit.emit(answer)

    def _lock_after_submit(self):
        """提交后锁定整卡（等价于 :meth:`freeze`，保留旧名以免上游调用点失效）。"""
        self.freeze()

    # ------------------------------------------------------------------
    # 组件约定：冻结 / 快照 / 恢复（见 card_registry）
    # ------------------------------------------------------------------
    def card_key(self) -> str:
        """去重键与快照匹配键：问题文本。"""
        return self._question or "question"

    def card_state(self) -> dict:
        """导出用户交互结果（选项 + 自答文本），供历史快照与归档保存。"""
        return {
            "selected": [w.text().strip() for w in self._option_widgets if w.isChecked()],
            "custom": self._edit.toPlainText().strip(),
        }

    def restore_state(self, state: dict):
        """回灌交互结果并冻结（历史重放路径）。"""
        state = state or {}
        selected = {str(s) for s in (state.get("selected") or [])}
        for w in self._option_widgets:
            w.setChecked(w.text().strip() in selected)
        custom = str(state.get("custom") or "")
        if custom:
            self._own_widget.setChecked(True)
            self._edit.setPlainText(custom)
        self._submitted = True
        self.freeze()

    def freeze(self):
        """冻结为只读组件：禁用输入与按钮，文字保留可框选复制。

        只读文本框必须用 ``setReadOnly(True)`` 而不是 ``setEnabled(False)`` —— 后者
        会让文字连框选、复制都做不到，与"不可编辑但可框选"的要求相反。选项行是
        QRadioButton / QCheckBox，文字本身无法框选，故额外用一条可选中的
        "Your answer" 摘要把作答内容显式呈现出来。
        """
        self._submitted = True
        self._btn_submit.setText(" Submitted")
        self._btn_submit.setEnabled(False)
        for w in self._option_widgets:
            w.setEnabled(False)
        self._own_widget.setEnabled(False)
        self._edit.setEnabled(True)
        self._edit.setReadOnly(True)
        self._hint_label.setText(tr(_HINT_DONE))
        self._show_answer_summary()

    def _answer_text(self) -> str:
        chosen = [w.text().strip() for w in self._option_widgets if w.isChecked()]
        parts = []
        if chosen:
            parts.append("; ".join(chosen))
        if self._own_widget is not None and self._own_widget.isChecked():
            text = self._edit.toPlainText().strip()
            if text:
                parts.append(text)
        return "\n".join(parts)

    def _show_answer_summary(self):
        """把作答内容落成一条可框选的只读标签（选项行的文字无法框选）。"""
        answer = self._answer_text()
        if not answer:
            return
        if self._answer_label is None:
            self._answer_label = QLabel(self)
            self._answer_label.setObjectName("askUserAnswer")
            self._answer_label.setWordWrap(True)
            self._answer_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._root_layout.addWidget(self._answer_label)
        self._answer_label.setText(f"Your answer: {answer}")
