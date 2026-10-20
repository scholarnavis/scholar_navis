"""多标签页对话的自动快照：读写口径的**唯一事实来源**。

为什么需要它
------------
对话此前只存在于内存（``ChatTool.history``）：进程一退（手动关闭、崩溃、断电）就全丢，
唯一的持久化手段是用户手动导出归档。本模块把**每个标签页**的对话持续落盘，
供下次启动按标签页恢复。

存什么、写多勤
--------------
目录：``BASE_DIR/scholar_workspace/chat_session/``（与归档解包目录同处工作区，
见 :mod:`src.core.chat_archive`）。

* ``tabs.json`` —— 标签页清单（**轻量**：id / 名称 / 当前活动标签页）。每次
  新建、关闭、改名、切换标签页时覆盖写，体积与标签页数成正比，与对话长度无关；
* ``tab_<id>.json`` —— 单个标签页的完整会话快照。体积等于该段对话（含思维链与
  参考文献），因此只在**轮次边界**覆盖写：用户消息入历史后，以及回答收尾
  （正常 / 取消 / 报错）后。写入是原子的（临时文件 + ``os.replace``）；
  图片与小体积附件会复制进 ``scholar_workspace/assets``（内容寻址，跨标签页去重），
  快照里记录 ``assets`` 映射与 ``placeholders`` 占位信息；
* ``inflight_<id>.json`` —— 某标签页**流式进行中**的半条回答，让"崩溃时正在生成的
  那段"也能恢复。为控制写放大，本模块自带节流（默认 :data:`INFLIGHT_MIN_INTERVAL`
  秒且文本有变化才写），超过 :data:`INFLIGHT_MAX_CHARS` 的部分直接截断。

标签页 id 只由 :func:`new_tab_id` 生成（32 位十六进制），并且**每次构造文件路径前
都会校验**：外部（归档 / 手工编辑的清单）永远无法把 ``../`` 之类的相对路径塞进来。

恢复语义
--------
:func:`load_manifest` 返回 ``(活动标签页 id, [{"id", "name"}, …])``；每个标签页的消息由
:func:`load_tab` 单独读取，并合并 ``inflight_<id>.json`` 里的半条（``status="interrupted"``）。
**只回灌消息，不触发任何模型请求** —— 由调用方（标签页容器）保证。

旧版迁移
--------
历史版本的单一快照（``autosave.json`` / ``inflight.json``，无标签页概念）在首次
:func:`load_manifest` 时被就地迁移为一个标签页，随后删除旧文件：升级后用户看到的
就是"上次那段对话仍在一个标签页里"，而不是被丢弃。

清理语义
--------
:func:`clear_tab` 删除某标签页的两个文件，是"用户明确清空该标签页对话 / 关闭该标签页"
的唯一落点：清掉之后下次启动不再恢复。**不删除** ``scholar_workspace/assets`` 里的
内容寻址副本——它们可能被其它标签页或已导入的归档共享，按引用删除会误伤。

线程约定
--------
调用方都在 GUI 线程（对话流、窗口关闭、崩溃处理器）。写盘是原子替换，故即便将来出现
并发调用，最坏也只是"后写覆盖先写"，不会产生读得出来的半截文件。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.core import BASE_DIR
from src.core import chat_assets
from src.core.chat_archive import (DEFAULT_TAB_NAME, clean_tab_name,
                                   remap_attachment_paths, sanitize)

logger = logging.getLogger("Core.ChatSession")

#: 快照目录名（位于 ``BASE_DIR/scholar_workspace`` 下，该目录已被 .gitignore 忽略）。
SESSION_DIRNAME = "chat_session"
#: 标签页清单文件名。
MANIFEST_NAME = "tabs.json"
#: 单标签页快照 / 半条回答的文件名前缀。
TAB_SNAPSHOT_PREFIX = "tab_"
INFLIGHT_PREFIX = "inflight_"

#: 旧版（无标签页）快照文件名，仅用于迁移。
LEGACY_AUTOSAVE_NAME = "autosave.json"
LEGACY_INFLIGHT_NAME = "inflight.json"

#: 快照格式标识与版本（与归档的 ``FORMAT`` / ``VERSION`` 解耦，两者可各自演进）。
#: 1 = 旧版单文件；2 = 清单 + 每标签页一个文件；3 = 增加内容寻址资产与占位符。
SESSION_FORMAT = "scholar_navis_chat_tabs"
SESSION_VERSION = 3

#: 流式半条的最小落盘间隔（秒）：低于它且文本未变化的重复调用不再写盘。
INFLIGHT_MIN_INTERVAL = 2.0
#: 流式半条的字符上限：超长思维链不值得每 2 秒写一份完整副本，直接截断。
INFLIGHT_MAX_CHARS = 200_000

#: 合法标签页 id：仅 32 位十六进制（:func:`new_tab_id` 的产物）。
_TAB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

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


def manifest_path() -> str:
    """标签页清单的绝对路径。"""
    return os.path.join(session_dir(), MANIFEST_NAME)


def new_tab_id() -> str:
    """生成一个新的标签页 id（32 位十六进制，可直接作文件名）。"""
    return uuid.uuid4().hex


def is_valid_tab_id(tab_id: Any) -> bool:
    """标签页 id 是否合法（非法 id 一律拒绝构造文件路径，防目录穿越）。"""
    return bool(_TAB_ID_RE.match(str(tab_id or "")))


def tab_snapshot_path(tab_id: str) -> Optional[str]:
    """单标签页快照路径；``tab_id`` 非法时返回 ``None``。"""
    if not is_valid_tab_id(tab_id):
        logger.warning("Rejected invalid tab id for snapshot path: %r", tab_id)
        return None
    return os.path.join(session_dir(), f"{TAB_SNAPSHOT_PREFIX}{tab_id}.json")


def tab_inflight_path(tab_id: str) -> Optional[str]:
    """单标签页半条回答路径；``tab_id`` 非法时返回 ``None``。"""
    if not is_valid_tab_id(tab_id):
        return None
    return os.path.join(session_dir(), f"{INFLIGHT_PREFIX}{tab_id}.json")


def _legacy_autosave_path() -> str:
    return os.path.join(session_dir(), LEGACY_AUTOSAVE_NAME)


def _legacy_inflight_path() -> str:
    return os.path.join(session_dir(), LEGACY_INFLIGHT_NAME)


# --------------------------------------------------------------------------- #
#  底层读写
# --------------------------------------------------------------------------- #
def _write_json_atomic(path: str, payload: Any) -> bool:
    """原子写入 JSON（0o600）；任何失败只记日志并返回 ``False``，绝不抛出。

    对话里可能含未公开的研究内容与 API 相关的推理过程，故文件按 0o600 落盘
    （Windows 上 chmod 基本无效，靠用户级目录隔离，与 ``secrets.json`` 同策略）。
    """
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".session_", text=False)
        os.close(fd)
    except OSError as e:
        logger.error("Cannot create session temp file in '%s': %s", directory, e)
        return False

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass    # Windows 无 POSIX 权限位，忽略即可
        os.replace(tmp_path, path)
        return True
    except (OSError, TypeError, ValueError) as e:
        logger.error("Failed to write session snapshot '%s': %s", path, e)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False


def _read_json(path: str) -> Optional[Any]:
    """读取 JSON 快照；缺失或损坏时返回 ``None``（绝不因它中断启动）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("Cannot read session snapshot '%s': %s", path, e)
        return None


def _messages_of(payload: Any) -> List[Dict[str, Any]]:
    """从快照 payload 取出消息列表，兼容 ``{"messages": [...]}`` 与裸列表两种形态。"""
    if isinstance(payload, dict):
        payload = payload.get("messages")
    if not isinstance(payload, list):
        return []
    return [m for m in payload if isinstance(m, dict)]


# --------------------------------------------------------------------------- #
#  标签页清单
# --------------------------------------------------------------------------- #
def save_manifest(active_id: Optional[str], tabs: Any) -> bool:
    """覆盖写入标签页清单（id / 名称 / 当前活动标签页），返回是否落盘成功。

    只写清单，不触碰任何标签页的会话内容：本函数成本与标签页**数量**相关，
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

    payload = {
        "format": SESSION_FORMAT,
        "version": SESSION_VERSION,
        "saved_at": time.time(),
        "active": active,
        "tabs": entries,
    }
    return _write_json_atomic(manifest_path(), payload)


def load_manifest() -> Tuple[Optional[str], List[Dict[str, str]]]:
    """读取标签页清单，返回 ``(活动标签页 id, [{"id", "name"}, …])``。

    没有清单（首次启动，或用户从未打开过标签页）时返回 ``(None, [])``；
    若存在旧版单文件快照，则先迁移成"一个标签页"再返回（见模块 docstring）。
    """
    payload = _read_json(manifest_path())
    active, tabs = _parse_manifest(payload)
    if tabs:
        return active, tabs

    migrated = _migrate_legacy_session()
    if migrated is not None:
        return migrated
    return None, []


def _parse_manifest(payload: Any) -> Tuple[Optional[str], List[Dict[str, str]]]:
    """校验清单结构：丢弃非法条目、修正活动标签页、名称回退默认值。"""
    if not isinstance(payload, dict):
        return None, []
    raw_tabs = payload.get("tabs")
    if not isinstance(raw_tabs, list):
        return None, []

    tabs: List[Dict[str, str]] = []
    seen = set()
    for raw in raw_tabs:
        if not isinstance(raw, dict):
            continue
        tab_id = str(raw.get("id") or "")
        if not is_valid_tab_id(tab_id) or tab_id in seen:
            continue
        seen.add(tab_id)
        tabs.append({
            "id": tab_id,
            "name": clean_tab_name(raw.get("name")) or DEFAULT_TAB_NAME,
        })
    if not tabs:
        return None, []

    active = str(payload.get("active") or "")
    if not is_valid_tab_id(active) or active not in seen:
        active = tabs[0]["id"]
    return active, tabs


def _migrate_legacy_session() -> Optional[Tuple[str, List[Dict[str, str]]]]:
    """把旧版单文件快照迁移为一个标签页；无可迁移内容时返回 ``None``。

    迁移是**幂等**的：成功后旧文件被删除，再次调用只会读清单（已被写入）。
    """
    legacy_snapshot = _read_json(_legacy_autosave_path())
    legacy_inflight = _read_json(_legacy_inflight_path())
    if legacy_snapshot is None and legacy_inflight is None:
        return None

    tab_id = new_tab_id()
    name = DEFAULT_TAB_NAME
    ok = True
    if legacy_snapshot is not None:
        snapshot = dict(legacy_snapshot) if isinstance(legacy_snapshot, dict) else {}
        snapshot.update({"format": SESSION_FORMAT, "version": SESSION_VERSION})
        snapshot.setdefault("message_count", len(_messages_of(legacy_snapshot)))
        ok = _write_json_atomic(tab_snapshot_path(tab_id), snapshot) and ok
    if legacy_inflight is not None and isinstance(legacy_inflight, dict):
        inflight = dict(legacy_inflight)
        inflight.update({"format": SESSION_FORMAT, "version": SESSION_VERSION})
        ok = _write_json_atomic(tab_inflight_path(tab_id), inflight) and ok

    if not ok:
        # 迁移失败：保留旧文件原样，下次启动再试（宁可不迁移，也不能丢内容）。
        logger.error("Legacy chat session migration failed; legacy files are kept as-is.")
        return None

    if not save_manifest(tab_id, [{"id": tab_id, "name": name}]):
        logger.error("Legacy chat session migrated but manifest write failed.")
        return None

    for path in (_legacy_autosave_path(), _legacy_inflight_path()):
        try:
            os.remove(path)
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.warning("Failed to remove migrated legacy snapshot '%s': %s", path, e)
    logger.info("Migrated legacy single-conversation snapshot into tab %s.", tab_id)
    return tab_id, [{"id": tab_id, "name": name}]


# --------------------------------------------------------------------------- #
#  单标签页快照：写
# --------------------------------------------------------------------------- #
def save_tab(tab_id: str, messages: Any) -> bool:
    """覆盖写入某标签页的完整会话快照，返回是否落盘成功。

    只在轮次边界调用：单次成本与对话长度成正比（序列化 + 写盘），轮内高频调用
    请用 :func:`save_inflight`。

    自 v3 起，小体积附件与图片会先复制进 ``scholar_workspace/assets`` 内容寻址
    仓库，快照里只记录"原路径 -> 仓库副本"的映射；大文件 / 源文件缺失只记录
    占位信息。恢复时按映射把消息里的原路径改写成本机仓库路径，因此原文件被移动
    或删除后，图片与常用附件仍能一起恢复。
    """
    path = tab_snapshot_path(tab_id)
    if path is None:
        return False
    msgs = [sanitize(m) for m in (messages or []) if isinstance(m, dict)]

    # 规划 -> 复制到仓库 -> 标注占位符。快照消息保留原始路径，恢复时再由
    # assets 映射统一改写；这样原路径始终是稳定的匹配键。
    embedded, placeholders = chat_assets.plan_assets(
        chat_assets.collect_paths(msgs),
        chat_assets.collect_name_hints(msgs))
    asset_map = chat_assets.materialize_assets(embedded, placeholders)
    chat_assets.annotate_messages(msgs, placeholders, detect_missing=False)

    # 快照里的消息保留**原始路径**，只额外记录"原路径 -> 仓库副本"的映射：
    #   * 原始路径是稳定的匹配键，工作区整体搬家后仍能通过映射找回副本；
    #   * 若仓库副本被清理而原文件还在，旧路径依然可用，不会因映射失效而丢图。
    relative_map = {
        original: (os.path.relpath(stored, BASE_DIR)
                   if os.path.abspath(stored).startswith(os.path.abspath(BASE_DIR) + os.sep)
                   else stored)
        for original, stored in asset_map.items()
    }

    payload = {
        "format": SESSION_FORMAT,
        "version": SESSION_VERSION,
        "saved_at": time.time(),
        "message_count": len(msgs),
        "messages": msgs,
        # 原路径 -> 仓库路径（相对 BASE_DIR）：恢复时据此改写消息
        "assets": relative_map,
        "placeholders": placeholders,
    }
    return _write_json_atomic(path, payload)


def save_inflight(tab_id: str, content: str, force: bool = False) -> bool:
    """写入某标签页流式进行中的半条回答；节流命中时返回 ``True``（"已是最新"）。

    :param content: 当前累积的 AI 文本（含 ``<think>`` / ``[mcp_process]`` 等标记，
        与历史条目同构，恢复时走同一渲染管线）。
    :param force: 为 ``True`` 时跳过节流（退出 / 崩溃兜底用，必须拿到最后一段文本）。
    """
    path = tab_inflight_path(tab_id)
    if path is None:
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

    payload = {
        "format": SESSION_FORMAT,
        "version": SESSION_VERSION,
        "saved_at": time.time(),
        "content": text,
    }
    if not _write_json_atomic(path, payload):
        return False
    state["at"] = now
    state["text"] = text
    return True


def clear_inflight(tab_id: str) -> None:
    """删除某标签页的半条文件并复位其节流状态（一轮结束、或用户清空对话时调用）。"""
    _inflight_last.pop(tab_id, None)
    path = tab_inflight_path(tab_id)
    if path is None:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Failed to remove inflight snapshot: %s", e)


def clear_tab(tab_id: str) -> None:
    """删除某标签页的整份会话快照（用户清空对话 / 关闭标签页的唯一落点）。

    只删内容文件，**不改清单**：调用方（标签页容器）决定该标签页是"留在清单里
    （清空）"还是"从清单中移出（关闭）"。
    """
    _inflight_last.pop(tab_id, None)
    for path in (tab_snapshot_path(tab_id), tab_inflight_path(tab_id)):
        if path is None:
            continue
        try:
            os.remove(path)
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.warning("Failed to remove session snapshot '%s': %s", path, e)
            continue
        logger.info("Removed chat tab snapshot: %s", path)


# --------------------------------------------------------------------------- #
#  单标签页快照：读
# --------------------------------------------------------------------------- #
def _restore_snapshot_assets(messages: List[Dict[str, Any]], payload: Any) -> int:
    """把快照里的资产映射与占位信息应用到内存消息，返回改写处数。

    仓库文件缺失时（用户手工清理、迁移了工作区）不抛错，而是把该条降级为
    ``store_lost`` 占位符，界面会显示文件名与"内嵌副本缺失"。
    """
    if not isinstance(payload, dict):
        return 0

    raw_assets = payload.get("assets")
    asset_map: Dict[str, str] = {}
    lost: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw_assets, dict):
        for original, stored in raw_assets.items():
            if not isinstance(stored, str) or not stored:
                continue
            # v3 起存相对 BASE_DIR 的路径：工作区整体搬家后仍能找回；
            # 早期/绝对路径原样判断，兼容已经落盘的快照。
            resolved = stored if os.path.isabs(stored) else os.path.join(BASE_DIR, stored)
            # 只接受仓库内文件：快照可被手工编辑，不能让它把消息路径指向任意本机文件
            if os.path.isfile(resolved) and chat_assets.is_asset_store_path(resolved):
                asset_map[str(original)] = resolved
            else:
                lost[str(original)] = {
                    "status": chat_assets.STATUS_STORE_LOST,
                    "name": os.path.basename(str(original)) or str(original),
                }

    placeholders: Dict[str, Dict[str, Any]] = {}
    raw_placeholders = payload.get("placeholders")
    if isinstance(raw_placeholders, dict):
        placeholders = {str(k): v for k, v in raw_placeholders.items()
                        if isinstance(v, dict)}
    placeholders.update(lost)

    changed = remap_attachment_paths(messages, asset_map) if asset_map else 0
    chat_assets.annotate_messages(messages, placeholders, detect_missing=True)
    return changed


def load_tab(tab_id: str) -> List[Dict[str, Any]]:
    """读取某标签页的会话（含未完成的半条）；无可用快照时返回空列表。

    快照里的 ``assets`` 会在内存中改写成仓库路径，``placeholders``（以及旧快照里
    本机不存在的路径）会标注到对应文件条目上，供气泡显示"文件名 + 原因"。
    旧版快照（无 ``assets``）仍按原路径恢复，不会因此报错。
    """
    snapshot_path = tab_snapshot_path(tab_id)
    if snapshot_path is None:
        return []

    payload = _read_json(snapshot_path)
    messages = _messages_of(payload)
    if not messages:
        # 没有已完成轮次时，孤立的半条回答没有上下文可言，不恢复
        return []

    _restore_snapshot_assets(messages, payload)

    inflight = _read_json(tab_inflight_path(tab_id) or "")
    text = ""
    if isinstance(inflight, dict):
        text = str(inflight.get("content") or "").strip()
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
