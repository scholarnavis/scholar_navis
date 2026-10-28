"""External tools panel (sidebar page).

把"应用不自带、取决于用户机器装了什么"的三件事放在同一页：R 运行时、生信工具、
WSL。它们此前挂在设置页里，但排查"为什么某个分析跑不了"是一个高频动作，埋在多页
设置里需要来回滚动。

界面本身不重复实现：R 区块复用 :class:`~src.tools.settings_sections.env_section.
EnvSectionMixin`，工具表格复用 :class:`~src.tools.settings_sections.toolenv_section.
ToolEnvSectionMixin`。两处显示同一份状态，避免"设置页说可用、这里说不可用"。
"""

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from src.core.i18n import tr
from src.core.theme_manager import ThemeManager
from src.tools.base_tool import BaseTool
from src.tools.settings_sections.env_section import EnvSectionMixin
from src.tools.settings_sections.toolenv_section import ToolEnvSectionMixin

logger = logging.getLogger(__name__)


class ToolEnvTool(EnvSectionMixin, ToolEnvSectionMixin, BaseTool):
    """侧边栏页面：R 环境 + 生信工具 + WSL 状态。"""

    def __init__(self):
        super().__init__("Tool Environment")
        self.widget = None
        self._ui_ready = False
        self._theme_connected = False

    def get_ui_widget(self) -> QWidget:
        if getattr(self, "_ui_ready", False):
            return self.widget
        self._ui_ready = True

        self.widget = QWidget()
        outer = QVBoxLayout(self.widget)
        outer.setContentsMargins(15, 10, 15, 20)
        outer.setSpacing(20)
        # 两个 mixin 都通过 self.layout 追加区块，这里给它挂到本页的纵向布局上。
        self.layout = outer

        try:
            self.init_r_environment_section()
            self.init_toolenv_section()
        except Exception as e:
            # 单块构建失败不应让整页空白：另一块通常仍然可用。
            logger.exception("Failed to build the external tools panel: %s", e)
            error_label = QLabel(f"{tr('Failed to build this panel:')} {e}")
            error_label.setAlignment(Qt.AlignLeft)
            outer.addWidget(error_label)

        outer.addStretch()

        if not self._theme_connected:
            self._theme_connected = True
            ThemeManager().theme_changed.connect(self._on_theme_changed)
        return self.widget

    def _on_theme_changed(self, *_args):
        """Re-apply colours without re-running any subprocess detection."""
        try:
            if hasattr(self, "_style_toolenv"):
                self._style_toolenv()
            if hasattr(self, "_refresh_r_status"):
                self._refresh_r_status()
        except Exception as e:
            logger.debug(f"Theme refresh failed for external tools panel: {e}")
