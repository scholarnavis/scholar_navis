"""Global Settings tool (aggregate shell).

The original ~2700-line implementation has been split by functional domain
into the mixins under ``settings_sections/``:

- EnvSectionMixin: hardware / R env / system / network / API keys / API server
- McpSectionMixin: MCP servers & native skills management
- LlmSectionMixin / LlmModelOpsMixin: LLM providers and model params
- ModelSectionMixin: embedding / reranker models & download queue
- ConfigTransferMixin: config import / export
- SaveFlowMixin: save & verification flow

This file keeps only the shell: construction, UI skeleton, theme handling
and unsaved-changes tracking.
"""

import logging

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import (QHBoxLayout, QPushButton, QScrollArea,
                               QTableWidget, QVBoxLayout, QWidget)

from src.core.config_manager import ConfigManager
from src.core.core_task import TaskManager, TaskMode
from src.core.device_manager import DeviceManager
from src.core.signals import GlobalSignals
from src.core.theme_manager import ThemeManager
from src.task.settings_tasks import HWDetectTask
from src.tools.base_tool import BaseTool
from src.tools.settings_sections import (ConfigTransferMixin, EnvSectionMixin,
                                         LlmModelOpsMixin, LlmSectionMixin,
                                         McpSectionMixin, ModelSectionMixin,
                                         SaveFlowMixin)
from src.ui.components.toast import ToastManager

logger = logging.getLogger(__name__)


class ScrollInterceptTableWidget(QTableWidget):
    """Table that keeps wheel events inside its viewport (no parent scroll)."""

    def wheelEvent(self, event):
        super().wheelEvent(event)
        event.accept()


class FloatingOverlayFilter(QObject):
    """Keeps a button pinned to the bottom-center of a parent widget."""

    def __init__(self, parent_widget, btn):
        super().__init__()
        self.parent_widget = parent_widget
        self.btn = btn

    def eventFilter(self, obj, event):
        if obj == self.parent_widget and event.type() == QEvent.Resize:
            x = (self.parent_widget.width() - self.btn.width()) // 2
            y = self.parent_widget.height() - self.btn.height() - 20
            self.btn.move(x, y)
        return super().eventFilter(obj, event)


class SettingsTool(EnvSectionMixin, McpSectionMixin, LlmSectionMixin,
                   LlmModelOpsMixin, ModelSectionMixin, ConfigTransferMixin,
                   SaveFlowMixin, BaseTool):
    # 由 UI 构建与硬件探测流程创建，仅作静态检查声明
    layout: QVBoxLayout
    btn_undo: QPushButton
    btn_save: QPushButton
    btn_export: QPushButton
    btn_import: QPushButton
    status_timer: QTimer
    _cached_hw_info: dict
    hw_task_mgr: TaskManager

    def __init__(self):
        super().__init__("Global Settings")
        self.config = ConfigManager()
        self.dev_mgr = DeviceManager()
        self.widget = None
        self.llm_configs = []
        self._is_updating_model_ui = False

        # Debouncer for LLM editor edits
        self._sync_timer = QTimer(self.widget)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(300)
        self._sync_timer.timeout.connect(self._sync_llm_data_execute)

        GlobalSignals().request_model_download.connect(self.on_download_requested)
        ThemeManager().theme_changed.connect(self._apply_theme)

    def get_ui_widget(self):
        self.widget = QWidget()
        main_layout = QVBoxLayout(self.widget)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background-color: transparent; } QWidget#scroll_content { background-color: transparent; }")
        scroll_content = QWidget()

        self.layout = QVBoxLayout(scroll_content)
        self.layout.setSpacing(20)
        self.layout.setContentsMargins(15, 0, 15, 40)

        self.init_hardware_section()
        self.init_r_environment_section()
        self.init_system_section()
        self.init_network_section()
        self.init_llm_section()
        self.init_model_section()
        self.init_api_keys_section()
        self.init_agent_tool_section()

        self.init_api_server_section()

        self.layout.addStretch()
        scroll.setWidget(scroll_content)
        main_layout.addWidget(scroll)

        # Bottom Button Area
        btn_layout = QHBoxLayout()

        self.btn_undo = QPushButton(" Revert Changes")
        self.btn_undo.clicked.connect(self.on_undo_clicked)
        self.btn_undo.setEnabled(False)

        self.btn_save = QPushButton(" Save Settings")
        self.btn_save.clicked.connect(self.on_save_clicked)

        self.btn_export = QPushButton(" Export Config")
        self.btn_export.clicked.connect(self.on_export_clicked)

        self.btn_import = QPushButton(" Import Config")
        self.btn_import.clicked.connect(self.on_import_clicked)

        btn_layout.addWidget(self.btn_export)
        btn_layout.addWidget(self.btn_import)
        btn_layout.addWidget(self.btn_undo)
        btn_layout.addWidget(self.btn_save)
        main_layout.addLayout(btn_layout)

        self._setup_change_listeners()
        self._load_current_settings()
        self._apply_theme()

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._refresh_mcp_status)
        self.status_timer.start(5000)

        self._cached_hw_info = {}

        self.hw_task_mgr = TaskManager()
        self.hw_task_mgr.sig_result.connect(self._on_hw_detected_result)
        self.hw_task_mgr.start_task(HWDetectTask, "hw_detect", mode=TaskMode.THREAD)

        return self.widget

    def _setup_change_listeners(self):
        """Attach change listeners on every input widget to track dirty state."""
        self.input_ncbi_email.textChanged.connect(self._mark_unsaved)
        self.input_ncbi_api_key.textChanged.connect(self._mark_unsaved)
        self.input_openalex_api_key.textChanged.connect(self._mark_unsaved)
        self.input_s2_api_key.textChanged.connect(self._mark_unsaved)
        self.input_s2_rate_limit.textChanged.connect(self._mark_unsaved)
        self.input_github_token.textChanged.connect(self._mark_unsaved)
        self.combo_proxy_mode.currentIndexChanged.connect(self._mark_unsaved)
        self.input_proxy.textChanged.connect(self._mark_unsaved)
        self.input_mirror.textChanged.connect(self._mark_unsaved)
        self.combo_embed.currentIndexChanged.connect(self._mark_unsaved)
        self.combo_rerank.currentIndexChanged.connect(self._mark_unsaved)
        self.combo_device.currentIndexChanged.connect(self._mark_unsaved)
        self.combo_theme.currentIndexChanged.connect(self._mark_unsaved)
        self.combo_log.currentIndexChanged.connect(self._mark_unsaved)

        # API Server listeners
        self.input_api_host.textChanged.connect(self._mark_unsaved)
        self.input_api_port.textChanged.connect(self._mark_unsaved)
        self.input_api_key.textChanged.connect(self._mark_unsaved)

        # LLM listeners
        self.input_llm_name.textChanged.connect(self._mark_unsaved)
        self.input_llm_url.textChanged.connect(self._mark_unsaved)
        self.input_llm_key.textChanged.connect(self._mark_unsaved)
        self.combo_llm_preset.currentIndexChanged.connect(self._mark_unsaved)
        self.combo_llm_model.currentIndexChanged.connect(self._mark_unsaved)
        self.combo_model_param_strategy.currentIndexChanged.connect(self._mark_unsaved)
        self.editor_provider_params.sig_data_changed.connect(self._mark_unsaved)
        self.editor_model_params.sig_data_changed.connect(self._mark_unsaved)

    def _get_input_style(self):
        tm = ThemeManager()
        return f"background: {tm.color('bg_input')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; padding: 5px; border-radius: 4px;"

    def _update_all_styles(self):
        tm = ThemeManager()

        # Update Bottom Action Buttons
        if hasattr(self, 'btn_undo'): self.btn_undo.setStyleSheet(self._get_btn_style())
        if hasattr(self, 'btn_save'): self.btn_save.setStyleSheet(self._get_btn_style(btn_type="primary"))

        # Update MCP Buttons
        if hasattr(self, 'btn_add_mcp'): self.btn_add_mcp.setStyleSheet(self._get_btn_style(btn_type="success"))
        if hasattr(self, 'btn_import_skill'): self.btn_import_skill.setStyleSheet(self._get_btn_style(btn_type="warning"))
        if hasattr(self, 'btn_refresh_mcp'): self.btn_refresh_mcp.setStyleSheet(self._get_btn_style())

        # Update LLM/Model Buttons with Colors
        if hasattr(self, 'btn_add_llm'): self.btn_add_llm.setStyleSheet(self._get_btn_style(btn_type="success"))
        if hasattr(self, 'btn_del_llm'): self.btn_del_llm.setStyleSheet(self._get_btn_style(btn_type="danger"))

        if hasattr(self, 'btn_add_model'): self.btn_add_model.setStyleSheet(self._get_btn_style(btn_type="success"))
        if hasattr(self, 'btn_del_model'): self.btn_del_model.setStyleSheet(self._get_btn_style(btn_type="danger"))

        if hasattr(self, 'btn_fetch_models'): self.btn_fetch_models.setStyleSheet(
            self._get_btn_style(btn_type="primary"))
        if hasattr(self, 'btn_test_api'): self.btn_test_api.setStyleSheet(self._get_btn_style(btn_type="primary"))

        if hasattr(self, 'btn_add_provider_param'): self.btn_add_provider_param.setStyleSheet(
            self._get_btn_style(btn_type="success"))
        if hasattr(self, 'btn_add_model_param'): self.btn_add_model_param.setStyleSheet(
            self._get_btn_style(btn_type="success"))

        # Default styling for the rest
        for btn_name in ['btn_help_params', 'btn_copy_params']:
            if hasattr(self, btn_name):
                getattr(self, btn_name).setStyleSheet(self._get_btn_style())

        # Update Subtext & Status Labels
        if hasattr(self, 'lbl_mcp_hint'):
            self.lbl_mcp_hint.setStyleSheet(f"color: {tm.color('text_muted')}; font-size: 11px;")
        if hasattr(self, 'lbl_embed_status'):
            self.lbl_embed_status.setStyleSheet(
                f"color: {tm.color('text_muted')}; font-size: 11px; margin-bottom: 5px;")
        if hasattr(self, 'lbl_rerank_status'):
            self.lbl_rerank_status.setStyleSheet(
                f"color: {tm.color('text_muted')}; font-size: 11px; margin-bottom: 5px;")

        if hasattr(self, 'table_mcp'):
            for row in range(self.table_mcp.rowCount()):
                chk_widget = self.table_mcp.cellWidget(row, 0)
                if chk_widget and chk_widget.layout():
                    chk = chk_widget.layout().itemAt(0).widget()
                    if chk:
                        chk.setStyleSheet(
                            f"color: {tm.color('text_main')}; background: transparent; margin-left: 10px;")

    def _mark_unsaved(self, *args, **kwargs):
        if getattr(self, '_is_loading', False): return
        if not self.btn_undo.isEnabled():
            self.btn_undo.setEnabled(True)
            self.btn_save.setText(" Save Settings*")

    def _clear_unsaved(self):
        self.btn_undo.setEnabled(False)
        self.btn_save.setText(" Save Settings")

    def _apply_theme(self):
        if not self.widget: return
        tm = ThemeManager()

        self.widget.setStyleSheet(f"background-color: {tm.color('bg_main')};" + tm.get_custom_qss())

        if hasattr(self, 'table_mcp'):
            self.table_mcp.setStyleSheet(f"""
                   QTableWidget {{ background-color: {tm.color('bg_card')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; }}
                   QHeaderView::section {{ background-color: {tm.color('bg_input')}; color: {tm.color('text_muted')}; border: 1px solid {tm.color('border')}; padding: 4px; }}
               """)

        self._update_all_styles()
        self._update_all_icons()

        if hasattr(self, '_update_hardware_html'): self._update_hardware_html()
        if hasattr(self, '_update_api_keys_html'): self._update_api_keys_html()
        if hasattr(self, '_update_vram_html'): self._update_vram_html()
        if hasattr(self, '_refresh_mcp_status'): self._refresh_mcp_status()

        if hasattr(self, 'combo_proxy_mode'):
            self._on_proxy_mode_changed(self.combo_proxy_mode.currentIndex())

    def _update_all_icons(self):
        """Re-assign icons to update their currentColor based on the tinted buttons"""
        tm = ThemeManager()
        if hasattr(self, 'btn_undo'): self.btn_undo.setIcon(tm.icon("undo", "text_main"))
        if hasattr(self, 'btn_save'): self.btn_save.setIcon(tm.icon("save", "bg_main"))

        # Color matched icons for the tinted backgrounds
        if hasattr(self, 'btn_add_llm'): self.btn_add_llm.setIcon(tm.icon("add", "success"))
        if hasattr(self, 'btn_del_llm'): self.btn_del_llm.setIcon(tm.icon("delete", "danger"))
        if hasattr(self, 'btn_add_model'): self.btn_add_model.setIcon(tm.icon("add", "success"))
        if hasattr(self, 'btn_del_model'): self.btn_del_model.setIcon(tm.icon("delete", "danger"))
        if hasattr(self, 'btn_fetch_models'): self.btn_fetch_models.setIcon(tm.icon("api", "bg_main"))
        if hasattr(self, 'btn_test_api'): self.btn_test_api.setIcon(tm.icon("test", "bg_main"))
        if hasattr(self, 'btn_add_provider_param'): self.btn_add_provider_param.setIcon(tm.icon("add", "success"))
        if hasattr(self, 'btn_add_model_param'): self.btn_add_model_param.setIcon(tm.icon("add", "success"))

        if hasattr(self, 'btn_help_params'): self.btn_help_params.setIcon(tm.icon("help", "text_main"))
        if hasattr(self, 'btn_copy_params'): self.btn_copy_params.setIcon(tm.icon("copy", "text_main"))

        # Append test_device next to btn_open_cache
        if hasattr(self, 'btn_open_cache'): self.btn_open_cache.setIcon(tm.icon("folder", "accent"))
        if hasattr(self, 'btn_test_device'): self.btn_test_device.setIcon(tm.icon("test", "accent"))

        if hasattr(self, 'btn_add_mcp'): self.btn_add_mcp.setIcon(tm.icon("add", "success"))
        if hasattr(self, 'btn_import_skill'): self.btn_import_skill.setIcon(tm.icon("download", "warning"))
        if hasattr(self, 'btn_refresh_mcp'): self.btn_refresh_mcp.setIcon(tm.icon("refresh", "text_main"))

    def _load_current_settings(self, is_undo=False, reload_from_disk=True):
        self._is_loading = True

        if reload_from_disk:
            self.config.load_settings()
            self.config.load_mcp_servers()
            self.llm_configs = self._load_llm_config()

        # Load API Server settings into UI
        self.input_api_host.setText(str(self.config.user_settings.get("api_server_host", "127.0.0.1")))
        self.input_api_port.setText(str(self.config.user_settings.get("api_server_port", 8000)))
        self.input_api_key.setText(self.config.user_settings.get("api_server_key", "navis-local-key"))

        self.input_ncbi_email.setText(self.config.user_settings.get("ncbi_email", ""))
        self.input_ncbi_api_key.setText(self.config.user_settings.get("ncbi_api_key", ""))
        self.input_openalex_api_key.setText(self.config.user_settings.get("openalex_api_key", ""))
        self.input_s2_api_key.setText(self.config.user_settings.get("s2_api_key", ""))
        self.input_github_token.setText(self.config.user_settings.get("github_token", ""))
        self.input_s2_rate_limit.setText(str(self.config.user_settings.get("s2_rate_limit", 1.0)))

        mode_map = {"system": 0, "off": 1, "custom": 2}
        self.combo_proxy_mode.setCurrentIndex(
            {"off": 0, "custom": 1}.get(self.config.user_settings.get("proxy_mode", "off"), 0))
        self.input_proxy.setText(self.config.user_settings.get("proxy_url", ""))
        self.input_mirror.setText(self.config.user_settings.get("hf_mirror", ""))

        curr_embed = self.config.user_settings.get("current_model_id", "embed_auto")
        idx_embed = self.combo_embed.findData(curr_embed)
        if idx_embed >= 0: self.combo_embed.setCurrentIndex(idx_embed)

        curr_rerank = self.config.user_settings.get("rerank_model_id", "rerank_auto")
        idx_rerank = self.combo_rerank.findData(curr_rerank)
        if idx_rerank >= 0: self.combo_rerank.setCurrentIndex(idx_rerank)

        curr_device = self.config.user_settings.get("inference_device", "auto")
        idx_dev = self.combo_device.findData(curr_device)
        if idx_dev >= 0: self.combo_device.setCurrentIndex(idx_dev)

        self.combo_llm_preset.blockSignals(True)
        self.combo_llm_preset.clear()
        for conf in self.llm_configs:
            self.combo_llm_preset.addItem(conf.get("name", "Unnamed Provider"))
        self.combo_llm_preset.blockSignals(False)

        active_id = self.config.user_settings.get("active_llm_id", "openai")
        idx_to_select = next((i for i, c in enumerate(self.llm_configs) if c.get("id") == active_id), 0)
        if self.combo_llm_preset.count() > 0:
            self.combo_llm_preset.setCurrentIndex(idx_to_select)
            self._on_llm_preset_changed(idx_to_select)

        self.combo_theme.setCurrentText(self.config.user_settings.get("theme", "Dark"))
        self.combo_log.setCurrentText(self.config.user_settings.get("log_level", "INFO"))

        if hasattr(self, '_load_mcp_servers_to_ui'):
            self._load_mcp_servers_to_ui()

        self._clear_unsaved()
        self._is_loading = False

        if is_undo:
            ToastManager().show("Changes reverted to the last saved state.", "info")
            if hasattr(self, '_refresh_mcp_status'):
                self._refresh_mcp_status()

    def on_undo_clicked(self):
        self._load_current_settings(is_undo=True)

    def _get_btn_style(self, btn_type="default"):
        tm = ThemeManager()

        def hex_to_rgba(hex_color, alpha):
            h = hex_color.lstrip('#')
            if len(h) < 6: return "transparent"
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            return f"rgba({r}, {g}, {b}, {alpha})"

        if btn_type == "primary":
            bg, hover, text = tm.color('accent'), tm.color('accent_hover'), tm.color('bg_main')
        elif btn_type == "danger":
            bg = hex_to_rgba(tm.color('danger'), 0.12)
            hover = hex_to_rgba(tm.color('danger'), 0.25)
            text = tm.color('danger')
        elif btn_type == "success":
            bg = hex_to_rgba(tm.color('success'), 0.12)
            hover = hex_to_rgba(tm.color('success'), 0.25)
            text = tm.color('success')
        else:
            bg, hover, text = tm.color('btn_bg'), tm.color('btn_hover'), tm.color('text_main')

        border = f"1px solid {tm.color('border')}" if btn_type == "default" else "none"

        return f"""
            QPushButton {{
                background-color: {bg}; color: {text};
                border: {border}; border-radius: 4px; padding: 6px 12px; font-weight: bold;
            }}
            QPushButton:hover:!disabled {{ background-color: {hover}; }}
            QPushButton:disabled {{
                background-color: transparent; color: {tm.color('text_muted')}; border: 1px dashed {tm.color('border')};
            }}
        """
