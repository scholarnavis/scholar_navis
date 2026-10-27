"""聊天附件解析的**唯一实现**：PDF / DOCX / 纯文本 → 可进上下文的文本块。

三条约束决定这里的实现方式：

* **附件不可信**：必须有体积、页数与字符上限。一本 600 页 PDF 或一个 200MB
  文本文件能把进程内存吃光，并把 token 预算直接冲垮；
* **解析昂贵**：结果按"内容指纹"缓存。键里带上 :data:`CACHE_VERSION`，避免
  升级 ``pymupdf4llm`` 之后继续复用旧解析结果；同路径等大小替换（另存为）
  也不会被误判成同一个文件；
* **缓存本身不可信**：缓存目录可能被系统清理、被多个任务进程同时写、被强杀
  留下半截 JSON。因此写入用原子替换，命中前先校验结构。

缓存放在 ``<BASE_DIR>/cache/attachments`` 而不是系统临时目录：临时目录会被
随系统清理，一旦清掉，每一轮对话都要把历史附件全部重新解析一遍。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List

from src.core import BASE_DIR, safe_io

logger = logging.getLogger("Core.AttachmentParser")

#: 解析口径版本。改变切块方式或读取策略时递增，旧缓存随之失效。
CACHE_VERSION = "2"

#: 单个附件的体积上限
MAX_FILE_BYTES = 50 * 1024 * 1024
#: PDF 最多解析的页数
MAX_PAGES = 300
#: 单个附件进入上下文的最大字符数（解析与缓存阶段的上限）
MAX_CHARS_PER_FILE = 400_000
#: 全部附件合计字符数的上限。:func:`trim_total` 支持由调用方传入更贴合的预算，
#: 当前聊天链路直接使用这个固定值。
MAX_TOTAL_CHARS = 1_200_000

#: 缓存条目保留天数
CACHE_MAX_AGE_DAYS = 7
#: 两次缓存清理之间的最小间隔（秒）
_CLEANUP_INTERVAL = 3600.0

_last_cleanup = 0.0


def cache_root() -> str:
    """应用级缓存根目录（附件解析、图生文等共用）。"""
    return os.path.join(BASE_DIR, "cache")


def cache_dir() -> str:
    """附件解析缓存目录。"""
    return os.path.join(cache_root(), "attachments")


def caption_cache_dir() -> str:
    """图生文描述缓存目录。"""
    return os.path.join(cache_root(), "captions")


def _fingerprint(path: str) -> str:
    """缓存键：绝对路径 + 大小 + 纳秒级修改时间 + 解析口径版本。

    不含内容哈希是刻意的：对大文件整份哈希的代价与重新解析同一量级，而
    ``st_mtime_ns`` 已能识别"同路径被替换成另一份文件"的常见情形。
    """
    absolute = os.path.abspath(path)
    try:
        info = os.stat(absolute)
        stamp = f"{info.st_size}|{info.st_mtime_ns}"
    except OSError:
        stamp = "0|0"
    key = f"{CACHE_VERSION}|{absolute}|{stamp}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _cache_file(path: str) -> str:
    return os.path.join(cache_dir(), f"{_fingerprint(path)}.json")


def _read_cache(path: str) -> List[Dict[str, Any]] | None:
    """读缓存；结构不符时按未命中处理（不抛）。"""
    target = _cache_file(path)
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None

    if not isinstance(data, dict) or str(data.get("v")) != CACHE_VERSION:
        return None
    chunks = data.get("chunks")
    if not isinstance(chunks, list):
        return None
    for chunk in chunks:
        if not isinstance(chunk, dict) or "content" not in chunk or "path" not in chunk:
            return None
    return chunks


def _write_cache(path: str, chunks: List[Dict[str, Any]]) -> None:
    """原子写缓存；失败只记录，不影响本次结果。"""
    try:
        safe_io.atomic_write_json(_cache_file(path), {"v": CACHE_VERSION, "chunks": chunks},
                                  indent=0)
    except (OSError, TypeError, ValueError) as e:
        logger.debug("Cannot write attachment cache for %s: %s", path, e)


def _maybe_cleanup() -> None:
    """低频清扫整个缓存目录下的过期条目，避免它们无限增长。

    覆盖 ``cache/`` 下的所有子目录（附件解析、图生文），使清理策略只有一处。
    """
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < _CLEANUP_INTERVAL:
        return
    _last_cleanup = now

    root = cache_root()
    if not os.path.isdir(root):
        return
    cutoff = now - CACHE_MAX_AGE_DAYS * 86400
    removed = 0
    try:
        for current, _dirs, files in os.walk(root):
            for name in files:
                if not name.endswith(".json"):
                    continue
                full = os.path.join(current, name)
                try:
                    if os.path.getmtime(full) < cutoff:
                        os.remove(full)
                        removed += 1
                except OSError:
                    continue
    except OSError as e:
        logger.debug("Attachment cache cleanup skipped: %s", e)
    if removed:
        logger.info("Removed %d expired cache entries", removed)


# --------------------------------------------------------------------------- #
#  解析
# --------------------------------------------------------------------------- #
def _looks_textual(text: str) -> bool:
    """粗判解码结果是否像正文（控制字符占比很低）。"""
    if not text:
        return False
    sample = text[:4096]
    control = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\n\r\t")
    return control / max(len(sample), 1) < 0.05


def decode_text_bytes(raw: bytes) -> str:
    """解码文本：UTF-8 优先，其次 chardet 头部探测，再试 GB18030，最后容错。

    旧实现直接对整份字节跑 ``chardet.detect`` 并完全忽略置信度，中文 GBK 文本
    常被误判成 Windows-1252 后以替换字符入库——静默损坏，比报错更难发现。

    GB18030 作为显式候选保留：UTF-8 已经失败，而本项目的用户主要在中文环境，
    中文编码是此时最可能的答案；它向上兼容 GBK / GB2312。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    guess: Dict[str, Any] = {}
    try:
        import chardet

        guess = chardet.detect(raw[:65536]) or {}
    except Exception as e:          # noqa: BLE001 - 探测失败退化为容错解码
        logger.debug("Encoding detection failed: %s", e)

    encoding = str(guess.get("encoding") or "").strip()
    confidence = float(guess.get("confidence") or 0.0)

    candidates = []
    if encoding and confidence >= 0.5:
        candidates.append(encoding)
    candidates.append("gb18030")

    for candidate in candidates:
        try:
            text = raw.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
        if _looks_textual(text):
            return text
    return raw.decode("utf-8", errors="replace")


def _parse_pdf(path: str, name: str) -> List[Dict[str, Any]]:
    import pymupdf4llm

    chunks: List[Dict[str, Any]] = []
    pages = pymupdf4llm.to_markdown(path, page_chunks=True)
    for chunk in list(pages)[:MAX_PAGES]:
        if not isinstance(chunk, dict):
            continue
        text = str(chunk.get("text") or "").strip()
        if len(text) > 10:
            chunks.append({
                "path": path,
                "name": name,
                "page": (chunk.get("metadata") or {}).get("page", 1),
                "content": text,
            })
    return chunks


def read_docx_text(path: str) -> str:
    """读取 DOCX 的全部段落文本（空段落丢弃）。

    供附件解析与查看器共用，避免同一份文件在两处解出不同内容。
    """
    import docx

    document = docx.Document(path)
    try:
        return "\n".join(p.text for p in document.paragraphs if p.text.strip())
    finally:
        # python-docx 的 Document 底层持有 ZipFile；不关会在 Windows 上占住文件，
        # 用户随后无法重命名或删除该附件。
        package = getattr(getattr(document, "part", None), "package", None)
        closer = getattr(package, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:      # noqa: BLE001 - 关闭失败不影响已取到的文本
                pass


def _parse_docx(path: str, name: str) -> List[Dict[str, Any]]:
    text = read_docx_text(path)
    if len(text) <= 10:
        return []
    return [{"path": path, "name": name, "page": 1, "content": text}]


def _parse_text(path: str, name: str) -> List[Dict[str, Any]]:
    with open(path, "rb") as f:
        head = f.read(8192)
    if b"\x00" in head:
        raise ValueError("文件包含二进制内容，已按非文本附件跳过")

    with open(path, "rb") as f:
        raw = f.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError(f"文件超过 {MAX_FILE_BYTES // (1024 * 1024)}MB 上限")

    text = decode_text_bytes(raw).lstrip("\ufeff").strip()
    if not text:
        return []
    return [{"path": path, "name": name, "page": 1, "content": text}]


def extract(path: str, name: str = "") -> List[Dict[str, Any]]:
    """解析一个附件，返回可并入上下文的文本块。

    命中缓存且结构完好时直接返回。失败向上抛 ``OSError`` / ``ValueError``，
    由调用方转成用户可见的失败说明——静默跳过会让人误以为"模型没读附件"。
    """
    display_name = str(name or os.path.basename(path))
    lower = display_name.lower()

    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise OSError(f"无法读取附件：{e}") from e
    if size > MAX_FILE_BYTES:
        raise ValueError(f"文件 {size // (1024 * 1024)}MB 超过 "
                         f"{MAX_FILE_BYTES // (1024 * 1024)}MB 上限")

    cached = _read_cache(path)
    if cached is not None:
        return cached

    _maybe_cleanup()

    if lower.endswith(".pdf"):
        chunks = _parse_pdf(path, display_name)
    elif lower.endswith(".docx"):
        chunks = _parse_docx(path, display_name)
    elif lower.endswith(".doc"):
        raise ValueError("旧版 .doc 格式不受支持，请另存为 .docx")
    else:
        chunks = _parse_text(path, display_name)

    chunks = _trim_chunks(chunks, MAX_CHARS_PER_FILE)
    _write_cache(path, chunks)
    return chunks


def _trim_chunks(chunks: List[Dict[str, Any]], budget: int) -> List[Dict[str, Any]]:
    """按累计字符数截断文本块，并在最后一块标记被截断。"""
    total = 0
    trimmed: List[Dict[str, Any]] = []
    for chunk in chunks:
        content = str(chunk.get("content") or "")
        if total + len(content) <= budget:
            trimmed.append(chunk)
            total += len(content)
            continue
        remaining = budget - total
        if remaining > 50:
            cut = dict(chunk)
            cut["content"] = content[:remaining] + "\n\n[内容已截断]"
            trimmed.append(cut)
        break
    return trimmed


def trim_total(chunks: List[Dict[str, Any]],
               budget: int | None = MAX_TOTAL_CHARS) -> List[Dict[str, Any]]:
    """对整段对话的附件上下文施加总量上限。

    ``budget`` 可由调用方按主模型预算传入；不传时用 :data:`MAX_TOTAL_CHARS`。
    """
    return _trim_chunks(chunks, int(budget) if budget else MAX_TOTAL_CHARS)


def clear_cache() -> int:
    """删除全部附件解析缓存，返回删除的文件数。"""
    directory = cache_dir()
    if not os.path.isdir(directory):
        return 0
    removed = 0
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        try:
            os.remove(os.path.join(directory, name))
            removed += 1
        except OSError:
            continue
    return removed
