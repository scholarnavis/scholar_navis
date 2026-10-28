"""私有 conda 前缀的生命周期与子进程环境注入。

为什么不移动已创建的前缀
----------------------
conda 前缀内大量文件硬编码了自身绝对路径（脚本的 shebang、部分工具的配置模板），
因此"先建到临时目录再 rename"会让前缀在改名后失效。这里一律原地创建，失败即保留
残骸：``meta.json`` 不写入，:func:`is_ready` 因此判为未就绪，下次创建会在同一路径
上做增量求解，已满足的包直接跳过。这同时也让"装到一半失败再重试"成为正常路径。

环境注入只有一处
----------------
所有生信子进程都经 :func:`child_env` 取环境变量。它只做三件事：把前缀的可执行
目录前置到 ``PATH``、把 ``lib`` 前置到 ``LD_LIBRARY_PATH``（POSIX）、清理可能干扰
子进程的 Python 变量。**刻意不动 ``HOME``**：R 的绘图链路已确认 fontconfig 依赖它
找用户字体，生信工具同样可能读取用户级配置，改掉它的收益远小于风险。
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from typing import Callable, Dict, List, Optional, Sequence

from src.core.bioenv import catalog as catalog_mod
from src.core.bioenv import env as env_mod
from src.core.bioenv import paths
from src.core.platform_env import is_windows
from src.core.safe_io import atomic_write_json
from src.core.toolchain import micromamba as mamba
from src.core.toolchain import probe

logger = logging.getLogger("BioEnv.Prefix")

#: 前缀元数据文件名：存在且可执行文件齐全才算就绪。
META_NAME = "meta.json"

#: 求解失败时最多重试几次（每次剔掉一个通道内不存在的包）。
_MAX_SOLVE_ATTEMPTS = 3

# 子进程环境的构造在 :mod:`src.core.bioenv.env`，本模块只决定"用哪个前缀"。


# --------------------------------------------------------------------------- #
#  路径
# --------------------------------------------------------------------------- #
def prefix_dir(prefix_name: str) -> str:
    """某个前缀的绝对路径。"""
    return os.path.join(paths.prefixes_dir(), str(prefix_name))


def meta_path(prefix_name: str) -> str:
    """前缀元数据文件路径。"""
    return os.path.join(prefix_dir(prefix_name), META_NAME)


def _read_meta(prefix_name: str) -> Optional[dict]:
    """读取前缀元数据；不存在或不可解析时返回 None。"""
    import json

    path = meta_path(prefix_name)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Cannot read prefix metadata {path}: {e}")
        return None
    return data if isinstance(data, dict) else None


def is_ready(prefix_name: str) -> bool:
    """前缀是否已就绪（只看元数据与目录，不联网、不启动子进程）。"""
    if not os.path.isdir(prefix_dir(prefix_name)):
        return False
    return _read_meta(prefix_name) is not None


def list_prefixes() -> List[str]:
    """已创建的前缀名（按目录列举，不依赖清单）。"""
    root = paths.prefixes_dir()
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return sorted(n for n in names if os.path.isdir(os.path.join(root, n)))


def remove(prefix_name: str) -> bool:
    """删除前缀（失败只记日志，不抛）。"""
    target = prefix_dir(prefix_name)
    if not os.path.isdir(target):
        return True
    try:
        shutil.rmtree(target)
        logger.info(f"Prefix removed: {target}")
        return True
    except OSError as e:
        logger.error(f"Cannot remove prefix {target}: {e}")
        return False


def size_bytes(prefix_name: str) -> int:
    """前缀的磁盘占用（用于设置页展示）。"""
    total = 0
    target = prefix_dir(prefix_name)
    if not os.path.isdir(target):
        return 0
    for dirpath, _dirnames, filenames in os.walk(target):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
    return total


# --------------------------------------------------------------------------- #
#  可执行文件与环境
# --------------------------------------------------------------------------- #
def tool_executable(prefix_name: str, entry: catalog_mod.ToolEntry,
                    platform: str = "") -> str:
    """某个工具在前缀内的可执行文件路径（不存在时返回空串）。

    Windows 上清单可以只写 ``bin/tool``：实际产物常带 ``.exe``，因此按候选顺序
    探测而不是要求清单为每个平台写全后缀。
    """
    plat = platform or mamba.runtime_platform()
    rel = entry.executable_relpath(plat)
    base = prefix_dir(prefix_name)
    candidates = [os.path.join(base, rel)]
    if is_windows() and not rel.lower().endswith(".exe"):
        candidates.append(os.path.join(base, rel + ".exe"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return ""


def child_env(prefix_name: str, base: Optional[Dict[str, str]] = None,
              tmpdir: str = "", timezone: str = "") -> Dict[str, str]:
    """前缀内工具的进程环境（委托 :func:`src.core.bioenv.env.for_prefix`）。

    :param tmpdir: 非空时把 ``TMPDIR`` / ``TEMP`` / ``TMP`` 指向它，让中间文件落在
        受控目录里，跑完即可整体清除。
    :param timezone: 非空时写入 ``TZ``；涉及日期与时间统计的工具需要固定时区。
    """
    return env_mod.for_prefix(prefix_dir(prefix_name), prefix_name,
                              base=base, tmpdir=tmpdir, timezone=timezone)


# --------------------------------------------------------------------------- #
#  创建与校验
# --------------------------------------------------------------------------- #
def ensure_micromamba(*, log_cb: Optional[Callable[[str], None]] = None,
                      progress_cb: Optional[Callable[[int, str], None]] = None,
                      cancel_cb: Optional[Callable[[], bool]] = None) -> dict:
    """确保私有 micromamba 可用（已存在则直接返回）。"""
    paths.ensure_dirs()

    def _scaled(pct: int, text: str) -> None:
        if progress_cb:
            progress_cb(min(10, int(pct * 10 / 100)), text)

    return mamba.download(paths.bin_dir(), paths.cache_dir(),
                          log_cb=log_cb, progress_cb=_scaled, cancel_cb=cancel_cb)


def create(prefix_name: str, packages: Sequence[str], *,
           channels: Sequence[str] = ("bioconda", "conda-forge"),
           tools: Sequence[catalog_mod.ToolEntry] = (),
           log_cb: Optional[Callable[[str], None]] = None,
           progress_cb: Optional[Callable[[int, str], None]] = None,
           cancel_cb: Optional[Callable[[], bool]] = None) -> dict:
    """创建（或增量补齐）一个私有前缀。

    :param packages: 需要安装的 conda 包名。
    :param tools:    写入元数据的工具条目快照，供就绪判断与验证使用。
    :return: ``{"success", "cancelled", "error", "skipped"}``
    """
    result: Dict[str, object] = {"success": False, "cancelled": False,
                                 "error": "", "skipped": []}
    wanted = [str(p).strip() for p in packages if str(p).strip()]
    if not wanted:
        result["error"] = "no package requested"
        logger.error(f"Prefix '{prefix_name}' requested with no packages")
        return result

    got = ensure_micromamba(log_cb=log_cb, progress_cb=progress_cb, cancel_cb=cancel_cb)
    if got.get("cancelled"):
        result["cancelled"] = True
        result["error"] = "cancelled by user"
        return result
    if not got.get("success"):
        result["error"] = f"micromamba unavailable: {got.get('error')}"
        logger.error(result["error"])
        return result

    mamba_exe = mamba.find_binary(paths.bin_dir())
    root = prefix_dir(prefix_name)
    paths.ensure_dirs()
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as e:
        result["error"] = f"cannot create prefix directory: {e}"
        logger.error(result["error"])
        return result

    log_path = os.path.join(paths.logs_dir(),
                            f"prefix-{prefix_name}-{time.strftime('%Y%m%d-%H%M%S')}.log")
    channel_args: List[str] = []
    for channel in channels:
        channel_args += ["-c", str(channel)]

    remaining = list(wanted)
    solved = False
    out: Dict[str, object] = {"code": -1, "cancelled": False, "tail": []}
    for attempt in range(_MAX_SOLVE_ATTEMPTS):
        cmd = [mamba_exe, "create", "-y", "-p", root, "--no-rc"] + channel_args + remaining
        if log_cb:
            log_cb(f"Creating prefix '{prefix_name}' "
                   f"({len(remaining)} package(s), attempt {attempt + 1})")
            log_cb("Command: " + " ".join(cmd))
        logger.info(f"Creating prefix '{prefix_name}' with {len(remaining)} package(s)")

        ticks = {"n": 0}

        def _on_line(line: str) -> None:
            text = line.strip()
            if not text.startswith(("Downloading", "Extracting", "Linking")):
                return
            ticks["n"] += 1
            if progress_cb:
                progress_cb(min(90, 10 + ticks["n"]), text[:200])

        out = mamba.stream_command(
            cmd, env=mamba.prefix_env(paths.cache_dir()), log_cb=log_cb,
            line_cb=_on_line, cancel_cb=cancel_cb, log_path=log_path)

        if out["cancelled"]:
            result["cancelled"] = True
            result["error"] = "cancelled by user"
            return result
        if out["code"] == 0:
            solved = True
            break

        bad = mamba.unsatisfiable_package(out["tail"])
        if not bad or bad in remaining or len(remaining) <= 1:
            result["error"] = next((l.strip() for l in reversed(out["tail"]) if l.strip()),
                                   f"micromamba exited {out['code']}")
            if log_cb:
                log_cb(f"Prefix creation failed: {result['error']}")
                log_cb(f"Log: {log_path}")
            logger.error(f"Prefix '{prefix_name}' creation failed: {result['error']}")
            return result

        remaining = [p for p in remaining if p != bad]
        result["skipped"].append(bad)
        if log_cb:
            log_cb(f"Package {bad} is unavailable; retrying without it.")

    if not solved:
        result["error"] = next((l.strip() for l in reversed(out["tail"]) if l.strip()),
                               f"micromamba exited {out['code']}")
        logger.error(f"Prefix '{prefix_name}' creation failed after retries: {result['error']}")
        return result

    try:
        atomic_write_json(meta_path(prefix_name), {
            "schema_version": 1,
            "prefix": prefix_name,
            "platform": mamba.runtime_platform(),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "packages": remaining,
            "requested": wanted,
            "skipped": list(result["skipped"]),
            "channels": list(channels),
            "mamba_version": (mamba.verify(mamba_exe, paths.cache_dir()) or {}).get("version", ""),
            "tools": {entry.name: {"conda_name": entry.conda_name,
                                   "version_args": list(entry.version_args)}
                      for entry in tools},
        })
    except OSError as e:
        # 元数据写不进去就等于"没装好"：is_ready 会一直为假，下次重试仍在同一路径
        # 增量求解，不会静默放过一个不可用的前缀。
        result["error"] = f"cannot write prefix metadata: {e}"
        logger.error(result["error"])
        return result

    result["success"] = True
    if progress_cb:
        progress_cb(95, f"Prefix '{prefix_name}' ready")
    logger.info(f"Prefix '{prefix_name}' created at {root} "
                f"(skipped: {len(result['skipped'])})")
    return result


def verify(prefix_name: str, tools: Sequence[catalog_mod.ToolEntry],
           platform: str = "") -> Dict[str, dict]:
    """逐个真实运行前缀内的工具，确认它们可用并读出版本。

    :return: ``{tool_name: {"available", "version", "detail"}}``
    """
    plat = platform or mamba.runtime_platform()
    env = child_env(prefix_name)
    report: Dict[str, dict] = {}
    for entry in tools:
        if not entry.available_on(plat):
            report[entry.name] = {"available": False, "version": "",
                                  "detail": f"not supported on {plat}"}
            continue
        executable = tool_executable(prefix_name, entry, plat)
        if not executable:
            report[entry.name] = {"available": False, "version": "",
                                  "detail": "executable missing from prefix"}
            logger.warning(f"Tool '{entry.name}' has no executable in prefix '{prefix_name}'")
            continue
        report[entry.name] = probe.probe(executable, entry.version_args, env=env)
    return report
