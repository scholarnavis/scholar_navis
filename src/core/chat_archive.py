"""聊天记录归档（``.schat`` / ``.json``）的读写口径 —— **唯一事实来源**。

为什么不是纯 JSON
-----------------
1. **体积**：reasoning 模型的 ``content`` 里含整段思维链，另加参考文献与
   Provenance 的 HTML 页脚，明文 JSON 动辄数 MB；
2. **附件**：历史里只存本机绝对路径，换机器或原文件被移动后就失效。

因此归档统一为 **ZIP 容器（DEFLATE 压缩）**：

.. code-block:: text

    chat_history.json      # 标签页与消息（format / version / tabs / attachments …）
    attachments/<name>     # external_files 引用的文件本体，去重后按原名保存

JSON 部分刻意保留 ``indent=2``：压缩后缩进几乎不增加体积，而解包出来仍可人读。

标签页语义
----------
归档内容是**若干个标签页**（``tabs: [{"name": …, "messages": [...]}, …]``）：
导出单个标签页就是只含一个元素的 ``tabs``；"导出全部"则原样带上所有标签页。
读取侧统一走 :func:`extract_tabs`，历史版本（只有 ``messages`` 的旧归档）会被
视作"一个未命名标签页"，因此 :data:`DEFAULT_TAB_NAME` 与 :func:`clean_tab_name`
只在这里定义一次，快照与界面均从本模块取用。

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
#: 容器结构版本：1 = 纯 JSON（旧），2 = ZIP（压缩 + 内嵌附件），3 = ZIP + 多标签页。
ARCHIVE_VERSION = 3

#: 归档内的固定入口名。
ENTRY_JSON = "chat_history.json"
#: 附件在归档内的目录前缀。
ATTACHMENT_DIR = "attachments"

#: 标签页默认名（用户未命名时）。快照（chat_session）与界面创建标签页共用此常量。
DEFAULT_TAB_NAME = "Default Conversation"
#: 标签页名长度上限：超出部分截断，既避免顶栏被撑爆，也防止恶意归档写入超长串。
TAB_NAME_MAX = 80

_ZIP_MAGIC = b"PK\x03\x04"

#: 消息里承载附件路径的字段（``external_files`` 条目的键）。
_ATTACHMENT_PATH_KEYS = ("path", "image_path")

#: 引用条目（``references`` 的条目）里指向本机原文的字段名，见 src/core/references.py。
_REFERENCE_PATH_KEY = "path"


# --------------------------------------------------------------------------- #
#  写
# --------------------------------------------------------------------------- #
def sanitize(value):
    """把任意对象降级为 JSON 可序列化结构（不可序列化的转 str）。

    归档写出与对话快照（:mod:`src.core.chat_session`）共用这一份实现：两处落盘的是
    同一批消息条目，降级规则必须一致，否则会出现"导得出去、存不下来"。
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    try:
        return str(value)
    except Exception:  # pragma: no cover - 纯防御
        return None


def clean_tab_name(name) -> str:
    """把任意标签页名归一为可安全落盘 / 上存档的短字符串。

    去掉控制字符、折叠为单行、限长 :data:`TAB_NAME_MAX`；空串表示"未命名"
    （调用方自行决定回退为 :data:`DEFAULT_TAB_NAME` 还是文件名）。
    """
    text = str(name or "")
    # 控制字符（含换行/制表/回车）一律换成空格：标签名是单行文本，
    # 且会被写进归档 JSON 与界面标签，留着只会让两处显示都出问题。
    text = "".join(" " if ch < " " or ch == "\x7f" else ch for ch in text)
    return " ".join(text.split())[:TAB_NAME_MAX]


def normalize_tabs(tabs: Iterable[Dict]) -> List[Dict[str, Any]]:
    """把 ``[{"name": …, "messages": [...]}, …]`` 归一为可序列化的标签页列表。

    归档写出（本模块）与"导出全部"共用这一份实现：缺失的名字回退
    :data:`DEFAULT_TAB_NAME`，消息逐条 :func:`sanitize`。
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
        })
    return normalized


def extract_tabs(payload) -> List[Tuple[Optional[str], List[Dict]]]:
    """从归档 JSON 取出 ``[(标签名或 None, 原始消息列表), …]``。

    * 新格式：``{"tabs": [{"name": …, "messages": [...]}, …]}``；
    * 旧格式：``{"messages": [...]}`` / ``{"history": [...]}`` / 裸列表 / 单条
      消息 —— 一律视作**一个未命名标签页**（``None`` 表示交由调用方决定名字）。

    只做**结构**抽取，不做消息字段规范化（那是导入任务的职责），
    这样"归档长什么样"与"消息怎么规范化"各自只有一处定义。
    """
    if isinstance(payload, dict):
        tabs = payload.get("tabs")
        if isinstance(tabs, list):
            extracted = [
                (clean_tab_name(t.get("name")) or None,
                 [m for m in (t.get("messages") or []) if isinstance(m, dict)])
                for t in tabs if isinstance(t, dict)
            ]
            if extracted:
                return extracted
        for key in ("messages", "history", "conversation"):
            value = payload.get(key)
            if isinstance(value, list):
                return [(None, [m for m in value if isinstance(m, dict)])]
        if "role" in payload and "content" in payload:
            return [(None, [payload])]
    if isinstance(payload, list):
        return [(None, [m for m in payload if isinstance(m, dict)])]
    return []


def _collect_attachments(messages: Iterable[Dict]) -> List[str]:
    """按出现顺序收集消息引用的附件绝对路径（去重，保持原样字符串）。

    三类来源都要收，缺任一都会让"导出的对话"在别的机器上缺东西：

    * ``external_files``：用户附件与正文跳转链接；
    * ``references[].path``：引用条目里指向本机原文的字段——不收，导入后引用卡还在，
      但"打开原文"会提示文件不存在；
    * 高级 UI 组件声明的成品文件（见 :mod:`src.ui.components.card_registry` 的
      ``artifacts_of``，例如 R 图卡的 PNG/SVG/PDF/脚本/数据）——不收就会出现
      "组件过去了、图没了"。
    """
    seen: List[str] = []
    for msg in messages or []:
        for item in (msg or {}).get("external_files") or []:
            if not isinstance(item, dict):
                continue
            for key in _ATTACHMENT_PATH_KEYS:
                raw = str(item.get(key) or "").strip()
                if raw and raw not in seen:
                    seen.append(raw)
        for item in (msg or {}).get("references") or []:
            if not isinstance(item, dict):
                continue
            raw = str(item.get(_REFERENCE_PATH_KEY) or "").strip()
            if raw and raw not in seen:
                seen.append(raw)
    for path in _card_artifact_paths(messages):
        if path not in seen:
            seen.append(path)
    return seen


def _card_artifact_paths(messages: Iterable[Dict]) -> List[str]:
    """组件依赖的成品文件路径（登记表不可用时静默退化为只收附件）。"""
    try:
        from src.ui.components.card_registry import artifact_paths_of
        return artifact_paths_of(messages)
    except Exception as e:  # pragma: no cover - 纯防御
        logger.debug("Card artifact collection skipped: %s", e)
        return []


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


def write_archive(path: str, tabs: Iterable[Dict]) -> Dict[str, Any]:
    """把若干标签页的聊天记录写入压缩归档，返回统计信息（供日志与结果提示）。

    :param tabs: ``[{"name": 标签名, "messages": [...]}, …]``；只导出一个标签页时
        传单元素列表即可（归档格式与"导出全部"完全相同，不存在两种容器）。

    附件按**内容路径**嵌入归档（跨标签页去重）；读取不到的文件只告警并保留原
    路径，绝不因此中断导出（导出本身必须成功，附件缺失是次要问题）。
    """
    norm_tabs = normalize_tabs(tabs)
    all_msgs = [m for tab in norm_tabs for m in tab["messages"]]

    # 1) 收集可嵌入的附件
    attachment_map: Dict[str, str] = {}
    missing: List[str] = []
    used_names: set = set()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for raw_path in _collect_attachments(all_msgs):
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
            "tabs": norm_tabs,
            # 原路径 -> 归档内路径：导入时据此把附件还原到本机并改写消息
            "attachments": attachment_map,
        }
        try:
            zf.writestr(ENTRY_JSON, json.dumps(payload, ensure_ascii=False, indent=2))
        except (OSError, ValueError) as e:
            logger.error("Write archive payload failed: %s", e)
            raise

    size = os.path.getsize(path) if os.path.exists(path) else 0
    logger.info("Archive written: %s (tabs=%d, messages=%d, attachments=%d, missing=%d, bytes=%d)",
                path, len(norm_tabs), len(all_msgs), len(attachment_map), len(missing), size)
    return {
        "tabs": len(norm_tabs),
        "messages": len(all_msgs),
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
      附件跳转链接）——只改 ``external_files`` 会让这些链接指向旧机路径；
    * ``references[].path``（引用卡里"打开原文"指向的本机文件）；
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
            for key in _ATTACHMENT_PATH_KEYS:
                old = str(item.get(key) or "")
                new = attachment_map.get(old)
                if new:
                    item[key] = new
                    changed += 1
        for item in msg.get("references") or []:
            if not isinstance(item, dict):
                continue
            old = str(item.get(_REFERENCE_PATH_KEY) or "")
            new = attachment_map.get(old)
            if new:
                item[_REFERENCE_PATH_KEY] = new
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
