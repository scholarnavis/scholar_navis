"""Compatibility layer: dialogs split into the ``dialogs/`` sub-package.

All dialog classes moved to :mod:`src.ui.components.dialogs`; this module
re-exports every symbol so legacy imports
(``from src.ui.components.dialog import X``) keep working.

再导出同样是**惰性**的（PEP 562）：本模块被主窗口与各工具面板在导入期引用，
若在此处就把全部对话框子模块拉起来，会连带 ``kb_project_dialogs`` →
``core.models_registry`` → torch/chromadb 以及 pygments 等重依赖，主窗口要推迟
数秒才出现。符号与子模块的对应关系集中在 :mod:`src.ui.components.dialogs`，
这里只做按需转发。
"""
from src.ui.components import dialogs as _dialogs

__all__ = list(_dialogs.__all__)


def __getattr__(name: str):
    """按需向 ``dialogs`` 包取符号，并缓存到本模块命名空间。"""
    try:
        value = getattr(_dialogs, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    globals()[name] = value
    return value
