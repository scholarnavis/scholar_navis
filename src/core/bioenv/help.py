"""工具帮助信息的抓取与缓存。

为什么先看 help 再执行
--------------------
封装层把参数语义化，但工具的标志随版本漂移：同一个子命令在不同版本里可能改名、
移除或改变默认值。执行前先落一份 help，等于把"这一版工具的接口长什么样"固定在
磁盘上——封装用到的标志是否还存在、模型想了解细节时看什么，都有据可查。

缓存失效只认一个判据
--------------------
可执行文件的修改时间晚于 help 文件，就重新抓。工具升级后第一次调用自动刷新，
不需要用户手动清理；WSL 落点的可执行文件不在宿主上，无法比较时间，因此只在
``force`` 时重抓。

不阻断是刻意的
--------------
不少工具没有 ``--help``（或帮助返回非零退出码）。这类情况只记日志、不返回失败：
把"帮助抓不到"升级成"工具不可用"，会让本来能跑的工具被误判为不可用。
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional, Tuple

from src.core.bioenv import catalog as catalog_mod
from src.core.bioenv import paths
from src.core.bioenv import resolver
from src.core.bioenv import runner
from src.core.safe_io import atomic_write_text

logger = logging.getLogger("BioEnv.Help")

#: 单次帮助输出的读取上限。帮助文本通常几十 KB，超过这个量基本是工具在打印用法
#: 之外的诊断信息，再读下去没有意义。
MAX_HELP_BYTES = 512 * 1024

#: 单次抓取的超时。
DEFAULT_TIMEOUT = 30.0


def help_path(tool_name: str) -> str:
    """某个工具的帮助缓存文件路径（不论是否存在）。"""
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(tool_name))
    return os.path.join(paths.help_dir(), f"{safe}.txt")


def _read_capped(path: str) -> str:
    """读取文本文件的前 ``MAX_HELP_BYTES`` 字节（解码失败按替换处理）。"""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_HELP_BYTES)
    except OSError as e:
        logger.warning(f"Cannot read {path}: {e}")
        return ""
    return data.decode("utf-8", "replace")


def _size(text: str) -> Dict[str, int]:
    """帮助文本的体量（供返回值与日志使用）。"""
    return {"chars": len(text), "lines": text.count("\n") + 1 if text else 0}


def _is_fresh(target: str, location: resolver.ToolLocation) -> bool:
    """缓存是否仍然对应当前这个可执行文件。"""
    if not os.path.isfile(target):
        return False
    if location.source == "wsl":
        # 发行版内的文件时间拿不到，只能信任已有缓存，需要时走 force。
        return True
    try:
        return os.path.getmtime(target) >= os.path.getmtime(location.path)
    except OSError:
        return False


def ensure_help(location: resolver.ToolLocation, entry: catalog_mod.ToolEntry,
                *, force: bool = False,
                timeout: float = DEFAULT_TIMEOUT) -> dict:
    """确保该工具的帮助信息已落到缓存，返回观察记录。

    :return: ``{"observed", "cached", "path", "chars", "lines", "reason"}``
    """
    record: Dict[str, object] = {"observed": False, "cached": False, "path": "",
                                "chars": 0, "lines": 0, "reason": ""}
    args: Tuple[str, ...] = tuple(entry.help_args)
    if not args:
        record["reason"] = "declared as not supporting help"
        logger.debug(f"Help observation skipped for '{entry.name}': declared unsupported")
        return record

    target = help_path(entry.name)
    if not force and _is_fresh(target, location):
        text = _read_capped(target)
        record.update(observed=True, cached=True, path=target, **_size(text))
        return record

    result = runner.run(location, [location.path, *args], timeout=timeout)
    text = _read_capped(result.stdout_path) + _read_capped(result.stderr_path)
    if not text.strip():
        record["reason"] = f"no help output (exit code {result.returncode})"
        logger.info(f"Help observation produced nothing for '{entry.name}': "
                    f"{record['reason']}")
        return record

    try:
        os.makedirs(paths.help_dir(), exist_ok=True)
        atomic_write_text(target, text)
    except OSError as e:
        record["reason"] = f"cannot write help cache: {e}"
        logger.warning(record["reason"])
        return record

    record.update(observed=True, path=target, **_size(text))
    logger.info(f"Help observed for '{entry.name}' "
                f"({record['chars']} chars) -> {target}")
    return record


def read_lines(tool_name: str, limit: int = 80) -> List[str]:
    """读取已缓存的帮助文本的前若干行（未缓存时返回空列表）。"""
    text = _read_capped(help_path(tool_name))
    if not text:
        return []
    return text.splitlines()[:max(1, int(limit))]


def total_lines(tool_name: str) -> int:
    """已缓存的帮助文本行数（未缓存返回 0）。"""
    text = _read_capped(help_path(tool_name))
    return text.count("\n") + 1 if text else 0


def cached_tools() -> List[str]:
    """已经抓过帮助的工具名（按缓存文件名反推）。"""
    root = paths.help_dir()
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return sorted(os.path.splitext(n)[0] for n in names if n.endswith(".txt"))


def available(location: resolver.ToolLocation,
              entry: catalog_mod.ToolEntry) -> Optional[dict]:
    """不做任何动作，只报告缓存现状（供设置页展示）。"""
    target = help_path(entry.name)
    if not os.path.isfile(target):
        return None
    text = _read_capped(target)
    return {"path": target, **_size(text),
            "fresh": _is_fresh(target, location)}
