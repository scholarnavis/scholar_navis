"""工具执行边界（唯一实现处）。

三件事只在这里做
----------------
1. **argv 列表**，永不拼接字符串、永不经过 shell。参数值由封装函数给出，模型看到
   的是领域语义，不是命令行标志；
2. **子进程环境**，来自 :func:`src.core.bioenv.resolver.env_for`，系统安装与私有
   前缀的差别只体现在那里；
3. **现场留档**，每次执行一个目录：``command.json``（完整 argv、工作目录、工具版本
   与来源）、``stdout.log``、``stderr.log``、``result.json``（退出码、耗时、是否
   超时/取消）。

为什么输出一律落盘
------------------
生信工具的输出动辄几十万行，而工具调用的返回值最终会被运行时按字符预算截断
（``AgentRuntime._truncate`` 的 head+tail 截断会把 JSON 切坏）。因此这里只回传
**尾部若干行 + 落盘路径**，结构化解析在封装层做。这样"模型看到什么"与"磁盘上
有什么"是两件事，后者始终完整可核查。

为什么用线程读管道而不是 ``communicate``
--------------------------------------
``communicate`` 会阻塞到进程结束，中途无法响应取消；``timeout`` 参数也只对整体
生效，无法区分"用户取消"与"超时"。这里两个守护线程各自把管道泵到文件，主线程轮
询退出状态，因此两种中断都能被区分并如实记录。

远端（WSL）执行的取消与超时只能终止**宿主侧**的 ``wsl.exe``：发行版里的进程是否
随之退出由 WSL 自己决定。因此这两种终止一律记为 cancelled / timed_out，而不是
声称"工具已经被杀掉"。
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from src.core.bioenv import paths
from src.core.bioenv import resolver
from src.core.bioenv import wsl
from src.core.platform_env import no_window_flags
from src.core.safe_io import atomic_write_json

logger = logging.getLogger("BioEnv.Runner")

#: 默认单次执行超时（秒）。生信工具在交互场景下的合理上限。
DEFAULT_TIMEOUT = 120.0

#: 回传给调用方的尾部行数（stdout 与 stderr 各自）。
TAIL_LINES = 40

#: 管道读取块大小。
_CHUNK = 64 * 1024

#: 终止后等待进程自行退出的宽限期，超时则强杀。
_TERM_GRACE = 10.0

#: 单次运行目录名冲突时的最大重试次数。
_MAX_DIR_RETRY = 50


@dataclass(frozen=True)
class RunResult:
    """一次执行的完整结果。"""

    ok: bool
    returncode: int
    command: List[str] = field(default_factory=list)
    elapsed: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    error: str = ""
    run_dir: str = ""
    stdout_path: str = ""
    stderr_path: str = ""
    stdout_tail: List[str] = field(default_factory=list)
    stderr_tail: List[str] = field(default_factory=list)

    def summary(self) -> str:
        """一行可读摘要，用于日志与错误信息。"""
        state = "ok" if self.ok else ("cancelled" if self.cancelled
                                      else "timeout" if self.timed_out else "failed")
        return (f"{state} rc={self.returncode} in {self.elapsed:.2f}s "
                f"({os.path.basename(self.run_dir) if self.run_dir else 'no run dir'})")

    def stderr_hint(self) -> str:
        """失败时从 stderr 尾部取一行最有信息量的内容。"""
        for line in reversed(self.stderr_tail):
            text = line.strip()
            if text and not text.startswith("environment:"):
                return text[:300]
        return self.error or "no diagnostic output"


# --------------------------------------------------------------------------- #
#  文件尾部读取
# --------------------------------------------------------------------------- #
def tail_lines(path: str, limit: int = TAIL_LINES, block: int = 8192) -> List[str]:
    """从文件尾部倒着读到够 ``limit`` 行为止，不把整个文件读进内存。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if size <= 0:
        return []
    data = b""
    position = size
    try:
        with open(path, "rb") as fh:
            while position > 0 and data.count(b"\n") <= limit:
                step = min(block, position)
                position -= step
                fh.seek(position)
                data = fh.read(step) + data
    except OSError as e:
        logger.warning(f"Cannot read tail of {path}: {e}")
        return []
    return data.decode("utf-8", "replace").splitlines()[-limit:]


# --------------------------------------------------------------------------- #
#  运行目录
# --------------------------------------------------------------------------- #
def _new_run_dir(tool_name: str) -> str:
    """为一次执行创建独立目录（同名冲突时追加序号）。"""
    paths.ensure_dirs()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (tool_name or "tool"))
    for index in range(_MAX_DIR_RETRY):
        suffix = f"-{index}" if index else ""
        candidate = os.path.join(paths.runs_dir(), f"{stamp}-{safe}{suffix}")
        try:
            os.makedirs(candidate, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
        except OSError as e:
            logger.error(f"Cannot create run directory {candidate}: {e}")
            return ""
    logger.error(f"Cannot allocate a run directory for {tool_name} after {_MAX_DIR_RETRY} tries")
    return ""


def _pump(stream, handle) -> None:
    """把管道内容原样泵进文件（二进制，不做任何解码）。"""
    try:
        while True:
            chunk = stream.read(_CHUNK)
            if not chunk:
                break
            handle.write(chunk)
            handle.flush()
    except (OSError, ValueError) as e:
        logger.debug(f"Pipe pump stopped: {e}")
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


# --------------------------------------------------------------------------- #
#  执行
# --------------------------------------------------------------------------- #
def run(location: resolver.ToolLocation, argv: Sequence[str], *,
        timeout: float = DEFAULT_TIMEOUT,
        cwd: str = "",
        cancel_cb: Optional[Callable[[], bool]] = None) -> RunResult:
    """执行一条命令并返回结果。

    :param location: 由 :mod:`src.core.bioenv.resolver` 解析出的工具落点，决定用哪套
        环境变量。
    :param argv: 完整命令行，**必须是列表**；第一项是可执行文件。
    :param cwd: 工作目录；省略时沿用当前进程目录。
    :param cancel_cb: 返回 True 时终止进程（与超时区分记录）。
    """
    command = [str(a) for a in argv]
    if not command:
        return RunResult(ok=False, returncode=-1, error="empty command")

    run_dir = _new_run_dir(location.name)
    stdout_path = os.path.join(run_dir, "stdout.log") if run_dir else ""
    stderr_path = os.path.join(run_dir, "stderr.log") if run_dir else ""

    # WSL 落点要把命令包进 wsl.exe；宿主侧的 cwd 与临时目录对它都不适用，因此这两项
    # 只在本地执行时生效。
    host_command = command
    popen_cwd = cwd or None
    remote = location.source == "wsl"
    if remote:
        status = wsl.detect()
        if not status.usable:
            message = wsl.describe(status)
            logger.error(f"Cannot execute '{location.name}' via WSL: {message}")
            return RunResult(ok=False, returncode=-1, command=command, error=message,
                             run_dir=run_dir, stdout_path=stdout_path,
                             stderr_path=stderr_path)
        distro = location.distro or status.distro
        wsl_cwd = wsl.to_wsl_path(cwd) if cwd else ""
        try:
            host_command = wsl.wrap_argv(command, distro=distro, cwd=wsl_cwd,
                                         supports_cd=status.supports_cd)
        except RuntimeError as e:
            return RunResult(ok=False, returncode=-1, command=command, error=str(e),
                             run_dir=run_dir, stdout_path=stdout_path,
                             stderr_path=stderr_path)
        popen_cwd = None
        logger.info(f"Dispatching '{location.name}' into WSL '{distro}'")

    env = resolver.env_for(location)
    # 远端执行要给 wsl.exe 自身的启动留出预算：发行版未运行时冷启动可达十余秒，
    # 那段时间与工具本身无关，算进工具的超时会让正常命令被误判成超时。
    effective_timeout = max(1.0, float(timeout)) + (wsl.STARTUP_ALLOWANCE if remote else 0.0)
    tmpdir = ""
    if run_dir and not remote:
        tmpdir = os.path.join(run_dir, "tmp")
        try:
            os.makedirs(tmpdir, exist_ok=True)
        except OSError as e:
            logger.warning(f"Cannot create private tmpdir {tmpdir}: {e}")
            tmpdir = ""
        if tmpdir:
            for key in ("TMPDIR", "TEMP", "TMP"):
                env[key] = tmpdir

    if run_dir:
        try:
            atomic_write_json(os.path.join(run_dir, "command.json"), {
                "tool": location.name,
                "source": location.source,
                "executable": location.path,
                "version": location.version,
                "prefix": location.prefix_name,
                "distro": location.distro,
                "argv": command,
                "host_command": host_command,
                "cwd": cwd or os.getcwd(),
                "timeout": timeout,
                "effective_timeout": round(effective_timeout, 1),
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        except OSError as e:
            # 留档失败不阻断执行：结果本身仍然有效，只是少了现场记录。
            logger.warning(f"Cannot write command.json in {run_dir}: {e}")

    logger.info(f"Running '{location.name}' from {location.source}: {' '.join(host_command[:4])}")
    started = time.monotonic()
    timed_out = False
    cancelled = False
    error = ""
    returncode = -1

    try:
        proc = subprocess.Popen(
            host_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=popen_cwd,
            creationflags=no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        error = f"cannot start process: {e}"
        logger.error(f"{error} ({location.path})")
        _write_result(run_dir, {"ok": False, "error": error, "returncode": -1})
        return RunResult(ok=False, returncode=-1, command=host_command, error=error,
                         run_dir=run_dir, stdout_path=stdout_path, stderr_path=stderr_path)

    try:
        with open(stdout_path or os.devnull, "wb") as fout, \
                open(stderr_path or os.devnull, "wb") as ferr:
            assert proc.stdout is not None and proc.stderr is not None
            out_thread = threading.Thread(target=_pump, args=(proc.stdout, fout), daemon=True)
            err_thread = threading.Thread(target=_pump, args=(proc.stderr, ferr), daemon=True)
            out_thread.start()
            err_thread.start()

            deadline = started + effective_timeout
            while proc.poll() is None:
                if cancel_cb is not None and cancel_cb():
                    cancelled = True
                    proc.terminate()
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    proc.terminate()
                    break
                time.sleep(0.1)
            try:
                proc.wait(timeout=_TERM_GRACE)
            except subprocess.TimeoutExpired:
                logger.warning(f"'{location.name}' ignored terminate; killing pid {proc.pid}")
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    error = "process did not exit after kill"
            out_thread.join(timeout=5)
            err_thread.join(timeout=5)
            returncode = proc.returncode if proc.returncode is not None else -1
    except OSError as e:
        error = f"cannot capture output: {e}"
        logger.error(error)

    elapsed = time.monotonic() - started
    ok = returncode == 0 and not timed_out and not cancelled and not error
    result = RunResult(
        ok=ok, returncode=returncode, command=host_command, elapsed=elapsed,
        timed_out=timed_out, cancelled=cancelled, error=error,
        run_dir=run_dir, stdout_path=stdout_path, stderr_path=stderr_path,
        stdout_tail=tail_lines(stdout_path) if stdout_path else [],
        stderr_tail=tail_lines(stderr_path) if stderr_path else [],
    )
    _write_result(run_dir, {
        "ok": ok, "returncode": returncode, "elapsed": round(elapsed, 3),
        "timed_out": timed_out, "cancelled": cancelled, "error": error,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    logger.info(f"Finished '{location.name}': {result.summary()}")
    return result


def _write_result(run_dir: str, payload: Dict[str, object]) -> None:
    """把结果元数据写进运行目录（失败只记日志）。"""
    if not run_dir:
        return
    try:
        atomic_write_json(os.path.join(run_dir, "result.json"), payload)
    except OSError as e:
        logger.warning(f"Cannot write result.json in {run_dir}: {e}")
