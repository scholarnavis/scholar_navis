import os
import logging
import re
import shutil
import subprocess
import sys
import json
import time

import qdarktheme
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QFormLayout, QLineEdit,
                               QLabel, QPushButton, QGroupBox, QMessageBox,
                               QScrollArea, QHBoxLayout, QComboBox, QTableWidget, QAbstractItemView, QHeaderView,
                               QTableWidgetItem, QFrame)
from PySide6.QtCore import Qt, QThread, Signal, QObject, QTimer
from huggingface_hub import constants

from src.core.core_task import TaskState, TaskManager
from src.core.device_manager import DeviceManager
from src.core.models_registry import (EMBEDDING_MODELS, RERANKER_MODELS,
                                      get_model_conf, check_model_exists, resolve_auto_model)
from src.core.network_worker import setup_global_network_env, LightNetworkWorker
from src.core.signals import GlobalSignals
from src.tools.base_tool import BaseTool
from src.core.config_manager import ConfigManager
from src.task.hf_download_task import RealTimeHFDownloadTask
from src.ui.components.combo import BaseComboBox
from src.ui.components.dialog import ProgressDialog, StandardDialog
from src.ui.components.toast import ToastManager


class DownloadCancelledException(Exception):
    pass


class BatchDownloadWorker(QObject):
    sig_progress = Signal(int, str)
    sig_finished = Signal(bool, str)

    def __init__(self, download_list):
        super().__init__()
        self.download_list = download_list
        self.is_running = True
        self._process = None
        self.current_repo = None

    def stop(self):
        self.is_running = False
        if self._process is not None:
            try:
                self._process.kill()
            except Exception:
                pass

    def _nuke_cache(self, repo_id):
        hf_home = constants.HF_HOME
        folder_name = "models--" + repo_id.replace("/", "--")
        target_dir = os.path.join(hf_home, "hub", folder_name)

        if os.path.exists(target_dir):
            try:
                shutil.rmtree(target_dir)
                print(f"Cache nuked: {target_dir}")
            except Exception as e:
                print(f"Failed to wipe cache: {e}")

    def run(self):
        try:
            env = os.environ.copy()
            env["HF_HUB_DISABLE_PROGRESS_BARS"] = "0"

            total = len(self.download_list)
            for i, repo_id in enumerate(self.download_list):
                self.current_repo = repo_id
                if not self.is_running:
                    break

                self.sig_progress.emit(0, f"Preparing ({i + 1}/{total}): {repo_id}")

                cmd = [sys.executable, "-m", "huggingface_hub.commands.huggingface_cli", "download", repo_id]

                kwargs = {}
                if sys.platform == "win32":
                    kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

                self._process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, encoding='utf-8', errors='replace',
                    env=env, **kwargs
                )

                for line in self._process.stdout:
                    if not self.is_running:
                        break

                    pct_match = re.search(r'(\d+)%', line)
                    if pct_match:
                        pct = int(pct_match.group(1))
                        speed_str = ""
                        speed_match = re.search(r'\[.*?,\s*([^\]]+)\]', line)
                        if speed_match:
                            speed_str = f" | {speed_match.group(1).strip()}"
                        self.sig_progress.emit(pct, f"Downloading {repo_id} ({pct}%){speed_str}")

                self._process.wait()

                if not self.is_running:
                    raise Exception("Task explicitly killed by user (SIGKILL).")
                elif self._process.returncode != 0:
                    raise Exception(f"Download failed with process code {self._process.returncode}.")

            if self.is_running:
                self.sig_progress.emit(100, "All downloads complete.")
                self.sig_finished.emit(True, "Success")

        except Exception as e:
            if self.current_repo:
                self._nuke_cache(self.current_repo)
            reason = "Task aborted and residual cache wiped." if not self.is_running else str(e)
            self.sig_finished.emit(False, reason)


class SettingsTool(BaseTool):
    def __init__(self):
        super().__init__("Global Settings")
        self.config = ConfigManager()
        self.dev_mgr = DeviceManager()
        self.widget = None
        self.llm_configs = []
        self.logger = logging.getLogger("SettingsTool")
        self._is_updating_model_ui = False

        GlobalSignals().request_model_download.connect(self.on_download_requested)

    def get_ui_widget(self) -> QWidget:
        if self.widget: return self.widget
        self.widget = QWidget()
        main_layout = QVBoxLayout(self.widget)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background-color: transparent; }")

        content = QWidget()
        self.layout = QVBoxLayout(content)
        self.layout.setSpacing(20)

        self.init_hardware_section()
        self.init_network_section()
        self.init_model_section()
        self.init_llm_section()
        self.init_ncbi_section()
        self.init_system_section()

        btn_save = QPushButton("💾 Save Settings & Verify Models")
        btn_save.setCursor(Qt.PointingHandCursor)
        btn_save.setStyleSheet("""
            QPushButton { background-color: #007acc; color: white; padding: 12px; font-weight: bold; border-radius: 6px; font-size: 14px; }
            QPushButton:hover { background-color: #0062a3; }
        """)
        btn_save.clicked.connect(self.on_save_clicked)
        self.layout.addWidget(btn_save)
        self.layout.addStretch()

        scroll.setWidget(content)
        main_layout.addWidget(scroll)
        return self.widget

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
        group = QGroupBox("🖥️ System Hardware Info")
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #444; margin-top: 10px; padding-top: 15px; background: #252526; }")
        layout = QVBoxLayout(group)

        info = self.dev_mgr.get_sys_info()
        gpu_str = "<br>".join([f"&nbsp;&nbsp;• {g}" for g in info['gpus']])

        status_color = "#4caf50" if info['cuda_support'] else "#ff9800"
        cuda_status = "Available" if info['cuda_support'] else "Not Available"

        if info['cuda_support']:
            ver_info = f"Toolkit: {info['torch_cuda_ver']} | Driver: {info['gpu_driver_ver']}"
        else:
            ver_info = "N/A"

        html = f"""
        <div style='font-family: Consolas, monospace; font-size: 13px; color: #ddd; line-height: 1.6;'>
            <b>OS:</b> {info['os']}<br>
            <b>Python:</b> {info['python_ver']}<br>
            <b>CPU:</b> {info['cpu']} ({info['cpu_cores']})<br>
            <b>RAM:</b> {info['ram_available']} / {info['ram_total']}<br>
            <b>GPU:</b><br>{gpu_str}<br>
            <b>CUDA:</b> <span style='color:{status_color}'>{cuda_status}</span> ({ver_info})
        </div>
        """
        lbl = QLabel(html)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(lbl)
        self.layout.addWidget(group)

    def init_ncbi_section(self):
        group = QGroupBox("Academic Databases (NCBI & Semantic Scholar)")
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #444; margin-top: 10px; padding-top: 15px; background: #252526; }")
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        self.input_ncbi_email = QLineEdit()
        self.input_ncbi_email.setPlaceholderText("Required: e.g. user@university.edu")
        self.input_ncbi_email.setText(self.config.user_settings.get("ncbi_email", ""))
        self.input_ncbi_email.setStyleSheet("background: #333; color: #fff; border: 1px solid #555; padding: 5px;")

        self.input_ncbi_api_key = QLineEdit()
        self.input_ncbi_api_key.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.input_ncbi_api_key.setPlaceholderText("NCBI Key (Optional but recommended)")
        self.input_ncbi_api_key.setText(self.config.user_settings.get("ncbi_api_key", ""))
        self.input_ncbi_api_key.setStyleSheet("background: #333; color: #fff; border: 1px solid #555; padding: 5px;")

        self.input_s2_api_key = QLineEdit()
        self.input_s2_api_key.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.input_s2_api_key.setPlaceholderText("Semantic Scholar Key (Prevents 429 Errors)")
        self.input_s2_api_key.setText(self.config.user_settings.get("s2_api_key", ""))
        self.input_s2_api_key.setStyleSheet("background: #333; color: #fff; border: 1px solid #555; padding: 5px;")

        lbl_hint = QLabel(
            "💡 <b>Important Notice:</b><br>"
            "• <b>NCBI:</b> API Key increases limits from 3 to 10 requests/sec. "
            "<a href='https://account.ncbi.nlm.nih.gov/settings/' style='color:#05B8CC; text-decoration:none;'>Get NCBI Key</a>.<br>"
            "• <b>Semantic Scholar:</b> Prevents '429 Too Many Requests' errors during deep academic RAG tasks. "
            "<a href='https://www.semanticscholar.org/product/api' style='color:#05B8CC; text-decoration:none;'>Get S2 Key</a>."
        )
        lbl_hint.setWordWrap(True)
        lbl_hint.setOpenExternalLinks(True)
        lbl_hint.setStyleSheet("color: #aaa; font-size: 11px; margin-top: 5px; margin-bottom: 5px; line-height: 1.4;")

        layout.addRow("NCBI Email:", self.input_ncbi_email)
        layout.addRow("NCBI API Key:", self.input_ncbi_api_key)
        layout.addRow("S2 API Key:", self.input_s2_api_key)
        layout.addRow("", lbl_hint)

        self.layout.addWidget(group)

    def init_network_section(self):
        group = QGroupBox("Network & Proxy")
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #444; margin-top: 10px; padding-top: 15px; background: #252526; }")
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        self.combo_proxy_mode = BaseComboBox()
        self.combo_proxy_mode.addItems(["System Proxy (Default)", "No Proxy (Direct)", "Custom Proxy"])

        current_mode = self.config.user_settings.get("proxy_mode", "system")
        mode_map = {"system": 0, "off": 1, "custom": 2}
        self.combo_proxy_mode.setCurrentIndex(mode_map.get(current_mode, 0))

        self.input_proxy = QLineEdit()
        self.input_proxy.setPlaceholderText("e.g. http://127.0.0.1:7890")
        self.input_proxy.setText(self.config.user_settings.get("proxy_url", ""))
        self.input_proxy.setStyleSheet("background: #333; color: #fff; border: 1px solid #555; padding: 5px;")

        self.input_mirror = QLineEdit()
        self.input_mirror.setPlaceholderText("Leave empty for default (huggingface.co)")
        self.input_mirror.setText(self.config.user_settings.get("hf_mirror", ""))
        self.input_mirror.setStyleSheet("background: #333; color: #fff; border: 1px solid #555; padding: 5px;")

        self.combo_proxy_mode.currentIndexChanged.connect(self._on_proxy_mode_changed)
        self._on_proxy_mode_changed(self.combo_proxy_mode.currentIndex())

        layout.addRow("Proxy Mode:", self.combo_proxy_mode)
        layout.addRow("Proxy URL:", self.input_proxy)
        layout.addRow("HF Mirror:", self.input_mirror)

        self.layout.addWidget(group)

    def _on_proxy_mode_changed(self, index):
        is_custom = (index == 2)
        self.input_proxy.setEnabled(is_custom)
        if not is_custom:
            self.input_proxy.setStyleSheet("background: #222; color: #666; border: 1px solid #444; padding: 5px;")
        else:
            self.input_proxy.setStyleSheet("background: #333; color: #fff; border: 1px solid #555; padding: 5px;")

    def init_model_section(self):
        group = QGroupBox("🧠 AI Models Configuration")
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #444; margin-top: 10px; padding-top: 15px; background: #252526; }")
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        self.combo_embed = BaseComboBox()
        self.lbl_embed_status = QLabel("Checking...")
        self.lbl_embed_status.setTextFormat(Qt.RichText)
        self.lbl_embed_status.setWordWrap(True)
        self.lbl_embed_status.setStyleSheet("color: #888; font-size: 11px; margin-bottom: 5px;")

        for m in EMBEDDING_MODELS:
            self.combo_embed.addItem(m['ui_name'], m['id'])

        curr_embed = self.config.user_settings.get("current_model_id", "embed_auto")
        idx = self.combo_embed.findData(curr_embed)
        self.combo_embed.setCurrentIndex(max(0, idx))
        self.combo_embed.currentIndexChanged.connect(self.check_models_status)

        self.combo_rerank = BaseComboBox()
        self.lbl_rerank_status = QLabel("Checking...")
        self.lbl_rerank_status.setTextFormat(Qt.RichText)
        self.lbl_rerank_status.setWordWrap(True)
        self.lbl_rerank_status.setStyleSheet("color: #888; font-size: 11px; margin-bottom: 5px;")

        for m in RERANKER_MODELS:
            self.combo_rerank.addItem(m['ui_name'], m['id'])

        curr_rerank = self.config.user_settings.get("rerank_model_id", "rerank_auto")
        idx = self.combo_rerank.findData(curr_rerank)
        self.combo_rerank.setCurrentIndex(max(0, idx))
        self.combo_rerank.currentIndexChanged.connect(self.check_models_status)

        layout.addRow("Embedding:", self.combo_embed)
        layout.addRow("", self.lbl_embed_status)
        layout.addRow("Reranker:", self.combo_rerank)
        layout.addRow("", self.lbl_rerank_status)

        self.layout.addWidget(group)
        QThread.msleep(50)
        self.check_models_status()

    def _load_llm_config(self):
        config_path = os.path.join(os.getcwd(), "config", "llm_config.json")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        default_config = [
            {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1", "model_name": "gpt-4o",
             "api_key": ""},
            {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
             "model_name": "deepseek-chat", "api_key": ""},
            {"id": "gemini", "name": "Google Gemini",
             "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model_name": "gemini-3-pro",
             "api_key": ""},
            {"id": "anthropic", "name": "Anthropic", "base_url": "https://api.anthropic.com/v1",
             "model_name": "claude-3-5-sonnet-latest", "api_key": ""},
            {"id": "nvidia", "name": "Nvidia Build", "base_url": "https://integrate.api.nvidia.com/v1",
             "model_name": "meta/llama-3.1-70b-instruct", "api_key": ""},
            {"id": "qwen", "name": "Alibaba Qwen", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "model_name": "qwen-plus", "api_key": ""},
            {"id": "zhipu", "name": "Zhipu GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
             "model_name": "glm-4-plus", "api_key": ""},
            {"id": "siliconflow", "name": "SiliconFlow", "base_url": "https://api.siliconflow.cn/v1",
             "model_name": "deepseek-ai/DeepSeek-V3", "api_key": ""},
            {"id": "custom", "name": "Local Custom (Ollama)", "base_url": "http://localhost:11434/v1",
             "model_name": "llama3", "api_key": "ollama"}
        ]

        loaded_configs = []
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    loaded_configs = json.load(f)
                    for cfg in loaded_configs:
                        cfg.pop("thinking_model_name", None)

                        # Migrate old provider-level params to model-specific structure
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

        if needs_resave or not os.path.exists(config_path):
            try:
                with open(config_path, 'w', encoding='utf-8') as f:
                    json.dump(loaded_configs, f, indent=4)
            except Exception:
                pass

        return loaded_configs if loaded_configs else default_config

    def _save_llm_config(self):
        config_path = os.path.join(os.getcwd(), "config", "llm_config.json")
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(self.llm_configs, f, indent=4)
        except Exception as e:
            self.logger.error(f"Error saving llm_config.json: {e}")

    def init_llm_section(self):
        from src.ui.components.param_editor import ParamEditorWidget
        from PySide6.QtWidgets import QFrame

        group = QGroupBox("💬 LLM Generation API")
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #444; margin-top: 10px; padding-top: 15px; background: #252526; }")
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        self.llm_configs = self._load_llm_config()

        header_layout = QHBoxLayout()
        self.combo_llm_preset = BaseComboBox()
        for conf in self.llm_configs:
            self.combo_llm_preset.addItem(conf.get("name", "Unnamed Provider"))

        self.btn_add_llm = QPushButton("➕ Add")
        self.btn_add_llm.clicked.connect(self._add_llm_provider)
        self.btn_del_llm = QPushButton("🗑️ Delete")
        self.btn_del_llm.clicked.connect(self._del_llm_provider)

        btn_help_params = QPushButton("❓ Parameter Help")
        btn_help_params.clicked.connect(lambda: StandardDialog(
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
        header_layout.addWidget(btn_help_params)

        self.input_llm_name = QLineEdit()
        self.input_llm_url = QLineEdit()
        self.input_llm_key = QLineEdit()
        self.input_llm_key.setEchoMode(QLineEdit.Password)
        for inp in (self.input_llm_name, self.input_llm_url, self.input_llm_key):
            inp.setStyleSheet("background: #333; color: #fff; border: 1px solid #555; padding: 5px;")

        self.editor_provider_params = ParamEditorWidget()
        btn_add_provider_param = QPushButton("➕ Add Provider Parameter")
        btn_add_provider_param.clicked.connect(lambda: self.editor_provider_params.add_param_row())

        provider_param_layout = QVBoxLayout()
        provider_param_layout.addWidget(self.editor_provider_params)
        provider_param_layout.addWidget(btn_add_provider_param)

        model_layout = QHBoxLayout()
        self.combo_llm_model = QComboBox()
        self.combo_llm_model = BaseComboBox()  # 替换为不可编辑的下拉框
        self.combo_llm_model.setStyleSheet(
            "background: #333; color: #fff; border: 1px solid #555; padding: 4px; selection-background-color: #007acc;")

        self.btn_add_model = QPushButton("➕ Add")
        self.btn_add_model.clicked.connect(self._add_llm_model)
        self.btn_del_model = QPushButton("🗑️ Delete")
        self.btn_del_model.clicked.connect(self._del_llm_model)

        self.btn_fetch_models = QPushButton("🔄 Fetch")
        self.btn_fetch_models.clicked.connect(self._start_fetch_task)
        self.btn_test_api = QPushButton("🧪 Test")
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
        btn_add_model_param = QPushButton("➕ Add Model Parameter")
        btn_add_model_param.clicked.connect(lambda: self.editor_model_params.add_param_row())

        btn_copy_params = QPushButton("📥 Copy from Provider")
        btn_copy_params.setToolTip("Copies global provider parameters to the current model.")
        btn_copy_params.clicked.connect(self._on_copy_params_clicked)

        model_param_btn_layout.addWidget(btn_add_model_param)
        model_param_btn_layout.addWidget(btn_copy_params)

        self.model_param_container = QWidget()
        mp_layout = QVBoxLayout(self.model_param_container)
        mp_layout.setContentsMargins(0, 0, 0, 0)
        mp_layout.addWidget(self.editor_model_params)
        mp_layout.addLayout(model_param_btn_layout)

        self.combo_model_param_strategy.currentIndexChanged.connect(
            lambda idx: self.model_param_container.setVisible(idx == 1)
        )

        trans_group = QGroupBox("🌐 Translation Agent Configuration")
        trans_group.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #444; margin-top: 10px; padding-top: 15px; background: #252526; }")
        trans_layout = QFormLayout(trans_group)
        self.combo_trans_preset = BaseComboBox()
        self.combo_trans_preset.addItem("❌ None (Disable)", None)
        for conf in self.llm_configs:
            self.combo_trans_preset.addItem(conf.get("name", "Unnamed Provider"), conf.get("id"))
        trans_layout.addRow("Translation Provider:", self.combo_trans_preset)

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
        idx_trans = self.combo_trans_preset.findData(trans_id)
        if idx_trans >= 0:
            self.combo_trans_preset.setCurrentIndex(idx_trans)

    def _extract_real_model_name(self, display_text):
        for suffix in [" (⚙️ Custom)", " (🚫 Closed)"]:
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

                # 使用自定义的 StandardDialog 解决黑暗主题适配问题
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
        from src.ui.components.dialog import BaseDialog
        from PySide6.QtWidgets import QLineEdit

        class AddModelDialog(BaseDialog):
            def __init__(self, parent=None):
                super().__init__(parent, title="Add Custom Model", width=350)
                self.inp_name = QLineEdit()
                self.inp_name.setPlaceholderText("Enter model ID/name...")
                self.inp_name.setStyleSheet("background: #333; color: #fff; border: 1px solid #555; padding: 5px;")
                self.content_layout.addWidget(self.inp_name)
                self.add_button("Cancel", self.reject)
                self.add_button("Add", self.accept, is_primary=True)

            def get_name(self):
                return self.inp_name.text().strip()

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

                    # 添加完毕后自动选中该模型
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

                # 从内存中剔除数据
                if "fetched_models" in conf and real_name in conf["fetched_models"]:
                    conf["fetched_models"].remove(real_name)
                if "models_config" in conf and real_name in conf["models_config"]:
                    del conf["models_config"][real_name]

                # 如果刚好删除了正在使用的模型，兜底更新回溯配置
                if conf.get("model_name") == real_name:
                    conf["model_name"] = conf["fetched_models"][0] if conf.get("fetched_models") else ""

                self._refresh_model_combo(conf)

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

        curr_text = self.combo_llm_model.currentText().strip()
        curr_real = self._extract_real_model_name(curr_text)

        self.combo_llm_model.blockSignals(True)
        self.combo_llm_model.clear()

        fetched = conf.get("fetched_models", [])
        models_config = conf.get("models_config", {})

        items_to_add = []
        if curr_real and curr_real not in fetched:
            fetched.insert(0, curr_real)

        for m in fetched:
            mode = models_config.get(m, {}).get("mode", "inherit")
            if mode == "custom":
                items_to_add.append(f"{m} (⚙️ Custom)")
            elif mode == "closed":
                items_to_add.append(f"{m} (🚫 Closed)")
            else:
                items_to_add.append(m)

        self.combo_llm_model.addItems(items_to_add)

        idx_to_select = -1
        for i in range(self.combo_llm_model.count()):
            if self._extract_real_model_name(self.combo_llm_model.itemText(i)) == curr_real:
                idx_to_select = i
                break

        if idx_to_select >= 0:
            self.combo_llm_model.setCurrentIndex(idx_to_select)
        else:
            self.combo_llm_model.setCurrentText(curr_text)

        self.combo_llm_model.blockSignals(False)
        self._is_updating_model_ui = False

        self._load_model_params_to_ui(conf, curr_real)

    def _update_current_model_marker(self, real_name, mode):
        self.combo_llm_model.blockSignals(True)
        marker = ""
        if mode == "custom":
            marker = " (⚙️ Custom)"
        elif mode == "closed":
            marker = " (🚫 Closed)"

        new_text = f"{real_name}{marker}"

        idx = self.combo_llm_model.currentIndex()
        if idx >= 0 and self._extract_real_model_name(self.combo_llm_model.itemText(idx)) == real_name:
            self.combo_llm_model.setItemText(idx, new_text)
        elif self.combo_llm_model.currentText() != new_text:
            self.combo_llm_model.setCurrentText(new_text)

        self.combo_llm_model.blockSignals(False)

    def _on_llm_preset_changed(self, index):
        if index < 0 or index >= len(self.llm_configs): return
        conf = self.llm_configs[index]

        self.input_llm_name.blockSignals(True)
        self.input_llm_url.blockSignals(True)
        self.input_llm_key.blockSignals(True)

        self.input_llm_name.setText(conf.get("name", ""))
        self.input_llm_url.setText(conf.get("base_url", ""))
        self.input_llm_key.setText(conf.get("api_key", ""))

        self.input_llm_name.blockSignals(False)
        self.input_llm_url.blockSignals(False)
        self.input_llm_key.blockSignals(False)

        self.editor_provider_params.blockSignals(True)
        self.editor_provider_params.load_data(conf.get("provider_params", []))
        self.editor_provider_params.blockSignals(False)

        if "model_name" in conf and conf["model_name"]:
            self.combo_llm_model.blockSignals(True)
            self.combo_llm_model.setCurrentText(conf["model_name"])
            self.combo_llm_model.blockSignals(False)

        self._refresh_model_combo(conf)

        default_ids = ["openai", "deepseek", "gemini", "anthropic", "nvidia", "qwen", "zhipu", "siliconflow", "custom"]
        self.btn_del_llm.setEnabled(conf.get("id") not in default_ids)

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

        self._update_current_model_marker(curr_real, mode)

    def _start_fetch_task(self):
        self._sync_llm_data()
        idx = self.combo_llm_preset.currentIndex()
        conf = self.llm_configs[idx] if 0 <= idx < len(self.llm_configs) else {}

        base_url = conf.get("base_url", "").strip()
        api_key = conf.get("api_key", "").strip()

        if not base_url:
            StandardDialog(self.widget, "Warning", "Please enter API Base URL first.").exec()
            return

        self.net_pd = ProgressDialog(
            self.widget, "Network Request", "Contacting API...\n(You can cancel at any time)",
            telemetry_config={"cpu": False, "ram": False, "gpu": False, "net": False, "io": False}
        )
        self.net_pd.show()

        self.net_thread = QThread()
        self.net_worker = LightNetworkWorker()
        self.net_worker.moveToThread(self.net_thread)
        self.net_pd.sig_canceled.connect(self.net_worker.cancel)

        self.net_worker.base_url = base_url
        self.net_worker.api_key = api_key
        self.net_thread.started.connect(self.net_worker.do_fetch_models)
        self.net_worker.sig_models_fetched.connect(self._on_models_fetched)

        self.net_worker.sig_models_fetched.connect(self.net_thread.quit)
        self.net_worker.sig_models_fetched.connect(self.net_worker.deleteLater)
        self.net_thread.finished.connect(self.net_thread.deleteLater)

        self.net_thread.start()

    def _on_models_fetched(self, success, models, msg):
        self.net_pd.close_safe()

        if success:
            self.logger.info(f"🔄 Successfully fetched {len(models)} models from API.")
            idx = self.combo_llm_preset.currentIndex()
            if 0 <= idx < len(self.llm_configs):
                conf = self.llm_configs[idx]
                conf["fetched_models"] = models
                self._refresh_model_combo(conf)
            StandardDialog(self.widget, "Success", msg).exec()
        else:
            self.logger.warning(f"⚠️ Failed to fetch models: {msg}")
            ToastManager().show(f"Fetch Models Failed: {msg}", "error")
            StandardDialog(self.widget, "Information", msg).exec()

    def _start_test_task(self):
        self._sync_llm_data()
        idx = self.combo_llm_preset.currentIndex()
        conf = self.llm_configs[idx] if 0 <= idx < len(self.llm_configs) else {}

        base_url = conf.get("base_url", "").strip()
        api_key = conf.get("api_key", "").strip()
        model_name = self._extract_real_model_name(self.combo_llm_model.currentText().strip())

        models_config = conf.get("models_config", {})
        current_model_conf = models_config.get(model_name, {})

        param_mode = current_model_conf.get("mode", conf.get("model_params_mode", "inherit"))
        custom_params_list = []

        if param_mode == "inherit":
            custom_params_list = conf.get("provider_params", [])
        elif param_mode == "custom":
            custom_params_list = current_model_conf.get("params", conf.get("model_params", []))

        parsed_params = {}
        for p in custom_params_list:
            name = p.get("name", "").strip()
            if not name: continue
            val_str = str(p.get("value", ""))
            ptype = p.get("type", "str")
            try:
                if ptype == "int":
                    parsed_params[name] = int(val_str)
                elif ptype == "float":
                    parsed_params[name] = float(val_str)
                elif ptype == "bool":
                    parsed_params[name] = val_str.lower() in ['true', '1', 'yes', 'on']
                else:
                    parsed_params[name] = val_str
            except ValueError:
                self.logger.warning(f"Test Task: Skipped invalid param {name}")

        if not base_url or not model_name:
            StandardDialog(self.widget, "Warning", "Please ensure Base URL and Model Name are provided.").exec()
            return

        self.net_pd = ProgressDialog(
            self.widget, "API Connection Test",
            f"Sending test prompt to '{model_name}'...\n(You can cancel at any time)",
            telemetry_config={"cpu": False, "ram": False, "gpu": False, "net": False, "io": False}
        )
        self.net_pd.show()

        self.test_thread = QThread()
        self.test_worker = LightNetworkWorker()
        self.test_worker.moveToThread(self.test_thread)
        self.net_pd.sig_canceled.connect(self.test_worker.cancel)

        self.test_worker.base_url = base_url
        self.test_worker.api_key = api_key
        self.test_worker.model_name = model_name
        self.test_worker.custom_params = parsed_params

        self.test_thread.started.connect(self.test_worker.do_test_api)
        self.test_worker.sig_test_finished.connect(self._on_test_finished)

        self.test_worker.sig_test_finished.connect(self.test_thread.quit)
        self.test_worker.sig_test_finished.connect(self.test_worker.deleteLater)
        self.test_thread.finished.connect(self.test_thread.deleteLater)

        self.test_thread.start()

    def _on_test_finished(self, success, msg):
        self.net_pd.close_safe()
        if success:
            self.logger.info("✅ API Connection Test Passed.")
            StandardDialog(self.widget, "Test Passed", msg).exec()
        else:
            self.logger.error(f"❌ API Connection Test Failed: {msg}")
            ToastManager().show(f"API Test Failed: {msg}", "error")
            StandardDialog(self.widget, "Test Failed", msg).exec()

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
            QMessageBox.warning(self.widget, "Warning", "Built-in default providers cannot be deleted.")
            return

        del self.llm_configs[idx]
        self.combo_llm_preset.removeItem(idx)

    def init_system_section(self):
        group = QGroupBox("System Preferences")
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #444; margin-top: 10px; padding-top: 15px; background: #252526; }")
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
        self.input_ext_python.setStyleSheet("background: #333; color: #fff; border: 1px solid #555; padding: 5px;")

        layout.addRow("Theme:", self.combo_theme)
        layout.addRow("Log Level:", self.combo_log)
        layout.addRow("External Python Path:", self.input_ext_python)
        self.layout.addWidget(group)

    def _get_req_html(self, conf):
        if not conf or 'recommended_config' not in conf:
            return ""
        rc = conf['recommended_config']
        prio = rc.get('device_priority', 'Unknown')
        vram = rc.get('min_vram', 'N/A')
        ram = rc.get('min_ram', 'N/A')

        prio_color = "#ffb86c" if "High-End" in prio or "Required" in prio else "#888"

        return f"""
        <div style='margin-top:4px; font-family:Consolas; font-size:10px; color:#aaa;'>
           👉 <span style='color:{prio_color}; font-weight:bold;'>[{prio}]</span> 
           | VRAM: <span style='color:#ccc'>{vram}</span> 
           | RAM: <span style='color:#ccc'>{ram}</span>
        </div>
        """

    def check_models_status(self):
        embed_id = self.combo_embed.currentData()
        real_embed = embed_id
        is_auto = (embed_id == "embed_auto")

        if is_auto:
            real_embed = resolve_auto_model("embedding", self.dev_mgr.get_optimal_device())

        embed_conf = get_model_conf(real_embed, "embedding")
        repo_id = embed_conf.get('hf_repo_id') if embed_conf else "Unknown"
        req_html = self._get_req_html(embed_conf)

        exists = check_model_exists(repo_id)
        msg = f"Target: {real_embed}" if is_auto else f"Repo: {repo_id}"
        if exists:
            self.lbl_embed_status.setText(f"✅ Ready | {msg}{req_html}")
        else:
            self.lbl_embed_status.setText(f"❌ Not Found | {msg} (Will Download){req_html}")

        rerank_id = self.combo_rerank.currentData()
        real_rerank = rerank_id
        is_auto_r = (rerank_id == "rerank_auto")

        if is_auto_r:
            real_rerank = resolve_auto_model("reranker", self.dev_mgr.get_optimal_device())

        rerank_conf = get_model_conf(real_rerank, "reranker")
        repo_id_r = rerank_conf.get('hf_repo_id') if rerank_conf else "Unknown"
        req_html_r = self._get_req_html(rerank_conf)

        exists_r = check_model_exists(repo_id_r)
        msg_r = f"Target: {real_rerank}" if is_auto_r else f"Repo: {repo_id_r}"
        if exists_r:
            self.lbl_rerank_status.setText(f"✅ Ready | {msg_r}{req_html_r}")
        else:
            self.lbl_rerank_status.setText(f"❌ Not Found | {msg_r} (Will Download){req_html_r}")

    def on_save_clicked(self):
        self.widget.setFocus()
        if hasattr(self, '_sync_llm_data'):
            self._sync_llm_data()

        self._save_llm_config()

        old_email = self.config.user_settings.get("ncbi_email", "")
        old_key = self.config.user_settings.get("ncbi_api_key", "")
        old_s2_key = self.config.user_settings.get("s2_api_key", "")
        old_proxy_mode = self.config.user_settings.get("proxy_mode", "system")
        old_proxy_url = self.config.user_settings.get("proxy_url", "")

        new_email = self.input_ncbi_email.text().strip()
        new_key = self.input_ncbi_api_key.text().strip()
        new_s2_key = self.input_s2_api_key.text().strip()

        mode_idx = self.combo_proxy_mode.currentIndex()
        new_proxy_mode = ["system", "off", "custom"][mode_idx]
        new_proxy_url = self.input_proxy.text().strip()

        needs_mcp_restart = (
                (old_email != new_email) or
                (old_key != new_key) or
                (old_s2_key != new_s2_key) or
                (old_proxy_mode != new_proxy_mode) or
                (old_proxy_url != new_proxy_url)
        )

        self.config.user_settings.update({
            "proxy_mode": new_proxy_mode,
            "proxy_url": new_proxy_url,
            "hf_mirror": self.input_mirror.text().strip(),
            "download_speed_limit": self.combo_embed.currentText(),
            "current_model_id": self.combo_embed.currentData(),
            "rerank_model_id": self.combo_rerank.currentData(),
            "active_llm_id": self._get_active_llm_id(),
            "trans_llm_id": self.combo_trans_preset.currentData(),
            "theme": self.combo_theme.currentText(),
            "log_level": self.combo_log.currentText(),
            "ncbi_email": new_email,
            "ncbi_api_key": new_key,
            "s2_api_key": new_s2_key,
            "external_python_path": self.input_ext_python.text().strip()
        })
        self.config.save_settings()
        self.logger.info("Configuration saved successfully.")

        setup_global_network_env()
        os.environ["NCBI_API_EMAIL"] = new_email
        os.environ["NCBI_API_KEY"] = new_key
        os.environ["S2_API_KEY"] = new_s2_key

        if hasattr(GlobalSignals(), 'llm_config_changed'):
            GlobalSignals().llm_config_changed.emit()

        qdarktheme.setup_theme(self.combo_theme.currentText().lower())
        logging.getLogger().setLevel(getattr(logging, self.combo_log.currentText()))

        if needs_mcp_restart:
            from src.core.mcp_manager import MCPManager
            from PySide6.QtWidgets import QApplication

            pd = ProgressDialog(self.widget, "Restarting Plugin",
                                "Applying new network and API settings to NCBI Plugin...", telemetry_config={})
            pd.btn_cancel.setVisible(False)
            pd.show()
            QApplication.processEvents()

            try:
                MCPManager.get_instance().restart_sync()
            except Exception as e:
                self.logger.error(f"Failed to hot-restart MCP server: {e}")
            finally:
                pd.close_safe()

        dev = self.dev_mgr.get_optimal_device()
        embed_id = self.combo_embed.currentData()
        if embed_id == "embed_auto": embed_id = resolve_auto_model("embedding", dev)

        rerank_id = self.combo_rerank.currentData()
        if rerank_id == "rerank_auto": rerank_id = resolve_auto_model("reranker", dev)

        to_download = []
        e_conf = get_model_conf(embed_id, "embedding")
        if e_conf and not check_model_exists(e_conf.get('hf_repo_id')):
            to_download.append(e_conf['hf_repo_id'])

        r_conf = get_model_conf(rerank_id, "reranker")
        if r_conf and not check_model_exists(r_conf.get('hf_repo_id')):
            to_download.append(r_conf['hf_repo_id'])

        if not to_download:
            StandardDialog(self.widget, "Success", "Settings saved. All models and LLM APIs are ready.").exec()
            self.check_models_status()
            return

        msg = "The following models need to be downloaded:\n"
        for m in to_download: msg += f"• {m}\n"

        dlg = StandardDialog(self.widget, "Download Required", msg, show_cancel=True)
        if dlg.exec():
            self.start_download(to_download)

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
            self.pd.lbl_message.setText(f"❌ Failed to download {self.current_repo}:\n{msg}")
            self.pd.btn_cancel.setText("Close")
            self.pd.btn_cancel.setEnabled(True)