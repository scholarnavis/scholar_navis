import os
import json
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel
from PySide6.QtCore import Signal
from src.core.config_manager import ConfigManager

from src.ui.components.combo import BaseComboBox


class ModelSelectorWidget(QWidget):
    sig_model_changed = Signal()

    def __init__(self, parent=None, label_text="🧠 Model:", config_key="active_llm_id", model_key="model_name"):
        super().__init__(parent)
        self.config_key = config_key
        self.model_key = model_key
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(QLabel(label_text))
        self.combo_llm = BaseComboBox(min_width=200)
        layout.addWidget(self.combo_llm)
        layout.addStretch()

        self.combo_llm.currentIndexChanged.connect(self._on_llm_changed)
        self.load_llm_configs()

    def load_llm_configs(self):
        path = os.path.join(os.getcwd(), "config", "llm_config.json")

        self.combo_llm.blockSignals(True)
        self.combo_llm.clear()

        # 如果是翻译器，允许选择关闭 (None)
        if self.config_key == "trans_llm_id":
            self.combo_llm.addItem("❌ None (Disable)", None)

        active_id = ConfigManager().user_settings.get(self.config_key, "openai")
        target_idx = 0

        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    configs = json.load(f)
                    for i, cfg in enumerate(configs):
                        # 读取配置时，如果没有专门的翻译模型，使用默认模型兜底
                        target_model = cfg.get(self.model_key, cfg.get("model_name", "No Model"))
                        display_text = f"{cfg.get('name', 'Unknown')} ({target_model})"
                        self.combo_llm.addItem(display_text, cfg)

                        if cfg.get("id") == active_id:
                            target_idx = self.combo_llm.count() - 1
            except Exception:
                pass

        if self.combo_llm.count() > 0:
            self.combo_llm.setCurrentIndex(target_idx)

        self.combo_llm.blockSignals(False)

    def _on_llm_changed(self):
        llm_config = self.combo_llm.currentData()
        if llm_config:
            ConfigManager().user_settings[self.config_key] = llm_config.get("id")
        else:
            ConfigManager().user_settings[self.config_key] = None

        ConfigManager().save_settings()
        self.sig_model_changed.emit()

    def get_current_config(self):
        return self.combo_llm.currentData()