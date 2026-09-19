"""LLM 错误统一归一化与错误面板标记协议。

职责（高内聚，core 层零 UI 依赖，可安全用于子进程）：

1. 归一化：把不同来源的错误（litellm 异常标题、任务端业务检测、
   Agent 运行时捕获的流式错误）统一为 ``{"title", "body", "details"}``：

   - ``title``   面向用户的一行摘要（统一命名风格，见 ``_TITLE_ALIASES``）；
   - ``body``    面向用户的友好说明与可操作建议；
   - ``details`` 程序运行中的真实错误信息（程序自身异常 + LLM 原始返回），
                 由 UI 错误面板的折叠栏展示，并随主流程写入日志文件。

2. 标记协议：与 ``<rplot_card>`` / ``<plot_plan>`` 相同的 base64 内联
   标记（``<error_panel data="..."></error_panel>``）。core/task 层产出
   标记字符串；UI 层（``chat_bubble._extract_error_panels``）解码渲染
   为统一错误面板组件，保证全部报错美术样式一致。
"""
import base64
import json
import re

# ------------------------------------------------------------------ #
#  标题规范化：把底层零散的错误标题统一为一致的用户可读风格
# ------------------------------------------------------------------ #
_TITLE_ALIASES = {
    "api request error: http 401": "Authentication Failed (HTTP 401)",
    "api request error: http 403": "Permission Denied (HTTP 403)",
    "api request error: http 404": "Model Not Found (HTTP 404)",
    "api request error: http 408": "Request Timeout (HTTP 408)",
    "api request error: http 429": "Rate Limit (HTTP 429)",
    "api request error: http 500": "Provider Server Error (HTTP 500)",
    "api request error: http 502": "Provider Server Error (HTTP 502)",
    "api request error: http 503": "Service Unavailable (HTTP 503)",
    "system error: connection failed": "Connection Failed",
    "context exceeded error": "Context Window Exceeded",
    "rate limit error": "Rate Limit Exceeded",
    "timeout error": "Request Timeout",
    "system error": "System Error",
    "provider error": "Provider Error",
}

# 兜底匹配任意 "API Request Error: HTTP xxx"
_HTTP_TITLE_RE = re.compile(r"^api request error:\s*http\s*(\d+)$")

# body 缺失时按标题补充的通用建议
_TITLE_TIPS = {
    "Authentication Failed (HTTP 401)": "Please check the API Key in Global Settings.",
    "Model Not Found (HTTP 404)": "The model name or API endpoint is wrong. Verify the model ID in Global Settings.",
    "Rate Limit (HTTP 429)": "Too many requests or insufficient quota. Wait a moment and retry.",
    "Service Unavailable (HTTP 503)": "The provider is overloaded or down. Retry later or switch models.",
    "Connection Failed": "Check your network, proxy settings and API endpoint URL.",
    "Request Timeout": "The provider did not respond in time. Check the network or retry.",
    "Context Window Exceeded": "The input is too long. Clear history or use a larger-context model.",
}

# "模型拒绝图片输入"类 400 错误的统一友好文案
_IMAGE_REJECT_BODY = (
    "The model provider rejected the request because the selected model does not "
    "support image input.\n\n"
    "How to fix (choose one):\n"
    "1. Switch the Main Model to a multimodal (vision-capable) model;\n"
    "2. Or configure a Vision model in the model selector — it converts images "
    "into text descriptions for the main model;\n"
    "3. Or remove the image attachments and retry with plain text.\n\n"
    "Provider details: {provider}"
)

#: 400 错误中出现以下关键字时，判定为"模型不接受图片输入"
_IMAGE_HINTS = ('image', 'multimodal', 'media', 'image_url', 'content policy', 'modalit')

#: 图片类 400 错误中出现以下关键字时，进一步细分为"图片格式不被接受"
#: （模型可能支持图片，只是不收该格式，如部分服务商拒绝 webp/bmp）
_FORMAT_HINTS = ('format', 'file type', 'filetype', 'extension', 'not a valid', 'media type')

#: "图片格式不被接受"的统一友好文案
_IMAGE_FORMAT_BODY = (
    "The provider rejected the image file format. The model may still support "
    "images in general, just not this format.\n\n"
    "How to fix (choose one):\n"
    "1. Convert the image to PNG or JPEG and re-attach;\n"
    "2. Or re-export / re-screenshot the chart as PNG, then retry;\n"
    "3. Or remove the image attachments and send the question as plain text.\n\n"
    "Provider details: {provider}"
)


def normalize_title(title: str) -> str:
    """把底层错误标题规范化为统一命名风格。未知标题原样透传。"""
    t = (title or "").strip()
    if not t:
        return "Generation Error"
    key = t.lower()
    if key in _TITLE_ALIASES:
        return _TITLE_ALIASES[key]
    m = _HTTP_TITLE_RE.match(key)
    if m:
        code = m.group(1)
        family = "Provider Server Error" if code.startswith("5") else "Provider Error"
        return f"{family} (HTTP {code})"
    return t


def friendly_payload(title: str, body: str, details: str = "") -> dict:
    """归一化错误 payload。

    :param title:   底层错误标题（如 ``API Request Error: HTTP 400``）。
    :param body:    底层错误描述 / 友好建议。
    :param details: 真实错误信息（异常文本、Provider 原始返回等）。
                    缺省时回退为 ``title\\nbody``，保证折叠栏始终可追溯。
    """
    raw_title = (title or "").strip()
    raw_body = (body or "").strip()
    combined = f"{raw_title}\n{raw_body}".lower()
    out_title = normalize_title(raw_title)

    # 图片类 400 错误 → 统一映射为可操作的友好提示，并按原因细分：
    # 格式问题（如 webp 不被接受）与整体不支持图片分开反馈，避免误导。
    if ('400' in raw_title or 'bad request' in combined) and any(h in combined for h in _IMAGE_HINTS):
        if any(h in combined for h in _FORMAT_HINTS):
            out_title = "Image Format Not Accepted"
            raw_body = _IMAGE_FORMAT_BODY.format(provider=raw_body[:400])
        else:
            out_title = "Model Cannot Read Images"
            raw_body = _IMAGE_REJECT_BODY.format(provider=raw_body[:400])

    # details 兜底必须基于"原始" title/body（真实错误信息），
    # 而不是下方补建议后的文案，保证折叠栏内容可追溯到源头。
    out_details = (details or "").strip() or f"{raw_title}\n{raw_body}".strip()

    if not raw_body:
        raw_body = _TITLE_TIPS.get(
            out_title,
            "The request could not be completed. See Technical Details for the raw error.")

    return {"title": out_title, "body": raw_body, "details": out_details}


# ------------------------------------------------------------------ #
#  <error_panel> 标记协议（base64 JSON）
# ------------------------------------------------------------------ #
_MARKER_RE = re.compile(r'<error_panel data="[^"]*"\s*>\s*</error_panel>', re.DOTALL)
_OPEN_TAG_RE = re.compile(r'<error_panel[^>]*/?>')


def error_marker(payload: dict) -> str:
    """把错误 payload 编码为内联标记字符串。"""
    data = json.dumps(payload, ensure_ascii=False)
    b64 = base64.b64encode(data.encode("utf-8")).decode("ascii")
    return f'<error_panel data="{b64}"></error_panel>'


def error_marker_from(title: str, body: str, details: str = "") -> str:
    """归一化并直接生成内联标记（一步到位）。"""
    return error_marker(friendly_payload(title, body, details))


def strip_markers(text: str) -> str:
    """从文本中剥离错误面板标记（导出复制 / 历史清洗时使用）。"""
    if not text or "<error_panel" not in text:
        return text
    text = _MARKER_RE.sub("", text)
    # 清理流式渲染中可能残留的不完整标记
    text = _OPEN_TAG_RE.sub("", text)
    return text
