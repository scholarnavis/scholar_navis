"""Settings UI mixin: AI models (Embedding / Reranker) management and downloads.

拆分自 src/tools/settings_tool.py。负责模型区块构建、状态校验、
手动下载 / 删除与 HF 下载队列。
"""

import os

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QPushButton)

from src.core import BASE_DIR
from src.core.core_task import TaskManager, TaskMode, TaskState
from src.core.models_registry import (EMBEDDING_MODELS, RERANKER_MODELS,
                                      get_model_conf, resolve_auto_model, get_onnx_cache_dir)
from src.core.network_worker import setup_global_network_env
from src.core.signals import GlobalSignals
from src.core.theme_manager import ThemeManager
from src.task.hf_download_task import RealTimeHFDownloadTask
from src.task.settings_tasks import TestDeviceTask
from src.task.common_task import VerifyModelsTask
from src.ui.components.combo import BaseComboBox
from src.ui.components.dialog import ProgressDialog, StandardDialog
from src.ui.components.toast import ToastManager


class ModelSectionMixin:
    """AI 模型配置区块与模型下载队列。"""

    # ---------- Section build ----------
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
        curr_device = self.config.user_settings.get("inference_device", "auto")
        self.combo_device.addItem("Detecting devices...", curr_device)
        layout.addRow("Compute Device:", self.combo_device)

        # --- 4. 其他模型设置 ---
        self.btn_test_device = QPushButton(" Test Compute Device")
        ThemeManager().apply_class(self.btn_test_device, "link-btn")
        self.btn_test_device.setCursor(Qt.PointingHandCursor)
        self.btn_test_device.clicked.connect(self._test_compute_device)
        layout.addRow("", self.btn_test_device)


        self.layout.addWidget(group)
        QTimer.singleShot(50, self.check_models_status)

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
        model_dir = os.path.join(BASE_DIR, "models")
        os.makedirs(model_dir, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(model_dir))

    # ---------- Device test ----------
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
        if result.get("success"):
            self.test_dev_pd.show_finish_state(True, "Test Passed", result["msg"])
        else:
            self.test_dev_pd.show_finish_state(False, "Test Failed", result["msg"])

    # ---------- Manual model actions ----------
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

    # ---------- Combo refresh ----------
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

    # ---------- Status verification ----------
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

    # ---------- Download queue ----------
    def start_download(self, repo_list):
        if not repo_list: return
        self.pending_downloads = repo_list
        self.pd = ProgressDialog(self.widget, "Downloading", "Initializing...", telemetry_config={"net": True})
        self.pd.show()
        self._download_next()

    def _download_next(self):
        if not self.pending_downloads:
            self.pd.show_finish_state(True, "Complete", "All downloads finished.")
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
        elif state in [TaskState.FAILED.value, TaskState.TERMINATED.value]:
            self.pd.show_finish_state(False, "Download Halted", f"Task ended: {msg}")
