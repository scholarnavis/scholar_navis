"""micromamba 引导与流式执行（唯一实现处）。

为什么单独成层
--------------
私有 R 运行时与生信工具前缀都需要「在没有编译器、没有管理员权限、不污染用户家
目录」的前提下拿到预编译二进制。两者的差别只有通道与包名，引导过程完全一致：
下载官方 tar.bz2 → 只解出其中的可执行文件 → **真实运行一次**确认它在当前系统上
可用。这份实现在过去只存在于 ``r_runtime`` 里，若为生信前缀再写一遍，两处会各
自演化（一处修好 NixOS 的兼容层提示，另一处仍然只报一句加载错误）。

为什么落在 ``<cache>/mamba`` 而不在家目录
---------------------------------------
``MAMBA_ROOT_PREFIX`` 与 ``CONDA_PKGS_DIRS`` 都指向私有目录：卸载应用即可整体
清除，也不会与用户自己的 conda 安装互相干扰。

为什么必须真实运行
------------------
解包成功不等于能跑。非 FHS 系统（NixOS 等）上外来二进制需要 ``nix-ld`` 之类的
兼容层，缺了它 micromamba 只会在后续步骤里报一句难以定位的加载错误。因此
:func:`download` 结尾一律调用 :func:`verify`，失败即如实报错并给出可执行的修
复建议，不做「看起来装好了」的假成功。
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import tarfile
import time
from typing import Callable, Dict, Optional, Sequence

from src.core.platform_env import distro_family, is_macos, is_windows, no_window_flags

logger = logging.getLogger("Toolchain.Micromamba")

#: micromamba 官方分发入口（返回 tar.bz2）。
MAMBA_API = "https://micro.mamba.pm/api/micromamba/{platform}/latest"

#: 求解失败时输出的「通道内无此包」写法，用于剔除该包后重试。
_RE_UNSATISFIABLE = re.compile(
    r"nothing provides|package\s+([A-Za-z0-9._-]+)\s+is not|"
    r"([A-Za-z0-9._-]+)\s+not found in"
)

#: 流式执行时保留的尾部行数，用于失败原因提炼。
_TAIL_LINES = 40


# --------------------------------------------------------------------------- #
#  平台与落点
# --------------------------------------------------------------------------- #
def runtime_platform() -> str:
    """返回 micromamba 的平台标识（``linux-64`` / ``osx-arm64`` / ``win-64`` …）。"""
    machine = (platform.machine() or "").lower()
    arch = {"x86_64": "64", "amd64": "64",
            "aarch64": "arm64", "arm64": "arm64"}.get(machine, "")
    if is_windows():
        return f"win-{arch or '64'}"
    if is_macos():
        return f"osx-{arch or '64'}"
    return f"linux-{arch or '64'}"


def binary_path(bin_dir: str) -> str:
    """micromamba 的落点路径，**不论文件是否已存在**。

    解包时二进制还不存在，必须用这个函数拿到目标路径；:func:`find_binary`
    在文件缺失时返回空串，两者不能混用。
    """
    name = "micromamba.exe" if is_windows() else "micromamba"
    return os.path.join(bin_dir, name)


def find_binary(bin_dir: str) -> str:
    """micromamba 可执行文件路径（不存在时返回空串）。"""
    path = binary_path(bin_dir)
    return path if os.path.isfile(path) else ""


def prefix_env(cache_dir: str, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """运行 micromamba 的环境：缓存与根前缀都留在私有目录内。

    :param base: 以哪个环境为底（默认当前进程环境），便于调用方叠加自己的变量。
    """
    env = dict(base if base is not None else os.environ)
    env["MAMBA_ROOT_PREFIX"] = os.path.join(cache_dir, "mamba")
    env["CONDA_PKGS_DIRS"] = os.path.join(cache_dir, "pkgs")
    return env


# --------------------------------------------------------------------------- #
#  流式执行
# --------------------------------------------------------------------------- #
def stream_command(
    cmd: Sequence[str],
    *,
    env: Optional[Dict[str, str]] = None,
    log_cb: Optional[Callable[[str], None]] = None,
    line_cb: Optional[Callable[[str], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    log_path: str = "",
) -> dict:
    """执行命令并逐行回传输出（下载解包与前缀创建共用）。

    :param line_cb: 每行额外回调一次，供调用方从输出里解析进度。
    :param log_path: 非空时把原始输出另存一份，便于事后复现失败现场。
    :return: ``{"code", "cancelled", "tail"}``
    """
    out = {"code": -1, "cancelled": False, "tail": []}
    handle = None
    if log_path:
        try:
            handle = open(log_path, "w", encoding="utf-8", buffering=1)
        except OSError as e:
            logger.warning(f"Cannot open log file {log_path}: {e}")
    try:
        logger.info(f"Streaming command: {cmd[0]} (args: {len(cmd) - 1})")
        proc = subprocess.Popen(
            list(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env or os.environ,
            creationflags=no_window_flags(),
        )
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                if handle:
                    handle.write(raw)
                out["tail"].append(line)
                if len(out["tail"]) > _TAIL_LINES:
                    out["tail"].pop(0)
                if line_cb:
                    line_cb(line)
                if log_cb:
                    log_cb(line)
                if cancel_cb and cancel_cb():
                    out["cancelled"] = True
                    proc.terminate()
                    break
        finally:
            if proc.poll() is None:
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
            out["code"] = proc.returncode
    except (OSError, subprocess.SubprocessError) as e:
        message = f"cannot run {cmd[0]}: {e}"
        out["tail"].append(message)
        if log_cb:
            log_cb(message)
        logger.error(message)
    finally:
        if handle:
            handle.close()

    if out["cancelled"]:
        logger.warning(f"Command cancelled: {cmd[0]}")
    elif out["code"] != 0:
        logger.error(f"Command failed with exit code {out['code']}: {cmd[0]}")
    return out


# --------------------------------------------------------------------------- #
#  下载与校验
# --------------------------------------------------------------------------- #
def download(
    bin_dir: str,
    cache_dir: str,
    *,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[Callable[[int, str], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> dict:
    """下载并解包 micromamba 到 ``bin_dir``。

    :param progress_cb: ``(百分比 0-100, 说明)``，只覆盖下载阶段；调用方若把整个
        安装流程的进度条分成若干段，需自行缩放。
    :return: ``{"success", "cancelled", "error", "path"}``
    """
    result = {"success": False, "cancelled": False, "error": "", "path": ""}
    existing = find_binary(bin_dir)
    if existing:
        result.update(success=True, path=existing)
        return result

    plat = runtime_platform()
    url = MAMBA_API.format(platform=plat)
    try:
        os.makedirs(bin_dir, exist_ok=True)
        os.makedirs(cache_dir, exist_ok=True)
    except OSError as e:
        result["error"] = f"cannot create directory: {e}"
        logger.error(result["error"])
        return result

    archive = os.path.join(cache_dir, f"micromamba-{plat}.tar.bz2")

    if log_cb:
        log_cb(f"Downloading micromamba for {plat}")
        log_cb(f"URL: {url}")
    logger.info(f"Downloading micromamba for {plat} from {url}")

    try:
        from src.core.network_worker import create_robust_session

        session = create_robust_session()
        # curl_cffi 的 Response 不支持上下文管理器（实测 TypeError），显式关闭。
        resp = session.get(url, timeout=30, stream=True)
        try:
            if resp.status_code >= 400:
                result["error"] = f"HTTP {resp.status_code} for {url}"
                if log_cb:
                    log_cb(result["error"])
                logger.error(result["error"])
                return result
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            last_report = 0.0
            with open(archive, "wb") as f:
                for chunk in resp.iter_content(256 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    got += len(chunk)
                    if cancel_cb and cancel_cb():
                        result["cancelled"] = True
                        logger.warning("micromamba download cancelled")
                        return result
                    now = time.time()
                    if progress_cb and now - last_report > 0.3:
                        last_report = now
                        pct = int(100 * got / total) if total else 0
                        size = f"{got / 1048576:.1f} MB"
                        if total:
                            size += f" / {total / 1048576:.1f} MB"
                        progress_cb(min(100, pct), size)
        finally:
            try:
                resp.close()
            except Exception as e:
                logger.debug(f"Closing download response failed: {e}")
            _close_session(session)
    except Exception as e:  # 网络/磁盘异常：如实上报，由调用方决定是否回退
        result["error"] = str(e)
        logger.error(f"micromamba download failed: {e}")
        if log_cb:
            log_cb(f"micromamba download failed: {e}")
        return result

    if log_cb:
        log_cb("Extracting micromamba")
    try:
        with tarfile.open(archive, "r:bz2") as tar:
            member = None
            for item in tar.getmembers():
                name = item.name.replace("\\", "/")
                if name.endswith("bin/micromamba") or name.endswith("bin/micromamba.exe"):
                    member = item
                    break
            if member is None:
                result["error"] = "micromamba binary not found in archive"
                logger.error(result["error"])
                if log_cb:
                    log_cb(result["error"])
                return result
            src = tar.extractfile(member)
            if src is None:
                result["error"] = "cannot read micromamba from archive"
                logger.error(result["error"])
                return result
            target = binary_path(bin_dir)
            with open(target, "wb") as out_file:
                shutil.copyfileobj(src, out_file)
            os.chmod(target, 0o755)
    except (OSError, tarfile.TarError) as e:
        result["error"] = f"cannot unpack micromamba: {e}"
        logger.error(result["error"])
        if log_cb:
            log_cb(result["error"])
        return result

    target = binary_path(bin_dir)
    check = verify(target, cache_dir)
    if not check["available"]:
        result["error"] = unusable_hint(check["detail"])
        logger.error(result["error"])
        if log_cb:
            log_cb(result["error"])
        return result

    result.update(success=True, path=target)
    if progress_cb:
        progress_cb(100, "micromamba ready")
    if log_cb:
        log_cb(f"micromamba installed: {target} ({check['version']})")
    logger.info(f"micromamba ready at {target}")
    return result


def _close_session(session) -> None:
    """静默关闭下载会话（关闭失败不影响结果）。"""
    if session is None:
        return
    try:
        session.close()
    except Exception as e:
        logger.debug(f"Closing download session failed: {e}")


def verify(binary: str, cache_dir: str, timeout: float = 30.0) -> dict:
    """真实运行一次 micromamba，确认它在当前系统上可执行。

    :return: ``{"available", "version", "detail"}``
    """
    info = {"available": False, "version": "", "detail": ""}
    if not binary:
        info["detail"] = "micromamba not found"
        return info
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True,
            timeout=timeout, env=prefix_env(cache_dir),
            creationflags=no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        info["detail"] = str(e)
        logger.warning(f"micromamba verification failed: {e}")
        return info

    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if proc.returncode == 0 and out:
        info.update(available=True, version=out.splitlines()[0].strip())
        logger.info(f"micromamba verified: {info['version']}")
    else:
        info["detail"] = (out or f"exit code {proc.returncode}").strip()[:300]
        logger.warning(f"micromamba unusable: {info['detail']}")
    return info


def unusable_hint(detail: str) -> str:
    """micromamba 跑不起来时，按平台给出照做就能修的建议。"""
    lines = [f"micromamba cannot run on this system: {detail}"]
    try:
        family = distro_family()
    except Exception as e:  # 探测失败只影响提示精度
        logger.debug(f"distro_family() unavailable: {e}")
        family = "unknown"

    if family == "nix":
        lines.append(
            "NixOS 上外来二进制需要一个兼容层：在 configuration.nix 里启用 "
            "programs.nix-ld.enable = true（并执行 nixos-rebuild switch）后重试；"
            "若本机已有可用的工具链，也可以直接使用系统安装的版本，无需私有前缀。")
    elif is_macos():
        lines.append("macOS 上若被 Gatekeeper/公证拦截，请为该二进制放行后重试。")
    elif is_windows():
        lines.append("Windows 上请确认下载未被安全软件拦截或截断，然后重试。")
    else:
        lines.append("请确认该二进制可在本机执行（架构匹配、动态库齐全）后重试。")
    return "\n".join(lines)


def unsatisfiable_package(lines: Sequence[str]) -> str:
    """从 micromamba 输出里找出「通道内无此包」的包名（找不到返回空串）。"""
    for line in reversed(list(lines)):
        match = _RE_UNSATISFIABLE.search(line)
        if match:
            for group in match.groups():
                if group:
                    return group
    return ""


# 通用可执行文件探测与版本比较在 :mod:`src.core.toolchain.probe`，不在此处重复。
