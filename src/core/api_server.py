import time
import json
import uuid
import logging
import queue
import threading
import re
from typing import List, Dict, Optional, Any
from fastapi import FastAPI, HTTPException, Request, Depends, Security, Body
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

from src.core.config_manager import ConfigManager
from src.core.device_manager import DeviceManager
from src.core.kb_manager import KBManager
from src.core.mcp_manager import MCPManager
from src.core.models_registry import check_model_exists, get_model_conf, resolve_auto_model
from src.core.network_worker import setup_global_network_env
from src.tools.chat_tool import ChatWorker

app = FastAPI(title="Scholar Navis Agentic API", version="1.0.0")
logger = logging.getLogger("API_Server")
security = HTTPBearer()


class OpenAIException(Exception):
    """Custom exception class to generate standard OpenAI error responses."""
    def __init__(self, message: str, status_code: int = 400, error_type: str = "invalid_request_error"):
        self.message = message
        self.status_code = status_code
        self.error_type = error_type


@app.exception_handler(OpenAIException)
async def openai_exception_handler(request: Request, exc: OpenAIException):
    """Intercepts OpenAIException and formats it to strict OpenAI API JSON standards."""
    logger.error(f"API Exception Triggered -> Type: {exc.error_type} | Message: {exc.message}")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.message,
                "type": exc.error_type,
                "param": None,
                "code": exc.status_code
            }
        }
    )


def verify_api_key(credentials: HTTPAuthorizationCredentials = Security(security)):
    config = ConfigManager()
    expected_key = config.user_settings.get("api_server_key", "")
    if expected_key and credentials.credentials != expected_key:
        raise OpenAIException("Invalid API Key. Please check your settings in Scholar Navis.", status_code=401, error_type="authentication_error")
    return credentials.credentials



class ChoiceMessage(BaseModel):
    role: str
    content: str
    reasoning_content: Optional[str] = None
    follow_ups: Optional[List[str]] = None
    cited_sources: Optional[List[str]] = None

class Choice(BaseModel):
    index: int
    message: ChoiceMessage
    finish_reason: Optional[str] = None

class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[Choice]




# ==========================================
# Pydantic Models (Chat & Completions)
# ==========================================
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "default"
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = None

    # 自定义底层干预参数
    kb_id: Optional[str] = "none"
    use_mcp: Optional[bool] = True
    mcp_tags: Optional[List[str]] = None
    force_translate: Optional[bool] = False


class CompletionRequest(BaseModel):
    model: str = "default"
    prompt: str
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    kb_id: Optional[str] = "none"
    use_mcp: Optional[bool] = True


# ==========================================
# Worker 执行与包装逻辑
# ==========================================
def _run_worker_through_queue(request_data, messages_dict):
    config = ConfigManager()
    setup_global_network_env()

    llm_id = config.user_settings.get("chat_llm_id")
    llm_configs = config.load_llm_configs()
    main_config = next((c for c in llm_configs if c.get("id") == llm_id), None)

    trans_llm_id = config.user_settings.get("chat_trans_llm_id")
    trans_config = next((c for c in llm_configs if c.get("id") == trans_llm_id), None)

    if not main_config:
        raise HTTPException(status_code=500, detail="No active Main Model configured in UI.")

    api_queue = queue.Queue()

    # 实例化无头的 ChatWorker
    worker = ChatWorker(
        main_config=main_config,
        trans_config=trans_config,
        messages=messages_dict,
        kb_id=request_data.kb_id,
        requires_translation=request_data.force_translate if hasattr(request_data, 'force_translate') else False,
        external_context=None,
        use_mcp=request_data.use_mcp,
        api_queue=api_queue
    )
    if hasattr(request_data, 'mcp_tags'):
        worker.selected_mcp_tags = request_data.mcp_tags

    # 脱离 QThread，使用标准 Python 线程池执行
    thread = threading.Thread(target=worker.run)
    thread.start()

    return api_queue


def _clean_ui_token(raw_token: str) -> str:
    """剥离专门给 UI 看的 HTML 装饰，并将其逆向转换为终端友好的纯净 Markdown"""
    if raw_token in ["[CLEAR_SEARCH]", "[START_LLM_NETWORK]"]:
        return ""

    clean = raw_token
    # 1. 过滤掉顶部通知、MCP 执行状态和搜索进度的 UI 框
    clean = re.sub(r'<mcp_process.*?>.*?</mcp_process>\n*', '', clean, flags=re.IGNORECASE | re.DOTALL)
    clean = re.sub(r'<i.*?>.*?</i>\n*', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'<div.*?class=[\'"]header-.*?</div>\n*', '', clean, flags=re.IGNORECASE | re.DOTALL)

    # 2. 将引用的内部 cite:// 协议，优雅地还原为标准的纯文本脚注，如 [1], [2]
    # UI 的格式一般是: <a href='cite://...'><b>[1]</b> Source Name</a>
    clean = re.sub(r"<a[^>]*href=['\"]cite://[^>]*>.*?<b>\[(\d+)\]</b>.*?</a>", r"[\1]", clean, flags=re.IGNORECASE)

    # 3. 将常见的 HTML 样式还原为终端支持的 Markdown 标记
    clean = re.sub(r'<br\s*/?>', '\n', clean, flags=re.IGNORECASE)
    clean = re.sub(r'<hr.*?>', '\n---\n', clean, flags=re.IGNORECASE)
    clean = re.sub(r'<b>(.*?)</b>', r'**\1**', clean, flags=re.IGNORECASE)

    # 4. 兜底清理其他所有残留的非封闭 HTML div/span (如彩色警告块)
    clean = re.sub(r'<div.*?>', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'</div>', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'<span.*?>', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'</span>', '', clean, flags=re.IGNORECASE)

    return clean


class APIStreamParser:
    """State machine to route tokens to reasoning, content, sources, or follow-ups"""

    def __init__(self):
        self.is_thinking = False
        self.current_section = "content"  # States: content, sources, follow_ups
        self.follow_up_buffer = ""
        self.cited_sources_buffer = ""

    def parse_token(self, raw_token: str):
        """Returns (reasoning_chunk, content_chunk)"""
        token = _clean_ui_token(raw_token)
        if not token:
            return None, None

        # Check for section headers and switch state
        if "📚 Cited Sources" in token or "[CITED_SOURCES]" in token:
            self.current_section = "sources"
            token = re.sub(r'(?i)(📚\s*Cited Sources|\[CITED_SOURCES\]|Cited Sources:?)', '', token)

        if "💡 Suggested Follow-ups" in token or "[FOLLOW_UPS]" in token:
            self.current_section = "follow_ups"
            token = re.sub(r'(?i)(💡\s*Suggested Follow-ups|\[FOLLOW_UPS\]|Suggested Follow-ups:?)', '', token)

        # Route text to the appropriate buffer
        if self.current_section == "sources":
            self.cited_sources_buffer += token
            return None, None
        elif self.current_section == "follow_ups":
            self.follow_up_buffer += token
            return None, None

        # Handle reasoning state transitions
        reasoning_chunk = ""
        content_chunk = ""

        if "<think>" in token:
            parts = token.split("<think>")
            content_chunk += parts[0]
            self.is_thinking = True
            token = parts[1] if len(parts) > 1 else ""

        if "</think>" in token:
            parts = token.split("</think>")
            reasoning_chunk += parts[0]
            self.is_thinking = False
            token = parts[1] if len(parts) > 1 else ""
            content_chunk += token
            return reasoning_chunk, content_chunk

        if self.is_thinking:
            reasoning_chunk += token
        else:
            content_chunk += token

        return reasoning_chunk, content_chunk

    def extract_list_items(self, buffer: str) -> List[str]:
        """Generic method to parse markdown lists into JSON arrays"""
        items = []
        for line in buffer.split('\n'):
            line = line.strip()
            line = re.sub(r'^>\s*', '', line)  # Remove blockquotes
            if re.match(r'^([-*]|\d+\.)', line):
                item = re.sub(r'^([-*\s]+|\d+\.\s*)', '', line).strip()
                item = item.replace('**', '').strip()
                if len(item) > 2:
                    items.append(item)
        return items

    def extract_cited_sources(self) -> List[str]:
        return self.extract_list_items(self.cited_sources_buffer)

    def extract_follow_ups(self) -> List[str]:
        return self.extract_list_items(self.follow_up_buffer)


def _validate_prerequisites(request: ChatCompletionRequest, config: ConfigManager):
    """Validates all required models and configurations before initiating the pipeline."""

    # 1. Validate Main LLM Configuration
    llm_id = config.user_settings.get("chat_llm_id")
    llm_configs = config.load_llm_configs()
    main_config = next((c for c in llm_configs if c.get("id") == llm_id), None)

    if not main_config:
        raise OpenAIException("No active Main Model configured. Please set an LLM in the Settings UI.", status_code=400,
                              error_type="invalid_request_error")

    # 2. Validate RAG and Hardware Models if KB is requested
    if request.kb_id and request.kb_id != "none":
        kb_info = KBManager().get_kb_by_id(request.kb_id)
        if not kb_info:
            raise OpenAIException(f"Knowledge Base '{request.kb_id}' not found or deleted.", status_code=404,
                                  error_type="invalid_request_error")

        user_device = config.user_settings.get("inference_device", "Auto")
        target_device = DeviceManager().parse_device_string(user_device)

        # Check Embedding Model Capability
        model_id = kb_info.get('model_id', 'embed_auto')
        conf = get_model_conf(model_id, "embedding")
        if not conf or conf.get('is_auto'):
            model_id = resolve_auto_model("embedding", target_device)

        if not check_model_exists(model_id, "embedding"):
            raise OpenAIException(
                f"Required Embedding model '{model_id}' for this KB is missing. Please download it via Settings.",
                status_code=500,
                error_type="model_missing_error"
            )

        # Check Reranker Model Capability
        rerank_id = config.user_settings.get("rerank_model_id", "rerank_auto")
        conf_rerank = get_model_conf(rerank_id, "reranking")
        if not conf_rerank or conf_rerank.get('is_auto'):
            rerank_id = resolve_auto_model("reranking", target_device)

        if not check_model_exists(rerank_id, "reranking"):
            raise OpenAIException(
                f"Required Reranker model '{rerank_id}' is missing. Hardware inference requires this model. Please download it via Settings.",
                status_code=500,
                error_type="model_missing_error"
            )


# ==========================================
# Endpoints
# ==========================================
@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
def chat_completions(request: ChatCompletionRequest):
    config = ConfigManager()

    _validate_prerequisites(request, config)

    messages_dict = [msg.model_dump() for msg in request.messages]

    query_preview = request.messages[-1].content.replace('\n', ' ')[:50]
    logger.info(f"API Request Received -> Model: {request.model} | Query: '{query_preview}...'")

    try:
        api_queue = _run_worker_through_queue(request, messages_dict)
    except Exception as e:
        raise OpenAIException(f"Internal Worker Initialization Error: {str(e)}", status_code=500,
                              error_type="internal_server_error")

    response_id = f"chatcmpl-{uuid.uuid4().hex}"
    created_time = int(time.time())

    if request.stream:
        def event_generator():
            parser = APIStreamParser()
            while True:
                msg = api_queue.get(timeout=300)
                msg_type = msg.get("type")

                if msg_type == "finished":
                    follow_ups = parser.extract_follow_ups()
                    cited_sources = parser.extract_cited_sources()

                    delta_update = {}
                    if follow_ups: delta_update["follow_ups"] = follow_ups
                    if cited_sources: delta_update["cited_sources"] = cited_sources

                    if delta_update:
                        chunk = {
                            "id": response_id, "object": "chat.completion.chunk", "created": created_time,
                            "model": request.model,
                            "choices": [{"index": 0, "delta": delta_update, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

                    yield f"data: [DONE]\n\n"
                    logger.info(
                        f"API Request Fulfilled -> Stream complete. Parsed {len(cited_sources)} sources, {len(follow_ups)} follow-ups.")
                    break

                elif msg_type == "error":
                    error_data = msg.get("data", "Unknown Runtime Error")
                    error_payload = {
                        "error": {
                            "message": error_data,
                            "type": "server_error",
                            "param": None,
                            "code": 500
                        }
                    }
                    # 对于严重流中断，通常客户端也会解析非标准 chunk 的 JSON 错误对象
                    yield f"data: {json.dumps(error_payload)}\n\n"
                    yield f"data: [DONE]\n\n"
                    logger.error(f"API Request Failed Mid-stream -> {error_data}")
                    break

                elif msg_type == "token":
                    # 2. Parse token dynamically into thinking or content
                    r_chunk, c_chunk = parser.parse_token(msg.get("data", ""))

                    delta = {}
                    if r_chunk: delta["reasoning_content"] = r_chunk
                    if c_chunk: delta["content"] = c_chunk

                    if delta:
                        chunk = {
                            "id": response_id, "object": "chat.completion.chunk", "created": created_time,
                            "model": request.model,
                            "choices": [{"index": 0, "delta": delta, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")


    else:

        parser = APIStreamParser()
        full_content = ""
        full_reasoning = ""

        while True:
            msg = api_queue.get(timeout=300)
            if msg.get("type") == "finished":
                break

            elif msg.get("type") == "error":
                raise OpenAIException(msg.get("data"), status_code=500, error_type="server_error")

            elif msg.get("type") == "token":
                r_chunk, c_chunk = parser.parse_token(msg.get("data", ""))
                if r_chunk: full_reasoning += r_chunk
                if c_chunk: full_content += c_chunk

        follow_ups = parser.extract_follow_ups()
        cited_sources = parser.extract_cited_sources()

        logger.info(f"API Sync Request Fulfilled -> {len(cited_sources)} sources, {len(follow_ups)} follow-ups.")

        return {
            "id": response_id, "object": "chat.completion", "created": created_time, "model": request.model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": full_content,
                    "reasoning_content": full_reasoning if full_reasoning else None,
                    "cited_sources": cited_sources if cited_sources else None,
                    "follow_ups": follow_ups if follow_ups else None
                },
                "finish_reason": "stop"
            }]
        }


@app.post("/v1/completions", dependencies=[Depends(verify_api_key)])
def legacy_completions(request: CompletionRequest):
    """支持旧版 completion API 端点"""
    messages_dict = [{"role": "user", "content": request.prompt}]
    api_queue = _run_worker_through_queue(request, messages_dict)

    response_id = f"cmpl-{uuid.uuid4().hex}"
    created_time = int(time.time())

    if request.stream:
        def event_generator():
            while True:
                msg = api_queue.get(timeout=300)
                msg_type = msg.get("type")

                if msg_type == "finished":
                    yield f"data: [DONE]\n\n"
                    break
                elif msg_type == "error":
                    chunk = {"id": response_id, "object": "text_completion", "created": created_time,
                             "model": request.model,
                             "choices": [{"text": f"\n[API Error: {msg.get('data')}]", "finish_reason": "error"}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                    yield f"data: [DONE]\n\n"
                    break
                elif msg_type == "token":
                    clean_token = _clean_ui_token(msg.get("data", ""))
                    if clean_token:
                        chunk = {"id": response_id, "object": "text_completion", "created": created_time,
                                 "model": request.model, "choices": [{"text": clean_token, "finish_reason": None}]}
                        yield f"data: {json.dumps(chunk)}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")
    else:
        full_content = ""
        while True:
            msg = api_queue.get(timeout=300)
            if msg.get("type") == "finished":
                break
            elif msg.get("type") == "error":
                full_content += f"\n[API Error: {msg.get('data')}]"
                break
            elif msg.get("type") == "token":
                full_content += _clean_ui_token(msg.get("data", ""))

        return {
            "id": response_id, "object": "text_completion", "created": created_time, "model": request.model,
            "choices": [{"text": full_content, "finish_reason": "stop"}]
        }


# ====== Modified Section: API Endpoints (Append before Thread Launcher) ======

@app.get("/api/state")
def get_system_state():
    """Endpoint for scripts to read current UI state and capabilities"""
    return {
        "active_device": ConfigManager().user_settings.get("inference_device", "auto"),
        "mcp_tools": len(MCPManager.get_instance().get_all_tools_schema()),
        "kbs": len(KBManager().get_all_kbs())
    }


# --- New Configuration and State Endpoints ---

@app.post("/v1/models")
def list_models(payload: dict = Body(...)):
    """
    Retrieves the list of available models for a specified provider,
    compliant with the OpenAI API specification.
    """
    provider_id = payload.get("provider")
    config = ConfigManager()
    llm_configs = config.load_llm_configs()

    # Locate the configuration corresponding to the requested provider
    target_conf = next((c for c in llm_configs if c.get("id") == provider_id), {})

    models = target_conf.get("fetched_models", [])
    if target_conf.get("model_name") and target_conf.get("model_name") not in models:
        models.insert(0, target_conf.get("model_name"))

    data = []
    for m in models:
        data.append({
            "id": m,
            "object": "model",
            "created": int(time.time()),
            "owned_by": target_conf.get("name", "custom")
        })

    return {"object": "list", "data": data}


@app.get("/api/providers")
def list_all_providers():
    """
    Retrieves a comprehensive inventory of all configured LLM providers
    and their associated model arrays.
    """
    config = ConfigManager()
    configs = config.load_llm_configs()
    active_id = config.user_settings.get("active_llm_id", "openai")

    result = []
    for c in configs:
        result.append({
            "id": c.get("id"),
            "name": c.get("name"),
            "is_active": c.get("id") == active_id,
            "models": c.get("fetched_models", []),
            "current_model": c.get("model_name", "")
        })
    return {"providers": result}


@app.get("/api/mcp/tools")
def list_mcp_tools():
    """
    Exposes the available Model Context Protocol (MCP) tools schema
    and dynamic operational tags for client-side tool routing and filtering.
    """
    mcp_mgr = MCPManager.get_instance()
    # Ensure servers are bootstrapped to reflect accurate schema states
    mcp_mgr.bootstrap_servers(force_all=False)

    tools = mcp_mgr.get_all_tools_schema()

    # Extract unique server tags assuming 'server_name' acts as the primary tag
    tags = list(set([mcp_mgr.tool_map.get(t.get("function", {}).get("name")) for t in tools if
                     mcp_mgr.tool_map.get(t.get("function", {}).get("name"))]))

    return {
        "available_tags": tags,
        "tools_count": len(tools),
        "tools": tools
    }


@app.get("/api/kbs")
def list_knowledge_bases():
    """
    Retrieves the indexed vector databases (Knowledge Bases) currently
    available for Retrieval-Augmented Generation (RAG).
    """
    kb_manager = KBManager()
    kbs = kb_manager.get_all_kbs()

    result = []
    for kb in kbs:
        if kb.get('status') == 'ready':
            result.append({
                "id": kb.get("id"),
                "name": kb.get("name"),
                "domain": kb.get("domain", "General Academic"),
                "doc_count": kb.get("doc_count", 0)
            })
    return {"knowledge_bases": result}


# ==========================================
# Thread Launcher
# ==========================================
def run_server():
    port = ConfigManager().user_settings.get("api_server_port", 8000)
    logger.info(f"Starting Standalone API Server on 0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
