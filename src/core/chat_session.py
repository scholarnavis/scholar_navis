"""多标签页对话的自动快照：读写口径的**唯一事实来源**。

为什么需要它
------------
对话此前只存在于内存（``ChatTool.history``）：进程一退（手动关闭、崩溃、断电）
就全丢，唯一的持久化手段是用户手动导出归档。本模块把**每个标签页**的对话持续
落盘，供下次启动按标签页恢复。

存什么、写多勤
--------------
单个 SQLite 数据库：``BASE_DIR/scholar_workspace/chat_session/session.sqlite3``
（表结构与归档共用，见 :mod:`src.core.chat_store`）。与旧的"每标签页一个大
JSON"相比，改一条书签、存一条半截回答都不必重写整段对话：

* ``tabs`` —— 标签页清单（id / 名称 / 顺序 / 活动项）。每次新建、关闭、改名、
  切换标签页时写入，成本与标签页数成正比，与对话长度无关；
* ``messages`` / ``message_references`` / ``message_bookmarks`` —— 会话正文、
  **参考文献逐条成行**、书签逐条成行。整页覆盖写发生在**轮次边界**：用户消息
  入历史后，以及回答收尾（正常 / 取消 / 报错）后。超长正文在库内 zlib 压缩；
* ``inflight`` —— 某标签页**流式进行中**的半条回答，让"崩溃时正在生成的那段"
  也能恢复。为控制写放大，本模块自带节流（默认 :data:`INFLIGHT_MIN_INTERVAL`
  秒且文本有变化才写），超过 :data:`INFLIGHT_MAX_CHARS` 的部分直接截断；
* ``assets`` / ``asset_links`` —— 图片与小体积附件仍落在内容寻址仓库
  （``scholar_workspace/assets``，跨标签页去重），库里只登记"原路径 -> 仓库
  副本"；仓库文件缺失时降级为占位说明，不会让整次恢复失败；
* ``tags`` / ``tab_tags`` —— 对话标签（一个标签下多个对话、一段对话多个标签）。

标签页 id 只由 :func:`new_tab_id` 生成（32 位十六进制），并且**每次进入存储层
前都会校验**：外部（归档 / 手工编辑的文件）永远无法把 ``../`` 之类的相对路径
塞进来。

恢复语义
--------
:func:`load_manifest` 返回 ``(活动标签页 id, [{"id", "name"}, …])``；每个标签页
的消息由 :func:`load_tab` 单独读取，并合并 ``inflight`` 里的半条
（``status="interrupted"``）。**只回灌消息，不触发任何模型请求** —— 由调用方
（标签页容器）保证。

旧版迁移
--------
历史版本的 ``tabs.json`` + ``tab_<id>.json``（以及更早的单文件
``autosave.json`` / ``inflight.json``）在第一次打开数据库时被就地迁入，随后
**移动到** ``chat_session/legacy/`` 而不是删除：升级后用户看到的就是"上次那段
对话仍在一个标签页里"，同时留一份原始文件以便回退排查。迁移是幂等的——目标库
已有标签页时不会重跑。

清理语义
--------
:func:`clear_tab` 删除某标签页的消息、引用、书签与半条，是"用户明确清空该标签页
对话 / 关闭该标签页"的唯一落点：清掉之后下次启动不再恢复。**不删除**资产仓库里
的内容寻址副本——它们可能被其它标签页或已导入的归档共享，按引用删除会误伤。

线程约定
--------
调用方都在 GUI 线程（对话流、窗口关闭、崩溃处理器）。会话库使用 WAL：单写者、
崩溃后自动回滚未提交事务，最坏情况是"丢掉最后一次未提交的写入"，不会留下读得
出来的半截记录。每次操作独立开关连接，避免把连接对象暴露给其它线程。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.core import BASE_DIR
from src.core import chat_assets, chat_store
from src.core.chat_archive import (DEFAULT_TAB_NAME, clean_tab_name,
                                   remap_attachment_paths)

logger = logging.getLogger("Core.ChatSession")

#: 快照目录名（位于 ``BASE_DIR/scholar_workspace`` 下，该目录已被 .gitignore 忽略）。
SESSION_DIRNAME = "chat_session"
#: 会话数据库文件名（历史记录的唯一落点）。
DB_NAME = "session.sqlite3"
#: 旧版文件名：仅用于迁移，迁移后移动到 ``legacy/``。
LEGACY_MANIFEST_NAME = "tabs.json"
LEGACY_TAB_PREFIX = "tab_"
LEGACY_INFLIGHT_PREFIX = "inflight_"
LEGACY_AUTOSAVE_NAME = "autosave.json"
LEGACY_INFLIGHT_NAME = "inflight.json"
LEGACY_DIRNAME = "legacy"

#: 快照格式标识（与归档的 ``FORMAT`` 解耦，两者可各自演进）。结构版本由
#: :data:`src.core.chat_store.SCHEMA_VERSION` 统一记录在库的 ``meta`` 表里，
#: 这里不再维护第二份版本号。
SESSION_FORMAT = chat_store.FORMAT_CHAT_TABS

#: 流式半条的最小落盘间隔（秒）：低于它且文本未变化的重复调用不再写盘。
INFLIGHT_MIN_INTERVAL = 2.0
#: 流式半条的字符上限：超长思维链不值得每 2 秒写一份完整副本，直接截断。
INFLIGHT_MAX_CHARS = 200_000

#: 每个标签页的半条节流状态（``tab_id -> {"at": float, "text": str}``）。
_inflight_last: Dict[str, Dict[str, Any]] = {}

#: 退出 / 崩溃前的落盘回调（见 :func:`register_flush_hook`）。
_flush_hooks: List[Callable[[], None]] = []


# --------------------------------------------------------------------------- #
#  路径与 id
# --------------------------------------------------------------------------- #
def session_dir() -> str:
    """快照目录的绝对路径。"""
    return os.path.join(BASE_DIR, "scholar_workspace", SESSION_DIRNAME)


def db_path() -> str:
    """会话数据库的绝对路径。"""
    return os.path.join(session_dir(), DB_NAME)


def manifest_path() -> str:
    """标签页清单所在文件（历史名字保留：清单如今是数据库里的 ``tabs`` 表）。"""
    return db_path()


def new_tab_id() -> str:
    """生成一个新的标签页 id（32 位十六进制）。"""
    return uuid.uuid4().hex


def is_valid_tab_id(tab_id: Any) -> bool:
    """标签页 id 是否合法（判定口径在 :func:`src.core.chat_store.is_valid_id`）。"""
    return chat_store.is_valid_id(tab_id)


# --------------------------------------------------------------------------- #
#  连接
# --------------------------------------------------------------------------- #
def _open() -> Optional[sqlite3.Connection]:
    """打开（必要时创建）会话数据库；不可用时返回 ``None`` 而不是抛出。

    对话持久化是"尽力而为"的旁路：磁盘只读、文件被占用、库文件损坏，都不该让
    应用启动失败。此时本轮不落盘、日志留痕，用户至少还能用完整功能。

    目录按 0o700 收紧：对话里可能有未公开的研究内容与推理过程。库文件本身是
    0o600；WAL / SHM 伴生文件沿用 umask，靠目录权限保证他人不可读。
    """
    try:
        os.makedirs(session_dir(), mode=0o700, exist_ok=True)
    except OSError:
        pass
    try:
        conn = chat_store.create_database(db_path(), fmt=SESSION_FORMAT,
                                          kind=chat_store.KIND_SESSION, wal=True)
    except Exception as e:      # 数据库不可用不得中断应用
        logger.error("Cannot open session database '%s': %s", db_path(), e)
        return None
    try:
        os.chmod(db_path(), 0o600)
    except OSError:
        pass    # Windows 无 POSIX 权限位，忽略即可
    return conn


def _open_existing() -> Optional[sqlite3.Connection]:
    """只读打开已存在的会话库；文件不存在时返回 ``None``（首次启动的常态）。"""
    path = db_path()
    if not os.path.isfile(path):
        return None
    try:
        return chat_store.connect(path, readonly=True)
    except Exception as e:
        logger.error("Cannot read session database '%s': %s", path, e)
        return None


# --------------------------------------------------------------------------- #
#  标签页清单
# --------------------------------------------------------------------------- #
def save_manifest(active_id: Optional[str], tabs: Any) -> bool:
    """写入标签页清单（id / 名称 / 顺序 / 活动项），返回是否落盘成功。

    只动清单，不触碰任何标签页的会话内容：本函数成本与标签页**数量**相关，
    与对话长度无关，因此在新建 / 关闭 / 改名 / 切换标签页时调用都是安全的。
    """
    entries: List[Dict[str, str]] = []
    seen = set()
    for tab in tabs or []:
        if not isinstance(tab, dict):
            continue
        tab_id = str(tab.get("id") or "")
        if not is_valid_tab_id(tab_id) or tab_id in seen:
            logger.warning("Skipped invalid/duplicated tab entry while saving manifest: %r", tab)
            continue
        seen.add(tab_id)
        entries.append({
            "id": tab_id,
            "name": clean_tab_name(tab.get("name")) or DEFAULT_TAB_NAME,
        })
    if not entries:
        # 清单为空既没有意义，也会让下次启动无处恢复：宁可保留上一份也不写空。
        logger.info("Skip manifest write: no valid tab entry.")
        return False

    active = str(active_id or "")
    if not is_valid_tab_id(active) or active not in seen:
        active = entries[0]["id"]

    conn = _open()
    if conn is None:
        return False
    try:
        return chat_store.put_manifest(conn, active, entries)
    except Exception as e:
        logger.error("Failed to save tab manifest: %s", e)
        return False
    finally:
        conn.close()


def load_manifest() -> Tuple[Optional[str], List[Dict[str, str]]]:
    """读取标签页清单，返回 ``(活动标签页 id, [{"id", "name"}, …])``。

    没有清单（首次启动，或用户从未打开过标签页）时返回 ``(None, [])``；
    若存在旧版 JSON 快照，则先迁移进数据库再返回（见模块 docstring）。
    """
    conn = _open()
    if conn is None:
        return None, []
    try:
        active, tabs = chat_store.read_manifest(conn)
        if tabs:
            return active, _normalize_entries(tabs)
        migrated = _migrate_legacy_json(conn)
        if migrated:
            active, tabs = chat_store.read_manifest(conn)
            return active, _normalize_entries(tabs)
        return None, []
    except Exception as e:
        logger.error("Failed to load tab manifest: %s", e)
        return None, []
    finally:
        conn.close()


def _normalize_entries(tabs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """清单条目归一：名称限长、空名回退默认值、丢弃非法 id。"""
    entries: List[Dict[str, str]] = []
    seen = set()
    for raw in tabs or []:
        if not isinstance(raw, dict):
            continue
        tab_id = str(raw.get("id") or "")
        if not is_valid_tab_id(tab_id) or tab_id in seen:
            continue
        seen.add(tab_id)
        entries.append({"id": tab_id,
                        "name": clean_tab_name(raw.get("name")) or DEFAULT_TAB_NAME})
    return entries


# --------------------------------------------------------------------------- #
#  单标签页快照：写
# --------------------------------------------------------------------------- #
def save_tab(tab_id: str, messages: Any, name: str = "",
             tags: Optional[List[str]] = None) -> bool:
    """覆盖写入某标签页的完整会话，返回是否落盘成功。

    只在轮次边界调用：单次成本与对话长度成正比（序列化 + 压缩 + 写库），轮内
    高频调用请用 :func:`save_inflight`。

    参考文献与书签由 :func:`src.core.chat_store.put_tab` 写进各自的表：正文里
    ``[n]`` 指向的著录、支撑原文、书签锚点因此有独立位置，不会因为"消息字典少
    抄一个字段"而丢失。小体积附件与图片先复制进内容寻址仓库，库里只登记
    "原路径 -> 仓库副本"，原文件被移动或删除后仍能恢复。
    """
    if not is_valid_tab_id(tab_id):
        logger.warning("Rejected save for invalid tab id: %r", tab_id)
        return False
    msgs = [chat_store.sanitize(m) for m in (messages or []) if isinstance(m, dict)]

    # 规划 -> 复制到仓库 -> 标注占位符。消息里保留原始路径，恢复时再由资产
    # 映射统一改写；这样原路径始终是稳定的匹配键。
    embedded, placeholders = chat_assets.plan_assets(
        chat_assets.collect_paths(msgs),
        chat_assets.collect_name_hints(msgs))
    asset_map = chat_assets.materialize_assets(embedded, placeholders)
    chat_assets.annotate_messages(msgs, placeholders, detect_missing=False)

    assets = []
    for original, stored in asset_map.items():
        meta = embedded.get(original) or {}
        assets.append({
            "original": original,
            "sha256": str(meta.get("sha256") or chat_assets.file_digest(stored) or ""),
            "name": str(meta.get("name") or os.path.basename(str(original)) or original),
            "size": int(meta.get("size") or 0),
            "relpath": _relative_to_base(stored),
        })

    conn = _open()
    if conn is None:
        return False
    try:
        chat_store.put_tab(conn, tab_id, msgs, name=clean_tab_name(name) or "",
                           assets=assets, placeholders=placeholders,
                           tags=tags if tags is not None else None)
        return True
    except Exception as e:
        logger.error("Failed to save chat tab %s: %s", tab_id, e)
        return False
    finally:
        conn.close()


def _relative_to_base(path: str) -> str:
    """仓库路径转成相对 ``BASE_DIR`` 的形式（工作区整体搬家后仍能找回）。"""
    absolute = os.path.abspath(str(path))
    root = os.path.abspath(BASE_DIR) + os.sep
    return os.path.relpath(absolute, BASE_DIR) if absolute.startswith(root) else str(path)


def load_inflight(tab_id: str) -> str:
    """读取某标签页的半条回答（流式进行中）；没有时返回空串。

    内存里最新的那份优先：节流窗口内的内容可能还没落盘，但证据清理判断"这轮是否
    正在生成"必须用最新状态，否则会把正在使用的证据当成孤儿删掉。
    """
    if not is_valid_tab_id(tab_id):
        return ""
    cached = _inflight_last.get(tab_id)
    if cached and str(cached.get("text") or "").strip():
        return str(cached["text"])
    conn = _open_existing()
    if conn is None:
        return ""
    try:
        return chat_store.read_inflight(conn, tab_id) or ""
    except Exception as e:
        logger.debug("Failed to read inflight snapshot for %s: %s", tab_id, e)
        return ""
    finally:
        conn.close()


def save_inflight(tab_id: str, content: str, force: bool = False) -> bool:
    """写入某标签页流式进行中的半条回答；节流命中时返回 ``True``（"已是最新"）。

    :param content: 当前累积的 AI 文本（含 ``<think>`` / ``[mcp_process]`` 等标记，
        与历史条目同构，恢复时走同一渲染管线）。
    :param force: 为 ``True`` 时跳过节流（退出 / 崩溃兜底用，必须拿到最后一段文本）。
    """
    if not is_valid_tab_id(tab_id):
        return False
    text = str(content or "")
    if not text.strip():
        return False
    if len(text) > INFLIGHT_MAX_CHARS:
        logger.debug("Inflight snapshot truncated: %d -> %d chars",
                     len(text), INFLIGHT_MAX_CHARS)
        text = text[:INFLIGHT_MAX_CHARS]

    state = _inflight_last.setdefault(tab_id, {"at": 0.0, "text": ""})
    now = time.monotonic()
    if not force:
        if text == state["text"]:
            return True
        if (now - state["at"]) < INFLIGHT_MIN_INTERVAL:
            return True

    conn = _open()
    if conn is None:
        return False
    try:
        chat_store.ensure_tab(conn, tab_id)
        chat_store.put_inflight(conn, tab_id, text)
    except Exception as e:
        logger.error("Failed to save inflight snapshot for %s: %s", tab_id, e)
        return False
    finally:
        conn.close()
    state["at"] = now
    state["text"] = text
    return True


def clear_inflight(tab_id: str) -> None:
    """删除某标签页的半条记录并复位其节流状态（一轮结束、或用户清空对话时调用）。"""
    _inflight_last.pop(tab_id, None)
    if not is_valid_tab_id(tab_id):
        return
    if not os.path.isfile(db_path()):
        return              # 从未落过盘：没有半条可删
    conn = _open()          # 删除是写操作，必须可写打开
    if conn is None:
        return
    try:
        chat_store.delete_inflight(conn, tab_id)
    except Exception as e:
        logger.warning("Failed to clear inflight snapshot for %s: %s", tab_id, e)
    finally:
        conn.close()


def clear_tab(tab_id: str) -> None:
    """删除某标签页的全部会话内容（用户清空对话 / 关闭标签页的唯一落点）。

    只删内容，**不改清单**：调用方（标签页容器）决定该标签页是"留在清单里
    （清空）"还是"从清单中移出（关闭）"。资产仓库里的副本刻意保留——它们可能
    被其它标签页或已导入的归档共享。
    """
    _inflight_last.pop(tab_id, None)
    if not is_valid_tab_id(tab_id):
        return
    if not os.path.isfile(db_path()):
        return
    conn = _open()          # 删除是写操作，必须可写打开
    if conn is None:
        return
    try:
        chat_store.delete_tab(conn, tab_id)
        logger.info("Removed chat tab content: %s", tab_id)
    except Exception as e:
        logger.warning("Failed to clear chat tab %s: %s", tab_id, e)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  单标签页快照：读
# --------------------------------------------------------------------------- #
def _restore_assets(messages: List[Dict[str, Any]], assets: List[Dict[str, Any]],
                    placeholders: Dict[str, Dict[str, Any]]) -> int:
    """把库里的资产映射与占位信息应用到内存消息，返回路径改写处数。

    仓库文件缺失时（用户手工清理、迁移了工作区）不抛错，而是把该条降级为
    ``store_lost`` 占位符，界面会显示文件名与"内嵌副本缺失"。
    """
    asset_map: Dict[str, str] = {}
    lost: Dict[str, Dict[str, Any]] = {}
    for row in assets or []:
        original = str(row.get("original") or "")
        stored = str(row.get("relpath") or "")
        if not original or not stored:
            continue
        # 存的是相对 BASE_DIR 的路径：工作区整体搬家后仍能找回；早期绝对路径
        # （以及仓库外路径）按原样判断，兼容已经落盘的旧快照。
        resolved = stored if os.path.isabs(stored) else os.path.join(BASE_DIR, stored)
        # 只接受仓库内文件：库文件可被手工编辑，不能让它把消息路径指向任意本机文件
        if os.path.isfile(resolved) and chat_assets.is_asset_store_path(resolved):
            asset_map[original] = resolved
        else:
            lost[original] = {
                "status": chat_assets.STATUS_STORE_LOST,
                "name": str(row.get("name") or os.path.basename(original) or original),
            }

    merged = dict(placeholders or {})
    merged.update(lost)
    changed = remap_attachment_paths(messages, asset_map) if asset_map else 0
    chat_assets.annotate_messages(messages, merged, detect_missing=True)
    return changed


def load_tab(tab_id: str) -> List[Dict[str, Any]]:
    """读取某标签页的会话（含未完成的半条）；无可用内容时返回空列表。

    库里的资产映射会在内存中改写成仓库路径，占位说明会标注到对应文件条目上，
    供气泡显示"文件名 + 原因"。
    """
    if not is_valid_tab_id(tab_id):
        logger.warning("Rejected load for invalid tab id: %r", tab_id)
        return []
    conn = _open_existing()
    if conn is None:
        return []
    try:
        messages, assets, placeholders = chat_store.read_tab(conn, tab_id)
        inflight = chat_store.read_inflight(conn, tab_id)
    except Exception as e:
        logger.error("Failed to load chat tab %s: %s", tab_id, e)
        return []
    finally:
        conn.close()

    if not messages:
        # 没有已完成轮次时，孤立的半条回答没有上下文可言，不恢复
        return []

    _restore_assets(messages, assets, placeholders)

    text = str(inflight or "").strip()
    if not text:
        return messages

    last = messages[-1]
    already_finished = (last.get("role") == "assistant"
                        and str(last.get("content") or "").strip() == text)
    if not already_finished:
        # 半条按"被中断"补进历史：与用户主动 Stop 时的状态一致，气泡与续问都按此渲染
        messages.append({"role": "assistant", "content": text, "status": "interrupted"})
    return messages


# --------------------------------------------------------------------------- #
#  标签（一个标签下多个对话 / 一段对话多个标签）
# --------------------------------------------------------------------------- #
def load_tab_tags(tab_id: str) -> List[str]:
    """读取某对话的标签名列表（无库/无标签时为空）。"""
    if not is_valid_tab_id(tab_id):
        return []
    conn = _open_existing()
    if conn is None:
        return []
    try:
        return chat_store.tab_tags(conn, tab_id)
    except Exception as e:
        logger.warning("Failed to read tags of %s: %s", tab_id, e)
        return []
    finally:
        conn.close()


def save_tab_tags(tab_id: str, tags: List[str]) -> List[str]:
    """覆盖写入某对话的标签，返回归一后的标签名。"""
    if not is_valid_tab_id(tab_id):
        return []
    conn = _open()
    if conn is None:
        return []
    try:
        chat_store.ensure_tab(conn, tab_id)
        return chat_store.set_tab_tags(conn, tab_id, tags)
    except Exception as e:
        logger.warning("Failed to save tags of %s: %s", tab_id, e)
        return []
    finally:
        conn.close()


def tabs_with_tag(tag: str) -> List[str]:
    """某标签下的全部对话 id。"""
    conn = _open_existing()
    if conn is None:
        return []
    try:
        return chat_store.tabs_with_tag(conn, tag)
    except Exception as e:
        logger.warning("Failed to query tag %r: %s", tag, e)
        return []
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  标签页附属负载（证据快照）
# --------------------------------------------------------------------------- #
def save_evidence_bundle(tab_id: str, bundle: Dict[str, Any]) -> bool:
    """把该对话的证据快照写进会话库（随历史 / 导出 / 导入一起走）。

    证据的**权威副本**放在这里，向量库只是可重建的索引：换 embedding 模型时
    索引可以重做，这份"文献元数据 + 检索正文"不能丢。
    """
    if not is_valid_tab_id(tab_id) or not isinstance(bundle, dict) or not bundle:
        return False
    conn = _open()
    if conn is None:
        return False
    try:
        chat_store.ensure_tab(conn, tab_id)
        chat_store.put_tab_payload(conn, tab_id, chat_store.PAYLOAD_EVIDENCE, bundle)
        return True
    except Exception as e:
        logger.warning("Failed to save evidence bundle of %s: %s", tab_id, e)
        return False
    finally:
        conn.close()


def load_evidence_bundle(tab_id: str) -> Dict[str, Any]:
    """读取该对话的证据快照；没有时返回空 dict。"""
    if not is_valid_tab_id(tab_id):
        return {}
    conn = _open_existing()
    if conn is None:
        return {}
    try:
        return chat_store.read_tab_payload(conn, tab_id,
                                           chat_store.PAYLOAD_EVIDENCE) or {}
    except Exception as e:
        logger.warning("Failed to read evidence bundle of %s: %s", tab_id, e)
        return {}
    finally:
        conn.close()


def clear_evidence_bundle(tab_id: str) -> bool:
    """删除某对话的证据快照（清除对话 / 关闭标签页时调用）。"""
    if not is_valid_tab_id(tab_id) or not os.path.isfile(db_path()):
        return False
    conn = _open()
    if conn is None:
        return False
    try:
        chat_store.delete_tab_payload(conn, tab_id, chat_store.PAYLOAD_EVIDENCE)
        return True
    except Exception as e:
        logger.warning("Failed to clear evidence bundle of %s: %s", tab_id, e)
        return False
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  旧版 JSON 快照迁移
# --------------------------------------------------------------------------- #
def _read_json(path: str) -> Optional[Any]:
    """读取旧版 JSON 快照；缺失或损坏时返回 ``None``（迁移不得中断启动）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("Cannot read legacy session snapshot '%s': %s", path, e)
        return None


def _messages_of(payload: Any) -> List[Dict[str, Any]]:
    """从快照 payload 取出消息列表，兼容 ``{"messages": [...]}`` 与裸列表两种形态。"""
    if isinstance(payload, dict):
        payload = payload.get("messages")
    if not isinstance(payload, list):
        return []
    return [m for m in payload if isinstance(m, dict)]


_DIGEST_IN_NAME_RE = re.compile(r"([0-9a-f]{64})")


def _legacy_asset_rows(payload: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """把旧快照的 ``assets`` / ``placeholders`` 转成库里的资产行。

    旧格式是 ``{原路径: 仓库路径}``；这里补上摘要与体积（摘要优先从仓库文件名
    解析，解析不出就现算一次哈希），其余字段与新版完全一致。
    """
    if not isinstance(payload, dict):
        return [], {}
    rows: List[Dict[str, Any]] = []
    raw_assets = payload.get("assets")
    if isinstance(raw_assets, dict):
        for original, stored in raw_assets.items():
            if not isinstance(stored, str) or not stored:
                continue
            resolved = stored if os.path.isabs(stored) else os.path.join(BASE_DIR, stored)
            match = _DIGEST_IN_NAME_RE.search(os.path.basename(resolved))
            digest = match.group(1) if match else (chat_assets.file_digest(resolved) or "")
            if not digest:
                # 找不到本体也拿不到摘要：留给占位说明，别写一条无主的资产行。
                continue
            try:
                size = os.path.getsize(resolved)
            except OSError:
                size = 0
            rows.append({
                "original": str(original),
                "sha256": digest,
                "name": os.path.basename(str(original)) or str(original),
                "size": size,
                "relpath": stored,
            })
    raw_placeholders = payload.get("placeholders")
    placeholders = {}
    if isinstance(raw_placeholders, dict):
        placeholders = {str(k): v for k, v in raw_placeholders.items()
                        if isinstance(v, dict)}
    return rows, placeholders


def _legacy_files() -> List[str]:
    """需要迁移的旧文件（只返回真实存在的）。"""
    directory = session_dir()
    found: List[str] = []
    try:
        names = os.listdir(directory)
    except OSError:
        return found
    for name in names:
        if name in (LEGACY_MANIFEST_NAME, LEGACY_AUTOSAVE_NAME, LEGACY_INFLIGHT_NAME) \
                or (name.startswith(LEGACY_TAB_PREFIX) and name.endswith(".json")) \
                or (name.startswith(LEGACY_INFLIGHT_PREFIX) and name.endswith(".json")):
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                found.append(path)
    return found


def _migrate_legacy_json(conn) -> int:
    """把旧版 JSON 快照迁进数据库，返回迁移的对话数（0 表示无可迁移内容）。

    迁移完成后旧文件被**移动**到 ``legacy/`` 子目录：既保证下次启动不会重跑，
    又给用户留一份原始数据。
    """
    files = _legacy_files()
    if not files:
        return 0

    directory = session_dir()
    migrated = 0
    try:
        manifest = _read_json(os.path.join(directory, LEGACY_MANIFEST_NAME))
    except Exception:   # pragma: no cover - _read_json 已吞异常
        manifest = None

    tabs: List[Dict[str, str]] = []
    if isinstance(manifest, dict) and isinstance(manifest.get("tabs"), list):
        for raw in manifest["tabs"]:
            if not isinstance(raw, dict):
                continue
            tab_id = str(raw.get("id") or "")
            if not is_valid_tab_id(tab_id):
                continue
            tabs.append({"id": tab_id,
                         "name": clean_tab_name(raw.get("name")) or DEFAULT_TAB_NAME})

    if tabs:
        active = str(manifest.get("active") or "")
        for entry in tabs:
            payload = _read_json(os.path.join(
                directory, f"{LEGACY_TAB_PREFIX}{entry['id']}.json"))
            messages = _messages_of(payload)
            assets, placeholders = _legacy_asset_rows(payload)
            if messages or assets:
                chat_store.put_tab(conn, entry["id"], messages,
                                   name=entry["name"], assets=assets,
                                   placeholders=placeholders)
                migrated += 1
            inflight = _read_json(os.path.join(
                directory, f"{LEGACY_INFLIGHT_PREFIX}{entry['id']}.json"))
            if isinstance(inflight, dict) and str(inflight.get("content") or "").strip():
                chat_store.put_inflight(conn, entry["id"],
                                        str(inflight.get("content"))[:INFLIGHT_MAX_CHARS])
        chat_store.put_manifest(conn, active, tabs)
    else:
        # 更早的形态：单一 autosave.json（+ inflight.json），没有标签页概念。
        autosave = _read_json(os.path.join(directory, LEGACY_AUTOSAVE_NAME))
        inflight = _read_json(os.path.join(directory, LEGACY_INFLIGHT_NAME))
        if autosave is not None or inflight is not None:
            tab_id = new_tab_id()
            messages = _messages_of(autosave)
            assets, placeholders = _legacy_asset_rows(autosave)
            chat_store.put_tab(conn, tab_id, messages, name=DEFAULT_TAB_NAME,
                               assets=assets, placeholders=placeholders)
            if isinstance(inflight, dict) and str(inflight.get("content") or "").strip():
                chat_store.put_inflight(conn, tab_id,
                                        str(inflight.get("content"))[:INFLIGHT_MAX_CHARS])
            chat_store.put_manifest(conn, tab_id,
                                    [{"id": tab_id, "name": DEFAULT_TAB_NAME}])
            migrated += 1

    if not migrated:
        return 0

    legacy_dir = os.path.join(directory, LEGACY_DIRNAME)
    try:
        os.makedirs(legacy_dir, exist_ok=True)
    except OSError as e:
        logger.warning("Cannot create legacy dir '%s': %s", legacy_dir, e)
    for path in files:
        try:
            os.replace(path, os.path.join(legacy_dir, os.path.basename(path)))
        except OSError as e:
            logger.warning("Failed to archive legacy snapshot '%s': %s", path, e)
    logger.info("Migrated %d legacy JSON conversation(s) into %s (originals kept in %s).",
                migrated, DB_NAME, legacy_dir)
    return migrated


# --------------------------------------------------------------------------- #
#  退出 / 崩溃兜底
# --------------------------------------------------------------------------- #
def register_flush_hook(hook: Callable[[], None]) -> None:
    """登记一枚"退出 / 崩溃前落盘"回调（幂等：同一可调用对象只登记一次）。

    ``MainWindow.closeEvent`` 与 ``main.global_exception_handler`` 都会触发
    :func:`flush_hooks`，因此这里不需要知道是谁在提供快照。
    """
    if hook not in _flush_hooks:
        _flush_hooks.append(hook)


def unregister_flush_hook(hook: Callable[[], None]) -> None:
    """注销回调（标签页被关闭 / 面板被销毁时调用，避免残留已失效的绑定方法）。"""
    try:
        _flush_hooks.remove(hook)
    except ValueError:
        pass


def flush_hooks() -> None:
    """依次调用已登记的回调；单个失败只记日志，绝不阻断退出流程。"""
    for hook in list(_flush_hooks):
        try:
            hook()
        except Exception as e:      # 退出路径必须吞掉一切异常
            logger.error("Session flush hook failed: %s", e)
