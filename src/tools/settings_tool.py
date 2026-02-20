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
                               QScrollArea, QHBoxLayout, QComboBox)
from PySide6.QtCore import Qt, QThread, Signal, QObject, QTimer

from src.core.core_task import TaskState, TaskManager
from src.core.device_manager import DeviceManager
from src.core.models_registry import (EMBEDDING_MODELS, RERANKER_MODELS,
                                      get_model_conf, check_model_exists, resolve_auto_model)
from src.core.network_worker import setup_global_network_env
from src.core.signals import GlobalSignals
from src.tools.base_tool import BaseTool
from src.core.config_manager import ConfigManager
from src.task.hf_download_task import RealTimeHFDownloadTask
from src.ui.components.combo import BaseComboBox
from src.ui.components.dialog import ProgressDialog, StandardDialog


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
        hf_home = os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
        folder_name = "models--" + repo_id.replace("/", "--")
        target_dir = os.path.join(hf_home, "hub", folder_name)

        if os.path.exists(target_dir):
            try:
                shutil.rmtree(target_dir)
                print(f"🧹 Cache strictly nuked: {target_dir}")
            except Exception as e:
                print(f"⚠️ Failed to wipe cache: {e}")

    def run(self):
        try:
            env = os.environ.copy()
            env["HF_HOME"] = env.get("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
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

        # 1. 硬件详情
        self.init_hardware_section()

        # 2. 网络设置
        self.init_network_section()

        # 3. 模型管理 (Embedding & Reranker)
        self.init_model_section()

        # 🌟 4. LLM API 配置 (动态 JSON 管理)
        self.init_llm_section()

        # 5. 系统设置
        self.init_system_section()

        # Save Button
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

        # 构建显卡版本详情字符串
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

    def init_network_section(self):
        group = QGroupBox("Network & Proxy")
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #444; margin-top: 10px; padding-top: 15px; background: #252526; }")
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        # 🌟 新增：代理模式选择
        self.combo_proxy_mode = BaseComboBox()
        self.combo_proxy_mode.addItems(["System Proxy (Default)", "No Proxy (Direct)", "Custom Proxy"])

        # 映射配置字符串到 Index
        current_mode = self.config.user_settings.get("proxy_mode", "system")
        mode_map = {"system": 0, "off": 1, "custom": 2}
        self.combo_proxy_mode.setCurrentIndex(mode_map.get(current_mode, 0))

        # 代理地址输入框
        self.input_proxy = QLineEdit()
        self.input_proxy.setPlaceholderText("e.g. http://127.0.0.1:7890")
        self.input_proxy.setText(self.config.user_settings.get("proxy_url", ""))
        self.input_proxy.setStyleSheet("background: #333; color: #fff; border: 1px solid #555; padding: 5px;")

        # 镜像地址
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


    # =========================================================================
    # 🌟 LLM API 动态配置模块
    # =========================================================================
    def _load_llm_config(self):
        config_path = os.path.join(os.getcwd(), "config", "llm_config.json")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # 🚀 2026年最新默认配置池
        default_config = [
            {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1", "model_name": "gpt-4o", "thinking_model_name": "o3", "api_key": ""},
            {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model_name": "deepseek-chat", "thinking_model_name": "deepseek-reasoner", "api_key": ""},
            {"id": "gemini", "name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model_name": "gemini-3-pro", "thinking_model_name": "gemini-3-pro-thinking", "api_key": ""},
            {"id": "anthropic", "name": "Anthropic", "base_url": "https://api.anthropic.com/v1", "model_name": "claude-3-5-sonnet-latest", "thinking_model_name": "claude-3-7-sonnet-thinking", "api_key": ""},
            {"id": "nvidia", "name": "Nvidia Build", "base_url": "https://integrate.api.nvidia.com/v1", "model_name": "meta/llama-3.1-70b-instruct", "thinking_model_name": "", "api_key": ""},
            {"id": "qwen", "name": "Alibaba Qwen", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model_name": "qwen-plus", "thinking_model_name": "qwen-max", "api_key": ""},
            {"id": "zhipu", "name": "Zhipu GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model_name": "glm-4-plus", "thinking_model_name": "glm-4-long", "api_key": ""},
            {"id": "siliconflow", "name": "SiliconFlow (硅基流动)", "base_url": "https://api.siliconflow.cn/v1", "model_name": "deepseek-ai/DeepSeek-V3", "thinking_model_name": "deepseek-ai/DeepSeek-R1", "api_key": ""},
            {"id": "custom", "name": "Local Custom (Ollama)", "base_url": "http://localhost:11434/v1", "model_name": "llama3", "thinking_model_name": "", "api_key": "ollama"}
        ]

        loaded_configs = []
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    loaded_configs = json.load(f)
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
        """将当前内存中的 LLM 配置列表写入 llm_config.json"""
        config_path = os.path.join(os.getcwd(), "config", "llm_config.json")
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(self.llm_configs, f, indent=4)
        except Exception as e:
            self.logger.error(f"Error saving llm_config.json: {e}")

    def init_llm_section(self):
        group = QGroupBox("💬 LLM Generation API")
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #444; margin-top: 10px; padding-top: 15px; background: #252526; }")
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        self.llm_configs = self._load_llm_config()

        # --- 服务商选择栏 ---
        header_layout = QHBoxLayout()
        self.combo_llm_preset = BaseComboBox()
        for conf in self.llm_configs:
            self.combo_llm_preset.addItem(conf.get("name", "Unnamed Provider"))

        self.btn_add_llm = QPushButton("➕ Add")
        self.btn_add_llm.clicked.connect(self._add_llm_provider)
        self.btn_del_llm = QPushButton("🗑️ Delete")
        self.btn_del_llm.clicked.connect(self._del_llm_provider)

        header_layout.addWidget(self.combo_llm_preset, stretch=1)
        header_layout.addWidget(self.btn_add_llm)
        header_layout.addWidget(self.btn_del_llm)

        # --- 基本信息输入栏 ---
        self.input_llm_name = QLineEdit()
        self.input_llm_name.setStyleSheet("background: #333; color: #fff; border: 1px solid #555; padding: 5px;")

        self.input_llm_url = QLineEdit()
        self.input_llm_url.setStyleSheet("background: #333; color: #fff; border: 1px solid #555; padding: 5px;")

        self.input_llm_key = QLineEdit()
        self.input_llm_key.setEchoMode(QLineEdit.Password)
        self.input_llm_key.setStyleSheet("background: #333; color: #fff; border: 1px solid #555; padding: 5px;")

        # --- 🚀 混合下拉输入栏：Standard Model ---
        model_layout = QHBoxLayout()
        self.combo_llm_model = QComboBox()  # 使用标准 QComboBox 以支持原生 Editable
        self.combo_llm_model.setEditable(True)
        self.combo_llm_model.setStyleSheet(
            "background: #333; color: #fff; border: 1px solid #555; padding: 4px; selection-background-color: #007acc;")

        self.btn_fetch_models = QPushButton("🔄 Fetch Models")
        self.btn_fetch_models.setToolTip("Refresh available models from the API")
        self.btn_fetch_models.clicked.connect(self._start_fetch_task)

        model_layout.addWidget(self.combo_llm_model, stretch=1)
        model_layout.addWidget(self.btn_fetch_models)

        # --- 🚀 混合下拉输入栏：Thinking Model ---
        think_layout = QHBoxLayout()
        self.combo_llm_think = QComboBox()
        self.combo_llm_think.setEditable(True)
        self.combo_llm_think.setStyleSheet(
            "background: #333; color: #fff; border: 1px solid #555; padding: 4px; selection-background-color: #007acc;")

        self.btn_test_api = QPushButton("🧪 Test Connection")
        self.btn_test_api.setToolTip("Test if the current API key and Model are working")
        self.btn_test_api.clicked.connect(self._start_test_task)

        think_layout.addWidget(self.combo_llm_think, stretch=1)
        think_layout.addWidget(self.btn_test_api)

        # --- 提示语 ---
        lbl_think_hint = QLabel(
            "💡 <b>Tip:</b> Using advanced reasoning models (e.g., <i>DeepSeek-R1, OpenAI o3, Gemini 3 Pro Thinking</i>) "
            "significantly improves RAG accuracy and reduces hallucinations, but will heavily increase token usage and response time."
        )
        lbl_think_hint.setWordWrap(True)
        lbl_think_hint.setStyleSheet("color: #888888; font-size: 11px; margin-top: 2px; margin-bottom: 8px;")

        # --- 挂载到表单 ---
        layout.addRow("Service Provider:", header_layout)
        layout.addRow("Provider Name:", self.input_llm_name)
        layout.addRow("API Base URL:", self.input_llm_url)
        layout.addRow("API Key:", self.input_llm_key)
        layout.addRow("Standard Model:", model_layout)
        layout.addRow("Thinking Model:", think_layout)
        layout.addRow("", lbl_think_hint)

        self.layout.addWidget(group)

        # --- 信号绑定 ---
        self.combo_llm_preset.currentIndexChanged.connect(self._on_llm_preset_changed)
        self.input_llm_name.textChanged.connect(self._sync_llm_data)
        self.input_llm_url.textChanged.connect(self._sync_llm_data)
        self.input_llm_key.textChanged.connect(self._sync_llm_data)
        self.combo_llm_model.editTextChanged.connect(self._sync_llm_data)
        self.combo_llm_think.editTextChanged.connect(self._sync_llm_data)

        # --- 回显选择 ---
        active_id = self.config.user_settings.get("active_llm_id", "openai")
        idx_to_select = 0
        for i, c in enumerate(self.llm_configs):
            if c.get("id") == active_id:
                idx_to_select = i
                break

        self.combo_llm_preset.setCurrentIndex(idx_to_select)
        self._on_llm_preset_changed(idx_to_select)

    def _on_llm_preset_changed(self, index):
        if index < 0 or index >= len(self.llm_configs): return
        conf = self.llm_configs[index]

        # 阻断信号，防止同步覆盖
        self.input_llm_name.blockSignals(True)
        self.input_llm_url.blockSignals(True)
        self.input_llm_key.blockSignals(True)
        self.combo_llm_model.blockSignals(True)
        self.combo_llm_think.blockSignals(True)

        self.input_llm_name.setText(conf.get("name", ""))
        self.input_llm_url.setText(conf.get("base_url", ""))
        self.input_llm_key.setText(conf.get("api_key", ""))

        # 处理可编辑下拉框的文字
        self.combo_llm_model.setCurrentText(conf.get("model_name", ""))
        self.combo_llm_think.setCurrentText(conf.get("thinking_model_name", ""))

        self.input_llm_name.blockSignals(False)
        self.input_llm_url.blockSignals(False)
        self.input_llm_key.blockSignals(False)
        self.combo_llm_model.blockSignals(False)
        self.combo_llm_think.blockSignals(False)

        # 默认服务商保护
        default_ids = ["openai", "deepseek", "gemini", "anthropic", "nvidia", "qwen", "zhipu", "siliconflow", "custom"]
        is_default = conf.get("id") in default_ids
        self.btn_del_llm.setEnabled(not is_default)


    def _sync_llm_data(self):
        idx = self.combo_llm_preset.currentIndex()
        if idx < 0 or idx >= len(self.llm_configs): return

        self.llm_configs[idx]["name"] = self.input_llm_name.text().strip()
        self.llm_configs[idx]["base_url"] = self.input_llm_url.text().strip()
        self.llm_configs[idx]["api_key"] = self.input_llm_key.text().strip()
        self.llm_configs[idx]["model_name"] = self.combo_llm_model.currentText().strip()
        self.llm_configs[idx]["thinking_model_name"] = self.combo_llm_think.currentText().strip()

        self.combo_llm_preset.blockSignals(True)
        self.combo_llm_preset.setItemText(idx, self.llm_configs[idx]["name"])
        self.combo_llm_preset.blockSignals(False)

    def _start_fetch_task(self):
        base_url = self.input_llm_url.text().strip()
        api_key = self.input_llm_key.text().strip()
        if not base_url:
            StandardDialog(self.widget, "Warning", "Please enter API Base URL first.").exec()
            return

        # 启动纯 UI 进度的对话框
        self.net_pd = ProgressDialog(
            self.widget, "Network Request", "Contacting API...\n(You can cancel at any time)",
            telemetry_config={"cpu": False, "ram": False, "gpu": False, "net": False, "io": False}
        )
        self.net_pd.show()

        from PySide6.QtCore import QThread
        from src.core.network_worker import LightNetworkWorker

        # 🚀 采用全新的轻量级 QThread 架构，不再使用 TaskManager
        self.net_thread = QThread()
        self.net_worker = LightNetworkWorker()
        self.net_worker.moveToThread(self.net_thread)

        # 绑定取消操作到 Socket 掐断
        self.net_pd.sig_canceled.connect(self.net_worker.cancel)

        # 启动线程执行请求
        self.net_worker.base_url = base_url
        self.net_worker.api_key = api_key
        self.net_thread.started.connect(self.net_worker.do_fetch_models)
        self.net_worker.sig_models_fetched.connect(self._on_models_fetched)

        # 线程安全回收闭环
        self.net_worker.sig_models_fetched.connect(self.net_thread.quit)
        self.net_worker.sig_models_fetched.connect(self.net_worker.deleteLater)
        self.net_thread.finished.connect(self.net_thread.deleteLater)

        self.net_thread.start()

    def _on_fetch_log(self, level, msg):
        if level == "RESULT":
            import json
            try:
                self._fetched_models = json.loads(msg)
            except:
                pass

    def _on_models_fetched(self, success, models, msg):
        self.net_pd.close_safe()

        if success:
            self.logger.info(f"🔄 Successfully fetched {len(models)} models from API.")  # 🆕
            curr_std = self.combo_llm_model.currentText()
            curr_thk = self.combo_llm_think.currentText()

            # 阻断信号，静默刷新下拉框
            self.combo_llm_model.blockSignals(True)
            self.combo_llm_think.blockSignals(True)

            self.combo_llm_model.clear()
            self.combo_llm_think.clear()

            self.combo_llm_model.addItems(models)
            self.combo_llm_think.addItems([""] + models)  # 思考模型允许留空

            # 恢复用户之前的输入（如果存在于新列表中会自动匹配，不存在也会作为手写文本保留）
            self.combo_llm_model.setCurrentText(curr_std)
            self.combo_llm_think.setCurrentText(curr_thk)

            self.combo_llm_model.blockSignals(False)
            self.combo_llm_think.blockSignals(False)

            StandardDialog(self.widget, "Success", msg).exec()
        else:
            self.logger.warning(f"⚠️ Failed to fetch models: {msg}")
            StandardDialog(self.widget, "Information", msg).exec()

    def _start_test_task(self):
        base_url = self.input_llm_url.text().strip()
        api_key = self.input_llm_key.text().strip()

        # 优先测试思考模型，留空则测试标准模型
        model_name = self.combo_llm_think.currentText().strip() or self.combo_llm_model.currentText().strip()

        if not base_url or not model_name:
            StandardDialog(self.widget, "Warning",
                           "Please ensure Base URL and at least one Model Name are provided.").exec()
            return

        self.net_pd = ProgressDialog(
            self.widget, "API Connection Test",
            f"Sending test prompt to '{model_name}'...\n(You can cancel at any time)",
            telemetry_config={"cpu": False, "ram": False, "gpu": False, "net": False, "io": False}
        )
        self.net_pd.show()

        from PySide6.QtCore import QThread
        from src.core.network_worker import LightNetworkWorker

        # 🚀 同样采用轻量级 QThread，彻底告别 TaskManager 和 TestApiTask
        self.test_thread = QThread()
        self.test_worker = LightNetworkWorker()
        self.test_worker.moveToThread(self.test_thread)

        # 绑定取消操作
        self.net_pd.sig_canceled.connect(self.test_worker.cancel)

        # 启动线程执行请求
        self.test_worker.base_url = base_url
        self.test_worker.api_key = api_key
        self.test_worker.model_name = model_name
        self.test_thread.started.connect(self.test_worker.do_test_api)
        self.test_worker.sig_test_finished.connect(self._on_test_finished)

        # 线程安全回收闭环
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
            StandardDialog(self.widget, "Test Failed", msg).exec()


    def _on_fetch_state_changed(self, state, msg):
        if state == TaskState.SUCCESS.value:
            self.net_pd.close_safe()

            if self._fetched_models:
                # 记住当前文字，防止被清空
                curr_std = self.combo_llm_model.currentText()
                curr_thk = self.combo_llm_think.currentText()

                self.combo_llm_model.blockSignals(True)
                self.combo_llm_think.blockSignals(True)

                self.combo_llm_model.clear()
                self.combo_llm_think.clear()

                self.combo_llm_model.addItems(self._fetched_models)
                self.combo_llm_think.addItems([""] + self._fetched_models)

                self.combo_llm_model.setCurrentText(curr_std)
                self.combo_llm_think.setCurrentText(curr_thk)

                self.combo_llm_model.blockSignals(False)
                self.combo_llm_think.blockSignals(False)

                StandardDialog(self.widget, "Success",
                               f"Successfully fetched {len(self._fetched_models)} models!").exec()
            else:
                StandardDialog(self.widget, "Warning",
                               "API returned an empty list. You can still type the model name manually.").exec()

        elif state == TaskState.FAILED.value:
            self.net_pd.pbar.setVisible(False)
            self.net_pd.lbl_message.setText(f"Fetch Error:\n{msg}")
            self.net_pd.btn_cancel.setText("Close")
            self.net_pd.btn_cancel.clicked.connect(self.net_pd.close)

        elif state == TaskState.TERMINATED.value:
            self.net_pd.close()


    def _on_test_log(self, level, msg):
        if level == "RESULT":
            self._test_result_msg = msg

    def _on_test_state_changed(self, state, msg):
        if state == TaskState.SUCCESS.value:
            self.net_pd.close_safe()
            StandardDialog(self.widget, "Test Passed", self._test_result_msg).exec()

        elif state == TaskState.FAILED.value:
            self.net_pd.pbar.setVisible(False)
            self.net_pd.lbl_message.setText(f"Test Error:\n{msg}")
            self.net_pd.btn_cancel.setText("Close")
            self.net_pd.btn_cancel.clicked.connect(self.net_pd.close)

        elif state == TaskState.TERMINATED.value:
            self.net_pd.close()



    def _add_llm_provider(self):
        """动态增加一个服务商"""
        new_id = f"custom_{int(time.time())}"
        new_conf = {
            "id": new_id,
            "name": "New Provider",
            "base_url": "https://",
            "model_name": "",
            "api_key": ""
        }
        self.llm_configs.append(new_conf)
        self.combo_llm_preset.addItem(new_conf["name"])
        self.combo_llm_preset.setCurrentIndex(len(self.llm_configs) - 1)

    def _del_llm_provider(self):
        """删除当前服务商"""
        idx = self.combo_llm_preset.currentIndex()
        if idx < 0: return

        # 双重保险：后端逻辑再次拦截
        conf = self.llm_configs[idx]
        default_ids = ["openai", "deepseek", "gemini", "anthropic", "grok", "custom"]
        if conf.get("id") in default_ids:
            QMessageBox.warning(self.widget, "Warning", "Built-in default providers cannot be deleted.")
            return

        # 执行删除
        del self.llm_configs[idx]
        self.combo_llm_preset.removeItem(idx)

    # =========================================================================

    def init_system_section(self):
        group = QGroupBox("⚙️ System Preferences")
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

        layout.addRow("Theme:", self.combo_theme)
        layout.addRow("Log Level:", self.combo_log)
        self.layout.addWidget(group)

    def _get_req_html(self, conf):
        if not conf or 'recommended_config' not in conf:
            return ""

        rc = conf['recommended_config']
        prio = rc.get('device_priority', 'Unknown')
        vram = rc.get('min_vram', 'N/A')
        ram = rc.get('min_ram', 'N/A')

        prio_color = "#ffb86c" if "High-End" in prio or "Required" in prio else "#888"

        html = f"""
        <div style='margin-top:4px; font-family:Consolas; font-size:10px; color:#aaa;'>
           👉 <span style='color:{prio_color}; font-weight:bold;'>[{prio}]</span> 
           | VRAM: <span style='color:#ccc'>{vram}</span> 
           | RAM: <span style='color:#ccc'>{ram}</span>
        </div>
        """
        return html

    def check_models_status(self):
        # 1. Check Embedding
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

        # 2. Check Reranker
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
        # 1. 保存 LLM 配置
        self._save_llm_config()

        # 2. 转换 Proxy Mode UI 到 字符串
        mode_idx = self.combo_proxy_mode.currentIndex()
        mode_str = ["system", "off", "custom"][mode_idx]

        # 3. 更新设置
        self.config.user_settings.update({
            "proxy_mode": mode_str,  # 🌟 保存模式
            "proxy_url": self.input_proxy.text().strip(),
            "hf_mirror": self.input_mirror.text().strip(),
            "download_speed_limit": self.combo_embed.currentText(),
            "current_model_id": self.combo_embed.currentData(),
            "rerank_model_id": self.combo_rerank.currentData(),
            "active_llm_id": self._get_active_llm_id(),
            "theme": self.combo_theme.currentText(),
            "log_level": self.combo_log.currentText()
        })
        self.config.save_settings()
        setup_global_network_env() # 应用
        if hasattr(GlobalSignals(), 'llm_config_changed'):
            GlobalSignals().llm_config_changed.emit()
        self.logger.info(f"Settings Saved. Proxy Mode: {mode_str}")


        # 应用主题和日志级别
        qdarktheme.setup_theme(self.combo_theme.currentText().lower())
        logging.getLogger().setLevel(getattr(logging, self.combo_log.currentText()))

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

    def check_models_download_needed(self):
        # 提取原 on_save_clicked 后半部分逻辑
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
            StandardDialog(self.widget, "Success", "Settings saved. All Ready.").exec()
            self.check_models_status()
            return

        msg = "The following models need to be downloaded:\n"
        for m in to_download: msg += f"• {m}\n"

        dlg = StandardDialog(self.widget, "Download Required", msg, show_cancel=True)
        if dlg.exec():
            self.start_download(to_download)

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
            # 🌟 关键：下载完强制刷新一次全局状态，避免UI显示不一致
            GlobalSignals().kb_list_changed.emit()
            return

        self.current_repo = self.pending_downloads.pop(0)
        self.logger.info(f"⬇️ Starting download queue for: {self.current_repo}")

        if hasattr(self, 'task_mgr'): self.task_mgr = None
        self.task_mgr = TaskManager()
        self.task_mgr.sig_progress.connect(self.pd.update_progress)
        self.task_mgr.sig_state_changed.connect(self.on_task_state_changed)
        self.pd.sig_canceled.connect(self.task_mgr.cancel_task)

        # 🌟 核心修复：显式传递 HF_HOME 到子进程
        # 确保子进程知道我们要下载到 D:\xxx\models 而不是 C盘
        real_hf_home = os.environ.get("HF_HOME", os.path.join(os.getcwd(), "models"))

        self.task_mgr.start_task(
            RealTimeHFDownloadTask,
            task_id="hf_dl",
            repo_id=self.current_repo,
            hf_home=real_hf_home  # <--- 传参
        )

    def on_task_state_changed(self, state, msg):
        """处理下载状态：单步成功后静默跳转下一个"""
        if state == TaskState.SUCCESS.value:
            self.logger.info(f"✅ 模型分段下载完成: {self.current_repo}")

            # 💡 核心修复：彻底断开信号，防止重入导致 0xC0000409 崩溃
            if hasattr(self, 'task_mgr') and self.task_mgr:
                try:
                    self.task_mgr.sig_state_changed.disconnect()
                    self.task_mgr.sig_progress.disconnect()
                except:
                    pass

            # 💡 关键：延迟 500ms 启动下一个，避开信号死锁
            QTimer.singleShot(500, self._download_next)

        elif state == TaskState.FAILED.value:
            self.pd.pbar.setRange(0, 100)
            self.pd.lbl_message.setText(f"❌ 下载 {self.current_repo} 失败:\n{msg}")
            self.pd.btn_cancel.setText("关闭")
            self.pd.btn_cancel.setEnabled(True)



