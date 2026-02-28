import os
import logging
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QHBoxLayout, QPushButton
from PySide6.QtCore import Qt, QUrl, QSize
from PySide6.QtGui import QDesktopServices
from PySide6.QtSvgWidgets import QSvgWidget

from src.tools.base_tool import BaseTool
from src.core.theme_manager import ThemeManager
from src.core.core_task import TaskManager, BackgroundTask, TaskMode
from src.core.network_worker import create_robust_session
from src.version import __version__, __app_name__, __description__


# --- 后台版本检测任务 ---
class VersionCheckTask(BackgroundTask):
    def _execute(self):
        try:
            session = create_robust_session()
            response = session.get("https://scholarnavis.com/latest", timeout=5)
            if response.status_code == 200:
                latest_version = response.text.strip()
                return {"latest_version": latest_version}
        except Exception as e:
            self.logger.error(f"Failed to check for updates: {e}")
        return {"latest_version": None}


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
        self.lbl_update.mousePressEvent = lambda e: QDesktopServices.openUrl(QUrl("https://scholarnavis.com/dl"))

        layout.addWidget(self.lbl_title)
        layout.addWidget(self.lbl_desc)
        layout.addWidget(self.lbl_version)
        layout.addWidget(self.lbl_update)
        layout.addSpacing(20)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(20)

        self.btn_web = QPushButton(" Website")
        self.btn_web.setCursor(Qt.PointingHandCursor)
        self.btn_web.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://scholarnavis.com")))

        self.btn_git = QPushButton(" GitHub")
        self.btn_git.setCursor(Qt.PointingHandCursor)
        self.btn_git.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://github.com/scholarnavis/scholar_navis")))

        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_web)
        btn_layout.addWidget(self.btn_git)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        self.lbl_copy = QLabel("© 2026 Scholar Navis Studio. Built with PySide6.")
        self.lbl_copy.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_copy)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

        self.task_manager.start_task(VersionCheckTask, "check_update", TaskMode.THREAD)

        return self.widget

    def _on_version_checked(self, payload):
        latest_version = payload.get("latest_version")
        if latest_version and latest_version != "0.0.0" and latest_version != __version__:
            self.lbl_update.setText(f"New version v{latest_version} available! Click to download.")
            self.lbl_update.show()
            self._apply_theme()

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
        self.btn_web.setIconSize(QSize(18, 18))
        self.btn_git.setIconSize(QSize(18, 18))

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
        self.btn_web.setStyleSheet(btn_style)
        self.btn_git.setStyleSheet(btn_style)