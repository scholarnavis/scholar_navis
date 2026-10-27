import html
import json
import logging
import os
import re
import time
import uuid
from typing import List, Tuple

from src.core.config_manager import ConfigManager
from src.core.core_task import BackgroundTask, TaskState
from src.core.device_manager import DeviceManager
from src.core.image_utils import IMAGE_EXTENSIONS, encode_data_url
from src.core.kb_manager import KBManager, DatabaseManager
from src.core.llm_errors import friendly_payload, strip_markers
from src.core.mcp_manager import MCPManager
from src.core.models_registry import get_model_conf, resolve_auto_model
from src.core.references import (CITE_LINK_RE, FOOTER_RULE_HTML,
                                 PROVENANCE_HEADER_HTML, ReferenceItem,
                                 ReferenceRegistry, citation_href)
from src.core.evidence import EvidenceStore, is_usable_source_text
from src.core.evidence_gate import apply_evidence_gate, gate_finder
from src.core.prompt_rules import (CITATION_TOKEN_RULE,
                                   LANGUAGE_AND_PUNCTUATION_RULE)
from src.core import plot_styles
from src.core.think_tags import strip as strip_think_blocks
from src.core.token_estimator import (estimate_message_tokens, estimate_tokens,
                                      resolve_context_window, derive_context_budgets)

logger = logging.getLogger(__name__)

# KB 检索注入治理：单 chunk 字符上限与总 token 预算**都**按主模型上下文窗口
# 动态推导（derive_context_budgets，含 kb_chunk_chars / kb_token_budget），使不同
# 模型各按自身容量注入——固定 1600 字符的 chunk 上限会让大窗口模型拿到一堆碎片。
# 窗口的解析顺序（用户声明 → 运行时实测 → litellm/家族表推断 → 保守默认）定义在
# src/core/model_capabilities.py，此处不再自带任何口径。被省略的文档仍写入
# sources_map，引用可点开原文。

# Human-in-the-loop 深度研究计划协议：确认卡（<deep_plan> 标记）由 UI 渲染，
# 用户确认/跳过后以哨兵前缀文本重新进入发送管线；任务端据此跳过重复分解
# 或关闭本轮 deep 模式。
_DEEP_PLAN_CONFIRMED_TAG = "[DEEP_PLAN_CONFIRMED]"
_DEEP_PLAN_SKIPPED_TAG = "[DEEP_PLAN_SKIPPED]"
#: 全文入库后自动续跑本轮的哨兵：PDF 已经作为私有证据入库，这一轮不再重复做
#: 自动文献检索（否则会拿"已获取全文：X"这种界面文案去搜文献）。


_RESEARCH_CONTEXT_HINTS = (
    "研究", "文献", "论文", "综述", "机制", "基因", "蛋白", "通路", "表达",
    "突变", "表型", "信号", "文献", "reference", "paper", "study", "literature",
    "mechanism", "gene", "protein", "pathway", "mutation", "phenotype",
)


def _looks_research_query(query: str) -> bool:
    low = str(query or "").lower()
    return any(hint in low for hint in _RESEARCH_CONTEXT_HINTS)



def _source_dedupe_key(meta):
    """引用来源归一键：URL/DOI 优先，其次 标题|年份，用于引用去重合并。

    同一来源在多轮工具调用（或 Deep Mode 多路并行子任务）中被重复收集时，
    归一键命中即复用既有引用号：参考文献列表不虚增、引用块更省 token，
    编号与实际来源一一对应（准确度↑）。无可归一字段时返回 None（按新来源处理）。

    字段覆盖说明：AgentRuntime._collect_tool_citations（本地技能与 MCP 共用）
    注入的在线来源用
    ``path`` 携带 URL、``name`` 携带标题；其他调用点可能用 ``url``/``title``，
    故全部纳入候选。
    """
    if not isinstance(meta, dict):
        return None
    for field in ("url", "path", "link", "href", "doi"):
        val = str(meta.get(field) or "").strip().lower().rstrip("/")
        if val:
            return val
    title = str(meta.get("title") or meta.get("name") or "").strip().lower()
    if title:
        year = str(meta.get("year") or "").strip()
        return f"{title}|{year}"
    return None

def _resolve_user_message_text(message: dict) -> Tuple[str, str]:
    """把一条用户消息拆成 ``(展示文本, 协议文本)``。

    协议哨兵只存在于 ``content``：``display_text`` 是"给用户看的那一份"，deep-plan
    确认轮尤其如此（content = ``[DEEP_PLAN_CONFIRMED]\\n计划``，display_text = 纯
    计划）。只读 display_text 会永远判不中哨兵，于是又走回"分解 → 出计划卡"，表现
    就是无限重复出卡；只读 content 又会让界面与历史里冒出协议前缀。故两者分开取：
    展示/检索用前者，协议判定用后者（非协议消息时为空串）。
    """
    message = message or {}
    content = str(message.get("content") or "")
    display = str(message.get("display_text") or content)
    protocol = (content if content.startswith((_DEEP_PLAN_CONFIRMED_TAG,
                                              _DEEP_PLAN_SKIPPED_TAG)) else "")
    return display, protocol


def _has_visible_answer(text: str) -> bool:
    """正文里是否真有"回答"（去掉思考块、过程块、控制标记与纯 HTML 后仍非空）。

    工具循环跑满却没有产出结论时，缓存里只剩思考与"我先检索…"的过程文本；若照原样
    上屏，用户看到的是一个只有页脚的空气泡——那是比报错更糟的结果。
    """
    body = strip_think_blocks(str(text or ""))
    body = re.sub(r"\[(?:CLEAR_SEARCH|START_LLM_NETWORK)\]", "", body)
    body = re.sub(r"<[^>]+>", "", body)
    return bool(body.strip())

def _absorb_usage(target, usage) -> None:
    """把一份 ``{"prompt_tokens", "completion_tokens", "estimated"}`` 累加进 ``target``。

    深度模式是"N 路并行子 Agent + 一次合成"：每路都有自己的 runtime 统计，不汇总
    的话外层 agent 用量恒为 0，UI 的 token 计数器整轮都不显示。
    """
    if not isinstance(target, dict) or not isinstance(usage, dict):
        return
    target["prompt_tokens"] = (int(target.get("prompt_tokens", 0) or 0)
                               + int(usage.get("prompt_tokens", 0) or 0))
    target["completion_tokens"] = (int(target.get("completion_tokens", 0) or 0)
                                   + int(usage.get("completion_tokens", 0) or 0))
    if usage.get("estimated"):
        target["estimated"] = True

def _registry_item_from_source(index, meta):
    """把 sources_map 的来源元数据规整为参考文献条目（单一映射定义）。

    本地 KB 文档（本地 path）与在线来源（path 为 http(s) URL）走同一入口，
    避免"来源 -> 条目"的转换散落多处造成字段不一致。

    支撑原文（snippet）只接受达到"可引用原文"门槛的文本（判据见
    :func:`src.core.evidence.is_usable_source_text`）：不足门槛的不是原文——典型
    是检索列表里的占位串——登记为**无原文**，UI 会明确显示"未记录支撑原文"，
    绝不把它当作 "Cited passage" 展示。命中门槛的原文直接来自程序持有的来源
    （工具结果 / KB chunk），未经模型转写，故标 ``verified``（UI 标注 verbatim）。
    """
    meta = meta or {}
    path = str(meta.get("path") or "")
    name = str(meta.get("name") or "")
    snippet = str(meta.get("search_text") or meta.get("snippet") or "").strip()
    if not is_usable_source_text(snippet):
        snippet = ""
    verified = bool(snippet)

    # 论文级元数据优先：自动检索 / 付费墙下载 / 用户上传都应生成
    # "论文"引用，而不是笼统的 local_document / 知识库。
    doi = str(meta.get("doi") or "").strip().replace("https://doi.org/", "")
    title = str(meta.get("title") or "").strip()
    authors = str(meta.get("authors") or "").strip()
    year = str(meta.get("year") or "").strip()
    journal = str(meta.get("journal") or "").strip()
    url = str(meta.get("url") or "").strip()
    kind = str(meta.get("kind") or "").strip()

    if path.lower().startswith(("http://", "https://")):
        kind = kind or "web"
        return ReferenceItem(index=index, title=title or re.sub(r"^\[Online\]\s*", "", name).strip() or path,
                             authors=authors, year=year, journal=journal, doi=doi,
                             url=url or path, snippet=snippet, kind=kind, verified=verified)

    if doi or url or title:
        return ReferenceItem(index=index, title=title or name or os.path.basename(path) or "Local paper",
                             authors=authors, year=year, journal=journal, doi=doi,
                             url=url or (f"https://doi.org/{doi}" if doi else ""),
                             snippet=snippet, path=path, page=int(meta.get("page") or 1),
                             kind=kind or "article", verified=verified)

    return ReferenceItem(index=index, path=path,
                         page=int(meta.get("page") or 1),
                         title=name or os.path.basename(path) or "Local document",
                         snippet=snippet, kind="local_document", verified=verified)

def _seed_registry_from_sources(registry, sources_map, evidence_store=None):
    """把 sources_map 中尚未登记的来源补录进注册表（幂等）。

    Deep 模式在合并阶段才把子任务来源写回 sources_map，故注册表同步不能只在
    检索后做一次；渲染前调用本函数，保证编号与来源一一对应。同时把来源原文
    （完整 chunk / 摘要）采集进逐字证据库，供 cite_references 的 snippet 校验。
    """
    if not sources_map:
        return
    if evidence_store is not None:
        # 证据采集先于注册表循环：即使来源已注册过（幂等跳过），原文仍需入库。
        for meta in sources_map.values():
            meta = meta or {}
            text = str(meta.get("search_text") or meta.get("snippet") or "")
            if text.strip():
                evidence_store.add_source(
                    text,
                    url=str(meta.get("path") or meta.get("url") or ""),
                    title=str(meta.get("name") or meta.get("title") or ""))
    for rid, meta in sources_map.items():
        if not isinstance(rid, int) or rid in registry:
            continue
        registry.seed(rid, _registry_item_from_source(rid, meta))

def _seed_registry_from_history(registry, messages):
    """把历史回答里已登记过的引用补录进本轮注册表（跨轮连续性）。

    注册表在每轮任务内新建，但模型在追问轮里会**复用上一条回答的编号**
    （``[1]`` 或原样粘贴的引用 token）。若不回灌，本轮注册表为空，这些编号
    既查不到条目、也渲染不出参考文献列表。

    历史条目的 ``references`` 字段就是"该轮已登记条目"的无损快照（由 UI 收尾时
    写入，见 ``ChatResponseFlowMixin._attach_references``）。此处按消息**从新到旧**
    回灌：编号在未被占用时沿用（最近一次回答的编号优先），与 KB/在线来源已有的
    编号冲突时退化为新编号，避免占用既有编号空间。

    :return: 回灌的条目数（供日志）。
    """
    if not messages:
        return 0
    seeded = 0
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        refs = msg.get("references")
        if not isinstance(refs, list):
            continue
        for entry in refs:
            if not isinstance(entry, dict):
                continue
            item = ReferenceItem.from_dict(entry)
            if not (item.title or item.url or item.doi or item.path):
                continue
            try:
                idx = int(entry.get("index") or 0)
            except (TypeError, ValueError):
                idx = 0
            if idx > 0 and idx not in registry:
                registry.seed(idx, item)
            else:
                registry.add(item)
            seeded += 1
    return seeded

_FOLLOWUP_ONLY_PATTERNS = (
    r"^(?:请|麻烦)?(?:用)?中文(?:回答|回复|说|解释|输出|重写|翻译)?(?:我|一下|一遍)?(?:啊|呀|吧|谢谢)?$",
    r"^(?:请|麻烦)?(?:用)?英文(?:回答|回复|说|解释|输出|重写|翻译)?(?:我|一下|一遍)?(?:啊|呀|吧|谢谢)?$",
    r"^(?:请)?(?:继续|接着|往下|继续说|接着说|展开|展开说|详细|再详细|具体|简单|简短|总结|重写|重新回答|翻译|translate|answer\s*in\s*(?:chinese|english)|in\s*(?:chinese|english)|thanks|thank\s*you|ok|好的|可以|谢谢|收到|明白|知道了)(?:一下|一点|说说|讲|啊|呀|吧|谢谢)?$",
)


def _is_followup_only_query(text: str) -> bool:
    """判断“中文回答我啊/继续/详细一点”这类纯追问，而不是新检索主题。

    这类消息如果照常走自动文献检索，会把“中文回答我啊”当成学术 query，
    检索回来一堆语言学/教育学好文，直接把上一轮植物学引用替换掉。
    """
    raw = str(text or "").strip()
    if not raw or len(raw) > 40:
        return False
    compact = re.sub(r"\s+", "", raw).strip("，。！？,.!?;；:：\"'“”‘’")
    if not compact:
        return True
    return any(re.fullmatch(p, compact, flags=re.IGNORECASE)
               for p in _FOLLOWUP_ONLY_PATTERNS)

def _trim_history_for_budget(history, token_budget):
    """按轮次边界裁剪历史消息至 token 预算内（从最新往回保留）。

    长会话全量回传是 Agent 循环中增长最快的 token 开销（每轮工具循环都要
    重发全部历史）。按 user 消息为界的"轮"从新到旧贪心保留：
    - 整轮取舍，不拆散 assistant(tool_calls) 与 tool 结果的配对（API 约束）；
    - 至少保留最新一轮（对话连续性下限）；
    - 预算内原样返回（短会话零行为变化，仅长会话生效）。
    """
    if not history:
        return history

    def _tokens(msgs):
        return sum(estimate_message_tokens([m]) for m in msgs)

    total = _tokens(history)
    if total <= token_budget:
        return history

    rounds, current = [], None
    for m in history:
        if m.get("role") == "user":
            if current:
                rounds.append(current)
            current = [m]
        else:
            if current is None:
                current = []
            current.append(m)
    if current:
        rounds.append(current)

    kept, used = [], 0
    for r in reversed(rounds):
        r_tokens = _tokens(r)
        if kept and used + r_tokens > token_budget:
            break
        kept.insert(0, r)
        used += r_tokens

    flat = [m for r in kept for m in r]
    # 防御：头部残留 assistant/tool 消息（无 user 起始的孤立轮）会导致部分
    # API 报"tool 结果无对应 tool_calls"，裁到首个 user/system 为止。
    while flat and flat[0].get("role") not in ("user", "system"):
        flat.pop(0)
    logger.info(
        f"History trimmed for context budget: {len(history)} -> {len(flat)} "
        f"messages (~{total} -> ~{used} tokens).")
    return flat

def _kb_retrieval_core(kb_id, search_query, main_model_name, history_context="",
                       main_base_url="", declared_window=None):
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

    _kb_limits = derive_context_budgets(
        resolve_context_window(main_model_name, main_base_url, declared_window))
    _kb_budget = _kb_limits["kb_token_budget"]
    _kb_chunk_chars = _kb_limits["kb_chunk_chars"]
    context_str = ""
    sources_map = {}
    current_ref_id = 1
    kb_tokens = 0
    for doc in final_docs:
        sources_map[current_ref_id] = {
            "path": doc['metadata'].get('file_path', ''),
            "page": doc['metadata'].get('page', 1),
            "name": doc['metadata'].get('source', 'Local DB'),
            # 完整 chunk 原文（不再截断）：引用条目的 snippet 必须是来源逐字内容。
            "search_text": doc['content'],
        }
        chunk = doc['content']
        if len(chunk) > _kb_chunk_chars:
            chunk = chunk[:_kb_chunk_chars] + "...[chunk truncated]"
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

def _kb_retrieval_worker(kb_id, search_query, main_model_name, out_path, history_context="",
                         main_base_url="", declared_window=None):
    """独立子进程入口：隔离本地 ONNX 推理。执行权威检索逻辑并把结果写 out_path。
    JSON 的 key 只能是字符串，写入前把整型 sources_map 键转字符串，父进程读回时复原。

    ``main_base_url`` / ``declared_window`` 与父进程同口径传入：KB 注入预算按主模型
    窗口推导，而窗口解析需要 (base_url, 声明值) 才能命中能力表的实测/声明记录。
    """
    import json
    import logging
    _log = logging.getLogger("KB.SubProc")
    try:
        from src.task.kb_tasks import _setup_worker_env
        _setup_worker_env()
        context_str, sources_map, _domain = _kb_retrieval_core(
            kb_id, search_query, main_model_name, history_context,
            main_base_url, declared_window)
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
                f"tokens, citation tokens / [n] markers and code blocks remain ASCII "
                f"unchanged.\n"
            )
        return (
            f"### OUTPUT LANGUAGE (MANDATORY):\n"
            f"The user's query was automatically translated to English for tool "
            f"retrieval, but the user originally wrote in {reply_lang}. You MUST compose "
            f"your final answer in {reply_lang} (do NOT reply in English), and use that "
            f"language's native punctuation. Structural protocol tokens, citation "
            f"tokens / [n] markers and code blocks remain ASCII and unchanged.\n"
        )

    def cancel(self):
        super().cancel()
        if hasattr(self, 'main_llm') and self.main_llm: self.main_llm.cancel()
        if hasattr(self, 'trans_llm') and self.trans_llm: self.trans_llm.cancel()
        if hasattr(self, 'vision_llm') and self.vision_llm: self.vision_llm.cancel()

    def _main_model_facts(self):
        """主模型能力事实：``(模型名, base_url, 用户声明的窗口)``，预算推导的唯一入口。

        模型名必须走 :meth:`ConfigManager.effective_model_name`——``main_config``
        来自模型选择器，通常已带正确模型名，但 API server 等入口传进来的可能是
        ``llm_config.json`` 里的 provider 条目（``model_name`` 往往为空）。直接读
        条目会让窗口退化成兜底默认（远程 300K / 本地 32K）、历史被砍到零头，表现为"明明是大窗口模型，
        程序只喂一点点"。``context_window`` 是用户显式声明的窗口（可选），优先级
        高于一切推断，见 :mod:`src.core.model_capabilities`。
        """
        cfg = self.main_config or {}
        config = getattr(self, "config", None)
        if config is not None:
            name = config.effective_model_name(cfg)
        else:
            name = str(cfg.get("model_name") or "").strip()
        return name, str(cfg.get("base_url") or ""), cfg.get("context_window")

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

    def _turn_elapsed_ms(self) -> int:
        """本轮对话已耗时（毫秒）；未开始计时时返回 0。

        计时起点在 :meth:`_execute` 入口（``self._turn_t0``），随用量事件一并上报，
        由 UI 显示在 token 标签旁；语义是"从任务开始到本次上报"的墙钟时间（含附件
        处理、检索与模型生成）。
        """
        t0 = getattr(self, "_turn_t0", None)
        return int((time.perf_counter() - t0) * 1000) if t0 else 0

    def _execute(self):
        from src.core.llm_impl import OpenAICompatibleLLM, get_cached_translation

        # 本轮墙钟计时起点：用量事件里一并回传 elapsed_ms，UI 在 token 旁显示"本轮耗时"
        self._turn_t0 = time.perf_counter()

        # 新对话发送：复位鉴权拒绝熔断计数（用户规格——熔断只在本轮对话内
        # 生效，"发送新对话"即重置点，而非清除聊天记录）。
        from src.core.network_worker import auth_breaker
        auth_breaker.reset()

        self.send_log("INFO", f"Chat task started. KB_ID: {self.kwargs.get('kb_id')}")
        time.sleep(0.1)

        self.main_config = self.kwargs.get('main_config')
        self.trans_config = self.kwargs.get('trans_config')
        self.messages = self.kwargs.get('messages', [])
        self.kb_id = self.kwargs.get('kb_id')
        # 所属对话 id：会话级私有文档（用户上传的 PDF）按它隔离。
        self.conversation_id = str(self.kwargs.get('conversation_id') or "")
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

        # 展示文本与协议文本分开取（见 _resolve_user_message_text）：哨兵只在
        # content 里，display_text 是给用户看的那一份。
        original_user_query, protocol_text = _resolve_user_message_text(
            self.messages[-1] if self.messages else {})

        try:
            from src.core.lang_detect import detect_primary_language
            # 语言探测不能用协议轮的消息文本（确认轮的内容是英文子任务清单，会把
            # 回复语言误判为英文）：回溯最近一条自然用户消息。
            lang_probe = (self._find_last_natural_user_text() or original_user_query) \
                if protocol_text else original_user_query
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

        retrieval_query = original_user_query
        search_query = original_user_query
        domain = "General Academic"
        context_str = ""
        sources_map = {}
        # 本轮回答的参考文献注册表：本地 KB 文档 / 在线来源 / cite_references 工具
        # 提交的条目共用同一编号空间，正文 [n] 与文献列表恒一一对应。任务每轮新建，
        # 历史已登记条目在渲染前由 _seed_registry_from_history 回灌（跨轮连续性）。
        reference_registry = ReferenceRegistry()
        # 会话级逐字证据库：收集工具结果 / KB chunk / 附件中的原文，强制
        # cite_references 提交的 snippet 为来源逐字内容（见 runtime 的校验逻辑）。
        evidence_store = EvidenceStore()

        # ---- Human-in-the-loop 协议轮：deep plan 确认 / 跳过哨兵解析 ----
        deep_plan_confirmed = None
        if protocol_text.startswith(_DEEP_PLAN_CONFIRMED_TAG):
            deep_plan_confirmed = self._parse_confirmed_deep_plan(protocol_text)
            if deep_plan_confirmed:
                # KB 检索查询改用确认后的子任务合集（原文哨兵文本不可直接检索）
                search_query = " ".join(t.query for t in deep_plan_confirmed)
            self.send_log("INFO",
                          f"Deep plan confirmed by user with {len(deep_plan_confirmed or [])} sub-task(s).")
        elif protocol_text.startswith(_DEEP_PLAN_SKIPPED_TAG):
            original_user_query = protocol_text[len(_DEEP_PLAN_SKIPPED_TAG):].strip()
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

        # Phase 2: 自动文献证据（全局库）优先；没有命中再回退到用户手选的 KB。
        # 纯追问（“中文回答我啊/继续/详细一点”）沿用历史文献，不发起新检索；
        # 否则会把追问本身当学术 query，抓回一堆无关论文并替换上一轮引用。
        followup_only = (
            _is_followup_only_query(search_query)
            and any(isinstance(m, dict)
                    and m.get("role") == "assistant"
                    and str(m.get("content") or "").strip()
                    for m in (self.messages or []))
        )
        if followup_only:
            self.logger.info(
                "Follow-up-only query detected; relying on conversation history "
                "instead of running a new literature search.")
        # 本地 KB：只有用户显式挂了一个库时才走。本地 embedding/rerank 属重量级
        # ONNX/torch 推理，统一交给子进程隔离。
        if self.kb_id and not followup_only:
            self.send_log("INFO", "Initiating local Vector RAG retrieval...")
            self._emit_token("[CLEAR_SEARCH]")
            self._emit_token(
                "<div class='status-msg' style='color:#05B8CC; margin-bottom:4px;'>Searching local knowledge base and reranking documents...</div>\n\n")
            time.sleep(0.05)

            kb_info = self.kb_manager.get_kb_by_id(self.kb_id)
            if kb_info and kb_info.get('doc_count', 0) == 0:
                self.logger.warning(f"Knowledge Base '{kb_info.get('name')}' is empty. Skipping vector retrieval.")
            elif kb_info:
                domain = kb_info.get('domain', 'General Academic')
                self._emit_token(
                    "<div class='status-msg' style='color:#05B8CC; margin-bottom:4px;'>Loading local vector model and retrieving literature...</div>\n\n")
                context_str, sources_map, domain = self._run_kb_retrieval(search_query, domain)
                # KB 文档编号（1..N）统一进注册表：与 cite_references 的编号空间合并。
                _seed_registry_from_sources(reference_registry, sources_map, evidence_store)

        if not context_str.strip():
            context_str = "No local database documents provided."

        external_chunks = self.external_context or []
        images = [c for c in external_chunks if c.get("type") == "image" or str(c.get("path", "")).lower().endswith(
            IMAGE_EXTENSIONS)]
        docs = [c for c in external_chunks if c not in images]

        # 附件文档正文进入逐字证据库：模型引用用户上传文件内容时可校验。
        for d in docs:
            content = str(d.get("content") or "")
            if content.strip():
                evidence_store.add_source(content, title=str(d.get("name") or ""))

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
        # 追问建议改为结构化产出（工具调用），不再依赖模型输出固定文本 + 正则解析
        combined_tools.append(dict(_ALWAYS_TOOLS)["suggest_follow_ups"])
        # 参考文献改为结构化产出：模型只提交条目，编号与文献列表由程序生成
        combined_tools.append(dict(_ALWAYS_TOOLS)["cite_references"])
        # 付费墙全文：只有摘要、而结论依赖正文时，向用户要一次 PDF（同轮会合等待，
        # 拿到后由抽取子代理给要点），比让模型凭摘要硬答可靠。
        combined_tools.append(dict(_ALWAYS_TOOLS)["request_fulltext"])

        # R 可视化属于**默认能力**：不再作为可勾选的独立技能，也不受技能标签筛选
        # 影响——始终把 plot_chart 交给模型，由它自行判断本次是否需要画图。
        from src.core.skill_manager import SkillManager

        plot_schema = SkillManager.get_instance().academic_schemas.get("plot_chart")
        if plot_schema and not any(
                (t.get("function") or {}).get("name") == "plot_chart" for t in combined_tools):
            combined_tools.append(plot_schema)

        if combined_tools:
            self._emit_token("<mcp_process>⚙️ Query intent analyzed — the model selects tools natively...</mcp_process>")
            tool_names = [t.get("function", {}).get("name", "Unknown") for t in combined_tools]
            # 当绘图工具可用时，追加一条强约束，避免 reasoning 模型把 chart 参数
            # 以纯文本形式“泄漏”到最终答案里，而不是真正调用 plot_chart。
            plot_guard = ""
            if any(name == "plot_chart" for name in tool_names):
                plot_guard = (
                    "\n### DATA VISUALIZATION (plot_chart / propose_plot_plan):\n"
                    "Chart rendering is a built-in capability that is always available — judge for yourself "
                    "whether a figure serves the request (bubble, bar, scatter, volcano, heatmap, "
                    "GO enrichment, etc.). When you do decide to draw one, you MUST render it by calling the "
                    "plot_chart tool. NEVER reply with chart parameters, the data table, or a chart "
                    "specification as plain text.\n"
                    "IMPORTANT: If the user asks to visualize/plot data but has NOT clearly specified the "
                    "chart type, the x/y columns, the title, or styling, call the propose_plot_plan tool "
                    "FIRST to show a confirmation card. Only call plot_chart after the user confirms the plan.\n"
                    "DEFAULT FIGURE STYLE: every chart is rendered in the built-in reference style "
                    "documented in the plot_chart tool description (journal canvas size, theme, palette and "
                    "layout rules). That style is a BASELINE, not a constraint: whenever the user requests a "
                    "specific chart type, palette, theme, layout or annotation, follow the user's requirement "
                    "instead and pass it through the style / palette / theme parameters. "
                    f"({plot_styles.STYLE_REFERENCE})\n"
                    "ENRICHMENT DOTPLOT REFERENCE LAYOUT (KEGG / GO / GSEA / Reactome bubble plots): "
                    "chart_type='bubble', x=Gene Ratio plotted HORIZONTALLY at the bottom, y=Pathway/Term "
                    "name on the left ordered by Gene Ratio DESCENDING (largest ratio on TOP), size=Gene "
                    "Count, color=FDR (BH-corrected p-value) on the red-to-blue ramp (small FDR = red); "
                    "the right-side legend shows a color bar labeled 'FDR' plus a 'Count' size legend. "
                    "Do NOT coord_flip; do NOT use -log10(FDR) for the color mapping (use the raw FDR "
                    "column directly so the gradient matches the reference).\n"
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
            "1. LITERATURE RETRIEVAL (CRITICAL): Whenever the question needs published evidence, you MUST invoke search_academic_literature BEFORE answering — never answer from memory and NEVER fabricate citations, DOIs or author lists. Search BROADLY: run SEVERAL differently-worded English queries (synonyms, field jargon, method or author names) instead of one, keep source='auto' so OpenAlex / Crossref / PubMed / Semantic Scholar are all covered (PNAS, Nature, Science, Cell class journals are indexed there — do NOT narrow 'source' unless the user asks for one database), and raise max_results when the topic is wide. Then follow the trail with traverse_citation_graph on the key paper to catch landmark work that keyword search misses.\n"
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
            "the run automatically; the user's answer arrives as the next user message.\n"
            "### FULL TEXT (fetch_paper_fulltext TOOL — CONDITIONAL MUST):\n"
            "You MUST call fetch_paper_fulltext with the paper's DOI BEFORE writing any claim that depends "
            "on method steps, sample sizes, statistics, thresholds, figure-level details or verbatim "
            "wording — an abstract cannot provide those, so an abstract-only answer for such a claim counts "
            "as a failed retrieval. Search results already carry 'full_text' (+ 'full_text_source') for the "
            "most citable hits when an open-access body exists: use that body text directly for those papers "
            "and skip the extra call. For papers whose claims need only their headline finding, the abstract "
            "is acceptable. If the tool returns no body, read its 'conclusion' field: "
            "'no_open_access_copy' means the paper genuinely has no open-access version, while "
            "'incomplete_sources_failed' means the lookup itself was incomplete — never present the second "
            "as the first. In either case, when a claim needs the body, do NOT stop at the abstract: go to "
            "PAYWALLED FULL TEXT below and offer the user the manual route. Copy supporting snippets from "
            "the body VERBATIM. NEVER invent body content.\n\n"
            "### PAYWALLED FULL TEXT (request_fulltext TOOL — MUST OFFER):\n"
            "Open-access lookup only covers free copies. When fetch_paper_fulltext returns NO body (either "
            "conclusion) AND a claim you must support depends on the body (methods, sample sizes, statistics, "
            "thresholds, figure-level wording), you MUST call request_fulltext with that paper's DOI/title "
            "BEFORE falling back to the abstract — a paywalled paper is often readable through the user's own "
            "subscription, and this tool is how they hand it over. A picker window opens on the user's screen: "
            "they download the PDF and hand it over, and this call BLOCKS until then — it returns a reading "
            "brief (key points + verbatim quotes + page numbers) that a reading sub-agent extracted from that "
            "body. Quote those passages verbatim in your cite_references snippets. Rules: only after "
            "fetch_paper_fulltext returned no body; at most ONCE per paper per turn; do not call any other tool "
            "while it is pending; if it returns 'unavailable', keep the abstract-only conclusion and say "
            "plainly that the full text could not be obtained in this run. Never explain a missing body as a "
            "paywall unless the tool reported 'no_open_access_copy' — with 'incomplete_sources_failed' the "
            "cause is unknown.\n\n"
            "### RESPONSE GUIDELINES & CITATION PROTOCOL:\n"
            f"1. IN-TEXT CITATIONS (STRICT): {CITATION_TOKEN_RULE} Attach the token inline IMMEDIATELY after the claim it supports, and NEVER claim a fact without an inline citation.\n"
            f"2. FORMAL BIBLIOGRAPHY (MANDATORY — use the cite_references TOOL): For every source you cite that carries no 'citation_token' and no pre-assigned number, you MUST register it by calling cite_references ONCE, passing its bibliographic fields (title, authors, year, journal, doi, url, and the supporting 'snippet'); the tool returns a 'citation_token' for each entry — paste that token at the claim it supports. Sources that already carry a token or a number (knowledge-base '--- [Document n] ---' entries, tool results with '{AgentRuntime.TOOL_CITE_TOKEN_FIELD}') are already registered by the app — reuse THAT token/number and do NOT register them again. The 'snippet' MUST be a VERBATIM excerpt copied character-for-character from a tool result or from the provided Context (the paper's abstract or body text) — NEVER paraphrase, summarize, or write it from memory; the app verifies it against captured sources and replaces or drops non-verbatim passages. Every number is assigned by the app: do NOT invent, shift or renumber one, and never fall back to an ASCII alias such as [lariguet2004].\n"
            "3. NEVER WRITE A REFERENCES SECTION YOURSELF: do NOT output a 'References' / 'Bibliography' heading or a manually numbered citation list in your answer text. The app generates that list from your cite_references call; writing one yourself duplicates it and can contradict it.\n\n"
            "4. ZERO HALLUCINATION (CRITICAL): You MUST NOT fabricate, extrapolate, or infer information that is not explicitly present in the provided Context or Tool Results. If the provided data is insufficient to address the query, you MUST explicitly state: 'The provided context does not contain sufficient information to address this inquiry.' Under no circumstances should internal training data be utilized to circumvent contextual gaps.\n\n"
            f"{LANGUAGE_AND_PUNCTUATION_RULE}\n\n"
            "### FOLLOW-UP SUGGESTIONS (MANDATORY):\n"
            "At the very end of your response — after ALL other content — you MUST call the "
            "suggest_follow_ups tool with exactly 6 follow-up questions, each carrying the tag of "
            "the angle it explores:\n"
            "   - [Deep Dive] question about specific details or mechanisms\n"
            "   - [Critical] question about limitations, alternatives, or weaknesses\n"
            "   - [Broader] question about implications or future trends\n"
            "   - [Brainstorm] a creative brainstorming question or hypothetical \"what if\" scenario\n"
            "   - [Similar] question connecting to a similar or parallel topic/concept\n"
            "   - [Application] question about real-world applications or cross-disciplinary use\n"
            "Write every question in the user's language. The suggestions travel through the tool "
            "call ONLY: never print them as text and never emit a [FOLLOW_UPS] block or any other "
            "marker — the question list must not appear anywhere in your answer body. Even for short "
            "or negative answers, still register at least 3 follow-ups.\n\n"
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
                    # 历史里的思考/工具过程不是回答内容：剥掉后再发给 provider，
                    # 避免工具参数/搜索结果里的词触发内容审查 400。
                    content = strip_think_blocks(content)
                    content = re.sub(r"\[Status:\s*Executing\][^\n]*", "",
                                     content, flags=re.IGNORECASE)
                msg = {"role": m["role"], "content": content}
                if m.get("tool_calls"): msg["tool_calls"] = m["tool_calls"]
                if m.get("tool_call_id"): msg["tool_call_id"] = m["tool_call_id"]
                if m.get("name"): msg["name"] = m["name"]
                clean_history.append(msg)

        # ---- 历史预算裁剪：按主模型窗口推导历史 token 预算，超限按轮次
        # 从旧到新丢弃（保留最新完整轮）。短会话零影响；长会话显著降低
        # Agent 循环每轮重发历史的 token 开销，单 Agent / Deep 模式均受益。
        try:
            _model_name, _base_url, _declared_window = self._main_model_facts()
            _hist_budget = derive_context_budgets(
                resolve_context_window(_model_name, _base_url, _declared_window)
            )["history_token_budget"]
            clean_history = _trim_history_for_budget(clean_history, _hist_budget)
        except Exception as e:
            self.logger.warning(f"History budget trim skipped: {e}")

        rag_messages = [{"role": "system", "content": system_prompt}] + clean_history
        rag_messages.append({"role": "user", "content": llm_content})

        # ---- Run the modern Agent loop (plan -> execute -> observe) ----

        def _cite_collector(source_meta: dict):
            """Register an online MCP source for the 'Cited Sources' UI block.

            编号统一由参考文献注册表分配（不再单独用 100+ 偏移），因此在线来源
            与 KB 文档、模型登记的文献共享同一编号空间，绝不会出现"两个来源同一个
            号"。按归一键去重：同一来源重复收集时复用既有引用号，列表不虚增。
            """
            key = _source_dedupe_key(source_meta)
            if key:
                for rid, registered in sources_map.items():
                    if _source_dedupe_key(registered) == key:
                        return rid
            ref_id = reference_registry.add(_registry_item_from_source(0, source_meta))
            sources_map[ref_id] = source_meta
            return ref_id

        # 成文后补来源那一轮的判定入口（规则与"没有来源就不空转"都在
        # src.core.evidence_gate；深度研究里每个子任务用同一个工厂传自己的登记表）。
        _evidence_unsupported = gate_finder(sources_map)

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
                reference_registry=reference_registry,
                evidence_store=evidence_store,
            )
            # 付费墙全文的同轮会合通道：弹窗 -> 用户下载/选择 PDF -> 唤醒本线程。
            agent.fulltext_waiter = self._wait_for_fulltext
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
                    evidence_store=evidence_store,
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
                    evidence_unsupported=(
                        _evidence_unsupported if _looks_research_query(search_query) else None),
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

            follow_ups = getattr(agent, "last_follow_ups", None)
            if follow_ups:
                agent.last_follow_ups = None
                self.send_log("INFO", f"Follow-up suggestions dispatched: {len(follow_ups)} item(s)")
                self._emit_state(TaskState.PROCESSING, -1, "", payload={
                    "event": "follow_ups",
                    "data": follow_ups,
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
                    "elapsed_ms": self._turn_elapsed_ms(),
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
                "elapsed_ms": self._turn_elapsed_ms(),
            })

        # Phase 6: 参考文献收口（**编号在这一刻定稿**）
        # 生成过程中所有引用标记都不展示（见 references.strip_citation_markers），
        # 因此"用户看到的编号"只在这里产生：按正文首次出现顺序分配 1..K，正文就
        # 地改写，列表按同一顺序渲染。工具侧的内部号只在模型与程序之间流转。
        _seed_registry_from_sources(reference_registry, sources_map, evidence_store)
        # 跨轮引用连续性：仅在**本轮没有新来源**时回灌历史引用。
        # 如果本轮已有自动检索/知识库来源，历史里另一主题的 [8]/[9] 会占用
        # 相同编号，模型复述旧编号时就会被渲染成完全无关的论文（例如植物学
        # 回答末尾出现文学/电影文献）。有当前来源时只认当前来源，宁可为旧编号
        # 报缺失，也不能把错误论文挂上去。
        restored = 0
        if not sources_map:
            restored = _seed_registry_from_history(reference_registry, self.messages)
        if restored:
            self.send_log(
                "INFO",
                f"Historical references restored for cross-turn continuity: {restored} item(s).")
        # Agent 的返回缓存里混有 UI 控制标记（[CLEAR_SEARCH]/[START_LLM_NETWORK] 等，
        # 由 AgentRuntime._emit 一并计入）；作为"上屏正文"前必须先剥离，否则这些
        # 字面量会在整段替换时漏成正文。全角编号（［3］/【3】）由定稿函数归一。
        display_text = re.sub(
            r"\[(?:CLEAR_SEARCH|START_LLM_NETWORK)\]", "", self.full_response_cache or "")
        # 编号定稿 + 正文改写（含思考块内的同一套编号；全角编号一并归一）。
        new_text, cited, unmatched = reference_registry.renumber_by_appearance(display_text)
        # 空回答兜底：整轮都在检索/思考、没有产出结论时明确告诉用户，而不是留一个
        # 只有页脚的空气泡（也没有"被引用的条目"可列）。
        if not _has_visible_answer(new_text):
            self.send_log("WARNING", "No final answer text was produced this turn.")
            if "Chinese" in str(getattr(self, "reply_lang", "")):
                new_text = ("**本轮没有产出最终回答**：模型一直在检索与思考，没有给出结论。"
                            "可以重新发送这个问题，或把它拆成一个更小的子问题再试。")
            else:
                new_text = ("**No final answer was produced this turn**: the model kept searching "
                            "and reasoning without concluding. Resend the question, or narrow it "
                            "to a single sub-question.")
            cited = []
            unmatched = []
        # 正文出现、注册表却没有的编号：如实记日志并从正文抹掉（多为模型自拟数字
        # 或历史残留）。不补条目——凭空造一条著录就是捏造文献。
        if unmatched:
            self.send_log(
                "WARNING",
                "Inline citation number(s) with no registered source (removed from the answer): "
                + ", ".join(f"[{n}]" for n in unmatched[:20]))
        # 证据门禁：有来源时标出无引用的高风险段落；没有来源时明确标注未核实。
        new_text = apply_evidence_gate(
            new_text, bool(sources_map or cited),
            research_context=_looks_research_query(search_query))
        self.full_response_cache = new_text
        if cited:
            self.full_response_cache = new_text + reference_registry.render_items(cited)
            self.send_log(
                "INFO",
                f"References rendered: {len(reference_registry)} registered, "
                f"{len(cited)} cited (numbered 1..{len(cited)} by first appearance).")
        elif len(reference_registry):
            self.send_log("INFO", "No cited reference matched the registry; reference block skipped.")
        # 结构化收口：把正文实际引用到的条目送到悬停卡/详情面板与历史快照。
        # 无论是否有引用都回传正文——证据门禁可能已给段落追加标记，只改缓存不改
        # 上屏会让"屏幕所见"与"落历史/导出"两处不一致。
        self._emit_state(TaskState.PROCESSING, -1, "", payload={
            "event": "answer_final",
            "text": self.full_response_cache,
            "references": [it.to_dict() for it in cited],
        })

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
                "\n" + FOOTER_RULE_HTML
                + PROVENANCE_HEADER_HTML
                + f"<div style='margin-top:6px; font-size:13px;'>"
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

    def _wait_for_fulltext(self, request: dict):
        """挂起本线程，等用户把 PDF 交进来；返回 ``(ok, path, note)``。

        界面收到 ``fulltext_request`` 事件后弹窗（选择/拖拽为主，约定目录兜底），用户交回
        文件即经 :mod:`src.core.fulltext_bridge` 唤醒这里。等待期间本轮**尚未结束**：计划、
        已检索到的来源、引用注册表、思考过程全留在上下文里，不存在"新起一轮"的信息断层。
        """
        from src.core import fulltext_bridge

        handle = fulltext_bridge.begin(self.conversation_id)
        if handle is None:
            return False, "", "no conversation context"
        self._emit_state(TaskState.PROCESSING, -1, "", payload={
            "event": "fulltext_request",
            "mode": "wait",
            "data": dict(request or {}),
        })
        self.send_log("INFO", "Waiting for the user to hand over the PDF full text...")
        return fulltext_bridge.wait(
            self.conversation_id, handle,
            timeout=fulltext_bridge.DEFAULT_TIMEOUT,
            cancel_check=self.is_cancelled)

    def _run_deep_agent(self, agent, search_query, rag_messages, system_prompt,
                        candidate_tools, llm_content, skill_mgr, mcp_mgr,
                        planner, sources_map, evidence_store=None,
                        confirmed_plan=None):
        """Deep research: decompose into parallel sub-investigations.

        Args:
            confirmed_plan: 用户确认后的 SubTask 列表（来自 deep-plan 确认卡）。
                非空时跳过分解直接执行；为 None 时先出计划卡等待用户确认。
            evidence_store: 会话级逐字证据库。子 Agent 的 citation 校验依赖它，必须
                显式传入——它只存在于主流程的局部作用域，靠名字是取不到的。
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
                    evidence_unsupported=gate_finder(sources_map),
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
                    evidence_unsupported=gate_finder(sources_map),
                )
            # Human-in-the-loop：先出计划确认卡并结束本轮，用户确认后以
            # [DEEP_PLAN_CONFIRMED] 哨兵重进管线；避免方向跑偏时白烧
            # N 路并行子 Agent 的 token。
            self._emit_token("[CLEAR_SEARCH]")
            marker = self._propose_deep_plan(search_query, decomp)
            waiting = self._deep_plan_waiting_text()
            self._emit_token(waiting)
            # 结构化事件：通知 UI 进入"等待用户确认计划"状态（锁定通用发送）。
            self._emit_state(
                TaskState.PROCESSING, -1, "",
                payload={"event": "await_user", "source": "deep_plan"})
            self.send_log("INFO",
                          f"Deep plan proposed ({len(decomp.sub_tasks)} sub-tasks); waiting for user confirmation.")
            # 标记必须随正文一起返回：本分支在毫秒内结束，UI 的渲染节拍（60ms）
            # 都来不及触发，收口时整段替换正文就会把只上过屏的标记冲掉。
            return marker + waiting

        self.send_log("INFO", f"Deep mode: launching {len(sub_tasks)} parallel sub-investigation(s).")
        self._emit_token(
            f"<mcp_process>⚙️ Deep research: {len(sub_tasks)} parallel sub-investigations launched...</mcp_process>\n"
        )

        # ---- 并行执行各子任务 ----
        # 子 Agent 无法触达用户：从其工具池剔除 ask_user，避免子任务发起
        # 无人应答的暂停式提问。
        # 同时剔除 cite_references：子任务的编号需要在合并阶段重映射到全局编号，
        # 而重映射只覆盖工具来源（sources_map）；子任务自行登记的文献编号会与全局
        # 冲突，故统一由主流程/合成阶段负责文献登记。
        sub_candidate_tools = [
            t for t in (candidate_tools or [])
            if (t or {}).get("function", {}).get("name")
            not in ("ask_user", "cite_references", "request_fulltext")
        ]
        results = [None] * len(sub_tasks)

        def _run_sub(idx, st):
            local_sources = {}

            def _local_cite(meta):
                """子任务局部引用注册（带去重，与主路径 _cite_collector 同键）。"""
                key = _source_dedupe_key(meta)
                if key:
                    for rid, registered in local_sources.items():
                        if _source_dedupe_key(registered) == key:
                            return rid
                rid = max((k for k in local_sources if isinstance(k, int)), default=100) + 1
                local_sources[rid] = meta
                return rid

            # 兄弟任务上下文：告知本子 Agent 其他并行子任务的分工，减少重复
            # 检索与内容重叠，各路覆盖不同侧面（广度↑、综合更干净、token↓）。
            siblings = "\n".join(
                f"- {s.query}" for j, s in enumerate(sub_tasks) if j != idx)
            query_text = f"User Query:\n{st.query}"
            if siblings:
                query_text += (
                    "\n\nSibling sub-investigations (handled by other parallel agents; "
                    "do NOT answer them here — stay focused on the query above and "
                    "avoid redundant searches already covered by siblings):\n" + siblings)

            sub_llm_content = list(llm_content[:-1]) + [
                {"type": "text", "text": query_text}
            ]
            sub_rag = [dict(m) for m in rag_messages]
            sub_rag[-1] = dict(sub_rag[-1])
            sub_rag[-1]["content"] = sub_llm_content

            sub_agent = AgentRuntime(
                self.main_llm, skill_mgr, mcp_mgr, planner=planner,
                cite_collector=_local_cite, log_fn=self.send_log,
                evidence_store=evidence_store,
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
                    # 深度研究同样要能取全文：判定表用本子任务自己的 local_sources
                    # （各子任务各登记一份，互不串味，也不与主线程的 sources_map 竞争）。
                    evidence_unsupported=gate_finder(local_sources),
                )
            except Exception as e:
                self.logger.warning(f"Sub-task '{st.query[:40]}' failed: {e}")
                text = f"[Sub-investigation unavailable: {e}]"
            return idx, text, local_sources, dict(getattr(sub_agent, "last_usage", {}) or {})

        with ThreadPoolExecutor(max_workers=len(sub_tasks)) as pool:
            futures = [pool.submit(_run_sub, i, st) for i, st in enumerate(sub_tasks)]
            done_count = 0
            for fut in as_completed(futures):
                try:
                    idx, text, local_sources, sub_usage = fut.result()
                    results[idx] = (text, local_sources)
                    # 子 Agent 的用量在主线程累加，避免多线程同时改同一个 dict
                    _absorb_usage(getattr(agent, "last_usage", None), sub_usage)
                    # 完成即上屏一行状态：消除"确认计划后长时间黑箱等待"，
                    # 用户可提前感知各路进度（as_completed 顺序不定，标注原编号）。
                    done_count += 1
                    heading = html.escape(str(sub_tasks[idx].query))[:80]
                    self._emit_token(
                        f"<div class='status-msg' style='color:#05B8CC;'>"
                        f"✓ Sub-investigation {idx + 1}/{len(sub_tasks)} completed "
                        f"({done_count}/{len(sub_tasks)} done): {heading}</div>\n")
                except Exception as e:
                    self.logger.warning(f"Sub-task future failed: {e}")

        # ---- 合并引用并重映射（跨子任务去重 + 未引用剔除）----
        next_id = max((k for k in sources_map if isinstance(k, int)), default=0) + 1
        merged = []
        plot_markers = []  # 收集子任务产生的 <rplot_card> 标记，避免 base64 污染合成器
        # 归一键 -> 全局引用号：单 Agent 阶段（KB/在线工具）已注册的来源先入
        # 索引，多个子任务重复发现同一来源时直接复用其引用号，参考文献不虚增。
        global_keys = {}
        for gid, gmeta in sources_map.items():
            _gk = _source_dedupe_key(gmeta)
            if _gk and _gk not in global_keys:
                global_keys[_gk] = gid
        new_ids = []  # 本轮 deep 合并新增的全局引用号（用于未引用剔除）

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
                key = _source_dedupe_key(meta)
                if key and key in global_keys:
                    # 跨子任务重复来源：复用既有全局引用号
                    remap[local_id] = global_keys[key]
                    continue
                remap[local_id] = next_id
                sources_map[next_id] = meta
                if key:
                    global_keys[key] = next_id
                new_ids.append(next_id)
                next_id += 1
            text = self._clean_sub_result(text)
            text = self._remap_citations(text, remap)
            text = re.sub(r'<rplot_card data="([^"]*)"></rplot_card>', _pull_plot_markers, text)
            merged.append({
                "heading": st.query,
                "query": st.query,
                "text": text,
            })

        # 未被任何子任务正文引用的来源不进参考文献块：子 Agent 常注册了工具
        # 来源但正文最终未引用，剔除后引用块更短、编号与正文引用一致。
        if new_ids:
            body = "\n".join(m["text"] for m in merged)
            used_ids = {int(n) for n in re.findall(r'\[(\d+)\]', body)}
            dropped = [gid for gid in new_ids if gid not in used_ids]
            for gid in dropped:
                sources_map.pop(gid, None)
            if dropped:
                self.send_log("INFO",
                              f"Deep mode: dropped {len(dropped)} uncited source(s) from references.")

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

        # 合成阶段同样消耗 token：synthesizer 走 main_llm 的流式接口，provider 不
        # 回传 usage，按输入（各路子调查正文 + 查询）与输出估算后并入累计。
        _absorb_usage(getattr(agent, "last_usage", None), {
            "prompt_tokens": estimate_tokens(search_query) + sum(
                estimate_tokens(str(m.get("text") or "")) for m in merged),
            "completion_tokens": estimate_tokens(final_text),
            "estimated": True,
        })

        # 合成后把 R 绘图卡片标记追加回最终文本，保证 UI 端仍能渲染固定卡片
        if plot_markers:
            final_text = final_text.rstrip() + "\n\n" + "\n".join(plot_markers)

        self._emit_token("[CLEAR_SEARCH]")
        self._emit_token("[START_LLM_NETWORK]")
        self._emit_token(final_text)
        return final_text

    def _propose_deep_plan(self, search_query, decomp) -> str:
        """流出深度研究计划确认卡标记（base64 JSON，UI 解码后渲染交互卡）。

        :return: 标记文本。**调用方必须把它并入本轮正文**：收尾的 ``answer_final``
            会用最终正文整段替换流式内容，标记只上屏、不并入正文，就会在收口那
            一刻被覆盖掉——卡片从未渲染出来，只剩"见上方计划卡"这句等待文案。
        """
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
        marker = f'<deep_plan data="{encoded}"></deep_plan>\n'
        self._emit_token(marker)
        return marker

    def _deep_plan_waiting_text(self) -> str:
        """计划确认等待文案（跟随**界面语言**）。

        这段是操作指引而不是模型产出：写成 ``reply_lang`` 驱动，用户用英文提问时
        中文界面里就会冒出一整段英文操作说明。按钮名与卡片标题同样是界面文案，
        两者必须同源，故统一走 :func:`src.core.i18n.tr`。
        """
        from src.core.i18n import tr

        return tr("I have drafted a deep-research plan with several parallel "
                  "sub-investigations (see the plan card above). Review or edit the "
                  "sub-questions, then click 'Confirm & Execute' to start; or click "
                  "'Answer Directly' to get a direct answer without decomposition.")

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
        """将文本中 `[id]` 与引用 token 的 ``ref://cite?n=id`` 换成新的全局 id。

        子 Agent 的编号是局部号（工具结果里回传的 token 也带局部号），合并阶段
        必须两者一起改写，否则 token 里的超链接会指向一个不存在/别人的条目。
        只替换映射内 id，其余原样保留。
        """
        if not text or not remap:
            return text or ""
        text = CITE_LINK_RE.sub(
            lambda m: citation_href(remap[int(m.group(1))])
            if int(m.group(1)) in remap else m.group(0), text)

        def _repl(m):
            cid = int(m.group(1))
            return f"[{remap[cid]}]" if cid in remap else m.group(0)
        return re.sub(r"\[(\d+)\]", _repl, text)

    @staticmethod
    def _clean_sub_result(text):
        """清理子任务返回文本中的运行时控制标记，避免污染合成器。

        Agent.run 的返回值混杂了 UI 控制 token（[CLEAR_SEARCH]、
        [START_LLM_NETWORK]）与思考块（<think>...</think>），合成前必须剥离。
        剥离走 src/core/think_tags.py 的统一口径：它同时覆盖 thinking /
        reasoning / 管道 / Harmony 等变体写法，也覆盖**未闭合**的思考块——
        子任务思考一旦漏进 final_text，就会以正文形态出现在合成结果里。
        """
        if not text:
            return ""
        text = strip_think_blocks(text)
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
        model_name, base_url, declared_window = self._main_model_facts()
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
                args=(self.kb_id, search_query, model_name, tmp_path, history_context,
                      base_url, declared_window),
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
        return _kb_retrieval_core(self.kb_id, search_query, model_name, history_context,
                                  base_url, declared_window)[:2] + (domain,)

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
    """后台任务：把一段或全部对话写入 ``.schat`` 归档。

    归档是**唯一**的导出格式（SQLite 容器）：正文、思考链、参考文献、书签、
    标签与组件状态全部保留；图片与小体积附件按内容寻址内嵌，超过上限的文件留下
    文件名占位符。读写口径统一在 :mod:`src.core.chat_archive` 与
    :mod:`src.core.chat_store`。
    """

    def _execute(self):
        history = self.kwargs.get("history") or []
        tabs = self.kwargs.get("tabs")
        path = self.kwargs.get("path")
        tab_name = self.kwargs.get("tab_name")
        if not history and not tabs:
            return {"success": False, "msg": "No valid chat records to export."}

        try:
            from src.core.chat_archive import write_archive
            export_tabs = tabs if tabs else [{"name": tab_name, "messages": history}]
            stats = write_archive(path, export_tabs)
            return {"success": True, "path": path, "stats": stats}
        except Exception as e:
            self.send_log("ERROR", f"Export task failed: {str(e)}")
            return {"success": False, "msg": str(e)}

class ImportChatHistoryTask(BackgroundTask):
    """后台任务：解析并导入对话归档（``.schat`` / ``.sqlite3`` / ``.db`` / ``.json``）。

    只接受 Scholar Navis 归档：新容器（SQLite）按内容寻址资产恢复内嵌文件；旧容器
    （ZIP / 纯 JSON，``archive_version`` 1–4）按原路径恢复，源文件已不在的条目会补上
    "文件名 + 原因"占位信息，用户能看出缺了什么、为什么缺。

    返回 ``{"success": True, "tabs": [{"name", "tags", "messages"}, …],
    "lossless": True, "placeholders": int}``；每个标签页的消息已规范化为可写入
    ``self.history`` 的条目。
    """

    #: 允许导入的扩展名。容器类型由文件头判定（见 ``chat_archive.read_archive``），
    #: 扩展名只做初步过滤：用户可能把归档存成 ``.db`` / ``.sqlite3``。
    IMPORT_SUFFIXES = (".schat", ".sqlite3", ".sqlite", ".db", ".json")

    def _execute(self):
        path = self.kwargs.get("path")
        if not path or not os.path.exists(path):
            return {"success": False, "msg": f"File not found: {path}"}

        lower = str(path).lower()
        if not lower.endswith(self.IMPORT_SUFFIXES):
            return {"success": False,
                    "msg": "Unsupported format. Please import a .schat, .sqlite3, "
                           ".db or .json archive."}

        try:
            tabs, placeholders = self._parse_lossless(path)
            tabs = [t for t in tabs if t["messages"]]
            if not tabs:
                return {"success": False,
                        "msg": "No chat messages could be parsed from this file."}
            return {"success": True, "tabs": tabs, "lossless": True,
                    "placeholders": placeholders}
        except Exception as e:
            self.send_log("ERROR", f"Import task failed: {str(e)}")
            return {"success": False, "msg": str(e)}

    @staticmethod
    def _tab_name(raw_name):
        """对话名：归档自带名字 > 默认名（"默认对话"）。

        命名口径只在这里定义一次，单对话 / 多对话导入全部走它。
        """
        from src.core.chat_archive import DEFAULT_TAB_NAME, clean_tab_name
        return clean_tab_name(raw_name) or DEFAULT_TAB_NAME

    @staticmethod
    def _normalize_msg(raw):
        """把原始记录条目规范化为可写回 ``self.history`` 的 user/assistant 条目。

        采用"**整体复制 + 规范化已知字段**"而不是字段白名单：白名单每新增一种
        消息字段（书签、远程图片缓存等）就要改一次，漏改会静默丢数据。未知字段
        原样保留，落盘前统一由 ``sanitize`` 处理。
        """
        source = dict(raw) if isinstance(raw, dict) else {}
        role = source.get("role", "")
        if role not in ("user", "assistant"):
            role = "assistant"
        content = source.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, dict):
            text = content.get("text") or content.get("content") or ""
        elif content is None:
            text = ""
        else:
            text = str(content)
        source["role"] = role
        source["content"] = text
        return source

    def _parse_lossless(self, path):
        """解析归档：容器判别、标签页抽取、资产解包统一在 chat_archive。"""
        from src.core import chat_assets
        from src.core.chat_archive import (extract_tabs, read_archive,
                                           remap_attachment_paths)

        payload, meta = read_archive(path)
        attachment_map = meta.get("attachment_map") or {}
        placeholders = meta.get("asset_placeholders") or {}

        tabs = []
        annotated = 0
        for entry in extract_tabs(payload):
            messages = [self._normalize_msg(m) for m in entry.get("messages") or []]
            if messages:
                if attachment_map:
                    remap_attachment_paths(messages, attachment_map)
                annotated += chat_assets.annotate_messages(
                    messages, placeholders, detect_missing=True)
            tabs.append({"name": self._tab_name(entry.get("name")),
                         "tags": entry.get("tags") or [],
                         "messages": messages,
                         "evidence": entry.get("evidence") or {}})
        return tabs, annotated

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
