"""Settings UI mixin: environment, network, system, credentials and API server sections.

拆分自 src/tools/settings_tool.py。所有方法运行于共享的 SettingsTool 实例上，
通过 `self` 访问其状态（config / layout / 各输入控件）。
"""

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFormLayout, QHBoxLayout, QLineEdit,
                               QLabel, QPushButton, QGroupBox, QVBoxLayout)

from src.core.theme_manager import ThemeManager, strong_weight_css
from src.ui.components.HoverRevealLineEdit import HoverRevealLineEdit
from src.ui.components.combo import BaseComboBox

logger = logging.getLogger("Settings.EnvSection")


class EnvSectionMixin:
    """环境、网络、系统与凭据相关的设置区块。"""

    # ---------- Hardware ----------
    def init_hardware_section(self):
        self.group_hw = QGroupBox("System Hardware Info")
        self.group_hw.setObjectName("group_hw")
        layout = QVBoxLayout(self.group_hw)

        self.lbl_hw_info = QLabel("Scanning hardware info... Please wait.")
        self.lbl_hw_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_hw_info.setTextFormat(Qt.RichText)
        layout.addWidget(self.lbl_hw_info)

        self.layout.addWidget(self.group_hw)

    def _on_hw_detected_result(self, result):
        info = result.get("info", {})
        devs = result.get("devs", [{"name": "Auto Detect", "id": "auto"}])

        self._cached_hw_info = info
        self._update_hardware_html()

        curr_device = self.config.user_settings.get("inference_device", "auto")
        self.combo_device.blockSignals(True)
        self.combo_device.clear()
        for dev in devs:
            self.combo_device.addItem(dev["name"], dev["id"])
            index = self.combo_device.count() - 1
            hint = dev.get("hint", "")
            if hint:
                self.combo_device.setItemData(index, hint, Qt.ItemDataRole.ToolTipRole)
            if not dev.get("selectable", True):
                # 不可用设备（如显卡存在但缺 CUDA 运行库）只作说明展示：
                # 禁止选中，避免保存成无效配置后再去"测试设备"得到无意义报错。
                item = self.combo_device.model().item(index)
                if item is not None:
                    item.setEnabled(False)

        idx_dev = self.combo_device.findData(curr_device)
        saved_selectable = (
            idx_dev >= 0
            and self.combo_device.model().item(idx_dev) is not None
            and self.combo_device.model().item(idx_dev).isEnabled()
        )

        if saved_selectable:
            self.combo_device.setCurrentIndex(idx_dev)
        else:
            fallback = self.combo_device.findData("auto")
            self.combo_device.setCurrentIndex(fallback if fallback >= 0 else 0)
            if curr_device and curr_device != "auto":
                logger.warning(
                    f"Saved compute device '{curr_device}' is no longer available; "
                    f"switched to Auto Detect.")
                self._notify_device_fallback(curr_device)

        self.combo_device.blockSignals(False)

    def _notify_device_fallback(self, unavailable_device: str):
        """告知用户"已保存的设备在当前机器上不可用，已回落到自动选择"。"""
        try:
            from src.ui.components.toast import ToastManager

            ToastManager().show(
                f"Saved compute device '{unavailable_device}' is not available on this "
                f"machine. Switched to Auto Detect.", "warning")
        except Exception as e:  # Toast 不可用不应影响设置页加载
            logger.debug(f"Could not notify device fallback: {e}")

    def _update_hardware_html(self):
        if not hasattr(self, 'lbl_hw_info') or not getattr(self, '_cached_hw_info', None): return

        tm = ThemeManager()
        info = self._cached_hw_info
        gpu_info_list = info.get('gpu_info', [])

        gpu_str = "<br>".join([
            f"&nbsp;&nbsp;• {g.get('name', 'Unknown')} <span style='color:{tm.color('accent')};'>[{g.get('vram', 'N/A')}]</span>"
            for g in gpu_info_list
        ])
        if not gpu_str: gpu_str = "None detected"

        # 判定是否真的在加速：优先用"运行期真实可用"的探测结果；旧版本/异常时
        # 回退到构建期列表（避免字段缺失导致显示异常）。
        accel_providers = info.get('ort_providers_active') or info.get('ort_providers', [])
        has_accel = any(p in accel_providers for p in
                        ["CUDAExecutionProvider", "DmlExecutionProvider", "CoreMLExecutionProvider",
                         "ROCmExecutionProvider"])

        status_color = tm.color("success") if has_accel else tm.color("warning")
        accel_status = "Hardware Accelerated" if has_accel else "CPU Fallback"

        clean_providers = [p.replace("ExecutionProvider", "") for p in info.get('ort_providers', [])]

        html = f"""
        <div style='font-family: {tm.mono_font_family()}; font-size: 13px; color: {tm.color("text_main")}; line-height: 1.6;'>
            <b>OS:</b> {info.get('os', 'Unknown')}<br>
            <b>CPU:</b> {info.get('cpu', 'Unknown')} ({info.get('cpu_cores', 'Unknown')})<br>
            <b>RAM:</b> {info.get('ram_available', 'Unknown')} / {info.get('ram_total', 'Unknown')}<br>
            <b>GPU(s):</b><br>{gpu_str}<br>
            <b>ONNX Engine:</b> v{info.get('ort_version', 'N/A')} <span style='color:{status_color}'>[{accel_status}]</span><br>
            <b>Providers:</b> {", ".join(clean_providers)}
        </div>
        """
        self.lbl_hw_info.setText(html)

    # ---------- R environment ----------
    def init_r_environment_section(self):
        """Detect and configure the R runtime (used by the visualization engine)."""
        from src.core.r_engine import get_r_engine, R_DOWNLOAD_URL

        self.group_r = QGroupBox("R Environment (Visualization)")
        self.group_r.setObjectName("group_r")
        layout = QVBoxLayout(self.group_r)

        # Status label
        self.lbl_r_status = QLabel("Detecting R environment...")
        self.lbl_r_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_r_status.setTextFormat(Qt.RichText)
        layout.addWidget(self.lbl_r_status)

        # Path selection row
        path_layout = QHBoxLayout()
        self.edit_r_path = QLineEdit()
        if sys.platform == "win32":
            self.edit_r_path.setPlaceholderText(
                "Rscript path, e.g. C:\\Program Files\\R\\R-4.3.1\\bin\\Rscript.exe")
        else:
            self.edit_r_path.setPlaceholderText(
                "Rscript path, e.g. /usr/bin/Rscript (leave empty to auto-detect from PATH)")
        self.edit_r_path.textChanged.connect(self._on_r_path_edited)
        path_layout.addWidget(self.edit_r_path, stretch=1)

        self.btn_r_browse = QPushButton("Browse...")
        self.btn_r_browse.clicked.connect(self._on_browse_r_path)
        path_layout.addWidget(self.btn_r_browse)
        layout.addLayout(path_layout)

        self.layout.addWidget(self.group_r)
        self._refresh_r_status()

    def _refresh_r_status(self):
        """Re-detect R and refresh the status display."""
        if not hasattr(self, 'lbl_r_status'):
            return
        from src.core.r_engine import get_r_engine, R_DOWNLOAD_URL

        tm = ThemeManager()
        engine = get_r_engine()
        # Prefer the user-typed path; otherwise fall back to the configured path.
        custom = ""
        if hasattr(self, 'edit_r_path'):
            custom = self.edit_r_path.text().strip()
        if not custom:
            custom = self.config.get_r_path()
            if custom and hasattr(self, 'edit_r_path'):
                self.edit_r_path.setText(custom)
        engine.set_custom_path(custom or None)

        info = engine.detect()
        if info.get("available"):
            status_color = tm.color("success")
            status_text = "R detected"
            detail = (
                f"<b>Rscript:</b> {info.get('executable', '')}<br>"
                f"<b>Version:</b> R {info.get('version', 'Unknown')}"
            )
            detail += self._r_packages_html(engine)
        else:
            status_color = tm.color("warning")
            status_text = "R not detected"
            detail = (
                "Visualization requires R.<br>"
                f"Download and install it from <a href='{R_DOWNLOAD_URL}'>{R_DOWNLOAD_URL}</a>, "
                "then specify the Rscript path above or add it to PATH."
            )

        html = (
            f"<div style='font-size:13px; line-height:1.6;'>"
            f"<b>Status:</b> <span style='color:{status_color};'>{status_text}</span><br>"
            f"{detail}</div>"
        )
        self.lbl_r_status.setText(html)
        # Fill the placeholder with the detected path (only if not user-set).
        if info.get("available") and not self.edit_r_path.text().strip():
            self.edit_r_path.setPlaceholderText(info.get("executable", ""))

    def _on_r_path_edited(self, _text):
        from src.core.r_engine import get_r_engine
        get_r_engine().set_custom_path(_text.strip() or None)
        self.config.set_r_path(_text.strip() or "")
        self._refresh_r_status()

    def _r_packages_html(self, engine) -> str:
        """检查核心 R 绘图包并生成状态片段（缺失即给出安装命令）。

        可视化依赖 ggplot2 等包。Linux 发行版通常把 R 包拆成独立软件包
        （r-cran-* 或需 install.packages），把缺失项提前显示出来可以避免
        用户在出图阶段才遇到报错。
        """
        from src.core.plot_engine import CORE_R_PACKAGES

        tm = ThemeManager()
        try:
            status = engine.check_packages(CORE_R_PACKAGES)
        except Exception as e:
            logger.warning(f"R package check skipped: {e}")
            return ""

        missing = [p for p in CORE_R_PACKAGES if not status.get(p)]
        if not missing:
            return (f"<br><b>R packages:</b> "
                    f"<span style='color:{tm.color('success')};'>all core packages available</span>")

        install_cmd = "install.packages(c(" + ", ".join(f'"{p}"' for p in missing) + "))"
        return (
            f"<br><b>R packages missing:</b> "
            f"<span style='color:{tm.color('warning')};'>{', '.join(missing)}</span><br>"
            f"In an R session run: <code>{install_cmd}</code>"
        )

    def _on_browse_r_path(self):
        from PySide6.QtWidgets import QFileDialog

        # 过滤器按平台给首选项：Windows 的可执行文件是 *.exe，POSIX 上
        # Rscript 无扩展名（旧的 *.exe 优先过滤在 Linux 上会让用户以为选不中）。
        if sys.platform == "win32":
            filters = "Rscript executable (*.exe);;All Files (*)"
        else:
            filters = "Rscript executable (Rscript);;All Files (*)"

        path, _ = QFileDialog.getOpenFileName(
            self.widget, "Select Rscript executable", "", filters)
        if path:
            self.edit_r_path.setText(path)

    # ---------- Network ----------
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

    # ---------- System ----------
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

        layout.addRow("Theme:", self.combo_theme)
        layout.addRow("Log Level:", self.combo_log)
        self.layout.addWidget(group)

    # ---------- API Keys ----------
    def init_api_keys_section(self):
        group = QGroupBox("Application Interface (API Keys)")
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        self.input_ncbi_email = QLineEdit()
        self.input_ncbi_email.setPlaceholderText("Required for NCBI Tools: e.g. user@university.edu")
        self.input_ncbi_email.setText(self.config.user_settings.get("ncbi_email", ""))

        self.input_ncbi_api_key = HoverRevealLineEdit()
        self.input_ncbi_api_key.setPlaceholderText("NCBI API Key (Optional but recommended)")
        self.input_ncbi_api_key.setText(self.config.user_settings.get("ncbi_api_key", ""))

        self.input_openalex_api_key = HoverRevealLineEdit()
        self.input_openalex_api_key.setPlaceholderText("OpenAlex Premium API Key (Optional)")
        self.input_openalex_api_key.setText(self.config.user_settings.get("openalex_api_key", ""))

        self.input_s2_api_key = HoverRevealLineEdit()
        self.input_s2_api_key.setPlaceholderText("Semantic Scholar Key (Prevents 429 Errors)")
        self.input_s2_api_key.setText(self.config.user_settings.get("s2_api_key", ""))

        self.input_s2_rate_limit = QLineEdit()
        self.input_s2_rate_limit.setPlaceholderText("S2 Rate Limit (requests/sec, default: 1.0)")

        from PySide6.QtGui import QDoubleValidator
        validator = QDoubleValidator(0.01, 1000.0, 2, self.input_s2_rate_limit)
        validator.setNotation(QDoubleValidator.StandardNotation)
        self.input_s2_rate_limit.setValidator(validator)

        current_limit = self.config.user_settings.get("s2_rate_limit", 1.0)
        try:
            val = float(current_limit)
            if val <= 0: val = 1.0
        except (ValueError, TypeError):
            val = 1.0
        self.input_s2_rate_limit.setText(str(val))

        self.input_github_token = HoverRevealLineEdit()
        self.input_github_token.setPlaceholderText("GitHub Personal Access Token (Prevents rate limiting)")
        self.input_github_token.setText(self.config.user_settings.get("github_token", ""))

        self.lbl_api_hint = QLabel()
        self.lbl_api_hint.setWordWrap(True)
        self.lbl_api_hint.setOpenExternalLinks(True)
        ThemeManager().apply_class(self.lbl_api_hint, "hint")
        self._update_api_keys_html()

        layout.addRow("NCBI Email:", self.input_ncbi_email)
        layout.addRow("NCBI API Key:", self.input_ncbi_api_key)
        layout.addRow("OpenAlex Key:", self.input_openalex_api_key)
        layout.addRow("S2 API Key:", self.input_s2_api_key)
        layout.addRow("S2 Rate Limit (req/s):", self.input_s2_rate_limit)
        layout.addRow("GitHub Token:", self.input_github_token)
        layout.addRow("", self.lbl_api_hint)

        self.layout.addWidget(group)

    def _update_api_keys_html(self):
        if not hasattr(self, 'lbl_api_hint'): return
        tm = ThemeManager()
        self.lbl_api_hint.setText(
            f"<div style='line-height: 1.5;'>"
            f"<span style='color:{tm.color('warning')}; font-weight:{strong_weight_css()};'>⚠️ NCBI RATE LIMITS:</span> "
            f"You MUST provide a valid email address to use NCBI tools. An API Key is <span style='color:{tm.color('success')}; font-weight:{strong_weight_css()};'>optional but highly recommended</span>. Without a key, tools will still function but under strict rate limits, which may slow down massive literature retrieval.<br><br>"
            f"<span style='color:{tm.color('accent')}; font-weight:{strong_weight_css()};'>INFO & API Keys:</span><br>"
            f"• <b>NCBI PubMed:</b> Email is mandatory. Adding an API key increases rate limits from 3 to 10 requests/sec. "
            f"<a href='https://account.ncbi.nlm.nih.gov/settings/' style='color:{tm.color('accent')}; text-decoration:none;'>[Apply for NCBI Key]</a><br>"
            f"• <b>OpenAlex:</b> Works without a key, but <span style='color:{tm.color('warning')};'>the daily quota is low and 429 Too Many Requests is common</span>. A <b>free</b> API key (sign in with an email, no payment) raises the daily quota 10&times;; paid plans raise it further. "
            f"<a href='https://openalex.org/settings/api-key' style='color:{tm.color('accent')}; text-decoration:none;'>[Get OpenAlex API Key]</a><br>"
            f"• <b>Semantic Scholar:</b> An API Key severely prevents '429 Too Many Requests' errors during massive literature retrieval. "
            f"<a href='https://www.semanticscholar.org/product/api' style='color:{tm.color('accent')}; text-decoration:none;'>[Apply for S2 Key]</a><br>"
            f"• <b>GitHub Token:</b> Increases search limits from 10/min to 30/min. "
            f"<a href='https://github.com/settings/tokens?type=beta' style='color:{tm.color('accent')}; text-decoration:none;'>[Generate Token]</a>"
            f"</div>"
        )

    # ---------- API Server ----------
    def init_api_server_section(self):
        group = QGroupBox("Local API Server (OpenAI Compatible)")
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        self.input_api_host = QLineEdit()
        self.input_api_host.setPlaceholderText("e.g., 127.0.0.1 or 0.0.0.0")

        self.input_api_port = QLineEdit()
        self.input_api_port.setPlaceholderText("Default: 8000")

        self.input_api_key = HoverRevealLineEdit()
        self.input_api_key.setPlaceholderText("Set a custom API Key to secure your local endpoint (Optional)")

        layout.addRow("Host Address:", self.input_api_host)
        layout.addRow("Server Port:", self.input_api_port)
        layout.addRow("Access Key:", self.input_api_key)

        hint = QLabel(
            "💡 <i>API Server runs in the background. It shares all active models, RAG, and MCP settings with the GUI. Restart the application to apply port/host changes.</i>")
        ThemeManager().apply_class(hint, "hint")
        hint.setWordWrap(True)
        layout.addRow("", hint)

        self.layout.addWidget(group)
