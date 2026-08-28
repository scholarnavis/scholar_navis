"""Plot plan confirmation card.

A self-contained QWidget shown inside an AI chat bubble whenever the agent proposes
a data-visualization plan via the ``propose_plot_plan`` tool (the runtime streams a
``<plot_plan data="...">`` marker which the bubble renders into this widget).

Features:
    * Displays the English plotting proposal as plain text (click anywhere to copy).
    * Lets the user edit / extend the plan in a text box.
    * Detects non-English content in the edited text and translates it back to English
      using the translator model (via a background thread, so the UI never freezes).
    * A "Confirm & Draw" button emits the final, English requirement through
      ``sig_confirm(str)``; the chat tool then re-sends it to the AI to render the chart.
"""

import logging
import re
import threading

from PySide6.QtCore import Qt, QEvent, Signal, Slot
from PySide6.QtGui import QGuiApplication
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


def _contains_non_english(text: str) -> bool:
    """Best-effort heuristic: True when the text contains CJK or other non-Latin script."""
    if not text:
        return False
    return bool(re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u0600-\u06ff\u0400-\u04ff]", text))


class PlotPlanCardWidget(QFrame):
    """Shows the agent's English plotting proposal for review/edit/translate/confirm."""

    # Emitted with the final English requirement the user wants the AI to render.
    sig_confirm = Signal(str)
    # Internal: carries the translated text from the worker thread back to the GUI thread.
    _translation_done = Signal(str)

    def __init__(self, data: dict, translator_config: dict = None, parent=None):
        super().__init__(parent)
        self.data = data or {}
        self.translator_config = translator_config or {}
        self.setObjectName("plotPlanCard")
        self._translation_done.connect(self._on_translation_done)

        self._plan_text = self.data.get("plan_text", "")
        self._request = self.data.get("request", "")
        self._data_context = self.data.get("data_context", "")

        self._translating = False
        self._build_ui()
        self._apply_theme()
        ThemeManager().theme_changed.connect(self._apply_theme)

    # ------------------------------------------------------------------ UI ---
    def _build_ui(self):
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 12, 14, 12)
        self._layout.setSpacing(8)

        # --- header: badge + title ---
        header = QHBoxLayout()
        header.setSpacing(10)

        self._badge = QLabel("PLOT")
        self._badge.setAlignment(Qt.AlignCenter)
        self._badge.setFixedSize(40, 40)
        self._badge.setProperty("cssClass", "plotPlanBadge")

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self._title = QLabel("Proposed Plotting Plan")
        self._title.setProperty("cssClass", "plotPlanTitle")
        title_box.addWidget(self._title)

        self._hint = QLabel(
            "Click the suggestion below to copy. You may edit the plan freely; "
            "if it contains non-English text it will be translated to English automatically."
        )
        self._hint.setProperty("cssClass", "plotPlanHint")
        self._hint.setWordWrap(True)
        title_box.addWidget(self._hint)
        header.addWidget(self._badge)
        header.addLayout(title_box, 1)
        self._layout.addLayout(header)

        # --- read-only suggestion (click to copy) ---
        self._suggestion = QLabel(self._plan_text or "(no suggestion)")
        self._suggestion.setProperty("cssClass", "plotPlanSuggestion")
        self._suggestion.setWordWrap(True)
        self._suggestion.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._suggestion.setCursor(Qt.PointingHandCursor)
        self._suggestion.setToolTip("Click to copy the suggestion")
        self._suggestion.installEventFilter(self)
        self._layout.addWidget(self._suggestion)

        # --- editable plan ---
        self._edit = QPlainTextEdit(self._plan_text or "")
        self._edit.setProperty("cssClass", "plotPlanEdit")
        self._edit.setPlaceholderText("Edit the plotting requirements here...")
        self._edit.setMinimumHeight(90)
        self._edit.setMaximumHeight(200)
        self._layout.addWidget(self._edit)

        # --- status line for translation feedback ---
        self._status = QLabel("")
        self._status.setProperty("cssClass", "plotPlanStatus")
        self._status.setWordWrap(True)
        self._status.setVisible(False)
        self._layout.addWidget(self._status)

        # --- action buttons ---
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch(1)

        self._btn_copy = QPushButton(" Copy Suggestion")
        self._btn_copy.setProperty("cssClass", "plotPlanBtn")
        self._btn_copy.setCursor(Qt.PointingHandCursor)
        self._btn_copy.clicked.connect(self._copy_suggestion)
        actions.addWidget(self._btn_copy)

        self._btn_translate = QPushButton(" Translate to English")
        self._btn_translate.setProperty("cssClass", "plotPlanBtn")
        self._btn_translate.setCursor(Qt.PointingHandCursor)
        self._btn_translate.clicked.connect(self._translate_to_english)
        actions.addWidget(self._btn_translate)

        self._btn_confirm = QPushButton(" Confirm & Draw")
        self._btn_confirm.setProperty("cssClass", "plotPlanConfirm")
        self._btn_confirm.setCursor(Qt.PointingHandCursor)
        self._btn_confirm.clicked.connect(self._on_confirm)
        actions.addWidget(self._btn_confirm)

        self._layout.addLayout(actions)

    # --------------------------------------------------------------- theme ---
    def _apply_theme(self):
        tm = ThemeManager()
        card_bg = tm.color("bg_card")
        border = tm.color("border")
        text_main = tm.color("text_main")
        text_muted = tm.color("text_muted")
        accent = tm.color("accent")
        academic_blue = tm.color("academic_blue")
        btn_bg = tm.color("btn_bg")
        btn_hover = tm.color("btn_hover")
        bg_input = tm.color("bg_input")
        font_family = tm.font_family()

        self.setStyleSheet(f"""
            QFrame#plotPlanCard {{
                background-color: {card_bg};
                border: 1px solid {border};
                border-left: 4px solid {academic_blue};
                border-radius: 8px;
            }}
            QLabel[cssClass="plotPlanBadge"] {{
                background-color: {academic_blue};
                color: #ffffff;
                border-radius: 10px;
                font-size: 12px;
                font-weight: bold;
                font-family: {font_family};
            }}
            QLabel[cssClass="plotPlanTitle"] {{
                color: {text_main};
                font-size: 14px;
                font-weight: bold;
                font-family: {font_family};
            }}
            QLabel[cssClass="plotPlanHint"] {{
                color: {text_muted};
                font-size: 11px;
                font-family: {font_family};
            }}
            QLabel[cssClass="plotPlanSuggestion"] {{
                color: {text_main};
                font-size: 12px;
                font-family: {font_family};
                background-color: {bg_input};
                border: 1px dashed {border};
                border-radius: 6px;
                padding: 8px 10px;
            }}
            QPlainTextEdit[cssClass="plotPlanEdit"] {{
                background-color: {bg_input};
                color: {text_main};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 6px;
                font-size: 12px;
                font-family: {font_family};
            }}
            QPlainTextEdit[cssClass="plotPlanEdit"]:focus {{
                border-color: {accent};
            }}
            QLabel[cssClass="plotPlanStatus"] {{
                color: {accent};
                font-size: 11px;
                font-family: {font_family};
            }}
            QPushButton[cssClass="plotPlanBtn"] {{
                background-color: {btn_bg};
                color: {text_main};
                border: 1px solid {border};
                border-radius: 5px;
                padding: 5px 12px;
                font-size: 12px;
                font-family: {font_family};
            }}
            QPushButton[cssClass="plotPlanBtn"]:hover {{
                background-color: {btn_hover};
                border-color: {accent};
                color: {accent};
            }}
            QPushButton[cssClass="plotPlanBtn"]:disabled {{
                background-color: {btn_bg};
                color: {text_muted};
                border-color: {border};
            }}
            QPushButton[cssClass="plotPlanConfirm"] {{
                background-color: {academic_blue};
                color: #ffffff;
                border: 1px solid {academic_blue};
                border-radius: 5px;
                padding: 5px 14px;
                font-size: 12px;
                font-weight: bold;
                font-family: {font_family};
            }}
            QPushButton[cssClass="plotPlanConfirm"]:hover {{
                background-color: {accent};
                border-color: {accent};
            }}
            QPushButton[cssClass="plotPlanConfirm"]:disabled {{
                background-color: {btn_bg};
                color: {text_muted};
                border-color: {border};
            }}
        """)

    # ------------------------------------------------------------ handlers ---
    def eventFilter(self, obj, event):
        # Click anywhere on the suggestion box copies it to the clipboard.
        if obj is self._suggestion and event.type() == QEvent.MouseButtonRelease:
            self._copy_suggestion()
            return True
        return super().eventFilter(obj, event)

    def _copy_suggestion(self):
        if self._plan_text:
            QGuiApplication.clipboard().setText(self._plan_text)
            ToastManager().show("Plotting plan copied to clipboard.", "success")
        else:
            ToastManager().show("Nothing to copy.", "warning")

    def _set_translating(self, busy: bool):
        self._translating = busy
        self._btn_confirm.setEnabled(not busy)
        self._btn_translate.setEnabled(not busy)
        self._btn_copy.setEnabled(not busy)
        self._status.setVisible(busy)
        self._status.setText("Translating to English...")

    def _translate_to_english(self):
        text = self._edit.toPlainText().strip()
        if not text:
            ToastManager().show("Nothing to translate.", "warning")
            return
        if not self.translator_config:
            ToastManager().show("No translator model selected. Enable one in the ribbon first.", "error")
            return

        self._set_translating(True)
        cfg = self.translator_config.copy()

        def _worker():
            result = ""
            try:
                from src.core.network_worker import setup_global_network_env
                setup_global_network_env()
                from src.core.llm_impl import OpenAICompatibleLLM, get_cached_translation

                cfg["timeout"] = 15.0
                llm = OpenAICompatibleLLM(cfg)
                result = get_cached_translation(
                    text, "to_en", llm, is_translation=True, stream=False
                )
            except Exception as e:  # noqa: BLE001
                logger.error(f"Plot plan translation failed: {e}")
                result = ""

            self._translation_done.emit(result)

        threading.Thread(target=_worker, daemon=True).start()

    @Slot(str)
    def _on_translation_done(self, translated: str):
        # This slot is emitted from a worker thread; queued connection marshals it
        # onto the GUI thread automatically.
        self._set_translating(False)
        if translated:
            self._edit.setPlainText(translated)
            self._status.setText("Translated to English. Review and confirm to draw.")
            self._status.setVisible(True)
            ToastManager().show("Plan translated to English.", "success")
        else:
            self._status.setText("Translation failed. Check the translator model and try again.")
            self._status.setVisible(True)
            ToastManager().show("Translation failed. Check the translator model.", "error")

    def _on_confirm(self):
        text = self._edit.toPlainText().strip()
        if not text:
            ToastManager().show("The plan is empty. Please enter or edit your requirement.", "warning")
            return

        if self._translating:
            ToastManager().show("Translation in progress. Please wait.", "warning")
            return

        if _contains_non_english(text):
            if not self.translator_config:
                ToastManager().show(
                    "The plan contains non-English text but no translator is selected. "
                    "Select a translator model and click 'Translate to English' first.",
                    "error",
                )
                return
            ToastManager().show("Plan contains non-English text. Translating before sending...", "info")
            self._translate_to_english()
            return

        self.sig_confirm.emit(text)

    def closeEvent(self, event):
        try:
            ThemeManager().theme_changed.disconnect(self._apply_theme)
        except (TypeError, RuntimeError):
            pass
        super().closeEvent(event)
