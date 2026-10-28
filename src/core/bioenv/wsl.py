"""WSL 检测与命令包装。

为什么需要这一层
----------------
bioconda 几乎没有 ``win-64`` 产物，Windows 上的权威生信工具基本只有一个来源：
WSL 里的 Linux 环境。这不是"锦上添花"的兼容选项，而是 Windows 侧唯一能拿到
与 Linux 一致结果的路径。

三个必须处理的平台细节
----------------------
1. **两套编码**。``wsl.exe`` 自身输出（``-l`` / ``--status``）经管道时是
   **UTF-16LE**，而 ``wsl -e <cmd>`` 的输出是子进程的原始编码（通常 UTF-8）。
   混用同一套解码会得到乱码或直接抛异常，因此这里分成两个函数，各自固定解码方式。
2. **错误文本会本地化**。中文 Windows 上 WSL 的报错是中文，靠 ``stderr`` 文本匹配
   会失效，所以判定一律以**返回码**为准，文本只作为 detail 保留给用户看。
3. **``stdin`` 必须关闭**。``wsl.exe`` 在管道模式下若 stdin 未关闭会等待输入，
   表现为"命令永远不返回"。这里统一 ``DEVNULL``。

为什么不用 ``--cd`` 一把梭
--------------------------
``--cd`` 是较新版本才有的选项，旧版会直接报未知参数。检测阶段会显式验证它可用，
不可用时回落到 ``sh -c 'cd "$1" && shift && exec "$@"'``：那段 shell 片段是常量，
目录与参数都作为位置参数传入，不拼接用户输入。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from src.core.bioenv import paths
from src.core.platform_env import is_windows, no_window_flags

logger = logging.getLogger("BioEnv.Wsl")

#: 检测状态。
STATE_NOT_APPLICABLE = "not_applicable"
STATE_NO_WSL = "no_wsl"
STATE_NO_DISTRO = "no_distro"
STATE_WSL1_ONLY = "wsl1_only"
STATE_READY = "ready"
STATE_UNRESPONSIVE = "unresponsive"
STATE_BLOCKED = "blocked"

#: 用户配置：允许手动指定发行版或整体关闭 WSL 路径。
CONFIG_FILE_NAME = "wsl.json"

#: 检测结果的有效期（秒）。到期后重新探测，避免发行版被卸载后仍显示可用。
STATUS_TTL = 6 * 60 * 60

#: 检测各步骤的超时。发行版冷启动可达十余秒，给足余量但必须封顶。
_TIMEOUT_LIST = 10.0
_TIMEOUT_PROBE = 30.0

#: ``sh -c 'cd "$1" && shift && exec "$@"'`` 里的固定引导片段。
_CD_SHIM = 'cd "$1" && shift && exec "$@"'

_STATUS: Optional["WslStatus"] = None
_STATUS_AT = 0.0
_STATUS_LOCK = threading.Lock()

_PATH_CACHE: Dict[Tuple[str, str], str] = {}
_PATH_LOCK = threading.Lock()


@dataclass(frozen=True)
class WslStatus:
    """一次检测的结论。"""

    state: str
    distro: str = ""
    version: int = 0
    supports_cd: bool = False
    detail: str = ""

    @property
    def usable(self) -> bool:
        """是否可以直接用来执行生信工具。"""
        return self.state == STATE_READY and bool(self.distro)


# --------------------------------------------------------------------------- #
#  配置
# --------------------------------------------------------------------------- #
def config_path() -> str:
    """WSL 配置文件位置。"""
    return os.path.join(paths.env_root(), CONFIG_FILE_NAME)


def load_config() -> dict:
    """读取用户配置（``distro`` 指定发行版，``disabled`` 整体关闭）。"""
    path = config_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Cannot read WSL config {path}: {e}")
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
#  底层执行
# --------------------------------------------------------------------------- #
def _wsl_exe() -> str:
    """``wsl.exe`` 的绝对路径（不存在返回空串）。"""
    return shutil.which("wsl") or ""


def _build_command(exe: str, distro: str, argv: Sequence[str], cwd: str,
                   supports_cd: bool) -> List[str]:
    """拼出完整的宿主命令行。

    有 ``--cd`` 时是 ``wsl -d D --cd C -e TOOL ARGS``；没有时**不能**在末尾再补一个
    ``-e``——必须让 ``sh -c`` 接管整条命令，把目录与工具名依次当作位置参数传进去：
    ``wsl -d D -e sh -c 'cd "$1" && shift && exec "$@"' sh C TOOL ARGS``。
    这条差别是"旧版 WSL 上命令直接失效"与"正常工作"的分界。
    """
    if cwd and not supports_cd:
        return [exe, "-d", distro, "-e", "sh", "-c", _CD_SHIM, "sh", cwd,
                *[str(a) for a in argv]]
    command = [exe, "-d", distro]
    if cwd:
        command += ["--cd", cwd]
    command += ["-e", *[str(a) for a in argv]]
    return command


def _decode_console(data: bytes) -> str:
    """解码 ``wsl.exe`` 自身的输出：UTF-16LE 为主，UTF-8 作回落。"""
    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or data.count(b"\x00") > len(data) // 4:
        try:
            return data.decode("utf-16-le", "replace").lstrip("\ufeff")
        except (UnicodeDecodeError, LookupError) as e:
            logger.debug(f"UTF-16 decode failed, falling back to UTF-8: {e}")
    return data.decode("utf-8", "replace")


def _run_console(args: Sequence[str], timeout: float) -> Tuple[int, str]:
    """执行 ``wsl.exe`` 自身的管理命令（如 ``-l -q``），返回 ``(返回码, 文本)``。"""
    exe = _wsl_exe()
    if not exe:
        return -1, ""
    try:
        proc = subprocess.run(
            [exe, *args], capture_output=True, timeout=timeout,
            stdin=subprocess.DEVNULL, creationflags=no_window_flags(),
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"wsl.exe {args} timed out after {timeout}s")
        return -1, ""
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(f"Cannot run wsl.exe {args}: {e}")
        return -1, ""
    return proc.returncode, _decode_console(proc.stdout or b"")


def run_in_distro(distro: str, argv: Sequence[str], *, timeout: float = _TIMEOUT_PROBE,
                  cwd: str = "", supports_cd: bool = True) -> dict:
    """在发行版内执行一条命令，返回 ``{"rc", "stdout", "stderr", "timed_out"}``。

    输出按 UTF-8 解码（子进程的原始编码），失败时保留 stderr 供诊断。
    """
    exe = _wsl_exe()
    result = {"rc": -1, "stdout": "", "stderr": "", "timed_out": False}
    if not exe:
        result["stderr"] = "wsl.exe not found"
        return result
    command = _build_command(exe, distro, argv, cwd, supports_cd)
    try:
        proc = subprocess.run(
            command, capture_output=True, timeout=timeout,
            stdin=subprocess.DEVNULL, creationflags=no_window_flags(),
        )
    except subprocess.TimeoutExpired:
        result["timed_out"] = True
        result["stderr"] = f"timed out after {timeout}s"
        logger.warning(f"WSL command timed out in '{distro}': {argv[:2]}")
        return result
    except (OSError, subprocess.SubprocessError) as e:
        result["stderr"] = str(e)
        logger.warning(f"Cannot run WSL command in '{distro}': {e}")
        return result
    result["rc"] = proc.returncode
    result["stdout"] = (proc.stdout or b"").decode("utf-8", "replace")
    result["stderr"] = (proc.stderr or b"").decode("utf-8", "replace")
    return result


# --------------------------------------------------------------------------- #
#  检测
# --------------------------------------------------------------------------- #
def list_distros() -> List[str]:
    """列出已安装的发行版名（``-l -q`` 的输出是 UTF-16LE）。"""
    if not is_windows():
        return []
    rc, text = _run_console(["-l", "-q"], _TIMEOUT_LIST)
    if rc != 0:
        # 无发行版时部分版本返回非零并输出帮助文本，这里不把它当成错误处理。
        logger.info(f"wsl -l -q exited {rc}")
    names = []
    for line in text.replace("\x00", "").splitlines():
        name = line.strip().strip("\ufeff")
        if name and not name.lower().startswith(("windows subsystem", "copyright", "usage")):
            names.append(name)
    logger.info(f"WSL distributions found: {names}")
    return names


def _kernel_release(distro: str) -> Tuple[str, int]:
    """读取发行版内核版本，返回 ``(原始文本, 版本号)``；1 表示 WSL1，2 表示 WSL2。"""
    res = run_in_distro(distro, ["uname", "-r"], timeout=_TIMEOUT_PROBE)
    release = (res["stdout"] or "").strip()
    if res["rc"] != 0 or not release:
        logger.info(f"Cannot read kernel release in '{distro}': {res['stderr'][:160]}")
        return "", 0
    lowered = release.lower()
    if "microsoft-standard-wsl2" in lowered or "wsl2" in lowered:
        return release, 2
    if "microsoft" in lowered:
        return release, 1
    return release, 0


def _supports_cd(distro: str) -> bool:
    """``--cd`` 是否可用（旧版 WSL 没有这个选项）。"""
    exe = _wsl_exe()
    if not exe:
        return False
    try:
        proc = subprocess.run(
            [exe, "-d", distro, "--cd", "/", "-e", "/bin/true"],
            capture_output=True, timeout=_TIMEOUT_PROBE,
            stdin=subprocess.DEVNULL, creationflags=no_window_flags(),
        )
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError) as e:
        logger.info(f"--cd probe failed in '{distro}': {e}")
        return False
    return proc.returncode == 0


def detect(refresh: bool = False) -> WslStatus:
    """探测 WSL 可用性（结果带有效期缓存，``refresh`` 强制重探）。"""
    global _STATUS, _STATUS_AT
    with _STATUS_LOCK:
        if not refresh and _STATUS is not None and (time.time() - _STATUS_AT) < STATUS_TTL:
            return _STATUS

    status = _detect_once()
    with _STATUS_LOCK:
        _STATUS = status
        _STATUS_AT = time.time()
    logger.info(f"WSL status: {status.state} distro={status.distro or '-'} "
                f"version={status.version or '-'} cd={status.supports_cd}")
    return status


def _detect_once() -> WslStatus:
    """实际执行一次探测（无缓存）。"""
    if not is_windows():
        return WslStatus(state=STATE_NOT_APPLICABLE)

    config = load_config()
    if config.get("disabled"):
        return WslStatus(state=STATE_BLOCKED, detail="disabled in wsl.json")

    if not _wsl_exe():
        return WslStatus(state=STATE_NO_WSL,
                         detail="wsl.exe not found; the Windows feature is not enabled")

    distros = list_distros()
    if not distros:
        return WslStatus(state=STATE_NO_DISTRO,
                         detail="no Linux distribution is installed")

    preferred = str(config.get("distro") or "").strip()
    if preferred and preferred not in distros:
        logger.warning(f"Configured WSL distro '{preferred}' is missing; using '{distros[0]}'")
    distro = preferred if preferred in distros else distros[0]

    release, version = _kernel_release(distro)
    if version == 1:
        return WslStatus(state=STATE_WSL1_ONLY, distro=distro, version=1,
                         detail=f"'{distro}' is WSL1; WSL2 is required for the toolchain")
    if version == 0:
        return WslStatus(state=STATE_UNRESPONSIVE, distro=distro,
                         detail=f"cannot read the kernel release of '{distro}' ({release[:80]})")

    res = run_in_distro(distro, ["/bin/true"], timeout=_TIMEOUT_PROBE)
    if res["timed_out"]:
        return WslStatus(state=STATE_UNRESPONSIVE, distro=distro, version=version,
                         detail="the distribution did not respond in time; "
                                "it may still be starting")
    if res["rc"] != 0:
        return WslStatus(state=STATE_BLOCKED, distro=distro, version=version,
                         detail=(res["stderr"] or f"exit code {res['rc']}")[:300])

    supports_cd = _supports_cd(distro)
    return WslStatus(state=STATE_READY, distro=distro, version=version,
                     supports_cd=supports_cd)


def invalidate() -> None:
    """清掉检测结果与路径缓存（设置页"重新检测"用）。"""
    global _STATUS, _STATUS_AT
    with _STATUS_LOCK:
        _STATUS = None
        _STATUS_AT = 0.0
    with _PATH_LOCK:
        _PATH_CACHE.clear()
    logger.info("WSL detection cache cleared")


# --------------------------------------------------------------------------- #
#  路径与命令包装
# --------------------------------------------------------------------------- #
def to_wsl_path(host_path: str, distro: str = "") -> str:
    """把 Windows 路径转成 WSL 内路径（走 ``wslpath``，不自己实现映射规则）。

    自己实现 ``C:\\a`` -> ``/mnt/c/a`` 会踩两个坑：``/etc/wsl.conf`` 可改 automount
    根，且发行版可能配了自定义挂载。让 ``wslpath`` 回答是唯一可靠的做法。
    """
    text = str(host_path or "").strip()
    if not text:
        return ""
    if text.startswith("/"):
        return text
    status = detect()
    target = distro or status.distro
    if not target:
        return ""
    key = (target, text)
    with _PATH_LOCK:
        cached = _PATH_CACHE.get(key)
    if cached:
        return cached
    res = run_in_distro(target, ["wslpath", "-u", text], timeout=_TIMEOUT_PROBE)
    converted = (res["stdout"] or "").strip()
    if res["rc"] != 0 or not converted:
        logger.warning(f"wslpath failed for {text}: {res['stderr'][:160]}")
        return ""
    with _PATH_LOCK:
        _PATH_CACHE[key] = converted
    return converted


def wrap_argv(argv: Sequence[str], *, distro: str, cwd: str = "",
              supports_cd: bool = True) -> List[str]:
    """把发行版内的一条命令包装成宿主侧可执行的 ``wsl.exe`` 命令行。

    :param distro: 发行版名。
    :param cwd: 发行版内的**绝对路径**（不是 Windows 路径），调用方需先转换。
    """
    exe = _wsl_exe()
    if not exe:
        raise RuntimeError("wsl.exe is not available")
    return _build_command(exe, distro, argv, cwd, supports_cd)


def which_in_distro(name: str, distro: str = "") -> str:
    """在发行版内定位可执行文件（``command -v``），找不到返回空串。"""
    status = detect()
    target = distro or status.distro
    if not target:
        return ""
    res = run_in_distro(target, ["sh", "-c", 'command -v "$1"', "sh", name],
                        timeout=_TIMEOUT_PROBE)
    path = (res["stdout"] or "").strip().splitlines()
    return path[0] if res["rc"] == 0 and path else ""


def probe_tool(name: str, version_args: Sequence[str], distro: str = "",
               timeout: float = _TIMEOUT_PROBE) -> dict:
    """在发行版内探测工具版本，结构与本地的 ``probe.probe`` 一致。"""
    info = {"available": False, "version": "", "detail": ""}
    status = detect()
    target = distro or status.distro
    if not target:
        info["detail"] = "no WSL distribution"
        return info
    res = run_in_distro(target, [name, *[str(a) for a in version_args]], timeout=timeout)
    out = (res["stdout"] or "").strip() or (res["stderr"] or "").strip()
    if res["rc"] == 0 and out:
        info.update(available=True, version=out.splitlines()[0].strip()[:200])
        logger.info(f"WSL tool probe ok: {name} -> {info['version']}")
    else:
        info["detail"] = (out or f"exit code {res['rc']}")[:300]
        logger.info(f"WSL tool probe unusable: {name}: {info['detail']}")
    return info


def describe(status: Optional["WslStatus"] = None) -> str:
    """给用户看的一句话结论。"""
    st = status or detect()
    messages = {
        STATE_NOT_APPLICABLE: "WSL is only relevant on Windows.",
        STATE_NO_WSL: "WSL is not enabled on this system.",
        STATE_NO_DISTRO: "WSL is enabled but no Linux distribution is installed.",
        STATE_WSL1_ONLY: "Only a WSL1 distribution is available; WSL2 is required.",
        STATE_UNRESPONSIVE: "The WSL distribution did not respond; it may still be starting.",
        STATE_BLOCKED: "WSL cannot be used: " + (st.detail or "blocked by policy"),
        STATE_READY: f"WSL is ready: {st.distro} (WSL{st.version or 2}).",
    }
    return messages.get(st.state, st.state)
