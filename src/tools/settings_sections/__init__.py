"""Settings sections: functional mixins split from src/tools/settings_tool.py.

每个 Mixin 只负责一个功能域，运行时全部挂载到同一个 SettingsTool 实例上。
"""
from src.tools.settings_sections.env_section import EnvSectionMixin
from src.tools.settings_sections.mcp_section import McpSectionMixin
from src.tools.settings_sections.llm_section import LlmSectionMixin
from src.tools.settings_sections.llm_model_ops import LlmModelOpsMixin
from src.tools.settings_sections.model_section import ModelSectionMixin
from src.tools.settings_sections.save_flow import SaveFlowMixin
from src.tools.settings_sections.transfer import ConfigTransferMixin

__all__ = [
    "EnvSectionMixin",
    "McpSectionMixin",
    "LlmSectionMixin",
    "LlmModelOpsMixin",
    "ModelSectionMixin",
    "SaveFlowMixin",
    "ConfigTransferMixin",
]
