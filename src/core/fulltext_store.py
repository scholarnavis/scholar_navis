"""DOI ↔ 本地 PDF 的关联表（**唯一实现**）。

用户为某篇论文交过一次 PDF 之后，同一篇不该再让他下载第二次：取全文时先查这里，
命中就直接用那份文件，既不联网也不弹窗。关联只在两种情况下失效并自动解除：

* **文件不见了**（被移动/删除/外接盘没插）；
* **内容对不上了**（路径还在，但换成了别的论文——按 DOI 与标题词校验）。

任何一种失效都立刻解除关联并回到"请用户交文件"的流程，绝不拿错文件充当正文：
读错论文比读不到论文更糟。

落点 ``<BASE_DIR>/config/fulltext_links.json``，与其它本地配置同处，随应用目录迁移。
校验分两级：文件指纹（大小 + mtime，变了再看 sha256）没变就直接信任；指纹变了
才重新抽取前 3 页正文核对 DOI/标题，几十毫秒级，不会每次都按整篇解析。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger("Core.FulltextStore")

__all__ = [
    "normalize_doi",
    "links_path",
    "resolve",
    "link",
    "unlink",
    "all_links",
    "clear",
]

#: 校验标题词时使用的最短词长与命中比例（与取全文窗口的宽松口径一致）。
_TOKEN_MIN_LEN = 4
#: 内容核对只读前几页：DOI 与标题都在首页/页眉，读整篇是白烧时间。
_PROBE_PAGES = 3

_lock = threading.RLock()


def links_path() -> str:
    """关联表落点（惰性拼路径：BASE_DIR 可能被测试或打包流程改写）。"""
    from src.core.config_manager import ConfigManager

    return os.path.join(ConfigManager().CONFIG_DIR, "fulltext_links.json")


def normalize_doi(doi: str) -> str:
    """归一化 DOI：小写、去掉协议前缀与 ``doi:``，供键与内容比对共用。"""
    text = str(doi or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/",
                   "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.strip().rstrip(".,;)")


def _alnum(text: str) -> str:
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def _read_index() -> Dict[str, dict]:
    path = links_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        logger.warning("Cannot read the full-text link table (%s): %s", path, e)
        return {}
    items = data.get("links") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return {}
    return {str(k): v for k, v in items.items() if isinstance(v, dict)}


def _write_index(index: Dict[str, dict]) -> None:
    path = links_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "links": index}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("Cannot write the full-text link table (%s): %s", path, e)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as e:
        logger.debug("Cannot hash %s: %s", path, e)
        return ""
    return digest.hexdigest()


def _stat(path: str) -> Tuple[int, int]:
    try:
        info = os.stat(path)
        return int(info.st_size), int(info.st_mtime)
    except OSError:
        return -1, -1


def _content_matches(path: str, doi: str, title: str) -> bool:
    """文件的正文是否确实属于这篇论文（DOI 命中即可；否则看标题词）。"""
    try:
        from src.core.pdf_text import probe_pdf

        found_doi, head_text = probe_pdf(path, pages=_PROBE_PAGES)
    except Exception as e:          # 解析失败：当作"不是可用正文"，解除关联
        logger.info("Linked PDF cannot be parsed (%s): %s", os.path.basename(path), e)
        return False

    target = _alnum(normalize_doi(doi))
    if target:
        if target in _alnum(found_doi) or target in _alnum(head_text):
            return True
    # 扫描版 PDF 常抽不出 DOI：退一步看标题词，命中过半也算对得上。
    tokens = [t for t in re.findall(rf"[A-Za-z]{{{_TOKEN_MIN_LEN},}}", str(title or "").lower())]
    if tokens:
        haystack = _alnum(head_text)
        hits = sum(1 for t in tokens if t in haystack)
        return hits * 2 >= len(tokens)
    return False


def _verify(path: str, doi: str, title: str) -> bool:
    """文件存在且内容对得上。"""
    if not os.path.isfile(path):
        return False
    if not path.lower().endswith(".pdf"):
        return False
    return _content_matches(path, doi, title)


def resolve(doi: str, title: str = "") -> str:
    """取该 DOI 已关联且**校验通过**的本地 PDF；失效则顺手解除关联并返回空串。"""
    key = normalize_doi(doi)
    if not key:
        return ""
    with _lock:
        index = _read_index()
        entry = index.get(key)
        if not entry:
            return ""
        path = str(entry.get("path") or "")
        size, mtime = _stat(path)
        if size < 0:
            logger.info("Linked PDF is gone for %s; the link was dropped.", key)
            index.pop(key, None)
            _write_index(index)
            return ""

        # 快路径：文件没被动过（大小与 mtime 都对得上）就直接信任上次的校验结果。
        if size == int(entry.get("size") or -1) and mtime == int(entry.get("mtime") or -1):
            return path

        sha = _sha256(path)
        if sha and sha == str(entry.get("sha256") or ""):
            entry.update(size=size, mtime=mtime)      # 只是时间戳动了，内容没变
            index[key] = entry
            _write_index(index)
            return path

        if not _verify(path, key, title or str(entry.get("title") or "")):
            logger.warning("Linked PDF no longer matches %s (%s); the link was dropped.",
                           key, os.path.basename(path))
            index.pop(key, None)
            _write_index(index)
            return ""
        entry.update(size=size, mtime=mtime, sha256=sha or entry.get("sha256") or "")
        index[key] = entry
        _write_index(index)
        return path


def link(doi: str, path: str, title: str = "") -> bool:
    """记录"这个 DOI 对应这份 PDF"；写入前校验内容，对不上不记（返回 False）。"""
    key = normalize_doi(doi)
    path = os.path.abspath(str(path or ""))
    if not key or not os.path.isfile(path):
        return False
    if not _verify(path, key, title):
        logger.info("Not linking %s to %s: the PDF content does not match.",
                    key, os.path.basename(path))
        return False
    size, mtime = _stat(path)
    with _lock:
        index = _read_index()
        index[key] = {
            "path": path,
            "doi": key,
            "title": str(title or ""),
            "size": size,
            "mtime": mtime,
            "sha256": _sha256(path),
            "linked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _write_index(index)
    logger.info("Full text linked: %s -> %s", key, path)
    return True


def unlink(doi: str) -> bool:
    key = normalize_doi(doi)
    if not key:
        return False
    with _lock:
        index = _read_index()
        existed = index.pop(key, None) is not None
        if existed:
            _write_index(index)
    return existed


def all_links() -> Dict[str, dict]:
    """全部关联（设置页展示用）；已失效的条目顺手清掉。"""
    with _lock:
        index = _read_index()
        stale = [k for k, v in index.items() if not os.path.isfile(str(v.get("path") or ""))]
        if stale:
            for key in stale:
                index.pop(key, None)
            _write_index(index)
        return index


def clear() -> int:
    """清空全部关联，返回清掉的条数。"""
    with _lock:
        index = _read_index()
        count = len(index)
        if count:
            _write_index({})
    return count
