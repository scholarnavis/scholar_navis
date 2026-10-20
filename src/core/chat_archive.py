"""聊天记录归档（``.schat`` / ``.json``）的读写口径 —— **唯一事实来源**。

为什么不再用 ZIP + JSON
-----------------------
旧归档是"ZIP 里塞一份 ``chat_history.json`` + ``assets/``"。要记的东西越来越多
（正文、标签、书签、小型附件、图片、参考文献，以及一个标签下的多个对话），这种
"整篇 JSON + 打包"的形态有两个硬伤：一是每次都要把整段对话序列化成一个大字符串，
二是字段全靠约定——遍历消息时漏抄一个键，参考文献、书签就静默消失，事后还查不出来。

现在归档就是**一个 SQLite 数据库文件**（扩展名仍为 ``.schat``）：

.. code-block:: text

    meta       # format / archive_version / generated
    tabs       # 对话（名称、顺序、标签关系）
    messages   # 消息体（含 zlib 压缩的超长正文）
    message_references / message_bookmarks   # 逐条成行的引用与书签
    assets     # 内嵌文件本体（BLOB，内容寻址）
    asset_links / placeholders               # 原路径 -> 资产；未内嵌文件的说明

体积策略集中在 :mod:`src.core.chat_assets`：图片与"小体积文件"按 SHA-256 去重后
内嵌；超过单文件上限、源文件缺失或总量超限的文件**不写本体**，只在 ``placeholders``
里保留源文件名与原因码，导入后由界面显示占位说明。

标签页语义
----------
归档内容是**若干个对话**（``tabs: [{"name": …, "messages": […], "tags": […]}, …]``）：
"导出当前对话"只含一个元素，"导出全部对话"带上所有标签页。读取侧统一走
:func:`extract_tabs`，历史版本（只有 ``messages`` 的旧归档）会被视作"一个未命名
对话"，因此 :data:`DEFAULT_TAB_NAME` 与 :func:`clean_tab_name` 只在这里定义一次，
快照与界面均从本模块取用。

兼容
----
* 读取按魔数判别：SQLite 头走本版分支，``PK`` 走 ZIP 分支，其余按纯 JSON 解析。
* ``archive_version`` 1–3 的旧归档走 ``attachments`` 目录解包；4 走 ZIP 内的
  ``assets`` + 内容寻址仓库；5（本版）是 SQLite 容器。
* 旧版导出的 ``.schat`` / ``.json`` 仍可正常导入，写入一律是新容器。
* :data:`ARCHIVE_VERSION` 记录**容器**版本，与消息 schema 的 ``version`` 解耦。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import urllib.parse
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.core import BASE_DIR
from src.core import chat_assets, chat_store

logger = logging.getLogger("Core.ChatArchive")

#: 消息 schema 与格式标识（与历史版本保持一致，导入侧据此判定"无损"）。
FORMAT = chat_store.FORMAT_CHAT_HISTORY
VERSION = 1
#: 容器结构版本：1 = 纯 JSON（旧），2 = ZIP + 附件，3 = ZIP + 多标签页，
#: 4 = ZIP + 内容寻址资产 + 占位符，5 = SQLite（当前）。
ARCHIVE_VERSION = 5

#: 归档内的固定入口名（仅旧 ZIP 容器使用）。
ENTRY_JSON = "chat_history.json"
#: 旧版附件目录前缀（v2/v3 读取兼容用）。
ATTACHMENT_DIR = "attachments"

#: 标签页默认名（用户未命名时）。快照（chat_session）与界面创建标签页共用此常量。
DEFAULT_TAB_NAME = "Default Conversation"
#: 标签页名长度上限：超出部分截断，既避免顶栏被撑爆，也防止恶意归档写入超长串。
TAB_NAME_MAX = 80

_ZIP_MAGIC = b"PK\x03\x04"

#: 落盘降级口径（历史快照与归档共用，定义在 :mod:`src.core.chat_store`）。
sanitize = chat_store.sanitize


def clean_tab_name(name) -> str:
    """把任意标签页名归一为可安全落盘 / 上存档的短字符串。

    去掉控制字符、折叠为单行、限长 :data:`TAB_NAME_MAX`；空串表示"未命名"
    （调用方自行决定回退为 :data:`DEFAULT_TAB_NAME` 还是文件名）。
    """
    text = str(name or "")
    # 控制字符（含换行/制表/回车）一律换成空格：标签名是单行文本，
    # 且会被写进归档与界面标签，留着只会让两处显示都出问题。
    text = "".join(" " if ch < " " or ch == "\x7f" else ch for ch in text)
    return " ".join(text.split())[:TAB_NAME_MAX]


def normalize_tabs(tabs: Iterable[Dict]) -> List[Dict[str, Any]]:
    """把 ``[{"name": …, "messages": [...], "tags": [...]}, …]`` 归一为可落盘的列表。

    归档写出（本模块）与"导出全部"共用这一份实现：缺失的名字回退
    :data:`DEFAULT_TAB_NAME`，消息逐条 :func:`sanitize`，标签逐条归一。
    """
    normalized: List[Dict[str, Any]] = []
    for tab in tabs or []:
        if not isinstance(tab, dict):
            continue
        messages = tab.get("messages")
        msgs = [sanitize(m) for m in messages if isinstance(m, dict)] \
            if isinstance(messages, list) else []
        normalized.append({
            "name": clean_tab_name(tab.get("name")) or DEFAULT_TAB_NAME,
            "messages": msgs,
            "tags": chat_store.normalize_tag_names(tab.get("tags") or []),
        })
    return normalized


def extract_tabs(payload) -> List[Dict[str, Any]]:
    """从归档 payload 取出 ``[{"name", "messages", "tags"}, …]``。

    * 新格式：``{"tabs": [{"name": …, "messages": […], "tags": […]}, …]}``；
    * 旧格式：``{"messages": [...]}`` / ``{"history": [...]}`` / 裸列表 / 单条
      消息 —— 一律视作**一个未命名标签页**（``name`` 为 ``None``，交由调用方决定）。

    只做**结构**抽取，不做消息字段规范化（那是导入任务的职责），
    这样"归档长什么样"与"消息怎么规范化"各自只有一处定义。
    """
    def _tab(raw: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": clean_tab_name(raw.get("name")) or None,
            "messages": [m for m in (raw.get("messages") or []) if isinstance(m, dict)],
            "tags": chat_store.normalize_tag_names(raw.get("tags") or []),
        }

    if isinstance(payload, dict):
        tabs = payload.get("tabs")
        if isinstance(tabs, list):
            extracted = [_tab(t) for t in tabs if isinstance(t, dict)]
            if extracted:
                return extracted
        for key in ("messages", "history", "conversation"):
            value = payload.get(key)
            if isinstance(value, list):
                return [_tab({"messages": value})]
        if "role" in payload and "content" in payload:
            return [_tab({"messages": [payload]})]
    if isinstance(payload, list):
        return [_tab({"messages": payload})]
    return []


def _collect_attachments(messages: Iterable[Dict]) -> List[str]:
    """按出现顺序收集消息引用的本地文件路径（去重）。

    收集口径集中在 :func:`src.core.chat_assets.collect_paths`：用户附件、
    引用原文、AI 回答缓存的远程图片、组件成品文件。这里保留薄封装是为了让
    归档与快照永远用同一份收集逻辑。
    """
    return chat_assets.collect_paths(messages)


# --------------------------------------------------------------------------- #
#  写
# --------------------------------------------------------------------------- #
def write_archive(path: str, tabs: Iterable[Dict]) -> Dict[str, Any]:
    """把若干对话写入归档（SQLite 容器），返回统计信息（供日志与结果提示）。

    :param tabs: ``[{"name": 对话名, "tags": [...], "messages": [...]}, …]``；
        只导出一个对话时传单元素列表即可（归档格式与"导出全部"完全相同）。

    文件按 SHA-256 去重内嵌；超过体积上限 / 源文件缺失 / 总量超限的文件写入
    ``placeholders``，绝不因此中断导出。整个归档先写到同目录临时文件再原子替换，
    中途失败不会留下半个 ``.schat``。
    """
    norm_tabs = normalize_tabs(tabs)
    all_msgs = [m for tab in norm_tabs for m in tab["messages"]]
    paths = chat_assets.collect_paths(all_msgs)
    embedded, placeholders = chat_assets.plan_assets(
        paths, chat_assets.collect_name_hints(all_msgs))

    target_dir = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".archive_", suffix=".part")
    os.close(fd)

    conn = None
    try:
        conn = chat_store.create_database(tmp_path, fmt=FORMAT,
                                          kind=chat_store.KIND_ARCHIVE)
        written_entries = _write_asset_blobs(conn, embedded, placeholders)
        # 占位信息必须在写消息**之前**定稿：写文件阶段失败的文件也要带上
        chat_assets.annotate_messages(all_msgs, placeholders, detect_missing=False)

        for index, tab in enumerate(norm_tabs):
            tab_id = f"{index:032x}"
            chat_store.put_tab(conn, tab_id, tab["messages"], name=tab["name"],
                               tags=tab.get("tags") or [])
        chat_store.put_placeholders(conn, chat_store.ARCHIVE_SCOPE, placeholders)
        chat_store.set_meta(conn, "version", VERSION)
        chat_store.set_meta(conn, "archive_version", ARCHIVE_VERSION)
        chat_store.set_meta(conn, "generated", _now_iso())
    except Exception:
        if conn is not None:
            conn.close()
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    else:
        conn.close()

    try:
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    size = os.path.getsize(path) if os.path.exists(path) else 0
    logger.info("Archive written: %s (tabs=%d, messages=%d, assets=%d/%d, "
                "placeholders=%d, bytes=%d)",
                path, len(norm_tabs), len(all_msgs), written_entries,
                len(embedded), len(placeholders), size)
    return {
        "tabs": len(norm_tabs),
        "messages": len(all_msgs),
        "attachments": len(embedded),
        "asset_entries": written_entries,
        "placeholders": len(placeholders),
        "bytes": size,
    }


def _write_asset_blobs(conn, embedded: Dict[str, Dict[str, Any]],
                       placeholders: Dict[str, Dict[str, Any]]) -> int:
    """把资产本体写进归档库；单个文件读写失败降级为占位符。返回写入条目数。"""
    written = 0
    for original, meta in list(embedded.items()):
        digest = str(meta.get("sha256") or "")
        source = str(meta.get("source") or original)
        if not digest:
            embedded.pop(original, None)
            placeholders[original] = {"status": chat_assets.STATUS_MISSING,
                                      "name": meta.get("name") or os.path.basename(original)}
            continue
        try:
            with open(source, "rb") as f:
                data = f.read(chat_assets.ASSET_EXTRACT_MAX_BYTES + 1)
            if len(data) > chat_assets.ASSET_EXTRACT_MAX_BYTES:
                raise ValueError("asset exceeds embed limit")
        except (OSError, ValueError) as e:
            logger.warning("Embed asset failed (kept as placeholder): %s (%s)", original, e)
            embedded.pop(original, None)
            placeholders[original] = {"status": chat_assets.STATUS_MISSING,
                                      "name": meta.get("name") or os.path.basename(original)}
            continue
        chat_store.put_assets(conn, chat_store.ARCHIVE_SCOPE, [{
            "original": original,
            "sha256": digest,
            "name": str(meta.get("name") or os.path.basename(original)),
            "size": int(meta.get("size") or len(data)),
            "entry": str(meta.get("entry") or ""),
            "blob": data,
        }])
        written += 1
    return written


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
#  读
# --------------------------------------------------------------------------- #
def read_archive(path: str, extract_root: Optional[str] = None) -> Tuple[Any, Dict[str, Any]]:
    """读取归档，返回 ``(payload, meta)``。

    * ``payload`` —— 与旧版纯 JSON 同形的字典（``tabs`` / ``assets`` /
      ``placeholders``），调用方不必关心容器是 SQLite 还是 ZIP；
    * ``meta`` —— ``{"container": "sqlite"|"zip"|"json", "archive_version": int,
      "attachment_map": {原路径: 本机新路径},
      "asset_placeholders": {原路径: 占位信息}}``。

    ``attachment_map`` 对新容器是"解到内容寻址仓库"的结果，对 v1–v3 是旧解包目录
    中的结果；读取纯 JSON（历史格式）时为空，调用方保持原路径即可。
    """
    with open(path, "rb") as f:
        head = f.read(len(chat_store.SQLITE_MAGIC))

    if head == chat_store.SQLITE_MAGIC:
        return _read_sqlite(path, extract_root)

    if head[:2] == b"PK":
        if head[:4] != _ZIP_MAGIC:
            logger.debug("ZIP variant header %r; parsing as archive anyway", head[:4])
        return _read_zip(path, extract_root)

    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    logger.info("Read legacy plain-JSON chat archive: %s", path)
    return payload, {"container": "json", "archive_version": 1,
                     "attachment_map": {}, "asset_placeholders": {}}


def _read_sqlite(path: str, extract_root: Optional[str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """读 SQLite 容器：消息从表还原，资产 BLOB 解到内容寻址仓库。"""
    conn = chat_store.connect(path, readonly=True)
    try:
        fmt = chat_store.get_meta(conn, "format", "")
        if fmt and fmt not in (FORMAT, chat_store.FORMAT_CHAT_TABS):
            raise ValueError(f"Unsupported archive format: {fmt!r}")
        try:
            archive_version = int(chat_store.get_meta(conn, "archive_version", "")
                                  or ARCHIVE_VERSION)
        except ValueError:
            archive_version = ARCHIVE_VERSION
        placeholders = chat_store.read_placeholders(conn, chat_store.ARCHIVE_SCOPE)
        attachment_map, assets_payload = _extract_sqlite_assets(conn, placeholders)

        tabs: List[Dict[str, Any]] = []
        for row in conn.execute("SELECT id, name FROM tabs ORDER BY position, rowid"):
            tab_id = str(row["id"])
            messages, _assets, _ph = chat_store.read_tab(conn, tab_id)
            tabs.append({
                "name": str(row["name"] or ""),
                "messages": messages,
                "tags": chat_store.tab_tags(conn, tab_id),
            })
        generated = chat_store.get_meta(conn, "generated", "")
        try:
            schema_version = int(chat_store.get_meta(conn, "version", "") or VERSION)
        except ValueError:
            schema_version = VERSION
    finally:
        conn.close()

    payload = {
        "format": FORMAT,
        "version": schema_version,
        "archive_version": archive_version,
        "generated": generated,
        "tabs": tabs,
        "assets": assets_payload,
        "placeholders": placeholders,
        "attachments": {original: str(meta.get("entry") or "")
                        for original, meta in assets_payload.items()},
    }
    logger.info("Read SQLite chat archive: %s (tabs=%d, assets=%d, placeholders=%d)",
                path, len(tabs), len(attachment_map), len(placeholders))
    return payload, {
        "container": "sqlite",
        "archive_version": archive_version,
        "attachment_map": attachment_map,
        "asset_placeholders": placeholders,
    }


def _extract_sqlite_assets(conn, placeholders: Dict[str, Dict[str, Any]]
                           ) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
    """把归档内的资产 BLOB 解到内容寻址仓库，返回 ``(原路径->本机路径, 资产描述)``。"""
    mapping: Dict[str, str] = {}
    described: Dict[str, Dict[str, Any]] = {}
    for row in chat_store.read_assets(conn, chat_store.ARCHIVE_SCOPE):
        original = str(row.get("original") or "")
        digest = str(row.get("sha256") or "")
        if not original:
            continue
        entry = str(row.get("entry") or f"{chat_assets.ASSET_ENTRY_PREFIX}/{digest}")
        described[original] = {"entry": entry, "sha256": digest,
                               "name": str(row.get("name") or os.path.basename(original)),
                               "size": int(row.get("size") or 0)}
        blob = row.get("blob")
        dest = None
        if blob is not None:
            dest = chat_assets.store_blob(digest, os.path.splitext(entry)[1],
                                          bytes(blob), int(row.get("size") or 0))
        if not dest:
            placeholders[original] = {
                "status": chat_assets.STATUS_MISSING,
                "name": str(row.get("name") or os.path.basename(original)),
            }
            continue
        mapping[original] = dest
        chat_assets.remember_store(original, dest)
    return mapping, described


def _extract_attachments(zf: zipfile.ZipFile, payload: Dict[str, Any],
                         archive_path: str, extract_root: Optional[str]) -> Dict[str, str]:
    """把旧版（v2/v3）归档内附件解到本机，返回 ``{原路径: 新路径}``。

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
                written = 0
                with zf.open(arcname) as src, open(dest, "wb") as out:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > chat_assets.ASSET_EXTRACT_MAX_BYTES:
                            raise ValueError("attachment exceeds extraction limit")
                        out.write(chunk)
        except (OSError, KeyError, ValueError) as e:
            logger.warning("Extract attachment failed: %s (%s)", arcname, e)
            try:
                os.remove(dest)
            except OSError:
                pass
            continue
        mapping[str(original)] = dest

    logger.info("Extracted %d/%d archive attachments into %s",
                len(mapping), len(attachment_map), target_dir)
    return mapping


def _read_zip(path: str, extract_root: Optional[str]) -> Tuple[Any, Dict[str, Any]]:
    """读旧版 ZIP 容器（v2–v4）。"""
    with zipfile.ZipFile(path, "r") as zf:
        names = zf.namelist()
        entry = ENTRY_JSON if ENTRY_JSON in names else next(
            (n for n in names if n.lower().endswith(".json")), None)
        if entry is None:
            raise ValueError("Archive does not contain a chat_history.json entry.")
        with zf.open(entry) as f:
            payload = json.loads(f.read().decode("utf-8"))

        attachment_map: Dict[str, str] = {}
        placeholders: Dict[str, Dict[str, Any]] = {}
        if isinstance(payload, dict):
            raw_placeholders = payload.get("placeholders")
            if isinstance(raw_placeholders, dict):
                placeholders = {str(k): v for k, v in raw_placeholders.items()
                                if isinstance(v, dict)}
            assets = payload.get("assets")
            if isinstance(assets, dict) and assets:
                # v4：内容寻址资产，解到本地仓库并跨归档去重
                attachment_map = chat_assets.extract_assets_from_zip(
                    zf, assets, placeholders)
            else:
                # v2/v3：旧 attachments 目录，解到按归档哈希命名的目录
                attachment_map = _extract_attachments(zf, payload, path, extract_root)

    return payload, {
        "container": "zip",
        "archive_version": int((payload or {}).get("archive_version", ARCHIVE_VERSION)
                               if isinstance(payload, dict) else ARCHIVE_VERSION),
        "attachment_map": attachment_map,
        "asset_placeholders": placeholders,
    }


def remap_attachment_paths(messages: Iterable[Dict], attachment_map: Dict[str, str]) -> int:
    """把消息里的旧附件路径改写为解包后的新路径，返回改写处数。

    同时处理两处引用来源，缺一不可：

    * ``external_files`` 的 ``path`` / ``image_path``（缩略图与文件芯片）；
    * ``context_html`` 里的 ``cite://view?path=<url-encoded>`` 链接（正文里的
      附件跳转链接）——只改 ``external_files`` 会让这些链接指向旧机路径；
    * ``references[].path``（引用卡里"打开原文"指向的本机文件）；
    * ``remote_images[url]``（AI 回答里已缓存的远程图片，正文按 URL 索引它）；
    * 正文里高级 UI 组件标记内的成品文件路径（R 图卡等，见
      :func:`src.ui.components.card_registry.rewrite_card_paths`）——不改这些，
      导入后组件虽然重建，预览与下载却都指向旧机路径。
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
            for key in chat_assets.ATTACHMENT_PATH_KEYS:
                old = str(item.get(key) or "")
                new = attachment_map.get(old)
                if new:
                    item[key] = new
                    changed += 1
        for item in msg.get("references") or []:
            if not isinstance(item, dict):
                continue
            old = str(item.get(chat_assets.REFERENCE_PATH_KEY) or "")
            new = attachment_map.get(old)
            if new:
                item[chat_assets.REFERENCE_PATH_KEY] = new
                changed += 1
        remote = msg.get("remote_images")
        if isinstance(remote, dict):
            for url, old in list(remote.items()):
                new = attachment_map.get(str(old or ""))
                if new:
                    remote[url] = new
                    changed += 1
        content = msg.get("content")
        if isinstance(content, str) and content:
            new_content, hits = _rewrite_card_paths(content, attachment_map)
            if hits:
                msg["content"] = new_content
                changed += hits
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


def _rewrite_card_paths(text: str, attachment_map: Dict[str, str]):
    """改写正文中组件标记内的文件路径（登记表不可用时原样返回）。"""
    try:
        from src.ui.components.card_registry import rewrite_card_paths
        return rewrite_card_paths(text, attachment_map)
    except Exception as e:  # pragma: no cover - 纯防御
        logger.debug("Card path rewrite skipped: %s", e)
        return text, 0
