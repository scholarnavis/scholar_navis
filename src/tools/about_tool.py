from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QHBoxLayout, QPushButton
from PySide6.QtCore import Qt, QUrl, QSize
from PySide6.QtGui import QDesktopServices
from PySide6.QtSvgWidgets import QSvgWidget

from src.task.common_task import VersionCheckTask
from src.tools.base_tool import BaseTool
from src.core.theme_manager import ThemeManager
from src.core.core_task import TaskManager, TaskMode
from src.ui.components.dialog import LicenseDialog
from src.version import __version__, __app_name__, __description__, __website__, __github__, __dl__


class AboutTool(BaseTool):
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

        self.lbl_update = QLabel()
        self.lbl_update.setAlignment(Qt.AlignCenter)
        self.lbl_update.setCursor(Qt.PointingHandCursor)
        self.lbl_update.hide()
        self.lbl_update.setOpenExternalLinks(True)

        layout.addWidget(self.lbl_title)
        layout.addWidget(self.lbl_desc)
        layout.addWidget(self.lbl_version)
        layout.addWidget(self.lbl_update)
        layout.addSpacing(20)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(15)

        self.btn_web = QPushButton(" Website")
        self.btn_git = QPushButton(" GitHub")
        self.btn_license = QPushButton(" Licenses")  # 新增按钮

        # 设置鼠标形状
        for btn in [self.btn_web, self.btn_git, self.btn_license]:
            btn.setCursor(Qt.PointingHandCursor)

        self.btn_web.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(__website__)))
        self.btn_git.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(__github__)))
        self.btn_license.clicked.connect(self._show_licenses)  # 连接新方法

        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_web)
        btn_layout.addWidget(self.btn_git)
        btn_layout.addWidget(self.btn_license)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        self.lbl_copy = QLabel("Licensed under AGPL v3 | © 2026 Scholar Navis Studio")
        self.lbl_copy.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_copy)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

        self.task_manager.start_task(VersionCheckTask, task_id="check_update", mode=TaskMode.THREAD)

        return self.widget

    def _on_version_checked(self, payload):
        if not payload:
            return

        latest_version = payload.get("latest_version")
        if latest_version and latest_version != "0.0.0" and latest_version != __version__:
            self.lbl_update.setText(
                f'<a href="{__dl__}" style="color: inherit; text-decoration: none;">New version v{latest_version} available! Click to download.</a>')
            self.lbl_update.show()
            self._apply_theme()


    def _show_licenses(self):
        """显示开源许可对话框"""
        dlg = LicenseDialog(self.widget)
        dlg.exec()


    def _apply_theme(self):
        tm = ThemeManager()
        base_font = f"font-family: {tm.font_family()};"

        self.widget.setStyleSheet("background-color: transparent;")
        self.lbl_title.setStyleSheet(f"{base_font} color: {tm.color('title_blue')}; font-size: 36px; font-weight: 900;")
        self.lbl_desc.setStyleSheet(f"{base_font} color: {tm.color('text_main')}; font-size: 15px; margin-bottom: 5px;")

        self.lbl_version.setStyleSheet(
            f"color: {tm.color('text_muted')}; font-size: 13px; font-family: 'Consolas', monospace;")
        self.lbl_update.setStyleSheet(
            f"{base_font} color: {tm.color('success')}; font-weight: bold; font-size: 13px; text-decoration: underline;")
        self.lbl_copy.setStyleSheet(f"{base_font} color: {tm.color('text_muted')}; font-size: 11px; margin-top: 30px;")


        self.btn_web.setIcon(tm.icon("link", "text_main"))
        self.btn_git.setIcon(tm.icon("github", "text_main"))
        self.btn_license.setIcon(tm.icon("copyright", "text_main"))

        btn_style = f"""
            QPushButton {{ 
                {base_font}
                background-color: {tm.color('bg_card')}; 
                color: {tm.color('text_main')}; 
                border: 1px solid {tm.color('border')}; 
                border-radius: 8px; 
                padding: 8px 20px; 
                font-weight: bold;
            }}
            QPushButton:hover {{ 
                background-color: {tm.color('btn_hover')}; 
                border: 1px solid {tm.color('accent')};
                color: {tm.color('accent')};
            }}
        """

        for btn in [self.btn_web, self.btn_git, self.btn_license]:
            btn.setIconSize(QSize(18, 18))
            btn.setIconSize(QSize(18, 18))


            btn.setStyleSheet(btn_style)
            btn.setStyleSheet(btn_style)