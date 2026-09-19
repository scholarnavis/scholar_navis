import datetime
import platform

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.core.core_task import TaskManager, TaskMode
from src.core.theme_manager import ThemeManager, strong_weight_css, title_weight_css
from src.core.version import (
    __app_name__,
    __channel__,
    __company__,
    __description__,
    __dl__,
    __github__,
    __version__,
    __website__,
)
from src.task.common_task import VersionCheckTask
from src.tools.base_tool import BaseTool
from src.ui.components.dialog import (
    ApiProvidersDialog,
    LicenseDialog,
    ReleaseNotesDialog,
)
from src.ui.components.text_formatter import mono_font_family_css

#: 更新提示行里"应用内查看更新日志"的伪协议地址（区别于 http(s) 外链）。
_RELEASE_NOTES_URL = "navis://release-notes"


class AboutTool(BaseTool):
    # 由 get_ui_widget/_on_version_clicked 创建，仅作静态检查声明
    logo: QSvgWidget
    lbl_title: QLabel
    lbl_desc: QLabel
    lbl_version: QLabel
    lbl_third_party: QLabel
    _version_click_count: int
    _dev_dialog: QWidget
    # 更新检查结果（由 _on_version_checked 填充，_update_link_ui/_show_release_notes 读取）
    _latest_version: str
    _channel: str
    _changelog: str
    _release_url: str
    _dl_url: str

    def __init__(self):
        super().__init__("About")
        self.widget = None
        self.task_manager = TaskManager()
        self.task_manager.sig_result.connect(self._on_version_checked)

    def get_ui_widget(self) -> QWidget:
        if self.widget: return self.widget

        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)
        layout.setAlignment(Qt.AlignCenter)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(12)

        self.logo = QSvgWidget(ThemeManager.get_resource_path("assets", "ico.svg"))
        self.logo.setFixedSize(140, 140)
        layout.addWidget(self.logo, alignment=Qt.AlignCenter)
        layout.addSpacing(10)

        self.lbl_title = QLabel(__app_name__)
        self.lbl_title.setAlignment(Qt.AlignCenter)

        self.lbl_desc = QLabel(__description__)
        self.lbl_desc.setAlignment(Qt.AlignCenter)
        self.lbl_desc.setWordWrap(True)

        self.lbl_version = QLabel(f"Current Release: v{__version__}")
        self.lbl_version.setAlignment(Qt.AlignCenter)
        self.lbl_version.setCursor(Qt.PointingHandCursor)
        # Hidden entry: click the version label 5 times to open developer mode.
        self.lbl_version.mousePressEvent = self._on_version_clicked
        self._version_click_count = 0
        self._dev_dialog = None

        self.lbl_update = QLabel()
        self.lbl_update.setAlignment(Qt.AlignCenter)
        self.lbl_update.setCursor(Qt.PointingHandCursor)
        self.lbl_update.hide()
        # 两类链接：伪协议（应用内更新日志）与外链（下载）。因此不能交给
        # Qt 直接打开外链，必须自己分发，见 _on_update_link。
        self.lbl_update.setOpenExternalLinks(False)
        self.lbl_update.linkActivated.connect(self._on_update_link)

        layout.addWidget(self.lbl_version)
        layout.addWidget(self.lbl_update)
        layout.addSpacing(20)

        self.disclaimer_container = QWidget()
        self.disclaimer_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        disclaimer_layout = QHBoxLayout(self.disclaimer_container)
        disclaimer_layout.setContentsMargins(20, 20, 20, 20)
        disclaimer_layout.setSpacing(15)

        self.lbl_disclaimer_icon = QLabel()
        self.lbl_disclaimer_icon.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

        self.lbl_disclaimer_text = QLabel()
        self.lbl_disclaimer_text.setWordWrap(True)
        self.lbl_disclaimer_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        disclaimer_text = (
            "<b>IMPORTANT DISCLAIMER</b><br><br>"
            "Scholar Navis uses Large Language Models (LLMs). While augmented with RAG and MCP, "
            "AI-generated content may still contain <b>inaccuracies or hallucinations</b>. Users are <b>strictly required</b> "
            "to verify information via provided citations/links. Developers are not liable for any research errors or "
            "academic misconduct arising from the use of this tool."
        )
        self.lbl_disclaimer_text.setText(disclaimer_text)

        disclaimer_layout.addWidget(self.lbl_disclaimer_icon)
        disclaimer_layout.addWidget(self.lbl_disclaimer_text)

        layout.addWidget(self.disclaimer_container)
        # ---------------

        layout.addSpacing(25)

        # 按钮容器
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)

        self.btn_web = QPushButton(" Website")
        self.btn_git = QPushButton(" GitHub")
        self.btn_license = QPushButton(" Licenses")
        self.btn_api = QPushButton(" Data Providers")

        for btn in [self.btn_web, self.btn_git, self.btn_license, self.btn_api]:
            btn.setCursor(Qt.PointingHandCursor)

        self.btn_web.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(__website__)))
        self.btn_git.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(__github__)))
        self.btn_license.clicked.connect(self._show_licenses)
        self.btn_api.clicked.connect(self._show_api_providers)

        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_web)
        btn_layout.addWidget(self.btn_git)
        btn_layout.addWidget(self.btn_license)
        btn_layout.addWidget(self.btn_api)
        btn_layout.addStretch()

        layout.addLayout(btn_layout)

        # 版权/许可行：公司名取自 version.py 的 __company__，年份取当前年份 ——
        # 原先公司名与年份都硬编码在字符串里，改公司名或跨年后必须改代码。
        self.lbl_copy = QLabel(
            f"Licensed under AGPL-3.0 | © {datetime.date.today().year} {__company__}")
        self.lbl_copy.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_copy)

        self.lbl_third_party = QLabel(
            "Third-party components are listed under Licenses, "
            "including the optional R runtime and its plotting packages.")
        self.lbl_third_party.setAlignment(Qt.AlignCenter)
        self.lbl_third_party.setWordWrap(True)
        layout.addWidget(self.lbl_third_party)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

        self.task_manager.start_task(VersionCheckTask, task_id="check_update", mode=TaskMode.THREAD)

        return self.widget

    def _on_version_clicked(self, event):
        """Version-click counter: 5 consecutive clicks open the hidden developer mode."""
        self._version_click_count += 1
        remaining = 5 - self._version_click_count
        if remaining > 0:
            self.lbl_version.setToolTip(f"Click {remaining} more time(s) to open developer mode")
        else:
            self._version_click_count = 0
            self.lbl_version.setToolTip("")
            self._open_developer_mode()
        event.accept()

    def _open_developer_mode(self):
        """Open the developer-mode dialog (lazily created singleton)."""
        from src.ui.components.developer_dialog import DeveloperDialog
        if self._dev_dialog is None:
            main_win = self.widget.window() if self.widget else None
            self._dev_dialog = DeveloperDialog(main_win)
        self._dev_dialog.show()
        self._dev_dialog.raise_()
        self._dev_dialog.activateWindow()

    def _on_version_checked(self, payload):
        """消费 :class:`VersionCheckTask` 的结果。

        任务侧已经完成"通道归属 + 版本比较"，这里只负责展示：``latest_version``
        为空即代表"无更新 / 通道无产物 / 检查失败"，一律不打扰用户。
        """
        if not payload:
            return

        latest_version = payload.get("latest_version")
        if not latest_version:
            self.lbl_update.hide()
            return

        self._latest_version = latest_version
        self._channel = payload.get("channel") or __channel__
        self._changelog = payload.get("changelog") or ""
        self._release_url = payload.get("release_url") or ""
        self._dl_url = (payload.get("download_url")
                        or f"{__dl__}?os={platform.system().lower()}&channel={self._channel}")

        self._update_link_ui()
        self.lbl_update.show()

    def _on_update_link(self, url: str):
        """更新提示行的链接分发：伪协议走应用内，其余一律交给系统浏览器。"""
        if url.startswith("navis://"):
            self._show_release_notes()
            return
        QDesktopServices.openUrl(QUrl(url))

    def _show_release_notes(self):
        """在应用内展示更新日志（Markdown 渲染，见 ReleaseNotesDialog）。"""
        dlg = ReleaseNotesDialog(
            self.widget,
            version=getattr(self, "_latest_version", ""),
            markdown_text=getattr(self, "_changelog", ""),
            current_version=__version__,
            channel=getattr(self, "_channel", __channel__),
            release_url=getattr(self, "_release_url", ""),
            download_url=getattr(self, "_dl_url", ""),
        )
        dlg.exec()

    def _update_link_ui(self):
        """渲染更新提示行：查看更新日志（应用内） + 下载（浏览器）。"""
        if not (hasattr(self, '_latest_version') and hasattr(self, '_dl_url')):
            return

        tm = ThemeManager()
        base_color = tm.color('success')
        link_color = tm.color('accent')
        muted_color = tm.color('text_muted')
        link_style = f"color: {link_color}; text-decoration: underline;"
        channel_note = " (dev channel)" if getattr(self, '_channel', '') == 'dev' else ""

        html = (f'<span style="color: {base_color};">New version v{self._latest_version}'
                f'{channel_note} available!</span> '
                f'<a href="{_RELEASE_NOTES_URL}" style="{link_style}">Release notes</a>'
                f'<span style="color: {muted_color};"> · </span>'
                f'<a href="{self._dl_url}" style="{link_style}">Download</a>')
        self.lbl_update.setText(html)


    def _show_licenses(self):
        dlg = LicenseDialog(self.widget)
        dlg.exec()

    def _show_api_providers(self):
        dlg = ApiProvidersDialog(self.widget)
        dlg.exec()

    def _apply_theme(self):
        tm = ThemeManager()
        base_font = f"font-family: {tm.font_family()};"

        self.widget.setStyleSheet("background-color: transparent;")
        self.lbl_title.setStyleSheet(f"{base_font} color: {tm.color('title_blue')}; font-size: 36px; font-weight: {title_weight_css()};")
        self.lbl_desc.setStyleSheet(f"{base_font} color: {tm.color('text_main')}; font-size: 15px; margin-bottom: 5px;")

        self.lbl_version.setStyleSheet(
            f"color: {tm.color('text_muted')}; font-size: 13px; "
            f"font-family: {mono_font_family_css()};")
        self.lbl_update.setStyleSheet(f"{base_font} font-weight: {strong_weight_css()}; font-size: 13px;")
        if hasattr(self, '_update_link_ui'):
            self._update_link_ui()
        self.lbl_copy.setStyleSheet(f"{base_font} color: {tm.color('text_muted')}; font-size: 11px; margin-top: 30px;")
        self.lbl_third_party.setStyleSheet(
            f"{base_font} color: {tm.color('text_muted')}; font-size: 11px;")

        self.btn_web.setIcon(tm.icon("link", "text_main"))
        self.btn_git.setIcon(tm.icon("github", "text_main"))
        self.btn_license.setIcon(tm.icon("copyright", "text_main"))
        self.btn_api.setIcon(tm.icon("api", "text_main"))

        btn_style = f"""
            QPushButton {{ 
                {base_font}
                background-color: {tm.color('bg_card')}; 
                color: {tm.color('text_main')}; 
                border: 1px solid {tm.color('border')}; 
                border-radius: 8px; 
                padding: 8px 18px; 
                font-weight: {strong_weight_css()};
            }}
            QPushButton:hover {{ 
                background-color: {tm.color('btn_hover')}; 
                border: 1px solid {tm.color('accent')};
                color: {tm.color('accent')};
            }}
        """

        self.disclaimer_container.setStyleSheet(f"""
                    QWidget {{
                        background-color: {tm.color('bg_input')};
                        border: 1px solid {tm.color('border')};
                        border-left: 4px solid {tm.color('warning')};
                        border-radius: 8px;
                    }}
                """)

        icon_pixmap = tm.icon("info", "warning").pixmap(QSize(24, 24))
        self.lbl_disclaimer_icon.setPixmap(icon_pixmap)
        self.lbl_disclaimer_icon.setStyleSheet("background: transparent; border: none;")

        self.lbl_disclaimer_text.setStyleSheet(f"""
                    QLabel {{
                        {base_font} 
                        color: {tm.color('text_main')}; 
                        font-size: 14px; 
                        background: transparent;
                        border: none;
                    }}
                """)

        for btn in [self.btn_web, self.btn_git, self.btn_license, self.btn_api]:
            btn.setIconSize(QSize(16, 16))
            btn.setStyleSheet(btn_style)