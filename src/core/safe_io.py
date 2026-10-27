"""文件读写基础设施：原子写、损坏隔离、不可信来源的安全落盘。

为什么集中在这里：

* 之前仓里有 5 处各自实现 ``mkstemp + os.replace``，临时文件命名、权限、
  重试与失败清理各不相同，一处修好的问题在另一处仍然存在；
* 配置与索引写入是低频但高价值的操作，写入被截断等于用户数据丢失，因此统一
  加 ``fsync`` 并保留 Windows 上 ``PermissionError`` 的退避重试；
* 文件存在但解析失败（手改出错、断电半写、旧密文残留）与文件不存在是两件
  不同的事：前者必须先保留原件，绝不能静默用默认值覆盖；
* 从 ZIP、外部归档等不可信来源落盘时，路径与体积必须在同一处把关。

异常约定：写入失败一律抛 ``OSError``，由调用方决定降级还是报错；本模块不做
静默兜底，避免出现"看起来保存成功、重启后数据消失"的行为。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Windows 上杀毒、索引服务会短暂占用目标文件，重试即可
DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY = 0.1

#: 单个不可信文件落盘的默认上限
DEFAULT_MAX_BYTES = 64 * 1024 * 1024


# --------------------------------------------------------------------------- #
#  原子写入
# --------------------------------------------------------------------------- #
def _fsync_file(handle) -> None:
    """尽力把文件内容刷到磁盘；不支持 fsync 的文件系统上不为此失败。"""
    try:
        handle.flush()
        os.fsync(handle.fileno())
    except (OSError, ValueError) as e:
        logger.debug(f"fsync unavailable, continuing: {e}")


def atomic_write_text(
    path: str,
    text: str,
    mode: Optional[int] = None,
    retries: int = DEFAULT_RETRIES,
    retry_delay: float = DEFAULT_RETRY_DELAY,
) -> None:
    """以"临时文件 → fsync → 原子替换"的方式写入文本。

    ``mode`` 非空时对临时文件设置权限；``mkstemp`` 已创建为 0o600，
    因此传入更宽松的权限才需要显式 chmod。
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_", text=False)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            _fsync_file(handle)

        if mode is not None:
            try:
                os.chmod(temp_path, mode)
            except OSError as e:
                # Windows 无 POSIX 权限位，交给目录权限隔离
                logger.debug(f"chmod 0o{mode:o} on '{temp_path}' failed: {e}")

        for attempt in range(retries):
            try:
                os.replace(temp_path, path)
                return
            except PermissionError:
                if attempt == retries - 1:
                    raise
                time.sleep(retry_delay)
    except BaseException:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: str,
    data: Any,
    mode: Optional[int] = None,
    indent: int = 4,
    retries: int = DEFAULT_RETRIES,
) -> None:
    """原子写入 JSON。序列化失败与磁盘失败都抛异常，不做静默降级。"""
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    atomic_write_text(path, text, mode=mode, retries=retries)


def quarantine_corrupt(path: str) -> Optional[str]:
    """把无法解析的文件移走并保留，返回备份路径。

    用移动而非复制：原位置腾空后系统可以自愈（下次写入建立新文件），
    同时崩溃现场被完整保留下来供人工核查。移动到同目录可避免跨设备失败。

    失败时返回 ``None``，调用方仍应按"内容不可用"处理。
    """
    if not os.path.exists(path):
        return None

    directory = os.path.dirname(path) or "."
    basename = os.path.basename(path)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = os.path.join(directory, f"corrupt_{stamp}_{basename}")
    index = 1
    while os.path.exists(target):
        target = os.path.join(directory, f"corrupt_{stamp}_{index}_{basename}")
        index += 1

    try:
        os.replace(path, target)
        logger.error(f"Unreadable file quarantined: '{path}' -> '{target}'")
        return target
    except OSError as e:
        logger.error(f"Cannot quarantine unreadable file '{path}': {e}")
        return None


# --------------------------------------------------------------------------- #
#  不可信来源落盘
# --------------------------------------------------------------------------- #
def safe_child_name(name: Any, fallback: str = "unnamed") -> str:
    """把来自不可信数据的名字压成单层文件名，用于拼接落盘路径。

    ``os.path.basename`` 只去掉目录成分，Windows 上还会残留盘符与保留字符，
    因此再剔除路径分隔符与空字节，并拒绝纯点号与空串。
    """
    text = str(name or "").strip().replace("\\", "/").split("/")[-1]
    text = text.replace("\x00", "").strip()
    if text in ("", ".", ".."):
        return fallback
    return text


def ensure_within(root: str, candidate: str) -> bool:
    """判断 ``candidate`` 是否落在 ``root`` 目录内（解析符号链接后比较）。"""
    try:
        root_real = os.path.normcase(os.path.realpath(root))
        cand_real = os.path.normcase(os.path.realpath(candidate))
    except OSError:
        return False
    if cand_real == root_real:
        return True
    return cand_real.startswith(root_real + os.sep)


def copy_with_limit(
    source: str,
    target: str,
    max_bytes: Optional[int] = DEFAULT_MAX_BYTES,
) -> int:
    """分块复制并限制字节数，返回实际写入字节数。

    超限时抛 ``ValueError`` 并删除已写入的目标文件。用于从不可信来源（ZIP
    成员、外部归档）落盘，既避免整份读入内存，也避免压缩炸弹写满磁盘。
    """
    written = 0
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    try:
        with open(source, "rb") as src, open(target, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if max_bytes is not None and written > max_bytes:
                    raise ValueError(f"复制超出上限 {max_bytes} 字节: '{source}'")
                dst.write(chunk)
    except BaseException:
        try:
            os.remove(target)
        except OSError:
            pass
        raise
    return written
