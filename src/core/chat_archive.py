"""聊天记录归档（``.schat`` / ``.json``）的读写口径 —— **唯一事实来源**。

为什么不是纯 JSON
-----------------
1. **体积**：reasoning 模型的 ``content`` 里含整段思维链，另加参考文献与
   Provenance 的 HTML 页脚，明文 JSON 动辄数 MB；
2. **附件**：历史里只存本机绝对路径，换机器或原文件被移动后就失效。

因此归档统一为 **ZIP 容器（DEFLATE 压缩）**：

.. code-block:: text

    chat_history.json      # 与旧版纯 JSON 完全同构（format / version / messages …）
    attachments/<name>     # external_files 引用的文件本体，去重后按原名保存

JSON 部分刻意保留 ``indent=2``：压缩后缩进几乎不增加体积，而解包出来仍可人读。

兼容
----
* 读取按魔数判别：ZIP 走归档分支，否则按纯 JSON 解析 —— 历史版本导出的
  ``.schat`` / ``.json``（无压缩、附件仅路径）依然可导入。
* :data:`ARCHIVE_VERSION` 记录**容器**版本，与消息 schema 的 ``version`` 解耦。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.parse
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.core import BASE_DIR

logger = logging.getLogger("Core.ChatArchive")

#: 消息 schema 与格式标识（与历史版本保持一致，导入侧据此判定"无损"）。
FORMAT = "scholar_navis_chat_history"
VERSION = 1
#: 容器结构版本：1 = 纯 JSON（旧），2 = ZIP（压缩 + 内嵌附件）。
ARCHIVE_VERSION = 2

#: 归档内的固定入口名。
ENTRY_JSON = "chat_history.json"
#: 附件在归档内的目录前缀。
ATTACHMENT_DIR = "attachments"

_ZIP_MAGIC = b"PK\x03\x04"

#: 消息里承载附件路径的字段（``external_files`` 条目的键）。
_ATTACHMENT_PATH_KEYS = ("path", "image_path")


# --------------------------------------------------------------------------- #
#  写
# --------------------------------------------------------------------------- #
def _sanitize(value):
    """把任意对象降级为 JSON 可序列化结构（不可序列化的转 str）。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    try:
        return str(value)
    except Exception:  # pragma: no cover - 纯防御
        return None


def _collect_attachments(messages: Iterable[Dict]) -> List[str]:
    """按出现顺序收集消息引用的附件绝对路径（去重，保持原样字符串）。"""
    seen: List[str] = []
    for msg in messages or []:
        for item in (msg or {}).get("external_files") or []:
            if not isinstance(item, dict):
                continue
            for key in _ATTACHMENT_PATH_KEYS:
                raw = str(item.get(key) or "").strip()
                if raw and raw not in seen:
                    seen.append(raw)
    return seen


def _unique_arcname(used: set, name: str) -> str:
    """生成归档内不冲突的附件名（保留原文件名，冲突时加序号）。"""
    base = os.path.basename(name) or "attachment"
    candidate = base
    idx = 1
    while candidate.lower() in used:
        stem, ext = os.path.splitext(base)
        candidate = f"{stem}_{idx}{ext}"
        idx += 1
    used.add(candidate.lower())
    return f"{ATTACHMENT_DIR}/{candidate}"


def write_archive(path: str, messages: Iterable[Dict]) -> Dict[str, Any]:
    """把聊天记录写入压缩归档，返回统计信息（供日志与结果提示）。

    附件按**内容路径**嵌入归档；读取不到的文件只告警并保留原路径，绝不因此
    中断导出（导出本身必须成功，附件缺失是次要问题）。
    """
    msgs = [_sanitize(m) for m in (messages or [])]

    # 1) 收集可嵌入的附件
    attachment_map: Dict[str, str] = {}
    missing: List[str] = []
    used_names: set = set()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for raw_path in _collect_attachments(msgs):
            if not os.path.isfile(raw_path):
                missing.append(raw_path)
                continue
            arcname = _unique_arcname(used_names, raw_path)
            try:
                zf.write(raw_path, arcname)
            except OSError as e:  # pragma: no cover - 权限/占用等环境问题
                logger.warning("Embed attachment failed (kept as path): %s (%s)", raw_path, e)
                missing.append(raw_path)
                continue
            attachment_map[raw_path] = arcname

        # 2) 写主 JSON
        payload = {
            "format": FORMAT,
            "version": VERSION,
            "archive_version": ARCHIVE_VERSION,
            "generated": _now_iso(),
            "messages": msgs,
            # 原路径 -> 归档内路径：导入时据此把附件还原到本机并改写消息
            "attachments": attachment_map,
        }
        try:
            zf.writestr(ENTRY_JSON, json.dumps(payload, ensure_ascii=False, indent=2))
        except (OSError, ValueError) as e:
            logger.error("Write archive payload failed: %s", e)
            raise

    size = os.path.getsize(path) if os.path.exists(path) else 0
    logger.info("Archive written: %s (messages=%d, attachments=%d, missing=%d, bytes=%d)",
                path, len(msgs), len(attachment_map), len(missing), size)
    return {
        "messages": len(msgs),
        "attachments": len(attachment_map),
        "missing_attachments": missing,
        "bytes": size,
    }


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
#  读
# --------------------------------------------------------------------------- #
def _extract_attachments(zf: zipfile.ZipFile, payload: Dict[str, Any],
                         archive_path: str, extract_root: Optional[str]) -> Dict[str, str]:
    """把归档内附件解到本机，返回 ``{原路径: 新路径}``。

    解包目录按归档文件的绝对路径做哈希，保证"同一归档重复导入"落到同一目录
    （幂等，不会越解越多），不同归档之间又不会互相覆盖。
    """
    attachment_map = payload.get("attachments") or {}
    if not isinstance(attachment_map, dict) or not attachment_map:
        return {}

    root = extract_root or os.path.join(BASE_DIR, "scholar_workspace", "attachments")
    digest = hashlib.md5(os.path.abspath(archive_path).encode("utf-8")).hexdigest()[:10]
    stem = os.path.splitext(os.path.basename(archive_path))[0] or "archive"
    target_dir = os.path.join(root, f"{stem}_{digest}")
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError as e:
        logger.error("Create attachment dir failed: %s (%s)", target_dir, e)
        return {}

    names = set(zf.namelist())
    mapping: Dict[str, str] = {}
    for original, arcname in attachment_map.items():
        if not isinstance(arcname, str) or arcname not in names:
            logger.warning("Attachment entry missing in archive: %s", arcname)
            continue
        # 只取基名落盘：归档条目名不参与目录构造，避免 zip 路径穿越。
        dest = os.path.join(target_dir, os.path.basename(arcname))
        try:
            if not (os.path.isfile(dest) and os.path.getsize(dest)
                    == zf.getinfo(arcname).file_size):
                with zf.open(arcname) as src, open(dest, "wb") as out:
                    out.write(src.read())
        except (OSError, KeyError) as e:
            logger.warning("Extract attachment failed: %s (%s)", arcname, e)
            continue
        mapping[str(original)] = dest

    logger.info("Extracted %d/%d archive attachments into %s",
                len(mapping), len(attachment_map), target_dir)
    return mapping


def read_archive(path: str, extract_root: Optional[str] = None) -> Tuple[Any, Dict[str, Any]]:
    """读取归档，返回 ``(payload, meta)``。

    * ``payload`` —— 归档内的 JSON 结构（dict / list，与旧版纯 JSON 同形）；
    * ``meta`` —— ``{"container": "zip"|"json", "archive_version": int,
      "attachment_map": {原路径: 解包后新路径}}``。

    读取纯 JSON（历史格式）时 ``attachment_map`` 为空，调用方保持原路径即可。
    """
    with open(path, "rb") as f:
        head = f.read(4)

    if head[:2] == b"PK":
        if head != _ZIP_MAGIC:
            logger.debug("ZIP variant header %r; parsing as archive anyway", head)
        return _read_zip(path, extract_root)

    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    logger.info("Read legacy plain-JSON chat archive: %s", path)
    return payload, {"container": "json", "archive_version": 1, "attachment_map": {}}


def _read_zip(path: str, extract_root: Optional[str]) -> Tuple[Any, Dict[str, Any]]:
    with zipfile.ZipFile(path, "r") as zf:
        names = zf.namelist()
        entry = ENTRY_JSON if ENTRY_JSON in names else next(
            (n for n in names if n.lower().endswith(".json")), None)
        if entry is None:
            raise ValueError("Archive does not contain a chat_history.json entry.")
        with zf.open(entry) as f:
            payload = json.loads(f.read().decode("utf-8"))
        attachment_map = {}
        if isinstance(payload, dict):
            attachment_map = _extract_attachments(zf, payload, path, extract_root)

    return payload, {
        "container": "zip",
        "archive_version": int((payload or {}).get("archive_version", ARCHIVE_VERSION)
                               if isinstance(payload, dict) else ARCHIVE_VERSION),
        "attachment_map": attachment_map,
    }


def remap_attachment_paths(messages: Iterable[Dict], attachment_map: Dict[str, str]) -> int:
    """把消息里的旧附件路径改写为解包后的新路径，返回改写处数。

    同时处理两处引用来源，缺一不可：

    * ``external_files`` 的 ``path`` / ``image_path``（缩略图与文件芯片）；
    * ``context_html`` 里的 ``cite://view?path=<url-encoded>`` 链接（正文里的
      附件跳转链接）——只改 ``external_files`` 会让这些链接指向旧机路径。
    """
    if not attachment_map:
        return 0
    changed = 0
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        for item in msg.get("external_files") or []:
            if not isinstance(item, dict):
                continue
            for key in _ATTACHMENT_PATH_KEYS:
                old = str(item.get(key) or "")
                new = attachment_map.get(old)
                if new:
                    item[key] = new
                    changed += 1
        ctx = msg.get("context_html")
        if isinstance(ctx, str) and ctx:
            for old, new in attachment_map.items():
                for variant_old, variant_new in ((old, new),
                                                 (urllib.parse.quote(old), urllib.parse.quote(new))):
                    if variant_old and variant_old in ctx:
                        ctx = ctx.replace(variant_old, variant_new)
                        changed += 1
            msg["context_html"] = ctx
    if changed:
        logger.info("Remapped %d attachment reference(s) to extracted copies", changed)
    return changed
