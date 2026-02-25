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
                               QTableWidgetItem, QCheckBox, QApplication)
from PySide6.QtCore import Qt, QThread, Signal, QObject, QTimer, QEvent
from huggingface_hub import constants

from src.core.core_task import TaskState, TaskManager, TaskMode
from src.core.device_manager import DeviceManager
from src.core.mcp_manager import MCPManager
from src.core.models_registry import (EMBEDDING_MODELS, RERANKER_MODELS,
                                      get_model_conf, check_model_exists, resolve_auto_model)
from src.core.network_worker import setup_global_network_env, LightNetworkWorker
from src.core.signals import GlobalSignals
from src.task.settings_tasks import VerifySettingsTask
from src.tools.base_tool import BaseTool
from src.core.config_manager import ConfigManager
from src.task.hf_download_task import RealTimeHFDownloadTask
from src.ui.components.combo import BaseComboBox
from src.ui.components.dialog import ProgressDialog, StandardDialog, McpConfigDialog, NetworkModelDialog
from src.ui.components.toast import ToastManager


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

    def get_ui_widget(self):
        self.widget = QWidget()
        main_layout = QVBoxLayout(self.widget)

        # 1. 滚动区域设置
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        self.layout = QVBoxLayout(scroll_content)

        self.init_hardware_section()
        self.init_system_section()
        self.init_network_section()
        self.init_llm_section()
        self.init_model_section()
        self.init_ncbi_section()
        self.init_mcp_section()

        self.layout.addStretch()  # 防止上方内容被拉伸
        scroll.setWidget(scroll_content)

        # 将滚动区域优先加入全局布局
        main_layout.addWidget(scroll)

        # 2. 底部固定按钮区
        btn_layout = QHBoxLayout()
        btn_undo = QPushButton("↺ Revert Changes")
        btn_undo.setStyleSheet("padding: 10px; font-weight: bold; background-color: #444; color: white;")
        btn_undo.clicked.connect(self.on_undo_clicked)

        btn_save = QPushButton("💾 Save Settings & Verify")
        btn_save.setStyleSheet("padding: 10px; font-weight: bold; background-color: #05B8CC; color: white;")
        btn_save.clicked.connect(self.on_save_clicked)

        btn_layout.addWidget(btn_undo)
        btn_layout.addWidget(btn_save)

        # 把按钮区域加到 main_layout，脱离滚动条的控制
        main_layout.addLayout(btn_layout)

        self._load_current_settings()

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._refresh_mcp_status)
        self.status_timer.start(5000)

        return self.widget

    def _load_current_settings(self):
        """加载配置到 UI"""
        if hasattr(self, '_load_mcp_servers_to_ui'):
            self.config.load_settings()
            self.config.load_mcp_servers()
            self._load_mcp_servers_to_ui()

        ToastManager().show("Changes reverted to the last saved state.", "info")

        if hasattr(self, '_refresh_mcp_status'):
            self._refresh_mcp_status()




    def _refresh_mcp_status(self):
        try:
            from src.core.mcp_manager import MCPManager
            mcp_mgr = MCPManager.get_instance()

            for row in range(self.table_mcp.rowCount()):
                name_item = self.table_mcp.item(row, 1)
                if not name_item: continue
                name = name_item.text()

                status_lbl = self.table_mcp.cellWidget(row, 4)
                if not status_lbl: continue

                chk_widget = self.table_mcp.cellWidget(row, 0)
                chk = chk_widget.layout().itemAt(0).widget() if chk_widget else None
                is_enabled = chk.isChecked() if chk else False

                if is_enabled:
                    status = mcp_mgr.get_server_status(name)
                    if status == "connected":
                        status_lbl.setText("Connected")
                        status_lbl.setStyleSheet("color: #4caf50; font-weight: bold;")
                    elif "error" in status:
                        status_lbl.setText("Error")
                        status_lbl.setStyleSheet("color: #ff6b6b;")
                        status_lbl.setToolTip(status)
                    else:
                        status_lbl.setText(status.capitalize())
                        status_lbl.setStyleSheet("color: #ffb86c;")
                else:
                    status_lbl.setText("Disabled")
                    status_lbl.setStyleSheet("color: #888;")

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
        """撤销当前 UI 的更改，恢复到上次保存的配置状态"""
        # 1. 恢复 NCBI / S2 API 密钥配置
        self.input_ncbi_email.setText(self.config.user_settings.get("ncbi_email", ""))
        self.input_ncbi_api_key.setText(self.config.user_settings.get("ncbi_api_key", ""))
        self.input_s2_api_key.setText(self.config.user_settings.get("s2_api_key", ""))

        # 2. 恢复网络与代理配置
        mode_map = {"system": 0, "off": 1, "custom": 2}
        self.combo_proxy_mode.setCurrentIndex(mode_map.get(self.config.user_settings.get("proxy_mode", "system"), 0))
        self.input_proxy.setText(self.config.user_settings.get("proxy_url", ""))
        self.input_mirror.setText(self.config.user_settings.get("hf_mirror", ""))

        # 3. 恢复本地模型选项（包括刚刚加的低显存模式）
        curr_embed = self.config.user_settings.get("current_model_id", "embed_auto")
        idx_embed = self.combo_embed.findData(curr_embed)
        self.combo_embed.setCurrentIndex(max(0, idx_embed))

        curr_rerank = self.config.user_settings.get("rerank_model_id", "rerank_auto")
        idx_rerank = self.combo_rerank.findData(curr_rerank)
        self.combo_rerank.setCurrentIndex(max(0, idx_rerank))

        if hasattr(self, 'chk_low_vram'):
            self.chk_low_vram.setChecked(self.config.user_settings.get("low_vram_mode", False))

        # 4. 恢复 LLM 核心配置文件 (直接从本地 json 文件重新加载)
        self.llm_configs = self._load_llm_config()

        active_id = self.config.user_settings.get("active_llm_id", "openai")
        idx_to_select = next((i for i, c in enumerate(self.llm_configs) if c.get("id") == active_id), 0)
        self.combo_llm_preset.setCurrentIndex(idx_to_select)
        self._on_llm_preset_changed(idx_to_select)

        trans_id = self.config.user_settings.get("trans_llm_id", None)
        idx_trans = self.combo_trans_provider.findData(trans_id)
        if idx_trans >= 0:
            self.combo_trans_provider.setCurrentIndex(idx_trans)

        # 5. 恢复系统设置
        self.combo_theme.setCurrentText(self.config.user_settings.get("theme", "Dark"))
        self.combo_log.setCurrentText(self.config.user_settings.get("log_level", "INFO"))
        self.input_ext_python.setText(self.config.user_settings.get("external_python_path", "python"))

        self.config.load_mcp_servers()  # 从磁盘强行重新读取上次保存的 JSON
        if hasattr(self, '_load_mcp_servers_to_ui'):
            self._load_mcp_servers_to_ui()

        # 通知用户
        ToastManager().show("Changes reverted to the last saved state.", "info")

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

        # ------------------- 嵌入网络模型管理 -------------------
        self.table_net_models = QTableWidget(0, 3)
        self.table_net_models.setHorizontalHeaderLabels(["Type", "Provider / Model", "Status"])
        self.table_net_models.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table_net_models.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_net_models.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table_net_models.setStyleSheet(
            "background: #1e1e1e; color: #ccc; gridline-color: #333; border: 1px solid #444;")
        self.table_net_models.setFixedHeight(120)

        btn_layout = QHBoxLayout()
        btn_add = QPushButton("➕ Add Network Model")
        btn_edit = QPushButton("✏️ Edit")
        btn_del = QPushButton("🗑️ Delete")
        for btn in [btn_add, btn_edit, btn_del]:
            btn.setStyleSheet("padding: 5px 10px; background: #2d2d30; border: 1px solid #555; border-radius: 4px;")
            btn_layout.addWidget(btn)
        btn_layout.addStretch()

        btn_add.clicked.connect(self._add_net_model)
        btn_edit.clicked.connect(self._edit_net_model)
        btn_del.clicked.connect(self._del_net_model)

        if isinstance(layout, QFormLayout):
            layout.addRow(QLabel("Network Models:"), btn_layout)
            layout.addRow("", self.table_net_models)
        else:
            layout.addLayout(btn_layout)
            layout.addWidget(self.table_net_models)

        self.refresh_net_models_table()
        self.layout.addWidget(group)

    def refresh_net_models_table(self):
        self.table_net_models.setRowCount(0)
        net_models = self.config.user_settings.get("custom_network_models", [])
        for m in net_models:
            row = self.table_net_models.rowCount()
            self.table_net_models.insertRow(row)
            self.table_net_models.setItem(row, 0, QTableWidgetItem(m.get("type", "").capitalize()))
            # 💡 严格按照你要求的 Provider / Model 格式展示
            self.table_net_models.setItem(row, 1, QTableWidgetItem(f"{m.get('provider_name')} / {m.get('model_name')}"))
            self.table_net_models.setItem(row, 2, QTableWidgetItem("Active"))
            self.table_net_models.item(row, 0).setData(Qt.UserRole, m)

    def _add_net_model(self):
        providers = self.config.user_settings.get("llm_configs", [])  # 获取你的服务商列表
        dlg = NetworkModelDialog(self.widget, providers=providers)
        if dlg.exec():
            new_model = dlg.get_data()
            net_models = self.config.user_settings.get("custom_network_models", [])
            net_models.append(new_model)
            self.config.user_settings["custom_network_models"] = net_models
            self.refresh_net_models_table()
            self.config.save_settings()

    def _edit_net_model(self):
        row = self.table_net_models.currentRow()
        if row < 0: return
        old_data = self.table_net_models.item(row, 0).data(Qt.UserRole)
        providers = self.config.user_settings.get("llm_configs", [])

        dlg = NetworkModelDialog(self.widget, providers=providers, existing_data=old_data)
        if dlg.exec():
            new_model = dlg.get_data()
            net_models = self.config.user_settings.get("custom_network_models", [])
            idx = net_models.index(old_data)
            net_models[idx] = new_model
            self.config.user_settings["custom_network_models"] = net_models
            self.refresh_net_models_table()
            self.config.save_settings()

    def _del_net_model(self):
        row = self.table_net_models.currentRow()
        if row < 0: return
        old_data = self.table_net_models.item(row, 0).data(Qt.UserRole)
        net_models = self.config.user_settings.get("custom_network_models", [])
        net_models.remove(old_data)
        self.config.user_settings["custom_network_models"] = net_models
        self.refresh_net_models_table()
        self.config.save_settings()


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

    def init_mcp_section(self):
        group = QGroupBox("🔌 MCP Servers (Unified)")
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #444; margin-top: 10px; padding-top: 15px; background: #252526; }")
        layout = QVBoxLayout(group)

        header_layout = QHBoxLayout()
        header_layout.addWidget(QLabel("<b>Manage Local & Remote Tools:</b>"))
        header_layout.addStretch()

        btn_add = QPushButton("➕ Add Server")
        btn_add.clicked.connect(self._on_add_mcp_clicked)
        btn_add.setStyleSheet("background: #05B8CC; color: white; border-radius: 4px; padding: 4px 10px;")

        btn_refresh = QPushButton("🔄 Refresh Status")
        btn_refresh.clicked.connect(self._refresh_mcp_status)
        btn_refresh.setStyleSheet("background: #444; color: white; border-radius: 4px; padding: 4px 10px;")

        header_layout.addWidget(btn_add)
        header_layout.addWidget(btn_refresh)
        layout.addLayout(header_layout)

        # 统一的 Server 列表视图
        self.table_mcp = QTableWidget(0, 6)
        self.table_mcp.setHorizontalHeaderLabels(["Enabled", "Name", "Type", "Target", "Status", "Action"])
        self.table_mcp.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table_mcp.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        self.table_mcp.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table_mcp.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_mcp.setFixedHeight(220)
        self.table_mcp.setStyleSheet("""
            QTableWidget { background-color: #1e1e1e; color: #eee; border: 1px solid #444; }
            QHeaderView::section { background-color: #2a2d2e; color: #ccc; border: 1px solid #444; padding: 4px; }
        """)
        self.table_mcp.cellDoubleClicked.connect(self._on_mcp_double_clicked)
        layout.addWidget(self.table_mcp)

        lbl_hint = QLabel(
            "💡 <i>Changes to MCP servers require clicking the blue 'Save Settings & Verify' button below to take effect.</i>")
        lbl_hint.setStyleSheet("color: #aaa; font-size: 11px;")
        layout.addWidget(lbl_hint)

        self.layout.addWidget(group)

    def _on_mcp_double_clicked(self, row, col):
        name_item = self.table_mcp.item(row, 1)
        if not name_item: return

        name = name_item.text()

        if name in ["builtin", "external"]:
            from src.ui.components.toast import ToastManager
            ToastManager().show(f"Core service '{name}' cannot be edited here.", "info")
            return

        self._on_edit_mcp_clicked(row)

    def _load_mcp_servers_to_ui(self):
        """将 ConfigManager 中的 JSON 渲染到表格"""
        servers = self.config.mcp_servers.get("mcpServers", {})
        self.table_mcp.setRowCount(0)
        for name, cfg in servers.items():
            self._add_mcp_row(name, cfg)

    def _add_mcp_row(self, name, cfg):
        row = self.table_mcp.rowCount()
        self.table_mcp.insertRow(row)

        always_on = cfg.get("always_on", False)
        is_enabled = cfg.get("enabled", False) or always_on

        # 0. Enabled Checkbox
        chk = QCheckBox()
        chk.setChecked(is_enabled)
        if always_on:
            chk.setEnabled(False)  # 保护 builtin 核心不被意外关闭
            chk.setToolTip("Core service must remain enabled.")
        chk.setStyleSheet("color: white; background: transparent; margin-left: 10px;")
        chk_widget = QWidget()
        l = QHBoxLayout(chk_widget)
        l.addWidget(chk)
        l.setAlignment(Qt.AlignCenter)
        l.setContentsMargins(0, 0, 0, 0)
        self.table_mcp.setCellWidget(row, 0, chk_widget)

        # 1. Name
        name_item = QTableWidgetItem(name)
        name_item.setData(Qt.UserRole, cfg)  # 隐式存储完整配置对象
        name_item.setFlags(name_item.flags() ^ Qt.ItemIsEditable)
        self.table_mcp.setItem(row, 1, name_item)

        # 2. Type
        stype = cfg.get("type", "stdio")
        type_item = QTableWidgetItem(stype)
        type_item.setFlags(type_item.flags() ^ Qt.ItemIsEditable)
        self.table_mcp.setItem(row, 2, type_item)

        # 3. Target
        target = cfg.get("command", "") if stype == "stdio" else cfg.get("url", "")
        target_item = QTableWidgetItem(target)
        target_item.setFlags(target_item.flags() ^ Qt.ItemIsEditable)
        self.table_mcp.setItem(row, 3, target_item)

        # 4. Status
        status_lbl = QLabel("Checking...")
        status_lbl.setAlignment(Qt.AlignCenter)
        self.table_mcp.setCellWidget(row, 4, status_lbl)

        # 5. Actions (Edit / Delete)
        action_widget = QWidget()
        al = QHBoxLayout(action_widget)
        al.setContentsMargins(0, 0, 0, 0)

        btn_edit = QPushButton("✏️")
        btn_edit.setCursor(Qt.PointingHandCursor)
        btn_edit.setStyleSheet("background: transparent; border: none;")
        btn_edit.clicked.connect(lambda _, r=row: self._on_edit_mcp_clicked(r))
        al.addWidget(btn_edit)

        if not always_on and name not in ["builtin", "external"]:
            btn_del = QPushButton("🗑️")
            btn_del.setCursor(Qt.PointingHandCursor)
            btn_del.setStyleSheet("background: transparent; border: none;")

            def delete_mcp_row(row_idx, srv_name=name):
                # 🌟 替换掉原本原生的 QMessageBox，使用你的 StandardDialog
                dlg = StandardDialog(
                    self.widget,
                    "Confirm Delete",
                    f"Are you sure you want to delete MCP Server '{srv_name}'?\nThis will disconnect it immediately.",
                    show_cancel=True
                )

                if dlg.exec():
                    MCPManager.get_instance().disconnect_server(srv_name)
                    self.table_mcp.removeRow(row_idx)

            btn_del.clicked.connect(lambda _, r=row: delete_mcp_row(self.table_mcp.indexAt(btn_del.pos()).row()))
            al.addWidget(btn_del)


        self.table_mcp.setCellWidget(row, 5, action_widget)

    def _on_add_mcp_clicked(self):
        dlg = McpConfigDialog(self.widget)
        if dlg.exec():
            name, cfg = dlg.get_config()
            if not name: return
            cfg["enabled"] = True
            self._add_mcp_row(name, cfg)

    def _on_edit_mcp_clicked(self, row):
        name_item = self.table_mcp.item(row, 1)
        if not name_item: return

        old_name = name_item.text()
        old_cfg = name_item.data(Qt.UserRole)

        dlg = McpConfigDialog(self.widget, server_name=old_name, server_config=old_cfg)
        if dlg.exec():
            new_name, new_cfg = dlg.get_config()
            # 合并保留隐藏属性如 always_on
            new_cfg["always_on"] = old_cfg.get("always_on", False)
            new_cfg["enabled"] = old_cfg.get("enabled", True)

            # 更新 UI
            name_item.setText(new_name)
            name_item.setData(Qt.UserRole, new_cfg)
            self.table_mcp.item(row, 2).setText(new_cfg.get("type", "stdio"))
            target = new_cfg.get("command", "") if new_cfg.get("type", "stdio") == "stdio" else new_cfg.get("url", "")
            self.table_mcp.item(row, 3).setText(target)

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

        self.chk_low_vram = QCheckBox("Low VRAM Mode (Release RAG models after search)")
        self.chk_low_vram.setChecked(self.config.user_settings.get("low_vram_mode", False))
        self.chk_low_vram.setStyleSheet("color: #ff9800; font-weight: bold;")
        self.chk_low_vram.setToolTip(
            "Enable this to prevent OOM errors. It will unload Embedding and Reranker models before the LLM starts generating.")

        layout.addRow("Embedding:", self.combo_embed)
        layout.addRow("", self.lbl_embed_status)
        layout.addRow("Reranker:", self.combo_rerank)
        layout.addRow("", self.lbl_rerank_status)

        self.chk_low_vram = QCheckBox("Low VRAM Mode (Release RAG models after search)")
        self.chk_low_vram.setChecked(self.config.user_settings.get("low_vram_mode", False))
        self.chk_low_vram.setStyleSheet("color: #ffb86c; font-weight: bold; margin-top: 10px;")

        lbl_vram_desc = QLabel(
            "<div style='font-size: 11px; color: #aaa; line-height: 1.5; margin-left: 20px;'>"
            "💡 <b>Turn ON (Low VRAM):</b> Frees up memory immediately after document retrieval.<br>"
            "&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:#4caf50;'>👍 Pros: Maximizes LLM context length, prevents Out-of-Memory (OOM) crashes.</span><br>"
            "&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:#ff6b6b;'>👎 Cons: Adds 1~3s loading delay to every new query.</span><br>"
            "💡 <b>Turn OFF (Speed Mode):</b> Keeps RAG models persistently in memory.<br>"
            "&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:#4caf50;'>👍 Pros: Lightning-fast multi-turn conversation.</span><br>"
            "&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:#ff6b6b;'>👎 Cons: Embedding + Reranker will constantly occupy VRAM/RAM.</span>"
            "</div>"
        )
        lbl_vram_desc.setWordWrap(True)
        lbl_vram_desc.setTextFormat(Qt.RichText)

        layout.addRow("", self.chk_low_vram)
        layout.addRow("", lbl_vram_desc)
        # ==========================================

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
            {"id": "moonshot", "name": "Moonshot (Kimi)", "base_url": "https://api.moonshot.cn/v1",
             "model_name": "moonshot-v1-auto", "api_key": ""},
            {"id": "minimax", "name": "MiniMax", "base_url": "https://api.minimax.chat/v1",
             "model_name": "abab6.5s-chat", "api_key": ""},
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

        # --- 独立翻译层选择 ---
        trans_group = QGroupBox("🌐 Translation Agent Configuration")
        trans_group.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #444; margin-top: 10px; padding-top: 15px; background: #252526; }")
        trans_layout = QFormLayout(trans_group)

        trans_provider_layout = QHBoxLayout()
        self.combo_trans_provider = BaseComboBox()
        self.combo_trans_model = BaseComboBox()
        self.btn_trans_refresh = QPushButton("🔄 Refresh Models")
        self.btn_trans_refresh.setToolTip("从上方 LLM 配置的缓存模型列表中拉取最新数据")

        for conf in self.llm_configs:
            self.combo_trans_provider.addItem(conf.get("name", "Unnamed Provider"), conf.get("id"))

        trans_provider_layout.addWidget(self.combo_trans_provider, stretch=1)
        trans_provider_layout.addWidget(self.btn_trans_refresh)

        trans_layout.addRow("Translation Provider:", trans_provider_layout)
        trans_layout.addRow("Translation Model:", self.combo_trans_model)

        lbl_trans_hint = QLabel(
            "💡 <b>Note:</b> Configure the specific translation model for each provider here. It is highly recommended to select a fast, non-reasoning model (e.g., gpt-4o-mini). Do not use 'thinking' models as they inject unwanted reasoning blocks into translations.")
        lbl_trans_hint.setWordWrap(True)
        lbl_trans_hint.setStyleSheet("color: #aaa; font-size: 11px; font-style: italic; margin-top: 5px;")
        trans_layout.addRow("", lbl_trans_hint)

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

        # 连接翻译器专用信号
        self.combo_trans_provider.currentIndexChanged.connect(self._on_trans_provider_changed)
        self.combo_trans_model.currentTextChanged.connect(self._sync_trans_model)
        self.btn_trans_refresh.clicked.connect(self._on_trans_refresh_clicked)

        # 初始化显示
        self._on_trans_provider_changed(0)

    def _on_trans_refresh_clicked(self):
        """用户主动点击刷新按钮时触发，包含 Toast 提示"""
        self._refresh_trans_models_ui()
        ToastManager().show("Translation models refreshed from cache.", "success")

    def _on_trans_provider_changed(self, index):
        if index < 0 or index >= len(self.llm_configs): return
        self._refresh_trans_models_ui()

    def _refresh_trans_models_ui(self):
        idx = self.combo_trans_provider.currentIndex()
        if idx < 0: return
        conf = self.llm_configs[idx]

        # 尝试读取现有的翻译模型，若没有则用聊天模型兜底
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
            self._save_llm_config()  # 静默保存映射关系
            # 触发全局信号让 Chat 和 QuickTranslator 的下拉栏立即刷新展示文本
            if hasattr(GlobalSignals(), 'llm_config_changed'):
                GlobalSignals().llm_config_changed.emit()



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

        # 选中正确的项
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
        self.net_pd.sig_canceled.connect(self.net_worker.cancel, Qt.DirectConnection)
        self.net_pd.sig_canceled.connect(self.net_thread.terminate)

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
        self.net_pd.sig_canceled.connect(self.test_worker.cancel, Qt.DirectConnection)
        self.net_pd.sig_canceled.connect(self.test_thread.terminate)

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
            StandardDialog(
                self.widget,
                "Warning",
                "Built-in default providers cannot be deleted."
            ).exec()
            return

        del self.llm_configs[idx]
        self.combo_llm_preset.removeItem(idx)

    def init_network_rag_section(self):
        group = QGroupBox("🌐 Network RAG API (Embedding & Reranker)")
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #444; margin-top: 10px; padding-top: 15px; background: #252526; }")
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        self.inp_net_embed_url = QLineEdit()
        self.inp_net_embed_url.setPlaceholderText("e.g. https://api.siliconflow.cn")
        self.inp_net_embed_url.setText(self.config.user_settings.get("network_embed_url", ""))
        self.inp_net_embed_url.setStyleSheet("background: #333; color: #fff; border: 1px solid #555; padding: 5px;")

        self.inp_net_embed_key = QLineEdit()
        self.inp_net_embed_key.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.inp_net_embed_key.setPlaceholderText("Bearer Token for Network RAG")
        self.inp_net_embed_key.setText(self.config.user_settings.get("network_embed_key", ""))
        self.inp_net_embed_key.setStyleSheet("background: #333; color: #fff; border: 1px solid #555; padding: 5px;")

        layout.addRow("API Base URL:", self.inp_net_embed_url)
        layout.addRow("API Key:", self.inp_net_embed_key)

        self.layout.addWidget(group)

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

        # 1. 保存 LLM 配置
        if hasattr(self, '_sync_llm_data'):
            self._sync_llm_data()
        self._save_llm_config()

        # 2. 提取并保存统一的 MCP 配置
        new_mcp_servers = {}
        if hasattr(self, 'table_mcp'):
            for row in range(self.table_mcp.rowCount()):
                name_item = self.table_mcp.item(row, 1)
                if not name_item: continue

                name = name_item.text()
                cfg = name_item.data(Qt.UserRole)

                chk_widget = self.table_mcp.cellWidget(row, 0)
                if chk_widget and chk_widget.layout():
                    chk = chk_widget.layout().itemAt(0).widget()
                    cfg["enabled"] = chk.isChecked()
                else:
                    cfg["enabled"] = False

                new_mcp_servers[name] = cfg

            self.config.mcp_servers["mcpServers"] = new_mcp_servers
            self.config.save_mcp_servers()

        # 3. 提取常规设置
        new_email = self.input_ncbi_email.text().strip()
        new_key = self.input_ncbi_api_key.text().strip()
        new_s2_key = self.input_s2_api_key.text().strip()

        mode_idx = self.combo_proxy_mode.currentIndex()
        new_proxy_mode = ["system", "off", "custom"][mode_idx]
        new_proxy_url = self.input_proxy.text().strip()

        # 写入 user_settings
        self.config.user_settings.update({
            "proxy_mode": new_proxy_mode,
            "proxy_url": new_proxy_url,
            "hf_mirror": self.input_mirror.text().strip(),
            "download_speed_limit": self.combo_embed.currentText(),
            "current_model_id": self.combo_embed.currentData(),
            "rerank_model_id": self.combo_rerank.currentData(),
            "active_llm_id": self._get_active_llm_id(),
            "trans_llm_id": self.combo_trans_provider.currentData(),
            "theme": self.combo_theme.currentText(),
            "log_level": self.combo_log.currentText(),
            "ncbi_email": new_email,
            "ncbi_api_key": new_key,
            "s2_api_key": new_s2_key,
            "external_python_path": getattr(self, 'input_ext_python', QLineEdit()).text().strip(),
            "low_vram_mode": getattr(self, 'chk_low_vram', None) and self.chk_low_vram.isChecked()
        })

        # 清理配置里的旧版残留垃圾
        for old_key in ["external_mcp_enabled", "network_mcps", "network_mcp_enabled"]:
            self.config.user_settings.pop(old_key, None)

        self.config.save_settings()

        # 4. 应用环境变量与 UI
        setup_global_network_env()
        os.environ["NCBI_API_EMAIL"] = new_email
        os.environ["NCBI_API_KEY"] = new_key
        os.environ["S2_API_KEY"] = new_s2_key

        if hasattr(GlobalSignals(), 'llm_config_changed'):
            GlobalSignals().llm_config_changed.emit()

        qdarktheme.setup_theme(self.combo_theme.currentText().lower())
        import logging
        logging.getLogger().setLevel(getattr(logging, self.combo_log.currentText()))

        # 5. 唤起进度弹窗，启动后台验证
        from src.ui.components.dialog import ProgressDialog
        self.save_pd = ProgressDialog(
            self.widget, "Applying Settings",
            "Initializing background tasks...",
            telemetry_config={"cpu": False, "ram": False, "gpu": False, "net": False, "io": False}

        )
        self.save_pd.show()
        QApplication.processEvents()

        if hasattr(self, 'save_task_mgr') and self.save_task_mgr:
            self.save_task_mgr.cancel_task()

        from src.core.core_task import TaskManager, TaskMode
        from src.task.settings_tasks import VerifySettingsTask
        self.save_task_mgr = TaskManager()
        self.save_task_mgr.sig_progress.connect(self.save_pd.update_progress)
        self.save_task_mgr.sig_state_changed.connect(self._on_save_task_state_changed)
        self.save_task_mgr.sig_result.connect(self._on_save_task_result)
        self.save_pd.sig_canceled.connect(self.save_task_mgr.cancel_task)

        embed_id = self.combo_embed.currentData()
        rerank_id = self.combo_rerank.currentData()

        # 启动后台验证任务
        self.save_task_mgr.start_task(
            VerifySettingsTask,
            task_id="verify_settings",
            mode=TaskMode.THREAD,
            embed_id=embed_id,
            rerank_id=rerank_id,
            mcp_config={}
        )


    def _on_save_task_state_changed(self, state, msg):
        if state == TaskState.FAILED.value:
            if hasattr(self, 'save_pd'):
                self.save_pd.close_safe()
            self.logger.error(f"Save process failed: {msg}")
            ToastManager().show(f"System Error: {msg}", "error")

    def _on_save_task_result(self, result_dict):
        """主线程收到 TaskManager 传回的结果后，执行 MCP 连接与模型检查"""
        if hasattr(self, 'save_pd'):
            self.save_pd.close_safe()

        if not result_dict:
            return

        to_download = result_dict.get("to_download", [])

        try:
            from src.core.mcp_manager import MCPManager
            mcp_mgr = MCPManager.get_instance()
            mcp_mgr.bootstrap_servers()

            if hasattr(self, '_refresh_mcp_status'):
                self._refresh_mcp_status()
        except Exception as e:
            self.logger.error(f"MCP Update Failed: {e}")

        if not to_download:
            from src.ui.components.dialog import StandardDialog
            StandardDialog(self.widget, "Success",
                           "Settings saved successfully.\nAll models and LLM APIs are ready.").exec()
            self.check_models_status()
            return

        dl_msg = "The following models need to be downloaded:\n"
        for m in to_download: dl_msg += f"• {m}\n"

        from src.ui.components.dialog import StandardDialog
        dlg = StandardDialog(self.widget, "Download Required", dl_msg, show_cancel=True)
        if dlg.exec():
            self.start_download(to_download)
        else:
            self.check_models_status()



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