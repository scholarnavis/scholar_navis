"""
R Subprocess Boundary (execution isolation)
===========================================
绘图子进程的隔离边界，也是**唯一实现处**。三层隔离各归其位：

* 包隔离（依赖装在哪）—— :mod:`src.core.r_env`（``R_LIBS_USER`` 私有库）；
* 解释器定位（用哪个 R）—— :mod:`src.core.r_engine`；
* 执行边界（进程怎么跑）—— 本模块。

本模块负责的三件事：

1. **进程资源上限**：CPU 时间、地址空间（仅 Linux）、单文件大小、禁用 core dump。
   绘图脚本由模型生成，一个写错的 ``geom`` 就能吃掉整机内存或写出几十 GB 的
   PDF；墙钟超时挡不住"疯狂分配内存"，它只在进程还在跑的时候有效。
2. **私有 TMPDIR**：每次任务一个临时目录，跑完即删，中间文件不散落到系统临时目录。
   刻意**不动** ``HOME``：fontconfig 靠它找用户自装字体（``~/.local/share/fonts``、
   ``~/.fonts``），改掉会让图里的中文字形退化成缺字方框；而"不读用户的 R 配置"
   已经由 ``--vanilla`` 保证（见下），改 ``HOME`` 没有额外收益。
3. **确定的环境**：``--vanilla``（不加载用户的 ``.Rprofile`` / ``.Renviron``）+
   固定时区 + 固定 BLAS/OpenMP 线程数。同一份脚本在同一台机器上每次给出一致的
   图：日期轴不随时区平移，浮点归约顺序也不随线程数变化——这是科研图表可复现的
   前提之一。

为什么用 ``sh -c 'ulimit …; exec "$0" "$@"'`` 而不是 ``preexec_fn``：应用在子线程
里发起绘图，而 ``preexec_fn`` 在多线程进程中不安全（fork 时若别的线程正持有解释器
锁就会死锁）。让 shell 自己设限再 ``exec`` 既没有线程风险，也不需要额外的 C 层
封装；``exec`` 之后进程号不变，``subprocess`` 的超时与终止照常生效。

Windows 没有 ``ulimit``，资源上限退化为"墙钟超时 + 输出截断"，``--vanilla``、私有
TMPDIR 与固定的线程数仍然生效。容器级沙箱（bwrap / Job Object）刻意不做：威胁模型
是"模型写错代码"，不是"恶意用户"，按平台各写一套隔离层的收益与成本不匹配。
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from typing import Dict, Iterator, List

from src.core import r_env
from src.core.platform_env import is_linux, is_windows

logger = logging.getLogger("Core.RSandbox")

#: 单次绘图的 CPU 时间上限（秒）。墙钟超时（plot_engine.R_TIMEOUT_SECONDS）是第一道
#: 闸门，这里是兜底：子进程脱离父进程后墙钟超时管不到它，CPU 上限仍然有效。
R_MAX_CPU_SECONDS = 300

#: 地址空间上限（KB，``ulimit -v`` 的单位）。4 GiB 足够画任何科研图，同时能把
#: "整表笛卡尔积"这类失误挡在 OOM 之前变成一条可读报错。
R_MAX_MEMORY_KB = 4 * 1024 * 1024

#: 单个输出文件大小上限，单位是 512 字节块（``ulimit -f`` 的 POSIX 单位，
#: 实测 ``ulimit -f 8`` 恰好截断在 4096 字节）。1M 块 = 512 MiB。
R_MAX_FILE_BLOCKS = 1024 * 1024

#: 固定时区：R 的日期轴与 ``Sys.time()`` 取值都跟随时区，跟随本机会让同一份数据在
#: 不同机器上画出不同的刻度。
R_TIMEZONE = "UTC"

#: BLAS / OpenMP 线程数固定为 1。两个理由都是硬性的：
#: * 线程数决定浮点归约顺序，同一份数据在不同机器上会得到不同的聚类高度/坐标；
#: * OpenBLAS 按物理内存为每个线程预留缓冲区，多线程时预留量会直接撞上地址
#:   空间上限（实测本机 4 GiB 上限下多线程启动即失败）。
#: 绘图本身不需要并行线性代数，单线程的代价可以忽略。
R_BLAS_THREADS = "1"

#: 需要固定的线程数环境变量（OpenBLAS / OpenMP / MKL 各自读自己的名字）。
_THREAD_ENV_KEYS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")

#: R 因 CPU 时间上限被内核终止时 Python 看到的退出码（``-SIGXCPU``）。
_SIGXCPU_RETURNCODE = -24

#: 撞到地址空间上限时 R 的典型报错（退出码是普通的 1，只能靠文本识别）。
#: 只收 R 自己的确切措辞：太宽的 "out of memory" 会被包里的无关告警命中。
_MEMORY_ERROR_MARKERS = (
    "cannot allocate vector",
    "std::bad_alloc",
    "memory exhausted",
)


def _ulimit_prefix() -> str:
    """拼出 ``ulimit`` 前缀命令；某项不受支持时静默跳过（不能让 R 跑不起来）。"""
    parts = [
        "ulimit -c 0 2>/dev/null || :",                     # 不留 core dump
        f"ulimit -t {R_MAX_CPU_SECONDS} 2>/dev/null || :",  # CPU 秒
        f"ulimit -f {R_MAX_FILE_BLOCKS} 2>/dev/null || :",  # 单文件大小（512B 块）
    ]
    if is_linux():
        # macOS 的 RLIMIT_AS 会让 dyld 在 exec 阶段就失败，只在 Linux 上设。
        parts.append(f"ulimit -v {R_MAX_MEMORY_KB} 2>/dev/null || :")
    return "; ".join(parts)


def plot_argv(executable: str, script_path: str, *args: str) -> List[str]:
    """绘图子进程的命令行：固定 ``--vanilla``，POSIX 上再包一层资源上限。

    ``--vanilla`` 等价于 ``--no-save --no-restore --no-site-file --no-init-file
    --no-environ``：屏蔽用户的 ``.Rprofile`` / ``.Renviron``，与装包、包探测两条
    路径的口径一致（它们本来就用 ``--vanilla``）。私有库靠 ``R_LIBS_USER`` 环境
    变量注入，不受 ``--no-environ`` 影响。
    """
    base = [executable, "--vanilla", script_path, *args]
    if is_windows():
        return base
    return ["/bin/sh", "-c", _ulimit_prefix() + '; exec "$0" "$@"', *base]


def _job_token(job_id: str) -> str:
    """把 job_id 收敛成安全的目录名片段（只保留字母数字与 ``-_``）。"""
    token = "".join(c for c in str(job_id or "") if c.isalnum() or c in "-_")
    return token[:40] or "plot"


@contextmanager
def job_environment(job_id: str) -> Iterator[Dict[str, str]]:
    """一次绘图任务的子进程环境：私有 TMPDIR + 固定时区/线程数，退出时删目录。

    用法::

        with r_sandbox.job_environment(job_id) as env:
            subprocess.run(r_sandbox.plot_argv(exe, script, data), env=env, ...)
    """
    workdir = tempfile.mkdtemp(prefix=f"navis-r-{_job_token(job_id)}-")
    logger.debug("[plot %s] isolated TMPDIR: %s", job_id, workdir)
    try:
        env = r_env.child_env(tmpdir=workdir, timezone=R_TIMEZONE)
        # 并发度属于执行边界（见 R_BLAS_THREADS 注释），库路径仍只由 r_env 决定。
        for key in _THREAD_ENV_KEYS:
            env[key] = R_BLAS_THREADS
        yield env
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def describe_failure(returncode: int, output: str) -> str:
    """把"撞到资源上限"翻译成一句可执行的说明；其它失败返回空串。

    只解释本模块**确实设下**的限制：地址空间只在 Linux 上有，因此内存提示也只在
    Linux 上给（macOS / Windows 的 "cannot allocate vector" 来自系统内存，不能
    说成是我们的 4 GiB）。文件大小越界会让 R 在写设备时报 I/O 错误，已经有 R 自己
    的 stderr 可看，不在此重复解释。
    """
    if returncode == _SIGXCPU_RETURNCODE:
        return (f"R exceeded the {R_MAX_CPU_SECONDS}s CPU limit and was killed. "
                "Simplify the plotting code or aggregate the data before plotting.")
    if not is_linux():
        return ""
    text = (output or "").lower()
    if any(marker in text for marker in _MEMORY_ERROR_MARKERS):
        return (f"R hit the {R_MAX_MEMORY_KB // (1024 * 1024)} GiB memory limit. "
                "Reduce the data size (filter/aggregate rows or columns) and retry.")
    return ""
