import os
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QHBoxLayout, QPushButton
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtSvgWidgets import QSvgWidget

from src.tools.base_tool import BaseTool
from src.core.theme_manager import ThemeManager
from src.version import __version__, __app_name__, __description__


class AboutTool(BaseTool):
    def __init__(self):
        super().__init__("About")
        self.widget = None

    def get_ui_widget(self) -> QWidget:
        if self.widget: return self.widget

        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(20)

        self.logo = QSvgWidget(ThemeManager.get_resource_path("Assets", "ico.svg"))
        self.logo.setFixedSize(120, 120)
        layout.addWidget(self.logo, alignment=Qt.AlignCenter)

        self.lbl_title = QLabel(__app_name__)
        self.lbl_title.setAlignment(Qt.AlignCenter)

        self.lbl_desc = QLabel(__description__)
        self.lbl_desc.setAlignment(Qt.AlignCenter)

        self.lbl_version = QLabel(f"Version {__version__}")
        self.lbl_version.setAlignment(Qt.AlignCenter)

        layout.addWidget(self.lbl_title)
        layout.addWidget(self.lbl_desc)
        layout.addWidget(self.lbl_version)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(15)

        self.btn_web = QPushButton(" Official Website")
        self.btn_web.setCursor(Qt.PointingHandCursor)
        self.btn_web.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://scholarnavis.com")))

        self.btn_git = QPushButton(" GitHub Repository")
        self.btn_git.setCursor(Qt.PointingHandCursor)
        self.btn_git.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://github.com/scholarnavis/scholar_navis")))

        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_web)
        btn_layout.addWidget(self.btn_git)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

        return self.widget

    def _apply_theme(self):
        tm = ThemeManager()

        self.widget.setStyleSheet(f"background-color: {tm.color('bg_main')};")
        self.lbl_title.setStyleSheet(f"color: {tm.color('title_blue')}; font-size: 32px; font-weight: bold;")
        self.lbl_desc.setStyleSheet(f"color: {tm.color('text_main')}; font-size: 16px;")
        self.lbl_version.setStyleSheet(f"color: {tm.color('text_muted')}; font-size: 14px; font-family: Consolas;")

        self.btn_web.setIcon(tm.icon("link", "accent"))
        self.btn_git.setIcon(tm.icon("github", "accent"))
        self.btn_web.setIconSize(QSize(18, 18))
        self.btn_git.setIconSize(QSize(18, 18))

        btn_style = f"""
            QPushButton {{ 
                background-color: {tm.color('bg_card')}; color: {tm.color('accent')}; 
                border: 1px solid {tm.color('border')}; border-radius: 6px; padding: 8px 16px; font-weight: bold;
            }}
            QPushButton:hover {{ 
                background-color: {tm.color('btn_hover')}; 
            }}
        """
        self.btn_web.setStyleSheet(btn_style)
        self.btn_git.setStyleSheet(btn_style)