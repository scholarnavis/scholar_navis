"""全文获取的"同轮等待"通道（对话任务 ↔ UI）。

为什么需要它
------------
模型在调用 ``request_fulltext`` 之后唯一想要的是"那篇论文的全文"。如果把它做成
"结束本轮 + 用户登录后新起一轮"，模型就丢掉了这一轮的工作记忆（计划、已检索到的
结论、已引用过的来源），续跑只能靠重述重建——信息量必然下降。

因此本模块提供一次**同轮会合**：任务线程在工具调用里挂起等待，UI 在 PDF 入库后
把结果交回来，模型带着"全文已就绪"的工具结果继续跑完这一轮。上下文、引用注册表、
思考过程全部沿用，不存在"新开一轮"的断层。

两条触发路径共用这一通道：模型显式调用 ``request_fulltext``；以及
``fetch_paper_fulltext`` 报告取不到正文时运行时**自动升级**（见
``AgentRuntime._escalate_fulltext``，弹窗在进程内串行化、同一篇每轮只问一次）。

实现口径
--------
* 每个对话最多一个等待点（同一轮不可能并发请求两篇全文）；
* 结果槽一旦写入，等待方取走即清空，避免上一轮的 PDF 被下一轮误领；
* 等待方以 0.25 秒粒度轮询，因此"用户点停止"能立即退出，不必等超时；
* 纯内存结构：进程退出即失效，不留任何持久化状态。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("Core.FulltextBridge")

#: 默认最长等待（秒）。用户下载 + 选择文件 + PDF 解析都在里面，给足余量但不无限等。
DEFAULT_TIMEOUT = 900.0
_POLL = 0.25

_lock = threading.Lock()
_waiters: Dict[str, Dict[str, Any]] = {}


def begin(conversation_id: str) -> Optional[Dict[str, Any]]:
    """为一个对话登记等待点；返回句柄（重复登记会覆盖旧的）。"""
    cid = str(conversation_id or "")
    if not cid:
        return None
    handle = {"event": threading.Event(), "result": None, "started": time.time()}
    with _lock:
        previous = _waiters.get(cid)
        if previous is not None and not previous["event"].is_set():
            # 上一轮还挂着（用户中途取消后没清理干净）：让它先退出，避免两个
            # 等待点抢同一份 PDF。
            previous["event"].set()
        _waiters[cid] = handle
    return handle


def release(conversation_id: str) -> None:
    """注销等待点（等待方退出时调用，幂等）。"""
    with _lock:
        _waiters.pop(str(conversation_id or ""), None)


def signal(conversation_id: str, ok: bool, path: str = "", message: str = "") -> bool:
    """把结果交给等待方；没有等待点时返回 ``False``（例如手动上传的 PDF）。"""
    cid = str(conversation_id or "")
    with _lock:
        handle = _waiters.get(cid)
        if handle is None:
            return False
        handle["result"] = (bool(ok), str(path or ""), str(message or ""))
        handle["event"].set()
    return True


def wait(conversation_id: str, handle: Optional[Dict[str, Any]],
         timeout: float = DEFAULT_TIMEOUT,
         cancel_check: Optional[Callable[[], bool]] = None
         ) -> Tuple[bool, str, str]:
    """阻塞等待结果，返回 ``(ok, path, message)``。

    超时 / 用户取消 / 等待点被替换时返回 ``(False, "", 原因)``：调用方据此把
    "没等到"如实告诉模型，让它用现有证据作答，而不是继续空转。
    """
    if handle is None:
        return False, "", "no wait handle"
    event: threading.Event = handle["event"]
    deadline = time.time() + max(1.0, float(timeout))
    while True:
        if event.wait(_POLL):
            break
        if cancel_check is not None:
            try:
                if cancel_check():
                    release(conversation_id)
                    return False, "", "cancelled by the user"
            except Exception:
                pass
        if time.time() > deadline:
            release(conversation_id)
            return False, "", f"timed out after {float(timeout):g}s"
        with _lock:
            if _waiters.get(str(conversation_id or "")) is not handle:
                return False, "", "wait superseded by another request"
    result = handle.get("result")
    release(conversation_id)
    if not isinstance(result, tuple) or len(result) != 3:
        return False, "", "no result"
    return result  # type: ignore[return-value]


def pending(conversation_id: str) -> bool:
    """该对话当前是否有人在等全文（供 UI 判断"这次下载要不要交回去"）。"""
    with _lock:
        return str(conversation_id or "") in _waiters


__all__ = ["begin", "release", "signal", "wait", "pending", "DEFAULT_TIMEOUT"]
