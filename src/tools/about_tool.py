import os
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QHBoxLayout, QPushButton
from PySide6.QtCore import Qt, QSize, QUrl
from PySide6.QtGui import QDesktopServices
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
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(12)

        # Logo 居中
        self.logo = QSvgWidget(ThemeManager.get_resource_path("Assets", "ico.svg"))
        self.logo.setFixedSize(140, 140)
        layout.addWidget(self.logo, alignment=Qt.AlignCenter)
        layout.addSpacing(10)

        # 标题与描述
        self.lbl_title = QLabel(__app_name__)
        self.lbl_title.setAlignment(Qt.AlignCenter)

        self.lbl_desc = QLabel(__description__)
        self.lbl_desc.setAlignment(Qt.AlignCenter)
        self.lbl_desc.setWordWrap(True)

        self.lbl_version = QLabel(f"Stable Release: v{__version__}")
        self.lbl_version.setAlignment(Qt.AlignCenter)

        layout.addWidget(self.lbl_title)
        layout.addWidget(self.lbl_desc)
        layout.addWidget(self.lbl_version)
        layout.addSpacing(20)

        # 按钮组
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(20)

        self.btn_web = QPushButton("  Website")
        self.btn_web.setCursor(Qt.PointingHandCursor)
        self.btn_web.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://scholarnavis.com")))

        self.btn_git = QPushButton("  GitHub")
        self.btn_git.setCursor(Qt.PointingHandCursor)
        self.btn_git.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://github.com/scholarnavis/scholar_navis")))

        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_web)
        btn_layout.addWidget(self.btn_git)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # 版权声明
        lbl_copy = QLabel("© 2026 Scholar Navis Studio. Built with PySide6 & Nuitka.")
        lbl_copy.setStyleSheet("font-size: 10px; opacity: 0.6; margin-top: 30px;")
        lbl_copy.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl_copy)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

        return self.widget

    def _apply_theme(self):
        tm = ThemeManager()
        self.widget.setStyleSheet(f"background-color: transparent;")
        self.lbl_title.setStyleSheet(f"color: {tm.color('title_blue')}; font-size: 36px; font-weight: 900;")
        self.lbl_desc.setStyleSheet(f"color: {tm.color('text_main')}; font-size: 15px; margin-bottom: 5px;")
        self.lbl_version.setStyleSheet(f"color: {tm.color('accent')}; font-size: 13px; font-family: 'Consolas', monospace; font-weight: bold;")

        btn_style = f"""
            QPushButton {{ 
                background-color: {tm.color('bg_card')}; color: {tm.color('text_main')}; 
                border: 1px solid {tm.color('border')}; border-radius: 10px; padding: 10px 24px; font-weight: bold;
            }}
            QPushButton:hover {{ 
                background-color: {tm.color('btn_hover')}; border: 1px solid {tm.color('accent')};
                color: {tm.color('accent')};
            }}
        """
        self.btn_web.setStyleSheet(btn_style)
        self.btn_git.setStyleSheet(btn_style)