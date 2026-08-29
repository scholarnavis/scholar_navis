"""Settings UI mixin: LLM provider configuration.

拆分自 src/tools/settings_tool.py。负责 LLM Provider 区块的构建、
预设切换、数据同步与 Provider 的增删。
"""

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFrame, QFormLayout, QGroupBox, QHBoxLayout,
                               QLineEdit, QPushButton, QVBoxLayout, QWidget)

from src.core.theme_manager import ThemeManager
from src.ui.components.HoverRevealLineEdit import HoverRevealLineEdit
from src.ui.components.combo import BaseComboBox
from src.ui.components.dialog import StandardDialog
from src.ui.components.param_editor import ParamEditorWidget


class LlmSectionMixin:
    """LLM Provider 配置区块。"""

    # ---------- Config IO ----------
    def _load_llm_config(self):
        return self.config.load_llm_configs()

    def _save_llm_config(self):
        self.config.save_llm_configs(self.llm_configs)

    # ---------- Section build ----------
    def init_llm_section(self):

        group = QGroupBox("LLM Generation API")
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        self.llm_configs = self._load_llm_config()

        header_layout = QHBoxLayout()
        self.combo_llm_preset = BaseComboBox()
        for conf in self.llm_configs:
            self.combo_llm_preset.addItem(conf.get("name", "Unnamed Provider"))

        self.btn_add_llm = QPushButton(" Add")
        self.btn_add_llm.clicked.connect(self._add_llm_provider)

        self.btn_del_llm = QPushButton(" Delete")
        self.btn_del_llm.clicked.connect(self._del_llm_provider)

        self.btn_help_params = QPushButton(" Parameter Help")
        self.btn_help_params.clicked.connect(lambda: StandardDialog(
            self.widget, "Custom Parameter Guide",
            "You can specify request parameters (e.g., temperature, top_p, max_tokens) for the provider or specifically for a model.\n\n"
            "• Priority: Model Custom > Provider Inherit\n"
            "• If 'Closed' is selected for a model, no parameters are appended.\n"
            "• The model dropdown indicates your configuration with (⚙️ Custom) or (🚫 Closed).",
            show_cancel=False
        ).exec())

        header_layout.addWidget(self.combo_llm_preset, stretch=1)
        header_layout.addWidget(self.btn_add_llm)
        header_layout.addWidget(self.btn_del_llm)
        header_layout.addWidget(self.btn_help_params)

        self.input_llm_name = QLineEdit()
        self.input_llm_url = QLineEdit()
        self.input_llm_key = QLineEdit()
        self.input_llm_key = HoverRevealLineEdit()

        self.editor_provider_params = ParamEditorWidget()
        self.btn_add_provider_param = QPushButton(" Add Provider Parameter")
        self.btn_add_provider_param.clicked.connect(lambda: self.editor_provider_params.add_param_row())

        provider_param_layout = QVBoxLayout()
        provider_param_layout.addWidget(self.editor_provider_params)
        provider_param_layout.addWidget(self.btn_add_provider_param)

        model_layout = QHBoxLayout()
        self.combo_llm_model = BaseComboBox()

        self.btn_add_model = QPushButton(" Add")
        self.btn_add_model.clicked.connect(self._add_llm_model)

        self.btn_del_model = QPushButton(" Delete")
        self.btn_del_model.clicked.connect(self._del_llm_model)

        self.btn_fetch_models = QPushButton(" Fetch")
        self.btn_fetch_models.clicked.connect(self._start_fetch_task)

        self.btn_test_api = QPushButton(" Test")
        self.btn_test_api.clicked.connect(self._start_test_task)

        model_layout.addWidget(self.combo_llm_model, stretch=1)
        model_layout.addWidget(self.btn_add_model)
        model_layout.addWidget(self.btn_del_model)
        model_layout.addWidget(self.btn_fetch_models)
        model_layout.addWidget(self.btn_test_api)

        self.combo_model_param_strategy = BaseComboBox()
        self.combo_model_param_strategy.addItems(["Inherit (Provider)", "Custom (Model Only)", "Closed (No Params)"])

        self.editor_model_params = ParamEditorWidget()

        model_param_btn_layout = QHBoxLayout()
        self.btn_add_model_param = QPushButton(" Add Model Parameter")
        self.btn_add_model_param.clicked.connect(lambda: self.editor_model_params.add_param_row())

        self.btn_copy_params = QPushButton(" Copy from Provider")
        self.btn_copy_params.setToolTip("Copies global provider parameters to the current model.")
        self.btn_copy_params.clicked.connect(self._on_copy_params_clicked)

        model_param_btn_layout.addWidget(self.btn_add_model_param)
        model_param_btn_layout.addWidget(self.btn_copy_params)

        self.model_param_container = QWidget()
        mp_layout = QVBoxLayout(self.model_param_container)
        mp_layout.setContentsMargins(0, 0, 0, 0)
        mp_layout.addWidget(self.editor_model_params)
        mp_layout.addLayout(model_param_btn_layout)

        self.combo_model_param_strategy.currentIndexChanged.connect(
            lambda idx: self.model_param_container.setVisible(idx == 1)
        )

        layout.addRow("Service Provider:", header_layout)
        layout.addRow("Provider Name:", self.input_llm_name)
        layout.addRow("API Base URL:", self.input_llm_url)
        layout.addRow("API Key:", self.input_llm_key)
        layout.addRow("Provider Params:", provider_param_layout)
        layout.addRow(QFrame(frameShape=QFrame.HLine, frameShadow=QFrame.Sunken))
        layout.addRow("Model Name:", model_layout)
        layout.addRow("Params Strategy:", self.combo_model_param_strategy)
        layout.addRow("", self.model_param_container)

        self.layout.addWidget(group)

        self.combo_llm_preset.currentIndexChanged.connect(self._on_llm_preset_changed)
        self.input_llm_name.textChanged.connect(self._sync_llm_data_debounced)
        self.input_llm_url.textChanged.connect(self._sync_llm_data_debounced)
        self.input_llm_key.textChanged.connect(self._sync_llm_data_debounced)
        self.combo_model_param_strategy.currentIndexChanged.connect(self._sync_llm_data_debounced)
        self.editor_provider_params.sig_data_changed.connect(self._sync_llm_data_debounced)
        self.editor_model_params.sig_data_changed.connect(self._sync_llm_data_debounced)

        active_id = self.config.user_settings.get("active_llm_id", "openai")
        idx_to_select = next((i for i, c in enumerate(self.llm_configs) if c.get("id") == active_id), 0)
        self.combo_llm_preset.setCurrentIndex(idx_to_select)
        self._on_llm_preset_changed(idx_to_select)

    # ---------- Preset switching ----------
    def _on_llm_preset_changed(self, index):
        if index < 0 or index >= len(self.llm_configs): return
        conf = self.llm_configs[index]

        self.input_llm_name.blockSignals(True)
        self.input_llm_url.blockSignals(True)
        self.input_llm_key.blockSignals(True)
        self.combo_model_param_strategy.blockSignals(True)

        self.input_llm_name.setText(conf.get("name", ""))
        self.input_llm_url.setText(conf.get("base_url", ""))
        self.input_llm_key.setText(conf.get("api_key", ""))

        mode = conf.get("model_params_mode", "inherit")
        reverse_map = {"inherit": 0, "custom": 1, "closed": 2}
        self.combo_model_param_strategy.setCurrentIndex(reverse_map.get(mode, 0))
        self.model_param_container.setVisible(mode == "custom")

        self.editor_provider_params.blockSignals(True)
        self.editor_provider_params.load_data(conf.get("provider_params", []))
        self.editor_provider_params.blockSignals(False)

        self.input_llm_name.blockSignals(False)
        self.input_llm_url.blockSignals(False)
        self.input_llm_key.blockSignals(False)
        self.combo_model_param_strategy.blockSignals(False)

        default_ids = ["openai", "deepseek", "gemini", "anthropic", "nvidia", "qwen", "zhipu", "siliconflow","mimo",
                       "lmstudio", "ollma", "minimax"]
        self.btn_del_llm.setEnabled(conf.get("id") not in default_ids)

        is_minimax = conf.get("id") == "minimax"
        tm = ThemeManager()

        if is_minimax:
            self.btn_fetch_models.setText(" Refresh")
            self.btn_fetch_models.setIcon(tm.icon("refresh", "bg_main"))
            self.btn_fetch_models.setToolTip("Model retrieval is currently unsupported by the MiniMax provider. "
                                             "Click to restore predefined models.")
        else:
            self.btn_fetch_models.setText(" Fetch")
            self.btn_fetch_models.setIcon(tm.icon("api", "bg_main"))
            self.btn_fetch_models.setToolTip("")

        hide_url_providers = ["anthropic", "gemini", "zhipu", "qwen", "minimax", "deepseek", "openai", "mimo"]
        is_native = conf.get("id") in hide_url_providers

        form_layout = self.input_llm_url.parentWidget().layout()
        if form_layout:
            label = form_layout.labelForField(self.input_llm_url)
            if label:
                label.setVisible(not is_native)
        self.input_llm_url.setVisible(not is_native)

        self._refresh_model_combo(conf)

    # ---------- Data sync ----------
    def _sync_llm_data_debounced(self):
        self._sync_timer.start()

    def _sync_llm_data_execute(self):
        if self._is_updating_model_ui: return
        idx = self.combo_llm_preset.currentIndex()
        if idx < 0 or idx >= len(self.llm_configs): return

        conf = self.llm_configs[idx]
        conf["name"] = self.input_llm_name.text().strip()
        conf["base_url"] = self.input_llm_url.text().strip()
        conf["api_key"] = self.input_llm_key.text().strip()

        curr_text = self.combo_llm_model.currentText().strip()
        curr_real = self._extract_real_model_name(curr_text)
        conf["model_name"] = curr_real

        if "models_config" not in conf:
            conf["models_config"] = {}

        strategy_map = {0: "inherit", 1: "custom", 2: "closed"}
        mode = strategy_map.get(self.combo_model_param_strategy.currentIndex(), "inherit")

        conf["models_config"][curr_real] = {
            "mode": mode,
            "params": self.editor_model_params.extract_data()
        }
        conf["provider_params"] = self.editor_provider_params.extract_data()

        self.combo_llm_preset.blockSignals(True)
        self.combo_llm_preset.setItemText(idx, conf["name"])
        self.combo_llm_preset.blockSignals(False)
        self._update_current_model_marker(curr_real, mode)

    # ---------- Provider CRUD ----------
    def _add_llm_provider(self):
        new_id = f"custom_{int(time.time())}"
        new_conf = {
            "id": new_id,
            "name": "New Provider",
            "base_url": "https://",
            "model_name": "",
            "api_key": "",
            "models_config": {}
        }
        self.llm_configs.append(new_conf)
        self.combo_llm_preset.addItem(new_conf["name"])
        self.combo_llm_preset.setCurrentIndex(len(self.llm_configs) - 1)

    def _del_llm_provider(self):
        idx = self.combo_llm_preset.currentIndex()
        if idx < 0: return
        conf = self.llm_configs[idx]
        default_ids = ["openai", "deepseek", "gemini", "anthropic", "nvidia", "qwen", "zhipu", "siliconflow","mimo",
                       "lmstudio", "local"]
        if conf.get("id") in default_ids:
            StandardDialog(
                self.widget,
                "Warning",
                "Built-in default providers cannot be deleted."
            ).exec()
            return

        del self.llm_configs[idx]
        self.combo_llm_preset.removeItem(idx)

    def _get_active_llm_id(self):
        idx = self.combo_llm_preset.currentIndex()
        if 0 <= idx < len(self.llm_configs):
            return self.llm_configs[idx].get("id", "")
        return ""

    # ---------- Markers ----------
    def _extract_real_model_name(self, display_text):
        for suffix in [" [Custom]", " [Closed]"]:
            if display_text.endswith(suffix):
                return display_text[:-len(suffix)]
        return display_text

    def _update_current_model_marker(self, real_name, mode):
        self.combo_llm_model.blockSignals(True)
        tm = ThemeManager()

        if mode == "custom":
            marker = " [Custom]"
            icon = tm.icon("settings", "warning")
        elif mode == "closed":
            marker = " [Closed]"
            icon = tm.icon("cancel", "danger")
        else:
            marker = ""
            icon = tm.icon("api", "text_muted")

        new_text = f"{real_name}{marker}"
        idx = self.combo_llm_model.currentIndex()

        if idx >= 0 and self._extract_real_model_name(self.combo_llm_model.itemText(idx)) == real_name:
            self.combo_llm_model.setItemText(idx, new_text)
            self.combo_llm_model.setItemIcon(idx, icon)  # 实时更新图标
        elif self.combo_llm_model.currentText() != new_text:
            self.combo_llm_model.setCurrentText(new_text)
            current_idx = self.combo_llm_model.findText(new_text)
            if current_idx >= 0:
                self.combo_llm_model.setItemIcon(current_idx, icon)

        self.combo_llm_model.blockSignals(False)
