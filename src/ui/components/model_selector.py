import os
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel
from PySide6.QtCore import Signal, Qt
from src.core.config_manager import ConfigManager
from src.ui.components.combo import BaseComboBox


class ModelSelectorWidget(QWidget):
    sig_model_changed = Signal()

    def __init__(self, parent=None, label_text="🧠 Select:", config_key="active_llm_id", model_key="model_name"):
        super().__init__(parent)
        self.config_key = config_key
        self.model_key = model_key
        self.config_manager = ConfigManager()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        lbl = QLabel(label_text)
        lbl.setFixedWidth(85)  # 固定 Label 宽度保证对齐
        layout.addWidget(lbl)

        # 限制最大宽度，防止全屏时无限拉长
        self.combo_provider = BaseComboBox(min_width=120, max_width=200)
        self.combo_model = BaseComboBox(min_width=150, max_width=400)

        layout.addWidget(self.combo_provider)
        layout.addWidget(self.combo_model)
        layout.addStretch()

        self.combo_provider.currentIndexChanged.connect(self._on_provider_changed)
        self.combo_model.currentIndexChanged.connect(self._on_model_changed)

        self.combo_provider.currentTextChanged.connect(self.combo_provider.setToolTip)
        self.combo_model.currentTextChanged.connect(self.combo_model.setToolTip)

        self.configs = []
        self.load_llm_configs()

    def load_llm_configs(self):

        self.combo_provider.blockSignals(True)
        self.combo_provider.clear()

        if self.config_key in ["trans_llm_id", "chat_trans_llm_id", "quick_trans_llm_id"]:
            self.combo_provider.addItem("None (Disable)", None)

        active_id = self.config_manager.user_settings.get(self.config_key, "openai")
        target_idx = 0

        self.configs = self.config_manager.load_llm_configs()

        for i, cfg in enumerate(self.configs):
            provider_name = cfg.get('name', 'Unknown')
            self.combo_provider.addItem(provider_name, cfg)

            # 增加 ToolTip
            idx = self.combo_provider.count() - 1
            self.combo_provider.setItemData(idx, provider_name, Qt.ToolTipRole)

            if cfg.get("id") == active_id:
                target_idx = self.combo_provider.count() - 1

        if self.combo_provider.count() > 0:
            self.combo_provider.setCurrentIndex(target_idx)

        self.combo_provider.blockSignals(False)
        self._on_provider_changed()

    def _on_provider_changed(self):
        cfg = self.combo_provider.currentData()
        self.combo_model.blockSignals(True)
        self.combo_model.clear()

        if cfg:
            self.config_manager.user_settings[self.config_key] = cfg.get("id")
            self.config_manager.save_settings()

            models = cfg.get("fetched_models", [])
            current_model = cfg.get(self.model_key, cfg.get("model_name", "No Model"))

            items = list(models)
            if current_model and current_model not in items:
                items.insert(0, current_model)

            self.combo_model.addItems(items)

            for i, model_item in enumerate(items):
                self.combo_model.setItemData(i, model_item, Qt.ToolTipRole)

            self.combo_model.setCurrentText(current_model)
        else:
            self.config_manager.user_settings[self.config_key] = None
            self.config_manager.save_settings()

        self.combo_model.blockSignals(False)
        self.sig_model_changed.emit()

    def _on_model_changed(self):
        cfg = self.combo_provider.currentData()
        model_name = self.combo_model.currentText()
        if cfg and model_name:
            cfg[self.model_key] = model_name
            file_configs = self.config_manager.load_llm_configs()
            for c in file_configs:
                if c["id"] == cfg["id"]:
                    c[self.model_key] = model_name
                    break
            self.config_manager.save_llm_configs(file_configs)

        self.sig_model_changed.emit()

    def get_current_config(self):
        return self.combo_provider.currentData()