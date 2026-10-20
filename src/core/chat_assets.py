"""对话资产的内嵌策略：归档（``.schat``）与历史快照（``chat_session``）共用。

为什么需要这一层
----------------
对话里的图片、PDF、组件成品文件此前只以**本机绝对路径**存在。换机器、移动原文件
或清理临时目录后，记录还在、文件却没了，表现为空白缩略图或打不开的链接。

本模块负责回答三个问题，而且只回答这三个问题：

1. **哪些文件要收**：:func:`collect_paths` 汇总 ``external_files``、参考文献原文、
   AI 回答里已缓存的远程图片、高级 UI 组件声明的成品文件；
2. **哪些能内嵌**：:func:`plan_assets` 按体积策略分为"内嵌"与"占位符"，体积上限
   集中在文件头常量里，不散落到调用方；
3. **怎么落地**：归档把本体以 BLOB 写进 SQLite 容器，历史快照写
   ``scholar_workspace/assets`` 内容寻址仓库；两者都按 SHA-256 去重，正文里的
   路径始终是"原路径"，由 :func:`src.core.chat_archive.remap_attachment_paths`
   在导入 / 恢复时统一改写。

占位符不是"错误"，而是一种明确的数据状态：文件太大 / 源文件已不在 / 总量超限 /
旧版本没内嵌，都保留**源文件名**与原因码，界面据此显示"是什么情况"，而不是空白。

兼容
----
旧归档（``archive_version`` 1–3 的 ``attachments`` 目录、4 的 ZIP ``assets``）由
:mod:`src.core.chat_archive` 的旧解包分支读取（:func:`extract_assets_from_zip`）；
旧历史快照没有 ``assets`` 字段，读取时原路径保持不变。两类旧数据在加载时都会被
:func:`annotate_messages` 补上"源文件不可用（旧记录未内嵌）"的占位信息。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
import urllib.parse
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.core import BASE_DIR
from src.core.file_types import IMAGE_EXTS, file_extension
from src.core.image_utils import MAX_IMAGE_BYTES

logger = logging.getLogger("Core.ChatAssets")

#: 单张图片的内嵌上限：直接沿用聊天附件的上传上限，避免两处阈值各自漂移。
EMBED_IMAGE_MAX_BYTES = MAX_IMAGE_BYTES
#: 单个非图片文件的内嵌上限。超过它的 PDF / 数据集只留名字，避免归档无限膨胀。
EMBED_FILE_MAX_BYTES = 8 * 1024 * 1024
#: 单次归档 / 单个标签页快照的内嵌总量上限。超出后剩余文件转为占位符。
EMBED_TOTAL_MAX_BYTES = 256 * 1024 * 1024
#: 从**外部归档**解包单个条目时的硬上限：归档是不可信输入，防止伪造体积信息
#: 的 ZIP 炸弹把磁盘写满（正常条目远小于此值）。
ASSET_EXTRACT_MAX_BYTES = 64 * 1024 * 1024

#: 历史快照的资产仓库目录名（位于 ``BASE_DIR/scholar_workspace`` 下）。
ASSET_STORE_DIRNAME = "assets"
#: 归档内的资产目录前缀。
ASSET_ENTRY_PREFIX = "assets"

#: 占位原因码（界面据此本地化文案，数据文件里不写用户可见文本）。
STATUS_TOO_LARGE = "too_large"
STATUS_MISSING = "missing"
STATUS_TOTAL_LIMIT = "total_limit"
STATUS_STORE_LOST = "store_lost"
STATUS_LEGACY = "legacy"

#: 消息里承载本地文件路径的字段（``external_files`` 条目）。
ATTACHMENT_PATH_KEYS = ("path", "image_path")
#: 引用条目的原文路径字段。
REFERENCE_PATH_KEY = "path"
#: ``context_html`` 里附件跳转链接的本地路径（url-encoded）。
_CITE_PATH_RE = re.compile(r"cite://view\?[^\"']*?path=([^&\"'\s>]+)")

_CHUNK = 1 << 20
_HASH_CACHE_MAX = 512

#: 路径 -> (size, mtime_ns) -> sha256。保存快照是高频操作，避免每轮重读大文件。
_hash_cache: Dict[Tuple[str, int, int], str] = {}

#: 原路径 -> 仓库副本。内存缓存，用于"原文件在本次运行期间被移动 / 删除，
#: 但快照里已有副本"的场景（导出时据此回退，不把已有副本误判为缺失）。
_known_store: Dict[str, str] = {}
_KNOWN_STORE_MAX = 2048


# --------------------------------------------------------------------------- #
#  路径与仓库
# --------------------------------------------------------------------------- #
def asset_store_dir() -> str:
    """内容寻址资产仓库的绝对路径。"""
    return os.path.join(BASE_DIR, "scholar_workspace", ASSET_STORE_DIRNAME)


def is_asset_store_path(path: str) -> bool:
    """路径是否位于资产仓库内（清理临时图片时不能误删这些文件）。"""
    try:
        root = os.path.abspath(asset_store_dir()) + os.sep
        return os.path.abspath(str(path)).startswith(root)
    except (TypeError, ValueError):
        return False


def _is_remote(path: str) -> bool:
    """URL / data URI 不是本地文件，不参与内嵌。"""
    lowered = path.lower()
    return lowered.startswith(("http://", "https://", "data:", "file:"))


def _safe_ext(path: str) -> str:
    """取安全的扩展名（小写、仅字母数字与点、限长）。"""
    ext = os.path.splitext(str(path or ""))[1].lower()
    if not ext or len(ext) > 10:
        return ""
    return ext if all(ch.isalnum() or ch == "." for ch in ext) else ""


def _file_digest(path: str, stat: os.stat_result) -> Optional[str]:
    """计算（带缓存的）SHA-256；读失败返回 ``None``。"""
    key = (os.path.abspath(path), int(stat.st_size), int(stat.st_mtime_ns))
    cached = _hash_cache.get(key)
    if cached:
        return cached
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_CHUNK), b""):
                digest.update(chunk)
    except OSError as e:
        logger.warning("Cannot hash asset '%s': %s", path, e)
        return None
    value = digest.hexdigest()
    if len(_hash_cache) >= _HASH_CACHE_MAX:
        # dict 保持插入顺序：丢掉最早的一条即可（不做 LRU，够用且更简单）
        _hash_cache.pop(next(iter(_hash_cache)))
    _hash_cache[key] = value
    return value


def _is_digest(value: str) -> bool:
    """SHA-256 十六进制串校验：归档来自外部，不能让哈希参与路径构造前不设防。"""
    return (len(value) == 64
            and all(ch in "0123456789abcdef" for ch in value))


def store_path_for(digest: str, ext: str) -> str:
    """资产仓库中的落点（按哈希前两位分桶，避免单目录文件过多）。"""
    if not _is_digest(digest):
        raise ValueError(f"Invalid asset digest: {digest!r}")
    return os.path.join(asset_store_dir(), digest[:2], f"{digest}{ext}")


def file_digest(path: str) -> Optional[str]:
    """计算文件的 SHA-256（带缓存）；文件不可读时返回 ``None``。"""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return _file_digest(path, stat)


def store_blob(digest: str, ext: str, data: bytes,
               expected_size: int = 0) -> Optional[str]:
    """把一段字节写入内容寻址仓库，返回落点路径；校验不通过返回 ``None``。

    归档（SQLite BLOB）解包走这里：摘要必须与库内登记一致、体积必须等于登记值
    （登记为 0 时按实际长度），任何一项不符都拒绝落盘——归档是外部输入，不能
    让它把错误内容写进本地仓库。已存在同样大小的副本时直接复用，实现跨归档去重。
    """
    if not _is_digest(digest) or not isinstance(data, (bytes, bytearray)):
        return None
    size = len(data)
    if size > ASSET_EXTRACT_MAX_BYTES:
        logger.warning("Asset blob rejected: %d bytes exceeds extraction limit", size)
        return None
    if expected_size and size != expected_size:
        logger.warning("Asset blob rejected: size %d != recorded %d", size, expected_size)
        return None
    if hashlib.sha256(bytes(data)).hexdigest() != digest:
        logger.warning("Asset blob rejected: sha256 mismatch")
        return None

    dest = store_path_for(digest, _safe_ext(ext))
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.isfile(dest) and os.path.getsize(dest) == size:
            return dest
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(dest),
                                        prefix=".asset_", suffix=".part")
        os.close(fd)
        try:
            with open(tmp_path, "wb") as out:
                out.write(bytes(data))
            os.replace(tmp_path, dest)
            try:
                os.chmod(dest, 0o600)
            except OSError:
                pass
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    except OSError as e:
        logger.warning("Store asset blob failed: %s (%s)", dest, e)
        return None
    return dest


def remember_store(original: str, stored: str) -> None:
    """登记"原路径 -> 仓库副本"（导入 / 恢复时调用，供原文件被移走后回退）。"""
    _remember_store(original, stored)


def _remember_store(original: str, stored: str) -> None:
    """记住"原路径 -> 仓库副本"，供原文件被移走后的导出 / 快照回退。"""
    if not original or not stored:
        return
    if len(_known_store) >= _KNOWN_STORE_MAX:
        _known_store.pop(next(iter(_known_store)))
    _known_store[str(original)] = str(stored)


# --------------------------------------------------------------------------- #
#  收集
# --------------------------------------------------------------------------- #
def collect_paths(messages: Iterable[Dict]) -> List[str]:
    """按出现顺序收集消息引用的本地文件路径（去重，保持原样字符串）。

    来源缺一不可：用户附件、引用条目原文、AI 回答的远程图片缓存、组件成品文件。
    """
    seen: List[str] = []
    seen_set = set()

    def _add(raw):
        text = str(raw or "").strip()
        if text and text not in seen_set:
            seen_set.add(text)
            seen.append(text)

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        for item in msg.get("external_files") or []:
            if not isinstance(item, dict):
                continue
            for key in ATTACHMENT_PATH_KEYS:
                _add(item.get(key))
        for item in msg.get("references") or []:
            if isinstance(item, dict):
                _add(item.get(REFERENCE_PATH_KEY))
        remote = msg.get("remote_images")
        if isinstance(remote, dict):
            for value in remote.values():
                _add(value)
        # 旧记录可能只剩正文里的 cite://view 链接（external_files 已丢失）：
        # 能从链接里解析出的本地文件同样值得收进来。
        ctx = msg.get("context_html")
        if isinstance(ctx, str) and "cite://view" in ctx:
            for match in _CITE_PATH_RE.finditer(ctx):
                try:
                    _add(urllib.parse.unquote(match.group(1)))
                except (TypeError, ValueError):
                    continue

    for path in _card_artifact_paths(messages):
        _add(path)
    return seen


def collect_name_hints(messages: Iterable[Dict]) -> Dict[str, str]:
    """收集"路径 -> 用户可见文件名"的提示，供占位符显示原始文件名。

    路径一旦进入内容寻址仓库就变成 ``<sha256>.<ext>``，单看路径已经无法还原
    用户当初的 ``paper.pdf`` / ``figure.png``。因此归档与快照都把消息里已有的
    ``name`` 字段带进资产元数据。
    """
    hints: Dict[str, str] = {}
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        for item in msg.get("external_files") or []:
            if not isinstance(item, dict):
                continue
            display = str(item.get("name") or "").strip()
            if not display:
                continue
            for key in ATTACHMENT_PATH_KEYS:
                path = str(item.get(key) or "").strip()
                if path:
                    hints.setdefault(path, display)
        for item in msg.get("references") or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get(REFERENCE_PATH_KEY) or "").strip()
            if path:
                title = str(item.get("title") or "").strip()
                hints.setdefault(path, title or os.path.basename(path))
        remote = msg.get("remote_images")
        if isinstance(remote, dict):
            for url, path in remote.items():
                text = str(path or "").strip()
                if text:
                    hints.setdefault(
                        text, os.path.basename(urllib.parse.urlparse(str(url)).path)
                        or os.path.basename(text))
    return hints


def _card_artifact_paths(messages: Iterable[Dict]) -> List[str]:
    """组件依赖的成品文件路径（登记表不可用时静默退化为只收附件）。"""
    try:
        from src.ui.components.card_registry import artifact_paths_of
        return artifact_paths_of(messages)
    except Exception as e:  # pragma: no cover - 纯防御
        logger.debug("Card artifact collection skipped: %s", e)
        return []


# --------------------------------------------------------------------------- #
#  规划
# --------------------------------------------------------------------------- #
def plan_assets(paths: Iterable[str],
                name_hints: Optional[Dict[str, str]] = None
                ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """把路径分成"可内嵌"与"占位符"两类，返回 ``(embedded, placeholders)``。

    ``embedded[原路径] = {"entry", "sha256", "name", "size"}``；
    ``placeholders[原路径] = {"status", "name", "size"?, "limit"?}``。

    :param name_hints: 路径 -> 用户可见文件名（例如 ``paper.pdf``）。资产进入
        内容寻址仓库后文件名会变成哈希，占位说明必须靠它才能显示原名。
    """
    embedded: Dict[str, Dict[str, Any]] = {}
    placeholders: Dict[str, Dict[str, Any]] = {}
    name_hints = name_hints or {}
    total = 0
    counted_digests = set()

    for raw in paths or []:
        path = str(raw or "").strip()
        if not path or _is_remote(path):
            continue
        name = str(name_hints.get(path) or os.path.basename(path) or path)
        source = path
        stat = None
        try:
            stat = os.stat(source)
        except OSError:
            # 原文件可能在本次运行期间被移走，但快照已经复制过一份：回退到副本
            cached = _known_store.get(path)
            if cached and os.path.isfile(cached):
                source = cached
                try:
                    stat = os.stat(source)
                except OSError:
                    stat = None
        if stat is None or not os.path.isfile(source):
            placeholders[path] = {"status": STATUS_MISSING, "name": name}
            continue

        size = int(stat.st_size)
        is_image = file_extension(path) in IMAGE_EXTS
        limit = EMBED_IMAGE_MAX_BYTES if is_image else EMBED_FILE_MAX_BYTES
        if size > limit:
            placeholders[path] = {"status": STATUS_TOO_LARGE, "name": name,
                                  "size": size, "limit": limit}
            continue

        digest = _file_digest(source, stat)
        if not digest:
            placeholders[path] = {"status": STATUS_MISSING, "name": name}
            continue
        # 同一内容只占一份预算：先看摘要，再判断总量，避免同一文件出现两次时
        # 第二次被误判为"总量超限"。
        if digest not in counted_digests:
            if total + size > EMBED_TOTAL_MAX_BYTES:
                placeholders[path] = {"status": STATUS_TOTAL_LIMIT, "name": name,
                                      "size": size, "limit": EMBED_TOTAL_MAX_BYTES}
                continue
            counted_digests.add(digest)
            total += size

        ext = _safe_ext(path)
        embedded[path] = {
            "entry": f"{ASSET_ENTRY_PREFIX}/{digest}{ext}",
            "sha256": digest,
            "name": name,
            "size": size,
            # 实际读取来源：通常是原路径，原文件被移走时是仓库副本
            "source": source,
        }

    if placeholders:
        logger.info("Asset plan: %d embedded, %d placeholder(s), %.1f MB budget used",
                    len(embedded), len(placeholders), total / (1024 * 1024))
    return embedded, placeholders


# --------------------------------------------------------------------------- #
#  旧版归档（ZIP）读出：新归档是 SQLite，写入路径见 chat_archive
# --------------------------------------------------------------------------- #
def extract_assets_from_zip(zf: zipfile.ZipFile,
                            assets: Dict[str, Any],
                            placeholders: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """把归档内资产解到内容寻址仓库，返回 ``{原路径: 本机路径}``。

    解到仓库而不是"每个归档一个目录"有两个好处：同一附件被多份归档引用时只落一份；
    归档改名 / 重复导入不会越解越多。条目名只取基名，天然避免 ZIP 路径穿越。
    """
    mapping: Dict[str, str] = {}
    names = set(zf.namelist())
    for original, meta in (assets or {}).items():
        if not isinstance(meta, dict):
            continue
        entry = str(meta.get("entry") or "")
        digest = str(meta.get("sha256") or "")
        if entry not in names or not _is_digest(digest):
            placeholders[str(original)] = {
                "status": STATUS_MISSING,
                "name": meta.get("name") or os.path.basename(str(original)),
            }
            continue
        ext = _safe_ext(entry)
        dest = store_path_for(digest, ext)
        expected = int(meta.get("size") or 0)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if not (os.path.isfile(dest) and (not expected
                                               or os.path.getsize(dest) == expected)):
                fd, tmp_path = tempfile.mkstemp(
                    dir=os.path.dirname(dest), prefix=".asset_", suffix=".part")
                os.close(fd)
                try:
                    written = 0
                    with zf.open(entry) as src, open(tmp_path, "wb") as out:
                        while True:
                            chunk = src.read(_CHUNK)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > ASSET_EXTRACT_MAX_BYTES:
                                raise ValueError("asset entry exceeds extraction limit")
                            out.write(chunk)
                    if expected and written != expected:
                        raise ValueError("asset entry size mismatch")
                    os.replace(tmp_path, dest)
                    try:
                        os.chmod(dest, 0o600)
                    except OSError:
                        pass
                finally:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
        except (OSError, KeyError, ValueError) as e:
            logger.warning("Extract asset failed: %s (%s)", entry, e)
            placeholders[str(original)] = {
                "status": STATUS_MISSING,
                "name": meta.get("name") or os.path.basename(str(original)),
            }
            continue
        mapping[str(original)] = dest
        _remember_store(str(original), dest)

    logger.info("Extracted %d/%d archive asset(s) into the local store",
                len(mapping), len(assets or {}))
    return mapping


# --------------------------------------------------------------------------- #
#  历史快照：落到仓库
# --------------------------------------------------------------------------- #
def materialize_assets(embedded: Dict[str, Dict[str, Any]],
                       placeholders: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """把规划好的资产复制进本地仓库，返回 ``{原路径: 仓库路径}``。

    内容寻址天然去重：同一文件被多个标签页引用时只保存一份，后续保存只做
    ``os.path.isfile`` 判定，不重复复制。
    """
    mapping: Dict[str, str] = {}
    for original, meta in list(embedded.items()):
        digest = str(meta.get("sha256") or "")
        if not _is_digest(digest):
            embedded.pop(original, None)
            continue
        dest = store_path_for(digest, _safe_ext(original))
        expected = int(meta.get("size") or 0)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if not (os.path.isfile(dest) and (not expected
                                               or os.path.getsize(dest) == expected)):
                fd, tmp_path = tempfile.mkstemp(
                    dir=os.path.dirname(dest), prefix=".asset_", suffix=".part")
                os.close(fd)
                try:
                    shutil.copyfile(str(meta.get("source") or original), tmp_path)
                    os.replace(tmp_path, dest)
                    try:
                        os.chmod(dest, 0o600)
                    except OSError:
                        pass
                finally:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
        except OSError as e:
            logger.warning("Store asset failed (kept as placeholder): %s (%s)", original, e)
            embedded.pop(original, None)
            placeholders[original] = {
                "status": STATUS_MISSING,
                "name": meta.get("name") or os.path.basename(original),
            }
            continue
        mapping[original] = dest
        _remember_store(original, dest)

    return mapping


# --------------------------------------------------------------------------- #
#  占位符标注
# --------------------------------------------------------------------------- #
def _placeholder_for(path: Any,
                     placeholders: Dict[str, Dict[str, Any]],
                     detect_missing: bool) -> Optional[Dict[str, Any]]:
    text = str(path or "").strip()
    if not text or _is_remote(text):
        return None
    meta = placeholders.get(text)
    if isinstance(meta, dict):
        return dict(meta)
    if detect_missing and not os.path.exists(text):
        return {"status": STATUS_LEGACY, "name": os.path.basename(text) or text,
                "legacy": True}
    return None


def annotate_messages(messages: Iterable[Dict],
                      placeholders: Dict[str, Dict[str, Any]],
                      detect_missing: bool = True) -> int:
    """把占位信息写进消息条目，返回标注处数。

    * ``external_files[*].asset_placeholder`` —— 图片缩略图与附件芯片据此显示
      文件名 + 原因；
    * ``references[*].asset_placeholder`` —— 引用原文不可用时的说明；
    * ``remote_image_placeholders[url]`` —— AI 回答里那张远程图未能内嵌时，正文
      占位块据此说明情况。

    ``detect_missing=True`` 时，本机不存在的本地路径也会补一条
    ``status="missing"``（``legacy=True`` 表示"旧记录本来就没内嵌"）——这是旧版
    归档 / 旧版历史记录唯一的兜底显示方式。
    """
    placeholders = placeholders or {}
    changed = 0
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        for item in msg.get("external_files") or []:
            if not isinstance(item, dict):
                continue
            # 同一附件可能有 path / image_path 两种表示（SVG 与其栅格化 PNG）。
            # "可用"= 至少一条路径既不在占位表里、文件也真的存在；只有全不可用
            # 才标注。这样"超大但本机仍在"的文件仍会标"未内嵌"，而"源 SVG 没了、
            # 但栅格化 PNG 已内嵌"不会被误标成缺失。
            metas = []
            available = False
            has_path = False
            for key in ATTACHMENT_PATH_KEYS:
                path = str(item.get(key) or "").strip()
                if not path:
                    continue
                has_path = True
                if (path not in placeholders and not _is_remote(path)
                        and os.path.exists(path)):
                    available = True
                meta = _placeholder_for(path, placeholders, detect_missing)
                if meta:
                    metas.append(meta)
            if available:
                # 文件后来被找回 / 已内嵌：清掉上一轮留下的过期占位说明
                if item.pop("asset_placeholder", None) is not None:
                    changed += 1
            elif metas:
                item["asset_placeholder"] = metas[0]
                changed += 1
            elif not has_path and str(item.get("name") or "").strip():
                # 旧记录只剩文件名、路径早已丢失：至少让用户看到"缺的是哪个文件"
                item["asset_placeholder"] = {
                    "status": STATUS_LEGACY,
                    "name": str(item.get("name")),
                    "legacy": True,
                }
                changed += 1
        for item in msg.get("references") or []:
            if not isinstance(item, dict):
                continue
            meta = _placeholder_for(item.get(REFERENCE_PATH_KEY), placeholders,
                                    detect_missing)
            if meta:
                item["asset_placeholder"] = meta
                changed += 1
        remote = msg.get("remote_images")
        if isinstance(remote, dict):
            for url, path in remote.items():
                meta = _placeholder_for(path, placeholders, detect_missing)
                if meta:
                    msg.setdefault("remote_image_placeholders", {})[str(url)] = meta
                    changed += 1
    if changed:
        logger.info("Annotated %d asset placeholder(s)", changed)
    return changed


__all__ = [
    "EMBED_IMAGE_MAX_BYTES", "EMBED_FILE_MAX_BYTES", "EMBED_TOTAL_MAX_BYTES",
    "ASSET_EXTRACT_MAX_BYTES", "ASSET_STORE_DIRNAME", "ASSET_ENTRY_PREFIX",
    "STATUS_TOO_LARGE", "STATUS_MISSING", "STATUS_TOTAL_LIMIT", "STATUS_STORE_LOST",
    "STATUS_LEGACY", "ATTACHMENT_PATH_KEYS", "REFERENCE_PATH_KEY",
    "asset_store_dir", "is_asset_store_path", "store_path_for",
    "file_digest", "store_blob", "remember_store",
    "collect_paths", "collect_name_hints", "plan_assets",
    "extract_assets_from_zip",
    "materialize_assets", "annotate_messages",
]
