"""Reusable dialogs split from src/ui/components/dialog.py by functional domain.

惰性再导出（PEP 562）
--------------------
本包是 ``src/ui/components/dialog.py`` 拆分后的落点，被主窗口与各工具面板的
导入链引用。旧的 ``__init__`` 在导入期一次性拉起全部子模块，而其中
``kb_project_dialogs``（→ ``core.models_registry`` → torch/chromadb）、
``mcp_skill_dialogs``（→ pygments）等重依赖会被顺带加载——只要有任何代码
``import`` 本包下的任一符号，主窗口就要多等 1.5 s 以上。

现在改为按符号惰性导入：启动路径只加载真正用到的子模块，其余等首次访问
（``from src.ui.components.dialogs import ProjectEditorDialog``）时才导入。
"""
from importlib import import_module

#: 导出符号 → (子模块名, 属性名)。与下面的 __all__ 保持一致。
_EXPORTS = {
    "BaseDialog": ("base", "BaseDialog"),
    "HAS_NVML": ("base", "HAS_NVML"),
    "StandardDialog": ("common", "StandardDialog"),
    "ProgressDialog": ("common", "ProgressDialog"),
    "UnsavedChangesDialog": ("common", "UnsavedChangesDialog"),
    "ExportPasswordDialog": ("common", "ExportPasswordDialog"),
    "ImportPasswordDialog": ("common", "ImportPasswordDialog"),
    "AddModelDialog": ("common", "AddModelDialog"),
    "FeedEditorDialog": ("feed_dialogs", "FeedEditorDialog"),
    "FeedLibraryDialog": ("feed_dialogs", "FeedLibraryDialog"),
    "SelectKBFileDialog": ("kb_project_dialogs", "SelectKBFileDialog"),
    "ProjectEditorDialog": ("kb_project_dialogs", "ProjectEditorDialog"),
    "McpConfigDialog": ("mcp_skill_dialogs", "McpConfigDialog"),
    "SkillConfigDialog": ("mcp_skill_dialogs", "SkillConfigDialog"),
    "SkillSecurityAnalyzer": ("mcp_skill_dialogs", "SkillSecurityAnalyzer"),
    "PythonHighlighter": ("mcp_skill_dialogs", "PythonHighlighter"),
    "SkillPreviewDialog": ("mcp_skill_dialogs", "SkillPreviewDialog"),
    "ApiProvidersDialog": ("about_dialogs", "ApiProvidersDialog"),
    "LicenseDialog": ("about_dialogs", "LicenseDialog"),
    "ReleaseNotesDialog": ("about_dialogs", "ReleaseNotesDialog"),
}

__all__ = [
    "BaseDialog",
    "HAS_NVML",
    "StandardDialog",
    "ProgressDialog",
    "UnsavedChangesDialog",
    "ExportPasswordDialog",
    "ImportPasswordDialog",
    "AddModelDialog",
    "FeedEditorDialog",
    "FeedLibraryDialog",
    "SelectKBFileDialog",
    "ProjectEditorDialog",
    "McpConfigDialog",
    "SkillConfigDialog",
    "SkillSecurityAnalyzer",
    "PythonHighlighter",
    "SkillPreviewDialog",
    "ApiProvidersDialog",
    "LicenseDialog",
    "ReleaseNotesDialog",
]


def __getattr__(name: str):
    """按需导入符号所属的子模块，并缓存到本模块命名空间。"""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    module = import_module(f".{module_name}", __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
