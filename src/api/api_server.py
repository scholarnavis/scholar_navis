import asyncio
import json
import logging
import queue
import re
import threading
import time
import uuid
from typing import List, Dict, Optional, Any

import uvicorn
from fastapi import FastAPI, Request, Depends, Body
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from src.core.config_manager import ConfigManager
from src.core.core_task import TaskState
from src.core.device_manager import DeviceManager
from src.core.kb_manager import KBManager
from src.core.mcp_manager import MCPManager
from src.core.models_registry import check_model_exists, get_model_conf, resolve_auto_model
from src.core.version import __version__
from src.task.chat_tasks import ChatGenerationTask

app = FastAPI(
    title="Scholar Navis Agentic API",
    version=__version__,
    description=(
        "OpenAI-compatible chat and tooling API for Scholar Navis.\n\n"
        "Supports streaming completions, knowledge-base RAG, MCP tool routing, "
        "and multi-provider LLM selection."
    ),
    openapi_tags=[
        {"name": "Chat", "description": "Chat completions (OpenAI-compatible)"},
        {"name": "Models & Providers", "description": "List available models and providers"},
        {"name": "MCP Tools", "description": "Model Context Protocol tool discovery and filtering"},
        {"name": "Knowledge Bases", "description": "RAG knowledge-base management"},
        {"name": "System", "description": "System state and health"},
    ],
)
logger = logging.getLogger("API_Server")
security = HTTPBearer(auto_error=False)


class OpenAIException(Exception):
    def __init__(self, message: str, status_code: int = 400, error_type: str = "invalid_request_error"):
        self.message = message
        self.status_code = status_code
        self.error_type = error_type


@app.exception_handler(OpenAIException)
async def openai_exception_handler(request: Request, exc: OpenAIException):
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


# ==========================================
# Response Models
# ==========================================

class ChoiceMessage(BaseModel):
    role: str = Field(..., description="The role of the message author (e.g. 'assistant').")
    content: str = Field(..., description="The main reply text.")
    reasoning_content: Optional[str] = Field(None, description="Chain-of-thought reasoning tokens (if any).")
    follow_ups: Optional[List[str]] = Field(None, description="Suggested follow-up questions.")
    cited_sources: Optional[List[str]] = Field(None, description="List of cited source descriptions.")
    translated_query: Optional[str] = Field(None, description="Translated version of the user query (when translation was triggered).")


class Choice(BaseModel):
    index: int = Field(..., description="Index of this choice in the list (always 0 for now).")
    message: ChoiceMessage
    finish_reason: Optional[str] = Field(None, description="Reason the model stopped generating. 'stop' means complete, 'length' means truncated.")


class UsageInfo(BaseModel):
    prompt_tokens: int = Field(0, description="Number of tokens in the prompt.")
    completion_tokens: int = Field(0, description="Number of tokens in the completion.")
    total_tokens: int = Field(0, description="Total tokens used.")


class ChatCompletionResponse(BaseModel):
    """Non-streaming response object matching OpenAI chat.completion format."""
    id: str = Field(..., description="Unique identifier for this completion.")
    object: str = Field("chat.completion", description="Object type, always 'chat.completion'.")
    created: int = Field(..., description="Unix timestamp (seconds) of when the completion was created.")
    model: str = Field(..., description="The model used for this completion.")
    choices: List[Choice] = Field(..., description="List of completion choices.")
    usage: Optional[UsageInfo] = Field(None, description="Token usage statistics.")


class StreamChoiceDelta(BaseModel):
    role: Optional[str] = Field(None, description="Role of the author (only in the first chunk).")
    content: Optional[str] = Field(None, description="Incremental content token.")
    reasoning_content: Optional[str] = Field(None, description="Incremental reasoning token.")
    translated_query: Optional[str] = Field(None, description="Translated user query (chunk for translation event).")
    cited_sources: Optional[List[str]] = Field(None, description="Cited sources (flushed in the final chunk).")
    follow_ups: Optional[List[str]] = Field(None, description="Suggested follow-ups (flushed in the final chunk).")


class StreamChoice(BaseModel):
    index: int = Field(0, description="Choice index.")
    delta: StreamChoiceDelta
    finish_reason: Optional[str] = Field(None, description="'stop' on the final chunk, otherwise null.")


class ChatCompletionChunk(BaseModel):
    """A single server-sent event chunk during streaming."""
    id: str = Field(..., description="Completion ID (unique per request).")
    object: str = Field("chat.completion.chunk", description="Always 'chat.completion.chunk'.")
    created: int = Field(..., description="Unix timestamp of creation.")
    model: str = Field(..., description="Model name.")
    choices: List[StreamChoice]


# ==========================================
# Request Models
# ==========================================

class ChatMessage(BaseModel):
    """A single message in the conversation history."""
    role: str = Field(
        ...,
        description="The role of the message author.",
        examples=["system", "user", "assistant"],
    )
    content: str = Field(
        ...,
        description="The text content of the message.",
        examples=["Explain quantum entanglement in simple terms."],
    )


class ChatCompletionRequest(BaseModel):
    """
    Request body for chat completions.
    Compatible with the OpenAI /v1/chat/completions schema, with additional
    Scholar Navis extensions for knowledge-base, translation, and MCP tool control.
    """
    model: str = Field(
        "default",
        description=(
            "Model name to use. Pass 'default' to use the globally configured model. "
            "You can also pass a specific model name from the provider's available models."
        ),
        examples=["default", "gpt-4o", "deepseek-chat"],
    )
    messages: List[ChatMessage] = Field(
        ...,
        description="The conversation history. The last 'user' message is treated as the query.",
        min_length=1,
    )
    stream: Optional[bool] = Field(
        False,
        description="If true, tokens are streamed back as server-sent events (SSE).",
    )
    temperature: Optional[float] = Field(
        None,
        description="Sampling temperature (0.0–2.0). Higher values produce more random outputs.",
        ge=0.0,
        le=2.0,
    )
    provider_id: Optional[str] = Field(
        None,
        description=(
            "ID of the LLM provider to use (e.g. 'openai', 'deepseek'). "
            "Overrides the global setting."
        ),
        examples=["openai"],
    )
    trans_provider_id: Optional[str] = Field(
        None,
        description="Provider ID for the translation model. Overrides global translation provider.",
    )
    trans_model: Optional[str] = Field(
        None,
        description="Specific model name for translation, within the translation provider.",
    )
    kb_id: Optional[str] = Field(
        "none",
        description=(
            "Knowledge-base ID for RAG retrieval. "
            "Pass 'none' or omit to disable RAG entirely."
        ),
        examples=["none", "kb-abc123"],
    )
    use_academic_agent: Optional[bool] = Field(
        True,
        description="Whether to enable the internal academic agent (Native Skills).",
    )
    academic_tags: Optional[List[str]] = Field(
        None,
        description="Filter internal academic tools by tags. Null means use all.",
    )
    use_external_tools: Optional[bool] = Field(
        False,
        description="Whether to enable external tools (MCP + Custom Scripts).",
    )
    external_tool_names: Optional[List[str]] = Field(
        None,
        description="Exact identifiers of enabled external tools (e.g., '[MCP] web_search'). Null means use all.",
    )
    force_translate: Optional[bool] = Field(
        None,
        description=(
            "Force translation of the user query before LLM processing. "
            "When null, translation is auto-detected based on language."
        ),
    )


class CompletionRequest(BaseModel):
    """Request body for legacy /completions-style (single prompt) generation."""
    model: str = Field("default", description="Model name or 'default' for the global setting.")
    prompt: str = Field(..., description="The raw prompt text.")
    stream: Optional[bool] = Field(False, description="Enable SSE streaming.")
    temperature: Optional[float] = Field(None, description="Sampling temperature.", ge=0.0, le=2.0)
    kb_id: Optional[str] = Field("none", description="Knowledge-base ID for RAG, or 'none'.")
    use_mcp: Optional[bool] = Field(True, description="Enable MCP tool usage.")


class SemanticFilterRequest(BaseModel):
    """Request body for semantic Agent tool filtering."""
    query: str = Field(..., description="Natural-language query describing the user's intent.")
    history_context: Optional[str] = Field("", description="Optional conversation history.")
    top_k: Optional[int] = Field(8, description="Maximum number of tools to return after ranking.")
    use_academic_agent: Optional[bool] = Field(True, description="Include internal academic agent tools.")
    academic_tags: Optional[List[str]] = Field(None, description="Pre-filter academic tools by tags.")
    use_external_tools: Optional[bool] = Field(False, description="Include external MCPs and Skills.")
    external_tool_names: Optional[List[str]] = Field(None, description="Pre-filter external tools by identifiers.")

class SemanticFilterResponse(BaseModel):
    status: str = Field(..., description="'success', 'bypassed_insufficient_tools', or 'degraded_error'.")
    original_count: Optional[int] = Field(None, description="Tool count before filtering.")
    filtered_count: Optional[int] = Field(None, description="Tool count after filtering.")
    filtered_tools: List[Dict[str, Any]] = Field(..., description="The filtered tool schemas.")
    error: Optional[str] = Field(None, description="Error message if status is 'degraded_error'.")

class AgentFilterPayload(BaseModel):
    deselected_external_tools: List[str] = Field(..., description="List of deselected external tool identifiers.")


class ModelListPayload(BaseModel):
    """Request body for listing models of a specific provider."""
    provider: Optional[str] = Field(
        None,
        description="Provider ID whose models should be listed.",
        examples=["openai", "deepseek"],
    )


# ==========================================
# System State Response Models
# ==========================================

class SystemStateResponse(BaseModel):
    active_device: str = Field(..., description="Currently configured inference device (e.g. 'cuda', 'cpu', 'auto').")
    mcp_tools: int = Field(..., description="Number of registered MCP tools.")
    kbs: int = Field(..., description="Number of available knowledge bases.")


class ProviderInfo(BaseModel):
    id: str = Field(..., description="Unique provider identifier.")
    name: str = Field(..., description="Human-readable provider name.")
    is_active: bool = Field(..., description="Whether this is the currently active provider.")
    models: List[str] = Field(..., description="List of model names available from this provider.")
    current_model: str = Field(..., description="The currently selected model for this provider.")


class ProviderListResponse(BaseModel):
    providers: List[ProviderInfo]


class ModelInfo(BaseModel):
    id: str = Field(..., description="Model identifier.")
    object: str = Field("model", description="Object type, always 'model'.")
    created: int = Field(..., description="Unix timestamp of retrieval.")
    owned_by: str = Field(..., description="Provider or organization that owns this model.")


class ModelListResponse(BaseModel):
    object: str = Field("list", description="Always 'list'.")
    data: List[ModelInfo]


class MCPToolListResponse(BaseModel):
    available_tags: List[str] = Field(..., description="All available MCP tool tags.")
    tools_count: int = Field(..., description="Total number of MCP tools.")
    tools: List[Dict[str, Any]] = Field(..., description="Full OpenAI-function-call tool schemas.")


class MCPFilterResponse(BaseModel):
    status: str = Field(..., description="'success', 'bypassed_insufficient_tools', or 'degraded_error'.")
    original_count: Optional[int] = Field(None, description="Tool count before filtering.")
    filtered_count: Optional[int] = Field(None, description="Tool count after filtering.")
    filtered_tools: List[Dict[str, Any]] = Field(..., description="The filtered tool schemas.")
    error: Optional[str] = Field(None, description="Error message if status is 'degraded_error'.")


class KBInfo(BaseModel):
    id: str = Field(..., description="Knowledge-base identifier.")
    name: str = Field(..., description="Human-readable name.")
    domain: str = Field(..., description="Domain or topic of the knowledge base.")
    doc_count: int = Field(..., description="Number of indexed documents.")


class KBListResponse(BaseModel):
    knowledge_bases: List[KBInfo]


# ==========================================
# Helpers (unchanged logic, trimmed for brevity)
# ==========================================

def _clean_ui_token(raw_token: str) -> str:
    if raw_token in ["[CLEAR_SEARCH]", "[START_LLM_NETWORK]"]:
        return ""
    clean = raw_token
    clean = re.sub(r'<mcp_process.*?>.*?</mcp_process>\n*', '', clean, flags=re.IGNORECASE | re.DOTALL)
    clean = re.sub(r'<div class=[\'"]status-msg[\'"].*?>.*?</div>\n*', '', clean, flags=re.IGNORECASE | re.DOTALL)
    clean = re.sub(r'<i.*?>.*?</i>\n*', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'<div.*?class=[\'"]header-.*?</div>\n*', '', clean, flags=re.IGNORECASE | re.DOTALL)
    clean = re.sub(r"<a[^>]*href=['\"]cite://[^>]*>.*?<b>\[(\d+)\]</b>.*?</a>", r"[\1]", clean, flags=re.IGNORECASE)
    clean = re.sub(r'<br\s*/?>', '\n', clean, flags=re.IGNORECASE)
    clean = re.sub(r'<hr.*?>', '\n---\n', clean, flags=re.IGNORECASE)
    clean = re.sub(r'<b>(.*?)</b>', r'**\1**', clean, flags=re.IGNORECASE)
    clean = re.sub(r'<div.*?>', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'</div>', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'<span.*?>', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'</span>', '', clean, flags=re.IGNORECASE)
    return clean


class APIStreamParser:
    def __init__(self):
        self.is_thinking = False
        self.current_section = "content"
        self.follow_up_buffer = ""
        self.cited_sources_buffer = ""

    def parse_token(self, raw_token: str):
        token = _clean_ui_token(raw_token)
        if not token:
            return None, None
        if "📚 Cited Sources" in token or "[CITED_SOURCES]" in token:
            self.current_section = "sources"
            token = re.sub(r'(?i)(📚\s*Cited Sources|\[CITED_SOURCES\]|Cited Sources:?)', '', token)
        if "💡 Suggested Follow-ups" in token or "[FOLLOW_UPS]" in token:
            self.current_section = "follow_ups"
            token = re.sub(r'(?i)(💡\s*Suggested Follow-ups|\[FOLLOW_UPS\]|Suggested Follow-ups:?)', '', token)
        if self.current_section == "sources":
            self.cited_sources_buffer += token
            return None, None
        elif self.current_section == "follow_ups":
            self.follow_up_buffer += token
            return None, None
        reasoning_chunk = ""
        content_chunk = ""
        if "" in token:
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
        items = []
        for line in buffer.split('\n'):
            line = line.strip()
            line = re.sub(r'^>\s*', '', line)
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
    provider_id = request.provider_id or config.user_settings.get("chat_llm_id")
    llm_configs = config.load_llm_configs()
    main_config = next((c for c in llm_configs if c.get("id") == provider_id), None)
    if not main_config:
        raise OpenAIException(f"Main Model Provider '{provider_id}' not found.", status_code=400, error_type="invalid_request_error")
    if request.kb_id and request.kb_id != "none":
        kb_info = KBManager().get_kb_by_id(request.kb_id)
        if not kb_info:
            raise OpenAIException(f"Knowledge Base '{request.kb_id}' not found or deleted.", status_code=404, error_type="invalid_request_error")
        user_device = config.user_settings.get("inference_device", "Auto")
        target_device = DeviceManager().parse_device_string(user_device)
        model_id = kb_info.get('model_id', 'embed_auto')
        conf = get_model_conf(model_id, "embedding")
        if not conf or conf.get('is_auto'):
            model_id = resolve_auto_model("embedding", target_device)
        if not check_model_exists(model_id):
            raise OpenAIException(f"Required Embedding model '{model_id}' for this KB is missing.", status_code=500, error_type="model_missing_error")
        rerank_id = config.user_settings.get("rerank_model_id", "rerank_auto")
        conf_rerank = get_model_conf(rerank_id, "reranking")
        if not conf_rerank or conf_rerank.get('is_auto'):
            rerank_id = resolve_auto_model("reranking", target_device)
        if not check_model_exists(rerank_id):
            raise OpenAIException(f"Required Reranker model '{rerank_id}' is missing.", status_code=500, error_type="model_missing_error")


# ==========================================
# Endpoints
# ==========================================

@app.post(
    "/v1/chat/completions",
    tags=["Chat"],
    summary="Create chat completion",
    description=(
        "OpenAI-compatible chat completion endpoint. "
        "Supports both streaming (SSE) and non-streaming modes. "
        "Extends the standard schema with Scholar Navis parameters for "
        "knowledge-base RAG, MCP tool routing, and automatic translation."
    ),
    responses={
        200: {
            "description": "Successful completion",
            "content": {
                "application/json": {
                    "example": {
                        "id": "chatcmpl-abc123",
                        "object": "chat.completion",
                        "created": 1700000000,
                        "model": "gpt-4o",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": "Quantum entanglement is...",
                                    "cited_sources": [],
                                    "follow_ups": []
                                },
                                "finish_reason": "stop"
                            }
                        ],
                        "usage": {"prompt_tokens": 42, "completion_tokens": 128, "total_tokens": 170}
                    }
                }
            },
        },
        401: {"description": "Authentication failed (invalid or missing API key)"},
        404: {"description": "Requested model provider or knowledge base not found"},
        500: {"description": "Required model missing or internal server error"},
    },
)
async def chat_completions(
    body: ChatCompletionRequest,
    api_key: str = Depends(verify_api_key),
):
    config_mgr = ConfigManager()
    provider_id = body.provider_id or config_mgr.user_settings.get("chat_llm_id")
    llm_configs = config_mgr.load_llm_configs()
    main_config = next((c.copy() for c in llm_configs if c.get("id") == provider_id), None)

    if not main_config:
        raise OpenAIException(f"Main Model Provider '{provider_id}' not found.", status_code=404)
    if body.model and body.model != "default":
        main_config["model_name"] = body.model

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

    acad_tags = [t.replace("[ACADEMIC]", "").strip() for t in body.academic_tags if
                 isinstance(t, str)] if body.academic_tags else []
    ext_tags = [t.replace("[External]", "").strip() for t in body.external_tool_names if
                isinstance(t, str)] if body.external_tool_names else []

    use_acad = body.use_academic_agent
    if use_acad and body.academic_tags is not None and len(body.academic_tags) == 0:
        use_acad = False

    use_ext = body.use_external_tools
    if use_ext and body.external_tool_names is not None and len(body.external_tool_names) == 0:
        use_ext = False

    task_queue = queue.Queue()
    task_kwargs = {
        "main_config": main_config,
        "trans_config": trans_config,
        "messages": messages_dict,
        "kb_id": body.kb_id,
        "requires_translation": requires_translation,
        "external_context": [],
        "use_academic_agent": use_acad,
        "academic_tags": acad_tags,
        "use_external_tools": use_ext,
        "external_tool_names": ext_tags
    }

    task_id = f"api-{uuid.uuid4().hex[:8]}"
    worker = ChatGenerationTask(task_id, task_queue, task_kwargs)
    threading.Thread(target=worker.run, daemon=True).start()

    async def generate():
        created_time = int(time.time())
        parser = APIStreamParser()
        while True:
            try:
                msg_data = await asyncio.to_thread(task_queue.get, True, 600)
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
                            "choices": [{"index": 0, "delta": {"translated_query": translated_text}, "finish_reason": None}]
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
                        yield json.dumps(response_dict)
                    else:
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
                    error_payload = {
                        "error": {
                            "message": err_msg,
                            "type": "upstream_server_error",
                            "param": None,
                            "code": 500
                        }
                    }
                    if body.stream:
                        yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
                    else:
                        yield json.dumps(error_payload, ensure_ascii=False)
                    break

                if msg_type == "state" and msg_data.get("progress") == -1:
                    token = msg_data.get("msg", "")
                    if not token:
                        continue
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
                error_payload = {
                    "error": {
                        "message": f"Internal API Server Error: {str(e)}",
                        "type": "internal_server_error",
                        "param": None,
                        "code": 500
                    }
                }

                if body.stream:
                    yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
                else:
                    yield json.dumps(error_payload, ensure_ascii=False)

                break

        if body.stream:
            yield "data: [DONE]\n\n"

    if body.stream:
        return StreamingResponse(generate(), media_type="text/event-stream")
    else:
        final_output_chunks = []
        async for chunk in generate():
            final_output_chunks.append(chunk)
        final_output = "".join(final_output_chunks)

        try:
            parsed_response = json.loads(final_output)
            # 如果解析结果包含 OpenAI 标准错误体，则抛出 500 状态码
            status_code = 500 if "error" in parsed_response else 200
            return JSONResponse(content=parsed_response, status_code=status_code)
        except Exception:
            return JSONResponse(
                content={
                    "error": {
                        "message": "Failed to parse model response.",
                        "type": "parse_error",
                        "param": None,
                        "code": 500
                    },
                    "raw": final_output
                },
                status_code=500
            )


@app.get(
    "/api/state",
    response_model=SystemStateResponse,
    tags=["System"],
    summary="Get system state",
    description="Returns current device, MCP tool count, and knowledge-base count.",
)
def get_system_state():
    return {
        "active_device": ConfigManager().user_settings.get("inference_device", "auto"),
        "mcp_tools": len(MCPManager.get_instance().get_all_tools_schema()),
        "kbs": len(KBManager().get_all_kbs()),
    }


@app.post(
    "/v1/models",
    response_model=ModelListResponse,
    tags=["Models & Providers"],
    summary="List models for a provider",
    description="Returns available models for the specified LLM provider, OpenAI-compatible.",
)
def list_models(payload: ModelListPayload = Body(...)):
    provider_id = payload.provider
    config = ConfigManager()
    llm_configs = config.load_llm_configs()
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
            "owned_by": target_conf.get("name", "custom"),
        })
    return {"object": "list", "data": data}


@app.get(
    "/api/providers",
    response_model=ProviderListResponse,
    tags=["Models & Providers"],
    summary="List all LLM providers",
    description="Returns all configured providers with their models and active status.",
)
def list_all_providers():
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
            "current_model": c.get("model_name", ""),
        })
    return {"providers": result}


@app.get(
    "/api/agent/tools",
    tags=["Agent & Tools"],
    summary="List all available Agent tools",
    description="Returns separated lists: Academic tags for internal tools, and exact identifiers for external tools."
)
def list_agent_tools():
    from src.core.skill_manager import SkillManager
    from src.core.mcp_manager import MCPManager
    import re

    skill_mgr = SkillManager.get_instance()
    mcp_mgr = MCPManager.get_instance()

    # 1. Parse unique tags from Academic Skills
    academic_tags = set()
    for schema in skill_mgr.get_academic_schemas():
        desc = schema.get("function", {}).get("description", "")
        match = re.search(r"\[Tags:\s*(.*?)\]", desc)
        if match:
            for t in match.group(1).split(","):
                academic_tags.add(f"[ACADEMIC] {t.strip().title()}")

    # 2. Collect all External Tools with standardized identifiers
    external_tools = []

    for schema in skill_mgr.get_external_schemas():
        name = schema.get("function", {}).get("name", "Unknown")
        external_tools.append({
            "identifier": f"[External] {name}",
            "name": name,
            "type": "native_skill"
        })

    for schema in mcp_mgr.get_all_tools_schema():
        server_name = schema.get("server", "Unknown Server")
        if server_name not in [et["name"] for et in external_tools if et["type"] == "mcp_server"]:
            external_tools.append({
                "identifier": f"[External] {server_name}",
                "name": server_name,
                "type": "mcp_server"
            })

    return {
        "academic_tags": sorted(list(academic_tags)),
        "external_tools": external_tools
    }


@app.post(
    "/api/agent/filter",
    tags=["Agent & Tools"],
    summary="Save disabled external tools filter",
    description="Receives a list of disabled external tool identifiers from the frontend and saves them to ConfigManager."
)
def save_agent_filter(payload: AgentFilterPayload):
    config_mgr = ConfigManager()
    if "external_skills" not in config_mgr.user_settings:
        config_mgr.user_settings["external_skills"] = {}
    config_mgr.user_settings["external_skills"]["deselected_external_tools"] = payload.deselected_external_tools
    config_mgr.save_settings()
    return {"status": "success"}


@app.post(
    "/api/agent/semantic_filter",
    response_model=SemanticFilterResponse,
    tags=["Agent & Tools"],
    summary="Semantic Agent tool filter",
    description="Aggregates internal SKILL and external MCP/SKILL schemas based on dual-track toggles, and ranks them against user intent."
)
def semantic_filter_agent_tools(payload: SemanticFilterRequest):
    from src.core.skill_manager import SkillManager
    from src.core.mcp_manager import MCPManager
    skill_mgr = SkillManager.get_instance()
    mcp_mgr = MCPManager.get_instance()

    # 修改：清洗 API 请求参数的前缀
    acad_tags = [t.replace("[ACADEMIC]", "").strip() for t in payload.academic_tags if isinstance(t, str)] if payload.academic_tags else []
    ext_names = [t.replace("[External]", "").strip() for t in payload.external_tool_names if isinstance(t, str)] if payload.external_tool_names else []

    use_acad = payload.use_academic_agent
    if use_acad and payload.academic_tags is not None and len(payload.academic_tags) == 0:
        use_acad = False

    use_ext = payload.use_external_tools
    if use_ext and payload.external_tool_names is not None and len(payload.external_tool_names) == 0:
        use_ext = False

    logger.warning("\n" + "=" * 50)
    logger.warning("🔍 [RERANKER DEBUG] Starting Semantic Filter Tool Selection")
    logger.warning(f"-> Query Intent: {payload.query}")
    logger.warning(f"-> Academic Agent Enabled: {use_acad}")
    logger.warning(f"-> Cleaned Academic Tags Filter: {acad_tags}")
    logger.warning(f"-> External Tools Enabled: {use_ext}")
    logger.warning(f"-> Cleaned External Names Filter: {ext_names}")

    raw_tools = []

    # 1. Gather Internal Academic Tools
    if use_acad:
        raw_academic = skill_mgr.get_academic_schemas(acad_tags)
        if raw_academic:
            raw_tools.extend(raw_academic)
            logger.warning(f"-> Pulled {len(raw_academic)} Academic Tools based on tags.")

    # 2. Gather External Tools (SKILL + MCP)
    if use_ext:
        ext_skills = skill_mgr.get_external_schemas(ext_names)
        if ext_skills:
            raw_tools.extend(ext_skills)
            logger.warning(f"-> Pulled {len(ext_skills)} External Skills.")

        mcp_tools = mcp_mgr.get_all_tools_schema() or []
        mcp_added_count = 0
        for schema in mcp_tools:
            server_name = schema.get("server", "Unknown Server")
            if not ext_names or any(server_name in str(name) for name in ext_names):
                raw_tools.append(schema)
                mcp_added_count += 1
        logger.warning(f"-> Pulled {mcp_added_count} MCP Tools based on names.")

    logger.warning(f"✅ Total Candidate Tools before Reranking: {len(raw_tools)}")
    if raw_tools:
        names = [t.get("function", {}).get("name", "Unknown") for t in raw_tools]
        logger.warning(f"-> Candidate Names: {', '.join(names)}")

    if not raw_tools or len(raw_tools) <= payload.top_k:
        logger.warning(f"⏭ Bypassed Reranker: Candidate count ({len(raw_tools)}) <= Top-K ({payload.top_k}).")
        logger.warning("=" * 50 + "\n")
        return {"filtered_tools": raw_tools, "status": "bypassed_insufficient_tools"}

    candidate_docs = []
    for tool in raw_tools:
        func = tool.get("function", {})
        content = f"Tool Name: {func.get('name', '')}. Description: {func.get('description', '')}"
        candidate_docs.append({"content": content, "metadata": {"tool_schema": tool}})

    context_str = f" Previous Context: {payload.history_context}" if payload.history_context else ""
    rerank_query = f"User Intent: {payload.query}.{context_str} Find the most appropriate API tools to fulfill this request."

    # [修改点开始]：彻底移除 RunnerProcess 和 mp.Queue()，直接调用单例引擎
    from src.core.rerank_engine import RerankEngine

    ranked = None
    error_msg = None

    try:
        engine = RerankEngine()
        # 直接在当前 FastAPI 的 worker 线程中同步执行打分
        ranked = engine.rerank(rerank_query, candidate_docs, domain="Tool Selection", top_k=payload.top_k)

        if not ranked:
            ranked = candidate_docs[:payload.top_k]
    except Exception as e:
        error_msg = str(e)
        ranked = candidate_docs[:payload.top_k]
    # [修改点结束]

    if error_msg:
        logger.warning(f"❌ Reranker failed: {error_msg}. Falling back to full toolset.")
        logger.warning("=" * 50 + "\n")
        return {"filtered_tools": raw_tools, "status": "degraded_error", "error": error_msg}

    best_tools = [doc["metadata"]["tool_schema"] for doc in ranked]
    best_names = [t.get("function", {}).get("name", "Unknown") for t in best_tools]

    logger.warning(f"🏆 Reranker Success! Filtered down to Top {len(best_tools)}.")
    logger.warning(f"-> Selected Names: {', '.join(best_names)}")
    logger.warning("=" * 50 + "\n")

    return {
        "status": "success",
        "original_count": len(raw_tools),
        "filtered_count": len(best_tools),
        "filtered_tools": best_tools,
    }


@app.get(
    "/api/kbs",
    response_model=KBListResponse,
    tags=["Knowledge Bases"],
    summary="List knowledge bases",
    description="Returns all indexed, ready-to-use knowledge bases for RAG.",
)
def list_knowledge_bases():
    kb_manager = KBManager()
    kbs = kb_manager.get_all_kbs()
    result = []
    for kb in kbs:
        if kb.get('status') == 'ready':
            result.append({
                "id": kb.get("id"),
                "name": kb.get("name"),
                "domain": kb.get("domain", "General Academic"),
                "doc_count": kb.get("doc_count", 0),
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
    api_key = config_mgr.user_settings.get("api_server_key", "123456").strip()
    logger.info(f"Starting Standalone API Server on {host}:{port}")
    if api_key:
        logger.info("API Key authentication is ENABLED.")
    else:
        logger.warning("API Key authentication is DISABLED.")
    uvicorn.run(app, host=host, port=port, log_level="warning")
