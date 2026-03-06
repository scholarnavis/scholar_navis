import copy
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time

import qdarktheme
from PySide6.QtCore import Qt, QThread, Signal, QObject, QTimer, QEvent, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QFormLayout, QLineEdit,
                               QLabel, QPushButton, QGroupBox, QScrollArea, QHBoxLayout, QComboBox, QTableWidget,
                               QAbstractItemView, QHeaderView,
                               QTableWidgetItem, QCheckBox, QApplication, QFrame)
from huggingface_hub import constants

from src.core.config_manager import ConfigManager
from src.core.core_task import TaskState, TaskManager, TaskMode
from src.core.device_manager import DeviceManager
from src.core.email_check import verify_email_robust
from src.core.mcp_manager import MCPManager
from src.core.models_registry import (EMBEDDING_MODELS, RERANKER_MODELS,
                                      get_model_conf, check_model_exists, resolve_auto_model, get_onnx_cache_dir)
from src.core.network_worker import setup_global_network_env
from src.core.signals import GlobalSignals
from src.core.theme_manager import ThemeManager
from src.task.hf_download_task import RealTimeHFDownloadTask
from src.task.settings_tasks import FetchModelsTask, TestApiTask, TestDeviceTask
from src.task.common_task import VerifyModelsTask
from src.tools.base_tool import BaseTool
from src.ui.components.combo import BaseComboBox
from src.ui.components.dialog import ProgressDialog, StandardDialog, McpConfigDialog, AddModelDialog
from src.ui.components.param_editor import ParamEditorWidget
from src.ui.components.toast import ToastManager


class ScrollInterceptTableWidget(QTableWidget):
    def wheelEvent(self, event):
        super().wheelEvent(event)
        event.accept()


class FloatingOverlayFilter(QObject):
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


class EmailVerifyWorker(QObject):
    sig_finished = Signal(bool, str)

    def __init__(self, email):
        super().__init__()
        self.email = email

    def run(self):
        try:
            if not self.email:
                self.sig_finished.emit(True, "")
                return

            from src.core.email_check import verify_email_robust
            res = verify_email_robust(self.email)

            if not res.get("is_valid"):
                detailed_error = res.get("error_msg", "Unknown email validation error.")

                prompt_msg = (
                    f"Email Validation Failed:\n"
                    f"{detailed_error}\n\n"
                    f"Please provide a valid email address or leave it completely empty (which will disable NCBI tools)."
                )
                self.sig_finished.emit(False, prompt_msg)
            else:
                self.sig_finished.emit(True, "")
        except Exception as e:
            self.sig_finished.emit(False, f"Email validation encountered a system error: {e}")



class SettingsTool(BaseTool):
    def __init__(self):
        super().__init__("Global Settings")
        self.config = ConfigManager()
        self.dev_mgr = DeviceManager()
        self.widget = None
        self.llm_configs = []
        self._is_updating_model_ui = False

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
        self.init_system_section()
        self.init_network_section()
        self.init_llm_section()
        self.init_model_section()
        self.init_api_keys_section()
        self.init_mcp_section()

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

        btn_layout.addWidget(self.btn_undo)
        btn_layout.addWidget(self.btn_save)
        main_layout.addLayout(btn_layout)

        self._setup_change_listeners()
        self._load_current_settings()
        self._apply_theme()

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._refresh_mcp_status)
        self.status_timer.start(5000)

        return self.widget

    def _setup_change_listeners(self):
        """挂载全部输入组件变更事件，跟踪是否发生了改动"""
        self.input_ncbi_email.textChanged.connect(self._mark_unsaved)
        self.input_ncbi_api_key.textChanged.connect(self._mark_unsaved)
        self.input_s2_api_key.textChanged.connect(self._mark_unsaved)
        self.input_github_token.textChanged.connect(self._mark_unsaved)
        self.combo_proxy_mode.currentIndexChanged.connect(self._mark_unsaved)
        self.input_proxy.textChanged.connect(self._mark_unsaved)
        self.input_mirror.textChanged.connect(self._mark_unsaved)
        self.combo_embed.currentIndexChanged.connect(self._mark_unsaved)
        self.combo_rerank.currentIndexChanged.connect(self._mark_unsaved)
        self.combo_device.currentIndexChanged.connect(self._mark_unsaved)
        self.chk_low_vram.stateChanged.connect(self._mark_unsaved)
        self.combo_theme.currentIndexChanged.connect(self._mark_unsaved)
        self.combo_log.currentIndexChanged.connect(self._mark_unsaved)
        self.input_ext_python.textChanged.connect(self._mark_unsaved)

        # LLM listeners
        self.input_llm_name.textChanged.connect(self._mark_unsaved)
        self.input_llm_url.textChanged.connect(self._mark_unsaved)
        self.input_llm_key.textChanged.connect(self._mark_unsaved)
        self.combo_llm_preset.currentIndexChanged.connect(self._mark_unsaved)
        self.combo_llm_model.currentIndexChanged.connect(self._mark_unsaved)
        self.combo_model_param_strategy.currentIndexChanged.connect(self._mark_unsaved)
        self.combo_trans_provider.currentIndexChanged.connect(self._mark_unsaved)
        self.combo_trans_model.currentTextChanged.connect(self._mark_unsaved)
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
        for btn_name in ['btn_help_params', 'btn_copy_params', 'btn_trans_refresh']:
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
        if hasattr(self, 'chk_low_vram'):
            self.chk_low_vram.setStyleSheet(f"color: {tm.color('warning')}; font-weight: bold; margin-top: 10px;")

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
        if hasattr(self, 'btn_trans_refresh'): self.btn_trans_refresh.setIcon(tm.icon("refresh", "text_main"))

        # 将这里的 btn_open_cache 后追加一行 test_device
        if hasattr(self, 'btn_open_cache'): self.btn_open_cache.setIcon(tm.icon("folder", "accent"))
        if hasattr(self, 'btn_test_device'): self.btn_test_device.setIcon(tm.icon("test", "accent"))

        if hasattr(self, 'btn_add_mcp'): self.btn_add_mcp.setIcon(tm.icon("add", "success"))
        if hasattr(self, 'btn_refresh_mcp'): self.btn_refresh_mcp.setIcon(tm.icon("refresh", "text_main"))

    def _load_current_settings(self, is_undo=False):
        self._is_loading = True

        self.config.load_settings()
        self.config.load_mcp_servers()

        # 恢复全部文本框与复选框
        self.input_ncbi_email.setText(self.config.user_settings.get("ncbi_email", ""))
        self.input_ncbi_api_key.setText(self.config.user_settings.get("ncbi_api_key", ""))
        self.input_s2_api_key.setText(self.config.user_settings.get("s2_api_key", ""))
        self.input_github_token.setText(self.config.user_settings.get("github_token", ""))

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

        if hasattr(self, 'chk_low_vram'):
            self.chk_low_vram.setChecked(self.config.user_settings.get("low_vram_mode", False))

        self.llm_configs = self._load_llm_config()
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

        trans_id = self.config.user_settings.get("trans_llm_id", None)
        idx_trans = self.combo_trans_provider.findData(trans_id)
        if idx_trans >= 0:
            self.combo_trans_provider.setCurrentIndex(idx_trans)

        self.combo_theme.setCurrentText(self.config.user_settings.get("theme", "Dark"))
        self.combo_log.setCurrentText(self.config.user_settings.get("log_level", "INFO"))
        self.input_ext_python.setText(self.config.user_settings.get("external_python_path", "python"))

        if hasattr(self, '_load_mcp_servers_to_ui'):
            self._load_mcp_servers_to_ui()

        self._clear_unsaved()
        self._is_loading = False

        if is_undo:
            ToastManager().show("Changes reverted to the last saved state.", "info")
            if hasattr(self, '_refresh_mcp_status'):
                self._refresh_mcp_status()

    def init_api_keys_section(self):
        group = QGroupBox("Application Interface (API Keys)")
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        self.input_ncbi_email = QLineEdit()
        self.input_ncbi_email.setPlaceholderText("Required for NCBI Tools: e.g. user@university.edu")
        self.input_ncbi_email.setText(self.config.user_settings.get("ncbi_email", ""))

        self.input_ncbi_api_key = QLineEdit()
        self.input_ncbi_api_key.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.input_ncbi_api_key.setPlaceholderText("NCBI API Key (Optional but recommended)")
        self.input_ncbi_api_key.setText(self.config.user_settings.get("ncbi_api_key", ""))

        self.input_s2_api_key = QLineEdit()
        self.input_s2_api_key.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.input_s2_api_key.setPlaceholderText("Semantic Scholar Key (Prevents 429 Errors)")
        self.input_s2_api_key.setText(self.config.user_settings.get("s2_api_key", ""))

        self.input_github_token = QLineEdit()
        self.input_github_token.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.input_github_token.setPlaceholderText("GitHub Personal Access Token (Prevents rate limiting)")
        self.input_github_token.setText(self.config.user_settings.get("github_token", ""))

        self.lbl_api_hint = QLabel()
        self.lbl_api_hint.setWordWrap(True)
        self.lbl_api_hint.setOpenExternalLinks(True)
        ThemeManager().apply_class(self.lbl_api_hint, "hint")
        self._update_api_keys_html()

        layout.addRow("NCBI Email:", self.input_ncbi_email)
        layout.addRow("NCBI API Key:", self.input_ncbi_api_key)
        layout.addRow("S2 API Key:", self.input_s2_api_key)
        layout.addRow("GitHub Token:", self.input_github_token)
        layout.addRow("", self.lbl_api_hint)

        self.layout.addWidget(group)

    def _update_api_keys_html(self):
        if not hasattr(self, 'lbl_api_hint'): return
        tm = ThemeManager()
        self.lbl_api_hint.setText(
            f"<div style='line-height: 1.5;'>"
            f"<span style='color:{tm.color('danger')}; font-weight:bold;'>⚠️ NCBI STRICT POLICY:</span> "
            f"You MUST provide a valid real email address. Empty or incorrectly formatted emails will <span style='color:{tm.color('danger')}; font-weight:bold;'>completely disable</span> the NCBI PubMed/Omics tools to prevent server IP bans.<br><br>"
            f"<span style='color:{tm.color('accent')}; font-weight:bold;'>INFO & API Keys:</span><br>"
            f"• <b>NCBI PubMed:</b> An API Key increases rate limits from 3 to 10 requests/sec. "
            f"<a href='https://account.ncbi.nlm.nih.gov/settings/' style='color:{tm.color('accent')}; text-decoration:none;'>[Apply for NCBI Key]</a><br>"
            f"• <b>Semantic Scholar:</b> An API Key severely prevents '429 Too Many Requests' errors during massive literature retrieval. "
            f"<a href='https://www.semanticscholar.org/product/api' style='color:{tm.color('accent')}; text-decoration:none;'>[Apply for S2 Key]</a><br>"
            f"• <b>GitHub Token:</b> Increases search limits from 10/min to 30/min. "
            f"<a href='https://github.com/settings/tokens?type=beta' style='color:{tm.color('accent')}; text-decoration:none;'>[Generate Token]</a>"
            f"</div>"
        )


    def _refresh_mcp_status(self):
        try:
            tm = ThemeManager()
            mcp_mgr = MCPManager.get_instance()

            for row in range(self.table_mcp.rowCount()):
                name_item = self.table_mcp.item(row, 1)
                if not name_item: continue
                name = name_item.text()

                status_lbl = self.table_mcp.cellWidget(row, 5)
                if not status_lbl: continue

                chk_widget = self.table_mcp.cellWidget(row, 0)
                chk = chk_widget.layout().itemAt(0).widget() if chk_widget else None
                is_enabled = chk.isChecked() if chk else False

                if is_enabled:
                    status = mcp_mgr.get_server_status(name)
                    if status == "connected":
                        status_lbl.setText("Connected")
                        status_lbl.setStyleSheet(f"color: {tm.color('success')}; font-weight: bold;")
                    elif "error" in status:
                        status_lbl.setText("Error")
                        status_lbl.setStyleSheet(f"color: {tm.color('danger')};")
                        status_lbl.setToolTip(status)
                    else:
                        status_lbl.setText(status.capitalize())
                        status_lbl.setStyleSheet(f"color: {tm.color('warning')};")
                else:
                    status_lbl.setText("Disabled")
                    status_lbl.setStyleSheet(f"color: {tm.color('text_muted')};")

        except Exception as e:
            self.logger.error(f"Status refresh failed: {e}")

    def refresh_model_combos(self):
        curr_embed = self.combo_embed.currentData()
        curr_rerank = self.combo_rerank.currentData()

        self.combo_embed.blockSignals(True)
        self.combo_rerank.blockSignals(True)

        self.combo_embed.clear()
        for m in EMBEDDING_MODELS:
            self.combo_embed.addItem(m['ui_name'], m['id'])

        self.combo_rerank.clear()
        for m in RERANKER_MODELS:
            self.combo_rerank.addItem(m['ui_name'], m['id'])

        idx_e = self.combo_embed.findData(curr_embed)
        if idx_e >= 0: self.combo_embed.setCurrentIndex(idx_e)

        idx_r = self.combo_rerank.findData(curr_rerank)
        if idx_r >= 0: self.combo_rerank.setCurrentIndex(idx_r)

        self.combo_embed.blockSignals(False)
        self.combo_rerank.blockSignals(False)
        self.check_models_status()


    def on_undo_clicked(self):
        self._load_current_settings(is_undo=True)


    def on_download_requested(self, model_id, model_type):
        self.refresh_model_combos()

        if model_type == "embedding":
            idx = self.combo_embed.findData(model_id)
            if idx >= 0: self.combo_embed.setCurrentIndex(idx)
        else:
            idx = self.combo_rerank.findData(model_id)
            if idx >= 0: self.combo_rerank.setCurrentIndex(idx)

        self.check_models_status()

        StandardDialog(self.widget, "Model Required",
                       f"The model '{model_id}' is required for this operation but is not installed.\n\n"
                       f"It has been auto-selected in the list. Please click the blue 'Save Settings & Verify Models' button below to download it.",
                       show_cancel=False).exec()

    def init_hardware_section(self):
        self.group_hw = QGroupBox("System Hardware Info")
        self.group_hw.setObjectName("group_hw")
        layout = QVBoxLayout(self.group_hw)
        self.lbl_hw_info = QLabel()
        self.lbl_hw_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_hw_info.setTextFormat(Qt.RichText)
        layout.addWidget(self.lbl_hw_info)
        self.layout.addWidget(self.group_hw)
        self._update_hardware_html()


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

    def _update_hardware_html(self):
        if not hasattr(self, 'lbl_hw_info'): return

        tm = ThemeManager()
        info = self.dev_mgr.get_sys_info()

        gpu_info_list = info.get('gpu_info', self.dev_mgr.get_gpu_info())

        gpu_str = "<br>".join([
            f"&nbsp;&nbsp;• {g['name']} <span style='color:{tm.color('accent')};'>[{g['vram']}]</span>"
            for g in gpu_info_list
        ])

        has_accel = any(p in info.get('ort_providers', []) for p in
                        ["CUDAExecutionProvider", "DmlExecutionProvider", "CoreMLExecutionProvider",
                         "ROCmExecutionProvider"])

        status_color = tm.color("success") if has_accel else tm.color("warning")
        accel_status = "Hardware Accelerated" if has_accel else "CPU Fallback"

        clean_providers = [p.replace("ExecutionProvider", "") for p in info.get('ort_providers', [])]

        html = f"""
        <div style='font-family: Consolas, "Courier New", monospace; font-size: 13px; color: {tm.color("text_main")}; line-height: 1.6;'>
            <b>OS:</b> {info.get('os', 'Unknown')}<br>
            <b>CPU:</b> {info.get('cpu', 'Unknown')} ({info.get('cpu_cores', 'Unknown')})<br>
            <b>RAM:</b> {info.get('ram_available', 'Unknown')} / {info.get('ram_total', 'Unknown')}<br>
            <b>GPU(s):</b><br>{gpu_str}<br>
            <b>ONNX Engine:</b> v{info.get('ort_version', 'N/A')} <span style='color:{status_color}'>[{accel_status}]</span><br>
            <b>Providers:</b> {", ".join(clean_providers)}
        </div>
        """
        self.lbl_hw_info.setText(html)

    def init_ncbi_section(self):
        group = QGroupBox("Academic Databases (NCBI & Semantic Scholar)")
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        self.input_ncbi_email = QLineEdit()
        self.input_ncbi_email.setPlaceholderText("Required: e.g. user@university.edu")
        self.input_ncbi_email.setText(self.config.user_settings.get("ncbi_email", ""))

        self.input_ncbi_api_key = QLineEdit()
        self.input_ncbi_api_key.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.input_ncbi_api_key.setPlaceholderText("NCBI Key (Optional but recommended)")
        self.input_ncbi_api_key.setText(self.config.user_settings.get("ncbi_api_key", ""))

        self.input_s2_api_key = QLineEdit()
        self.input_s2_api_key.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.input_s2_api_key.setPlaceholderText("Semantic Scholar Key (Prevents 429 Errors)")
        self.input_s2_api_key.setText(self.config.user_settings.get("s2_api_key", ""))

        self.lbl_ncbi_hint = QLabel()
        self.lbl_ncbi_hint.setWordWrap(True)
        self.lbl_ncbi_hint.setOpenExternalLinks(True)
        ThemeManager().apply_class(self.lbl_ncbi_hint, "hint")
        self._update_ncbi_html()

        layout.addRow("NCBI Email:", self.input_ncbi_email)
        layout.addRow("NCBI API Key:", self.input_ncbi_api_key)
        layout.addRow("S2 API Key:", self.input_s2_api_key)
        layout.addRow("", self.lbl_ncbi_hint)

        self.layout.addWidget(group)


    def _update_ncbi_html(self):
        if not hasattr(self, 'lbl_ncbi_hint'): return
        tm = ThemeManager()
        self.lbl_ncbi_hint.setText(
            f"<div style='line-height: 1.5;'>"
            f"<span style='color:{tm.color('accent')}; font-weight:bold;'>INFO & API Keys:</span><br>"
            f"• <b>NCBI PubMed:</b> An API Key increases rate limits from 3 to 10 requests/sec. "
            f"<a href='https://account.ncbi.nlm.nih.gov/settings/' style='color:{tm.color('accent')}; text-decoration:none;'>[Apply for NCBI Key]</a><br>"
            f"• <b>Semantic Scholar:</b> An API Key severely prevents '429 Too Many Requests' errors during massive literature retrieval. "
            f"<a href='https://www.semanticscholar.org/product/api' style='color:{tm.color('accent')}; text-decoration:none;'>[Apply for S2 Key]</a>"
            f"</div>"
        )

    def init_mcp_section(self):
        tm = ThemeManager()
        group = QGroupBox("MCP Servers (Unified)")
        layout = QVBoxLayout(group)

        header_layout = QHBoxLayout()
        header_layout.addWidget(QLabel("<b>Manage Local & Remote Tools:</b>"))
        header_layout.addStretch()

        self.btn_add_mcp = QPushButton(" Add Server")
        self.btn_add_mcp.clicked.connect(self._on_add_mcp_clicked)

        self.btn_refresh_mcp = QPushButton(" Refresh Status")
        self.btn_refresh_mcp.clicked.connect(self._on_refresh_mcp_clicked)


        header_layout.addWidget(self.btn_add_mcp)
        header_layout.addWidget(self.btn_refresh_mcp)
        layout.addLayout(header_layout)

        self.table_mcp = QTableWidget(0, 7)
        self.table_mcp.setHorizontalHeaderLabels(
            ["Enabled", "Name", "Description", "Type", "Target", "Status", "Action"])
        self.table_mcp.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table_mcp.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        self.table_mcp.horizontalHeader().setSectionResizeMode(2, QHeaderView.Interactive)
        self.table_mcp.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table_mcp.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_mcp.setFixedHeight(220)
        self.table_mcp.cellDoubleClicked.connect(self._on_mcp_double_clicked)
        layout.addWidget(self.table_mcp)

        self.lbl_mcp_hint = QLabel(
            "💡 <i>Changes to MCP servers require clicking the blue 'Save Settings & Verify' button below to take effect.</i>")
        layout.addWidget(self.lbl_mcp_hint)

        self.layout.addWidget(group)

    def _on_mcp_double_clicked(self, row, col):
        name_item = self.table_mcp.item(row, 1)
        if not name_item: return

        name = name_item.text()

        if name == "builtin":
            ToastManager().show(f"Core service '{name}' cannot be edited here.", "info")
            return

        self._on_edit_mcp_clicked(row)

    def _load_mcp_servers_to_ui(self):
        servers = self.config.mcp_servers.get("mcpServers", {})
        self.table_mcp.setRowCount(0)
        for name, cfg in servers.items():
            self._add_mcp_row(name, cfg)

    def _on_refresh_mcp_clicked(self):
        try:
            mcp_mgr = MCPManager.get_instance()
            mcp_mgr.bootstrap_servers(force_all=False)
            from src.ui.components.toast import ToastManager
            ToastManager().show("Retrying offline MCP servers...", "info")
            self._refresh_mcp_status()
        except Exception as e:
            self.logger.error(f"Refresh MCP clicked failed: {e}")



    def _add_mcp_row(self, name, cfg):
        row = self.table_mcp.rowCount()
        self.table_mcp.insertRow(row)

        always_on = cfg.get("always_on", False)
        is_enabled = cfg.get("enabled", False) or always_on

        # 0. Enabled Checkbox
        chk = QCheckBox()
        chk.setChecked(is_enabled)
        if always_on:
            chk.setEnabled(False)
            chk.setToolTip("Core service must remain enabled.")

        if hasattr(self, '_mark_unsaved'):
            chk.stateChanged.connect(self._mark_unsaved)

        chk_widget = QWidget()
        l = QHBoxLayout(chk_widget)
        l.addWidget(chk)
        l.setAlignment(Qt.AlignCenter)
        l.setContentsMargins(0, 0, 0, 0)
        self.table_mcp.setCellWidget(row, 0, chk_widget)

        name_item = QTableWidgetItem(name)
        name_item.setData(Qt.UserRole, cfg)
        name_item.setFlags(name_item.flags() ^ Qt.ItemIsEditable)
        self.table_mcp.setItem(row, 1, name_item)

        desc_str = cfg.get("description", "")
        desc_item = QTableWidgetItem(desc_str)
        desc_item.setFlags(desc_item.flags() ^ Qt.ItemIsEditable)
        self.table_mcp.setItem(row, 2, desc_item)

        stype = cfg.get("type", "stdio")
        type_item = QTableWidgetItem(stype)
        type_item.setFlags(type_item.flags() ^ Qt.ItemIsEditable)
        self.table_mcp.setItem(row, 3, type_item)

        target = cfg.get("command", "") if stype == "stdio" else cfg.get("url", "")
        target_item = QTableWidgetItem(target)
        target_item.setFlags(target_item.flags() ^ Qt.ItemIsEditable)
        self.table_mcp.setItem(row, 4, target_item)

        status_lbl = QLabel("Checking...")
        status_lbl.setAlignment(Qt.AlignCenter)
        self.table_mcp.setCellWidget(row, 5, status_lbl)

        action_widget = QWidget()
        al = QHBoxLayout(action_widget)
        al.setContentsMargins(0, 0, 0, 0)
        tm = ThemeManager()

        if name != "builtin":
            btn_edit = QPushButton()
            btn_edit.setIcon(tm.icon("edit", "accent"))
            btn_edit.setCursor(Qt.PointingHandCursor)
            btn_edit.setStyleSheet("background: transparent; border: none;")
            btn_edit.clicked.connect(lambda _, r=row: self._on_edit_mcp_clicked(r))
            al.addWidget(btn_edit)

            if not always_on:
                btn_del = QPushButton()
                btn_del.setIcon(tm.icon("delete", "danger"))
                btn_del.setCursor(Qt.PointingHandCursor)
                btn_del.setStyleSheet("background: transparent; border: none;")

                def delete_mcp_row(srv_name):
                    dlg = StandardDialog(
                        self.widget,
                        "Confirm Delete",
                        f"Are you sure you want to delete MCP Server '{srv_name}'?\nThis will disconnect it immediately.",
                        show_cancel=True
                    )

                    if dlg.exec():
                        MCPManager.get_instance().disconnect_server(srv_name)

                        for i in range(self.table_mcp.rowCount()):
                            item = self.table_mcp.item(i, 1)
                            if item and item.text() == srv_name:
                                self.table_mcp.removeRow(i)
                                break

                        if hasattr(self, '_mark_unsaved'):
                            self._mark_unsaved()

                btn_del.clicked.connect(lambda _, n=name: delete_mcp_row(n))
                al.addWidget(btn_del)
        else:
            lbl_lock = QLabel()
            lbl_lock.setPixmap(tm.icon("lock", "text_muted").pixmap(16, 16))
            lbl_lock.setAlignment(Qt.AlignCenter)
            lbl_lock.setToolTip("Core system service (Read-only)")
            al.addWidget(lbl_lock)

        self.table_mcp.setCellWidget(row, 6, action_widget)

    def _on_add_mcp_clicked(self):
        warning_msg = (
            "<b>⚠️ Security Disclaimer for External MCP Servers</b><br><br>"
            "You are about to connect a third-party MCP server to Scholar Navis.<br>"
            "External servers are highly privileged and can execute code, read local files, or access the network on your behalf. "
            "<span style='color:#ff6b6b; font-weight:bold;'>Only connect to servers from trusted developers.</span><br><br>"
            "<i>The Scholar Navis developers are not responsible for any data loss, security breaches, or system damage caused by third-party MCP servers.</i><br><br>"
            "Do you understand the risks and wish to proceed?"
        )

        dlg = StandardDialog(self.widget, "Security Warning", warning_msg, show_cancel=True)
        if not dlg.exec():
            return

        # 用户同意后，才弹出真正的配置界面
        config_dlg = McpConfigDialog(self.widget)
        if config_dlg.exec():
            name, cfg = config_dlg.get_config()
            if not name: return
            cfg["enabled"] = True
            self._add_mcp_row(name, cfg)

            if hasattr(self, '_mark_unsaved'):
                self._mark_unsaved()

    def _on_edit_mcp_clicked(self, row):
        name_item = self.table_mcp.item(row, 1)
        if not name_item: return

        old_name = name_item.text()
        old_cfg = name_item.data(Qt.UserRole)

        dlg = McpConfigDialog(self.widget, server_name=old_name, server_config=old_cfg)
        if dlg.exec():
            new_name, new_cfg = dlg.get_config()
            new_cfg["always_on"] = old_cfg.get("always_on", False)
            new_cfg["enabled"] = old_cfg.get("enabled", True)

            name_item.setText(new_name)
            name_item.setData(Qt.UserRole, new_cfg)
            self.table_mcp.item(row, 2).setText(new_cfg.get("description", ""))
            self.table_mcp.item(row, 3).setText(new_cfg.get("type", "stdio"))
            target = new_cfg.get("command", "") if new_cfg.get("type", "stdio") == "stdio" else new_cfg.get("url", "")
            self.table_mcp.item(row, 4).setText(target)

            if hasattr(self, '_mark_unsaved'):
                self._mark_unsaved()

    def init_network_section(self):
        group = QGroupBox("Network Proxy")
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        self.combo_proxy_mode = BaseComboBox()

        self.combo_proxy_mode.addItems(["Disable Proxy (Direct)", "Enable Proxy (Custom)"])

        current_mode = self.config.user_settings.get("proxy_mode", "off")
        mode_map = {"off": 0, "custom": 1}
        self.combo_proxy_mode.setCurrentIndex(mode_map.get(current_mode, 0))

        self.input_proxy = QLineEdit()
        self.input_proxy.setPlaceholderText("e.g. http://127.0.0.1:7890")
        self.input_proxy.setText(self.config.user_settings.get("proxy_url", ""))

        self.input_mirror = QLineEdit()
        self.input_mirror.setPlaceholderText("Leave empty for default (huggingface.co)")
        self.input_mirror.setText(self.config.user_settings.get("hf_mirror", ""))

        self.combo_proxy_mode.currentIndexChanged.connect(self._on_proxy_mode_changed)

        layout.addRow("Proxy Mode:", self.combo_proxy_mode)
        layout.addRow("Proxy URL:", self.input_proxy)
        layout.addRow("HF Mirror:", self.input_mirror)

        self.layout.addWidget(group)

    def _on_proxy_mode_changed(self, index):
        is_custom = (index == 1)
        self.input_proxy.setEnabled(is_custom)

    def init_model_section(self):
        tm = ThemeManager()
        group = QGroupBox("AI Models Configuration")
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        # 把 Open Model Directory 按钮提前，并放在模型布局旁边/上方
        self.btn_open_cache = QPushButton(" Open Model Storage Directory")
        ThemeManager().apply_class(self.btn_open_cache, "link-btn")
        self.btn_open_cache.setCursor(Qt.PointingHandCursor)
        self.btn_open_cache.clicked.connect(self._open_hf_cache)
        layout.addRow("Model Storage:", self.btn_open_cache)

        # --- 1. Embedding 模型选择 ---
        self.combo_embed = BaseComboBox()
        self.lbl_embed_icon = QLabel()
        self.lbl_embed_text = QLabel("Checking...")
        self.lbl_embed_text.setWordWrap(True)
        self.lbl_embed_text.setTextFormat(Qt.RichText)

        embed_layout = QHBoxLayout()
        embed_layout.setContentsMargins(0, 0, 0, 0)
        embed_layout.addWidget(self.lbl_embed_icon)
        embed_layout.addWidget(self.lbl_embed_text)

        self.btn_dl_embed = QPushButton(" Download")
        self.btn_del_embed = QPushButton(" Delete")
        self.btn_dl_embed.clicked.connect(lambda: self._on_manual_model_action("embedding", "download"))
        self.btn_del_embed.clicked.connect(lambda: self._on_manual_model_action("embedding", "delete"))

        embed_layout.addStretch()
        embed_layout.addWidget(self.btn_dl_embed)
        embed_layout.addWidget(self.btn_del_embed)

        for m in EMBEDDING_MODELS:
            self.combo_embed.addItem(m['ui_name'], m['id'])

        curr_embed = self.config.user_settings.get("current_model_id", "embed_auto")
        idx = self.combo_embed.findData(curr_embed)
        self.combo_embed.setCurrentIndex(max(0, idx))
        self.combo_embed.currentIndexChanged.connect(self.check_models_status)

        # --- 2. Reranker 模型选择 ---
        self.combo_rerank = BaseComboBox()
        self.lbl_rerank_icon = QLabel()
        self.lbl_rerank_text = QLabel("Checking...")
        self.lbl_rerank_text.setWordWrap(True)
        self.lbl_rerank_text.setTextFormat(Qt.RichText)

        rerank_layout = QHBoxLayout()
        rerank_layout.setContentsMargins(0, 0, 0, 0)
        rerank_layout.addWidget(self.lbl_rerank_icon)
        rerank_layout.addWidget(self.lbl_rerank_text)
        self.btn_dl_rerank = QPushButton(" Download")
        self.btn_del_rerank = QPushButton(" Delete")
        self.btn_dl_rerank.clicked.connect(lambda: self._on_manual_model_action("reranker", "download"))
        self.btn_del_rerank.clicked.connect(lambda: self._on_manual_model_action("reranker", "delete"))

        rerank_layout.addStretch()
        rerank_layout.addWidget(self.btn_dl_rerank)
        rerank_layout.addWidget(self.btn_del_rerank)

        for m in RERANKER_MODELS:
            self.combo_rerank.addItem(m['ui_name'], m['id'])

        curr_rerank = self.config.user_settings.get("rerank_model_id", "rerank_auto")
        idx = self.combo_rerank.findData(curr_rerank)
        self.combo_rerank.setCurrentIndex(max(0, idx))
        self.combo_rerank.currentIndexChanged.connect(self.check_models_status)

        layout.addRow("Embedding:", self.combo_embed)
        layout.addRow("", embed_layout)
        layout.addRow("Reranker:", self.combo_rerank)
        layout.addRow("", rerank_layout)

        # --- 3. 硬件加速设备选择 ---
        self.combo_device = BaseComboBox()
        dev_mgr = DeviceManager()
        for dev in dev_mgr.get_available_devices():
            self.combo_device.addItem(dev["name"], dev["id"])

        curr_device = self.config.user_settings.get("inference_device", "auto")
        idx_dev = self.combo_device.findData(curr_device)
        self.combo_device.setCurrentIndex(max(0, idx_dev))

        layout.addRow("Compute Device:", self.combo_device)

        # --- 4. 其他模型设置 ---
        self.btn_test_device = QPushButton(" Test Compute Device")
        ThemeManager().apply_class(self.btn_test_device, "link-btn")
        self.btn_test_device.setCursor(Qt.PointingHandCursor)
        self.btn_test_device.clicked.connect(self._test_compute_device)
        layout.addRow("", self.btn_test_device)

        self.chk_low_vram = QCheckBox("Low VRAM Mode (Release RAG models after search)")
        self.chk_low_vram.setChecked(self.config.user_settings.get("low_vram_mode", False))
        self.chk_low_vram.setToolTip(
            "Enable this to prevent OOM errors. It will unload Embedding and Reranker models before the LLM starts generating.")

        self.lbl_vram_desc = QLabel()
        self.lbl_vram_desc.setWordWrap(True)
        self.lbl_vram_desc.setTextFormat(Qt.RichText)
        self._update_vram_html()

        layout.addRow("", self.chk_low_vram)
        layout.addRow("", self.lbl_vram_desc)

        self.layout.addWidget(group)
        QThread.msleep(50)
        self.check_models_status()

        self.combo_embed.currentTextChanged.connect(self.combo_embed.setToolTip)
        self.combo_rerank.currentTextChanged.connect(self.combo_rerank.setToolTip)


    def _update_vram_html(self):
        if not hasattr(self, 'lbl_vram_desc'): return
        tm = ThemeManager()
        self.lbl_vram_desc.setText(
            f"<div style='font-size: 11px; color: {tm.color('text_muted')}; line-height: 1.5; margin-left: 20px;'>"
            f"<b>Turn ON (Low VRAM):</b> Frees up memory immediately after document retrieval.<br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:{tm.color('success')};'>Pros: Maximizes LLM context length, prevents Out-of-Memory (OOM) crashes.</span><br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:{tm.color('danger')};'>Cons: Adds 1~3s loading delay to every new query.</span><br>"
            f"<b>Turn OFF (Speed Mode):</b> Keeps RAG models persistently in memory.<br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:{tm.color('success')};'>Pros: Lightning-fast multi-turn conversation.</span><br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:{tm.color('danger')};'>Cons: Embedding + Reranker will constantly occupy VRAM/RAM.</span>"
            f"</div>"
        )


    def _open_hf_cache(self):
        model_dir = os.path.join(self.config.BASE_DIR, "models")
        os.makedirs(model_dir, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(model_dir))


    def _test_compute_device(self):
        device_id = self.combo_device.currentData()
        if not device_id:
            return

        self.test_dev_pd = ProgressDialog(self.widget, "Device Connection Test",
                                          f"Testing inference device '{device_id}'...")
        self.test_dev_pd.show()

        self.test_dev_task_mgr = TaskManager()
        self.test_dev_task_mgr.sig_progress.connect(self.test_dev_pd.update_progress)
        self.test_dev_task_mgr.sig_result.connect(self._on_test_device_finished)
        self.test_dev_pd.sig_canceled.connect(self.test_dev_task_mgr.cancel_task)

        self.test_dev_task_mgr.start_task(
            TestDeviceTask, task_id="test_device", mode=TaskMode.THREAD,
            device_id=device_id
        )

    def _on_test_device_finished(self, result):
        if hasattr(self, 'test_dev_pd'):
            self.test_dev_pd.close_safe()

        def _show_result():
            if result.get("success"):
                StandardDialog(self.widget, "Test Passed", result["msg"]).exec()
            else:
                ToastManager().show("Device Test Failed", "error")
                StandardDialog(self.widget, "Test Failed", result["msg"]).exec()

        QTimer.singleShot(150, _show_result)



    def _load_llm_config(self):
        default_config = [
            {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1", "model_name": "",
             "api_key": ""},
            {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
             "model_name": "", "api_key": ""},
            {"id": "gemini", "name": "Google Gemini",
             "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model_name": "",
             "api_key": ""},
            {"id": "anthropic", "name": "Anthropic", "base_url": "https://api.anthropic.com/v1",
             "model_name": "", "api_key": ""},
            {"id": "nvidia", "name": "Nvidia Build", "base_url": "https://integrate.api.nvidia.com/v1",
             "model_name": "", "api_key": ""},
            {"id": "qwen", "name": "Alibaba Qwen", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "model_name": "", "api_key": ""},
            {"id": "zhipu", "name": "Zhipu GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
             "model_name": "", "api_key": ""},
            {"id": "siliconflow", "name": "SiliconFlow", "base_url": "https://api.siliconflow.cn/v1",
             "model_name": "", "api_key": ""},
            {"id": "custom", "name": "Local Custom (Ollama)", "base_url": "http://localhost:11434/v1",
             "model_name": "", "api_key": "ollama"}
        ]

        try:
            loaded_configs  = self.config.load_llm_configs()
            for cfg in loaded_configs:
                cfg.pop("thinking_model_name", None)
                if "model_params_mode" in cfg and "models_config" not in cfg:
                    m_name = cfg.get("model_name", "default")
                    cfg["models_config"] = {
                        m_name: {
                            "mode": cfg.get("model_params_mode", "inherit"),
                            "params": cfg.get("model_params", [])
                        }
                    }
        except Exception as e:
            self.logger.error(f"Error loading llm_config.json: {e}")

        existing_ids = {c.get("id") for c in loaded_configs}
        needs_resave = False
        for i, dc in enumerate(default_config):
            if dc["id"] not in existing_ids:
                loaded_configs.insert(i, dc)
                needs_resave = True


        return loaded_configs if loaded_configs else default_config

    def _save_llm_config(self):
        config_path = os.path.join(self.config.CONFIG_DIR, "llm_config.json")
        self.config.save_json(config_path, self.llm_configs, encrypt=True)

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
        self.input_llm_key.setEchoMode(QLineEdit.Password)

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

        # --- 独立翻译层选择 ---
        trans_group = QGroupBox("Translation Agent Configuration")
        trans_layout = QFormLayout(trans_group)

        trans_provider_layout = QHBoxLayout()
        self.combo_trans_provider = BaseComboBox()
        self.combo_trans_model = BaseComboBox()
        self.btn_trans_refresh = QPushButton()
        self.btn_trans_refresh.setIcon(ThemeManager().icon("refresh", "text_main"))
        self.btn_trans_refresh.setToolTip("Refresh models from cache")

        for conf in self.llm_configs:
            self.combo_trans_provider.addItem(conf.get("name", "Unnamed Provider"), conf.get("id"))

        trans_provider_layout.addWidget(self.combo_trans_provider, stretch=1)
        trans_provider_layout.addWidget(self.btn_trans_refresh)

        trans_layout.addRow("Translation Provider:", trans_provider_layout)
        trans_layout.addRow("Translation Model:", self.combo_trans_model)


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
        self.layout.addWidget(trans_group)

        self.combo_llm_preset.currentIndexChanged.connect(self._on_llm_preset_changed)
        self.input_llm_name.textChanged.connect(self._sync_llm_data)
        self.input_llm_url.textChanged.connect(self._sync_llm_data)
        self.input_llm_key.textChanged.connect(self._sync_llm_data)
        self.combo_llm_model.currentIndexChanged.connect(self._on_model_index_changed)
        self.combo_model_param_strategy.currentIndexChanged.connect(self._sync_llm_data)
        self.editor_provider_params.sig_data_changed.connect(self._sync_llm_data)
        self.editor_model_params.sig_data_changed.connect(self._sync_llm_data)

        active_id = self.config.user_settings.get("active_llm_id", "openai")
        idx_to_select = next((i for i, c in enumerate(self.llm_configs) if c.get("id") == active_id), 0)
        self.combo_llm_preset.setCurrentIndex(idx_to_select)
        self._on_llm_preset_changed(idx_to_select)

        trans_id = self.config.user_settings.get("trans_llm_id", None)
        idx_trans = self.combo_trans_provider.findData(trans_id)
        if idx_trans >= 0:
            self.combo_trans_provider.setCurrentIndex(idx_trans)

        self.combo_trans_provider.currentIndexChanged.connect(self._on_trans_provider_changed)
        self.combo_trans_model.currentTextChanged.connect(self._sync_trans_model)
        self.btn_trans_refresh.clicked.connect(self._on_trans_refresh_clicked)

        self._on_trans_provider_changed(0)

        self.combo_llm_model.currentTextChanged.connect(self.combo_llm_model.setToolTip)
        self.combo_trans_model.currentTextChanged.connect(self.combo_trans_model.setToolTip)


    def _on_trans_refresh_clicked(self):
        self._refresh_trans_models_ui()
        ToastManager().show("Translation models refreshed from cache.", "success")

    def _on_trans_provider_changed(self, index):
        if index < 0 or index >= len(self.llm_configs): return
        self._refresh_trans_models_ui()

    def _on_manual_model_action(self, model_type, action):

        if model_type == "embedding":
            model_id = self.combo_embed.currentData()
        else:
            model_id = self.combo_rerank.currentData()

        if model_id in ["embed_auto", "rerank_auto"]:
            dev = self.dev_mgr.get_optimal_device()
            model_id = resolve_auto_model(model_type, dev)

        conf = get_model_conf(model_id, model_type)
        if not conf:
            ToastManager().show(f"Model configuration not found for {model_id}", "error")
            return

        repo_id = conf.get("hf_repo_id")

        if action == "download":
            self.start_download([repo_id])

        elif action == "delete":
            dlg = StandardDialog(
                self.widget,
                "Confirm Delete",
                f"Are you sure you want to delete the local cache for '{repo_id}'?\nThis will free up disk space by removing the ONNX files.",
                show_cancel=True
            )
            if dlg.exec():
                cache_dir = get_onnx_cache_dir(repo_id)
                if os.path.exists(cache_dir):
                    try:
                        import shutil
                        shutil.rmtree(cache_dir)
                        ToastManager().show(f"Successfully deleted {repo_id}", "success")
                        self.check_models_status()
                    except Exception as e:
                        ToastManager().show(f"Failed to delete model: {e}", "error")
                else:
                    ToastManager().show("Model cache not found locally.", "info")
                    self.check_models_status()


    def _refresh_trans_models_ui(self):
        idx = self.combo_trans_provider.currentIndex()
        if idx < 0: return
        conf = self.llm_configs[idx]

        curr_model = conf.get("trans_model_name", conf.get("model_name", ""))
        fetched = conf.get("fetched_models", [])

        self.combo_trans_model.blockSignals(True)
        self.combo_trans_model.clear()

        items = list(fetched)
        if curr_model and curr_model not in items:
            items.insert(0, curr_model)

        self.combo_trans_model.addItems(items)
        if curr_model:
            self.combo_trans_model.setCurrentText(curr_model)
        self.combo_trans_model.blockSignals(False)
        self._sync_trans_model(self.combo_trans_model.currentText())

    def _sync_trans_model(self, text):
        idx = self.combo_trans_provider.currentIndex()
        if idx >= 0:
            self.llm_configs[idx]["trans_model_name"] = text.strip()
            self._save_llm_config()
            if hasattr(GlobalSignals(), 'llm_config_changed'):
                GlobalSignals().llm_config_changed.emit()

    def _extract_real_model_name(self, display_text):
        for suffix in [" [Custom]", " [Closed]"]:
            if display_text.endswith(suffix):
                return display_text[:-len(suffix)]
        return display_text




    def _on_copy_params_clicked(self):
        from src.ui.components.dialog import StandardDialog
        from src.ui.components.toast import ToastManager

        provider_params = self.editor_provider_params.extract_data()
        if not provider_params:
            ToastManager().show("Provider has no parameters to copy.", "info")
            return

        model_params = self.editor_model_params.extract_data()
        model_params_dict = {p['name']: p for p in model_params if p.get('name')}

        merged_params = list(model_params)

        for p_param in provider_params:
            name = p_param.get("name", "").strip()
            if not name:
                continue

            if name in model_params_dict:
                m_param = model_params_dict[name]
                msg = (
                    f"Parameter '{name}' already exists in this model.\n\n"
                    f"【Current Model Parameter】\n"
                    f"  • Type: {m_param.get('type')}\n"
                    f"  • Value: {m_param.get('value')}\n\n"
                    f"【Provider Parameter to Copy】\n"
                    f"  • Type: {p_param.get('type')}\n"
                    f"  • Value: {p_param.get('value')}\n\n"
                    f"Do you want to overwrite the model's parameter with the provider's?"
                )

                dlg = StandardDialog(self.widget, "Duplicate Parameter", msg, show_cancel=True)
                reply = dlg.exec()

                if reply:
                    for i, mp in enumerate(merged_params):
                        if mp.get('name') == name:
                            merged_params[i] = p_param.copy()
                            break
            else:
                merged_params.append(p_param.copy())

        try:
            self.editor_model_params.load_data(merged_params, append=False)
        except TypeError:
            self.editor_model_params.load_data(merged_params)

        self._sync_llm_data()
        ToastManager().show("Parameters copied and merged successfully.", "success")

    def _on_model_index_changed(self, index):
        if self._is_updating_model_ui or index < 0: return

        idx = self.combo_llm_preset.currentIndex()
        if idx < 0: return
        conf = self.llm_configs[idx]

        real_model_name = self._extract_real_model_name(self.combo_llm_model.itemText(index).strip())
        self._load_model_params_to_ui(conf, real_model_name)

    def _add_llm_model(self):
        dlg = AddModelDialog(self.widget)
        if dlg.exec():
            new_model = dlg.get_name()
            if new_model:
                idx = self.combo_llm_preset.currentIndex()
                if idx >= 0:
                    conf = self.llm_configs[idx]
                    if "fetched_models" not in conf:
                        conf["fetched_models"] = []
                    if new_model not in conf["fetched_models"]:
                        conf["fetched_models"].insert(0, new_model)
                    self._refresh_model_combo(conf)

                    for i in range(self.combo_llm_model.count()):
                        if self._extract_real_model_name(self.combo_llm_model.itemText(i)) == new_model:
                            self.combo_llm_model.setCurrentIndex(i)
                            break

    def _del_llm_model(self):
        idx = self.combo_llm_model.currentIndex()
        if idx < 0: return

        curr_text = self.combo_llm_model.itemText(idx)
        real_name = self._extract_real_model_name(curr_text)

        from src.ui.components.dialog import StandardDialog
        dlg = StandardDialog(self.widget, "Delete Model",
                             f"Are you sure you want to remove '{real_name}' from the list?", show_cancel=True)
        if dlg.exec():
            provider_idx = self.combo_llm_preset.currentIndex()
            if provider_idx >= 0:
                conf = self.llm_configs[provider_idx]

                if "fetched_models" in conf and real_name in conf["fetched_models"]:
                    conf["fetched_models"].remove(real_name)
                if "models_config" in conf and real_name in conf["models_config"]:
                    del conf["models_config"][real_name]

                if conf.get("model_name") == real_name:
                    conf["model_name"] = conf["fetched_models"][0] if conf.get("fetched_models") else ""

                self.combo_llm_model.blockSignals(True)
                self.combo_llm_model.setCurrentText("")
                self.combo_llm_model.blockSignals(False)

                self._refresh_model_combo(conf)
                self._sync_llm_data()

    def _load_model_params_to_ui(self, conf, model_name):
        self._is_updating_model_ui = True

        models_config = conf.get("models_config", {})
        m_conf = models_config.get(model_name, {})
        mode = m_conf.get("mode", "inherit")
        params = m_conf.get("params", [])

        reverse_map = {"inherit": 0, "custom": 1, "closed": 2}

        self.combo_model_param_strategy.blockSignals(True)
        self.combo_model_param_strategy.setCurrentIndex(reverse_map.get(mode, 0))
        self.combo_model_param_strategy.blockSignals(False)

        self.model_param_container.setVisible(mode == "custom")

        self.editor_model_params.blockSignals(True)
        self.editor_model_params.load_data(params)
        self.editor_model_params.blockSignals(False)

        self._is_updating_model_ui = False

    def _refresh_model_combo(self, conf):
        self._is_updating_model_ui = True

        curr_real = conf.get("model_name", "").strip()

        self.combo_llm_model.blockSignals(True)
        self.combo_llm_model.clear()

        fetched = list(conf.get("fetched_models", []))
        models_config = conf.get("models_config", {})

        if curr_real and curr_real not in fetched:
            fetched.insert(0, curr_real)

        tm = ThemeManager()

        for m in fetched:
            mode = models_config.get(m, {}).get("mode", "inherit")

            if mode == "custom":
                self.combo_llm_model.addItem(tm.icon("settings", "warning"), f"{m} [Custom]")
            elif mode == "closed":
                self.combo_llm_model.addItem(tm.icon("cancel", "danger"), f"{m} [Closed]")
            else:
                self.combo_llm_model.addItem(tm.icon("api", "text_muted"), m)

        idx_to_select = -1
        for i in range(self.combo_llm_model.count()):
            if self._extract_real_model_name(self.combo_llm_model.itemText(i)) == curr_real:
                idx_to_select = i
                break

        if idx_to_select >= 0:
            self.combo_llm_model.setCurrentIndex(idx_to_select)

        self.combo_llm_model.blockSignals(False)
        self._is_updating_model_ui = False

        self._load_model_params_to_ui(conf, curr_real)

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

        default_ids = ["openai", "deepseek", "gemini", "anthropic", "nvidia", "qwen", "zhipu", "siliconflow", "custom"]
        self.btn_del_llm.setEnabled(conf.get("id") not in default_ids)

        self.btn_fetch_models.setToolTip("")
        self.btn_fetch_models.setText(" Fetch")


        hide_url_providers = ["anthropic", "gemini", "zhipu", "qwen"]
        is_native = conf.get("id") in hide_url_providers

        form_layout = self.input_llm_url.parentWidget().layout()
        if form_layout:
            label = form_layout.labelForField(self.input_llm_url)
            if label:
                label.setVisible(not is_native)
        self.input_llm_url.setVisible(not is_native)


        self._refresh_model_combo(conf)

    def _sync_llm_data(self):
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
        self.combo_trans_provider.blockSignals(True)

        for i in range(1, self.combo_trans_provider.count()):
            if self.combo_trans_provider.itemData(i) == self.llm_configs[idx]["id"]:
                self.combo_trans_provider.setItemText(i, self.llm_configs[idx]["name"])
                break
        self.combo_trans_provider.blockSignals(False)
        self._update_current_model_marker(curr_real, mode)

    def _start_fetch_task(self):
        self._sync_llm_data()
        idx = self.combo_llm_preset.currentIndex()
        conf = self.llm_configs[idx] if 0 <= idx < len(self.llm_configs) else {}

        base_url = conf.get("base_url", "").strip()
        api_key = conf.get("api_key", "").strip()
        provider_id = conf.get("id", "").strip()

        if not base_url:
            StandardDialog(self.widget, "Warning", "Please enter API Base URL first.").exec()
            return

        self.net_pd = ProgressDialog(self.widget, "Network Request", "Contacting API...")
        self.net_pd.show()

        self.fetch_task_mgr = TaskManager()
        self.fetch_task_mgr.sig_progress.connect(self.net_pd.update_progress)
        self.fetch_task_mgr.sig_result.connect(self._on_models_fetched)
        self.net_pd.sig_canceled.connect(self.fetch_task_mgr.cancel_task)

        self.fetch_task_mgr.start_task(
            FetchModelsTask, task_id="fetch_models", mode=TaskMode.THREAD,
            base_url=base_url, api_key=api_key, provider_id=provider_id
        )

    def _on_models_fetched(self, result):
        self.net_pd.close_safe()

        def _show_result():
            if result.get("success"):
                models = result["models"]
                self.logger.info(f"Successfully fetched {len(models)} models from API.")
                idx = self.combo_llm_preset.currentIndex()
                if 0 <= idx < len(self.llm_configs):
                    self.llm_configs[idx]["fetched_models"] = models
                    self._refresh_model_combo(self.llm_configs[idx])
                StandardDialog(self.widget, "Success", result["msg"]).exec()
            else:
                self.logger.warning(f"Failed to fetch models: {result['msg']}")
                ToastManager().show(f"Fetch Models Failed: {result['msg']}", "error")

        QTimer.singleShot(150, _show_result)


    def _start_test_task(self):
        self._sync_llm_data()
        idx = self.combo_llm_preset.currentIndex()
        conf = self.llm_configs[idx] if 0 <= idx < len(self.llm_configs) else {}

        base_url = conf.get("base_url", "").strip()
        api_key = conf.get("api_key", "").strip()
        provider_id = conf.get("id", "")
        model_name = self._extract_real_model_name(self.combo_llm_model.currentText().strip())

        if not base_url or not model_name:
            StandardDialog(self.widget, "Warning", "Please ensure Base URL and Model Name are provided.").exec()
            return

        models_config = conf.get("models_config", {})
        param_mode = models_config.get(model_name, {}).get("mode", conf.get("model_params_mode", "inherit"))
        custom_params_list = conf.get("provider_params", []) if param_mode == "inherit" else models_config.get(
            model_name, {}).get("params", [])

        parsed_params = {}
        for p in custom_params_list:
            if not p.get("name"): continue
            try:
                if p["type"] == "int":
                    parsed_params[p["name"]] = int(p["value"])
                elif p["type"] == "float":
                    parsed_params[p["name"]] = float(p["value"])
                elif p["type"] == "bool":
                    parsed_params[p["name"]] = str(p["value"]).lower() in ['true', '1']
                else:
                    parsed_params[p["name"]] = p["value"]
            except:
                pass

        self.net_pd = ProgressDialog(self.widget, "API Connection Test", f"Sending test prompt to '{model_name}'...")
        self.net_pd.show()

        self.test_task_mgr = TaskManager()
        self.test_task_mgr.sig_progress.connect(self.net_pd.update_progress)
        self.test_task_mgr.sig_result.connect(self._on_test_finished)
        self.net_pd.sig_canceled.connect(self.test_task_mgr.cancel_task)

        self.test_task_mgr.start_task(
            TestApiTask, task_id="test_api", mode=TaskMode.THREAD,
            base_url=base_url, api_key=api_key, model_name=model_name, custom_params=parsed_params,
            provider_id=provider_id
        )

    def _on_test_finished(self, result):
        if hasattr(self, 'net_pd'):
            self.net_pd.close_safe()

        def _show_result():
            if result.get("success"):
                StandardDialog(self.widget, "Test Passed", result["msg"]).exec()
            else:
                ToastManager().show(f"API Test Failed", "error")
                StandardDialog(self.widget, "Test Failed", result["msg"]).exec()

        QTimer.singleShot(150, _show_result)


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
        default_ids = ["openai", "deepseek", "gemini", "anthropic", "nvidia", "qwen", "zhipu", "siliconflow", "custom"]
        if conf.get("id") in default_ids:
            StandardDialog(
                self.widget,
                "Warning",
                "Built-in default providers cannot be deleted."
            ).exec()
            return

        del self.llm_configs[idx]
        self.combo_llm_preset.removeItem(idx)

    def init_system_section(self):
        group = QGroupBox("System Preferences")
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        self.combo_theme = BaseComboBox()
        self.combo_theme.addItems(["Dark", "Light", "Auto"])
        self.combo_theme.setCurrentText(self.config.user_settings.get("theme", "Dark"))

        self.combo_log = BaseComboBox()
        self.combo_log.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.combo_log.setCurrentText(self.config.user_settings.get("log_level", "INFO"))

        self.input_ext_python = QLineEdit()
        self.input_ext_python.setPlaceholderText("e.g. /usr/bin/python3 or C:/Python310/python.exe")
        self.input_ext_python.setText(self.config.user_settings.get("external_python_path", "python"))

        layout.addRow("Theme:", self.combo_theme)
        layout.addRow("Log Level:", self.combo_log)
        layout.addRow("External Python Path:", self.input_ext_python)
        self.layout.addWidget(group)



    def _get_req_html(self, conf):
        if not conf or 'recommended_config' not in conf:
            return ""
        tm = ThemeManager()
        rc = conf['recommended_config']
        prio = rc.get('device_priority', 'Unknown')
        vram = rc.get('min_vram', 'N/A')
        ram = rc.get('min_ram', 'N/A')

        prio_color = tm.color("warning") if "High-End" in prio or "Required" in prio else tm.color("text_muted")

        return f"""
        <div style='margin-top:4px; font-family:Consolas; font-size:10px; color:{tm.color("text_muted")};'>
           <span style='color:{prio_color}; font-weight:bold;'>[{prio}]</span> 
           | VRAM: <span style='color:{tm.color("text_muted")}'>{vram}</span> 
           | RAM: <span style='color:{tm.color("text_muted")}'>{ram}</span>
        </div>
        """

    def check_models_status(self):
        """Asynchronously trigger VerifyModelsTask to check local ONNX files."""
        self.lbl_embed_text.setText("Verifying...")
        self.lbl_rerank_text.setText("Verifying...")

        if hasattr(self, 'verify_task_mgr') and self.verify_task_mgr:
            self.verify_task_mgr.cancel_task()

        self.verify_task_mgr = TaskManager()
        self.verify_task_mgr.sig_result.connect(self._on_models_status_result)

        embed_id = self.combo_embed.currentData()
        rerank_id = self.combo_rerank.currentData()

        self.verify_task_mgr.start_task(
            VerifyModelsTask,
            task_id="check_models_status",
            mode=TaskMode.THREAD,
            embed_id=embed_id,
            rerank_id=rerank_id
        )



    def _on_models_status_result(self, result):
        """Callback to update the UI based on VerifyModelsTask result."""
        tm = ThemeManager()
        to_download = result.get("to_download", [])
        embed_info = result.get("embed", {})
        rerank_info = result.get("rerank", {})

        # --- Embedding UI Update ---
        embed_id = self.combo_embed.currentData()
        e_conf = get_model_conf(embed_info.get("id"), "embedding")
        req_html = self._get_req_html(e_conf)
        repo_id = embed_info.get("repo_id") or "Unknown"

        msg = f"Target: {embed_info.get('id')}" if embed_id == "embed_auto" else f"Repo: {repo_id}"
        e_exists = repo_id not in to_download and not embed_info.get("is_network")

        if embed_info.get("is_network"):
            self.lbl_embed_icon.setPixmap(tm.icon("api", "success").pixmap(16, 16))
            self.lbl_embed_text.setText(f"Ready (Network API) | {msg}{req_html}")
            self.btn_dl_embed.setVisible(False)
            self.btn_del_embed.setVisible(False)
        elif e_exists:
            self.lbl_embed_icon.setPixmap(tm.icon("check-circle", "success").pixmap(16, 16))
            self.lbl_embed_text.setText(f"Ready (ONNX verified) | {msg}{req_html}")
            self.btn_dl_embed.setVisible(False)
            self.btn_del_embed.setVisible(True)
            self.btn_del_embed.setStyleSheet(self._get_btn_style(btn_type="danger"))
            self.btn_del_embed.setIcon(tm.icon("delete", "danger"))
        else:
            self.lbl_embed_icon.setPixmap(tm.icon("cancel", "danger").pixmap(16, 16))
            self.lbl_embed_text.setText(f"ONNX Not Found | {msg}{req_html}")
            self.btn_dl_embed.setVisible(True)
            self.btn_del_embed.setVisible(False)
            self.btn_dl_embed.setStyleSheet(self._get_btn_style(btn_type="primary"))
            self.btn_dl_embed.setIcon(tm.icon("download", "bg_main"))

        # --- Reranker UI Update ---
        rerank_id = self.combo_rerank.currentData()
        r_conf = get_model_conf(rerank_info.get("id"), "reranker")
        req_html_r = self._get_req_html(r_conf)
        repo_id_r = rerank_info.get("repo_id") or "Unknown"

        msg_r = f"Target: {rerank_info.get('id')}" if rerank_id == "rerank_auto" else f"Repo: {repo_id_r}"
        r_exists = repo_id_r not in to_download and not rerank_info.get("is_network")

        if rerank_info.get("is_network"):
            self.lbl_rerank_icon.setPixmap(tm.icon("api", "success").pixmap(16, 16))
            self.lbl_rerank_text.setText(f"Ready (Network API) | {msg_r}{req_html_r}")
            self.btn_dl_rerank.setVisible(False)
            self.btn_del_rerank.setVisible(False)
        elif r_exists:
            self.lbl_rerank_icon.setPixmap(tm.icon("check-circle", "success").pixmap(16, 16))
            self.lbl_rerank_text.setText(f"Ready (ONNX verified) | {msg_r}{req_html_r}")
            self.btn_dl_rerank.setVisible(False)
            self.btn_del_rerank.setVisible(True)
            self.btn_del_rerank.setStyleSheet(self._get_btn_style(btn_type="danger"))
            self.btn_del_rerank.setIcon(tm.icon("delete", "danger"))
        else:
            self.lbl_rerank_icon.setPixmap(tm.icon("cancel", "danger").pixmap(16, 16))
            self.lbl_rerank_text.setText(f"ONNX Not Found | {msg_r}{req_html_r}")
            self.btn_dl_rerank.setVisible(True)
            self.btn_del_rerank.setVisible(False)
            self.btn_dl_rerank.setStyleSheet(self._get_btn_style(btn_type="primary"))
            self.btn_dl_rerank.setIcon(tm.icon("download", "bg_main"))

    def on_save_clicked(self):
        self.widget.setFocus()

        new_email = self.input_ncbi_email.text().strip()

        self.save_pd = ProgressDialog(
            self.widget, "Applying Settings",
            "Validating settings and email address...",
            telemetry_config={"cpu": False, "ram": False, "gpu": False, "net": False, "io": False}
        )
        self.save_pd.pbar.setRange(0, 0)
        self.save_pd.show()

        self.email_thread = QThread()
        self.email_worker = EmailVerifyWorker(new_email)
        self.email_worker.moveToThread(self.email_thread)

        self.save_pd.sig_canceled.connect(self.email_thread.quit)

        self.email_thread.started.connect(self.email_worker.run)
        self.email_worker.sig_finished.connect(self._on_email_verified)
        self.email_worker.sig_finished.connect(self.email_thread.quit)
        self.email_worker.sig_finished.connect(self.email_worker.deleteLater)
        self.email_thread.finished.connect(self.email_thread.deleteLater)

        self.email_thread.start()

    def _on_email_verified(self, success, msg):
        if not success:
            if hasattr(self, 'save_pd'):
                self.save_pd.close_safe()
            StandardDialog(
                self.widget,
                "Validation Error",
                msg
            ).exec()
            return

        self.save_pd.update_progress(-1, "Saving configurations...")

        new_email = self.input_ncbi_email.text().strip()

        if hasattr(self, '_sync_llm_data'):
            self._sync_llm_data()
        self._save_llm_config()

        new_mcp_servers = {}
        if hasattr(self, 'table_mcp'):
            for row in range(self.table_mcp.rowCount()):
                name_item = self.table_mcp.item(row, 1)
                if not name_item: continue

                name = name_item.text()

                raw_cfg = name_item.data(Qt.UserRole)
                cfg = copy.deepcopy(raw_cfg) if isinstance(raw_cfg, dict) else {}

                chk_widget = self.table_mcp.cellWidget(row, 0)
                if chk_widget and chk_widget.layout():
                    chk = chk_widget.layout().itemAt(0).widget()
                    cfg["enabled"] = chk.isChecked()
                else:
                    cfg["enabled"] = False

                new_mcp_servers[name] = cfg

            if getattr(self.config, 'mcp_servers', None) is None or not isinstance(self.config.mcp_servers, dict):
                self.config.mcp_servers = {}

            self.config.mcp_servers["mcpServers"] = new_mcp_servers
            self.config.save_mcp_servers()

        new_key = self.input_ncbi_api_key.text().strip()
        new_s2_key = self.input_s2_api_key.text().strip()

        new_github_token = self.input_github_token.text().strip()

        mode_idx = self.combo_proxy_mode.currentIndex()
        new_proxy_mode = ["off", "custom"][mode_idx]
        new_proxy_url = self.input_proxy.text().strip()

        new_theme = self.combo_theme.currentText().lower()
        if new_theme == "auto": new_theme = "dark"

        ThemeManager().set_theme(new_theme)
        qdarktheme.setup_theme(new_theme)

        self.config.user_settings.update({
            "proxy_mode": new_proxy_mode,
            "proxy_url": new_proxy_url,
            "hf_mirror": self.input_mirror.text().strip(),
            "inference_device": self.combo_device.currentData(),
            "current_model_id": self.combo_embed.currentData(),
            "rerank_model_id": self.combo_rerank.currentData(),
            "active_llm_id": self._get_active_llm_id(),
            "trans_llm_id": self.combo_trans_provider.currentData(),
            "theme": self.combo_theme.currentText(),
            "log_level": self.combo_log.currentText(),
            "ncbi_email": new_email,
            "ncbi_api_key": new_key,
            "s2_api_key": new_s2_key,
            "github_token": new_github_token,
            "external_python_path": getattr(self, 'input_ext_python', QLineEdit()).text().strip(),
            "low_vram_mode": getattr(self, 'chk_low_vram', None) and self.chk_low_vram.isChecked()
        })

        for old_key in ["external_mcp_enabled", "network_mcps", "network_mcp_enabled", "custom_network_models"]:
            self.config.user_settings.pop(old_key, None)

        self.config.save_settings()

        setup_global_network_env()

        if hasattr(GlobalSignals(), 'llm_config_changed'):
            GlobalSignals().llm_config_changed.emit()

        logging.getLogger().setLevel(getattr(logging, self.combo_log.currentText()))

        self.save_pd.update_progress(-1, "Initializing background tasks...")

        if hasattr(self, 'save_task_mgr') and self.save_task_mgr:
            self.save_task_mgr.cancel_task()

        self.save_task_mgr = TaskManager()
        self.save_task_mgr.sig_progress.connect(self.save_pd.update_progress)
        self.save_task_mgr.sig_state_changed.connect(self._on_save_task_state_changed)
        self.save_task_mgr.sig_result.connect(self._on_save_task_result)
        self.save_pd.sig_canceled.connect(self.save_task_mgr.cancel_task)

        embed_id = self.combo_embed.currentData()
        rerank_id = self.combo_rerank.currentData()

        self.save_task_mgr.start_task(
            VerifyModelsTask,
            task_id="verify_settings",
            mode=TaskMode.THREAD,
            embed_id=embed_id,
            rerank_id=rerank_id,
            mcp_config=getattr(self.config, 'mcp_servers', {})
        )

    def _on_save_task_state_changed(self, state, msg):
        if state == TaskState.FAILED.value:
            if hasattr(self, 'save_pd'):
                self.save_pd.close_safe()
            self.logger.error(f"Save process failed: {msg}")
            ToastManager().show(f"System Error: {msg}", "error")


    def _on_save_task_result(self, result_dict):
        self._clear_unsaved()

        if hasattr(self, 'save_pd'):
            self.save_pd.close_safe()

        # 启动 MCP
        def _bootstrap_mcp_async():
            try:
                from src.core.mcp_manager import MCPManager
                mcp_mgr = MCPManager.get_instance()
                mcp_mgr.bootstrap_servers()

                if hasattr(self, '_refresh_mcp_status'):
                    self._refresh_mcp_status()
            except Exception as e:
                self.logger.error(f"MCP Update Failed: {e}")

        QTimer.singleShot(100, _bootstrap_mcp_async)

        def _show_success_dialog():
            msg = "Settings saved successfully."
            if result_dict and result_dict.get("to_download"):
                msg += "\n\nNote: Some selected models are missing locally. Please click 'Download' next to the models to fetch and convert them."

            StandardDialog(self.widget, "Settings Saved", msg).exec()
            self.check_models_status()

        QTimer.singleShot(150, _show_success_dialog)

    def _get_active_llm_id(self):
        idx = self.combo_llm_preset.currentIndex()
        if 0 <= idx < len(self.llm_configs):
            return self.llm_configs[idx].get("id", "")
        return ""

    def start_download(self, repo_list):
        if not repo_list: return
        self.pending_downloads = repo_list
        self.pd = ProgressDialog(self.widget, "Downloading", "Initializing...", telemetry_config={"net": True})
        self.pd.show()
        self._download_next()

    def _download_next(self):
        if not self.pending_downloads:
            self.pd.show_success_state(title="Complete", message="All downloads finished.")
            self.check_models_status()
            GlobalSignals().kb_list_changed.emit()
            return

        self.current_repo = self.pending_downloads.pop(0)

        if hasattr(self, 'task_mgr'): self.task_mgr = None
        self.task_mgr = TaskManager()
        self.task_mgr.sig_progress.connect(self.pd.update_progress)
        self.task_mgr.sig_state_changed.connect(self.on_task_state_changed)
        self.pd.sig_canceled.connect(self.task_mgr.cancel_task)

        self.task_mgr.start_task(
            RealTimeHFDownloadTask,
            task_id="hf_dl",
            repo_id=self.current_repo
        )

    def on_task_state_changed(self, state, msg):
        if state == TaskState.SUCCESS.value:
            if hasattr(self, 'task_mgr') and self.task_mgr:
                try:
                    self.task_mgr.sig_state_changed.disconnect()
                    self.task_mgr.sig_progress.disconnect()
                except:
                    pass
            QTimer.singleShot(500, self._download_next)
        elif state == TaskState.FAILED.value:
            self.pd.pbar.setRange(0, 100)
            self.pd.lbl_message.setText(f"Failed to download {self.current_repo}:\n{msg}")
            self.pd.btn_cancel.setText("Close")
            self.pd.btn_cancel.setEnabled(True)