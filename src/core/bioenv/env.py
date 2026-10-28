"""生信子进程的环境变量（唯一构造点）。

两种来源，两套环境，差别只在要不要注入前缀路径：

* **系统安装**（用户自己装的，或发行版/conda 装的）——沿用当前进程的 ``PATH``，
  只清掉会把应用自身运行时带进子进程的 Python 变量。不动 ``CONDA_PREFIX`` 与
  ``VIRTUAL_ENV``：用户可能正是从某个 conda 环境启动的应用，那条 ``PATH`` 才是
  工具所在的位置。
* **私有前缀**——把前缀的可执行目录前置到 ``PATH``、``lib`` 前置到
  ``LD_LIBRARY_PATH``。

两条路都**不动 ``HOME``**：fontconfig 与部分工具的用户级配置依赖它，改掉的收益
小于风险。所有生信子进程必须经这里取环境，不允许各自 ``os.environ.copy()``。
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional

from src.core.platform_env import is_windows

logger = logging.getLogger("BioEnv.Env")

#: 必须从子进程环境里剔除的变量：它们会把应用自带的解释器与 site-packages 带进
#: 工具进程，表现为难以定位的导入冲突或版本错配。
_DROP_VARS = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP")

#: Windows 前缀内的可执行目录（conda 布局把包与工具分处不同目录）。
_WIN_PATH_SUBDIRS = ("", "Library/mingw-w64/bin", "Library/bin", "Scripts", "bin")


def _base(base: Optional[Dict[str, str]], tmpdir: str = "", timezone: str = "") -> Dict[str, str]:
    """构造基础环境：复制、剔除干扰变量、按需写入临时目录与时区。"""
    env = dict(base if base is not None else os.environ)
    for name in _DROP_VARS:
        env.pop(name, None)
    if timezone:
        env["TZ"] = timezone
    if tmpdir:
        for key in ("TMPDIR", "TEMP", "TMP"):
            env[key] = tmpdir
    return env


def for_system(base: Optional[Dict[str, str]] = None, tmpdir: str = "",
               timezone: str = "") -> Dict[str, str]:
    """系统安装的工具用的环境：沿用调用方的 ``PATH``，不做任何前置。"""
    return _base(base, tmpdir=tmpdir, timezone=timezone)


def for_prefix(prefix_dir: str, prefix_name: str = "",
               base: Optional[Dict[str, str]] = None,
               tmpdir: str = "", timezone: str = "") -> Dict[str, str]:
    """私有前缀内的工具用的环境：把前缀路径前置到 ``PATH``。

    :param prefix_dir: 前缀根目录的绝对路径。
    """
    env = _base(base, tmpdir=tmpdir, timezone=timezone)
    if is_windows():
        extra = [os.path.join(prefix_dir, sub) if sub else prefix_dir
                 for sub in _WIN_PATH_SUBDIRS]
    else:
        extra = [os.path.join(prefix_dir, "bin")]

    current = env.get("PATH", "")
    env["PATH"] = os.pathsep.join([p for p in extra if p] + ([current] if current else []))
    env["CONDA_PREFIX"] = prefix_dir
    if prefix_name:
        env["CONDA_DEFAULT_ENV"] = str(prefix_name)

    if not is_windows():
        lib = os.path.join(prefix_dir, "lib")
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = lib + (os.pathsep + existing if existing else "")
    return env


def describe(env: Dict[str, str]) -> str:
    """把环境里与本层相关的部分渲染成一行，供日志记录实际生效的注入。"""
    head = env.get("PATH", "").split(os.pathsep)[:3]
    return (f"PATH[:3]={head} CONDA_PREFIX={env.get('CONDA_PREFIX', '')} "
            f"LD_LIBRARY_PATH={env.get('LD_LIBRARY_PATH', '')[:80]}")
