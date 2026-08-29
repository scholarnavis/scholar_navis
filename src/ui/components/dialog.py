"""Compatibility layer: dialogs split into the ``dialogs/`` sub-package.

All dialog classes moved to :mod:`src.ui.components.dialogs`; this module
re-exports every symbol so legacy imports
(``from src.ui.components.dialog import X``) keep working.
"""

from src.ui.components.dialogs.base import HAS_NVML, BaseDialog
from src.ui.components.dialogs.common import (AddModelDialog,
                                              ExportPasswordDialog,
                                              ImportPasswordDialog,
                                              ProgressDialog,
                                              StandardDialog,
                                              UnsavedChangesDialog)
from src.ui.components.dialogs.feed_dialogs import FeedEditorDialog, FeedLibraryDialog
from src.ui.components.dialogs.kb_project_dialogs import (ProjectEditorDialog,
                                                          SelectKBFileDialog)
from src.ui.components.dialogs.mcp_skill_dialogs import (McpConfigDialog,
                                                         PythonHighlighter,
                                                         SkillConfigDialog,
                                                         SkillPreviewDialog,
                                                         SkillSecurityAnalyzer)
from src.ui.components.dialogs.about_dialogs import ApiProvidersDialog, LicenseDialog

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
]
