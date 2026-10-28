"""
R Private Runtime
=================
按需下载一个**独立于系统**的 R 运行时到 ``<BASE_DIR>/r_env/runtime``。

为什么不是"把 R 打进安装包"：R 本体是 GPL 系自由软件（实测 4.6.1 自带 COPYING
为 GPL-2），以子进程方式调用属于聚合分发，随包分发在许可上可行；真正的障碍是
体积与平台覆盖——R 官方 Windows 安装包 87.5 MB（实测），macOS 同量级，而 Linux
**没有**官方便携二进制（各发行版自行构建）。因此内置不划算，改为"用到时才下载
一个私有运行时"，只在机器上确实没有可用 R 时才发生。

实现走 conda-forge 的预编译包：R 本体与绘图所需的 ``r-*`` 包都是现成二进制，
不需要编译器、不需要管理员权限，也绕开了发行版差异（NixOS 只读 store 等）。
非 FHS 系统上 conda 产物依赖兼容层（NixOS 的 nix-ld 已为
``/lib64/ld-linux-x86-64.so.2`` 建立入口），因此创建完成后会**真实运行一次 R**
做验证，失败即如实报错，不做"看起来装好了"的假成功。

micromamba 的引导（下载、解包、真实运行校验、流式执行）已下沉到
:mod:`src.core.toolchain.micromamba`，与生信工具前缀共用同一份实现；本模块只保留
R 专有的部分：用哪个前缀、装哪些包、装完怎么验证。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from typing import Callable, Dict, List, Optional, Sequence

from src.core import r_env
from src.core.platform_env import is_windows, no_window_flags
from src.core.toolchain import micromamba as mamba

logger = logging.getLogger("Core.RRuntime")

#: conda-forge 通道；预编译 ``r-*`` 包即来源于此。
_CONDA_CHANNEL = "conda-forge"

#: 约定：CRAN 包 ``ggplot2`` 在 conda-forge 上名为 ``r-ggplot2``（全小写）。
_CONDA_PREFIX = "r-"

#: 运行时里必须存在的包（校验运行时自身可用）。
_BASE_CONDA_PACKAGES = ("r-base",)

#: micromamba 的实现统一在 :mod:`src.core.toolchain.micromamba`，这里只保留入口名。
runtime_platform = mamba.runtime_platform


def micromamba_binary_path() -> str:
    """私有 micromamba 的落点路径，**不论文件是否已存在**。"""
    return mamba.binary_path(r_env.bin_dir())


def micromamba_path() -> str:
    """私有 micromamba 可执行文件路径（不存在时返回空串）。"""
    return mamba.find_binary(r_env.bin_dir())


def conda_package_name(package: str) -> str:
    """CRAN 包名 -> conda-forge 包名（``RColorBrewer`` -> ``r-colorbrewer``）。"""
    return _CONDA_PREFIX + str(package).strip().lower()


def private_rscript() -> str:
    """私有运行时的 Rscript 路径（不存在返回空串）。

    conda 环境里 Rscript 的位置随平台与构建略有差异，因此按候选顺序探测，
    不做单一路径的硬编码假设。
    """
    root = r_env.runtime_dir()
    candidates: List[str] = []
    if is_windows():
        candidates += [
            os.path.join(root, "Scripts", "Rscript.exe"),
            os.path.join(root, "Lib", "R", "bin", "Rscript.exe"),
            os.path.join(root, "bin", "Rscript.exe"),
        ]
    else:
        candidates += [
            os.path.join(root, "bin", "Rscript"),
            os.path.join(root, "lib", "R", "bin", "Rscript"),
        ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return ""


def runtime_ready() -> bool:
    """私有运行时是否就绪（只看可执行文件，不联网）。"""
    return bool(private_rscript())


def _mamba_env() -> Dict[str, str]:
    """运行 micromamba 的环境：缓存与根前缀都留在私有目录内，不污染用户家目录。"""
    return mamba.prefix_env(r_env.cache_dir())


def _stream(cmd: Sequence[str], *, env: Optional[Dict[str, str]] = None,
            log_cb: Optional[Callable[[str], None]] = None,
            line_cb: Optional[Callable[[str], None]] = None,
            cancel_cb: Optional[Callable[[], bool]] = None,
            log_path: str = "") -> dict:
    """执行命令并流式回传输出（实现见 :mod:`src.core.toolchain.micromamba`）。"""
    return mamba.stream_command(cmd, env=env, log_cb=log_cb, line_cb=line_cb,
                                cancel_cb=cancel_cb, log_path=log_path)


def download_micromamba(*, log_cb: Optional[Callable[[str], None]] = None,
                        progress_cb: Optional[Callable[[int, str], None]] = None,
                        cancel_cb: Optional[Callable[[], bool]] = None) -> dict:
    """下载并解包 micromamba 到 ``r_env/bin``。

    进度语义保持原样：下载阶段占 0-60，就绪后补一个 65，后续 conda 创建阶段
    从 65 接着往上走。底层回传的是 0-100，这里按原来的分段缩放。
    """
    r_env.ensure_dirs()

    def _scaled(pct: int, text: str) -> None:
        if progress_cb:
            progress_cb(min(60, int(pct * 60 / 100)), text)

    out = mamba.download(
        r_env.bin_dir(), r_env.cache_dir(),
        log_cb=log_cb, progress_cb=_scaled, cancel_cb=cancel_cb,
    )
    if out.get("success") and progress_cb:
        progress_cb(65, "micromamba ready")
    logger.debug(f"micromamba bootstrap finished: success={out.get('success')} "
                 f"cancelled={out.get('cancelled')}")
    return out


def verify_micromamba(binary: str, timeout: float = 30.0) -> dict:
    """真实运行一次 micromamba，确认它在当前系统上可执行。"""
    return mamba.verify(binary, r_env.cache_dir(), timeout=timeout)


def _micromamba_unusable_hint(detail: str) -> str:
    """micromamba 跑不起来时，按平台给出照做就能修的建议。"""
    return mamba.unusable_hint(detail)


def _unsatisfiable_package(lines: Sequence[str]) -> str:
    """从 micromamba 输出里找出"通道内无此包"的包名（找不到返回空串）。"""
    return mamba.unsatisfiable_package(lines)


def create_runtime(packages: Sequence[str], *,
                   log_cb: Optional[Callable[[str], None]] = None,
                   progress_cb: Optional[Callable[[int, str], None]] = None,
                   cancel_cb: Optional[Callable[[], bool]] = None) -> dict:
    """创建私有 R 运行时，并装入 ``packages`` 对应的 conda-forge 预编译包。

    :param packages: CRAN 包名（``ggplot2`` 等），内部转成 ``r-*``。
    :return: ``{"success", "cancelled", "error", "rscript", "skipped"}``
    """
    result: Dict[str, object] = {"success": False, "cancelled": False, "error": "",
                                 "rscript": "", "skipped": []}
    mamba_exe = micromamba_path()
    if not mamba_exe:
        result["error"] = "micromamba is not available"
        logger.error("Cannot create private R runtime: micromamba is missing")
        return result

    r_env.ensure_dirs()
    root = r_env.runtime_dir()
    log_path = os.path.join(r_env.logs_dir(),
                            f"runtime-{time.strftime('%Y%m%d-%H%M%S')}.log")

    wanted: List[str] = list(_BASE_CONDA_PACKAGES)
    wanted += [conda_package_name(p) for p in packages]

    # 求解失败的常见原因是某个包在通道里没有对应产物：剔掉它重试，而不是整单作废
    # （缺的包随后可由源码装包路径补齐，两种情况都会如实写进日志）。
    solved = False
    out: Dict[str, object] = {"code": -1, "cancelled": False, "tail": []}
    for attempt in range(3):
        cmd = [mamba_exe, "create", "-y", "-p", root, "-c", _CONDA_CHANNEL,
               "--no-rc"] + wanted
        if log_cb:
            log_cb(f"Creating private R runtime ({len(wanted)} conda package(s))")
            log_cb("Command: " + " ".join(cmd))

        ticks = {"n": 0}

        def _on_line(line: str) -> None:
            text = line.strip()
            if not text.startswith(("Downloading", "Extracting", "Linking")):
                return
            ticks["n"] += 1
            if progress_cb:
                progress_cb(min(95, 65 + ticks["n"]), text[:200])

        out = _stream(cmd, env=_mamba_env(), log_cb=log_cb, line_cb=_on_line,
                      cancel_cb=cancel_cb, log_path=log_path)
        logger.info(f"conda create attempt {attempt + 1}: exit={out['code']} "
                    f"cancelled={out['cancelled']} packages={len(wanted)}")
        if out["cancelled"]:
            result["cancelled"] = True
            result["error"] = "cancelled by user"
            return result
        if out["code"] == 0:
            solved = True
            break

        bad = _unsatisfiable_package(out["tail"])
        if not bad or bad in wanted or len(wanted) <= len(_BASE_CONDA_PACKAGES):
            result["error"] = next((l.strip() for l in reversed(out["tail"])
                                    if l.strip()), f"micromamba exited {out['code']}")
            if log_cb:
                log_cb(f"Private runtime creation failed: {result['error']}")
                log_cb(f"Log: {log_path}")
            logger.error(f"Private runtime creation failed: {result['error']}")
            return result

        wanted = [p for p in wanted if p != bad]
        result["skipped"].append(bad)
        if log_cb:
            log_cb(f"Package {bad} is unavailable on {_CONDA_CHANNEL}; retrying without it.")

    if not solved:
        # 重试次数用尽仍未成功：把最后一次的真实报错原样带出去，不伪装成"装好了
        # 但找不到 Rscript"。
        result["error"] = next((l.strip() for l in reversed(out["tail"]) if l.strip()),
                               f"micromamba exited {out['code']}")
        if log_cb:
            log_cb(f"Private runtime creation failed: {result['error']}")
            log_cb(f"Log: {log_path}")
        logger.error(f"Private runtime creation failed after retries: {result['error']}")
        return result

    rscript = private_rscript()
    if not rscript:
        result["error"] = "runtime created but Rscript was not found"
        if log_cb:
            log_cb(result["error"])
        logger.error(result["error"])
        return result

    if log_cb:
        log_cb(f"Private R runtime ready: {rscript}")
    result.update(success=True, rscript=rscript)
    if progress_cb:
        progress_cb(95, "Verifying private R runtime")
    logger.info(f"Private R runtime created at {rscript}")
    return result


def verify_runtime(rscript: str, timeout: float = 60.0) -> dict:
    """真实运行一次私有 R 并读取版本，确认它在当前系统上确实可用。"""
    info = {"available": False, "version": "", "detail": ""}
    if not rscript:
        info["detail"] = "no runtime"
        return info
    try:
        proc = subprocess.run(
            [rscript, "--vanilla", "-e", 'cat(R.version$version.string, "\\n")'],
            capture_output=True, text=True, timeout=timeout,
            env=r_env.child_env(), creationflags=no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        info["detail"] = str(e)
        logger.warning(f"Private R runtime verification failed: {e}")
        return info

    out = (proc.stdout or "").strip()
    if proc.returncode == 0 and "R version" in out:
        tokens = out.split()
        info.update(available=True, version=tokens[2] if len(tokens) > 2 else "")
    else:
        info["detail"] = (proc.stderr or out or f"exit code {proc.returncode}").strip()[:300]
        logger.warning(f"Private R runtime unusable: {info['detail']}")
    return info


def remove_runtime() -> bool:
    """删除私有运行时（保留私有库），供设置页"移除运行时"使用。"""
    root = r_env.runtime_dir()
    if not os.path.isdir(root):
        return True
    try:
        shutil.rmtree(root)
        logger.info(f"Private R runtime removed: {root}")
        return True
    except OSError as e:
        logger.error(f"Cannot remove private R runtime {root}: {e}")
        return False
