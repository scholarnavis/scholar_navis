"""生信环境目录布局（唯一来源）。

    <BASE_DIR>/bioenv/bin        私有引导工具（micromamba）
    <BASE_DIR>/bioenv/cache      下载缓存与 conda 包缓存
    <BASE_DIR>/bioenv/prefixes   按工具族划分的私有前缀，每个前缀一份 meta.json
    <BASE_DIR>/bioenv/logs       每次前缀创建与分析运行的完整日志
    <BASE_DIR>/bioenv/work       分析工作目录：inbox / work / outbox
    <BASE_DIR>/bioenv/catalog.json   用户自定义清单（覆盖内置条目的补充来源）

为什么不与 ``r_env`` 合并：两者的库路径语义不同（``R_LIBS_USER`` 与可执行
``PATH``），合并会让注入逻辑必须按环境类型分支，反而更容易出错。共用的是
:mod:`src.core.toolchain.micromamba` 那一层引导实现，不是目录。
"""

from __future__ import annotations

import logging
import os

from src.core.platform_env import app_root

logger = logging.getLogger("BioEnv.Paths")

#: 私有环境目录名（相对 :func:`src.core.platform_env.app_root`）。
ENV_DIR_NAME = "bioenv"

_BIN_SUBDIR = "bin"
_CACHE_SUBDIR = "cache"
_PREFIX_SUBDIR = "prefixes"
_LOGS_SUBDIR = "logs"
_WORK_SUBDIR = "work"
_RUNS_SUBDIR = "runs"
_HELP_SUBDIR = "help"

#: 工作目录的三个约定位置：主程序只写 inbox、只读 outbox，中间产物留在 work。
WORK_SUBDIRS = ("inbox", "work", "outbox")


def env_root() -> str:
    """私有环境根目录（``<BASE_DIR>/bioenv``）。"""
    return os.path.join(app_root(), ENV_DIR_NAME)


def bin_dir() -> str:
    """私有引导工具目录（micromamba 等）。"""
    return os.path.join(env_root(), _BIN_SUBDIR)


def cache_dir() -> str:
    """下载缓存与 conda 包缓存目录。"""
    return os.path.join(env_root(), _CACHE_SUBDIR)


def prefixes_dir() -> str:
    """私有前缀根目录，其下每个子目录对应一份独立的 conda 环境。"""
    return os.path.join(env_root(), _PREFIX_SUBDIR)


def logs_dir() -> str:
    """前缀创建与分析运行的日志目录。"""
    return os.path.join(env_root(), _LOGS_SUBDIR)


def runs_dir() -> str:
    """单次工具执行的完整现场：命令、stdout、stderr、结果元数据。

    与 ``logs`` 分开：日志是给人看的流水，运行现场是给"这次结果怎么来的"做核查
    用的，两类的保留策略不同。
    """
    return os.path.join(env_root(), _RUNS_SUBDIR)


def help_dir() -> str:
    """工具帮助信息的缓存目录。

    每工具一份，随可执行文件的修改时间失效：工具升级后第一次调用会自动重抓，
    不需要用户手动清理。
    """
    return os.path.join(env_root(), _HELP_SUBDIR)


def work_root() -> str:
    """分析工作目录根。"""
    return os.path.join(env_root(), _WORK_SUBDIR)


def work_dir(name: str) -> str:
    """某个约定工作位置（``inbox`` / ``work`` / ``outbox``）的绝对路径。"""
    return os.path.join(work_root(), name)


def catalog_override_path() -> str:
    """用户自定义清单的落点。"""
    return os.path.join(env_root(), "catalog.json")


def ensure_dirs() -> None:
    """建齐目录（幂等，失败只记日志不抛）。

    工作目录的三个子目录一并建好：后续写入方不必各自判断"父目录在不在"。
    """
    targets = [bin_dir(), cache_dir(), prefixes_dir(), logs_dir(),
               runs_dir(), help_dir(), work_root()]
    targets += [work_dir(name) for name in WORK_SUBDIRS]
    for path in targets:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            logger.warning(f"Cannot create bioenv directory {path}: {e}")


def environment_size_bytes() -> int:
    """私有环境的磁盘占用，用于设置页展示"是否值得清理"。"""
    total = 0
    root = env_root()
    if not os.path.isdir(root):
        return 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
    return total
