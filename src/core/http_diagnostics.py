"""HTTP 探针失败的归因口径。

设置页的 Fetch / Test 只做一件事：拿一个端点探一下，看 Key 与模型是否可用。失败时
``httpx`` 抛出的文本是面向开发者的（``Client error '401 Authorization Required' for
url ...`` 外加一行 MDN 文档链接）：既没说清是哪一环出的问题，也没有下一步能做什么。

本模块只回答"这次失败属于哪一类、服务端到底说了什么"，**不产出用户可见文案**——
文案在 UI 侧由 :mod:`src.core.locales` 提供，于是弹窗标题与正文共用同一份归因，
两个入口不会各自解释同一个 401。任务层可能跑在子进程里，也不该去碰翻译表。

分类刻意只覆盖"有明确动作可给"的三种：401（Key 不被接受）、403（Key 没有该权限）、
502（服务商网关够不到上游）。其余一律 ``unknown`` 并保留原始异常文本——认不出来的
失败不要假装有结论。
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

logger = logging.getLogger("http_diagnostics")

#: 归因码。与 UI 侧的标题 / 建议表按键对应。
KIND_UNAUTHORIZED = "unauthorized"
KIND_FORBIDDEN = "forbidden"
KIND_BAD_GATEWAY = "bad_gateway"
KIND_UNKNOWN = "unknown"

#: 只登记有明确处理动作的状态码。
_STATUS_KIND = {
    401: KIND_UNAUTHORIZED,
    403: KIND_FORBIDDEN,
    502: KIND_BAD_GATEWAY,
}

#: 服务端响应体的截取上限。整页 HTML 错误页对排障没有价值，只会把弹窗撑爆。
DETAIL_LIMIT = 400

#: 异常文本里对用户无信息量的行（httpx 固定附带）。
_NOISE_PREFIXES = ("For more information check",)


def classify_failure(exc: Exception, endpoint: str = "") -> Dict[str, object]:
    """把一次 HTTP 探针失败归成 ``{kind, status, endpoint, detail}``。

    ``status`` 为 0 表示连接层就没走通（DNS / TLS / 代理不可达），此时 ``kind`` 必然
    是 ``unknown``：连状态码都没有，说得更细就是编造。

    :param exc: ``httpx`` 或任何带 ``response`` / ``status_code`` 的异常。
    :param endpoint: 本次请求的完整 URL，供用户核对打到了哪个端点。
    """
    status = _status_of(exc)
    kind = _STATUS_KIND.get(status, KIND_UNKNOWN)
    failure = {
        "kind": kind,
        "status": status,
        "endpoint": str(endpoint or ""),
        "detail": _detail_text(exc),
    }
    logger.debug("HTTP probe failure classified: kind=%s status=%s endpoint=%s",
                 kind, status, endpoint)
    return failure


def _status_of(exc: Exception) -> int:
    """取 HTTP 状态码：优先 ``response.status_code``，其次异常自身的 ``status_code``。

    不按异常类名判定：``httpx.HTTPStatusError`` 之外还有 ``litellm`` 与自建包装，
    类层次随版本变化，而状态码是稳定契约。
    """
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            status = int(getattr(response, "status_code", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        if status > 0:
            return status
    candidate = getattr(exc, "status_code", None)
    if isinstance(candidate, int) and 0 < candidate < 1000:
        return candidate
    return 0


def _detail_text(exc: Exception) -> str:
    """服务端响应体优先，其次异常自身的描述——两者都保留原文，不做转述。"""
    body = _response_body(exc)
    if body:
        return body[:DETAIL_LIMIT]
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith(_NOISE_PREFIXES)]
    if not lines:
        return type(exc).__name__
    return " ".join(lines)[:DETAIL_LIMIT]


def _response_body(exc: Exception) -> str:
    """读服务端响应体。读取失败只降级为空串：诊断不该自己抛出异常。"""
    response: Optional[object] = getattr(exc, "response", None)
    if response is None:
        return ""
    try:
        return (getattr(response, "text", "") or "").strip()
    except Exception as e:  # pragma: no cover - 防御：流未关闭 / 编码异常
        logger.debug("Could not read provider response body: %s", e)
        return ""
