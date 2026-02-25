# In model_selector.py
import os
import json
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel
from PySide6.QtCore import Signal
from src.core.config_manager import ConfigManager
from src.ui.components.combo import BaseComboBox


class ModelSelectorWidget(QWidget):
    sig_model_changed = Signal()

    def __init__(self, parent=None, label_text="🧠 Select:", config_key="active_llm_id", model_key="model_name"):
        super().__init__(parent)
        self.config_key = config_key
        self.model_key = model_key

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(label_text))

        self.combo_provider = BaseComboBox(min_width=120)
        self.combo_model = BaseComboBox(min_width=150)

        layout.addWidget(self.combo_provider)
        layout.addWidget(self.combo_model)
        layout.addStretch()

        self.combo_provider.currentIndexChanged.connect(self._on_provider_changed)
        self.combo_model.currentIndexChanged.connect(self._on_model_changed)

        self.configs = []
        self.load_llm_configs()

    def load_llm_configs(self):
        path = os.path.join(os.getcwd(), "config", "llm_config.json")
        self.combo_provider.blockSignals(True)
        self.combo_provider.clear()

        if self.config_key == "trans_llm_id":
            self.combo_provider.addItem("❌ None (Disable)", None)

        active_id = ConfigManager().user_settings.get(self.config_key, "openai")
        target_idx = 0

        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    self.configs = json.load(f)
                    for i, cfg in enumerate(self.configs):
                        self.combo_provider.addItem(cfg.get('name', 'Unknown'), cfg)
                        if cfg.get("id") == active_id:
                            target_idx = self.combo_provider.count() - 1
            except Exception:
                pass

        if self.combo_provider.count() > 0:
            self.combo_provider.setCurrentIndex(target_idx)

        self.combo_provider.blockSignals(False)
        self._on_provider_changed()

    def _on_provider_changed(self):
        cfg = self.combo_provider.currentData()
        self.combo_model.blockSignals(True)
        self.combo_model.clear()

        if cfg:
            ConfigManager().user_settings[self.config_key] = cfg.get("id")
            ConfigManager().save_settings()

            # Populate Models
            models = cfg.get("fetched_models", [])
            current_model = cfg.get(self.model_key, cfg.get("model_name", "No Model"))

            items = list(models)
            if current_model and current_model not in items:
                items.insert(0, current_model)

            self.combo_model.addItems(items)
            self.combo_model.setCurrentText(current_model)
        else:
            ConfigManager().user_settings[self.config_key] = None
            ConfigManager().save_settings()

        self.combo_model.blockSignals(False)
        self.sig_model_changed.emit()

    def _on_model_changed(self):
        cfg = self.combo_provider.currentData()
        model_name = self.combo_model.currentText()
        if cfg and model_name:
            # Update the JSON locally
            cfg[self.model_key] = model_name
            path = os.path.join(os.getcwd(), "config", "llm_config.json")
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    file_configs = json.load(f)
                for c in file_configs:
                    if c["id"] == cfg["id"]:
                        c[self.model_key] = model_name
                        break
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(file_configs, f, indent=4)
            except Exception:
                pass

        self.sig_model_changed.emit()

    def get_current_config(self):
        return self.combo_provider.currentData()