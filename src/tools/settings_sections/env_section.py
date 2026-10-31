"""Settings UI mixin: environment, network, system, credentials and API server sections.

拆分自 src/tools/settings_tool.py。所有方法运行于共享的 SettingsTool 实例上，
通过 `self` 访问其状态（config / layout / 各输入控件）。
"""

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QHBoxLayout, QLineEdit,
                               QLabel, QPushButton, QGroupBox, QVBoxLayout)

from src.core.i18n import AUTO, LANGUAGE_NATIVE_NAMES, tr
from src.core.platform_env import is_windows
from src.core.theme_manager import ThemeManager, strong_weight_css
from src.ui.components.HoverRevealLineEdit import HoverRevealLineEdit
from src.ui.components.combo import BaseComboBox

logger = logging.getLogger("Settings.EnvSection")


class EnvSectionMixin:
    """环境、网络、系统与凭据相关的设置区块。"""

    # ---------- Hardware ----------
    def init_hardware_section(self):
        self.group_hw = QGroupBox(tr("System Hardware Info"))
        self.group_hw.setObjectName("group_hw")
        layout = QVBoxLayout(self.group_hw)

        self.lbl_hw_info = QLabel(tr("Scanning hardware info... Please wait."))
        self.lbl_hw_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_hw_info.setTextFormat(Qt.RichText)
        layout.addWidget(self.lbl_hw_info)

        self.layout.addWidget(self.group_hw)

    def _on_hw_detected_result(self, result):
        """硬件检测完成：只渲染 OS / CPU / 内存 / GPU。

        这里不再有"算力设备"下拉框：本地推理（ONNX 嵌入与重排）下线后，设备选择
        没有消费者，留着只会让用户以为它还能影响什么。
        """
        info = (result or {}).get("info", {})
        if not info:
            logger.warning("Hardware detection returned no data: %s",
                           (result or {}).get("msg", ""))
            return
        self._cached_hw_info = info
        self._update_hardware_html()

    def _update_hardware_html(self):
        if not hasattr(self, 'lbl_hw_info') or not getattr(self, '_cached_hw_info', None): return

        tm = ThemeManager()
        info = self._cached_hw_info
        gpu_info_list = info.get('gpu_info', [])

        gpu_str = "<br>".join([
            f"&nbsp;&nbsp;• {g.get('name', tr('Unknown'))} <span style='color:{tm.color('accent')};'>[{g.get('vram', 'N/A')}]</span>"
            for g in gpu_info_list
        ])
        if not gpu_str: gpu_str = tr("None detected")

        unknown = tr("Unknown")

        html = f"""
        <div style='font-family: {tm.mono_font_family()}; font-size: 13px; color: {tm.color("text_main")}; line-height: 1.6;'>
            <b>{tr('OS:')}</b> {info.get('os', unknown)}<br>
            <b>{tr('CPU:')}</b> {info.get('cpu', unknown)} ({info.get('cpu_cores', unknown)})<br>
            <b>{tr('RAM:')}</b> {info.get('ram_available', unknown)} / {info.get('ram_total', unknown)}<br>
            <b>{tr('GPU(s):')}</b><br>{gpu_str}
        </div>
        """
        self.lbl_hw_info.setText(html)

    # ---------- R environment ----------
    def init_r_environment_section(self):
        """Detect and configure the R runtime (used by the visualization engine)."""
        from src.core.r_engine import get_r_engine, R_DOWNLOAD_URL

        self.group_r = QGroupBox(tr("R Environment (Visualization)"))
        self.group_r.setObjectName("group_r")
        layout = QVBoxLayout(self.group_r)

        # Status label
        self.lbl_r_status = QLabel(tr("Detecting R environment..."))
        self.lbl_r_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_r_status.setTextFormat(Qt.RichText)
        layout.addWidget(self.lbl_r_status)

        # Path selection row
        path_layout = QHBoxLayout()
        self.edit_r_path = QLineEdit()
        if is_windows():
            self.edit_r_path.setPlaceholderText(
                tr("Rscript path, e.g. C:\\Program Files\\R\\R-4.3.1\\bin\\Rscript.exe"))
        else:
            self.edit_r_path.setPlaceholderText(
                tr("Rscript path, e.g. /usr/bin/Rscript (leave empty to auto-detect from PATH)"))
        self.edit_r_path.textChanged.connect(self._on_r_path_edited)
        path_layout.addWidget(self.edit_r_path, stretch=1)

        self.btn_r_browse = QPushButton(tr("Browse..."))
        self.btn_r_browse.clicked.connect(self._on_browse_r_path)
        path_layout.addWidget(self.btn_r_browse)
        layout.addLayout(path_layout)

        # 环境维护动作：把缺的包装进私有库，或在没有可用 R 时拉一个独立运行时。
        # 两者都以模态弹窗显示 R 的逐行输出，完成后再刷新本区块的状态。
        action_layout = QHBoxLayout()
        self.btn_r_recheck = QPushButton(tr("Refresh"))
        self.btn_r_recheck.clicked.connect(self._refresh_r_status)
        action_layout.addWidget(self.btn_r_recheck)

        self.btn_r_install = QPushButton(tr("Install missing packages"))
        self.btn_r_install.clicked.connect(self._on_r_install_packages)
        action_layout.addWidget(self.btn_r_install)

        self.btn_r_runtime = QPushButton(tr("Download private R runtime"))
        self.btn_r_runtime.clicked.connect(self._on_r_setup_runtime)
        action_layout.addWidget(self.btn_r_runtime)
        action_layout.addStretch()
        layout.addLayout(action_layout)

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
            status_text = tr("R detected")
            detail = (
                f"<b>{tr('Rscript:')}</b> {info.get('executable', '')}<br>"
                f"<b>{tr('Version:')}</b> R {info.get('version', tr('Unknown'))}"
            )
            detail += self._r_packages_html(engine)
        else:
            status_color = tm.color("warning")
            status_text = tr("R not detected")
            # 下载链接以占位符注入，避免把 URL 混进翻译键
            detail = tr(
                "Visualization requires R.<br>"
                "Download and install it from <a href='{url}'>{url}</a>, "
                "then specify the Rscript path above or add it to PATH."
            ).format(url=R_DOWNLOAD_URL)

        html = (
            f"<div style='font-size:13px; line-height:1.6;'>"
            f"<b>{tr('Status:')}</b> <span style='color:{status_color};'>{status_text}</span><br>"
            f"{detail}</div>"
        )
        self.lbl_r_status.setText(html)
        # Fill the placeholder with the detected path (only if not user-set).
        if info.get("available") and not self.edit_r_path.text().strip():
            self.edit_r_path.setPlaceholderText(info.get("executable", ""))
        self._update_r_actions(info)

    def _on_r_path_edited(self, _text):
        from src.core.r_engine import get_r_engine
        get_r_engine().set_custom_path(_text.strip() or None)
        self.config.set_r_path(_text.strip() or "")
        self._refresh_r_status()

    def _r_packages_html(self, engine) -> str:
        """检查 R 绘图包并生成状态片段（缺失即给出平台相关的安装指引）。

        检查范围是 :data:`src.core.plot_engine.ALLOWED_R_PACKAGES`——即引擎可能
        用到的**全部**包，而不是只查核心包：箱线图 / 小提琴图需要 ggpubr、
        相关图需要 ggcorrplot，只查核心包会让这些缺失等到出图时才暴露。
        指引统一由 :func:`src.core.r_engine.package_install_guidance` 生成，
        避免在 UI 里写死一条在部分平台走不通的命令。
        """
        from src.core import r_env
        from src.core.plot_engine import ALLOWED_R_PACKAGES

        tm = ThemeManager()
        try:
            status = engine.check_packages(ALLOWED_R_PACKAGES)
        except Exception as e:
            logger.warning(f"R package check skipped: {e}")
            return ""

        missing = [p for p in ALLOWED_R_PACKAGES if not status.get(p)]
        self._r_missing = missing

        private_lib = r_env.library_dir()
        count = len(r_env.installed_packages())
        size_mb = r_env.environment_size_bytes() / 1048576.0
        parts = [
            f"<br><b>{tr('Private library:')}</b> <code>{private_lib}</code>"
            f" <span style='color:{tm.color('text_muted')};'>"
            f"({count} {tr('packages')}, {size_mb:.1f} MB)</span>"
        ]

        try:
            from src.core.r_runtime import private_rscript, runtime_ready

            if runtime_ready():
                parts.append(f"<b>{tr('Private runtime:')}</b> "
                             f"<code>{private_rscript()}</code>")
        except Exception as e:
            logger.debug(f"Private runtime info unavailable: {e}")

        if not missing:
            parts.append(f"<b>{tr('R packages:')}</b> "
                         f"<span style='color:{tm.color('success')};'>"
                         f"{tr('all required packages available')}</span>")
            return "<br>".join(parts)

        from src.core.r_engine import package_install_guidance

        # 指引是纯文本：缩进行表示要在终端执行的命令，渲染时用等宽字体。
        guidance_html = "<br>".join(
            f"<code>{line.strip()}</code>" if line.startswith("  ") else line
            for line in package_install_guidance(missing).splitlines()
            if line.strip()
        )
        parts.append(f"<b>{tr('R packages missing:')}</b> "
                     f"<span style='color:{tm.color('warning')};'>{', '.join(missing)}</span>")
        parts.append(guidance_html)
        return "<br>".join(parts)

    def _update_r_actions(self, info):
        """按真实状态启停动作按钮，避免给出"点了必然失败"的按钮。"""
        if not hasattr(self, 'btn_r_install'):
            return
        missing = list(getattr(self, '_r_missing', []) or [])
        has_r = bool(info.get("available"))
        self.btn_r_install.setEnabled(has_r and bool(missing))
        self.btn_r_install.setToolTip(
            tr("Install the missing packages into the private library")
            if has_r else tr("No usable R runtime was found"))
        self.btn_r_runtime.setToolTip(tr("Download a self-contained R runtime"))

    def _on_r_install_packages(self):
        self._open_r_env_dialog("packages")

    def _on_r_setup_runtime(self):
        self._open_r_env_dialog("runtime")

    def _open_r_env_dialog(self, action: str):
        """打开 R 环境弹窗；弹窗内跑任务，结束后刷新状态并提示结果。"""
        from src.core.plot_engine import ALLOWED_R_PACKAGES
        from src.ui.components.dialogs.r_env_dialogs import REnvSetupDialog

        dialog = REnvSetupDialog(self.widget, action=action,
                                 packages=list(ALLOWED_R_PACKAGES))
        # 持有引用：模态对话框在 exec() 前不能只靠局部变量存活。
        self._r_env_dialog = dialog
        dialog.sig_finished.connect(self._on_r_env_finished)
        logger.info(f"Opening R environment dialog for action={action}")
        dialog.exec()
        self._r_env_dialog = None

    def _on_r_env_finished(self, payload: dict):
        """任务结束后刷新状态，并把结论以提示条告知用户。"""
        payload = payload or {}
        summary = str(payload.get("summary") or "")
        logger.info(f"R environment task finished: ok={payload.get('ok')} {summary}")
        self._refresh_r_status()
        if not summary:
            return
        try:
            from src.ui.components.toast import ToastManager

            ToastManager().show(summary, "success" if payload.get("ok") else "warning")
        except Exception as e:  # 提示条不可用不应影响状态刷新
            logger.debug(f"Could not notify R environment result: {e}")

    def _on_browse_r_path(self):
        from src.ui.components.file_dialogs import open_file_name

        # 过滤器按平台给首选项：Windows 的可执行文件是 *.exe，POSIX 上
        # Rscript 无扩展名（旧的 *.exe 优先过滤在 Linux 上会让用户以为选不中）。
        if is_windows():
            filters = "Rscript executable (*.exe);;All Files (*)"
        else:
            filters = "Rscript executable (Rscript);;All Files (*)"

        path, _ = open_file_name(
            self.widget, tr("Select Rscript executable"), "", filters)
        if path:
            self.edit_r_path.setText(path)

    # ---------- Network ----------
    def init_network_section(self):
        group = QGroupBox(tr("Network Proxy"))
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        self.combo_proxy_mode = BaseComboBox()

        # 代理模式按**索引**取值（system=0 / off=1 / custom=2），与显示文本无关，
        # 因此可以安全地本地化选项文本。默认第一项即默认行为（跟随系统代理）。
        self.combo_proxy_mode.addItems([
            tr("Use System Proxy"),
            tr("Disable Proxy (Direct)"),
            tr("Enable Proxy (Custom)"),
        ])
        self.combo_proxy_mode.setToolTip(tr(
            "System proxy follows the operating system's proxy settings (on Windows the "
            "system proxy, elsewhere the HTTP(S)_PROXY environment variables)."))

        current_mode = self.config.user_settings.get("proxy_mode", "system")
        mode_map = {"system": 0, "off": 1, "custom": 2}
        self.combo_proxy_mode.setCurrentIndex(mode_map.get(current_mode, 0))

        self.input_proxy = QLineEdit()
        self.input_proxy.setPlaceholderText(tr("e.g. http://127.0.0.1:7890"))
        self.input_proxy.setText(self.config.user_settings.get("proxy_url", ""))

        self.input_mirror = QLineEdit()
        self.input_mirror.setPlaceholderText(tr("Leave empty for default (huggingface.co)"))
        self.input_mirror.setText(self.config.user_settings.get("hf_mirror", ""))

        self.combo_proxy_mode.currentIndexChanged.connect(self._on_proxy_mode_changed)

        # 期刊登录浏览器是否走全局代理。默认关闭：部分期刊/机构（含 Tair）
        # 通过校园/机构出口 IP 自动登录，走代理反而会落到非机构 IP。
        self.chk_journal_proxy = QCheckBox(tr("Use the global proxy"))
        self.chk_journal_proxy.setToolTip(tr(
            "Off by default: some journals and institutional services auto-login by "
            "recognizing your institution's IP, and a proxy replaces that address."))
        self.chk_journal_proxy.setChecked(
            bool(self.config.user_settings.get("journal_use_proxy", False)))

        # 机构数据库直连（绕过代理）。与上面的 Journal Browser 是两件事：
        # 那条管内置浏览器，这条管应用自己的 HTTP 请求（TAIR 等按机构出口 IP 授权）。
        # 与上面的期刊浏览器开关**同向**：都是"使用全局代理"，默认都不勾（直连）。
        # 一个写"使用代理"、另一个写"直连"，用户每次都要在脑子里取反，也容易把默认值
        # 记错——两个开关的语义、默认值与措辞在此保持一致。
        self.chk_institutional_proxy = QCheckBox(tr("Use the global proxy"))
        self.chk_institutional_proxy.setToolTip(tr(
            "Off by default: TAIR and similar resources grant subscription features by "
            "recognizing your institution's IP address, and a proxy replaces that "
            "address."))
        self.chk_institutional_proxy.setChecked(
            bool(self.config.user_settings.get("institutional_use_proxy", False)))

        self.input_institutional_hosts = QLineEdit()
        self.input_institutional_hosts.setPlaceholderText(
            tr("Extra host suffix(es), comma separated, e.g. arabidopsis.org"))
        hosts = self.config.user_settings.get("institutional_hosts") or []
        self.input_institutional_hosts.setText(
            ", ".join(str(h) for h in hosts if str(h).strip()))
        self.input_institutional_hosts.setToolTip(tr(
            "Leave empty to use the built-in list. Suffixes match the host and its "
            "subdomains."))
        # 主机列表只在"不走代理"时有意义：走代理就不存在直连豁免。
        self.input_institutional_hosts.setEnabled(not self.chk_institutional_proxy.isChecked())
        self.chk_institutional_proxy.toggled.connect(
            lambda use_proxy: self.input_institutional_hosts.setEnabled(not use_proxy))

        layout.addRow(tr("Proxy Mode:"), self.combo_proxy_mode)
        layout.addRow(tr("Proxy URL:"), self.input_proxy)
        layout.addRow(tr("Journal Browser:"), self.chk_journal_proxy)
        layout.addRow(tr("Institutional DBs:"), self.chk_institutional_proxy)
        layout.addRow(tr("Direct Hosts:"), self.input_institutional_hosts)
        layout.addRow(tr("HF Mirror:"), self.input_mirror)

        self.layout.addWidget(group)

    def _on_proxy_mode_changed(self, index):
        """只有自定义模式才需要填代理地址（system=0 / off=1 / custom=2）。"""
        is_custom = (index == 2)
        self.input_proxy.setEnabled(is_custom)

    # ---------- System ----------
    def init_system_section(self):
        group = QGroupBox(tr("System Preferences"))
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        # 主题 / 日志级别：其**选项文本即配置值本身**（保存流程直接取
        # currentText 写入 settings，见 save_flow 与 ThemeManager），
        # 一旦随界面语言变化就会破坏取值，故这两项保持英文。
        self.combo_theme = BaseComboBox()
        self.combo_theme.addItems(["Dark", "Light", "Auto"])
        self.combo_theme.setCurrentText(self.config.user_settings.get("theme", "Dark"))

        self.combo_log = BaseComboBox()
        self.combo_log.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.combo_log.setCurrentText(self.config.user_settings.get("log_level", "INFO"))

        # 界面语言：显示名用各语言的原生写法（English / 简体中文），
        # 真实配置值放在 userData，避免"显示文本即配置值"在翻译后失效。
        # 切换后需重启才生效（界面在启动时一次性构建）。
        self.combo_language = BaseComboBox()
        self.combo_language.addItem(tr("Follow System"), AUTO)
        for lang_code in ("en", "zh_CN"):
            self.combo_language.addItem(LANGUAGE_NATIVE_NAMES[lang_code], lang_code)
        saved_language = self.config.user_settings.get("language", AUTO)
        idx_lang = self.combo_language.findData(saved_language)
        self.combo_language.setCurrentIndex(idx_lang if idx_lang >= 0 else 0)

        layout.addRow(tr("Theme:"), self.combo_theme)
        layout.addRow(tr("Log Level:"), self.combo_log)
        layout.addRow(tr("Interface Language:"), self.combo_language)

        # 聊天气泡排版（字号 / 字符间距 / 行距 / 段前距 / 段后距）：参数较多且需要
        # 实时预览，放在**独立的模态小面板**里编辑（含确认 / 取消 / 修改标记 /
        # 未保存退出确认）。该面板自管保存，不参与本页的 Save / Revert 流程。
        self.btn_chat_typography = QPushButton(tr(" Adjust..."))
        self.btn_chat_typography.setCursor(Qt.PointingHandCursor)
        self.btn_chat_typography.setToolTip(
            tr("Adjust font size, letter spacing, line spacing and paragraph spacing "
               "for LLM and user chat bubbles (with live preview)."))
        self.btn_chat_typography.clicked.connect(self._open_chat_typography)
        layout.addRow(tr("Chat text:"), self.btn_chat_typography)

        self.layout.addWidget(group)

    def _open_chat_typography(self):
        """打开聊天气泡排版面板。

        惰性导入：面板会拉起 ``ChatBubbleWidget``（连带 chat_tasks / 知识库等
        依赖），不能挂在设置页的导入链上，否则首次进入设置就要多等数秒。
        """
        from src.ui.components.dialogs.chat_typography_dialog import ChatTypographyDialog

        dialog = ChatTypographyDialog(self.widget)
        dialog.exec()
        # 面板是临时窗口且带 parent，不显式回收会在反复打开时持续堆积 C++ 对象
        dialog.deleteLater()

    # ---------- API Keys ----------
    def init_api_keys_section(self):
        group = QGroupBox(tr("Application Interface (API Keys)"))
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        self.input_ncbi_email = QLineEdit()
        self.input_ncbi_email.setPlaceholderText(tr("Required for NCBI Tools: e.g. user@university.edu"))
        self.input_ncbi_email.setText(self.config.user_settings.get("ncbi_email", ""))

        self.input_ncbi_api_key = HoverRevealLineEdit()
        self.input_ncbi_api_key.setPlaceholderText(tr("NCBI API Key (Optional but recommended)"))
        self.input_ncbi_api_key.setText(self.config.user_settings.get("ncbi_api_key", ""))

        self.input_openalex_api_key = HoverRevealLineEdit()
        self.input_openalex_api_key.setPlaceholderText(tr("OpenAlex Premium API Key (Optional)"))
        self.input_openalex_api_key.setText(self.config.user_settings.get("openalex_api_key", ""))

        self.input_s2_api_key = HoverRevealLineEdit()
        self.input_s2_api_key.setPlaceholderText(tr("Semantic Scholar Key (Prevents 429 Errors)"))
        self.input_s2_api_key.setText(self.config.user_settings.get("s2_api_key", ""))

        self.input_s2_rate_limit = QLineEdit()
        self.input_s2_rate_limit.setPlaceholderText(tr("S2 Rate Limit (requests/sec, default: 1.0)"))

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
        self.input_github_token.setPlaceholderText(tr("GitHub Personal Access Token (Prevents rate limiting)"))
        self.input_github_token.setText(self.config.user_settings.get("github_token", ""))

        self.lbl_api_hint = QLabel()
        self.lbl_api_hint.setWordWrap(True)
        self.lbl_api_hint.setOpenExternalLinks(True)
        ThemeManager().apply_class(self.lbl_api_hint, "hint")
        self._update_api_keys_html()

        layout.addRow(tr("NCBI Email:"), self.input_ncbi_email)
        layout.addRow(tr("NCBI API Key:"), self.input_ncbi_api_key)
        layout.addRow(tr("OpenAlex Key:"), self.input_openalex_api_key)
        layout.addRow(tr("S2 API Key:"), self.input_s2_api_key)
        layout.addRow(tr("S2 Rate Limit (req/s):"), self.input_s2_rate_limit)
        layout.addRow(tr("GitHub Token:"), self.input_github_token)
        layout.addRow("", self.lbl_api_hint)

        self.layout.addWidget(group)

    #: API 密钥说明的 HTML 模板：颜色与字重以 ``{占位符}`` 注入，
    #: 使整段文案成为**单一稳定翻译键**（不含随主题变化的色值）。
    _API_HINT_TEMPLATE = (
        "<div style='line-height: 1.5;'>"
        "<span style='color:{warning}; font-weight:{bold};'>⚠️ NCBI RATE LIMITS:</span> "
        "You MUST provide a valid email address to use NCBI tools. An API Key is "
        "<span style='color:{success}; font-weight:{bold};'>optional but highly recommended</span>. "
        "Without a key, tools will still function but under strict rate limits, which may slow down massive literature retrieval.<br><br>"
        "<span style='color:{accent}; font-weight:{bold};'>INFO & API Keys:</span><br>"
        "• <b>NCBI PubMed:</b> Email is mandatory. Adding an API key increases rate limits from 3 to 10 requests/sec. "
        "<a href='https://account.ncbi.nlm.nih.gov/settings/' style='color:{accent}; text-decoration:none;'>[Apply for NCBI Key]</a><br>"
        "• <b>OpenAlex:</b> Works without a key, but <span style='color:{warning};'>the daily quota is low and 429 Too Many Requests is common</span>. A <b>free</b> API key (sign in with an email, no payment) raises the daily quota 10&times;; paid plans raise it further. "
        "<a href='https://openalex.org/settings/api-key' style='color:{accent}; text-decoration:none;'>[Get OpenAlex API Key]</a><br>"
        "• <b>Semantic Scholar:</b> An API Key severely prevents '429 Too Many Requests' errors during massive literature retrieval. "
        "<a href='https://www.semanticscholar.org/product/api' style='color:{accent}; text-decoration:none;'>[Apply for S2 Key]</a><br>"
        "• <b>GitHub Token:</b> Increases search limits from 10/min to 30/min. "
        "<a href='https://github.com/settings/tokens?type=beta' style='color:{accent}; text-decoration:none;'>[Generate Token]</a>"
        "</div>"
    )

    def _update_api_keys_html(self):
        if not hasattr(self, 'lbl_api_hint'): return
        tm = ThemeManager()
        self.lbl_api_hint.setText(tr(self._API_HINT_TEMPLATE).format(
            warning=tm.color('warning'), success=tm.color('success'),
            accent=tm.color('accent'), bold=strong_weight_css()))

    # ---------- API Server ----------
    def init_api_server_section(self):
        group = QGroupBox(tr("Local API Server (OpenAI Compatible)"))
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignRight)

        self.input_api_host = QLineEdit()
        self.input_api_host.setPlaceholderText(tr("e.g., 127.0.0.1 or 0.0.0.0"))

        self.input_api_port = QLineEdit()
        self.input_api_port.setPlaceholderText(tr("Default: 8000"))

        self.input_api_key = HoverRevealLineEdit()
        self.input_api_key.setPlaceholderText(tr("Set a custom API Key to secure your local endpoint (Optional)"))

        layout.addRow(tr("Host Address:"), self.input_api_host)
        layout.addRow(tr("Server Port:"), self.input_api_port)
        layout.addRow(tr("Access Key:"), self.input_api_key)

        hint = QLabel(tr(
            "💡 <i>API Server runs in the background. It shares all active models, RAG, and MCP settings with the GUI. Restart the application to apply port/host changes.</i>"))
        ThemeManager().apply_class(hint, "hint")
        hint.setWordWrap(True)
        layout.addRow("", hint)

        self.layout.addWidget(group)
