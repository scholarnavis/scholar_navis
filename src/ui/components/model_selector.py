import os
import json
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel
from PySide6.QtCore import Signal
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

        self.lbl_current_model = QLabel("")
        self.lbl_current_model.setStyleSheet(
            "color: #05B8CC; font-size: 11px; font-weight: bold; font-family: 'Consolas', monospace;")
        layout.addWidget(self.lbl_current_model)
        layout.addStretch()

        self.combo_llm.currentIndexChanged.connect(self._on_llm_changed)

        # 加载本地配置
        self.load_llm_configs()

    def load_llm_configs(self):
        path = os.path.join(os.getcwd(), "config", "llm_config.json")

        self.combo_llm.blockSignals(True)
        self.combo_llm.clear()

        active_id = ConfigManager().user_settings.get("active_llm_id", "openai")
        target_idx = 0

        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    configs = json.load(f)
                    for i, cfg in enumerate(configs):
                        self.combo_llm.addItem(cfg.get('name', 'Unknown'), cfg)
                        if cfg.get("id") == active_id:
                            target_idx = i
            except Exception:
                pass

        if self.combo_llm.count() > 0:
            self.combo_llm.setCurrentIndex(target_idx)

        self.combo_llm.blockSignals(False)
        self._on_llm_changed()

    def _on_llm_changed(self):
        llm_config = self.combo_llm.currentData()
        if not llm_config: return
        actual_model = llm_config.get("model_name", "")
        self.lbl_current_model.setText(f"[{actual_model}]" if actual_model else "[No Model]")
        self.sig_model_changed.emit()

    def get_current_config(self):
        return self.combo_llm.currentData()
