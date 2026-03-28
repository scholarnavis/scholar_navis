import gc
import json
import os
import base64
import mimetypes
import re
import sys
import time
import uuid
import multiprocessing as mp
import queue as q
from urllib.parse import quote

import chardet


from src.core.config_manager import ConfigManager
from src.core.core_task import BackgroundTask
from src.core.device_manager import DeviceManager
from src.core.llm_impl import OpenAICompatibleLLM, get_cached_translation
from src.core.mcp_manager import MCPManager
from src.core.models_registry import get_model_conf, resolve_auto_model


class ProcessAttachmentTask(BackgroundTask):
    def _execute(self):
        file_infos = self.kwargs.get('file_infos', [])

        if not file_infos:
            paths = self.kwargs.get('paths', [])
            file_infos = [{"path": p, "name": os.path.basename(p)} for p in paths]

        chunks = []
        html = ""
        total = len(file_infos)

        for i, info in enumerate(file_infos):
            if self.is_cancelled():
                self.send_log("INFO", "Attachment processing cancelled by user.")
                raise InterruptedError("Task was safely terminated by the user.")

            path = info['path']
            f_name = info['name']

            ext = f_name.lower()

            try:
                # 1. Image processing
                if ext.endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp')):
                    self.update_progress(int((i / total) * 100), f"Encoding image: {f_name}...")
                    with open(path, "rb") as image_file:
                        encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                        mime_type, _ = mimetypes.guess_type(f_name)
                        mime_type = mime_type or 'image/jpeg'

                        chunks.append({
                            "path": path, "name": f_name, "page": 1,
                            "type": "image",
                            "base64_url": f"data:{mime_type};base64,{encoded_string}",
                            "content": f"[Image Attached: {f_name}]"
                        })

                    link = f"cite://view?path={quote(path)}&page=1&name={quote(f_name)}"
                    html += f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#05B8CC; text-decoration:none;'>🖼️ {f_name}</a></div>"

                # 2. PDF processing
                elif ext.endswith('.pdf'):
                    self.update_progress(int((i / total) * 100), f"Parsing PDF: {f_name}...")
                    import pymupdf4llm
                    md_chunks = pymupdf4llm.to_markdown(path, page_chunks=True)
                    for chunk in md_chunks:
                        text = chunk.get("text", "").strip()
                        if len(text) > 10:
                            chunks.append({
                                "path": path, "name": f_name, "page": chunk.get("metadata", {}).get("page", 1),
                                "content": text
                            })
                    link = f"cite://view?path={quote(path)}&page=1&name={quote(f_name)}"
                    html += f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#05B8CC; text-decoration:none;'>📄 {f_name}</a></div>"

                # 2.5 DOCX processing
                elif ext.endswith('.docx'):
                    self.update_progress(int((i / total) * 100), f"Parsing DOCX: {f_name}...")
                    import docx
                    doc = docx.Document(path)

                    text = "\n".join([paragraph.text for paragraph in doc.paragraphs if paragraph.text.strip()])

                    if len(text) > 10:
                        chunks.append({
                            "path": path, "name": f_name, "page": 1,
                            "content": text
                        })
                    link = f"cite://view?path={quote(path)}&page=1&name={quote(f_name)}"
                    html += f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#05B8CC; text-decoration:none;'>📄 {f_name}</a></div>"

                # 2.6 老旧 DOC 拦截
                elif ext.endswith('.doc'):
                    self.send_log("ERROR",
                                  f"Legacy .doc format is not natively supported. Please convert {f_name} to .docx")
                    # 给用户一个 HTML 提示，不用提取内容
                    link = f"cite://view?path={quote(path)}&page=1&name={quote(f_name)}"
                    html += f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#ffb86c; text-decoration:none;'>⚠️ {f_name} (Please convert to .docx)</a></div>"



                # 3. Text processing
                else:
                    self.update_progress(int((i / total) * 100), f"Reading file: {f_name}...")
                    text = ""

                    with open(path, 'rb') as f:
                        raw_data = f.read()
                        detected = chardet.detect(raw_data)

                        # 如果文件太小或特征不明显导致检测失败，默认回退到 utf-8
                        encoding = detected['encoding'] if detected['encoding'] else 'utf-8'
                        text = raw_data.decode(encoding, errors='replace').strip()

                    if text:
                        chunks.append({
                            "path": path, "name": f_name, "page": 1, "content": text
                        })
                    link = f"cite://view?path={quote(path)}&page=1&name={quote(f_name)}"
                    html += f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#05B8CC; text-decoration:none;'>📄 {f_name}</a></div>"

            except Exception as e:
                self.send_log("ERROR", f"Failed to process {f_name}: {e}")
                raise e

        self.update_progress(100, "Finalizing...")

        import json
        import tempfile
        import uuid

        result_dict = {"chunks": chunks, "html": html}
        temp_file_path = os.path.join(tempfile.gettempdir(), f"task_payload_{uuid.uuid4().hex}.json")

        with open(temp_file_path, 'w', encoding='utf-8') as f:
            json.dump(result_dict, f, ensure_ascii=False)

        return {"_is_temp_file": True, "path": temp_file_path}


# [Context: Imports at the top of chat_tasks.py]
from src.core.core_task import BackgroundTask, TaskState
from src.core.kb_manager import KBManager, DatabaseManager


class ChatGenerationTask(BackgroundTask):
    """
    Background task for executing local/remote LLM interactions, Vector Retrieval,
    and Multi-Agent tool processing within the Core Task Framework.
    """

    def cancel(self):
        super().cancel()
        if hasattr(self, 'main_llm') and self.main_llm: self.main_llm.cancel()
        if hasattr(self, 'trans_llm') and self.trans_llm: self.trans_llm.cancel()
        if hasattr(self, 'vision_llm') and self.vision_llm: self.vision_llm.cancel()

    def _init_llms(self):
        if self.main_config and not getattr(self, 'main_llm', None):
            cfg = self.main_config.copy()
            if "tools" not in cfg:
                cfg["tools"] = []

            if "extra_params" not in cfg:
                cfg["extra_params"] = {}
            cfg["extra_params"]["timeout"] = 600.0
            cfg["timeout"] = 600.0
            self.main_llm = OpenAICompatibleLLM(cfg)

        if self.requires_translation and self.trans_config and not getattr(self, 'trans_llm', None):
            self.trans_llm = OpenAICompatibleLLM(self.trans_config)

    def _emit_token(self, token: str):
        self.update_progress(-1, token)

    def _emit_error(self, msg: str):
        raise RuntimeError(msg)

    def _emit_translated(self, text: str):
        self._emit_state(TaskState.PROCESSING, -1, "", payload={"event": "translated", "text": text})

    def _execute(self):
        self.send_log("INFO", f"Chat task started. KB_ID: {self.kwargs.get('kb_id')}")
        time.sleep(0.1)

        self.main_config = self.kwargs.get('main_config')
        self.trans_config = self.kwargs.get('trans_config')
        self.messages = self.kwargs.get('messages', [])
        self.kb_id = self.kwargs.get('kb_id')
        if self.kb_id == "none": self.kb_id = None
        self.requires_translation = self.kwargs.get('requires_translation', False)
        self.external_context = self.kwargs.get('external_context', [])
        self.use_academic_agent = self.kwargs.get('use_academic_agent', True)
        self.academic_tags = self.kwargs.get('academic_tags', [])

        self.use_external_tools = self.kwargs.get('use_external_tools', False)
        self.external_tool_names = self.kwargs.get('external_tool_names',
                                                   [])

        self.db = DatabaseManager()
        self.kb_manager = KBManager()
        self.config = ConfigManager()
        self.full_response_cache = ""

        self.main_llm = None
        self.trans_llm = None
        self.vision_llm = None

        self._init_llms()
        original_user_query = self.messages[-1].get('display_text', self.messages[-1].get('content', ''))
        search_query = original_user_query
        domain = "General Academic"
        context_str = ""
        sources_map = {}

        # Phase 1: Query Extraction & Translation (Cache Accelerated)
        if self.requires_translation:
            self.send_log("INFO", f"Translating query: {original_user_query[:20]}...")
            self._emit_token(
                "<div class='status-msg' style='color:#05B8CC; margin-bottom:4px;'>🌐 Translating your query to academic English for precise retrieval...</div>\n\n")
            try:
                trans_kwargs = {
                    "is_translation": True,
                    "stream": False
                }
                search_query = get_cached_translation(original_user_query, "to_en", self.trans_llm, **trans_kwargs)
                self._emit_translated(search_query)
            except Exception as e:
                self._emit_error(f"Translation model request failed. Details: {e}")

        # Phase 2: Vector Retrieval & Reranking (Local KB)
        if self.kb_id:
            self.send_log("INFO", "Initiating local Vector RAG retrieval...")
            self._emit_token("[CLEAR_SEARCH]")
            self._emit_token(
                "<div class='status-msg' style='color:#05B8CC; margin-bottom:4px;'>📚 Searching local knowledge base and reranking documents...</div>\n\n")
            time.sleep(0.05)

            kb_info = self.kb_manager.get_kb_by_id(self.kb_id)
            if kb_info and kb_info.get('doc_count', 0) == 0:
                self.logger.warning(f"Knowledge Base '{kb_info.get('name')}' is empty. Skipping vector retrieval.")
            elif kb_info:
                self._emit_token(
                    "<div class='status-msg' style='color:#05B8CC; margin-bottom:4px;'>Loading local vector model and retrieving literature...</div>\n\n")
                domain = kb_info.get('domain', 'General Academic')
                model_id = kb_info.get('model_id', 'embed_auto')

                user_pref = self.config.user_settings.get("inference_device", "Auto")
                target_device = DeviceManager().parse_device_string(user_pref)

                conf = get_model_conf(model_id, "embedding")
                if not conf or conf.get('is_auto'):
                    from src.task.kb_tasks import _worker_load_model
                    real_id = resolve_auto_model("embedding", target_device)
                    conf = get_model_conf(real_id, "embedding")

                try:
                    from src.task.kb_tasks import _worker_load_model
                    embed_fn = _worker_load_model(self.kb_id, self.config)
                    if not self.db.switch_kb(self.kb_id, embedding_function=embed_fn):
                        self._emit_error(f"Failed to switch to Knowledge Base: {self.kb_id}")
                except Exception as e:
                    self._emit_error(f"Critical Model Error: {str(e)}")

                history_context = ""
                if len(self.messages) >= 3:
                    prev_assistant = self.messages[-2]['content'][:100]
                    history_context = f" (Context: {prev_assistant})"

                expanded_queries = [
                    search_query,
                    f"{search_query}{history_context}",
                    f"{domain} context: {search_query} research details"
                ]

                candidate_docs = []
                seen_contents = set()

                for eq in expanded_queries:
                    raw_results = self.db.query(eq, n_results=20)
                    if raw_results and raw_results.get('documents') and raw_results['documents'][0]:
                        docs = raw_results['documents'][0]
                        metas = raw_results['metadatas'][0]
                        distances = raw_results.get('distances', [[0] * len(docs)])[0]

                        for i, doc_text in enumerate(docs):
                            clean_text = doc_text.strip()
                            if clean_text not in seen_contents and len(clean_text) > 20:
                                seen_contents.add(clean_text)
                                candidate_docs.append({
                                    "content": clean_text,
                                    "metadata": metas[i],
                                    "v_dist": distances[i]
                                })

                if candidate_docs:
                    candidate_docs = sorted(candidate_docs, key=lambda x: x.get('v_dist', 0))[:40]
                    final_docs = self._process_rerank(search_query, candidate_docs, domain, 10)
                    if final_docs is None:
                        final_docs = candidate_docs[:10]

                    current_ref_id = 1
                    for doc in final_docs:
                        sources_map[current_ref_id] = {
                            "path": doc['metadata'].get('file_path', ''),
                            "page": doc['metadata'].get('page', 1),
                            "name": doc['metadata'].get('source', 'Local DB'),
                            "search_text": doc['content'][:100]
                        }
                        context_str += (
                            f"--- [Document {current_ref_id}] ---\n"
                            f"Source: {doc['metadata'].get('source', 'Local')}\n"
                            f"Content: {doc['content']}\n\n"
                        )
                        current_ref_id += 1

        if not context_str.strip():
            context_str = "No local database documents provided."

        external_chunks = self.external_context or []
        images = [c for c in external_chunks if c.get("type") == "image" or str(c.get("path", "")).lower().endswith(
            ('.png', '.jpg', '.jpeg', '.webp'))]
        docs = [c for c in external_chunks if c not in images]

        llm_content = []

        # Phase 3: External Attachments Integration(文件打分环节)
        if docs:
            self.send_log("INFO", f"Detected {len(docs)} uploaded document chunks. Starting Reranker scoring...")
            self._emit_token(
                "<div class='status-msg' style='color:#05B8CC; margin-bottom:4px;'>Filtering and reranking attached documents...</div>\n\n")
            cand_docs = [{"content": d.get("content", ""),
                          "metadata": {"name": d.get("name", "Unknown"), "page": d.get("page", 1)}} for d in docs]

            if len(cand_docs) > 5:
                reranked_docs = self._process_rerank(search_query, cand_docs, "General", 8)
                if reranked_docs is not None:
                    self.send_log("INFO",
                                  f"Reranker finished: Reduced {len(cand_docs)} chunks to top 8 most relevant segments.")
                    cand_docs = reranked_docs
                else:
                    self.send_log("WARNING", "Reranker failed for files, falling back to top-k selection.")
                    cand_docs = cand_docs[:8]
            else:
                self.send_log("INFO",
                              f"Small attachment size ({len(cand_docs)} chunks), skipping rerank and using all content.")

            files_dict = {}
            for doc in cand_docs:
                f_name = doc["metadata"]["name"]
                page = doc["metadata"]["page"]
                if f_name not in files_dict:
                    files_dict[f_name] = ""
                files_dict[f_name] += f"\n[Page {page}]\n{doc['content']}"

            docs_json = json.dumps(files_dict, ensure_ascii=False)
            llm_content.append({"type": "text", "text": f"User Uploaded Files (JSON Format):\n{docs_json}\n\n"})

        if images:
            vision_model_name = self.main_config.get("vision_model_name", "auto")
            main_model_name = self.main_config.get("model_name", "").lower()

            vision_keywords = ['image', 'vl', 'vision', 'llava', 'pixtral', 'gpt-4o', 'gpt-4-turbo', 'gemini-1.5',
                               'gemini-2.0', 'claude-3', 'qwen-vl']
            main_supports_vision = any(kw in main_model_name for kw in vision_keywords)
            if "deepseek" in main_model_name:
                main_supports_vision = False

            need_pre_caption = False
            active_vision_model = None

            if vision_model_name != "auto":
                need_pre_caption = True
                active_vision_model = vision_model_name
            elif not main_supports_vision:
                need_pre_caption = True
                active_vision_model = main_model_name

            if need_pre_caption:
                self._emit_token(
                    "<div class='status-msg' style='color:#05B8CC; margin-bottom:4px;'>Extracting image contexts via vision model...</div>\n\n")
                try:
                    vision_cfg = self.main_config.copy()
                    vision_cfg["model_name"] = active_vision_model
                    vision_cfg.pop("tools", None)
                    self.vision_llm = OpenAICompatibleLLM(vision_cfg)

                    image_descriptions = []
                    for img in images:
                        if self.is_cancelled(): break

                        img_data = img.get("base64_url") or img.get("content")
                        if not img_data.startswith("data:image"):
                            ext = str(img.get("path", ".jpeg")).split('.')[-1]
                            img_data = f"data:image/{ext};base64,{img_data}"

                        vision_prompt = [{"role": "user", "content": [
                            {"type": "text",
                             "text": "Please deeply analyze this image, extract all text (OCR), describe the charts/data, and detail its core contents. Output in pure text."},
                            {"type": "image_url", "image_url": {"url": img_data}}
                        ]}]

                        desc_res = self.vision_llm.chat(vision_prompt)
                        desc_content = desc_res.get('content', '') if isinstance(desc_res, dict) else str(desc_res)

                        image_descriptions.append(
                            f"[Image: {img.get('name', 'Unknown')}] Description:\n{desc_content}")

                    if image_descriptions:
                        llm_content.append({"type": "text",
                                            "text": "The user uploaded images. Here are their detailed textual descriptions analyzed by the vision model:\n" + "\n".join(
                                                image_descriptions)})
                except Exception as e:
                    self.logger.warning(f"Vision pre-captioning failed: {e}")
                    self._emit_token(
                        "<div style='color:#e6a23c;'>⚠️ Image parsing failed. The current model configuration might not support vision. Images will be ignored.</div><br>")
                    llm_content.append({"type": "text",
                                        "text": f"[System Warning: User uploaded an image, but the vision parser failed to read it.]"})
                finally:
                    self.vision_llm = None
            else:
                self.logger.info(f"Mounting images natively for vision-capable main model: [{main_model_name}]")
                for img in images:
                    img_data = img.get("base64_url") or img.get("content")
                    if img_data:
                        if not img_data.startswith("data:image"):
                            ext = str(img.get("path", ".jpeg")).split('.')[-1]
                            img_data = f"data:image/{ext};base64,{img_data}"
                        llm_content.append({
                            "type": "image_url",
                            "image_url": {"url": img_data}
                        })

        llm_content.append({"type": "text", "text": f"User Query:\n{search_query}"})
        self._emit_token("[CLEAR_SEARCH]")

        # Phase 5: Agentic Generation
        self._emit_token("[START_LLM_NETWORK]")

        mcp_mgr = MCPManager.get_instance()
        from src.core.skill_manager import SkillManager
        skill_mgr = SkillManager.get_instance()

        combined_tools = []
        raw_tools = []
        dynamic_tool_prompt = ""

        # 1. 内部学术 Agent
        if self.use_academic_agent:
            raw_academic = skill_mgr.get_academic_schemas(self.academic_tags)
            if raw_academic:
                raw_tools.extend(raw_academic)

        # 2. 外部工具组合
        if self.use_external_tools:
            # 2.1 提取外部 SKILL
            ext_skills = skill_mgr.get_external_schemas(self.external_tool_names)
            if ext_skills:
                raw_tools.extend(ext_skills)

            for schema in mcp_mgr.tool_schemas.values():
                server_name = schema.get("server", "Unknown Server")
                if not self.external_tool_names or server_name in self.external_tool_names:
                    clean_schema = {
                        "type": schema.get("type", "function"),
                        "function": schema.get("function", {})
                    }
                    raw_tools.append(clean_schema)

        if raw_tools:
            self.send_log("INFO",
                          f"Enabled tool pool contains {len(raw_tools)} candidate tools (MCP + Skills). Starting intent-based filtering...")
            self._emit_token(
                "<div class='status-msg' style='color:#05B8CC; margin-bottom:4px;'>Filtering optimal tools based on query intent...</div>\n\n")
            time.sleep(0.05)
            combined_tools = self.filter_tools_by_rag(search_query, raw_tools, top_k=8)
            self._emit_token("[CLEAR_SEARCH]")

            final_tool_names = [t.get("function", {}).get("name", "Unknown") for t in combined_tools]
            self.send_log("INFO", f"Final selected tools for LLM generation: {', '.join(final_tool_names)}")

        combined_tools.append({
            "type": "function",
            "function": {
                "name": "generate_image",
                "description": "Generates an image based on a text prompt. Use this tool ONLY when the user explicitly asks to draw, create, or generate a picture/image.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "A highly detailed English prompt describing the image to be generated."
                        }
                    },
                    "required": ["prompt"]
                }
            }
        })

        if combined_tools:
            self._emit_token("<mcp_process>⚙️ Analyzing query intent and filtering optimal MCP tools...</mcp_process>")
            tool_names = [t.get("function", {}).get("name", "Unknown") for t in combined_tools]
            dynamic_tool_prompt = (
                f"### CRITICAL TOOL UTILIZATION RULE:\n"
                f"The Reranker engine has exclusively selected the following tools for this specific query: {', '.join(tool_names)}.\n"
                f"You MUST read the user's prompt carefully. If the user asks for multi-dimensional data (e.g., metadata AND protein interactions), you MUST use multiple tools to fulfill ALL parts of the request. DO NOT skip required tools. DO NOT answer partially.\n\n"
            )

        system_prompt = (
            f"You are a Senior Research Scientist specializing in {domain}. "
            "Your goal is to provide high-density, evidence-based academic responses.\n\n"
            f"{dynamic_tool_prompt}\n\n"
            "### TOOL USE PROTOCOL (STRICT):\n"
            "1. CRITICAL FOR CITATIONS: If the user's prompt asks for literature, references, citations, or a review, you MUST explicitly invoke academic search tools (like search_academic_literature) BEFORE generating your response. NEVER rely on your internal training data to generate citations, DOIs, or author lists.\n"
            "2. If the provided Context is insufficient, invoke tools IMMEDIATELY.\n"
            "3. SILENT EXECUTION: Never output your reasoning process for choosing a tool. YOU MUST USE THE NATIVE TOOL CALLING API FORMAT.\n"
            "4. FALLBACK TOOL CALLING (CRITICAL FOR REASONING MODELS): If your native function calling API is disabled (e.g., DeepSeek-R1), you MUST invoke tools manually by outputting exactly this XML block in your response text: <｜DSML｜invoke name=\"tool_name\"><｜DSML｜parameter name=\"arg_name\">value</｜DSML｜parameter></｜DSML｜invoke>\n"
            "5. CROSS-DOMAIN FLEXIBILITY (CRITICAL): If the user's request matches the capability of ANY available tool (e.g., checking train tickets, weather, web search), you MUST use that tool to assist them, EVEN IF the request is not related to academic research.\n"
            "6. If graphics need to be created, use mermaid uniformly.\n\n"
            "### RESPONSE GUIDELINES & CITATION PROTOCOL:\n"
            "1. IN-TEXT GROUNDING (For UI Tracking): You MUST use bracketed numbers (e.g., [1], [101]) immediately after a claim to cite the Context or Tool Results. This automatically generates a UI 'Cited Sources' block. NEVER claim facts without these bracketed numbers.\n"
            "2. FORMAL BIBLIOGRAPHY (For the User): If the user explicitly requests 'references', 'citations', or a 'review', you MUST ALSO generate a standalone 'References' section at the very end of your main text (but BEFORE the [FOLLOW_UPS] section). \n"
            "3. STRICT FORMATTING: The standalone 'References' section must strictly follow academic formatting (e.g., APA/Nature style: Authors. (Year). Title. Journal. DOI). DO NOT include conversational fluff like 'Cited for the role of...' in this formal list. List purely the bibliographic data.\n\n"
            "4. ZERO HALLUCINATION (CRITICAL): You MUST NOT fabricate, extrapolate, or infer information that is not explicitly present in the provided Context or Tool Results. If the provided data is insufficient to address the query, you MUST explicitly state: 'The provided context does not contain sufficient information to address this inquiry.' Under no circumstances should internal training data be utilized to circumvent contextual gaps.\n\n"
            "### FOLLOW-UP STRUCTURE (MANDATORY):\n"
            "At the very end of your response, you MUST output the exact string [FOLLOW_UPS] followed by exactly 6 follow-up questions using this EXACT format:\n"
            "[FOLLOW_UPS]\n"
            "💡 Suggested Follow-ups:\n"
            "   - [Deep Dive] <Question about specific details or mechanisms>\n"
            "   - [Critical] <Question about limitations, alternatives, or weaknesses>\n"
            "   - [Broader] <Question about implications or future trends>\n"
            "   - [Brainstorm] <A creative brainstorming question or hypothetical \"What if\" scenario>\n"
            "   - [Similar] <Question connecting to a similar or parallel topic/concept>\n"
            "   - [Application] <Question about real-world applications or cross-disciplinary use>\n\n"
            f"### CONTEXT:\n{context_str}"
        )

        clean_history = [{"role": m["role"], "content": m["content"], "tool_calls": m.get("tool_calls"),
                          "tool_call_id": m.get("tool_call_id"), "name": m.get("name")} for m in self.messages[:-1] if
                         "role" in m and "content" in m]
        rag_messages = [{"role": "system", "content": system_prompt}] + clean_history
        rag_messages.append({"role": "user", "content": llm_content})

        tool_executed = False
        final_response_obtained = False

        if combined_tools:
            try:
                MAX_ITERATIONS = 12
                for iteration in range(MAX_ITERATIONS):
                    if self.is_cancelled():
                        self._emit_token("\n\n[⛔ Generation halted by user.]")
                        break

                    response_msg = self.main_llm.chat(
                        messages=rag_messages,
                        tools=combined_tools,
                        tool_choice="auto"
                    )

                    tool_calls = response_msg.get('tool_calls') if isinstance(response_msg, dict) else None
                    content = response_msg.get("content", "") if isinstance(response_msg, dict) else ""
                    reasoning = response_msg.get("reasoning_content", "") if isinstance(response_msg, dict) else ""

                    if not tool_calls and "<｜DSML｜function_calls>" in content:
                        dsml_matches = re.findall(r'<｜DSML｜invoke name=["\'](.*?)["\'](?:>(.*?)</｜DSML｜invoke>| />)',
                                                  content, re.DOTALL)
                        if dsml_matches:
                            tool_calls = []
                            for m_name, m_args_raw in dsml_matches:
                                arg_dict = {}
                                if m_args_raw:
                                    p_matches = re.findall(
                                        r'<｜DSML｜parameter name=["\'](.*?)["\'][^>]*>(.*?)</｜DSML｜parameter>',
                                        m_args_raw, re.DOTALL)
                                    for p_name, p_val in p_matches:
                                        p_val = p_val.strip()
                                        if p_val.lower() == "true":
                                            p_val = True
                                        elif p_val.lower() == "false":
                                            p_val = False
                                        arg_dict[p_name] = p_val

                                tool_calls.append({
                                    "id": f"call_{uuid.uuid4().hex[:12]}",
                                    "type": "function",
                                    "function": {"name": m_name, "arguments": json.dumps(arg_dict, ensure_ascii=False)}
                                })
                            content = re.sub(r'<｜DSML｜function_calls>.*?(?:</｜DSML｜function_calls>|$)', '', content,
                                             flags=re.DOTALL).strip()

                    if reasoning:
                        self._emit_token(f"<think>\n{reasoning}\n</think>\n\n")

                    if not tool_calls and "<｜DSML｜invoke" in content:
                        dsml_matches = re.findall(r'<｜DSML｜invoke name=["\'](.*?)["\'](?:>(.*?)</｜DSML｜invoke>| />)',
                                                  content, re.DOTALL)
                        if dsml_matches:
                            tool_calls = []
                            for m_name, m_args_raw in dsml_matches:
                                arg_dict = {}
                                if m_args_raw:
                                    p_matches = re.findall(
                                        r'<｜DSML｜parameter name=["\'](.*?)["\'][^>]*>(.*?)</｜DSML｜parameter>',
                                        m_args_raw, re.DOTALL)
                                    for p_name, p_val in p_matches:
                                        p_val = p_val.strip()
                                        if p_val.lower() == "true":
                                            p_val = True
                                        elif p_val.lower() == "false":
                                            p_val = False
                                        arg_dict[p_name] = p_val

                                tool_calls.append({
                                    "id": f"call_{uuid.uuid4().hex[:12]}",
                                    "type": "function",
                                    "function": {"name": m_name, "arguments": json.dumps(arg_dict, ensure_ascii=False)}
                                })
                            content = re.sub(r'<｜DSML｜invoke.*?(?:</｜DSML｜invoke>|$)', '', content,
                                             flags=re.DOTALL).strip()

                    if not tool_calls and "```json" in content:
                        json_blocks = re.findall(r'```json\s*\n(.*?)\n\s*```', content, re.DOTALL)
                        for jb in json_blocks:
                            try:
                                j_data = json.loads(jb)
                                if isinstance(j_data, dict) and "name" in j_data and "arguments" in j_data:
                                    tool_calls = [{
                                        "id": f"call_{uuid.uuid4().hex[:12]}",
                                        "type": "function",
                                        "function": {
                                            "name": j_data["name"],
                                            "arguments": json.dumps(j_data["arguments"],
                                                                    ensure_ascii=False) if isinstance(
                                                j_data["arguments"], dict) else j_data["arguments"]
                                        }
                                    }]
                                    content = content.replace(f"```json\n{jb}\n```", "").strip()
                                    break
                            except:
                                pass

                    if not tool_calls:
                        if content:
                            self._emit_token("[CLEAR_SEARCH]")
                            chunk_size = 5
                            for i in range(0, len(content), chunk_size):
                                if self.is_cancelled(): break
                                chunk = content[i:i + chunk_size]
                                self.full_response_cache += chunk
                                self._emit_token(chunk)
                                time.sleep(0.015)
                            final_response_obtained = True
                        break

                    tool_executed = True
                    assistant_msg = {"role": "assistant", "content": content or "", "tool_calls": tool_calls}
                    if reasoning: assistant_msg["reasoning_content"] = reasoning
                    rag_messages.append(assistant_msg)

                    for tool_call in tool_calls:
                        if self.is_cancelled(): break
                        tool_name = tool_call['function']['name']
                        try:
                            raw_args = tool_call['function']['arguments']
                            tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args

                            if tool_name == "generate_image":
                                prompt_text = tool_args.get("prompt", "")
                                self._emit_token(
                                    f"<mcp_process>🎨 Generating image (Prompt: {prompt_text[:30]}...)</mcp_process>\n")
                                try:
                                    img_url = self.main_llm.generate_image(prompt=prompt_text)
                                    self._emit_token(
                                        f"<br><img src='{img_url}' style='max-width: 100%; border-radius: 8px; border: 1px solid #444;' alt='Generated Image'/><br>\n\n")
                                    tool_result = f"Image generated successfully. URL: {img_url}"
                                except Exception as img_e:
                                    self.logger.error(f"Internal Image Tool failed: {img_e}")
                                    tool_result = f"Image generation failed: {str(img_e)}"





                            elif skill_mgr.is_skill_available(tool_name):
                                tool_args_str = json.dumps(tool_args, ensure_ascii=False)
                                short_args = tool_args_str if len(tool_args_str) < 120 else tool_args_str[:120] + "..."

                                if hasattr(skill_mgr, 'academic_skills') and tool_name in skill_mgr.academic_skills:
                                    prefix = "[ACADEMIC]"
                                    self.send_log("INFO", f"{prefix} Executing internal skill: {tool_name}")

                                else:
                                    prefix = "[SKILL]"
                                    self.send_log("INFO", f"{prefix} Executing external skill script: {tool_name}")
                                self._emit_token(
                                    f"<mcp_process><b>{prefix} {tool_name}</b><br>"
                                    f"<span style='font-size:12px; color:#888;'>[Status: Executing] Args: {short_args}</span></mcp_process>\n"
                                )

                                tool_result = skill_mgr.call_skill(tool_name, tool_args)

                            elif self.use_external_tools and mcp_mgr.is_tool_available(tool_name):
                                tool_args_str = json.dumps(tool_args, ensure_ascii=False)
                                short_args = tool_args_str if len(tool_args_str) < 120 else tool_args_str[:120] + "..."
                                prefix = "[MCP]"
                                self.send_log("INFO", f"{prefix} Requesting external MCP service: {tool_name}")
                                self._emit_token(
                                    f"<mcp_process><b>{prefix} {tool_name}</b><br>"
                                    f"<span style='font-size:12px; color:#888;'>[Status: Executing] Args: {short_args}</span></mcp_process>\n"
                                )

                                tool_result = mcp_mgr.call_tool_sync(tool_name, tool_args)
                                try:
                                    res_data = json.loads(tool_result)
                                    if isinstance(res_data, dict) and "results" in res_data:
                                        for item in res_data["results"]:
                                            source_url = item.get("url") or item.get("pdf_url") or item.get(
                                                "landing_page_url")
                                            source_title = item.get("title") or item.get("name") or item.get(
                                                "pref_name") or item.get("display_name") or item.get(
                                                "scientific_name") or f"Result from {tool_name}"
                                            if source_url:
                                                mcp_ref_id = len(sources_map) + 101
                                                sources_map[mcp_ref_id] = {
                                                    "path": source_url, "page": 1, "name": f"[Online] {source_title}",
                                                    "search_text": item.get("abstract", "")[:100]
                                                }
                                                item["_mcp_cite_id"] = mcp_ref_id
                                        tool_result = json.dumps(res_data, ensure_ascii=False)
                                except:
                                    pass

                        except Exception as e:
                            self.logger.error(f"MCP tool {tool_name} failed: {e}")
                            tool_result = f"Tool execution failed: {str(e)}"

                        if not isinstance(tool_result, str):
                            tool_result = json.dumps(tool_result, ensure_ascii=False)

                        if "API Key" in tool_result or "error" in tool_result.lower() or "missing" in tool_result.lower():
                            tool_result = (
                                f"[TOOL EXECUTION FAILED] The tool '{tool_name}' returned an error:\n\"{tool_result}\"\n\n"
                                f"INSTRUCTION TO AI:\n1. Explain to the user EXACTLY why the access failed."
                            )

                        rag_messages.append({"role": "tool", "tool_call_id": tool_call['id'], "name": tool_name,
                                             "content": tool_result})
            except Exception as e:
                self.logger.warning(f"Tool calling loop failed: {e}")

        if not final_response_obtained:
            if tool_executed:
                silence_prompt = "\n\n[System Notification: Tool execution limit reached. Please analyze the tool results above and answer the user's original query.\nFINAL OUTPUT RULE: YOU MUST NOT INVOKE ANY MORE TOOLS. Output your final response directly in plain Markdown.]"
                if rag_messages and rag_messages[-1]["role"] == "tool":
                    rag_messages[-1]["content"] += silence_prompt
                else:
                    rag_messages.append({"role": "user", "content": silence_prompt})

            self._emit_token("[CLEAR_SEARCH]")
            self._emit_token("[START_LLM_NETWORK]")

            stream_kwargs = {}
            if combined_tools:
                stream_kwargs["tools"] = combined_tools

            # --- 新增：截获底层流式错误的逻辑 ---
            error_buffer = ""
            for token in self.main_llm.stream_chat(rag_messages, **stream_kwargs):
                if self.is_cancelled():
                    break

                # 检查是否是底层异常前缀
                if error_buffer or "[API Request Error" in token or "[System Error" in token or "[Context Exceeded Error]" in token or "[Rate Limit Error]" in token or "[Timeout Error]" in token:
                    error_buffer += token
                    continue

                self.full_response_cache += token
                self._emit_token(token)

            if error_buffer:
                prefix_match = re.match(r'^\s*\[(.*?)\]\s*\n*(.*)', error_buffer, re.DOTALL)
                if prefix_match:
                    title = prefix_match.group(1).strip()
                    body = prefix_match.group(2).strip()
                    self._emit_error(json.dumps({"title": title, "body": body}))
                else:
                    self._emit_error(json.dumps({"title": "Provider Error", "body": error_buffer.strip()}))
                return

        # Phase 6: Dynamic Citation Mounting
        has_citation = bool(re.search(r'\[\d+\]', self.full_response_cache))
        if sources_map and has_citation:
            ref_html = "\n<br><hr style='border:0; height:1px; background:#444; margin:15px 0;'><b>📚 Cited Sources:</b><br>"
            used_indices = set(int(ref) for ref in re.findall(r'\[(\d+)\]', self.full_response_cache))
            displayed = 0
            for rid, info in sources_map.items():
                if rid in used_indices:
                    from urllib.parse import quote
                    safe_path, safe_text, safe_name = quote(info['path']), quote(info['search_text']), quote(
                        info['name'])
                    link = f"cite://view?path={safe_path}&page={info['page']}&text={safe_text}&name={safe_name}"
                    ref_html += f"<div style='margin-bottom: 5px;'>▪ <a style='color:#05B8CC; text-decoration:none;' href='{link}'><b>[{rid}]</b> {info['name']}</a></div>"
                    displayed += 1
            if displayed > 0:
                self._emit_token(ref_html)

        return self.full_response_cache

    def filter_tools_by_rag(self, user_query, candidate_tools, top_k=8):
        if not candidate_tools or len(candidate_tools) <= top_k:
            self.send_log("INFO", f"Skipping Reranker: {len(candidate_tools)} candidate tools is <= top_k ({top_k}).")
            return candidate_tools

        try:
            candidate_docs = []
            for tool in candidate_tools:
                func = tool.get("function", {})
                content = f"Tool Name: {func.get('name', '')}. Description: {func.get('description', '')}"
                candidate_docs.append({"content": content, "metadata": {"tool_schema": tool}})

            history_context = f" Previous Context: {self.messages[-2].get('content', '')[:200]}" if len(
                self.messages) >= 2 else ""
            rerank_query = f"User Intent: {user_query}.{history_context} Find the most appropriate API tools to fulfill this request."

            self.send_log("DEBUG", f"Reranker Query constructed: {rerank_query}")
            self.send_log("DEBUG", f"Sending {len(candidate_tools)} tools to Reranker engine for scoring...")

            ranked_docs = self._process_rerank(rerank_query, candidate_docs, domain="Tool Selection", top_k=len(candidate_docs), emit_warning=False)

            if ranked_docs is None:
                self.send_log("WARNING", "Reranker returned None. Bypassing tool filtering.")
                return candidate_tools

            log_lines = ["\n[--- Reranker Tool Scoring Report ---]"]
            for idx, doc in enumerate(ranked_docs):
                tool_name = doc["metadata"]["tool_schema"]["function"]["name"]
                score = doc.get("score", 0.0)
                status = "✅ [SELECTED]" if idx < top_k else "❌ [REJECTED]"
                log_lines.append(f"{idx+1:02d}. {status} Score: {score:.4f} | Tool: {tool_name}")

                if idx >= top_k and any(kw in tool_name.lower() for kw in ['search', 'literature', 'pubmed', 'crossref']):
                    self.send_log("WARNING", f"Critical tool '{tool_name}' was rejected with score ({score:.4f}). Check if its Schema Description matches the prompt intent.")

            log_lines.append("[------------------------------------]\n")
            self.send_log("INFO", "\n".join(log_lines))

            top_ranked_docs = ranked_docs[:top_k]
            selected_names = [doc["metadata"]["tool_schema"]["function"]["name"] for doc in top_ranked_docs]

            self.send_log("INFO", f"Reranker scoring complete. Final Top {len(selected_names)} tools: {', '.join(selected_names)}")

            return [doc["metadata"]["tool_schema"] for doc in top_ranked_docs]

        except Exception as e:
            self.send_log("ERROR", f"Exception during tool reranking: {str(e)}")
            return candidate_tools

    def _process_rerank(self, query, docs, domain, top_k, emit_warning=True):
        if not docs: return []
        time.sleep(0.05)

        from src.core.core_task import RunnerProcess
        from src.task.kb_tasks import RerankTask

        queue = mp.Queue()
        worker = RunnerProcess(
            RerankTask, "rerank_sync", queue,
            {"query": query, "docs": docs, "domain": domain, "top_k": top_k}
        )
        worker.start()

        ranked = None
        while True:
            if self.is_cancelled():
                worker.terminate()
                worker.join(timeout=0.5)
                queue.close()
                queue.cancel_join_thread()
                break
            try:
                data = queue.get(timeout=0.2)
                state = data.get("state")
                if state == TaskState.SUCCESS.value:
                    ranked = data["payload"] if data.get("payload") else docs[:top_k]
                    break
                elif state == TaskState.FAILED.value:
                    if emit_warning:
                        self._emit_token(
                            f"<br><div style='color:#e6a23c; font-size:13px;'>⚠️ <b>Reranker Processing Skipped</b><br>Failed to rerank. Defaulting.</div><br>")
                    break
            except q.Empty:
                if not worker.is_alive(): break
            except Exception:
                if not worker.is_alive(): break
        return ranked


class DownloadImageTask(BackgroundTask):
    """
    异步图片下载任务。
    负责从远程 URL 获取图像数据并将其持久化至本地临时目录。
    """

    def _execute(self):
        url = self.kwargs.get("url")
        save_path = self.kwargs.get("save_path")

        if not url or not save_path:
            return {"success": False, "url": url, "path": save_path, "msg": "Invalid parameters"}

        try:
            from src.core.config_manager import ConfigManager
            proxy_url = ConfigManager().user_settings.get("proxy_url", "").strip()

            httpx_kwargs = {"timeout": 30.0, "follow_redirects": True}
            if proxy_url:
                httpx_kwargs["proxy"] = proxy_url
            else:
                httpx_kwargs["trust_env"] = False

            if self.is_cancelled():
                raise InterruptedError("Image download safely terminated by user.")

            import httpx
            with httpx.Client(**httpx_kwargs) as client:
                response = client.get(url)
                response.raise_for_status()

                with open(save_path, "wb") as f:
                    f.write(response.content)

            return {"success": True, "url": url, "path": save_path}

        except Exception as e:
            self.send_log("ERROR", f"Image download failed for {url}: {str(e)}")
            return {"success": False, "url": url, "path": save_path, "msg": str(e)}