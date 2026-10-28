"""可执行文件探测与版本比较（唯一实现处）。

为什么单独拿出来
--------------
"跑一次 ``--version`` 并读出结论"是私有前缀、系统安装、WSL 远端三处都要做的事。
它的难点不在启动子进程，而在**版本参数并不统一**（``--version`` / ``-v`` /
``version`` 子命令 / 无参数），以及**输出的第一行未必是版本**。因此这里不做任何
猜测：参数由清单给出，版本号从输出里按语义版本形态提取，提取不到就如实留空。

版本比较只用于一个决策：系统安装的版本是否够新。够新就用系统的，不够新才回退到
私有前缀，避免"用户装了个 0.1 版，应用静默用它跑出不同的结果"。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Dict, Optional, Sequence

from src.core.platform_env import no_window_flags

logger = logging.getLogger("Toolchain.Probe")

#: 语义版本形态：主版本.次版本[.修订]。刻意不匹配 ``r1209`` 这类构建号后缀。
_RE_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def probe(executable: str, version_args: Sequence[str], *,
          env: Optional[Dict[str, str]] = None, timeout: float = 30.0) -> dict:
    """真实运行一次可执行文件，确认可用并读出第一行输出。

    :return: ``{"available", "version", "detail"}``；``version`` 为原始首行，
        语义版本号另行由 :func:`parse_version` 提取。
    """
    info = {"available": False, "version": "", "detail": ""}
    if not executable or not os.path.isfile(executable):
        info["detail"] = "executable not found"
        return info
    cmd = [executable, *[str(a) for a in version_args]]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env=env if env is not None else os.environ,
            creationflags=no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        info["detail"] = str(e)
        logger.warning(f"Probe failed for {executable}: {e}")
        return info

    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    # 无版本参数的工具（bwa / seqtk 这类）把用法打到 stdout/stderr 并返回非零，
    # 此时"有输出"本身就是可执行的证据。传了版本参数就必须成功返回，否则无法确认
    # 这一版到底是哪一个（版本读不出来时，是否满足 min_version 也就无从判断）。
    accepted = bool(out) and (proc.returncode == 0 or not version_args)
    if accepted:
        info.update(available=True, version=out.splitlines()[0].strip()[:200])
        logger.info(f"Probe ok: {os.path.basename(executable)} -> {info['version']}")
    else:
        info["detail"] = (out or f"exit code {proc.returncode}").strip()[:300]
        logger.warning(f"Probe unusable: {executable}: {info['detail']}")
    return info


def parse_version(text: str) -> str:
    """从任意版本输出里提取语义版本号（取第一个匹配，找不到返回空串）。"""
    match = _RE_VERSION.search(str(text or ""))
    if not match:
        return ""
    parts = [match.group(1), match.group(2)]
    if match.group(3):
        parts.append(match.group(3))
    return ".".join(parts)


def version_tuple(text: str) -> tuple:
    """把版本字符串转成可比较的整数元组（无法解析时返回空元组）。"""
    parsed = parse_version(text)
    if not parsed:
        return ()
    return tuple(int(p) for p in parsed.split("."))


def is_at_least(text: str, minimum: str) -> bool:
    """``text`` 表示的版本是否不低于 ``minimum``。

    任一侧无法解析时**返回 True**：宁可让用户用上他自己装的那份，也不因为一个
    读不出版本的包装脚本就强行回退到私有前缀并触发一次数百 MB 的下载。
    """
    if not str(minimum or "").strip():
        return True
    current = version_tuple(text)
    required = version_tuple(minimum)
    if not current or not required:
        return True
    width = max(len(current), len(required))
    return current + (0,) * (width - len(current)) >= required + (0,) * (width - len(required))
