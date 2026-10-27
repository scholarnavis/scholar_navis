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
from typing import Callable, Dict, List, Optional, Sequence

from src.core import r_env
from src.core.platform_env import distro_family, is_macos, is_windows, no_window_flags

logger = logging.getLogger("Core.RRuntime")

#: micromamba 官方分发入口（返回 tar.bz2）。
_MAMBA_API = "https://micro.mamba.pm/api/micromamba/{platform}/latest"

#: conda-forge 通道；预编译 ``r-*`` 包即来源于此。
_CONDA_CHANNEL = "conda-forge"

#: 约定：CRAN 包 ``ggplot2`` 在 conda-forge 上名为 ``r-ggplot2``（全小写）。
_CONDA_PREFIX = "r-"

#: 运行时里必须存在的包（校验运行时自身可用）。
_BASE_CONDA_PACKAGES = ("r-base",)

#: micromamba 输出里"通道内无此包"的写法，用于剔除后重试。
_RE_UNSATISFIABLE = re.compile(
    r"nothing provides|package\s+([A-Za-z0-9._-]+)\s+is not|"
    r"([A-Za-z0-9._-]+)\s+not found in"
)


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


def micromamba_binary_path() -> str:
    """私有 micromamba 的落点路径，**不论文件是否已存在**。

    解包时二进制还不存在，必须用这个函数拿到目标路径；:func:`micromamba_path`
    在文件缺失时返回空串，两者不能混用。
    """
    name = "micromamba.exe" if is_windows() else "micromamba"
    return os.path.join(r_env.bin_dir(), name)


def micromamba_path() -> str:
    """私有 micromamba 可执行文件路径（不存在时返回空串）。"""
    path = micromamba_binary_path()
    return path if os.path.isfile(path) else ""


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
    env = dict(os.environ)
    env["MAMBA_ROOT_PREFIX"] = os.path.join(r_env.cache_dir(), "mamba")
    env["CONDA_PKGS_DIRS"] = os.path.join(r_env.cache_dir(), "pkgs")
    return env


def _stream(cmd: Sequence[str], *, env: Optional[Dict[str, str]] = None,
            log_cb: Optional[Callable[[str], None]] = None,
            line_cb: Optional[Callable[[str], None]] = None,
            cancel_cb: Optional[Callable[[], bool]] = None,
            log_path: str = "") -> dict:
    """执行命令并流式回传输出（下载解包与 conda 创建共用）。

    :param line_cb: 每行额外回调一次（供调用方从输出里解析进度）。
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
                if len(out["tail"]) > 40:
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
    return out


def download_micromamba(*, log_cb: Optional[Callable[[str], None]] = None,
                        progress_cb: Optional[Callable[[int, str], None]] = None,
                        cancel_cb: Optional[Callable[[], bool]] = None) -> dict:
    """下载并解包 micromamba 到 ``r_env/bin``。

    分发产物是 tar.bz2（内含 ``bin/micromamba``，Windows 为
    ``Library/bin/micromamba.exe``），用标准库 tarfile 解包，不引入额外依赖。
    """
    result = {"success": False, "cancelled": False, "error": "", "path": ""}
    if micromamba_path():
        result.update(success=True, path=micromamba_path())
        return result

    plat = runtime_platform()
    url = _MAMBA_API.format(platform=plat)
    r_env.ensure_dirs()
    archive = os.path.join(r_env.cache_dir(), f"micromamba-{plat}.tar.bz2")

    if log_cb:
        log_cb(f"Downloading micromamba for {plat}")
        log_cb(f"URL: {url}")

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
                        return result
                    now = time.time()
                    if progress_cb and now - last_report > 0.3:
                        last_report = now
                        pct = int(60 * got / total) if total else 0
                        size = f"{got / 1048576:.1f} MB"
                        if total:
                            size += f" / {total / 1048576:.1f} MB"
                        progress_cb(min(60, pct), size)
        finally:
            try:
                resp.close()
            except Exception as e:
                logger.debug(f"Closing download response failed: {e}")
            r_env.close_session(session)
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
                if log_cb:
                    log_cb(result["error"])
                return result
            src = tar.extractfile(member)
            if src is None:
                result["error"] = "cannot read micromamba from archive"
                return result
            target = micromamba_binary_path()
            with open(target, "wb") as out_file:
                shutil.copyfileobj(src, out_file)
            os.chmod(target, 0o755)
    except (OSError, tarfile.TarError) as e:
        result["error"] = f"cannot unpack micromamba: {e}"
        logger.error(result["error"])
        if log_cb:
            log_cb(result["error"])
        return result

    target = micromamba_binary_path()
    check = verify_micromamba(target)
    if not check["available"]:
        result["error"] = _micromamba_unusable_hint(check["detail"])
        logger.error(result["error"])
        if log_cb:
            log_cb(result["error"])
        return result

    result.update(success=True, path=target)
    if progress_cb:
        progress_cb(65, "micromamba ready")
    if log_cb:
        log_cb(f"micromamba installed: {target} ({check['version']})")
    return result


def verify_micromamba(mamba: str, timeout: float = 30.0) -> dict:
    """真实运行一次 micromamba，确认它在当前系统上可执行。

    解包成功不等于能跑：非 FHS 系统（NixOS 等）上外来二进制需要兼容层，
    缺了它 micromamba 只会在后面的 conda 步骤里报一句难以定位的加载错误。
    这里提前验证并给出可执行的修复建议，而不是让用户面对一个看不懂的失败。
    """
    info = {"available": False, "version": "", "detail": ""}
    if not mamba:
        info["detail"] = "micromamba not found"
        return info
    try:
        proc = subprocess.run(
            [mamba, "--version"], capture_output=True, text=True,
            timeout=timeout, env=_mamba_env(), creationflags=no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        info["detail"] = str(e)
        logger.warning(f"micromamba verification failed: {e}")
        return info

    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if proc.returncode == 0 and out:
        info.update(available=True, version=out.splitlines()[0].strip())
    else:
        info["detail"] = (out or f"exit code {proc.returncode}").strip()[:300]
        logger.warning(f"micromamba unusable: {info['detail']}")
    return info


def _micromamba_unusable_hint(detail: str) -> str:
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
            "若已有可用的 R，也可以直接走系统 R 装包装进私有库，无需私有运行时。")
    elif is_macos():
        lines.append("macOS 上若被 Gatekeeper/公证拦截，请为该二进制放行后重试。")
    elif is_windows():
        lines.append("Windows 上请确认下载未被安全软件拦截或截断，然后重试。")
    else:
        lines.append("请确认该二进制可在本机执行（架构匹配、动态库齐全）后重试。")
    return "\n".join(lines)


def _unsatisfiable_package(lines: Sequence[str]) -> str:
    """从 micromamba 输出里找出"通道内无此包"的包名（找不到返回空串）。"""
    for line in reversed(list(lines)):
        match = _RE_UNSATISFIABLE.search(line)
        if match:
            for group in match.groups():
                if group:
                    return group
    return ""


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
    mamba = micromamba_path()
    if not mamba:
        result["error"] = "micromamba is not available"
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
    for _attempt in range(3):
        cmd = [mamba, "create", "-y", "-p", root, "-c", _CONDA_CHANNEL,
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
        return result

    rscript = private_rscript()
    if not rscript:
        result["error"] = "runtime created but Rscript was not found"
        if log_cb:
            log_cb(result["error"])
        return result

    if log_cb:
        log_cb(f"Private R runtime ready: {rscript}")
    result.update(success=True, rscript=rscript)
    if progress_cb:
        progress_cb(95, "Verifying private R runtime")
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