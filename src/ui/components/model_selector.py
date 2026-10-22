import logging

from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout
from PySide6.QtCore import Signal, Qt
from src.core.config_manager import ConfigManager
from src.core.token_estimator import litellm_context_window
from src.ui.components.combo import BaseComboBox

logger = logging.getLogger(__name__)


def format_context_size(tokens: int) -> str:
    """上下文 token 数的自动进制：512 / 128K / 1M / 10M。"""
    try:
        n = int(tokens or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B".replace(".0B", "B")
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        value = n / 1_000
        return (f"{value:.0f}K" if value >= 100
                else f"{value:.1f}K".replace(".0K", "K"))
    return str(n)


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
        # 模型动辄上百个：弹层限高 + 可输入过滤，避免下拉占满整屏。
        self.combo_model = BaseComboBox(min_width=180, max_width=2000,
                                        searchable=True, max_popup_height=340)
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

            self.combo_vision = BaseComboBox(min_width=180, max_width=2000,
                                             searchable=True, max_popup_height=320)
            row2.addWidget(self.combo_vision, 4)
            main_layout.addLayout(row2)

            self.combo_vision.currentIndexChanged.connect(self._on_vision_changed)
            self.combo_vision.currentIndexChanged.connect(
                lambda _: self._sync_combo_tooltip(self.combo_vision))

        self.combo_provider.currentIndexChanged.connect(self._on_provider_changed)
        self.combo_model.currentIndexChanged.connect(self._on_model_changed)
        self.combo_provider.currentTextChanged.connect(self.combo_provider.setToolTip)
        self.combo_model.currentIndexChanged.connect(
            lambda _: self._sync_combo_tooltip(self.combo_model))

        self.configs = []
        self.load_llm_configs()
        try:
            from src.core.signals import GlobalSignals
            GlobalSignals().model_metadata_changed.connect(self._on_model_metadata_changed)
        except Exception as e:
            logger.debug("Model metadata signal unavailable: %s", e)

    @staticmethod
    def _sync_combo_tooltip(combo):
        tip = combo.itemData(combo.currentIndex(), Qt.ToolTipRole)
        combo.setToolTip(str(tip or combo.currentText()))

    def _on_model_metadata_changed(self):
        """LiteLLM 元数据刷新后重算上下文大小；保持当前模型选择。"""
        try:
            self.load_llm_configs()
        except Exception as e:
            logger.debug("Reload model selector after metadata refresh failed: %s", e)

    def load_llm_configs(self):
        self.combo_provider.blockSignals(True)
        self.combo_provider.clear()

        if self.config_key in ["chat_trans_llm_id", "quick_trans_llm_id"]:
            self.combo_provider.addItem("None (Disable)", None)

        active_id = self.config_manager.user_settings.get(self.config_key)
        target_idx = 0

        self.configs = self.config_manager.load_llm_configs()

        for i, cfg in enumerate(self.configs):
            provider_name = cfg.get('name', 'Unknown')
            self.combo_provider.addItem(provider_name, cfg)
            idx = self.combo_provider.count() - 1
            self.combo_provider.setItemData(idx, provider_name, Qt.ToolTipRole)

            if cfg.get("id") == active_id:
                target_idx = idx

        if self.combo_provider.count() > 0:
            self.combo_provider.setCurrentIndex(target_idx)

        self.combo_provider.blockSignals(False)
        self._on_provider_changed()

    def _on_provider_changed(self):
        cfg = self.combo_provider.currentData()
        self.combo_model.blockSignals(True)
        has_vision = getattr(self, 'combo_vision', None) is not None
        if has_vision:
            self.combo_vision.blockSignals(True)

        self.combo_model.clear()
        if has_vision:
            self.combo_vision.clear()
            self.combo_vision.addItem("Auto (Use Main Model)", "auto")

        if cfg:
            provider_id = cfg.get("id")
            self.config_manager.user_settings[self.config_key] = provider_id

            models = []
            for m in cfg.get("fetched_models", []):
                clean_m = str(m).strip()
                if clean_m and clean_m not in models:
                    models.append(clean_m)

            models_config = cfg.get("models_config", {})
            for m in models_config:
                clean_m = str(m).strip()
                if clean_m and clean_m not in models:
                    models.append(clean_m)

            default_model = str(cfg.get("model_name", "")).strip()
            if default_model and default_model not in models:
                models.insert(0, default_model)

            memorized_model = str(self.config_manager.user_settings.get(f"{self.model_key}_{provider_id}", "")).strip()
            if not memorized_model or memorized_model not in models:
                memorized_model = default_model if default_model else (models[0] if models else "")

            tm = None
            try:
                from src.core.theme_manager import ThemeManager
                tm = ThemeManager()
            except ImportError:
                pass

            idx_to_select = 0
            for i, m_name in enumerate(models):
                mode = models_config.get(m_name, {}).get("mode", "inherit")
                display_name = m_name
                icon = None

                if mode == "custom":
                    display_name = f"{m_name} [Custom]"
                    if tm: icon = tm.icon("settings", "warning")
                elif mode == "closed":
                    display_name = f"{m_name} [Closed]"
                    if tm: icon = tm.icon("cancel", "danger")
                else:
                    if tm: icon = tm.icon("api", "text_muted")

                # 上下文大小只用 LiteLLM 的 model map；未知就显示“?”，不猜。
                ctx = litellm_context_window(m_name)
                ctx_label = format_context_size(ctx)
                display_name = f"{display_name} · {ctx_label or '?'}"
                tooltip = (f"{display_name}\nContext window: {ctx:,} tokens (LiteLLM)"
                           if ctx > 0 else
                           f"{display_name}\nContext window: unknown (LiteLLM has no entry)")

                if icon:
                    self.combo_model.addItem(icon, display_name, m_name)
                else:
                    self.combo_model.addItem(display_name, m_name)

                self.combo_model.setItemData(i, tooltip, Qt.ToolTipRole)

                if m_name == memorized_model:
                    idx_to_select = i

            if self.combo_model.count() > 0:
                self.combo_model.setCurrentIndex(idx_to_select)

            # 视觉模型也同样采用清洗后的列表
            if has_vision:
                memorized_vision = str(
                    self.config_manager.user_settings.get(f"{self.vision_key}_{provider_id}", "auto")).strip()
                for i, m_name in enumerate(models):
                    ctx = litellm_context_window(m_name)
                    ctx_label = format_context_size(ctx)
                    label = f"{m_name} · {ctx_label or '?'}"
                    self.combo_vision.addItem(label, m_name)
                    self.combo_vision.setItemData(
                        i + 1,
                        (f"{label}\nContext window: {ctx:,} tokens (LiteLLM)"
                         if ctx > 0 else
                         f"{label}\nContext window: unknown (LiteLLM has no entry)"),
                        Qt.ToolTipRole)
                idx = self.combo_vision.findData(memorized_vision)
                if idx >= 0:
                    self.combo_vision.setCurrentIndex(idx)
                else:
                    self.combo_vision.setCurrentIndex(0)

            self.combo_model.setEnabled(True)
            if has_vision: self.combo_vision.setEnabled(True)
        else:
            self.config_manager.user_settings[self.config_key] = ""
            self.combo_model.addItem("Disabled", "")
            self.combo_model.setEnabled(False)
            if has_vision: self.combo_vision.setEnabled(False)

        self.config_manager.save_settings()
        self.combo_model.blockSignals(False)
        if has_vision: self.combo_vision.blockSignals(False)
        self._sync_combo_tooltip(self.combo_model)
        if has_vision:
            self._sync_combo_tooltip(self.combo_vision)
        self.sig_model_changed.emit()

    def _on_model_changed(self):
        cfg = self.combo_provider.currentData()
        if not cfg: return

        provider_id = cfg.get("id")

        clean_model = str(self.combo_model.currentData() or "").strip()
        if not clean_model:
            # 可输入下拉：用户打了半截文本但没从列表里选（回车/失焦都会走到这里），
            # 不能把这段自由文本当成模型名写进配置——那会让后续请求直接 404。
            matched = self.combo_model.findText(
                self.combo_model.currentText(), Qt.MatchContains | Qt.MatchFixedString)
            if matched < 0:
                return
            clean_model = str(self.combo_model.itemData(matched) or "").strip()
            if not clean_model:
                return
            self.combo_model.setCurrentIndex(matched)
            return              # setCurrentIndex 会再次触发本槽，交给那次处理

        display_name = clean_model

        if provider_id and clean_model:
            self.config_manager.user_settings[f"{self.model_key}_{provider_id}"] = clean_model
            self.config_manager.user_settings[self.model_key] = display_name
            self.config_manager.save_settings()
        self.sig_model_changed.emit()

    def _on_vision_changed(self):
        cfg = self.combo_provider.currentData()
        if not cfg: return

        provider_id = cfg.get("id")
        vision_name = str(self.combo_vision.currentData() or "").strip()
        if not vision_name:
            matched = self.combo_vision.findText(
                self.combo_vision.currentText(), Qt.MatchContains | Qt.MatchFixedString)
            if matched < 0:
                return
            vision_name = str(self.combo_vision.itemData(matched) or "").strip()
            if not vision_name:
                return
            self.combo_vision.setCurrentIndex(matched)
            return

        if provider_id:
            self.config_manager.user_settings[f"{self.vision_key}_{provider_id}"] = vision_name
            self.config_manager.user_settings[self.vision_key] = vision_name
            self.config_manager.save_settings()


    def _save_to_file(self, current_cfg, key, value):
        file_configs = self.config_manager.load_llm_configs()
        for c in file_configs:
            if c["id"] == current_cfg["id"]:
                c[key] = value
                break
        self.config_manager.save_llm_configs(file_configs)


    def get_current_config(self):
        """直接吐出合并且清洗好的 config，无需外部再操作"""
        cfg = self.combo_provider.currentData()
        if cfg:
            cfg_copy = cfg.copy()
            clean_model = self.combo_model.currentData()

            if clean_model:
                # 最终兜底防线
                cfg_copy["model_name"] = str(clean_model).strip()

            if getattr(self, 'combo_vision', None) is not None and self.enable_vision:
                vision_val = self.combo_vision.currentData()
                if vision_val and vision_val != "auto":
                    cfg_copy["vision_model_name"] = str(vision_val).strip()
                else:
                    cfg_copy["vision_model_name"] = ""
            return cfg_copy
        return None