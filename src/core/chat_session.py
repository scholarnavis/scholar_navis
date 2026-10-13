"""上一次对话的自动快照：读写口径的**唯一事实来源**。

为什么需要它
------------
对话此前只存在于内存（``ChatTool.history``）：进程一退（手动关闭、崩溃、断电）就全丢，
唯一的持久化手段是用户手动导出归档。本模块把当前对话持续落盘，供下次启动恢复。

存什么、写多勤
--------------
目录：``BASE_DIR/scholar_workspace/chat_session/``（与归档解包目录同处工作区，
见 :mod:`src.core.chat_archive`）。

* ``autosave.json`` —— 完整会话快照。体积等于整段对话（含思维链与参考文献），
  因此只在**轮次边界**覆盖写：用户消息入历史后，以及回答收尾（正常 / 取消 / 报错）后。
  写入是原子的（临时文件 + ``os.replace``），永远不会留下半截文件；
* ``inflight.json`` —— 流式进行中的半条回答。它让"崩溃时正在生成的那段"也能恢复，
  而不是只剩上一轮。为控制写放大，本模块自带节流（默认 :data:`INFLIGHT_MIN_INTERVAL`
  秒且文本有变化才写），超过 :data:`INFLIGHT_MAX_CHARS` 的部分直接截断。

恢复语义
--------
:func:`load_session` 返回合并后的消息列表：``autosave.json`` 里的已完成轮次，再加上
（当 ``inflight.json`` 有内容、且与最后一条 AI 消息不同时）一条 ``status="interrupted"``
的半条回答。**只回灌消息，不触发任何模型请求** —— 由调用方（Chat 面板）保证。

清理语义
--------
:func:`clear_session` 删除两个文件，是"用户明确清空对话"的唯一落点：清掉之后下次启动
不再恢复，这正是"没清理过就恢复"这条规则的实现方式。

线程约定
--------
调用方都在 GUI 线程（对话流、窗口关闭、崩溃处理器）。写盘是原子替换，故即便将来出现
并发调用，最坏也只是"后写覆盖先写"，不会产生读得出来的半截文件。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional

from src.core import BASE_DIR
from src.core.chat_archive import sanitize

logger = logging.getLogger("Core.ChatSession")

#: 快照目录名（位于 ``BASE_DIR/scholar_workspace`` 下，该目录已被 .gitignore 忽略）。
SESSION_DIRNAME = "chat_session"
#: 完整会话快照文件名。
AUTOSAVE_NAME = "autosave.json"
#: 流式半条文件名。
INFLIGHT_NAME = "inflight.json"

#: 快照格式标识与版本（与归档的 ``FORMAT`` / ``VERSION`` 解耦，两者可各自演进）。
SESSION_FORMAT = "scholar_navis_chat_session"
SESSION_VERSION = 1

#: 流式半条的最小落盘间隔（秒）：低于它且文本未变化的重复调用不再写盘。
INFLIGHT_MIN_INTERVAL = 2.0
#: 流式半条的字符上限：超长思维链不值得每 2 秒写一份完整副本，直接截断。
INFLIGHT_MAX_CHARS = 200_000

#: 流式半条的节流状态（上次落盘时间 / 文本），避免重复写同一份内容。
_inflight_last: Dict[str, Any] = {"at": 0.0, "text": ""}

#: 退出 / 崩溃前的落盘回调（见 :func:`register_flush_hook`）。
_flush_hooks: List[Callable[[], None]] = []


# --------------------------------------------------------------------------- #
#  路径
# --------------------------------------------------------------------------- #
def session_dir() -> str:
    """快照目录的绝对路径。"""
    return os.path.join(BASE_DIR, "scholar_workspace", SESSION_DIRNAME)


def autosave_path() -> str:
    """完整会话快照的绝对路径。"""
    return os.path.join(session_dir(), AUTOSAVE_NAME)


def inflight_path() -> str:
    """流式半条文件的绝对路径。"""
    return os.path.join(session_dir(), INFLIGHT_NAME)


# --------------------------------------------------------------------------- #
#  写
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


def save_session(messages: Any) -> bool:
    """覆盖写入完整会话快照，返回是否落盘成功。

    只在轮次边界调用：单次成本与对话长度成正比（序列化 + 写盘），轮内高频调用请用
    :func:`save_inflight`。
    """
    msgs = [sanitize(m) for m in (messages or []) if isinstance(m, dict)]
    payload = {
        "format": SESSION_FORMAT,
        "version": SESSION_VERSION,
        "saved_at": time.time(),
        "message_count": len(msgs),
        "messages": msgs,
    }
    return _write_json_atomic(autosave_path(), payload)


def save_inflight(content: str, force: bool = False) -> bool:
    """写入流式半条回答；节流命中时返回 ``True``（表示"已是最新，无需写盘"）。

    :param content: 当前累积的 AI 文本（含 ``<think>`` / ``[mcp_process]`` 等标记，
        与历史条目同构，恢复时走同一渲染管线）。
    :param force: 为 ``True`` 时跳过节流（退出 / 崩溃兜底用，必须拿到最后一段文本）。
    """
    text = str(content or "")
    if not text.strip():
        return False
    if len(text) > INFLIGHT_MAX_CHARS:
        logger.debug("Inflight snapshot truncated: %d -> %d chars",
                     len(text), INFLIGHT_MAX_CHARS)
        text = text[:INFLIGHT_MAX_CHARS]

    now = time.monotonic()
    if not force:
        if text == _inflight_last["text"]:
            return True
        if (now - _inflight_last["at"]) < INFLIGHT_MIN_INTERVAL:
            return True

    payload = {
        "format": SESSION_FORMAT,
        "version": SESSION_VERSION,
        "saved_at": time.time(),
        "content": text,
    }
    if not _write_json_atomic(inflight_path(), payload):
        return False
    _inflight_last["at"] = now
    _inflight_last["text"] = text
    return True


def clear_inflight() -> None:
    """删除流式半条文件并复位节流状态（一轮结束、或用户清空对话时调用）。"""
    _inflight_last["at"] = 0.0
    _inflight_last["text"] = ""
    try:
        os.remove(inflight_path())
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Failed to remove inflight snapshot: %s", e)


def clear_session() -> None:
    """删除整份会话快照（用户明确清空对话的唯一落点）。"""
    _inflight_last["at"] = 0.0
    _inflight_last["text"] = ""
    for path in (autosave_path(), inflight_path()):
        try:
            os.remove(path)
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.warning("Failed to remove session snapshot '%s': %s", path, e)
            continue
        logger.info("Removed chat session snapshot: %s", path)


# --------------------------------------------------------------------------- #
#  读
# --------------------------------------------------------------------------- #
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


def load_session() -> List[Dict[str, Any]]:
    """读取上次会话（含未完成的半条）；无可用快照时返回空列表。"""
    messages = _messages_of(_read_json(autosave_path()))
    if not messages:
        # 没有已完成轮次时，孤立的半条回答没有上下文可言，不恢复
        return []

    inflight = _read_json(inflight_path())
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
    """注销回调（面板被销毁时调用，避免残留已失效的绑定方法）。"""
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
