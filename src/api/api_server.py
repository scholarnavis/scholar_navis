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
import multiprocessing as mp
import queue as q
from src.core.config_manager import ConfigManager
from src.core.core_task import TaskState, RunnerProcess
from src.core.device_manager import DeviceManager
from src.core.kb_manager import KBManager
from src.core.lang_detect import detect_primary_language
from src.core.mcp_manager import MCPManager
from src.core.models_registry import check_model_exists, get_model_conf, resolve_auto_model
from src.core.network_worker import setup_global_network_env
from src.task.chat_tasks import ChatGenerationTask
from src.task.kb_tasks import RerankTask

app = FastAPI(title="Scholar Navis Agentic API", version="1.0.0")
logger = logging.getLogger("API_Server")
security = HTTPBearer(auto_error=False)


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


def verify_api_key(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    """
    Evaluates the incoming Bearer token against the persistent configuration.
    If no key is designated in the configuration, access is universally granted.
    """
    config = ConfigManager()
    expected_key = config.user_settings.get("api_server_key", "").strip()

    if expected_key:
        if not credentials or credentials.credentials != expected_key:
            raise OpenAIException(
                message="Invalid or missing API Key. Please provide a valid Bearer token.",
                status_code=401,
                error_type="authentication_error"
            )
        return credentials.credentials
    return None


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

class MCPFilterRequest(BaseModel):
    query: str
    history_context: Optional[str] = ""
    top_k: Optional[int] = 8
    mcp_tags: Optional[List[str]] = None

class ChatCompletionRequest(BaseModel):
    model: str = "default"
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = None

    provider_id: Optional[str] = None         # 主模型服务商ID
    trans_provider_id: Optional[str] = None   # 翻译模型服务商ID
    trans_model: Optional[str] = None         # 翻译模型名称

    kb_id: Optional[str] = "none"
    use_mcp: Optional[bool] = True
    mcp_tags: Optional[List[str]] = None      # 过滤 MCP 工具的 Tag 列表
    force_translate: Optional[bool] = None    # 强制翻译开关 (None时按GUI逻辑自动检测)


class CompletionRequest(BaseModel):
    model: str = "default"
    prompt: str
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    kb_id: Optional[str] = "none"
    use_mcp: Optional[bool] = True


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

    # 1. Validate Main LLM Configuration (修改为支持 API 传入的 provider_id)
    provider_id = request.provider_id or config.user_settings.get("chat_llm_id")
    llm_configs = config.load_llm_configs()
    main_config = next((c for c in llm_configs if c.get("id") == provider_id), None)

    if not main_config:
        raise OpenAIException(f"Main Model Provider '{provider_id}' not found.", status_code=400,
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

        if not check_model_exists(model_id):
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

        if not check_model_exists(rerank_id):
            raise OpenAIException(
                f"Required Reranker model '{rerank_id}' is missing. Hardware inference requires this model. Please download it via Settings.",
                status_code=500,
                error_type="model_missing_error"
            )


# ==========================================
# Endpoints
# ==========================================
@app.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest):
    """
    OpenAI-compatible chat endpoint meticulously synchronized with ChatTool's internal logic.
    It replicates the exact parameter construction, model verification, and task dispatching
    found in `ChatTool.process_send` and `ChatTool.start_ai_response`.
    """
    config_mgr = ConfigManager()

    # 1. Replicate Model Selection Logic (Mirroring ModelSelectorWidget)
    provider_id = body.provider_id or config_mgr.user_settings.get("chat_llm_id")
    llm_configs = config_mgr.load_llm_configs()
    main_config = next((c.copy() for c in llm_configs if c.get("id") == provider_id), None)

    if not main_config:
        raise OpenAIException(f"Main Model Provider '{provider_id}' not found. Please verify Global Settings.",
                              status_code=404)
    if body.model and body.model != "default":
        main_config["model_name"] = body.model

    # 2. Replicate Translation Logic (Mirroring TransSelectorWidget & detect_primary_language)
    from src.core.lang_detect import detect_primary_language

    trans_provider_id = body.trans_provider_id or config_mgr.user_settings.get("chat_trans_llm_id")
    trans_config = next((c.copy() for c in llm_configs if c.get("id") == trans_provider_id), None)
    if trans_config and body.trans_model:
        trans_config["model_name"] = body.trans_model

    messages_dict = [{"role": m.role, "content": m.content} for m in body.messages]
    last_user_msg = next((m["content"] for m in reversed(messages_dict) if m["role"] == "user"), "")

    is_english = detect_primary_language(last_user_msg) == 'en'
    if body.force_translate is not None:
        requires_translation = body.force_translate
    else:
        requires_translation = (not is_english) and (trans_config is not None)

    # 3. Replicate MCP Tagging Logic (Mirroring _show_filter_menu & get_selected_tags)
    use_mcp_tools = body.use_mcp
    selected_mcp_tags = body.mcp_tags
    if use_mcp_tools and selected_mcp_tags is None:
        available_tags = MCPManager.get_instance().get_available_tags()
        deselected_tags = config_mgr.mcp_servers.get("deselected_mcp_tags", [])
        selected_mcp_tags = [t for t in available_tags if t not in deselected_tags]

    # 4. Replicate Task Initialization (Mirroring ChatTool.chat_task_mgr.start_task)
    q = queue.Queue()
    task_kwargs = {
        "main_config": main_config,
        "trans_config": trans_config,
        "messages": messages_dict,
        "kb_id": body.kb_id,
        "requires_translation": requires_translation,
        "external_context": [],  # Real-time attachment parsing is bypassed in API mode
        "use_mcp": use_mcp_tools,
        "selected_mcp_tags": selected_mcp_tags
    }

    task_id = f"api-{uuid.uuid4().hex[:8]}"
    worker = ChatGenerationTask(task_id, q, task_kwargs)
    threading.Thread(target=worker.run, daemon=True).start()

    def generate():
        created_time = int(time.time())
        parser = APIStreamParser()

        while True:
            try:
                msg_data = q.get(timeout=180)
                state = msg_data.get("state")
                msg_type = msg_data.get("type")

                event_payload = msg_data.get("payload")
                if isinstance(event_payload, dict) and event_payload.get("event") == "translated":
                    translated_text = event_payload.get("text")
                    if body.stream:
                        trans_chunk = {
                            "id": f"chatcmpl-{uuid.uuid4()}",
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": main_config.get("model_name", body.model),
                            "choices": [
                                {"index": 0, "delta": {"translated_query": translated_text}, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(trans_chunk, ensure_ascii=False)}\n\n"
                    continue

                if state == TaskState.SUCCESS.value:
                    if not body.stream:
                        raw_payload = msg_data.get("payload", "")
                        final_clean = _clean_ui_token(raw_payload)

                        temp_parser = APIStreamParser()
                        temp_parser.parse_token(final_clean)

                        response_dict = {
                            "id": f"chatcmpl-{uuid.uuid4()}",
                            "object": "chat.completion",
                            "created": created_time,
                            "model": main_config.get("model_name", body.model),
                            "choices": [{
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": final_clean,
                                    "cited_sources": temp_parser.extract_cited_sources(),
                                    "follow_ups": temp_parser.extract_follow_ups()
                                },
                                "finish_reason": "stop"
                            }],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                        }

                        if translated_text:
                            response_dict["choices"][0]["message"]["translated_query"] = translated_text

                        yield json.dumps(response_dict)
                    else:
                        # Flush buffered metadata as a discrete JSON chunk before stream termination
                        sources = parser.extract_cited_sources()
                        follow_ups = parser.extract_follow_ups()

                        if sources or follow_ups:
                            final_chunk = {
                                "id": f"chatcmpl-{uuid.uuid4()}",
                                "object": "chat.completion.chunk",
                                "created": created_time,
                                "model": main_config.get("model_name", body.model),
                                "choices": [{"index": 0, "delta": {}, "finish_reason": None}]
                            }
                            if sources:
                                final_chunk["choices"][0]["delta"]["cited_sources"] = sources
                            if follow_ups:
                                final_chunk["choices"][0]["delta"]["follow_ups"] = follow_ups

                            yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n"
                    break

                if state == TaskState.FAILED.value:
                    err_msg = msg_data.get("msg", "Internal Task Error")
                    # Translating UI Toast errors to API JSON errors
                    if body.stream:
                        yield f"data: {json.dumps({'error': err_msg})}\n\n"
                    else:
                        yield json.dumps({"error": err_msg})
                    break

                # --- Replicate Token Updates (Mirroring update_ai_bubble) ---
                if msg_type == "state" and msg_data.get("progress") == -1:
                    token = msg_data.get("msg", "")
                    if not token: continue

                    reasoning_chunk, content_chunk = parser.parse_token(token)

                    if body.stream:
                        chunk_dict = {
                            "id": f"chatcmpl-{uuid.uuid4()}",
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": main_config.get("model_name", body.model),
                            "choices": [{"index": 0, "delta": {}, "finish_reason": None}]
                        }

                        has_data = False
                        if reasoning_chunk:
                            chunk_dict["choices"][0]["delta"]["reasoning_content"] = reasoning_chunk
                            has_data = True
                        if content_chunk:
                            chunk_dict["choices"][0]["delta"]["content"] = content_chunk
                            has_data = True

                        if has_data:
                            yield f"data: {json.dumps(chunk_dict, ensure_ascii=False)}\n\n"

            except queue.Empty:
                logger.warning(f"API Task {task_id} timed out waiting for queue message.")
                break
            except Exception as e:
                logger.error(f"Error in API generate loop: {e}")
                break

        if body.stream:
            yield "data: [DONE]\n\n"

    if body.stream:
        return StreamingResponse(generate(), media_type="text/event-stream")
    else:
        final_output = "".join(list(generate()))
        try:
            return JSONResponse(content=json.loads(final_output))
        except:
            return JSONResponse(content={"error": "Failed to parse model response", "raw": final_output},
                                status_code=500)

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
    mcp_mgr.bootstrap_servers(force_all=False)

    tools = mcp_mgr.get_all_tools_schema()

    tags = mcp_mgr.get_available_tags()

    return {
        "available_tags": tags,
        "tools_count": len(tools),
        "tools": tools
    }

@app.post("/api/mcp/filter")
def semantic_filter_mcp_tools(payload: MCPFilterRequest):
    """
    Filters MCP tools dynamically based on user intent utilizing a Reranker model.
    This provides advanced contextual routing identical to the GUI's RAG pipeline.
    """
    mcp_mgr = MCPManager.get_instance()
    mcp_mgr.bootstrap_servers(force_all=False)

    # 1. Apply preliminary tag-based filtering if specified
    if payload.mcp_tags is not None:
        raw_tools = mcp_mgr.get_tools_schema_by_tags(payload.mcp_tags)
    else:
        raw_tools = mcp_mgr.get_all_tools_schema()

    if not raw_tools or len(raw_tools) <= payload.top_k:
        return {"filtered_tools": raw_tools, "status": "bypassed_insufficient_tools"}

    # 2. Construct document representations for the Reranker
    candidate_docs = []
    for tool in raw_tools:
        func = tool.get("function", {})
        content = f"Tool Name: {func.get('name', '')}. Description: {func.get('description', '')}"
        candidate_docs.append({
            "content": content,
            "metadata": {"tool_schema": tool}
        })

    context_str = f" Previous Context: {payload.history_context}" if payload.history_context else ""
    rerank_query = f"User Intent: {payload.query}.{context_str} Find the most appropriate API tools to fulfill this request."

    # 3. Dispatch the isolated multiprocessing task
    queue = mp.Queue()
    worker = RunnerProcess(
        RerankTask, "api_rerank_sync", queue,
        {"query": rerank_query, "docs": candidate_docs, "domain": "Tool Selection", "top_k": payload.top_k}
    )
    worker.start()

    ranked = None
    error_msg = None

    while True:
        try:
            data = queue.get(timeout=0.2)
            state = data.get("state")

            if state == TaskState.SUCCESS.value:
                ranked = data.get("payload") or candidate_docs[:payload.top_k]
                break
            elif state == TaskState.FAILED.value:
                error_msg = data.get('msg', 'Unknown execution anomaly.')
                break
        except q.Empty:
            if not worker.is_alive():
                error_msg = "Rerank process terminated unexpectedly."
                break
        except Exception as e:
            if not worker.is_alive():
                error_msg = str(e)
                break

    # 4. Handle degradation or successful extraction
    if error_msg:
        logger.warning(f"API Tool reranking failed: {error_msg}. Silently degrading to full toolset.")
        return {"filtered_tools": raw_tools, "status": "degraded_error", "error": error_msg}

    best_tools = [doc["metadata"]["tool_schema"] for doc in ranked]
    return {
        "status": "success",
        "original_count": len(raw_tools),
        "filtered_count": len(best_tools),
        "filtered_tools": best_tools
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
    config_mgr = ConfigManager()
    host = config_mgr.user_settings.get("api_server_host", "127.0.0.1")

    try:
        port = int(config_mgr.user_settings.get("api_server_port", 8000))
    except (ValueError, TypeError):
        port = 8000

    api_key = config_mgr.user_settings.get("api_server_key", "").strip()

    logger.info(f"Starting Standalone API Server on {host}:{port}")
    if api_key:
        logger.info("API Key authentication is ENABLED. Requests must include the Bearer token.")
    else:
        logger.warning("API Key authentication is DISABLED (No key set in Global Settings).")

    uvicorn.run(app, host=host, port=port, log_level="warning")
