"""应用包根：``BASE_DIR`` 作为可写数据（config / logs / models / output …）的基准目录。

"冻结产物 or 源码运行"的布局判断集中在 :mod:`src.core.platform_env` 的
:func:`~src.core.platform_env.app_root`，不再由本文件、打包脚本与主题资源解析
各自维护一份（三份实现曾出现 macOS ``.app`` 包内路径处理不一致的问题）。
"""

from src.core.platform_env import app_root

BASE_DIR = app_root()
