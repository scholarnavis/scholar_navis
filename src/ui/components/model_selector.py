import os
import json
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QCheckBox
from PySide6.QtCore import Qt, Signal
from src.core.config_manager import ConfigManager
from src.ui.components.combo import BaseComboBox

class ModelSelectorWidget(QWidget):
    sig_model_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(QLabel("🧠 Model:"))
        self.combo_llm = BaseComboBox(min_width=150)
        layout.addWidget(self.combo_llm)

        self.checkbox_think = QCheckBox("Think Mode")
        self.checkbox_think.setCursor(Qt.PointingHandCursor)
        self.checkbox_think.setStyleSheet("""
            QCheckBox { color: #aaaaaa; font-weight: bold; font-family: 'Segoe UI'; font-size: 13px; }
            QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px; border: 1px solid #555; background: #333; }
            QCheckBox::indicator:checked { background: #007acc; border: 1px solid #007acc; }
            QCheckBox:disabled { color: #555555; }
        """)
        layout.addWidget(self.checkbox_think)

        self.lbl_current_model = QLabel("")
        self.lbl_current_model.setStyleSheet(
            "color: #05B8CC; font-size: 11px; font-weight: bold; font-family: 'Consolas', monospace;")
        layout.addWidget(self.lbl_current_model)
        layout.addStretch()

        self.combo_llm.currentIndexChanged.connect(self._on_llm_changed)
        self.checkbox_think.stateChanged.connect(self._update_model_display)

        self.load_llm_configs()

    def load_llm_configs(self):
        path = os.path.join(os.getcwd(), "config", "llm_config.json")
        self.combo_llm.clear()
        active_id = ConfigManager().user_settings.get("active_llm_id", "openai")
        target_idx = 0

        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    configs = json.load(f)
                    for i, cfg in enumerate(configs):
                        self.combo_llm.addItem(cfg['name'], cfg)
                        if cfg.get("id") == active_id: target_idx = i
            except: pass

        if self.combo_llm.count() > 0:
            self.combo_llm.setCurrentIndex(target_idx)
        self._on_llm_changed()

    def _on_llm_changed(self):
        llm_config = self.combo_llm.currentData()
        if not llm_config: return
        has_think = bool(llm_config.get("thinking_model_name", "").strip())
        self.checkbox_think.blockSignals(True)
        if not has_think:
            self.checkbox_think.setChecked(False)
            self.checkbox_think.setEnabled(False)
        else:
            self.checkbox_think.setEnabled(True)
        self.checkbox_think.blockSignals(False)
        self._update_model_display()
        self.sig_model_changed.emit()

    def _update_model_display(self):
        llm_config = self.combo_llm.currentData()
        if not llm_config: return
        use_think = self.checkbox_think.isChecked()
        actual_model = llm_config.get("thinking_model_name", "") if use_think else llm_config.get("model_name", "")
        self.lbl_current_model.setText(f"[{actual_model}]" if actual_model else "[No Model]")

    def get_current_config(self):
        return self.combo_llm.currentData()

    def is_think_mode(self):
        return self.checkbox_think.isChecked()