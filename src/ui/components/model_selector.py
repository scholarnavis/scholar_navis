import os
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout
from PySide6.QtCore import Signal, Qt
from src.core.config_manager import ConfigManager
from src.ui.components.combo import BaseComboBox


class ModelSelectorWidget(QWidget):
    sig_model_changed = Signal()

    def __init__(self, parent=None, label_text="Main Model:", config_key="active_llm_id", model_key="model_name",
                 vision_key="vision_model_name", enable_vision=True):
        super().__init__(parent)
        self.config_key = config_key
        self.model_key = model_key
        self.vision_key = vision_key
        self.enable_vision = enable_vision
        self.config_manager = ConfigManager()
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(4)

        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(8)

        lbl_main = QLabel(label_text)
        lbl_main.setFixedWidth(85)
        row1.addWidget(lbl_main)

        self.combo_provider = BaseComboBox(min_width=120, max_width=300)
        self.combo_model = BaseComboBox(min_width=180, max_width=2000)
        row1.addWidget(self.combo_provider, 1)
        row1.addWidget(self.combo_model, 3)
        main_layout.addLayout(row1)

        self.combo_vision = None
        if self.enable_vision:
            row2 = QHBoxLayout()
            row2.setContentsMargins(0, 0, 0, 0)
            row2.setSpacing(8)

            lbl_vision = QLabel("Vision:")
            lbl_vision.setFixedWidth(85)
            lbl_vision.setToolTip("Select a vision model for image parsing (Optional. Useful if main model is text-only)")
            row2.addWidget(lbl_vision)

            self.combo_vision = BaseComboBox(min_width=180, max_width=2000)
            row2.addWidget(self.combo_vision, 4)
            main_layout.addLayout(row2)

            self.combo_vision.currentIndexChanged.connect(self._on_vision_changed)
            self.combo_vision.currentTextChanged.connect(self.combo_vision.setToolTip)

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

        # 定义一个标识，判断当前是否启用了视觉模型选择框
        has_vision = getattr(self, 'combo_vision', None) is not None

        if has_vision:
            self.combo_vision.blockSignals(True)

        self.combo_model.clear()

        if has_vision:
            self.combo_vision.clear()
            self.combo_vision.addItem("Auto (Use Main Model)", "auto")

        if cfg:
            self.config_manager.user_settings[self.config_key] = cfg.get("id")
            self.config_manager.save_settings()

            models = cfg.get("fetched_models", [])
            current_model = cfg.get(self.model_key, cfg.get("model_name", "No Model"))
            current_vision = cfg.get(self.vision_key, "auto")

            items = list(models)
            if current_model and current_model not in items:
                items.insert(0, current_model)

            self.combo_model.addItems(items)
            for i, model_item in enumerate(items):
                self.combo_model.setItemData(i, model_item, Qt.ToolTipRole)
            self.combo_model.setCurrentText(current_model)

            if has_vision:
                self.combo_vision.addItems(items)
                for i, model_item in enumerate(items):
                    self.combo_vision.setItemData(i + 1, model_item, Qt.ToolTipRole)

                idx = self.combo_vision.findText(current_vision)
                if idx >= 0:
                    self.combo_vision.setCurrentIndex(idx)
                else:
                    self.combo_vision.setCurrentIndex(0)
        else:
            self.config_manager.user_settings[self.config_key] = None
            self.config_manager.save_settings()

        self.combo_model.blockSignals(False)

        if has_vision:
            self.combo_vision.blockSignals(False)

        self.sig_model_changed.emit()

    def _on_model_changed(self):
        cfg = self.combo_provider.currentData()
        model_name = self.combo_model.currentText()
        if cfg and model_name:
            cfg[self.model_key] = model_name
            self._save_to_file(cfg, self.model_key, model_name)
        self.sig_model_changed.emit()

    def _on_vision_changed(self):
        cfg = self.combo_provider.currentData()
        vision_name = self.combo_vision.currentData() or self.combo_vision.currentText()
        if vision_name == "auto":
            vision_name = "auto"

        if cfg:
            cfg[self.vision_key] = vision_name
            self._save_to_file(cfg, self.vision_key, vision_name)

    def _save_to_file(self, current_cfg, key, value):
        file_configs = self.config_manager.load_llm_configs()
        for c in file_configs:
            if c["id"] == current_cfg["id"]:
                c[key] = value
                break
        self.config_manager.save_llm_configs(file_configs)

    def get_current_config(self):
        return self.combo_provider.currentData()