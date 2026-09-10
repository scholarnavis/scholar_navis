import json
import os
import re
import time
import uuid
from src.core.config_manager import ConfigManager
from src.core.core_task import BackgroundTask, TaskState
from src.core.device_manager import DeviceManager
from src.core.image_utils import IMAGE_EXTENSIONS, encode_data_url
from src.core.kb_manager import KBManager, DatabaseManager
from src.core.llm_errors import friendly_payload, strip_markers
from src.core.mcp_manager import MCPManager
from src.core.models_registry import get_model_conf, resolve_auto_model
from src.core.token_estimator import (estimate_message_tokens, estimate_tokens,
                                      resolve_context_window, derive_context_budgets)


# KB 检索注入治理：单 chunk 字符上限固定；总 token 预算按主模型上下文
# 窗口动态推导（derive_context_budgets，litellm 模型元数据优先），使不同
# 模型各按自身容量注入。被省略的文档仍写入 sources_map，引用可点开原文。
_KB_CHUNK_MAX_CHARS = 1600

# Human-in-the-loop 深度研究计划协议：确认卡（<deep_plan> 标记）由 UI 渲染，
# 用户确认/跳过后以哨兵前缀文本重新进入发送管线；任务端据此跳过重复分解
# 或关闭本轮 deep 模式。
_DEEP_PLAN_CONFIRMED_TAG = "[DEEP_PLAN_CONFIRMED]"
_DEEP_PLAN_SKIPPED_TAG = "[DEEP_PLAN_SKIPPED]"


def _kb_retrieval_core(kb_id, search_query, main_model_name, history_context=""):
    """本地 KB 检索唯一权威实现：embedding 加载 -> 三变体向量检索 -> 交叉编码器
    重排 -> 按主模型上下文预算裁剪 -> 生成 context_str 与 sources_map(整型 id)。

    可在 GUI 进程内联执行，也可被 _kb_retrieval_worker 在独立子进程中调用（隔离
    本地 ONNX 推理）。返回 (context_str, sources_map, domain)。KB 为空或检索为空
    时 context_str/sources_map 均为空，由调用方决定默认文案。
    """
    from src.task.kb_tasks import _worker_load_model
    from src.core.kb_manager import KBManager, DatabaseManager
    from src.core.config_manager import ConfigManager
    from src.core.token_estimator import estimate_tokens, resolve_context_window, derive_context_budgets
    from src.core.rerank_engine import RerankEngine

    cfg = ConfigManager()
    kb_mgr = KBManager()
    db = DatabaseManager()

    kb_info = kb_mgr.get_kb_by_id(kb_id)
    if not kb_info or kb_info.get('doc_count', 0) == 0:
        return "", {}, "General Academic"

    domain = kb_info.get('domain', 'General Academic')
    embed_fn = _worker_load_model(kb_id, cfg)
    if not db.switch_kb(kb_id, embedding_function=embed_fn):
        raise RuntimeError(f"Failed to switch to Knowledge Base: {kb_id}")

    # 三变体查询（严格零召回损失）：变体 3（domain 前缀）不可省略——单次查询无论
    # 召回量多大都无法覆盖不同查询文本的召回集；DatabaseManager.query 内部
    # _db_lock 串行化（含嵌入计算），并行查询无收益，故维持顺序执行。
    expanded_queries = [
        search_query,
        f"{search_query}{history_context}",
        f"{domain} context: {search_query} research details"
    ]

    candidate_docs = []
    seen_contents = set()
    for eq in expanded_queries:
        raw_results = db.query(eq, n_results=20)
        if raw_results and raw_results.get('documents') and raw_results['documents'][0]:
            docs = raw_results['documents'][0]
            metas = raw_results['metadatas'][0]
            distances = raw_results.get('distances', [[0] * len(docs)])[0]
            for i, doc_text in enumerate(docs):
                clean_text = doc_text.strip()
                if clean_text not in seen_contents and len(clean_text) > 20:
                    seen_contents.add(clean_text)
                    candidate_docs.append({
                        "content": clean_text, "metadata": metas[i], "v_dist": distances[i]})
    if not candidate_docs:
        return "", {}, domain

    candidate_docs = sorted(candidate_docs, key=lambda x: x.get('v_dist', 0))[:40]
    try:
        engine = RerankEngine()
        ranked = engine.rerank(search_query, candidate_docs, domain=domain, top_k=len(candidate_docs))
        final_docs = ranked or candidate_docs[:10]
    except Exception:
        final_docs = candidate_docs[:10]

    _kb_budget = derive_context_budgets(
        resolve_context_window(main_model_name))["kb_token_budget"]
    context_str = ""
    sources_map = {}
    current_ref_id = 1
    kb_tokens = 0
    for doc in final_docs:
        sources_map[current_ref_id] = {
            "path": doc['metadata'].get('file_path', ''),
            "page": doc['metadata'].get('page', 1),
            "name": doc['metadata'].get('source', 'Local DB'),
            "search_text": doc['content'][:100],
        }
        chunk = doc['content']
        if len(chunk) > _KB_CHUNK_MAX_CHARS:
            chunk = chunk[:_KB_CHUNK_MAX_CHARS] + "...[chunk truncated]"
        chunk_tokens = estimate_tokens(chunk)
        if kb_tokens + chunk_tokens > _kb_budget:
            context_str += (
                f"--- [Document {current_ref_id}] ---\n"
                f"Source: {doc['metadata'].get('source', 'Local')}\n"
                f"Content: [Omitted: KB context token budget exceeded; "
                f"the citation entry still links to the source.]\n\n"
            )
        else:
            kb_tokens += chunk_tokens
            context_str += (
                f"--- [Document {current_ref_id}] ---\n"
                f"Source: {doc['metadata'].get('source', 'Local')}\n"
                f"Content: {chunk}\n\n"
            )
        current_ref_id += 1
    return context_str, sources_map, domain


def _kb_retrieval_worker(kb_id, search_query, main_model_name, out_path, history_context=""):
    """独立子进程入口：隔离本地 ONNX 推理。执行权威检索逻辑并把结果写 out_path。
    JSON 的 key 只能是字符串，写入前把整型 sources_map 键转字符串，父进程读回时复原。
    """
    import json
    import logging
    _log = logging.getLogger("KB.SubProc")
    try:
        from src.task.kb_tasks import _setup_worker_env
        _setup_worker_env()
        context_str, sources_map, _domain = _kb_retrieval_core(
            kb_id, search_query, main_model_name, history_context)
        payload = {
            "context_str": context_str,
            "sources_map": {str(k): v for k, v in sources_map.items()},
        }
    except Exception as e:
        _log.error(f"KB subprocess retrieval failed: {e}", exc_info=True)
        payload = {"context_str": "", "sources_map": {}, "error": str(e)}
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass


#: detect_primary_language 返回的语言码 -> prompt 中可读的语言名。
#: 用于告诉主模型"用户原始语言是 X，请用 X 回复"（翻译仅用于英文检索）。
_LANG_EN_NAMES = {
    "zh": "Chinese", "zh-cn": "Chinese", "zh-tw": "Chinese",
    "ja": "Japanese", "ko": "Korean", "fr": "French", "de": "German",
    "es": "Spanish", "ru": "Russian", "it": "Italian", "pt": "Portuguese",
    "ar": "Arabic", "hi": "Hindi", "th": "Thai", "vi": "Vietnamese",
    "tr": "Turkish", "nl": "Dutch", "pl": "Polish", "uk": "Ukrainian",
    "id": "Indonesian", "ms": "Malay", "he": "Hebrew", "sv": "Swedish",
    "unknown": "unknown",
}


class ChatGenerationTask(BackgroundTask):
    """
    Background task for executing local/remote LLM interactions, Vector Retrieval,
    and Multi-Agent tool processing within the Core Task Framework.
    """

    # 首次重排失败弹一次警告，之后静默降级，避免每次问答刷屏
    _rerank_warned = False

    def _reply_lang_instruction(self) -> str:
        """返回告知主模型"用用户原始语言作答"的指令段。

        依赖两点：用户原始语言（reply_lang）始终有效；但 query 是否已被预译为
        英文分两种情形：
        - skip（chat_skip_translation_roundtrip 默认 True）：不再单开翻译往返，
          query 保持用户原文，因此额外提示主模型在调用仅英文检索/工具时自行英文化；
        - 非 skip：query 已被预译为英文用于检索/工具，需声明最终回复仍用原语言。
        英文用户（无需翻译）返回空串，不浪费 token。
        """
        if not getattr(self, "requires_translation", False):
            return ""
        reply_lang = getattr(self, "reply_lang", "English")
        if not reply_lang or reply_lang in ("English", "unknown"):
            return ""
        if getattr(self, "_skip_translation_roundtrip", True):
            return (
                f"### OUTPUT LANGUAGE & TOOL QUERY LANGUAGE (MANDATORY):\n"
                f"The user's question was written in {reply_lang} (it was NOT pre-translated). "
                f"(1) You MUST compose your final answer in {reply_lang} (do NOT reply in English), "
                f"using that language's native punctuation. (2) When you call tools/retrieval that "
                f"only accept English (e.g. academic literature search, NCBI, Semantic Scholar), "
                f"internally translate the user's intent into an accurate English query in your tool "
                f"arguments, while the final prose answer remains in {reply_lang}. Structural protocol "
                f"tokens, in-text citation markers ([1]/[101]) and code blocks remain ASCII unchanged.\n"
            )
        return (
            f"### OUTPUT LANGUAGE (MANDATORY):\n"
            f"The user's query was automatically translated to English for tool "
            f"retrieval, but the user originally wrote in {reply_lang}. You MUST compose "
            f"your final answer in {reply_lang} (do NOT reply in English), and use that "
            f"language's native punctuation. Structural protocol tokens, in-text citation "
            f"markers ([1]/[101]) and code blocks remain ASCII and unchanged.\n"
        )

    def cancel(self):
        super().cancel()
        if hasattr(self, 'main_llm') and self.main_llm: self.main_llm.cancel()
        if hasattr(self, 'trans_llm') and self.trans_llm: self.trans_llm.cancel()
        if hasattr(self, 'vision_llm') and self.vision_llm: self.vision_llm.cancel()

    def _init_llms(self):
        from src.core.llm_impl import OpenAICompatibleLLM
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
        # 走基类攒批通道：普通文本聚合入队，控制标记（[CLEAR_SEARCH] 等）
        # 穿透缓冲立即送达，保证 UI 端整 token 精确匹配路由
        self.stream_token(token)

    def _emit_error(self, msg: str):
        raise RuntimeError(msg)

    def _emit_translated(self, text: str):
        self._emit_state(TaskState.PROCESSING, -1, "", payload={"event": "translated", "text": text})

    def _execute(self):
        from src.core.llm_impl import OpenAICompatibleLLM, get_cached_translation

        self.send_log("INFO", f"Chat task started. KB_ID: {self.kwargs.get('kb_id')}")
        time.sleep(0.1)

        self.main_config = self.kwargs.get('main_config')
        self.trans_config = self.kwargs.get('trans_config')
        self.messages = self.kwargs.get('messages', [])
        self.kb_id = self.kwargs.get('kb_id')
        # 语言检测（下方 ~371 行）之前就需 config 与翻译开关：独立翻译往返默认关闭，
        # 非英文输入交由主模型首轮内化；此处提前初始化以决定回复语言约束是否生效。
        self.config = ConfigManager()
        self._skip_translation_roundtrip = bool(
            self.config.user_settings.get("chat_skip_translation_roundtrip", True))
        if self.kb_id == "none": self.kb_id = None

        current_external_files = self.kwargs.get('external_files', [])
        all_external_files = []

        # 1. 遍历历史获取上下文遗留文件
        for m in self.messages:
            if m.get('external_files'):
                for f in m['external_files']:
                    if f not in all_external_files:
                        all_external_files.append(f)

        # 2. 合并当前上传文件
        for f in current_external_files:
            if f not in all_external_files:
                all_external_files.append(f)

        self.external_context = []

        if all_external_files:
            self.send_log("INFO", f"Loading {len(all_external_files)} attached file(s) into memory context...")
            self._emit_token(
                f"<div class='status-msg' style='color:#05B8CC; margin-bottom:4px;'>📄 Loading {len(all_external_files)} attached file(s) into memory...</div>\n\n")
            time.sleep(0.05)

            import tempfile, hashlib, os
            cache_dir = os.path.join(tempfile.gettempdir(), "scholar_navis_cache")
            os.makedirs(cache_dir, exist_ok=True)

            for info in all_external_files:
                if self.is_cancelled(): break
                path = info.get('path', '')
                f_name = info.get('name', 'Unknown')
                content = info.get('content', None)
                ext = f_name.lower()

                if content is not None:
                    self.external_context.append(
                        {"path": path, "name": f_name, "page": info.get('page', 1), "content": content})
                    continue

                # 图片附件：编码为 data URL 交给视觉管线。
                # SVG 在 UI 附加时已栅格化为 PNG，image_path 指向该 PNG，
                # 因此这里无需任何 Qt/图像库，读取字节即可（子进程安全）。
                if info.get('type') == 'image' or ext.endswith(IMAGE_EXTENSIONS):
                    if ext.endswith('.svg') and not info.get('image_path'):
                        self.send_log("WARNING",
                                      f"SVG '{f_name}' lacks a rasterized PNG payload; skipped for model input.")
                        continue
                    img_src = info.get('image_path') or path
                    try:
                        data_url = encode_data_url(img_src)
                        self.external_context.append({
                            "type": "image", "path": path, "name": f_name, "base64_url": data_url,
                            "image_path": img_src
                        })
                        self.send_log("INFO", f"Image attachment ready for model input: {f_name}")
                    except (OSError, ValueError) as e:
                        self.send_log("ERROR", f"Failed to encode image '{f_name}': {e}")
                    continue

                if os.path.exists(path):
                    file_stat = os.stat(path)
                    hash_key = hashlib.md5(f"{path}_{file_stat.st_mtime}_{file_stat.st_size}".encode()).hexdigest()
                    cache_file = os.path.join(cache_dir, f"{hash_key}.json")

                    # 击中缓存，直接加载，实现秒进
                    if os.path.exists(cache_file):
                        try:
                            with open(cache_file, 'r', encoding='utf-8') as cf:
                                cached_data = json.load(cf)
                            self.external_context.extend(cached_data)
                            continue
                        except (OSError, ValueError):
                            pass

                    try:
                        chunks = []
                        if ext.endswith('.pdf'):
                            import pymupdf4llm
                            md_chunks = pymupdf4llm.to_markdown(path, page_chunks=True)
                            for chunk in md_chunks:
                                text = chunk.get("text", "").strip()
                                if len(text) > 10:
                                    chunks.append({
                                        "path": path, "name": f_name, "page": chunk.get("metadata", {}).get("page", 1),
                                        "content": text
                                    })
                        elif ext.endswith('.docx'):
                            import docx
                            doc = docx.Document(path)
                            text = "\n".join([paragraph.text for paragraph in doc.paragraphs if paragraph.text.strip()])
                            if len(text) > 10:
                                chunks.append({"path": path, "name": f_name, "page": 1, "content": text})
                        elif ext.endswith('.doc'):
                            self.send_log("WARNING", f"Legacy .doc format skipped: {f_name}")
                        else:
                            import chardet
                            with open(path, 'rb') as f:
                                raw_data = f.read()
                                detected = chardet.detect(raw_data)
                                encoding = detected['encoding'] if detected['encoding'] else 'utf-8'
                                text = raw_data.decode(encoding, errors='replace').strip()
                            if text:
                                chunks.append({"path": path, "name": f_name, "page": 1, "content": text})

                        if chunks:
                            self.external_context.extend(chunks)
                            with open(cache_file, 'w', encoding='utf-8') as cf:
                                json.dump(chunks, cf, ensure_ascii=False)
                    except Exception as e:
                        self.send_log("ERROR", f"Failed to parse {f_name}: {e}")

            self._emit_token("[CLEAR_SEARCH]")

        if self.kb_id:
            from src.core.models_registry import ModelManager
            ready, missing_label, missing_id, m_type = ModelManager().verify_chat_models(self.kb_id)
            if not ready:
                self._emit_error(json.dumps({
                    "title": "Model Missing - Action Blocked",
                    "body": f"Required offline model is not installed:\n• {missing_label}\n\nPlease go to [Global Settings] and click 'Save' to download required models."
                }))
                return

        original_user_query = self.messages[-1].get('display_text', self.messages[-1].get('content', ''))

        try:
            from src.core.lang_detect import detect_primary_language
            # 语言探测不能用协议哨兵文本（deep-plan 确认轮的消息是英文子任务
            # 列表，会把回复语言误判为英文）：回溯最近一条自然用户消息。
            lang_probe = original_user_query
            if lang_probe.startswith(_DEEP_PLAN_CONFIRMED_TAG) or lang_probe.startswith(_DEEP_PLAN_SKIPPED_TAG):
                lang_probe = self._find_last_natural_user_text() or lang_probe
            primary_lang = detect_primary_language(lang_probe)
            is_english = primary_lang == 'en'
            # 记录用户原始语言：翻译只把 query 转成英文用于检索/工具，
            # 但最终回复必须用用户原始语言（见 system_prompt 的语言指令与
            # deep 综合的 output_lang）。
            self.primary_lang = primary_lang
            self.reply_lang = _LANG_EN_NAMES.get(primary_lang) or _LANG_EN_NAMES.get(
                primary_lang.split('_')[0] if primary_lang else '', '') or 'English'
            self.reply_lang = "the user's original language" if self.reply_lang == 'unknown' else self.reply_lang

            if not is_english and not self._skip_translation_roundtrip and self.trans_config is None:
                # 仅旧式"独立翻译模型"路径才需要 trans_config；缺失则降级。
                self.send_log("WARNING",
                              "Non-English input detected, but translation model is not enabled. The core model may not perfectly handle this language.")
                self.requires_translation = False
            else:
                # 默认 skip 模式：非英文输入由主模型内化，仍强制"用原语言回复"
                self.requires_translation = (not is_english)

        except Exception as e:
            self.logger.warning(f"Language detection failed in background: {e}")
            self.requires_translation = False
            self.primary_lang = 'unknown'
            self.reply_lang = 'English'

        self.use_academic_agent = self.kwargs.get('use_academic_agent', True)
        self.academic_tags = self.kwargs.get('academic_tags', [])


        self.use_external_tools = self.kwargs.get('use_external_tools', False)
        self.external_tool_names = self.kwargs.get('external_tool_names',
                                                   [])

        # 深度研究开关：由 UI 传入（kwargs 优先），否则回退到全局配置
        self.deep_mode = self.kwargs.get('deep_mode', None)
        if self.deep_mode is None:
            self.deep_mode = bool(self.config.user_settings.get("agent_deep_mode", False))

        self.db = DatabaseManager()
        self.kb_manager = KBManager()
        self.full_response_cache = ""

        # New conversation: clear the previous round's Provenance evidence chain
        # to avoid unbounded cross-session accumulation (the collector is a
        # process-level singleton).
        try:
            from src.core.provenance import get_collector
            get_collector().clear()
        except Exception as e:
            self.logger.warning(f"Failed to clear provenance collector: {e}")

        # Reranker 仅在真正需要时才后台预加载：跨编码器加载要导入庞大的
        # transformers/optimum 栈，纯 LLM 对话（无 KB、无附件文档）根本用不到它，
        # 盲目预载会让每次对话前白等数十秒。需要时 _process_rerank 仍会按需惰性加载。
        if self.config.user_settings.get("rerank_auto_load", True) and self._rerank_needed_this_turn():
            try:
                import threading as _t
                _t.Thread(target=self._preload_reranker, daemon=True).start()
            except Exception as e:
                self.logger.warning(f"Failed to spawn reranker preload thread: {e}")

        self.main_llm = None
        self.trans_llm = None
        self.vision_llm = None

        self._init_llms()
        # 新一轮生成开始：复位各 LLM 实例的取消标志（若实例复用）。
        # 放在任务入口而非 AgentRuntime.run 内，避免 deep 模式多子 Agent 共享
        # 同一 LLM 时，并发 reset 与用户取消产生竞态。
        for llm in (self.main_llm, self.trans_llm, self.vision_llm):
            if llm is not None and hasattr(llm, "reset"):
                try:
                    llm.reset()
                except Exception as e:
                    self.logger.warning(f"Failed to reset LLM cancel state: {e}")

        original_user_query = self.messages[-1].get('display_text', self.messages[-1].get('content', ''))
        search_query = original_user_query
        domain = "General Academic"
        context_str = ""
        sources_map = {}

        # ---- Human-in-the-loop 协议轮：deep plan 确认 / 跳过哨兵解析 ----
        deep_plan_confirmed = None
        if original_user_query.startswith(_DEEP_PLAN_CONFIRMED_TAG):
            deep_plan_confirmed = self._parse_confirmed_deep_plan(original_user_query)
            if deep_plan_confirmed:
                # KB 检索查询改用确认后的子任务合集（原文哨兵文本不可直接检索）
                search_query = " ".join(t.query for t in deep_plan_confirmed)
            self.send_log("INFO",
                          f"Deep plan confirmed by user with {len(deep_plan_confirmed or [])} sub-task(s).")
        elif original_user_query.startswith(_DEEP_PLAN_SKIPPED_TAG):
            original_user_query = original_user_query[len(_DEEP_PLAN_SKIPPED_TAG):].strip()
            search_query = original_user_query
            self.deep_mode = False
            self.send_log("INFO", "Deep plan skipped by user; single-agent path for this turn.")

        # Phase 1: Query Extraction & Translation (Cache Accelerated)
        # 注意：翻译仅作用于 display_text（用户键入的问题文本）。
        # 附件（文档/图片）保存在 external_files 中，从不进入翻译模型——
        # 图片内容无法被翻译模型处理，文档正文也保持原文供视觉/正文模型消费。
        if self.requires_translation and not self._skip_translation_roundtrip:
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
        elif self.requires_translation and self._skip_translation_roundtrip:
            # 不单开翻译模型往返：search_query 保持用户原文，由主模型首轮自行理解
            # 非英文 query；调用仅英文检索/工具时由它内部英文化（见系统提示指令）。
            self.send_log(
                "INFO",
                f"Non-English query detected; separate translation round-trip disabled. "
                "Main agent will handle query and translate internally for English-only tools.",
            )

        # Phase 1.5: 工具池构建。Planner 只负责"暴露全部工具"，工具的取舍完全
        # 交给主模型第一轮 native function calling 自行决策——不再跑额外的
        # "语义聚焦" LLM 预判往返，避免为每轮对话增加一次串行延迟。
        mcp_mgr = MCPManager.get_instance()
        from src.core.skill_manager import SkillManager
        from src.core.agent.skill_registry import SkillRegistry
        from src.core.agent.planner import IntentPlanner
        skill_mgr = SkillManager.get_instance()
        planner = IntentPlanner(SkillRegistry(skill_mgr).build())

        raw_tools = []
        if self.use_academic_agent:
            raw_academic = skill_mgr.get_academic_schemas(self.academic_tags)
            if raw_academic:
                raw_tools.extend(raw_academic)
        if self.use_external_tools:
            ext_skills = skill_mgr.get_external_schemas(self.external_tool_names)
            if ext_skills:
                raw_tools.extend(ext_skills)
            for schema in mcp_mgr.tool_schemas.values():
                server_name = schema.get("server", "Unknown Server")
                if not self.external_tool_names or server_name in self.external_tool_names:
                    raw_tools.append({
                        "type": schema.get("type", "function"),
                        "function": schema.get("function", {})
                    })

        # Phase 2: Vector Retrieval & Reranking (Local KB)
        # 本地 embedding/rerank 属重量级且易崩溃的 ONNX/torch 推理。聊天任务现运行
        # 在 GUI 进程内（THREAD 模式），为避免把这类推理压在 GUI 进程里，统一交给
        # _run_kb_retrieval：优先独立短命子进程隔离执行并回传结果，失败则回退内联。
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
                domain = kb_info.get('domain', 'General Academic')
                self._emit_token(
                    "<div class='status-msg' style='color:#05B8CC; margin-bottom:4px;'>Loading local vector model and retrieving literature...</div>\n\n")
                context_str, sources_map, domain = self._run_kb_retrieval(search_query, domain)

        if not context_str.strip():
            context_str = "No local database documents provided."

        external_chunks = self.external_context or []
        images = [c for c in external_chunks if c.get("type") == "image" or str(c.get("path", "")).lower().endswith(
            IMAGE_EXTENSIONS)]
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
                reranked_docs = self._process_rerank(search_query, cand_docs, "General")
                if reranked_docs is not None:
                    self.send_log("INFO",
                                  f"Reranker finished: Reduced {len(cand_docs)} chunks to top {len(reranked_docs)} most relevant segments.")
                    cand_docs = reranked_docs
                else:
                    self.send_log("WARNING", "Reranker failed for files, falling back to top-k selection.")
                    cand_docs = cand_docs[:8]
            else:
                self.send_log("INFO",
                              f"Small attachment size ({len(cand_docs)} chunks), skipping rerank and using all content.")

            for doc in cand_docs:
                f_name = doc["metadata"]["name"]
                page = doc["metadata"]["page"]
                context_str += (
                    f"--- [User Attached File: {f_name} (Page {page})] ---\n"
                    f"Content: {doc['content']}\n\n"
                )

        if images:
            vision_model_name = str(self.main_config.get("vision_model_name", "") or "").strip()
            main_model_name = self.main_config.get("model_name", "")

            need_pre_caption = False
            active_vision_model = None

            if vision_model_name and vision_model_name.lower() != "auto":
                # 用户显式配置了 Vision 模型：先用它做图生文，保证任何主模型
                # （含纯文本模型）都能理解图片内容。
                need_pre_caption = True
                active_vision_model = vision_model_name
            else:
                # 一律原生挂载，能力校验交给 provider：OpenAI 兼容 API 没有
                # 能力查询接口，本地按模型名预判会误拦本可识图的模型
                # （Web 端 / CodeBuddy 等产品从不做此类预判）。若模型不支持
                # 图片或图片格式，API 返回 400，由统一错误面板反馈
                # （见 llm_errors 映射），provider 原始响应完整写入日志。
                self.logger.info(
                    f"Mounting {len(images)} image(s) natively for model [{main_model_name}]; "
                    "capability will be validated by the provider (no local pre-filtering).")

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

                        # 图生文结果磁盘缓存：同一图片在后续轮次重复出现时
                        # （历史附件每轮都会重载）直接命中缓存，避免重复
                        # 调用视觉模型产生不必要开销。
                        desc_content = self._load_caption_cache(img)
                        if desc_content is None:
                            vision_prompt = [{"role": "user", "content": [
                                {"type": "text",
                                 "text": "Please deeply analyze this image, extract all text (OCR), describe the charts/data, and detail its core contents. Output in pure text."},
                                {"type": "image_url", "image_url": {"url": img_data}}
                            ]}]

                            desc_res = self.vision_llm.chat(vision_prompt)
                            desc_content = desc_res.get('content', '') if isinstance(desc_res, dict) else str(desc_res)
                            self._save_caption_cache(img, desc_content)

                        image_descriptions.append(
                            f"[Image: {img.get('name', 'Unknown')}] Description:\n{desc_content}")

                    if image_descriptions:
                        llm_content.append({"type": "text",
                                            "text": "The user uploaded images. Here are their detailed textual descriptions analyzed by the vision model:\n" + "\n".join(
                                                image_descriptions)})
                except Exception as e:
                    # 视觉模型解析失败（如所选模型同样不支持图片/Key 无效）：
                    # 友好终止本轮对话，而不是静默丢弃图片导致答案与图片无关。
                    import traceback
                    self.logger.error(
                        f"Vision pre-captioning failed.\n{type(e).__name__}: {e}\n{traceback.format_exc()}")
                    self._emit_error(json.dumps(friendly_payload(
                        "Image Parsing Failed",
                        (
                            "The vision model failed to read the attached image(s), so this round "
                            "was safely terminated to avoid an answer that ignores your images.\n\n"
                            "How to fix:\n"
                            "1. Verify the configured Vision model supports image input;\n"
                            "2. Or switch the Main Model to a multimodal model and set Vision to "
                            "'Auto (Use Main Model)';\n"
                            "3. Or remove the image attachments and retry with plain text."
                        ),
                        details=f"{type(e).__name__}: {e}",
                    ), ensure_ascii=False))
                    return
                finally:
                    self.vision_llm = None
            else:
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

        # Phase 5: Agentic Generation (Modern Agent Runtime)
        self._emit_token("[START_LLM_NETWORK]")

        from src.core.agent.runtime import AgentRuntime

        # --- Modern AGENT tool exposure (main model decides via native function calling) ---
        # The Planner NEVER strips tools with keyword matching. All user-enabled tools are
        # exposed and the FIRST agent call lets the main model pick natively. No separate
        # "semantic-focus" LLM pre-pass is run (removed for latency); no guidance reminder.
        combined_tools = list(raw_tools)
        focus_reminder = ""
        if raw_tools:
            self.send_log(
                "INFO",
                f"Enabled tool pool: {len(raw_tools)} tools (Skills + MCP). "
                "Exposing all of them for native LLM function calling...",
            )
            self._emit_token("[CLEAR_SEARCH]")
            final_tool_names = [t.get("function", {}).get("name", "Unknown") for t in combined_tools]
            self.send_log(
                "INFO",
                f"Exposing {len(final_tool_names)} tools for LLM selection: {', '.join(final_tool_names)}",
            )
        else:
            combined_tools = []

        # Always expose the image generator and chart modifier so drawing requests
        # are never blocked and previously drawn charts can be re-rendered.
        from src.core.agent.runtime import _ALWAYS_TOOLS
        combined_tools.append(dict(_ALWAYS_TOOLS)["generate_image"])
        combined_tools.append(dict(_ALWAYS_TOOLS)["modify_chart"])
        combined_tools.append(dict(_ALWAYS_TOOLS)["propose_plot_plan"])
        # 通用 human-in-the-loop 澄清工具：歧义影响科学正确性或高成本操作前，
        # 由模型主动提问并等待用户作答（下一轮以用户消息回灌）。
        combined_tools.append(dict(_ALWAYS_TOOLS)["ask_user"])

        if combined_tools:
            self._emit_token("<mcp_process>⚙️ Query intent analyzed — the model selects tools natively...</mcp_process>")
            tool_names = [t.get("function", {}).get("name", "Unknown") for t in combined_tools]
            # 当绘图工具可用时，追加一条强约束，避免 reasoning 模型把 chart 参数
            # 以纯文本形式“泄漏”到最终答案里，而不是真正调用 plot_chart。
            plot_guard = ""
            if any(name == "plot_chart" for name in tool_names):
                plot_guard = (
                    "\n### DATA VISUALIZATION RULE (plot_chart / propose_plot_plan):\n"
                    "When the user requests any chart or plot (bubble, bar, scatter, volcano, heatmap, "
                    "GO enrichment, volcano plot, etc.), you MUST call the plot_chart tool to render the "
                    "figure. NEVER reply with chart parameters, the data table, or a chart specification "
                    "as plain text.\n"
                    "IMPORTANT: If the user asks to visualize/plot data but has NOT clearly specified the "
                    "chart type, the x/y columns, the title, or styling, call the propose_plot_plan tool "
                    "FIRST to show a confirmation card. Only call plot_chart after the user confirms the plan.\n"
                    "ENRICHMENT DOTPLOT REFERENCE LAYOUT (KEGG / GO / GSEA / Reactome bubble plots): "
                    "chart_type='bubble', x=Gene Ratio plotted HORIZONTALLY at the bottom, y=Pathway/Term "
                    "name on the left ordered by Gene Ratio DESCENDING (largest ratio on TOP), size=Gene "
                    "Count, color=FDR (BH-corrected p-value) with a blue-to-red continuous gradient; the "
                    "right-side legend has a vertical color bar labeled 'FDR' plus a 'Count' size legend "
                    "with discrete reference dots. Do NOT coord_flip; do NOT use -log10(FDR) for the "
                    "color mapping (use the raw FDR column directly so the gradient matches the reference).\n"
                    "If native function calling is unavailable, output exactly this JSON block and nothing else:\n"
                    "```json {\"name\": \"plot_chart\", \"arguments\": {\"chart_type\": \"bubble\", \"data\": \"[...]\", \"x\": \"...\", \"y\": \"...\"}} ```\n"
                )
            dynamic_tool_prompt = (
                f"### CRITICAL TOOL UTILIZATION RULE:\n"
                f"You have the following tools available for this query: {', '.join(tool_names)}.\n"
                f"Read the user's prompt carefully and USE the native function-calling API to invoke the "
                f"tool(s) you need. If the user asks for multi-dimensional data (e.g., metadata AND protein "
                f"interactions), you MUST use multiple tools to fulfill ALL parts of the request. DO NOT skip "
                f"required tools. DO NOT answer partially.\n\n"
                f"{focus_reminder}{plot_guard}"
            )
        else:
            dynamic_tool_prompt = ""

        system_prompt = (
            f"You are a Senior Research Scientist specializing in {domain}. "
            "Your goal is to provide high-density, evidence-based academic responses.\n\n"
            f"{self._reply_lang_instruction()}\n"
            f"{dynamic_tool_prompt}\n\n"
            "### TOOL USE PROTOCOL (STRICT):\n"
            "1. CRITICAL FOR CITATIONS: If the user's prompt asks for literature, references, citations, or a review, you MUST explicitly invoke academic search tools (like search_academic_literature) BEFORE generating your response. NEVER rely on your internal training data to generate citations, DOIs, or author lists.\n"
            "2. If the provided Context is insufficient, invoke tools IMMEDIATELY.\n"
            "3. SILENT EXECUTION: Never output your reasoning process for choosing a tool. YOU MUST USE THE NATIVE TOOL CALLING API FORMAT.\n"
            "4. FALLBACK TOOL CALLING (CRITICAL FOR REASONING MODELS): If your native function calling API is disabled (e.g., DeepSeek-R1), you MUST invoke tools manually by outputting exactly this JSON block in your response text: ```json {\"name\": \"tool_name\", \"arguments\": {\"arg\": \"value\"}} ```\n"
            "5. CROSS-DOMAIN FLEXIBILITY (CRITICAL): If the user's request matches the capability of ANY available tool (e.g., checking train tickets, weather, web search), you MUST use that tool to assist them, EVEN IF the request is not related to academic research.\n"
            "6. DIAGRAMS: use mermaid code blocks ONLY for non-data diagrams (flowcharts, architecture, relationships). Data-driven charts/plots (bubble, bar, scatter, volcano, heatmap, enrichment) must ALWAYS be rendered via the dedicated charting tool (plot_chart), never as plain text or a mermaid diagram.\n\n"
            "### CLARIFYING QUESTIONS (ask_user TOOL):\n"
            "You can pause and ask the user ONE clarifying question with clickable options via the ask_user tool. "
            "Write the question and 2-6 concrete options in the user's language. NEVER ask when the request is "
            "already clear — proceed directly instead.\n"
            "MUST-ASK-FIRST RULE: when the user names a gene / protein / metabolite / pathway WITHOUT specifying "
            "the organism, AND the task involves organism-specific data (sequence, IDs, expression, homology, "
            "pathways), you MUST call ask_user FIRST to confirm the organism/scope BEFORE invoking ANY search or "
            "retrieval tool. Do NOT guess the species and do NOT run broad or per-species exhaustive searches to "
            "work around the ambiguity.\n"
            "Also ask before any costly or hard-to-reverse operation. After calling ask_user the runtime pauses "
            "the run automatically; the user's answer arrives as the next user message.\n\n"
            "### RESPONSE GUIDELINES & CITATION PROTOCOL:\n"
            "1. IN-TEXT GROUNDING (For UI Tracking): You MUST use bracketed numbers (e.g., [1], [101]) immediately after a claim to cite the Context or Tool Results. This automatically generates a UI 'Cited Sources' block. NEVER claim facts without these bracketed numbers.\n"
            "2. FORMAL BIBLIOGRAPHY (For the User): If the user explicitly requests 'references', 'citations', or a 'review', you MUST ALSO generate a standalone 'References' section at the very end of your main text (but BEFORE the [FOLLOW_UPS] section). \n"
            "3. STRICT FORMATTING: The standalone 'References' section must strictly follow academic formatting (e.g., APA/Nature style: Authors. (Year). Title. Journal. DOI). DO NOT include conversational fluff like 'Cited for the role of...' in this formal list. List purely the bibliographic data.\n\n"
            "4. ZERO HALLUCINATION (CRITICAL): You MUST NOT fabricate, extrapolate, or infer information that is not explicitly present in the provided Context or Tool Results. If the provided data is insufficient to address the query, you MUST explicitly state: 'The provided context does not contain sufficient information to address this inquiry.' Under no circumstances should internal training data be utilized to circumvent contextual gaps.\n\n"
            "### PUNCTUATION LOCALIZATION (STRICT):\n"
            "The user's input may already have been translated to English for retrieval, so do NOT infer "
            "the reply language from the query. Instead, match punctuation to the language you are "
            "ACTUALLY writing each passage in:\n"
            "   - When writing in Chinese: use FULL-WIDTH punctuation — Chinese commas（，）, periods（。）, semicolons（；）, colons（：）, question/exclamation marks（？！）, Chinese ellipsis（……）, and Chinese parentheses（）for parenthetical remarks. Use Chinese curly quotes（“” and ‘’）for quotations instead of straight or half-width quotes.\n"
            "   - When writing in English or other languages: follow that language's standard punctuation conventions (half-width punctuation and straight quotes for English).\n"
            "2. CRITICAL EXCEPTION — do NOT modify these machine-parsed ASCII tokens under any circumstance: in-text citation markers written as [1]/[101], the literal [FOLLOW_UPS] header, JSON blocks, code fences (```...```), mermaid code blocks, tool names, identifiers, and URLs. Keep those exactly half-width ASCII.\n\n"
            "### FOLLOW-UP STRUCTURE (MANDATORY):\n"
            "At the very end of your response — after ALL other content — you MUST output the literal string [FOLLOW_UPS] on its own dedicated line, immediately followed by exactly 6 follow-up questions in this EXACT format:\n"
            "[FOLLOW_UPS]\n"
            "💡 Suggested Follow-ups:\n"
            "   - [Deep Dive] <Question about specific details or mechanisms>\n"
            "   - [Critical] <Question about limitations, alternatives, or weaknesses>\n"
            "   - [Broader] <Question about implications or future trends>\n"
            "   - [Brainstorm] <A creative brainstorming question or hypothetical \"What if\" scenario>\n"
            "   - [Similar] <Question connecting to a similar or parallel topic/concept>\n"
            "   - [Application] <Question about real-world applications or cross-disciplinary use>\n"
            "COMPLIANCE RULES (CRITICAL): (a) The string [FOLLOW_UPS] must appear EXACTLY once, alone on its own line — never inside a paragraph, heading, list item, or code block. (b) NEVER omit the follow-up section: even for short or negative answers, output at least 3 follow-ups. (c) The question list is the ABSOLUTE END of your response — output NOTHING after it. (d) Do NOT wrap this section in quotes or code fences.\n\n"
            f"### CONTEXT:\n{context_str}"
        )

        clean_history = []
        for m in self.messages[:-1]:
            if "role" in m and "content" in m:
                content = m["content"]
                if isinstance(content, str):
                    # 历史中的错误面板标记不含语义信息，剥离后避免污染 LLM 上下文
                    content = strip_markers(content)
                    # 交互卡片标记（ask_user/deep_plan/plot_plan 的 base64 载荷）
                    # 只服务 UI 渲染；卡片语义已由当轮 tool_calls / 正文保留，
                    # 回传上下文前剥除以节约 token。
                    content = self._strip_interactive_markers(content)
                msg = {"role": m["role"], "content": content}
                if m.get("tool_calls"): msg["tool_calls"] = m["tool_calls"]
                if m.get("tool_call_id"): msg["tool_call_id"] = m["tool_call_id"]
                if m.get("name"): msg["name"] = m["name"]
                clean_history.append(msg)

        rag_messages = [{"role": "system", "content": system_prompt}] + clean_history
        rag_messages.append({"role": "user", "content": llm_content})

        # ---- Run the modern Agent loop (plan -> execute -> observe) ----

        def _cite_collector(source_meta: dict):
            """Register an online MCP source for the 'Cited Sources' UI block."""
            ref_id = len(sources_map) + 101
            sources_map[ref_id] = source_meta
            return ref_id

        try:
            # 会话级 plot registry 缓存：AgentRuntime 每轮重建，但已画过的图
            # 注册在磁盘（plot_registry.json），这里传入内存缓存并回写，减少
            # 磁盘恢复开销；即使缓存丢失，modify_chart 也会自动从磁盘恢复。
            agent = AgentRuntime(
                self.main_llm, skill_mgr, mcp_mgr, planner=planner,
                cite_collector=_cite_collector,
                log_fn=self.send_log,
                plot_registry=getattr(self, "_plot_registry_cache", None),
                plot_seq=getattr(self, "_plot_seq_cache", 0),
            )
            if self.deep_mode or deep_plan_confirmed is not None:
                # 深度研究：分解为并行子任务 -> 用户确认计划 -> 独立 Agent 执行
                # -> 分节汇总。确认轮（哨兵）直接跳过分解、执行已确认的计划。
                self.full_response_cache = self._run_deep_agent(
                    agent=agent,
                    search_query=search_query,
                    rag_messages=rag_messages,
                    system_prompt=system_prompt,
                    candidate_tools=combined_tools,
                    llm_content=llm_content,
                    skill_mgr=skill_mgr,
                    mcp_mgr=mcp_mgr,
                    planner=planner,
                    sources_map=sources_map,
                    confirmed_plan=deep_plan_confirmed,
                )
            else:
                self.full_response_cache = agent.run(
                    query=search_query,
                    rag_messages=rag_messages,
                    system_prompt=system_prompt,
                    candidate_tools=combined_tools,
                    emit_token=self._emit_token,
                    is_cancelled=self.is_cancelled,
                )
            # 回写 registry，供同一 task 实例的下一轮直接复用。
            if getattr(agent, "_plot_registry", None):
                self._plot_registry_cache = agent._plot_registry
                self._plot_seq_cache = agent._plot_seq
            # 上报本次生成任务的 token 用量（真实 provider usage 优先，
            # 估算兜底；UI 在 AI 气泡下方展示）。
            # Human-in-the-loop：模型触发了 ask_user（runtime 暂停并暂存了
            # 问题卡载荷）。经结构化事件直达 UI 渲染卡片——不依赖文本标记
            # 穿过 markdown 管线，保证卡片必然出现。
            ask_payload = getattr(agent, "last_ask_user", None)
            if ask_payload:
                agent.last_ask_user = None
                self.send_log("INFO",
                              f"Ask-user card dispatched: {str(ask_payload.get('question', ''))[:80]}")
                self._emit_state(TaskState.PROCESSING, -1, "", payload={
                    "event": "ask_user",
                    "data": ask_payload,
                })

            usage = getattr(agent, "last_usage", None) or {}
            # 纯协议轮（如 deep-plan 确认等待）未跑 agent 循环，用量为 0，
            # 不上报避免 UI 显示无意义的 "0 in / 0 out"。
            if usage.get("prompt_tokens") or usage.get("completion_tokens"):
                self._emit_state(TaskState.PROCESSING, -1, "", payload={
                    "event": "usage",
                    "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                    "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                    "estimated": bool(usage.get("estimated", False)),
                })
        except Exception as e:
            self.logger.error(f"Agent runtime loop failed: {e}", exc_info=True)
            # Graceful degradation: plain streaming without tools.
            self._emit_token("[CLEAR_SEARCH]")
            self._emit_token("[START_LLM_NETWORK]")
            error_buffer = ""
            for token in self.main_llm.stream_chat(rag_messages):
                if self.is_cancelled():
                    break
                if "[API Request Error" in token or "[System Error" in token or "[Context Exceeded Error]" in token or "[Rate Limit Error]" in token or "[Timeout Error]" in token:
                    error_buffer += token
                    continue
                self.full_response_cache += token
                self._emit_token(token)
            if error_buffer:
                m = re.match(r'^\s*\[(.*?)\]\s*\n*(.*)', error_buffer, re.DOTALL)
                if m:
                    payload = friendly_payload(m.group(1).strip(), m.group(2).strip(),
                                               details=error_buffer.strip())
                else:
                    payload = friendly_payload("Provider Error", error_buffer.strip()[:400],
                                               details=error_buffer.strip())
                # 真实错误信息入日志，UI 折叠栏展示同一份 details
                self.logger.error(f"LLM stream error captured.\n{payload['details']}")
                self._emit_error(json.dumps(payload, ensure_ascii=False))
                return

            # 降级流式路径同样上报 token 用量（流式无真实 usage，纯估算）。
            self._emit_state(TaskState.PROCESSING, -1, "", payload={
                "event": "usage",
                "prompt_tokens": estimate_message_tokens(rag_messages),
                "completion_tokens": estimate_tokens(self.full_response_cache),
                "estimated": True,
            })

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

        # Phase 7: Persist Provenance evidence chain + show summary to user
        self._emit_provenance()

        return self.full_response_cache

    def _caption_cache_path(self, img_info: dict) -> str:
        """根据图片源路径与修改时间生成图生文缓存的 JSON 路径。"""
        import hashlib
        import tempfile
        cache_dir = os.path.join(tempfile.gettempdir(), "scholar_navis_cache")
        os.makedirs(cache_dir, exist_ok=True)
        src = img_info.get("image_path") or img_info.get("path", "")
        try:
            stat = os.stat(src)
            key = f"{src}_{stat.st_mtime_ns}_{stat.st_size}"
        except OSError:
            key = src
        return os.path.join(cache_dir, f"imgcap_{hashlib.md5(key.encode()).hexdigest()}.json")

    def _load_caption_cache(self, img_info: dict):
        """命中返回缓存描述文本，未命中返回 None。"""
        try:
            cache_file = self._caption_cache_path(img_info)
            if os.path.exists(cache_file):
                with open(cache_file, 'r', encoding='utf-8') as cf:
                    data = json.load(cf)
                if data.get("desc"):
                    self.send_log("INFO", f"Caption cache hit for image: {img_info.get('name', '')}")
                    return data["desc"]
        except (OSError, ValueError) as e:
            self.logger.warning(f"Caption cache read failed: {e}")
        return None

    def _save_caption_cache(self, img_info: dict, desc: str):
        """持久化图生文结果。失败静默降级（不影响主流程）。"""
        if not desc:
            return
        try:
            with open(self._caption_cache_path(img_info), 'w', encoding='utf-8') as cf:
                json.dump({"name": img_info.get("name", ""), "desc": desc}, cf, ensure_ascii=False)
        except OSError as e:
            self.logger.warning(f"Caption cache write failed: {e}")

    @staticmethod
    def _looks_vision_capable(model_name: str) -> bool:
        """根据模型名称启发式判断是否具备视觉（多模态）能力。

        关键词覆盖主流多模态模型系列；已知纯文本家族（DeepSeek 等）
        显式排除。

        注意：图片现已一律原生挂载、由 provider 裁决（见 _execute 的
        images 分支），本方法不再用于发送前拦截，仅保留给
        developer_dialog 的能力矩阵自检与诊断用途。
        """
        name = (model_name or "").lower()
        if not name:
            return False

        vision_keywords = [
            'image', 'vision', '-vl', 'vl-', 'llava', 'pixtral', 'internvl',
            'minicpm-v', 'molmo', 'qvq',
            'gpt-4o', 'gpt-4-turbo', 'gpt-4.1', 'gpt-5', 'o1', 'o3', 'o4-mini',
            'gemini-1.5', 'gemini-2.0', 'gemini-2.5', 'gemma-3',
            'claude-3', 'claude-4', 'claude-sonnet', 'claude-opus', 'claude-haiku',
            'qwen-vl', 'glm-4v', 'glm-4.5v', 'glm-4.6', 'doubao', 'hunyuan-vision',
            'step-1v', 'yi-vision', 'grok-vision', 'grok-4',
            'llama-3.2', 'llama-4', 'phi-3-vision', 'phi-4-multimodal',
        ]
        if any(kw in name for kw in vision_keywords):
            # 已知不支持图片的家族显式排除
            if 'deepseek' in name or 'ernie' in name:
                return False
            return True
        return False

    @staticmethod
    def _friendly_error_payload(title: str, body: str, details: str = "") -> str:
        """兼容入口：错误归一化 -> 统一 JSON payload 字符串（含 details）。

        映射逻辑集中在 ``src/core/llm_errors.py``，任务端与 Agent 运行时
        共用同一套规则，保证 LLM 侧报错文案与 UI 样式全局一致：
        ``title``/``body`` 面向用户，``details`` 为程序真实错误信息，
        由错误面板折叠栏展示并写入日志。
        """
        return json.dumps(friendly_payload(title, body, details), ensure_ascii=False)

    def _emit_provenance(self):
        """Persist the current conversation's evidence chain as JSONL and show
        a summary to the user.

        This is the Provenance "consumption" step: every tool call in this
        conversation (tool -> params -> status -> source -> timestamp) is
        written to ``scholar_workspace/provenance/`` and surfaced as an
        auditable summary + download link. Failures must never break the main
        flow, so everything degrades silently.
        """
        try:
            from src.core.provenance import get_collector
            collector = get_collector()
            records = collector.snapshot()
            if not records:
                return

            # Output directory: scholar_workspace/provenance/
            from src.core import BASE_DIR
            prov_dir = os.path.join(BASE_DIR, "scholar_workspace", "provenance")
            path = collector.export_to_dir(prov_dir, conversation_id=getattr(self, "task_id", ""))

            # Aggregate by tool for the summary.
            from collections import Counter
            counter = Counter(r["tool"] for r in records)
            ok_count = sum(1 for r in records if r["status"] == "success")
            fail_count = len(records) - ok_count

            import html as _html_mod
            rows = []
            for tool, cnt in counter.most_common():
                rows.append(
                    f"<div style='margin-bottom:3px;'>▪ <b>{_html_mod.escape(tool)}</b> "
                    f"<span style='color:#888;'>({cnt} call(s))</span></div>"
                )

            ref_html = (
                "\n<br><hr style='border:0; height:1px; background:#444; margin:15px 0;'>"
                "<b>📊 Provenance (trace log):</b><br>"
                f"<div style='margin-top:6px; font-size:13px;'>"
                f"{len(records)} tool call(s) this round, "
                f"{ok_count} succeeded, {fail_count} failed:<br>"
                + "".join(rows)
            )

            if path:
                fpath = path.replace("\\", "/")
                uri = f"file:///{fpath}" if not fpath.startswith("/") else f"file://{fpath}"
                ref_html += (
                    f"<div style='margin-top:8px; font-size:12px;'>"
                    f"Full evidence chain (JSONL): "
                    f"<a href='{uri}' style='color:#05B8CC; text-decoration:none;'>"
                    f"{_html_mod.escape(os.path.basename(path))}</a></div>"
                )
            ref_html += "</div>"
            self._emit_token(ref_html)
        except Exception as e:
            self.logger.warning(f"Provenance emission skipped: {e}")

    def _run_deep_agent(self, agent, search_query, rag_messages, system_prompt,
                        candidate_tools, llm_content, skill_mgr, mcp_mgr,
                        planner, sources_map, confirmed_plan=None):
        """Deep research: decompose into parallel sub-investigations.

        Args:
            confirmed_plan: 用户确认后的 SubTask 列表（来自 deep-plan 确认卡）。
                非空时跳过分解直接执行；为 None 时先出计划卡等待用户确认。
        """
        """深度研究：分解 -> 并行子 Agent -> 分节汇总。

        - 分解失败或不可分解时，回退单 Agent 路径。
        - 每个子任务共享本地 KB 上下文（Phase 2 已构建），独立收集在线引用。
        - 子任务引用 id（>=101）在合并时重映射为全局 id，避免冲突。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from src.core.agent.decomposer import TaskDecomposer, MIN_SUB_QUERIES
        from src.core.agent.synthesizer import Synthesizer
        from src.core.agent.runtime import AgentRuntime

        self._emit_token("[CLEAR_SEARCH]")

        sub_tasks = None
        if confirmed_plan is not None:
            # 用户已确认计划：直接采用确认的子任务，跳过重复分解。
            sub_tasks = [st for st in confirmed_plan if (st.query or "").strip()]
            self.send_log("INFO",
                          f"Deep mode: executing user-confirmed plan with {len(sub_tasks)} sub-task(s).")
            if len(sub_tasks) < MIN_SUB_QUERIES:
                single_query = sub_tasks[0].query if sub_tasks else search_query
                self.send_log("INFO", "Confirmed plan collapsed to a single focus; single-agent path.")
                return agent.run(
                    query=single_query,
                    rag_messages=rag_messages,
                    system_prompt=system_prompt,
                    candidate_tools=candidate_tools,
                    emit_token=self._emit_token,
                    is_cancelled=self.is_cancelled,
                )
        else:
            self._emit_token(
                "<div class='status-msg' style='color:#05B8CC; margin-bottom:4px;'>"
                "Analyzing query for multi-path decomposition...</div>\n\n"
            )
            decomp = TaskDecomposer(self.main_llm).decompose(search_query)
            if not decomp.decomposable:
                self.send_log("INFO", "Query not decomposable; using single-agent path.")
                return agent.run(
                    query=search_query,
                    rag_messages=rag_messages,
                    system_prompt=system_prompt,
                    candidate_tools=candidate_tools,
                    emit_token=self._emit_token,
                    is_cancelled=self.is_cancelled,
                )
            # Human-in-the-loop：先出计划确认卡并结束本轮，用户确认后以
            # [DEEP_PLAN_CONFIRMED] 哨兵重进管线；避免方向跑偏时白烧
            # N 路并行子 Agent 的 token。
            self._emit_token("[CLEAR_SEARCH]")
            self._propose_deep_plan(search_query, decomp)
            waiting = self._deep_plan_waiting_text()
            self._emit_token(waiting)
            # 结构化事件：通知 UI 进入"等待用户确认计划"状态（锁定通用发送）。
            self._emit_state(
                TaskState.PROCESSING, -1, "",
                payload={"event": "await_user", "source": "deep_plan"})
            self.send_log("INFO",
                          f"Deep plan proposed ({len(decomp.sub_tasks)} sub-tasks); waiting for user confirmation.")
            return waiting

        self.send_log("INFO", f"Deep mode: launching {len(sub_tasks)} parallel sub-investigation(s).")
        self._emit_token(
            f"<mcp_process>⚙️ Deep research: {len(sub_tasks)} parallel sub-investigations launched...</mcp_process>\n"
        )

        # ---- 并行执行各子任务 ----
        # 子 Agent 无法触达用户：从其工具池剔除 ask_user，避免子任务发起
        # 无人应答的暂停式提问。
        sub_candidate_tools = [
            t for t in (candidate_tools or [])
            if (t or {}).get("function", {}).get("name") != "ask_user"
        ]
        results = [None] * len(sub_tasks)

        def _run_sub(idx, st):
            local_sources = {}

            def _local_cite(meta):
                rid = len(local_sources) + 101
                local_sources[rid] = meta
                return rid

            sub_llm_content = list(llm_content[:-1]) + [
                {"type": "text", "text": f"User Query:\n{st.query}"}
            ]
            sub_rag = [dict(m) for m in rag_messages]
            sub_rag[-1] = dict(sub_rag[-1])
            sub_rag[-1]["content"] = sub_llm_content

            sub_agent = AgentRuntime(
                self.main_llm, skill_mgr, mcp_mgr, planner=planner,
                cite_collector=_local_cite, log_fn=self.send_log,
            )
            buffer = []
            try:
                text = sub_agent.run(
                    query=st.query,
                    rag_messages=sub_rag,
                    system_prompt=system_prompt,
                    candidate_tools=sub_candidate_tools,
                    emit_token=buffer.append,
                    is_cancelled=self.is_cancelled,
                )
            except Exception as e:
                self.logger.warning(f"Sub-task '{st.query[:40]}' failed: {e}")
                text = f"[Sub-investigation unavailable: {e}]"
            return idx, text, local_sources

        with ThreadPoolExecutor(max_workers=len(sub_tasks)) as pool:
            futures = [pool.submit(_run_sub, i, st) for i, st in enumerate(sub_tasks)]
            for fut in as_completed(futures):
                try:
                    idx, text, local_sources = fut.result()
                    results[idx] = (text, local_sources)
                except Exception as e:
                    self.logger.warning(f"Sub-task future failed: {e}")

        # ---- 合并引用并重映射 ----
        next_id = max((k for k in sources_map if isinstance(k, int)), default=0) + 1
        merged = []
        plot_markers = []  # 收集子任务产生的 <rplot_card> 标记，避免 base64 污染合成器

        def _pull_plot_markers(m):
            plot_markers.append(m.group(0))
            return ""

        for i, (st, res) in enumerate(zip(sub_tasks, results)):
            if res is None:
                text, local_sources = "", {}
            else:
                text, local_sources = res
            remap = {}
            for local_id, meta in local_sources.items():
                remap[local_id] = next_id
                sources_map[next_id] = meta
                next_id += 1
            text = self._clean_sub_result(text)
            text = self._remap_citations(text, remap)
            text = re.sub(r'<rplot_card data="([^"]*)"></rplot_card>', _pull_plot_markers, text)
            merged.append({
                "heading": st.query,
                "query": st.query,
                "text": text,
            })

        # ---- 分节汇总 ----
        self._emit_token(
            "<div class='status-msg' style='color:#05B8CC; margin-bottom:4px;'>"
            "Synthesizing structured synthesis across sub-investigations...</div>\n\n"
        )
        synthesizer = Synthesizer(self.main_llm)
        final_text = synthesizer.synthesize(
            search_query, merged,
            output_lang=getattr(self, "reply_lang", "English"),
        )

        # 合成后把 R 绘图卡片标记追加回最终文本，保证 UI 端仍能渲染固定卡片
        if plot_markers:
            final_text = final_text.rstrip() + "\n\n" + "\n".join(plot_markers)

        self._emit_token("[CLEAR_SEARCH]")
        self._emit_token("[START_LLM_NETWORK]")
        self._emit_token(final_text)
        return final_text

    def _propose_deep_plan(self, search_query, decomp):
        """流出深度研究计划确认卡标记（base64 JSON，UI 解码后渲染交互卡）。"""
        import base64
        payload = {
            "query": search_query,
            "sub_tasks": [
                {"query": st.query, "rationale": getattr(st, "rationale", "") or ""}
                for st in decomp.sub_tasks
            ],
        }
        encoded = base64.b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        self._emit_token(f'<deep_plan data="{encoded}"></deep_plan>\n')

    def _deep_plan_waiting_text(self) -> str:
        """计划确认等待文案（按用户回复语言本地化）。"""
        lang = getattr(self, "reply_lang", "English") or "English"
        if "Chinese" in lang:
            return (
                "我已将你的问题拆解为多个并行子调查（见上方计划卡）。请查看或修改子问题后"
                "点击“Confirm & Execute”开始深度研究；也可点击“Answer Directly”跳过拆解、"
                "直接作答。"
            )
        return (
            "I have drafted a deep-research plan with several parallel sub-investigations "
            "(see the plan card above). Review or edit the sub-questions, then click "
            "'Confirm & Execute' to start; or click 'Answer Directly' to get a direct "
            "answer without decomposition."
        )

    def _parse_confirmed_deep_plan(self, text):
        """解析 [DEEP_PLAN_CONFIRMED] 哨兵消息为 SubTask 列表。

        非协议消息返回 None（走常规分解流程）；协议消息逐行剥除编号后生成子任务。
        """
        m = re.match(r"^\[DEEP_PLAN_CONFIRMED\]\s*\n?(.*)$", text or "", re.DOTALL)
        if not m:
            return None
        from src.core.agent.decomposer import SubTask
        tasks = []
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line:
                continue
            line = re.sub(r"^\d+\s*[.、)）]\s*", "", line).strip(" -*\t")
            if line:
                tasks.append(SubTask(query=line))
        return tasks

    def _find_last_natural_user_text(self) -> str:
        """回溯最近一条非协议（非哨兵）用户消息文本，供回复语言检测使用。"""
        for m in reversed(self.messages or []):
            if m.get("role") != "user":
                continue
            content = m.get("content")
            text = m.get("display_text") or (content if isinstance(content, str) else "") or ""
            text = str(text).strip()
            if not text:
                continue
            if text.startswith(_DEEP_PLAN_CONFIRMED_TAG) or text.startswith(_DEEP_PLAN_SKIPPED_TAG):
                continue
            return text
        return ""

    @staticmethod
    def _strip_interactive_markers(text: str,
                                   tags=("ask_user", "deep_plan", "plot_plan")) -> str:
        """剥除文本中的交互卡片标记。

        卡片 base64 载荷只服务 UI 渲染；卡片语义（工具参数、计划文本）已由
        当轮 tool_calls / 正文保留，回传上下文前剥离以节约 token。
        """
        if not text:
            return text or ""
        for tag in tags:
            text = re.sub(rf'<{tag} data="[^"]*"\s*>\s*</{tag}>', "", text)
            text = re.sub(rf"<{tag}[^>]*/?>", "", text)
        return text

    @staticmethod
    def _remap_citations(text, remap):
        """将文本中 `[id]` 按 remap 映射替换为新的全局 id（仅替换映射内 id）。"""
        if not text or not remap:
            return text or ""
        def _repl(m):
            cid = int(m.group(1))
            return f"[{remap[cid]}]" if cid in remap else m.group(0)
        return re.sub(r"\[(\d+)\]", _repl, text)

    @staticmethod
    def _clean_sub_result(text):
        """清理子任务返回文本中的运行时控制标记，避免污染合成器。

        Agent.run 的返回值混杂了 UI 控制 token（[CLEAR_SEARCH]、
        [START_LLM_NETWORK]）与思考块（<think>...</think>），合成前必须剥离。
        """
        if not text:
            return ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        for token in ("[CLEAR_SEARCH]", "[START_LLM_NETWORK]", "[FOLLOW_UPS]"):
            text = text.replace(token, "")
        # ask_user/deep_plan 不应出现在子任务结果（前者已从子工具池剔除，
        # 后者仅主流程分解后产生）；万一出现，剥除避免 base64 污染合成器。
        text = ChatGenerationTask._strip_interactive_markers(
            text, tags=("ask_user", "deep_plan"))
        return text.strip()

    def _rerank_needed_this_turn(self):
        """判断本轮是否需要交叉编码器重排。

        重排只在两种情形被调用（见主流程 Phase 2/Phase 3）：
        - 命中了非空知识库的候选文档；
        - 用户附带 >5 个文本块需要打分。
        其余纯 LLM 对话（例如只跑学术 Agent、无 KB 无附件）不需要重排，
        提前预载只会白付 transformers/optimum 的庞大导入开销。
        """
        try:
            if self.kb_id:
                kb = self.kb_manager.get_kb_by_id(self.kb_id)
                # 与 Phase 2 空库跳过逻辑保持一致；取不到元信息时保守按需预载
                if kb and kb.get('doc_count', 0) > 0:
                    return True
            text_docs = [
                c for c in getattr(self, 'external_context', [])
                if c.get("type") != "image" and (c.get("content") or "").strip()
            ]
            if len(text_docs) > 5:
                return True
        except Exception as e:
            self.logger.debug(f"Reranker need-check failed, defaulting to eager preload: {e}")
            return True
        return False

    def _preload_reranker(self):
        """后台线程预加载交叉编码器重排模型。"""
        try:
            from src.core.rerank_engine import RerankEngine
            RerankEngine().preload()
        except Exception as e:
            self.logger.warning(f"Reranker preload failed: {e}")

    def _run_kb_retrieval(self, search_query, domain):
        """在 GUI 进程内执行 Phase 2 的本地 KB 检索与重排。

        聊天任务现运行在 GUI 进程的 QThread 中，本地 embedding/rerank 属于
        ONNX/torch 重量级且易崩溃（GPU OOM / 驱动问题）的推理。为保留崩溃隔离
        与资源释放，优先把它们放进一个独立短命子进程（_kb_retrieval_worker）
        执行并回传结果；子进程不可用或超时则回退为进程内联执行（_kb_retrieval_core），
        保证 KB 检索永不因隔离失败而中断。

        返回 (context_str, sources_map, effective_domain)。调用方负责在 context_str
        为空时写默认文案。
        """
        model_name = (self.main_config or {}).get("model_name", "")
        history_context = ""
        if len(self.messages) >= 3:
            prev_assistant = self.messages[-2].get('content', '')[:100]
            if isinstance(prev_assistant, str):
                history_context = f" (Context: {prev_assistant})"

        # 优先走独立子进程隔离本地推理
        try:
            import multiprocessing as mp
            import tempfile

            fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="kb_ret_")
            os.close(fd)

            proc = mp.Process(
                target=_kb_retrieval_worker,
                args=(self.kb_id, search_query, model_name, tmp_path, history_context),
                daemon=True,
            )
            proc.start()
            deadline = time.time() + 120.0  # 硬上限兜底，避免无限等待
            while proc.is_alive():
                if self.is_cancelled():
                    proc.terminate()
                    break
                if time.time() > deadline:
                    self.logger.warning("KB retrieval subprocess timed out; killing it.")
                    proc.terminate()
                    break
                time.sleep(0.1)
            proc.join(timeout=2.0)

            if os.path.exists(tmp_path):
                with open(tmp_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # JSON key 只能是字符串，这里把引用 id 还原为整型（下游 UI 匹配用）
                sources_map = {int(k): v for k, v in (data.get("sources_map") or {}).items()}
                return data.get("context_str", ""), sources_map, domain
        except Exception as e:
            self.logger.warning(f"KB retrieval subprocess unavailable, falling back inline: {e}")

        # 兜底：进程内联执行（共享同一权威实现，保证行为一致）
        return _kb_retrieval_core(self.kb_id, search_query, model_name, history_context)[:2] + (domain,)

    def _process_rerank(self, query, docs, domain, top_k=None, emit_warning=True):
        """两阶段精排：交叉编码器重排 + 分数阈值过滤。

        - top_k: 返回文档数上限；None 时取配置 rerank_top_k（默认 5）。
        - 低于 rerank_min_score 的文档会被丢弃（全低于阈值时保底保留前 3 个，
          避免上下文为空）。
        - 首次失败弹一次警告，之后静默降级为原始顺序。
        """
        if not docs:
            return []

        cfg = self.config.user_settings
        if top_k is None:
            top_k = int(cfg.get("rerank_top_k", 5))
        min_score = float(cfg.get("rerank_min_score", 0.0))

        try:
            from src.core.rerank_engine import RerankEngine
            engine = RerankEngine()

            ranked_docs = engine.rerank(query, docs, domain=domain, top_k=top_k)
            if not ranked_docs:
                return docs[:top_k]

            # 分数阈值过滤：cross-encoder 概率分数，bge-reranker 类模型通常在 0.1~0.99
            if min_score > 0:
                kept = [d for d in ranked_docs if d.get("score", 1.0) >= min_score]
                if not kept:
                    kept = ranked_docs[:3]
                elif len(kept) < len(ranked_docs):
                    self.send_log("INFO",
                                  f"Rerank threshold ({min_score}) dropped "
                                  f"{len(ranked_docs) - len(kept)} low-relevance chunks.")
                return kept

            return ranked_docs

        except Exception as e:
            self.logger.error(f"Direct Rerank Engine execution failed: {e}")

            if emit_warning and not self._rerank_warned:
                self._rerank_warned = True
                warning_html = (
                    f"<br><div style='color:#e6a23c; font-size:13px; margin-bottom:5px; padding:10px; border:1px solid #e6a23c; border-radius:6px; background-color: rgba(230, 162, 60, 0.05);'>"
                    f"⚠️ <b>Reranker Processing Failed</b><br><br>"
                    f"Failed to rerank documents: <i>{str(e)}</i>.<br>"
                    f"If the model is missing, please go to <b>[Global Settings] -> [Models]</b> to manually download it.<br><br>"
                    f"<i>* Continuing analysis with default document ordering.</i>"
                    f"</div><br>"
                )
                self._emit_token(warning_html)
            else:
                self.send_log("WARNING", "Reranker unavailable, using default document ordering.")

            # 降级方案：返回未重新排序的前 top_k 个文档
            return docs[:top_k]



class ExportChatTask(BackgroundTask):
    """
    后台任务：异步导出聊天记录（支持 PDF, MD, TXT, CSV）
    """
    def _execute(self):
        history = self.kwargs.get('history', [])
        path = self.kwargs.get('path')
        export_fmt = self.kwargs.get('export_fmt')
        colors = self.kwargs.get('colors', {})
        font_family = self.kwargs.get('font_family', 'sans-serif')
        user_icon = self.kwargs.get('user_icon', '')
        ai_icon = self.kwargs.get('ai_icon', '')

        import datetime
        import csv
        from src.ui.components.text_formatter import TextFormatter

        # 说明：不再静默过滤 interrupted/error 消息 —— 它们往往含部分已生成内容，
        # 直接丢弃会导致导出对话"不全/断档"。改为在各格式正文前标注其未完成状态。
        if not history:
            return {"success": False, "msg": "No valid chat records to export."}

        def _status_note(msg):
            st = msg.get("status")
            if st == "interrupted":
                return "⚠ This response was interrupted and may be incomplete."
            if st == "error":
                return "⚠ This response ended in an error and may be incomplete."
            return ""

        clean_history = history
        try:
            if export_fmt == ".pdf":
                from PySide6.QtGui import QPdfWriter, QTextDocument, QPageSize
                from PySide6.QtCore import QMarginsF

                doc = QTextDocument()
                date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                doc.setDefaultStyleSheet(f"""
                    body {{ font-family: {font_family}; font-size: 10.5pt; line-height: 1.6; color: #24292e; background-color: #ffffff; }}
                    h1, h2, h3 {{ color: {colors.get('title_blue')}; border-bottom: 1px solid #eaecef; padding-bottom: 4px; }}
                    .msg-box {{ margin-bottom: 25px; padding-bottom: 15px; border-bottom: 1px dashed #dddddd; page-break-inside: avoid; }}
                    .header-user {{ color: {colors.get('academic_blue')}; font-weight: bold; font-size: 12pt; margin-bottom: 8px; }}
                    .header-ai {{ color: {colors.get('success')}; font-weight: bold; font-size: 12pt; margin-bottom: 8px; }}
                    .content {{ margin-top: 5px; }}
                    pre {{ background-color: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 4px; padding: 12px; white-space: pre-wrap; font-family: Consolas, "Courier New", monospace; font-size: 9.5pt; }}
                    code {{ font-family: Consolas, "Courier New", monospace; background-color: #f3f4f6; padding: 2px 4px; border-radius: 3px; color: #d73a49; font-size: 9.5pt; }}
                    pre code {{ background-color: transparent; padding: 0; color: #24292e; }}
                    blockquote {{ border-left: 4px solid #dfe2e5; color: #6a737d; padding-left: 15px; margin-left: 0; }}
                    table {{ border-collapse: collapse; width: 100%; margin-top: 10px; margin-bottom: 10px; }}
                    th, td {{ border: 1px solid #dfe2e5; padding: 8px 12px; text-align: left; word-break: break-all; }}
                    th {{ background-color: #f6f8fa; font-weight: bold; }}
                    .doc-header {{ text-align: center; border-bottom: 2px solid {colors.get('title_blue')}; padding-bottom: 15px; margin-bottom: 30px; }}
                    .doc-title {{ font-size: 22pt; font-weight: bold; color: {colors.get('title_blue')}; font-family: 'Segoe UI', sans-serif; }}
                    .doc-meta {{ font-size: 10pt; color: #586069; margin-top: 5px; }}
                """)

                html = f"<html><body><div class='doc-header'><div class='doc-title'>Scholar Navis - Analysis Report</div><div class='doc-meta'>Generated on: {date_str} | Document Type: Academic Chat Log</div></div>"

                for msg in clean_history:
                    is_user = (msg['role'] == "user")
                    clean_content = TextFormatter.clean_text_for_export(msg['content'])
                    rendered_html = TextFormatter.markdown_to_html(clean_content)

                    if is_user:
                        header = f"<div class='header-user'><img src='{user_icon}' width='16' height='16' style='vertical-align:middle;'> User Inquiry</div>"
                    else:
                        header = f"<div class='header-ai'><img src='{ai_icon}' width='16' height='16' style='vertical-align:middle;'> AI Analysis</div>"

                    note = _status_note(msg)
                    note_html = f"<div style='color:#b8860b; font-style:italic; margin-bottom:4px;'>{note}</div>" if note else ""
                    html += f"<div class='msg-box'>{header}{note_html}<div class='content'>{rendered_html}</div></div>"

                html += "</body></html>"
                doc.setHtml(html)

                writer = QPdfWriter(path)
                writer.setPageSize(QPageSize(QPageSize.A4))
                writer.setPageMargins(QMarginsF(15, 20, 15, 20))
                writer.setResolution(300)
                doc.print_(writer)

            elif export_fmt == ".md":
                md_lines = [
                    "# Scholar Navis - Analysis Report\n\n",
                    f"> **Generated:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n",
                    "---\n\n"
                ]
                for msg in clean_history:
                    role = "🧑‍💻 User Inquiry" if msg['role'] == "user" else "🤖 AI Analysis"
                    content = TextFormatter.clean_text_for_export(msg['content'])
                    note = _status_note(msg)
                    note_text = f"> {note}\n\n" if note else ""
                    md_lines.append(f"### {role}\n\n{note_text}{content}\n\n---\n\n")

                with open(path, "w", encoding="utf-8") as f:
                    f.write("".join(md_lines))

            elif export_fmt == ".txt":
                txt_lines = [
                    "================ SCHOLAR NAVIS ACADEMIC REPORT ================",
                    f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    "===============================================================\n\n"
                ]
                for msg in clean_history:
                    role = "USER INQUIRY" if msg['role'] == "user" else "AI ANALYSIS"
                    content = TextFormatter.clean_text_for_export(msg['content'])
                    content = TextFormatter.markdown_to_plain_text(content)
                    note = _status_note(msg)
                    txt_lines.append(f"[{role}]")
                    if note:
                        txt_lines.append(f"[NOTE] {note}")
                    txt_lines.append(content)
                    txt_lines.append(f"\n{'-' * 70}\n")

                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(txt_lines))

            elif export_fmt in (".json", ".schat"):
                # 无损导出：完整序列化历史记录（含富字段、引文、附件引用等）。
                # 供 "Import" 还原为可继续对话的上下文。非 JSON 可序列化内容降级为 str。
                import json as _json

                def _sanitize(value):
                    if isinstance(value, (str, int, float, bool)) or value is None:
                        return value
                    if isinstance(value, list):
                        return [_sanitize(v) for v in value]
                    if isinstance(value, dict):
                        return {k: _sanitize(v) for k, v in value.items()}
                    try:
                        return str(value)
                    except Exception:
                        return None

                lossless_payload = {
                    "format": "scholar_navis_chat_history",
                    "version": 1,
                    "generated": datetime.datetime.now().isoformat(timespec="seconds"),
                    "messages": [_sanitize(m) for m in history],
                }
                with open(path, "w", encoding="utf-8") as f:
                    _json.dump(lossless_payload, f, ensure_ascii=False, indent=2)

            return {"success": True, "path": path}
        except Exception as e:
            self.send_log("ERROR", f"Export task failed: {str(e)}")
            return {"success": False, "msg": str(e)}


class ImportChatHistoryTask(BackgroundTask):
    """
    后台任务：解析并导入聊天记录。

    支持的输入：
    - ``.schat`` / ``.json``：Scholar Navis 无损格式（含富字段 / 引文 / 附件引用）。
    - ``.md`` / ``.txt``：旧格式导出，尽力还原为纯文本气泡（有损）。

    返回 ``{"success": True, "messages": [...], "lossless": bool}``，
    消息结构已规范化为可写入 ``self.history`` 的条目。
    """
    _TXT_USER = "[USER INQUIRY]"
    _TXT_AI = "[AI ANALYSIS]"

    def _execute(self):
        path = self.kwargs.get("path")
        if not path or not os.path.exists(path):
            return {"success": False, "msg": f"File not found: {path}"}

        lower = path.lower()
        try:
            if lower.endswith((".schat", ".json")):
                messages, lossless = self._parse_lossless(path)
            elif lower.endswith(".md"):
                messages, lossless = self._parse_markdown(path), False
            elif lower.endswith((".txt", ".csv")):
                messages, lossless = self._parse_text(path), False
            else:
                return {"success": False,
                        "msg": "Unsupported format. Please import a .schat/.json/.md/.txt file."}

            if not messages:
                return {"success": False,
                        "msg": "No chat messages could be parsed from this file."}

            return {"success": True, "messages": messages, "lossless": lossless}
        except Exception as e:
            self.send_log("ERROR", f"Import task failed: {str(e)}")
            return {"success": False, "msg": str(e)}

    @staticmethod
    def _normalize_msg(raw):
        """把原始记录条目规范化为可写回 self.history 的 user/assistant 条目。"""
        role = raw.get("role", "")
        content = raw.get("content")
        if role not in ("user", "assistant"):
            role = "assistant"
        msg = {"role": role}
        if isinstance(content, str):
            msg["content"] = content
        elif isinstance(content, dict):
            msg["content"] = content.get("text") or content.get("content") or ""
        elif content is None:
            msg["content"] = ""
        else:
            msg["content"] = str(content)
        # 保留供气泡渲染的富字段
        for key in ("display_text", "context_html", "external_files", "status"):
            if key in raw:
                msg[key] = raw[key]
        return msg

    def _parse_lossless(self, path):
        import json as _json
        with open(path, "r", encoding="utf-8") as f:
            payload = _json.load(f)
        if isinstance(payload, dict) and payload.get("format") == "scholar_navis_chat_history":
            messages = [self._normalize_msg(m) for m in payload.get("messages", [])]
            return messages, True
        # 退路：形如 {"role": ..., "content": ...} 的单条，或 {"history"/"messages": [...]} 的包装
        if isinstance(payload, list):
            return [self._normalize_msg(m) for m in payload if isinstance(m, dict)], True
        if isinstance(payload, dict):
            for key in ("messages", "history", "conversation"):
                val = payload.get(key)
                if isinstance(val, list):
                    return [self._normalize_msg(m) for m in val if isinstance(m, dict)], True
            if "role" in payload and "content" in payload:
                return [self._normalize_msg(payload)], True
        return [], True

    def _parse_markdown(self, path):
        # 按历史消息的 `### ... User Inquiry / AI Analysis` 标题切分。
        # 标题可能带角色 emoji（如 🧑💻 / 🤖），因此仅在标题行内检索角色关键字。
        import re as _re
        _user_kw = _re.compile(r"User Inquiry", _re.IGNORECASE)
        _ai_kw = _re.compile(r"AI Analysis", _re.IGNORECASE)
        _header_re = _re.compile(r"^\s*#{1,6}\s+.*$")
        seps = _re.compile(r"^\s*---\s*$")

        with open(path, "r", encoding="utf-8") as f:
            text = f.read()

        lines = text.splitlines()
        markers = []  # (line_index, role)
        for i, line in enumerate(lines):
            if _header_re.match(line):
                if _user_kw.search(line):
                    markers.append((i, "user"))
                elif _ai_kw.search(line):
                    markers.append((i, "assistant"))

        if not markers:
            body = self._strip_report_header(text)
            return [{"role": "assistant", "content": body.strip()}] if body.strip() else []

        messages = []
        for idx, (start_i, role) in enumerate(markers):
            end_i = markers[idx + 1][0] if idx + 1 < len(markers) else len(lines)
            body_lines = lines[start_i + 1:end_i]
            # 去除段落末尾的空行与水平分隔线
            while body_lines and (not body_lines[0].strip() or seps.match(body_lines[0])):
                body_lines.pop(0)
            while body_lines and (not body_lines[-1].strip() or seps.match(body_lines[-1])):
                body_lines.pop()
            content = "\n".join(body_lines).strip()
            if content:
                messages.append({"role": role, "content": content})
        return messages

    def _strip_report_header(self, text):
        # 去除导出文件顶部的报告头（标题 + Generated 元信息）
        lines = text.splitlines()
        out = []
        for line in lines:
            if line.strip().startswith("# Scholar Navis") or line.strip().startswith("> **Generated:**"):
                continue
            out.append(line)
        return "\n".join(out)

    def _parse_text(self, path):
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()

        user_re = None
        ai_re = None
        lower = path.lower()
        if lower.endswith(".csv"):
            import csv as _csv
            import io as _io
            messages = []
            reader = _csv.DictReader(_io.StringIO(text))
            for row in reader:
                role = row.get("Role", "").strip().lower()
                content = row.get("Content", "")
                if role.startswith("user"):
                    messages.append({"role": "user", "content": content})
                elif role.startswith("ai") or role.startswith("assistant"):
                    messages.append({"role": "assistant", "content": content})
            return messages

        # TXT 格式：行式 `[USER INQUIRY]` / `[AI ANALYSIS]`
        lines = text.splitlines()
        messages = []
        cur_role = None
        buf = []
        sep = "-" * 70

        def flush():
            nonlocal cur_role, buf
            body = "\n".join(buf).strip()
            # 剥离行内环绕分隔线
            body = "\n".join(l for l in body.splitlines() if l.strip() != sep)
            if cur_role and body:
                messages.append({"role": cur_role, "content": body})
            cur_role = None
            buf = []

        for line in lines:
            stripped = line.strip()
            if stripped.upper().startswith(self._TXT_USER) or stripped == "USER INQUIRY":
                flush()
                cur_role = "user"
            elif stripped.upper().startswith(self._TXT_AI) or stripped == "AI ANALYSIS":
                flush()
                cur_role = "assistant"
            elif cur_role is not None:
                buf.append(line)
            # 报告头/分隔线忽略（无 cur_role 时）
        flush()
        return messages


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


class FetchHardwareStatusTask(BackgroundTask):
    """
    异步获取硬件状态，避免阻塞主 UI 线程
    """

    def _execute(self):
        from src.core.device_manager import DeviceManager
        from src.core.config_manager import ConfigManager

        dev_mgr = DeviceManager()
        config = ConfigManager()

        curr_id = config.user_settings.get("inference_device", "auto")
        parsed_id = dev_mgr.parse_device_string(curr_id)

        dev_name = parsed_id
        for d in dev_mgr.get_available_devices():
            if d['id'] == parsed_id:
                dev_name = d['name']
                break

        return {"dev_name": dev_name}

