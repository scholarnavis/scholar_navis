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

from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)

_MAX_OPTIONS = 8
_OWN_ANSWER_LABEL = "My own answer (type below)"
_HINT_IDLE = "Select an option, or choose \u201cMy own answer\u201d to type a reply, then submit."
_HINT_DONE = "Answer submitted; waiting for the agent to continue."


class AskUserCardWidget(QFrame):
    """澄清卡：自行完成答案校验与提交后锁定，仅发出一次 sig_submit。"""

    sig_submit = Signal(str)

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

        q_label = QLabel(f"Question: {self._question}")
        q_label.setObjectName("askUserQuestion")
        q_label.setWordWrap(True)
        root.addWidget(q_label)

        if self._context:
            c_label = QLabel(self._context)
            c_label.setObjectName("askUserContext")
            c_label.setWordWrap(True)
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

        self._own_widget = QCheckBox(_OWN_ANSWER_LABEL) if self._multi else QRadioButton(_OWN_ANSWER_LABEL)
        self._own_widget.setObjectName("askUserOption")
        self._own_widget.toggled.connect(self._on_own_toggled)
        opts_layout.addWidget(self._own_widget)

        root.addWidget(opts_widget)

        self._edit = QPlainTextEdit()
        self._edit.setObjectName("askUserEdit")
        self._edit.setPlaceholderText("Type your own answer...")
        self._edit.setFixedHeight(54)
        self._edit.setEnabled(False)  # 仅选中 "My own answer" 后允许输入
        self._edit.textChanged.connect(self._update_submit_state)
        root.addWidget(self._edit)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)

        self._hint_label = QLabel(_HINT_IDLE)
        self._hint_label.setObjectName("askUserHint")
        self._hint_label.setWordWrap(True)
        bottom.addWidget(self._hint_label, 1)

        self._btn_submit = QPushButton(" Submit Answer")
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
            ToastManager().show("Select an option or type your own answer first.", "warning")
            logger.debug("AskUserCard submit rejected: empty answer.")
            return

        answer = "\n".join([("; ".join(chosen)) if chosen else "", extra]).strip()
        self._submitted = True
        self._lock_after_submit()
        logger.info(f"AskUserCard answer submitted ({len(answer)} chars); card locked against resubmit.")
        self.sig_submit.emit(answer)

    def _lock_after_submit(self):
        """提交后锁定整卡：禁用全部控件并更新提示，防止重复提交重复发送。"""
        self._btn_submit.setText(" Submitted")
        self._btn_submit.setEnabled(False)
        for w in self._option_widgets:
            w.setEnabled(False)
        self._own_widget.setEnabled(False)
        self._edit.setEnabled(False)
        self._hint_label.setText(_HINT_DONE)
