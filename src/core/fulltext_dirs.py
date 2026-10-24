"""取全文窗口的监听目录（**唯一定义处**）。

两类目录，来源不同，别混着用：

* **内置收件目录**：应用自己的兜底投递口（``~/.scholar_navis/fulltext_inbox``），
  窗口里会打印它的路径，始终监听；
* **额外监听目录**：用户在"设置 → 获取全文"里维护的列表。默认值是系统下载目录
  （旧版本硬编码的下载目录行为，现在变成一份可编辑的默认值）。

"两个路径是不是同一个目录"只有 :func:`dir_identity` 一处判断（列表去重与收件目录
判定都走它）。扫描成本与目录条目数成正比，所以这里有硬上限 :data:`MAX_WATCH_DIRS`；
扫描端（:mod:`src.ui.components.fulltext_request`）只做"列名 + 差集"，不逐文件 stat。
"""
import logging
import os
from typing import Iterable, List, Tuple, Union

from PySide6.QtCore import QStandardPaths

logger = logging.getLogger(__name__)

__all__ = [
    "INBOX_DIRNAME",
    "MAX_WATCH_DIRS",
    "inbox_dir",
    "downloads_dir",
    "dir_identity",
    "default_watch_dirs",
    "normalize_dirs",
    "configured_dirs",
    "watch_dirs",
]

#: 兜底收件目录名：把 PDF 直接拷进来，窗口会扫描并自动认领。
INBOX_DIRNAME = "fulltext_inbox"
#: 可额外监听的目录上限：每轮扫描都要把每个目录列一遍，数量直接决定成本。
MAX_WATCH_DIRS = 8


def inbox_dir() -> str:
    """内置收件目录：不便用"选择文件"时，用户直接把 PDF 放进来。

    放在用户主目录下而非应用目录：打包后的应用目录可能只读，而 PDF 本来就来自用户
    自己的下载目录。
    """
    return os.path.join(os.path.expanduser("~"), ".scholar_navis", INBOX_DIRNAME)


def downloads_dir() -> str:
    """系统下载目录（浏览器默认落盘处）；无法确定时返回空串。"""
    return QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.DownloadLocation) or ""


def dir_identity(path: str) -> Union[Tuple[int, int], str]:
    """目录的"同一性"键：优先文件系统给的 ``(设备号, inode)``，拿不到才退回路径。

    为什么不直接比 ``realpath`` 字符串：Windows 与 macOS 的默认文件系统**大小写不
    敏感**，``C:\\Users\\Me\\Downloads`` 与 ``c:\\users\\me\\downloads`` 是同一个目录，
    而 ``realpath`` 保留原样大小写，会被判成两个——同一目录扫两遍，候选列表里还会
    出现两份。设备号 + inode 是文件系统自己给的身份，比"猜平台的大小写规则"可靠。

    退回路径时用 ``normcase``：Windows 上折叠大小写与分隔符，POSIX 上原样返回——
    Linux 上 ``/Data`` 与 ``/data`` 确实是两个目录，不能合并。
    """
    real = os.path.realpath(path)
    try:
        info = os.stat(real)
        inode = int(getattr(info, "st_ino", 0) or 0)
        if inode:  # FAT/exFAT 与部分网络盘会报 0，此时 inode 不足以当身份
            return (int(info.st_dev), inode)
    except (OSError, ValueError) as e:
        logger.debug("Cannot stat %s for identity, falling back to the path: %s", path, e)
    return os.path.normcase(real)


def normalize_dirs(paths: Iterable[str]) -> List[str]:
    """展开 ``~``、转绝对路径、按 :func:`dir_identity` 去重，并保持原有顺序。

    去重按目录身份而非字符串：软链接、相对路径，以及 Windows/macOS 上的大小写差异，
    都会让同一个目录被扫两遍（白花开销）并在候选列表里出现两份（观感）。设置界面与
    扫描端都调这一个函数，口径不会分叉。
    """
    if isinstance(paths, str):  # 单个路径：当成只有一项，避免被按字符拆开
        paths = [paths]
    elif not isinstance(paths, (list, tuple, set)):
        if paths is not None:
            logger.warning("Expected a list of directories, got %s; ignored.",
                           type(paths).__name__)
        return []

    out: List[str] = []
    seen = set()
    for raw in paths:
        text = str(raw or "").strip()
        if not text:
            continue
        path = os.path.abspath(os.path.expanduser(text))
        key = dir_identity(path)
        if key in seen:
            logger.debug("Duplicate fulltext watch dir ignored: %s", path)
            continue
        seen.add(key)
        out.append(path)
    return out


def default_watch_dirs() -> List[str]:
    """首次运行时写进设置的默认值：系统下载目录（取不到时为空表，不猜路径）。"""
    return normalize_dirs([downloads_dir()])


def configured_dirs() -> List[str]:
    """用户在设置里维护的额外监听目录（规范化后，已按上限截断）。"""
    try:
        from src.core.config_manager import ConfigManager

        raw = ConfigManager().get_fulltext_watch_dirs()
    except Exception as e:  # 配置尚未装载（启动早期）不该让取全文窗口崩掉
        logger.debug("Cannot read fulltext_watch_dirs: %s", e)
        return []
    dirs = normalize_dirs(raw)
    if len(dirs) > MAX_WATCH_DIRS:
        logger.warning("fulltext_watch_dirs holds %d entries; only the first %d are watched.",
                       len(dirs), MAX_WATCH_DIRS)
        dirs = dirs[:MAX_WATCH_DIRS]
    return dirs


def watch_dirs() -> List[str]:
    """本次开窗要扫描的全部目录：内置收件目录在前，其后是设置里的额外目录。"""
    return normalize_dirs([inbox_dir()] + configured_dirs())