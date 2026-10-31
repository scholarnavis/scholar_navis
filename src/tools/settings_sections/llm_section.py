"""Settings UI mixin: LLM provider configuration.

拆分自 src/tools/settings_tool.py。负责 LLM Provider 区块的构建、
预设切换、数据同步与 Provider 的增删。
"""

import threading
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QFrame, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget)

from src.core.i18n import tr
from src.core.theme_manager import ThemeManager
from src.ui.components.HoverRevealLineEdit import HoverRevealLineEdit
from src.ui.components.combo import BaseComboBox
from src.ui.components.dialog import StandardDialog

from src.ui.components.toast import ToastManager


def parse_optional_positive_int(text) -> int:
    """把"可留空的正整数"输入解析成 int（留空/非法/非正数都返回 0 = 自动）。

    上下文窗口、输出上限都走这一个解析：调用方据 0 判定"交给自动推断"，
    这样 UI 与配置里都不会出现 ``"0"``、``""`` 这类含混值。
    """
    try:
        value = int(float(str(text).strip()))
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


class LlmSectionMixin:
    """LLM Provider 配置区块。"""

    # ---------- Config IO ----------
    def _load_llm_config(self):
        return self.config.load_llm_configs()

    def _save_llm_config(self):
        self.config.save_llm_configs(self.llm_configs)

    # ---------- Section build ----------
    def init_llm_section(self):

        group = QGroupBox(tr("LLM Generation API"))
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        self.llm_configs = self._load_llm_config()

        header_layout = QHBoxLayout()
        self.combo_llm_preset = BaseComboBox()
        for conf in self.llm_configs:
            self.combo_llm_preset.addItem(conf.get("name", tr("Unnamed Provider")))

        self.btn_add_llm = QPushButton(tr(" Add"))
        self.btn_add_llm.clicked.connect(self._add_llm_provider)

        self.btn_del_llm = QPushButton(tr(" Delete"))
        self.btn_del_llm.clicked.connect(self._del_llm_provider)

        header_layout.addWidget(self.combo_llm_preset, stretch=1)
        header_layout.addWidget(self.btn_add_llm)
        header_layout.addWidget(self.btn_del_llm)

        self.input_llm_name = QLineEdit()
        self.input_llm_url = QLineEdit()
        self.input_llm_key = QLineEdit()
        self.input_llm_key = HoverRevealLineEdit()

        # 模型容量声明（可选，留空 = 自动）。OpenAI 兼容接口**不提供**上下文窗口与
        # 输出上限，程序只能靠 litellm 元数据/模型家族名推断，自建网关的模型名往往
        # 推断不出（落回兜底默认：远程 1M、本地 32K）。此处填一次即永久生效，
        # 优先级高于一切推断，见 src/core/model_capabilities.py。
        self.input_llm_context_window = QLineEdit()
        self.input_llm_context_window.setPlaceholderText(tr("Auto"))
        self.input_llm_context_window.setToolTip(tr(
            "Declare this model's input context window in tokens (e.g. 128000).\n"
            "Leave empty to infer automatically: litellm metadata → built-in family\n"
            "table → measured value reported by the provider when a request overflows."))
        self.input_llm_max_output = QLineEdit()
        self.input_llm_max_output.setPlaceholderText(tr("Auto"))
        self.input_llm_max_output.setToolTip(tr(
            "Declare the maximum number of output tokens (e.g. 8192).\n"
            "Leave empty to use the provider's default."))
        capacity_layout = QHBoxLayout()
        capacity_layout.setContentsMargins(0, 0, 0, 0)
        self.btn_detect_context = QPushButton(tr("Detect"))
        self.btn_detect_context.setCursor(Qt.PointingHandCursor)
        self.btn_detect_context.setToolTip(tr(
            "Ask the provider's /models metadata for a context window. "
            "OpenAI official endpoints usually do not expose it."))
        self.btn_detect_context.clicked.connect(self._detect_context_window)
        self.btn_refresh_models = QPushButton(tr("Refresh models"))
        self.btn_refresh_models.setCursor(Qt.PointingHandCursor)
        self.btn_refresh_models.setToolTip(tr(
            "Download LiteLLM's official model_prices_and_context_window.json "
            "so new models (GPT-6, DeepSeek V4, Qwen3-Max, ...) resolve correctly."))
        self.btn_refresh_models.clicked.connect(self._refresh_litellm_models)
        capacity_layout.addWidget(QLabel(tr("Context:")))
        capacity_layout.addWidget(self.input_llm_context_window, stretch=1)
        capacity_layout.addWidget(self.btn_detect_context)
        capacity_layout.addWidget(self.btn_refresh_models)
        capacity_layout.addWidget(QLabel(tr("Max Output:")))
        capacity_layout.addWidget(self.input_llm_max_output, stretch=1)

        model_layout = QHBoxLayout()
        self.combo_llm_model = BaseComboBox()

        self.btn_add_model = QPushButton(tr(" Add"))
        self.btn_add_model.clicked.connect(self._add_llm_model)

        self.btn_del_model = QPushButton(tr(" Delete"))
        self.btn_del_model.clicked.connect(self._del_llm_model)

        self.btn_fetch_models = QPushButton(tr(" Fetch"))
        self.btn_fetch_models.clicked.connect(self._start_fetch_task)

        self.btn_test_api = QPushButton(tr(" Test"))
        self.btn_test_api.clicked.connect(self._start_test_task)

        model_layout.addWidget(self.combo_llm_model, stretch=1)
        model_layout.addWidget(self.btn_add_model)
        model_layout.addWidget(self.btn_del_model)
        model_layout.addWidget(self.btn_fetch_models)
        model_layout.addWidget(self.btn_test_api)

        layout.addRow(tr("Service Provider:"), header_layout)
        layout.addRow(tr("Provider Name:"), self.input_llm_name)
        layout.addRow(tr("API Base URL:"), self.input_llm_url)
        layout.addRow(tr("API Key:"), self.input_llm_key)
        layout.addRow(tr("Model Capacity:"), capacity_layout)
        layout.addRow(QFrame(frameShape=QFrame.HLine, frameShadow=QFrame.Sunken))
        layout.addRow(tr("Model Name:"), model_layout)

        self.layout.addWidget(group)

        self.combo_llm_preset.currentIndexChanged.connect(self._on_llm_preset_changed)
        self.input_llm_name.textChanged.connect(self._sync_llm_data_debounced)
        self.input_llm_url.textChanged.connect(self._sync_llm_data_debounced)
        self.input_llm_key.textChanged.connect(self._sync_llm_data_debounced)
        self.input_llm_context_window.textChanged.connect(self._sync_llm_data_debounced)
        self.input_llm_max_output.textChanged.connect(self._sync_llm_data_debounced)

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
        self.input_llm_context_window.blockSignals(True)
        self.input_llm_max_output.blockSignals(True)

        self.input_llm_name.setText(conf.get("name", ""))
        self.input_llm_url.setText(conf.get("base_url", ""))
        self.input_llm_key.setText(conf.get("api_key", ""))
        self.input_llm_context_window.setText(str(conf.get("context_window") or ""))
        self.input_llm_max_output.setText(str(conf.get("max_output_tokens") or ""))

        self.input_llm_name.blockSignals(False)
        self.input_llm_url.blockSignals(False)
        self.input_llm_key.blockSignals(False)
        self.input_llm_context_window.blockSignals(False)
        self.input_llm_max_output.blockSignals(False)

        default_ids = ["openai", "deepseek", "gemini", "anthropic", "nvidia", "qwen", "zhipu", "siliconflow","mimo",
                       "lmstudio", "ollma", "minimax"]
        self.btn_del_llm.setEnabled(conf.get("id") not in default_ids)

        is_minimax = conf.get("id") == "minimax"
        tm = ThemeManager()

        if is_minimax:
            self.btn_fetch_models.setText(tr(" Refresh"))
            self.btn_fetch_models.setIcon(tm.icon("refresh", "bg_main"))
            self.btn_fetch_models.setToolTip(tr(
                "Model retrieval is currently unsupported by the MiniMax provider. "
                "Click to restore predefined models."))
        else:
            self.btn_fetch_models.setText(tr(" Fetch"))
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
    def _refresh_litellm_models(self):
        """手动拉取 LiteLLM 官方模型表，解决本地备份滞后。"""
        from src.core.token_estimator import refresh_litellm_model_map

        result = {}
        self.btn_refresh_models.setEnabled(False)
        self.btn_refresh_models.setText(tr("Refreshing..."))

        def worker():
            result["count"] = refresh_litellm_model_map(timeout=25)

        thread = threading.Thread(target=worker, daemon=True,
                                  name="litellm-model-refresh")
        thread.start()

        def poll():
            if thread.is_alive():
                QTimer.singleShot(200, poll)
                return
            self.btn_refresh_models.setEnabled(True)
            self.btn_refresh_models.setText(tr("Refresh models"))
            count = int(result.get("count") or 0)
            if count > 0:
                ToastManager().show(
                    tr("LiteLLM model metadata refreshed: {n} entries").format(n=count),
                    "success")
            else:
                ToastManager().show(
                    tr("Could not refresh LiteLLM model metadata; using the local backup."),
                    "warning")

        QTimer.singleShot(200, poll)

    def _detect_context_window(self):
        """异步探测 provider 元数据里的上下文窗口，不阻塞设置页。"""
        url = self.input_llm_url.text().strip()
        # 查窗口要用**模型名**：provider 的 /models 与 LiteLLM 元数据都按模型名索引。
        # 原先取的是 Provider Name（配置备注名），两个来源必然都查不到，于是"备注名
        # ≠ 模型名"的条目永远弹"未暴露"，litellm 还会抱怨模型名未知。
        name = self._current_real_model_name()
        key = self.input_llm_key.text().strip()
        if not url or not name:
            ToastManager().show(
                tr("Provider URL and model name are required for detection."),
                "warning")
            return

        from src.core.model_capabilities import (probe_provider_context,
                                                 record_context_window)

        result = {}
        self.btn_detect_context.setEnabled(False)
        self.btn_detect_context.setText(tr("Detecting..."))

        def worker():
            value = probe_provider_context(url, name, key)
            source = "provider-metadata"
            if not value:
                # Provider 不暴露时退回 LiteLLM 的模型元数据（含别名/日期后缀）。
                from src.core.token_estimator import _static_context_window
                value = _static_context_window(name)
                source = "litellm-metadata"
            result["value"] = value
            result["source"] = source

        thread = threading.Thread(target=worker, daemon=True,
                                  name="context-window-probe")
        thread.start()

        def poll():
            if thread.is_alive():
                QTimer.singleShot(150, poll)
                return
            self.btn_detect_context.setEnabled(True)
            self.btn_detect_context.setText(tr("Detect"))
            # 探测期间用户可能换了 provider 或模型：只在字段未变时回填。
            if (self.input_llm_url.text().strip() != url
                    or self._current_real_model_name() != name):
                return
            value = int(result.get("value") or 0)
            if value > 0:
                self.input_llm_context_window.setText(str(value))
                record_context_window(url, name, value,
                                      source=result.get("source", "provider-metadata"))
                ToastManager().show(
                    tr("Detected context window: {n} tokens").format(n=value),
                    "success")
            else:
                ToastManager().show(
                    tr("Provider did not expose a context window; "
                       "using static inference or the 1M fallback."), "warning")

        QTimer.singleShot(150, poll)

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

        # 自定义参数编辑器已移除：不再写 models_config / provider_params。
        # 既有配置保持原样（不删除、不覆盖），避免"删了界面顺带清掉用户数据"。
        mode = "inherit"
        existing_model_conf = (conf.get("models_config") or {}).get(curr_real)
        if isinstance(existing_model_conf, dict):
            mode = str(existing_model_conf.get("mode") or "inherit")

        # 容量声明：留空即删除该键（配置里不留 "" / 0 这类含混值，自动推断看键是否存在）
        context_window = parse_optional_positive_int(self.input_llm_context_window.text())
        max_output = parse_optional_positive_int(self.input_llm_max_output.text())
        for key, value in (("context_window", context_window), ("max_output_tokens", max_output)):
            if value:
                conf[key] = value
            else:
                conf.pop(key, None)

        self.combo_llm_preset.blockSignals(True)
        self.combo_llm_preset.setItemText(idx, conf["name"])
        self.combo_llm_preset.blockSignals(False)
        self._update_current_model_marker(curr_real, mode)

    # ---------- Provider CRUD ----------
    def _add_llm_provider(self):
        new_id = f"custom_{int(time.time())}"
        new_conf = {
            "id": new_id,
            "name": tr("New Provider"),
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
                tr("Warning"),
                tr("Built-in default providers cannot be deleted.")
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

    def _current_real_model_name(self):
        """当前选中的**真实模型名**（剥掉下拉框里的 ``[Custom]`` / ``[Closed]`` 标记）。

        探测上下文窗口、写回 ``model_name`` 都必须用它，因为 provider 的 ``/models``
        与 LiteLLM 元数据都按模型名索引；``Provider Name`` 只是这条配置的备注名。
        """
        return self._extract_real_model_name(self.combo_llm_model.currentText().strip())

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
