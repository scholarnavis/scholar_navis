"""自动文献证据库：检索 -> 摘要入库 -> 按会话检索 -> 提供给对话上下文。

设计边界
--------
* **一个全局库**：所有自动检索到的公开文献都进入同一个 Chroma 库，按 DOI/标题
  去重；入库时带上 ``paper_id`` 与来源元数据，引用卡据此生成论文级条目。
* **会话隔离**：chunk 带 ``conversation_id``。自动文献是 ``visibility=global``，
  用户上传 / 付费墙下载的私有 PDF 用 ``visibility=private``，只允许当前会话
  检索；本模块先实现公开文献路径，私有路径复用同一套 metadata 约定。
* **不阻塞 UI**：检索、embedding、重排都在短命子进程里执行（与现有 KB 检索
  一致），父进程只等待结果；超时/模型缺失时安静降级为"没有检索到证据"。

数据约定
--------
Chroma metadata 字段::

    paper_id, conversation_id, visibility, origin, doi, title, authors,
    year, journal, url, type, page, chunk_index

``origin``：
    auto_search / login_pdf / manual_upload / manual_kb

引用时只使用论文级字段（doi/title/url），不使用 "knowledge base" 作为来源。
"""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing as mp
import os
import re
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Core.AutoEvidence")


def _file_sha256(path: str) -> str:
    """计算文件 SHA-256。

    优先 :func:`hashlib.file_digest`（3.11+）：它在 C 层完成读盘与摘要，比
    "Python 循环 + 1MB read" 快一个数量级；老解释器退回 4MB 块循环。
    """
    try:
        with open(path, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except (AttributeError, OSError):
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(4 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

#: 自动库显示名。用户不直接选择库；它始终在后台存在。
AUTO_KB_NAME = "Auto Literature"
#: meta.json 里的标记，避免把用户手工建的库误当成自动库。
AUTO_KB_MARKER = "auto_evidence"

#: 单轮最多新入库的论文数 / 进入上下文的论文数。
MAX_NEW_PAPERS = 12
MAX_CONTEXT_DOCS = 20
#: 重排相关度下限（Qwen3-Reranker 的 yes 概率）。低于它的候选**不进上下文、
#: 不进证据面板**：宁可这一轮没有文献证据，也不要把无关论文堆给用户。
MIN_RELEVANCE_PROB = 0.35

#: 查询里没有实际含义的词（仅用于兜底的词面过滤）。
_QUERY_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were",
    "how", "what", "why", "does", "did", "can", "could", "would", "should",
    "about", "into", "their", "there", "these", "those", "have", "has", "had",
    "研究", "分析", "影响", "作用", "机制", "进展", "综述", "基于", "以及", "我们",
}


def _query_tokens(query: str) -> List[str]:
    """把查询拆成可用于词面比对的片段（Latin 词 / CJK 三字片段）。

    CJK 用**三字**而不是二字：二字片段几乎必然命中（"应用""分析""现代"这类
    通用词到处都有），结果是"公路养护"能匹配"植物光形态建成"——正是无关文献
    混进来的路径。三字片段能保留"光形态""生长素""光周期"这类真正的术语。
    """
    text = str(query or "").lower()
    tokens = [w for w in re.findall(r"[a-z][a-z0-9\-]{2,}", text)
              if w not in _QUERY_STOPWORDS]
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", text)
    tokens += [cjk[i:i + 3] for i in range(len(cjk) - 2)
               if cjk[i:i + 3] not in _QUERY_STOPWORDS]
    return list(dict.fromkeys(tokens))


def _lexically_relevant(text: str, query: str, tokens: Optional[List[str]] = None) -> bool:
    """词面兜底相关度：没有重排模型时，至少挡住明显跨主题的结果。

    只做"前缀/子串命中"，刻意宽松：词形变化（phototropism / phototropic）能过，
    完全无关的主题（表观遗传、选举投票）过不去。查询本身没有可用片段时一律放行，
    避免把正常检索全毙掉。
    """
    tokens = tokens if tokens is not None else _query_tokens(query)
    if not tokens:
        return True
    body = str(text or "").lower()
    for token in tokens:
        needle = token[:5] if len(token) > 5 else token
        if needle in body:
            return True
    return False


def _lexically_related(text: str, query: str) -> bool:
    """严格版词面相关：CJK 必须共享**三字**术语，Latin 必须共享一次词干。"""
    return _lexically_relevant(text, query, _query_tokens(query))


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(text or "")))


#: 非拉丁文字（西里尔 / 阿拉伯 / 假名 / 谚文）。命中即可判定不是英文文献。
_NON_LATIN_SCRIPT_RE = re.compile(
    r"[\u0400-\u04ff\u0590-\u05ff\u0600-\u06ff\u3040-\u30ff\uac00-\ud7af]")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
#: 判定英文所需的最少拉丁字母数：一个短标题（<20 字母）不足以证明是英文正文。
_MIN_LATIN_LETTERS = 20


def _is_english_text(text: str) -> bool:
    """文本是否以英文（拉丁字母）为主。

    自动文献只收英文：检索源对中文/其它语言的召回质量差得多（同一关键词会带回
    大量同语言的跨领域命中），命中率低、噪声高。判据只有两条，宁可放行也不误杀
    英文：出现 CJK 或其它非拉丁文字即否决；拉丁字母不足 20 个也否决（只够一个
    短标题，无法证明正文是英文）。私有 PDF（用户主动下载/上传）不走这里。
    """
    body = str(text or "")
    if _has_cjk(body) or _NON_LATIN_SCRIPT_RE.search(body):
        return False
    return len(_LATIN_LETTER_RE.findall(body)) >= _MIN_LATIN_LETTERS


def _record_is_english(record: Dict[str, Any]) -> bool:
    """检索记录是否属于英文文献：标题与正文合起来判定。"""
    if not isinstance(record, dict):
        return False
    text = f"{record.get('title') or ''} {_record_text(record)}"
    return _is_english_text(text)


def _same_language_related(query: str, doc: Dict[str, Any]) -> bool:
    """同语言候选的硬性相关检查（跨语言交给重排模型判断）。

    真实故障：中文查询 + 中文标题的"预防性公路养护技术…应用分析"，与植物学
    问题毫无关系，却能靠通用措辞（应用/分析/现代）拿到不低的相关度。这里要求
    **同语言**时至少共享一个三字术语，否则丢弃；跨语言（中文查询 + 英文标题）
    不做词面要求，避免误杀翻译/同义表达。
    """
    q = str(query or "")
    meta = doc.get("metadata") if isinstance(doc, dict) else None
    title = str((meta or {}).get("title") or "")
    if not _has_cjk(q) or not _has_cjk(title):
        return True
    body = f"{title} {str((doc or {}).get('content') or '')[:400]}"
    return _lexically_related(body, q)


def _doc_is_english(doc: Dict[str, Any]) -> bool:
    """库里检索出的文档是否属于英文文献（标题 + 片段合起来判定）。"""
    meta = doc.get("metadata") if isinstance(doc, dict) else None
    title = str((meta or {}).get("title") or "")
    body = str((doc or {}).get("content") or "")
    return _is_english_text(f"{title} {body}")


#: 每轮最多尝试自动抓取几篇 OA 全文；失败就退回摘要。
MAX_FULLTEXT_FETCH = 3
FULLTEXT_MAX_BYTES = 12 * 1024 * 1024
#: 摘要/全文 chunk 参数。1200 字符（约 300 token）在 128K–1M 窗口下
#: 比 900 字符保留更多上下文，仍能保持检索精度。
CHUNK_SIZE = 1200
CHUNK_OVERLAP = 160
#: 子进程硬超时（秒）：搜索 + CPU embedding + 重排都可能慢；30s 在首次冷启动
#: 会直接杀掉进程，导致证据没入库、面板一直为空。45s 仍可用 Stop 中止。
WORKER_TIMEOUT = 90.0


# --------------------------------------------------------------------------- #
#  自动库
# --------------------------------------------------------------------------- #
def _available_embedding_id() -> str:
    """选一个本地已经有 ONNX 文件的 embedding 模型；都没有时返回 embed_auto。"""
    try:
        from src.core.models_registry import EMBEDDING_MODELS, ensure_onnx_model
        for model in EMBEDDING_MODELS:
            repo = model.get("hf_repo_id")
            if not repo:
                continue
            try:
                ensure_onnx_model(repo, "embedding", allow_download=False)
                return str(model.get("id") or "embed_auto")
            except Exception:
                continue
    except Exception as e:
        logger.debug("Embedding model probe failed: %s", e)
    # 没有任何可见模型已下载时，新库仍绑定首选模型；由设置页下载后即可用，
    # 绝不静默退回已移除的 Snowflake。
    try:
        from src.core.models_registry import EMBEDDING_MODELS
        if EMBEDDING_MODELS:
            return str(EMBEDDING_MODELS[0].get("id") or "embed_auto")
    except Exception:
        pass
    return "embed_auto"


def _model_installed(model_id: str) -> bool:
    """该 embedding 模型在本机是否已有可用的 ONNX 文件（不触发下载）。"""
    try:
        from src.core.models_registry import ensure_onnx_model, get_model_conf
        repo = str((get_model_conf(str(model_id or ""), "embedding") or {}).get("hf_repo_id") or "")
        if not repo:
            return False
        ensure_onnx_model(repo, "embedding", allow_download=False)
        return True
    except Exception:
        return False


def _collection_count(root: str) -> int:
    """自动库现存的向量条数；读不到时按"有数据"处理（宁可不换模型）。"""
    try:
        import chromadb
        client = chromadb.PersistentClient(
            path=os.path.join(root, "chroma_db"),
            settings=chromadb.Settings(anonymized_telemetry=False, allow_reset=True))
        return int(client.get_collection("main_collection").count())
    except Exception:
        return -1


def _reset_auto_collection(root: str) -> int:
    """删除自动库集合（连同其中的旧向量），返回删除前的条数；失败返回 -1。

    只在"库绑定的 embedding 已不在本机"时调用：那些向量已经无法用于检索
    （没有模型能把查询嵌进同一空间），保留只会让自动证据永久失效。
    """
    try:
        import chromadb
        client = chromadb.PersistentClient(
            path=os.path.join(root, "chroma_db"),
            settings=chromadb.Settings(anonymized_telemetry=False, allow_reset=True))
        try:
            count = int(client.get_collection("main_collection").count())
        except Exception:
            count = 0
        try:
            client.delete_collection("main_collection")
        except Exception as e:
            logger.warning("Cannot drop stale auto-evidence collection: %s", e)
            return -1
        return count
    except Exception as e:
        logger.warning("Cannot reset auto-evidence collection: %s", e)
        return -1


def ensure_auto_kb() -> Optional[str]:
    """返回自动库 id；不存在时创建。失败返回 ``None``（调用方静默降级）。"""
    try:
        from src.core.kb_manager import KBManager
        mgr = KBManager()
        for info in mgr.get_all_kbs():
            root = info.get("root_path", "")
            meta_path = os.path.join(root, "meta.json")
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                continue
            if not (meta.get(AUTO_KB_MARKER)
                    or str(meta.get("name") or "") == AUTO_KB_NAME):
                continue
            # 自动库可能是在另一个模型上创建的，而那个模型早已不在本机 / 已从
            # 模型表移除（例如旧版默认的 Snowflake）。此时入库必然抛异常，且异常
            # 发生在子进程里——用户只会看到"证据面板永远是空的"。
            # 判据用"模型是否装在本机"，不能用 auto_index_ready：那个标记一旦写
            # 过就永久为真，旧库即便一个可用向量都没有也不会再切模型。
            # 换过 embedding 模型：旧向量的维度与语义都不再匹配。**不删证据**，
            # 只丢索引——文献正文存在会话库的快照里，下一次检索前按需重建。
            model_id = str(meta.get("model_id") or "")
            index_model = str(meta.get("index_model_id") or "")
            if index_model and index_model != model_id:
                stale = _collection_count(root)
                dropped = _reset_auto_collection(root) if stale > 0 else 0
                meta["index_model_id"] = model_id
                meta["auto_index_ready"] = False
                try:
                    with open(meta_path, "w", encoding="utf-8") as f:
                        json.dump(meta, f, indent=4)
                    logger.warning(
                        "Auto evidence embedding changed (%s -> %s); dropped %d stale "
                        "vector(s). Evidence snapshots are kept and re-indexed on demand.",
                        index_model, model_id, dropped)
                except OSError as e:
                    logger.warning("Cannot update auto KB index model: %s", e)

            if not _model_installed(str(meta.get("model_id") or "")):
                alt = _available_embedding_id()
                count = _collection_count(root)
                if alt != "embed_auto":
                    # 旧向量与"已不存在的模型"绑在一起：没有那个模型就没法把查询
                    # 嵌进同一空间，留着既查不出也写不进（维度冲突）。因此换模型
                    # 时一并清掉，让自动检索恢复可用；条数写进日志便于核对。
                    dropped = _reset_auto_collection(root) if count > 0 else 0
                    from src.core.models_registry import get_model_conf
                    meta["model_id"] = alt
                    meta["model_info"] = get_model_conf(alt, "embedding")
                    meta["auto_index_ready"] = False
                    meta["index_model_id"] = alt
                    try:
                        with open(meta_path, "w", encoding="utf-8") as f:
                            json.dump(meta, f, indent=4)
                        logger.warning(
                            "Auto literature KB was bound to an unavailable embedding; "
                            "switched to %s and dropped %d stale vector(s).", alt, dropped)
                    except OSError as e:
                        logger.warning("Cannot update auto KB model: %s", e)
                else:
                    logger.warning(
                        "Auto literature KB embedding %r is not installed and no local "
                        "embedding is available; evidence panel will fall back to "
                        "per-turn abstracts.", meta.get("model_id"))
            return str(info.get("id") or "")

        kb_id = mgr.create_kb(AUTO_KB_NAME,
                              "Automatically retrieved literature evidence",
                              "General Academic", _available_embedding_id())
        meta_path = os.path.join(mgr.WORKSPACE_DIR, kb_id, "meta.json")
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        meta[AUTO_KB_MARKER] = True
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=4)
        logger.info("Created automatic literature KB: %s", kb_id)
        return kb_id
    except Exception as e:
        logger.warning("Cannot ensure automatic literature KB: %s", e)
        return None


def _auto_kb_root() -> str:
    """返回自动库根目录；不存在返回空串。不创建、不加载 Chroma。"""
    try:
        from src.core.kb_manager import KBManager
        for info in KBManager().get_all_kbs():
            root = info.get("root_path", "")
            meta_path = os.path.join(root, "meta.json")
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                continue
            if meta.get(AUTO_KB_MARKER) or str(meta.get("name") or "") == AUTO_KB_NAME:
                return root
    except Exception:
        pass
    return ""


def _manifest_path(root: str) -> str:
    return os.path.join(root, "conversation_evidence.json")


def _load_manifest(root: str) -> Dict[str, Any]:
    if not root:
        return {}
    try:
        with open(_manifest_path(root), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_manifest(root: str, data: Dict[str, Any]) -> None:
    if not root:
        return
    try:
        path = _manifest_path(root)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        logger.debug("Save conversation evidence manifest failed: %s", e)


def _context_path(root: str) -> str:
    return os.path.join(root, "conversation_context.json")


def save_conversation_context(conversation_id: str, query: str,
                              context_str: str,
                              sources_map: Dict[int, Dict[str, Any]]) -> None:
    """保存本轮检索上下文；纯追问轮直接复用，避免丢证据或重新乱搜。"""
    cid = str(conversation_id or "")
    if not cid or not str(context_str or "").strip():
        return
    root = _auto_kb_root()
    if not root:
        return
    try:
        path = _context_path(root)
        data = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
        data[cid] = {
            "query": str(query or ""),
            "context": str(context_str),
            "sources": {str(k): v for k, v in (sources_map or {}).items()},
            "updated_at": time.time(),
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        logger.debug("Save conversation context failed: %s", e)


def load_conversation_context(conversation_id: str) -> Tuple[str, Dict[int, Dict[str, Any]]]:
    """读取上一轮证据上下文；没有缓存时返回空串/空 map。"""
    cid = str(conversation_id or "")
    if not cid:
        return "", {}
    root = _auto_kb_root()
    if not root:
        return "", {}
    try:
        with open(_context_path(root), "r", encoding="utf-8") as f:
            data = json.load(f)
        entry = data.get(cid) if isinstance(data, dict) else None
        if not isinstance(entry, dict):
            return "", {}
        context = str(entry.get("context") or "")
        raw_sources = entry.get("sources") or {}
        sources: Dict[int, Dict[str, Any]] = {}
        if isinstance(raw_sources, dict):
            for key, value in raw_sources.items():
                try:
                    idx = int(key)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    sources[idx] = value
        return context, sources
    except (OSError, ValueError):
        return "", {}


def record_conversation_papers(conversation_id: str,
                               papers: List[Dict[str, Any]]) -> None:
    """把论文与对话的关联写入 manifest。

    全局自动文献在另一个对话已入库时，chunk 不会重复写；仅靠 chunk 的
    ``conversation_id`` 会让当前对话的证据面板看不到它。manifest 解决这个
    "全局文献跨对话关联"问题。
    """
    cid = str(conversation_id or "")
    if not cid or not papers:
        return
    root = _auto_kb_root()
    if not root:
        return
    data = _load_manifest(root)
    conv = data.setdefault(cid, {})
    for paper in papers:
        pid = str(paper.get("paper_id") or "")
        if not pid:
            continue
        entry = conv.setdefault(pid, {
            "paper_id": pid,
            "origins": [],
            "visibility": str(paper.get("visibility") or "global"),
        })
        for key in ("title", "doi", "year", "journal", "url", "path"):
            if paper.get(key):
                entry[key] = paper[key]
        entry["visibility"] = str(paper.get("visibility") or entry.get("visibility") or "global")
        entry["has_fulltext"] = bool(paper.get("has_fulltext") or entry.get("has_fulltext"))
        origin = str(paper.get("origin") or "auto_search")
        if origin not in entry["origins"]:
            entry["origins"].append(origin)
        entry["added_at"] = time.time()
    _save_manifest(root, data)


def prune_conversation_global_evidence(conversation_id: str,
                                       keep_paper_ids: List[str]) -> None:
    """把当前对话的全局 manifest 收窄到本轮真正进入上下文的论文。

    全局自动库会长期累积不同主题的论文；如果只做“追加”，证据面板会把
    以前搜过、但与当前问题无关的文献一直列出来。私有 PDF 永远保留。
    """
    cid = str(conversation_id or "")
    keep = {str(pid) for pid in keep_paper_ids if pid}
    if not cid or not keep:
        return
    root = _auto_kb_root()
    if not root:
        return
    data = _load_manifest(root)
    conv = data.get(cid)
    if not isinstance(conv, dict):
        return
    globals_ = {pid for pid, entry in conv.items()
                if isinstance(entry, dict)
                and str(entry.get("visibility") or "global") != "private"}
    if globals_ and not (globals_ & keep):
        # 一个都匹配不上，说明 keep 与 manifest 用了不同的 paper_id 口径（旧版本
        # 写入的条目、或来源换了一套 id），不是"这些文献都无关"。此时收窄等于
        # 把整个面板清空，绝不能做。
        logger.warning("Skip evidence prune: %d manifest id(s) match none of %d "
                       "candidate id(s).", len(globals_), len(keep))
        return
    for pid, entry in list(conv.items()):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("visibility") or "global") == "private":
            continue
        if pid not in keep:
            conv.pop(pid, None)
    if conv:
        data[cid] = conv
    else:
        data.pop(cid, None)
    _save_manifest(root, data)


def _remove_manifest_paper(conversation_id: str, paper_id: str) -> None:
    root = _auto_kb_root()
    if not root:
        return
    data = _load_manifest(root)
    conv = data.get(str(conversation_id or ""))
    if not isinstance(conv, dict):
        return
    conv.pop(str(paper_id or ""), None)
    if not conv:
        data.pop(str(conversation_id or ""), None)
    _save_manifest(root, data)


def _remove_conversation_context(conversation_id: str) -> None:
    """删除该对话保存的"上一轮证据上下文"。

    清除对话时如果只删 manifest 不删上下文，面板回填逻辑（manifest 为空时用
    ``conversation_context`` 兜底）会立刻把证据"复活"，看起来就像没清掉。
    """
    cid = str(conversation_id or "")
    if not cid:
        return
    root = _auto_kb_root()
    if not root:
        return
    path = _context_path(root)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or data.pop(cid, None) is None:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        logger.debug("Cannot drop saved context of %s: %s", cid, e)


def _remove_manifest_conversation(conversation_id: str) -> None:
    root = _auto_kb_root()
    if not root:
        return
    data = _load_manifest(root)
    data.pop(str(conversation_id or ""), None)
    _save_manifest(root, data)


def _auto_collection():
    """打开自动库的 Chroma collection（不加载 embedding 模型）。"""
    from src.core.kb_manager import KBManager
    mgr = KBManager()
    for info in mgr.get_all_kbs():
        root = info.get("root_path", "")
        meta_path = os.path.join(root, "meta.json")
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if not (meta.get(AUTO_KB_MARKER)
                or str(meta.get("name") or "") == AUTO_KB_NAME):
            continue
        db_path = os.path.join(root, "chroma_db")
        if not os.path.isdir(db_path):
            return None
        import chromadb
        client = chromadb.PersistentClient(
            path=db_path,
            settings=chromadb.Settings(anonymized_telemetry=False, allow_reset=True))
        try:
            return client.get_collection("main_collection")
        except Exception:
            return None
    return None


def _backfill_manifest_from_context(conversation_id: str) -> None:
    """manifest 里没有该对话时，用**已保存的本轮上下文**把论文补回去。

    记录链路里任何一环出问题（旧的 manifest 被收窄清空、模型换维度后重建过库）
    都会让面板空着，而"这一轮到底用了哪些文献"其实还留在 conversation_context
    里。这是只读回填，幂等，不会凭空造条目。
    """
    cid = str(conversation_id or "")
    if not cid:
        return
    try:
        _context, sources = load_conversation_context(cid)
    except Exception as e:      # 回填失败不得影响面板打开
        logger.debug("Evidence manifest backfill skipped: %s", e)
        return
    papers = [{
        "paper_id": str(meta.get("paper_id") or ""),
        "title": meta.get("title"), "doi": meta.get("doi"),
        "year": meta.get("year"), "journal": meta.get("journal"),
        "url": meta.get("url"), "path": meta.get("path"),
        "has_fulltext": bool(meta.get("has_fulltext")),
        "visibility": meta.get("visibility") or "global",
        "origin": "auto_search",
    } for meta in sources.values() if meta.get("paper_id")]
    if papers:
        record_conversation_papers(cid, papers)
        logger.info("Evidence manifest backfilled from saved context: %d paper(s).",
                    len(papers))


def list_conversation_evidence(conversation_id: str) -> List[Dict[str, Any]]:
    """列出某对话已入库的证据（公开自动文献 + 私有 PDF/附件）。

    只按 metadata 读取，不做向量检索，因此不需要加载 embedding 模型。
    """
    cid = str(conversation_id or "")
    if not cid:
        return []
    try:
        # 硬性不变量：**没有任何内容的对话，就不该有任何证据**。
        # 这里是所有读取路径的入口（面板、"获取全文"按钮、导出），在此拦截比在
        # 每个调用点各加一道守卫可靠：清空过的对话即便库里还残留 manifest，
        # 也永远不会被列出来。只看会话库（消息 + 半条快照），不做删除——
        # 读操作不改数据，真正的清理由 clear/close 路径负责。
        from src.core import chat_session
        if not chat_session.load_tab(cid) and not chat_session.load_inflight(cid):
            return []
    except Exception as e:
        logger.debug("Evidence emptiness check skipped for %s: %s", cid, e)
    try:
        root = _auto_kb_root()
        manifest = (_load_manifest(root).get(cid) or {}) if root else {}
        if root and not manifest:
            # 清单为空但"导入归档带回来的证据快照"还在（后台重建尚未跑完）：
            # 先用快照兜底，面板立刻就能看到这批文献。
            try:
                from src.core import chat_session
                bundle = chat_session.load_evidence_bundle(cid)
            except Exception:
                bundle = {}
            for paper in (bundle.get("papers") or []):
                pid = str(paper.get("paper_id") or "")
                if pid:
                    manifest = dict(manifest)
                    manifest[pid] = dict(paper)
            if not manifest:
                # 连快照也没有：再试"本轮上下文"回填，避免面板永远是空的。
                _backfill_manifest_from_context(cid)
                manifest = (_load_manifest(root).get(cid) or {}) if root else {}
        grouped: Dict[str, Dict[str, Any]] = {}
        # manifest 先铺底：全局文献可能只在别的对话写过 chunk，但当前对话
        # 确实使用过它，面板仍应列出来。
        for pid, entry in manifest.items():
            if not isinstance(entry, dict):
                continue
            # 历史数据里可能有"整段正文当标题"的条目（早前检索源返回的脏数据）：
            # 展示前一并清洗，清洗后仍无可读标题且无 DOI 的直接不显示。
            shown_title = _display_title(entry)
            if not shown_title:
                continue
            grouped[str(pid)] = {
                "paper_id": str(pid),
                "title": shown_title,
                "doi": str(entry.get("doi") or ""),
                "year": str(entry.get("year") or ""),
                "journal": str(entry.get("journal") or ""),
                "url": str(entry.get("url") or ""),
                "path": str(entry.get("path") or ""),
                "type": str(entry.get("type") or "article"),
                "origins": set(entry.get("origins") or ["auto_search"]),
                "visibility": str(entry.get("visibility") or "global"),
                "pages": set(),
                "has_fulltext": bool(entry.get("has_fulltext")),
            }
        collection = _auto_collection()
        if collection is not None:
            data = collection.get(where={"conversation_id": cid}, limit=5000)
            ids = data.get("ids") or []
            metas = data.get("metadatas") or []
            for idx, _chunk_id in enumerate(ids):
                meta = metas[idx] if idx < len(metas) and isinstance(metas[idx], dict) else {}
                pid = str(meta.get("paper_id") or "")
                if not pid:
                    continue
                shown = _display_title(meta)
                if pid not in grouped and not shown:
                    continue        # 段落式标题且无 DOI：不进面板
                item = grouped.setdefault(pid, {
                    "paper_id": pid,
                    "title": shown or "Untitled",
                    "doi": str(meta.get("doi") or ""),
                    "year": str(meta.get("year") or ""),
                    "journal": str(meta.get("journal") or ""),
                    "url": str(meta.get("url") or ""),
                    "path": str(meta.get("file_path") or ""),
                    "type": str(meta.get("type") or "article"),
                    "origins": set(),
                    "visibility": str(meta.get("visibility") or "global"),
                    "pages": set(),
                    "has_fulltext": bool(meta.get("has_fulltext")),
                })
                item["origins"].add(str(meta.get("origin") or "auto_search"))
                item["pages"].add(int(meta.get("page") or 1))
                if meta.get("has_fulltext"):
                    item["has_fulltext"] = True
                if not item["path"] and meta.get("file_path"):
                    item["path"] = str(meta.get("file_path"))
        # PDF 本体索引：一次查询拿到本对话登记过的文件，避免遍历 private_pdfs。
        try:
            from src.core import pdf_index
            indexed = {row["paper_id"]: row["path"]
                       for row in pdf_index.for_conversation(cid)}
        except Exception:
            indexed = {}
        for pid, item in grouped.items():
            if not item.get("path"):
                item["path"] = indexed.get(pid, "")

        result = []
        for item in grouped.values():
            item["origins"] = sorted(item["origins"])
            item["pages"] = sorted(item["pages"])
            result.append(item)
        result.sort(key=lambda x: (x["title"].lower(), x["year"]))
        return result
    except Exception as e:
        logger.warning("List conversation evidence failed: %s", e)
        return []


# --------------------------------------------------------------------------- #
#  证据快照（随历史 / 归档走，与 embedding 模型解耦）
# --------------------------------------------------------------------------- #
#: 快照结构版本：``{version, model, papers: [...], chunks: {paper_id: [text, ...]}}``。
EVIDENCE_BUNDLE_VERSION = 1


def _bundle_from_store(conversation_id: str) -> Dict[str, Any]:
    """从 manifest 与向量库拼出快照：文献元数据 + 每篇文献已索引的正文分片。

    正文分片是**与模型无关**的事实：换 embedding 后可以用新模型重新嵌入它们，
    所以快照里必须带上，不能只留 paper_id。
    """
    cid = str(conversation_id or "")
    if not cid:
        return {}
    root = _auto_kb_root()
    manifest = (_load_manifest(root).get(cid) or {}) if root else {}
    if not manifest:
        return {}
    papers: List[Dict[str, Any]] = []
    for pid, entry in manifest.items():
        if not isinstance(entry, dict):
            continue
        papers.append({
            "paper_id": str(pid),
            "title": entry.get("title") or "",
            "doi": entry.get("doi") or "",
            "year": entry.get("year") or "",
            "journal": entry.get("journal") or "",
            "url": entry.get("url") or "",
            "path": entry.get("path") or "",
            "type": str(entry.get("type") or "article"),
            "has_fulltext": bool(entry.get("has_fulltext")),
            "visibility": str(entry.get("visibility") or "global"),
            "origins": list(entry.get("origins") or ["auto_search"]),
        })
    chunks: Dict[str, List[str]] = {}
    collection = _auto_collection()
    if collection is not None:
        try:
            data = collection.get(where={"conversation_id": cid},
                                  include=["documents", "metadatas"], limit=5000)
            documents = data.get("documents") or []
            metas = data.get("metadatas") or []
            for index, text in enumerate(documents):
                meta = metas[index] if index < len(metas) and isinstance(metas[index], dict) else {}
                pid = str(meta.get("paper_id") or "")
                if pid and text:
                    chunks.setdefault(pid, []).append(str(text))
        except Exception as e:
            logger.debug("Evidence snapshot: cannot read chunks for %s: %s", cid, e)
    return {
        "version": EVIDENCE_BUNDLE_VERSION,
        "papers": papers,
        "chunks": chunks,
    }


def export_evidence_bundle(conversation_id: str) -> Dict[str, Any]:
    """导出该对话的证据快照；没有证据时返回空 dict。

    由聊天页在每轮收尾、以及导出归档时调用；快照写进会话库（``tab_payloads``），
    因此"清除对话 / 导出 / 导入"三条链路都不需要碰向量库。
    """
    return _bundle_from_store(conversation_id)


def import_evidence_bundle(conversation_id: str, bundle: Dict[str, Any]) -> int:
    """把快照写回 manifest 与向量库（尽力而为），返回恢复的文献数。

    向量入库依赖本机 embedding 模型：模型缺失或维度不符时只恢复元数据，面板与
    引用仍可用（检索退化为直接摘要），等模型可用后由
    :func:`_ensure_conversation_indexed` 补建索引。
    """
    cid = str(conversation_id or "")
    if not cid or not isinstance(bundle, dict):
        return 0
    papers = [p for p in (bundle.get("papers") or []) if isinstance(p, dict) and p.get("paper_id")]
    if not papers:
        return 0
    record_conversation_papers(cid, papers)
    chunks = bundle.get("chunks") if isinstance(bundle.get("chunks"), dict) else {}
    if not chunks:
        return len(papers)
    try:
        from src.core.config_manager import ConfigManager
        from src.core.kb_manager import DatabaseManager
        from src.task.kb_tasks import _worker_load_model

        kb_id = ensure_auto_kb()
        if not kb_id:
            return len(papers)
        embed_fn = _worker_load_model(kb_id, ConfigManager())
        db = DatabaseManager()
        if not db.switch_kb(kb_id, embedding_function=embed_fn):
            return len(papers)
        for paper in papers:
            pid = str(paper.get("paper_id"))
            texts = [str(t) for t in (chunks.get(pid) or []) if str(t).strip()]
            if not texts:
                continue
            meta_base = {
                "paper_id": pid,
                "conversation_id": cid,
                "visibility": str(paper.get("visibility") or "global"),
                "origin": str((paper.get("origins") or ["auto_search"])[0]),
                "doi": str(paper.get("doi") or ""),
                "title": str(paper.get("title") or ""),
                "authors": "",
                "year": str(paper.get("year") or ""),
                "journal": str(paper.get("journal") or ""),
                "url": str(paper.get("url") or ""),
                "type": "article",
                "page": 1,
                "has_fulltext": bool(paper.get("has_fulltext")),
                "file_path": str(paper.get("path") or ""),
            }
            ids = [f"{pid}_{i}" for i in range(len(texts))]
            metas = [dict(meta_base, chunk_index=i) for i in range(len(texts))]
            db.add_documents(documents=texts, metadatas=metas, ids=ids)
        logger.info("Evidence bundle imported for %s: %d paper(s), %d paper(s) with text.",
                    cid, len(papers), len([k for k, v in chunks.items() if v]))
    except Exception as e:
        logger.warning("Evidence bundle imported without vectors for %s: %s", cid, e)
    return len(papers)


def _snapshot_conversation_evidence(conversation_id: str) -> None:
    """把本对话的证据（元数据 + 已索引正文）写进会话库。

    会话库是**权威副本**：历史、导出、导入、换模型后的重建都读这一份。向量库
    只是它的一份索引，随时可以从这里重建。
    """
    try:
        from src.core import chat_session
        bundle = _bundle_from_store(conversation_id)
        if bundle and bundle.get("papers"):
            chat_session.save_evidence_bundle(conversation_id, bundle)
    except Exception as e:
        logger.debug("Evidence snapshot failed for %s: %s", conversation_id, e)


def fulltext_excerpt(conversation_id: str, doi: str = "", title: str = "",
                     max_chars: int = 12000) -> Tuple[str, str]:
    """取刚入库那篇论文的正文（只读文本，不解码向量、不加载模型）。

    同一轮里"边等登录边续跑"时，上下文是在本轮开始时装配好的：新入库的全文不在
    里面。这里按 paper_id 直接读分片正文，交给模型当作工具结果——这样它拿到的是
    **全文内容**，而不是一句"已就绪"的承诺。

    :return: ``(paper_id, text)``；取不到时 text 为空串。
    """
    cid = str(conversation_id or "")
    pid = _paper_id({"doi": doi, "title": title})
    if not cid or not pid:
        return pid, ""
    collection = _auto_collection()
    if collection is None:
        return pid, ""
    text = ""
    try:
        # 先找本对话的私有分片
        data = collection.get(
            where={"$and": [{"paper_id": pid}, {"conversation_id": cid}]},
            include=["documents"], limit=200)
        documents = data.get("documents") or []
        text = "\n".join(str(d) for d in documents if str(d).strip())
        if not text:
            # 本对话没有，但这个 DOI 在别处已经获取过全文：直接复用，避免让用户
            # 为同一篇论文再登录一次（"一篇文章只获取一次"）。
            data = collection.get(where={"paper_id": pid},
                                  include=["documents"], limit=200)
            text = "\n".join(str(d) for d in (data.get("documents") or [])
                             if str(d).strip())
            if text:
                # 复用的同时挂到当前对话，证据面板里也能看到这篇（正文仍复用原分片）
                record_conversation_papers(cid, [{
                    "paper_id": pid,
                    "title": title, "doi": doi,
                    "visibility": "private",
                    "has_fulltext": True,
                    "origin": "cached_fulltext",
                }])
    except Exception as e:
        logger.debug("Full-text excerpt unavailable for %s: %s", pid, e)
        return pid, ""
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[truncated]"
    return pid, text


def reindex_evidence_in_background(conversation_id: str,
                                   bundle: Dict[str, Any]) -> None:
    """后台把证据快照按当前模型写回向量库（导入归档后调用）。

    重建要加载本地 embedding 模型，首次可达数秒，绝不能挡在导入流程里；失败也
    只是暂时没有向量，面板与引用仍基于快照工作，下次检索前会自动重试。
    """
    import threading

    def worker():
        try:
            import_evidence_bundle(conversation_id, bundle)
        except Exception as e:
            logger.warning("Evidence re-index failed for %s: %s", conversation_id, e)

    threading.Thread(target=worker, daemon=True, name="evidence-reindex").start()


def _ensure_conversation_indexed(conversation_id: str, kb_id: str) -> None:
    """向量库里缺少本对话的分片时，用会话库里的快照按当前模型重建索引。

    这是"换 embedding 模型"的兜底：旧向量被丢弃后，文献正文仍在快照里，下一次
    检索前先把本对话重新嵌入一遍——只重建**当前对话**，不必全库重算。
    """
    cid = str(conversation_id or "")
    if not cid or not kb_id:
        return
    try:
        collection = _auto_collection()
        if collection is not None:
            existing = collection.get(where={"conversation_id": cid}, limit=1)
            if existing.get("ids"):
                return
        from src.core import chat_session
        bundle = chat_session.load_evidence_bundle(cid)
        if not bundle:
            return
        restored = import_evidence_bundle(cid, bundle)
        logger.info("Evidence index rebuilt from snapshot for %s: %d paper(s).",
                    cid, restored)
    except Exception as e:
        logger.warning("Evidence re-index skipped for %s: %s", cid, e)


#: 进行中的对话可能还没落盘消息：最近这么多秒内更新的证据一律不清，避免误删。
_ORPHAN_GRACE_SECONDS = 120


def purge_orphan_evidence() -> int:
    """清理"对话已不存在 / 已清空"却仍挂着的证据，返回清理的对话数。

    为什么要兜这一层：清空对话、关闭标签页都会主动删证据，但历史遗留（旧版本清空
    过、导入失败、进程被杀）会让证据留在库里，面板于是能列出"已经没有对话"的文献。
    判据只有两条，宁可不删：

    1. 该对话不在会话库的标签页清单里（标签页已关闭）；
    2. 标签页还在，但既没有消息、也没有半条快照（对话被清空）。

    最近 :data:`_ORPHAN_GRACE_SECONDS` 秒内更新过证据的对话跳过：第一轮回答还在
    生成时，消息尚未落盘但证据已经入库，这时清掉就是删掉正在用的文献。
    """
    root = _auto_kb_root()
    if not root:
        return 0
    removed = 0
    try:
        from src.core import chat_session
        _active, tabs = chat_session.load_manifest()
        known = {str(t.get("id") or "") for t in (tabs or [])}
        for cid, entry in list((_load_manifest(root) or {}).items()):
            if not isinstance(entry, dict) or not entry:
                continue
            if cid not in known:
                # 标签页已经不存在：不存在"正在进行的一轮"，不必等宽限期。
                delete_conversation_evidence(cid)
                removed += 1
                continue
            newest = max((float(v.get("added_at") or 0)
                          for v in entry.values() if isinstance(v, dict)), default=0.0)
            if newest and (time.time() - newest) < _ORPHAN_GRACE_SECONDS:
                continue
            try:
                if chat_session.load_tab(cid) or chat_session.load_inflight(cid):
                    continue
            except Exception:
                continue
            delete_conversation_evidence(cid)
            removed += 1
    except Exception as e:
        logger.debug("Evidence sweep skipped: %s", e)
    if removed:
        logger.info("Purged evidence for %d orphaned conversation(s).", removed)
    return removed


def delete_private_evidence(conversation_id: str, paper_id: str) -> bool:
    """删除某对话的一篇私有证据；全局自动文献不允许从这里删。"""
    cid = str(conversation_id or "")
    pid = str(paper_id or "")
    if not cid or not pid:
        return False
    try:
        collection = _auto_collection()
        if collection is None:
            return False
        collection.delete(where={"$and": [
            {"paper_id": pid},
            {"conversation_id": cid},
            {"visibility": "private"},
        ]})
        _remove_manifest_paper(cid, pid)
        invalidate_context_cache(cid)
        return True
    except Exception as e:
        logger.warning("Delete private evidence failed: %s", e)
        return False


def delete_conversation_evidence(conversation_id: str) -> bool:
    """删除某对话的私有证据 chunk；全局自动文献保留。

    关闭标签页时调用。GUI 进程通常没有持有自动库的 Chroma client（检索在
    子进程里做），这里直接打开持久化目录删除，避免为了删 chunk 去加载模型。
    """
    cid = str(conversation_id or "")
    if not cid:
        return False
    invalidate_context_cache(cid)
    _remove_manifest_conversation(cid)
    # 保存的上下文也要删：面板在 manifest 为空时会用它回填，不删等于"删了又回来"。
    _remove_conversation_context(cid)
    # PDF 索引里解除会话关联（文件本体作为缓存保留：同一篇论文只需获取一次）。
    try:
        from src.core import pdf_index
        pdf_index.detach_conversation(cid)
    except Exception as e:
        logger.debug("PDF index detach skipped: %s", e)
    # 会话库里的证据快照同样要清：只删 manifest 会让"清除对话"后证据面板仍能列出
    # 这份已不存在的对话用过的文献。
    try:
        from src.core import chat_session
        chat_session.clear_evidence_bundle(cid)
    except Exception as e:
        logger.debug("Clear evidence bundle failed for %s: %s", cid, e)
    try:
        from src.core.kb_manager import KBManager
        mgr = KBManager()
        for info in mgr.get_all_kbs():
            root = info.get("root_path", "")
            meta_path = os.path.join(root, "meta.json")
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                continue
            if not (meta.get(AUTO_KB_MARKER)
                    or str(meta.get("name") or "") == AUTO_KB_NAME):
                continue
            db_path = os.path.join(root, "chroma_db")
            if not os.path.isdir(db_path):
                return False
            import chromadb
            client = chromadb.PersistentClient(
                path=db_path,
                settings=chromadb.Settings(anonymized_telemetry=False, allow_reset=True))
            try:
                collection = client.get_collection("main_collection")
            except Exception:
                return False
            collection.delete(where={"$and": [
                {"visibility": "private"},
                {"conversation_id": cid},
            ]})
            logger.info("Deleted private evidence for conversation %s", cid)
            return True
    except Exception as e:
        logger.warning("Delete conversation evidence failed: %s", e)
    return False


def _mark_index_ready(kb_id: str) -> None:
    """标记自动库已有向量，并记下**这批向量是由哪个 embedding 产生的**。

    ``index_model_id`` 是模型更换检测的唯一依据：它和当前 ``model_id`` 不一致，
    说明索引已过期，需要按快照重建（而不是把证据删掉）。
    """
    try:
        from src.core.kb_manager import KBManager
        meta_path = os.path.join(KBManager().WORKSPACE_DIR, kb_id, "meta.json")
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        changed = False
        if not meta.get("auto_index_ready"):
            meta["auto_index_ready"] = True
            changed = True
        current = str(meta.get("model_id") or "")
        if current and meta.get("index_model_id") != current:
            meta["index_model_id"] = current
            changed = True
        if changed:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=4)
    except Exception as e:
        logger.debug("Cannot mark auto KB index ready: %s", e)


def _paper_id(record: Dict[str, Any]) -> str:
    doi = str(record.get("doi") or "").strip().lower()
    title = " ".join(str(record.get("title") or "").lower().split())
    seed = f"doi:{doi}" if doi else f"title:{title}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]


def _europe_pmc_fulltext(doi: str) -> str:
    """Europe PMC 的 OA 全文（XML）：对 PMC 收录的 PNAS/Nature 子刊更稳。"""
    try:
        from html import unescape
        from src.core.network_worker import create_robust_session

        session = create_robust_session()
        res = session.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": f'DOI:"{doi}"', "format": "json",
                    "resultType": "core", "pageSize": 1},
            timeout=15)
        if res.status_code != 200:
            return ""
        records = (res.json().get("resultList") or {}).get("result") or []
        if not records:
            return ""
        pmcid = str(records[0].get("pmcid") or "")
        if not pmcid:
            return ""
        xml_res = session.get(
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML",
            timeout=20)
        if xml_res.status_code != 200 or not xml_res.text:
            return ""
        text = unescape(re.sub(r"<[^>]+>", " ", xml_res.text))
        return re.sub(r"\s+", " ", text).strip()
    except Exception as e:
        logger.debug("Europe PMC full text failed for %s: %s", doi, e)
        return ""


def _fetch_oa_fulltext(record: Dict[str, Any]) -> str:
    """尝试用 DOI 找合法 OA 全文；失败返回空串。"""
    doi = str(record.get("doi") or "").strip()
    if not doi:
        return ""
    # 先走 Europe PMC 的 XML：不依赖出版社页面/反爬。
    pmc_text = _europe_pmc_fulltext(doi)
    if len(pmc_text) >= 500:
        return pmc_text
    tmp = ""
    try:
        from src.core.academic.base import ncbi_api_key, ncbi_email
        from src.core.network_worker import create_robust_session
        from src.core.oa import OAFetcher

        found = OAFetcher().fetch_best_oa_pdf(
            doi, ncbi_email, ncbi_api_key=ncbi_api_key)
        if not found.get("is_oa") or not found.get("pdf_url"):
            return ""
        session = create_robust_session()
        resp = session.get(found["pdf_url"], timeout=20)
        if resp.status_code != 200:
            return ""
        content = resp.content or b""
        if len(content) > FULLTEXT_MAX_BYTES or not content.startswith(b"%PDF"):
            return ""
        fd, tmp = tempfile.mkstemp(prefix="oa_pdf_", suffix=".pdf")
        os.close(fd)
        with open(tmp, "wb") as f:
            f.write(content)
        pages, _meta = _extract_pdf(tmp)
        text = "\n\n".join(t for _page, t in pages if t.strip())
        return text.strip()
    except Exception as e:
        logger.debug("OA full text fetch failed for %s: %s", doi, e)
        return ""
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _record_text(record: Dict[str, Any]) -> str:
    fulltext = str(record.get("_fulltext") or "").strip()
    if fulltext:
        return fulltext
    abstract = str(record.get("abstract") or "").strip()
    if abstract:
        return abstract
    parts = [
        str(record.get("title") or "").strip(),
        str(record.get("journal") or "").strip(),
        str(record.get("year") or "").strip(),
    ]
    return " ".join(p for p in parts if p)


# --------------------------------------------------------------------------- #
#  检索（父进程）
# --------------------------------------------------------------------------- #
_TOP_VENUES = (
    "proceedings of the national academy",
    "plant cell", "plant physiology", "new phytologist", "molecular plant",
    "elife", "current biology", "plos biology",
)
_LOW_VENUES = (
    "mdpi", "frontiers", "plos one", "scientific reports", "peerj",
    "hindawi", "bmc ",
)


def _venue_prior(rec: Dict[str, Any]) -> float:
    journal = str(rec.get("journal") or "").lower().strip()
    if not journal:
        return 0.0
    # 只认"期刊名第一个词就是顶刊"或明确的子刊/领域顶刊，避免
    # "Cellular Signalling" / "Frontiers in Plant Science" 被误判。
    head = journal.split(" ", 1)[0].strip(":,")
    if head in ("nature", "science", "cell", "pnas") \
            or any(v in journal for v in _TOP_VENUES):
        return 3.0
    if any(v in journal for v in _LOW_VENUES):
        return -0.5
    return 1.0


def _select_records(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """相关性顺序 + 期刊质量微调 + 同刊最多 2 篇。

    不直接按引用数/期刊重排（会压掉相关的新论文），只给高质量期刊一个
    有限加分，并防止一轮上下文被同一出版社占满。
    """
    scored = []
    for idx, rec in enumerate(candidates):
        journal = str(rec.get("journal") or "").strip().lower()
        scored.append((-idx + _venue_prior(rec), idx, journal, rec))
    scored.sort(key=lambda item: (-item[0], item[1]))

    selected = []
    per_journal: Dict[str, int] = {}
    for _score, _idx, journal, rec in scored:
        if len(selected) >= MAX_NEW_PAPERS:
            break
        if journal and per_journal.get(journal, 0) >= 2:
            continue
        selected.append(rec)
        if journal:
            per_journal[journal] = per_journal.get(journal, 0) + 1
    return selected


def _refine_query(text: str) -> str:
    """第一轮结果太少时，像人一样去掉口语/疑问词再搜一次。"""
    cleaned = re.sub(
        r"(?:请问|什么是|是什么|如何|为什么|有哪些|怎么样|怎么回事|"
        r"what\s+is|what\s+are|how\s+does|why\s+does|tell\s+me\s+about|"
        r"[？?！!。，,、；;：:])",
        " ", str(text or ""), flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.split())
    return cleaned if len(cleaned) >= 8 and cleaned.lower() != text.lower() else ""


def _expand_via_citations(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """选一篇种子论文，沿参考文献/被引文献扩展——真人会追引文，而不是只搜关键词。"""
    from src.core.academic.literature import _merge_records, traverse_citation_graph

    seeds = [r for r in candidates if r.get("doi")]
    if not seeds:
        return candidates
    # 优先高质量、较新的种子；老论文看"被谁引用"，新论文看"它引用了谁"。
    seeds.sort(key=lambda r: (-_venue_prior(r), -int(r.get("citation_count") or 0)))
    seed = seeds[0]
    try:
        year = int(seed.get("year") or 0)
    except (TypeError, ValueError):
        year = 0
    direction = "citations" if year and (time.gmtime().tm_year - year) >= 3 else "references"
    try:
        raw = traverse_citation_graph(seed["doi"], direction, max_results=8)
        payload = json.loads(raw)
        extra = payload.get("results") if isinstance(payload, dict) else None
        if isinstance(extra, list) and extra:
            merged = _merge_records(candidates + [r for r in extra
                                                   if isinstance(r, dict) and r.get("title")])
            logger.info("Citation graph (%s) added %d record(s) from seed %s",
                        direction, len(extra), seed.get("doi"))
            return merged
    except Exception as e:
        logger.debug("Citation expansion skipped: %s", e)
    return candidates


def search_for_turn(query: str) -> List[Dict[str, Any]]:
    """模拟研究者的单轮检索：先扫摘要，再改写/追引文，最后读一篇全文。"""
    from src.core.academic.literature import (_merge_records,
                                              search_academic_literature)

    text = str(query or "").strip()
    if len(text) < 6:
        return []

    def _search(search_text: str, min_year: int | None = None) -> List[Dict[str, Any]]:
        try:
            raw = search_academic_literature(search_text, max_results=25,
                                             include_preprints=True,
                                             min_year=min_year)
            payload = json.loads(raw)
        except Exception as e:
            logger.warning("Auto literature search failed: %s", e)
            return []
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return []
        return [rec for rec in results
                if isinstance(rec, dict) and rec.get("title")
                and not rec.get("is_retracted")]

    started = time.time()
    candidates = _search(text)
    # 结果太少时换个更干净的 query，而不是原样重搜。
    if len(candidates) < 3:
        refined = _refine_query(text)
        if refined:
            logger.info("Refining sparse search: '%s' -> '%s'", text[:60], refined[:60])
            candidates = _merge_records(candidates + _search(refined))
    # 如果命中全是老文献，再补一轮"近五年"——真人会主动看最近进展。
    years = []
    for rec in candidates:
        try:
            years.append(int(rec.get("year") or 0))
        except (TypeError, ValueError):
            pass
    current_year = time.gmtime().tm_year
    if (years and max(years) < current_year - 5
            and (time.time() - started) < 8.0):
        logger.info("All hits are older than 5 years; checking recent literature.")
        candidates = _merge_records(
            candidates + _search(text, min_year=current_year - 5))

    # 追一轮引文：像研究者从一篇关键论文的参考文献/被引列表继续找。
    # 预算不够时宁可少追一轮，也不要让整轮检索超时丢掉全部证据。
    if candidates and (time.time() - started) < 10.0:
        candidates = _expand_via_citations(candidates)
        candidates = [rec for rec in candidates if not rec.get("is_retracted")]
    # 自动文献只收英文：非英文来源的召回命中率低、同语言噪声多。放在抓 OA 全文
    # 之前挡掉，既避免污染全局库，也省下对非英文候选抓全文的开销。
    english_candidates = [rec for rec in candidates if _record_is_english(rec)]
    if len(english_candidates) != len(candidates):
        logger.info("Auto literature: dropped %d non-English candidate(s) of %d.",
                    len(candidates) - len(english_candidates), len(candidates))
    candidates = english_candidates
    # 优先高质量期刊，最多尝试 3 篇 OA 全文；失败就退回摘要。
    fulltext_deadline = started + 40.0
    fetched = 0
    attempts = 0
    ranked_for_fulltext = sorted(
        candidates, key=lambda r: (-_venue_prior(r), candidates.index(r)))
    for rec in ranked_for_fulltext:
        if (fetched >= MAX_FULLTEXT_FETCH or attempts >= 6
                or time.time() > fulltext_deadline):
            break
        if not rec.get("doi") or rec.get("_fulltext"):
            continue
        attempts += 1
        fulltext = _fetch_oa_fulltext(rec)
        if fulltext:
            rec["_fulltext"] = fulltext
            fetched += 1

    usable = [rec for rec in candidates
              if len(_record_text(rec).strip()) >= 80]
    picked = _select_records(usable)
    logger.info("Auto literature search: %d candidate(s) selected (%d full text)",
                len(picked), fetched)
    return picked


# --------------------------------------------------------------------------- #
#  入库 + 取回（子进程）
# --------------------------------------------------------------------------- #
def _chunk_text(text: str) -> List[str]:
    text = str(text or "").strip()
    if not text:
        return []
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
            separators=["\n\n", "\n", ". ", " ", ""], length_function=len)
        return [c for c in splitter.split_text(text) if c.strip()]
    except Exception:
        # 不因切分器缺失而丢证据：退化为定长切块
        return [text[i:i + CHUNK_SIZE] for i in range(0, len(text), CHUNK_SIZE - CHUNK_OVERLAP)]


#: 标题里不该出现的东西：模型/检索源把整段正文当标题时，先去掉行首引用号。
_TITLE_LEAD_MARK_RE = re.compile(r"^\[\d{1,3}\]\s*")
_SENTENCE_END_RE = re.compile(r"[.!?。！？]")


def _safe_title(record: Dict[str, Any]) -> str:
    """取一个**像标题**的标题；明显是段落时返回空串。

    真实故障：检索源把章节开头的一段话当成标题返回，于是证据面板里出现
    "Observe how implausible his claim is. He assessed his whole property…"
    这种行——既不能当著录名，点开"获取全文"还会把整段当标题显示。
    判据保守：超长（>200 字符）或含两个以上句末标点即视为段落。
    """
    raw = _TITLE_LEAD_MARK_RE.sub("", str(record.get("title") or "")).strip()
    raw = " ".join(raw.split())
    if not raw:
        return ""
    if len(raw) > 200 or len(_SENTENCE_END_RE.findall(raw)) >= 2:
        return ""
    return raw


def _display_title(record: Dict[str, Any]) -> str:
    """著录用的显示名：标题不像标题时退回 DOI，绝不留空或塞段落。"""
    title = _safe_title(record)
    if title:
        return title
    doi = str(record.get("doi") or "").strip()
    return f"DOI: {doi}" if doi else ""


def _ingest_records(records: List[Dict[str, Any]], conversation_id: str,
                    kb_id: str) -> int:
    """把摘要切成 chunk 写入自动库；按 paper_id 去重。返回新入库论文数。"""
    from src.core.config_manager import ConfigManager
    from src.core.kb_manager import DatabaseManager
    from src.task.kb_tasks import _worker_load_model

    cfg = ConfigManager()
    embed_fn = _worker_load_model(kb_id, cfg)
    db = DatabaseManager()
    if not db.switch_kb(kb_id, embedding_function=embed_fn):
        raise RuntimeError("Cannot connect to automatic literature KB")

    added = 0
    skipped = 0
    for rec in records:
        title = _display_title(rec)
        if not title:
            # 既没有可读标题、也没有 DOI：这种条目只会在面板里变成一堵字墙，
            # 入库与引用都没有意义。
            skipped += 1
            continue
        pid = _paper_id(rec)
        try:
            existing = db.collection.get(where={"paper_id": pid}, limit=1)
            if existing and existing.get("ids"):
                continue
        except Exception as e:
            logger.debug("Dedupe lookup failed for %s: %s", pid, e)

        chunks = _chunk_text(_record_text(rec))
        if not chunks:
            continue
        authors = rec.get("authors")
        if isinstance(authors, list):
            authors = "; ".join(str(a) for a in authors)
        meta_base = {
            "paper_id": pid,
            "conversation_id": str(conversation_id or ""),
            "visibility": "global",
            "origin": "auto_search",
            "doi": str(rec.get("doi") or ""),
            "title": str(rec.get("title") or ""),
            "authors": str(authors or ""),
            "year": str(rec.get("year") or ""),
            "journal": str(rec.get("journal") or ""),
            "url": str(rec.get("url") or ""),
            "type": str(rec.get("type") or "article"),
            "page": 1,
            "has_fulltext": bool(rec.get("_fulltext")),
            "file_path": "",
        }
        ids = [f"{pid}_{i}" for i in range(len(chunks))]
        metas = [dict(meta_base, chunk_index=i) for i in range(len(chunks))]
        db.add_documents(documents=chunks, metadatas=metas, ids=ids)
        added += 1
    # 注意：这里**不写 manifest**。证据面板只登记"本轮真正用到的来源"
    # （见 _worker 里的 sources_map），否则检索返回的每一条都会被记进对话，
    # 面板里就堆满了与本次提问无关的论文。
    if records:
        _mark_index_ready(kb_id)
    logger.info("Auto evidence ingest: %d new paper(s) from %d record(s)%s",
                added, len(records),
                f", {skipped} skipped (no usable title)" if skipped else "")
    return added


def _retrieve(query: str, conversation_id: str,
              main_model_name: str = "", main_base_url: str = "",
              declared_window: Any = None,
              paper_ids: Optional[List[str]] = None) -> Tuple[str, Dict[int, Dict[str, Any]]]:
    """从自动库检索公开证据 + 当前会话私有证据，返回 (context_str, sources_map)。"""
    from src.core.config_manager import ConfigManager
    from src.core.kb_manager import DatabaseManager
    from src.core.rerank_engine import RerankEngine
    from src.core.token_estimator import (derive_context_budgets,
                                          estimate_tokens,
                                          resolve_context_window)
    from src.task.kb_tasks import _worker_load_model

    kb_id = ensure_auto_kb()
    if not kb_id:
        return "", {}
    cfg = ConfigManager()
    embed_fn = _worker_load_model(kb_id, cfg)
    db = DatabaseManager()
    if not db.switch_kb(kb_id, embedding_function=embed_fn):
        return "", {}

    # 公开文献全局可查；私有 PDF 只查当前会话。两路分开 query 再合并，
    # 避免 Chroma where 的 $or 语法在不同版本里行为不一致。
    scoped_ids = [str(pid) for pid in (paper_ids or []) if pid]
    if scoped_ids:
        # 只查本轮检索到的论文，避免全局库里历史积累的其他学科文献混进来。
        wheres = [{"$and": [{"visibility": "global"},
                            {"paper_id": {"$in": scoped_ids}}]}]
    else:
        wheres = [{"visibility": "global"}]
    if conversation_id:
        wheres.append({"$and": [{"visibility": "private"},
                                {"conversation_id": str(conversation_id)}]})
    window = resolve_context_window(main_model_name, main_base_url, declared_window)
    # 大窗口模型没必要只取 20 条候选；1M 级窗口给 40 条，重排后再按预算裁。
    n_results = 40 if window >= 200_000 else 20
    candidates: List[Dict[str, Any]] = []
    seen = set()
    for where in wheres:
        try:
            raw = db.query(query, n_results=n_results, where=where)
        except Exception as e:
            logger.debug("Auto evidence query failed (%s): %s", where, e)
            continue
        if not raw or not raw.get("documents") or not raw["documents"][0]:
            continue
        docs = raw["documents"][0]
        metas = raw.get("metadatas", [[]])[0]
        distances = raw.get("distances", [[0] * len(docs)])[0]
        for i, text in enumerate(docs):
            clean = str(text or "").strip()
            if len(clean) < 20 or clean in seen:
                continue
            seen.add(clean)
            candidates.append({"content": clean, "metadata": metas[i] if i < len(metas) else {},
                               "v_dist": distances[i] if i < len(distances) else 0})
    if not candidates:
        return "", {}

    candidates = sorted(candidates, key=lambda x: x.get("v_dist", 0))[:40]
    try:
        ranked = RerankEngine().rerank(query, candidates, domain="General Academic",
                                       top_k=len(candidates))
        scored = [d for d in (ranked or []) if d.get("score") is not None]
        if scored:
            top = max(float(d.get("score") or 0) for d in scored)
            if top <= 1.0:
                # Qwen3-Reranker 输出的是 yes/no 概率（0–1）。早前这里用 0.05 做阈值，
                # 等于"几乎不过滤"——于是检索源里的无关论文（表观遗传、结核病…）
                # 全被当成证据堆进面板。概率模型必须用一个真正的下限。
                threshold = MIN_RELEVANCE_PROB
            else:
                # BGE 交叉编码器输出 logits：相关为正、无关为负。
                threshold = 0.0
            docs = [d for d in scored if float(d.get("score") or 0) >= threshold]
            # 同语言候选再加一道术语检查（见 _same_language_related）。
            before = len(docs)
            docs = [d for d in docs if _same_language_related(query, d)]
            if before != len(docs):
                logger.info("Dropped %d same-language candidate(s) sharing no term with the "
                            "query.", before - len(docs))
            # 自动文献只收英文：库里可能残留历史入库的非英文条目，读侧再挡一道，
            # 保证进入上下文与证据面板的来源全是英文（与入库侧的同一判据）。
            before = len(docs)
            docs = [d for d in docs if _doc_is_english(d)]
            if before != len(docs):
                logger.info("Dropped %d non-English candidate(s) from the auto library.",
                            before - len(docs))
            if not docs:
                logger.info("All %d candidate(s) scored below the relevance floor (%.2f); "
                            "no literature evidence will be attached this turn.",
                            len(scored), threshold)
        else:
            # 重排模型不可用：退回词面过滤，而不是把所有候选原样塞进上下文
            # （那正是无关论文堆进证据面板的老路）。
            tokens = _query_tokens(query)
            docs = [c for c in candidates
                    if _lexically_relevant(c.get("content", ""), query, tokens)][:MAX_CONTEXT_DOCS]
            logger.info("Reranker unavailable; lexical filter kept %d/%d candidate(s).",
                        len(docs), len(candidates))
    except Exception as e:
        logger.warning("Auto evidence rerank failed: %s", e)
        docs = candidates[:MAX_CONTEXT_DOCS]

    budgets = derive_context_budgets(
        resolve_context_window(main_model_name, main_base_url, declared_window))
    budget = budgets["kb_token_budget"]
    chunk_chars = budgets["kb_chunk_chars"]
    context_str = ""
    sources_map: Dict[int, Dict[str, Any]] = {}
    used_tokens = 0
    ref = 1
    for doc in docs:
        if ref > MAX_CONTEXT_DOCS:
            break
        meta = doc.get("metadata") or {}
        content = str(doc.get("content") or "")
        if len(content) > chunk_chars:
            content = content[:chunk_chars] + "...[chunk truncated]"
        tokens = estimate_tokens(content)
        if used_tokens + tokens > budget:
            break
        used_tokens += tokens
        title = str(meta.get("title") or "")
        doi = str(meta.get("doi") or "")
        journal = str(meta.get("journal") or "")
        year = str(meta.get("year") or "")
        context_str += (
            f"--- [Document {ref}] ---\n"
            f"Title: {title}\n"
            f"DOI: {doi}\n"
            f"Source: {journal} {year}\n"
            f"Content: {content}\n\n"
        )
        sources_map[ref] = {
            "paper_id": str(meta.get("paper_id") or ""),
            "doi": doi,
            "title": title,
            "authors": str(meta.get("authors") or ""),
            "year": year,
            "journal": journal,
            "url": str(meta.get("url") or ""),
            "kind": str(meta.get("type") or "article"),
            "page": int(meta.get("page") or 1),
            "path": str(meta.get("file_path") or ""),
            # 重排分数随来源一起带出：只有高置信来源才值得主动弹窗要全文。
            "score": round(float(doc.get("score") or 0.0), 3),
            "search_text": str(doc.get("content") or ""),
        }
        ref += 1
    logger.info("Auto evidence retrieval: %d chunk(s) -> %d citation(s)",
                len(candidates), len(sources_map))
    return context_str, sources_map


# --------------------------------------------------------------------------- #
#  私有 PDF：付费墙下载 / 手动上传
# --------------------------------------------------------------------------- #
_DOI_RE = None
MAX_PDF_CHUNKS = 400


def _extract_pdf(path: str) -> Tuple[List[Tuple[int, str]], Dict[str, Any]]:
    """返回 ``[(页码, 文本)]`` 与 PDF 内嵌元数据；解析失败抛异常。

    性能约束（这是全链路最贵的一步）：
    * **只打开一次**：元数据、首页 DOI、正文都从同一个 ``fitz.Document`` 取；
      ``pymupdf4llm`` 直接接收已打开的 doc（旧版本不支持该参数时退回传路径，
      但那只在库版本过低时发生一次）。
    * **markdown 失败就降级纯文本**：布局分析（表格/列表重建）在几十页的 PDF 上
      是数十秒级；拿不到 markdown 时不重试，直接按页 ``get_text`` —— 对检索来说
      纯文本完全够用，比"再解析一遍"划算得多。
    """
    import fitz  # PyMuPDF

    pages: List[Tuple[int, str]] = []
    meta: Dict[str, Any] = {}
    first_text = ""
    page_data: List[Dict[str, Any]] = []
    doc = None
    try:
        doc = fitz.open(path)
        info = doc.metadata or {}
        meta = {
            "title": str(info.get("title") or "").strip(),
            "authors": str(info.get("author") or "").strip(),
            "journal": str(info.get("subject") or "").strip(),
            "year": "",
        }
        first_text = " ".join(
            doc[i].get_text() for i in range(min(3, doc.page_count)))

        try:
            import pymupdf4llm
            try:
                page_data = pymupdf4llm.to_markdown(doc, page_chunks=True) or []
            except TypeError:           # 旧版只接受路径
                page_data = pymupdf4llm.to_markdown(path, page_chunks=True) or []
        except Exception as e:
            logger.debug("Markdown extraction unavailable (%s); falling back to text.", e)
            page_data = []

        for chunk in page_data:
            text = str(chunk.get("text") or "").strip()
            if not text:
                continue
            page = int((chunk.get("metadata") or {}).get("page", 0)) + 1
            pages.append((page, text))

        if not pages:
            # 降级：按页纯文本，一次遍历即可（C 层取词，比 markdown 快一个量级）
            for index in range(doc.page_count):
                text = (doc[index].get_text("text") or "").strip()
                if text:
                    pages.append((index + 1, text))
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass

    # DOI 通常出现在首页/页眉；用正则兜底，避免依赖 PDF 的 XMP 元数据。
    global _DOI_RE
    if _DOI_RE is None:
        import re as _re
        _DOI_RE = _re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
    match = _DOI_RE.search(first_text)
    if match:
        meta["doi"] = match.group(0).rstrip(".,;).")
    return pages, meta


def _private_pdf_worker(path: str, conversation_id: str, meta: Dict[str, Any],
                        origin: str, out_path: str) -> None:
    from src.task.kb_tasks import _setup_worker_env
    _setup_worker_env()
    result = {"ok": False, "message": ""}
    try:
        from src.core import BASE_DIR
        from src.core.config_manager import ConfigManager
        from src.core.kb_manager import DatabaseManager
        from src.task.kb_tasks import _worker_load_model

        source = str(path or "")
        if not os.path.isfile(source):
            raise FileNotFoundError(source)
        file_hash = _file_sha256(source)
        meta = dict(meta or {})

        # 命中索引就直接返回：同一份文件已经在本对话取过全文时，不必再解析一遍
        # （大 PDF 的布局分析是这条链路上最贵的一步，通常是几十秒级）。
        try:
            from src.core import pdf_index
            cached = pdf_index.lookup(sha256=file_hash)
        except Exception:
            cached = None
        if cached and str(cached.get("conversation_id") or "") == str(conversation_id):
            result = {"ok": True,
                      "message": f"该 PDF 已在本对话证据库中（{cached.get('title') or 'cached'}）。"}
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
            return

        pages, pdf_meta = _extract_pdf(source)
        if not pages:
            raise RuntimeError("PDF 没有可提取的文本（可能是扫描件，需 OCR）")
        for key in ("title", "authors", "journal", "year", "doi"):
            if not meta.get(key) and pdf_meta.get(key):
                meta[key] = pdf_meta[key]
        doi = str(meta.get("doi") or "")
        pid = _paper_id({"doi": doi, "title": meta.get("title") or file_hash})

        # 解析完先做一次**不加载模型**的查重：同一对话已有这篇的分片就结束。
        # 加载 embedding 模型（数百 MB）比读一次 Chroma 元数据贵得多。
        try:
            collection = _auto_collection()
            if collection is not None:
                found = collection.get(where={"$and": [{"paper_id": pid},
                                                       {"conversation_id": str(conversation_id)}]},
                                       limit=1)
                if found.get("ids"):
                    result = {"ok": True, "message": "该文献已在本对话证据库中。"}
                    with open(out_path, "w", encoding="utf-8") as f:
                        json.dump(result, f, ensure_ascii=False)
                    return
        except Exception as e:
            logger.debug("Pre-dedupe check skipped: %s", e)

        kb_id = ensure_auto_kb()
        if not kb_id:
            raise RuntimeError("无法创建自动证据库")
        cfg = ConfigManager()
        embed_fn = _worker_load_model(kb_id, cfg)
        db = DatabaseManager()
        if not db.switch_kb(kb_id, embedding_function=embed_fn):
            raise RuntimeError("无法连接自动证据库")

        # 同一会话已导入同一文件/DOI：直接视为成功，避免重复解析。
        existing = db.collection.get(
            where={"$and": [{"paper_id": pid},
                            {"conversation_id": str(conversation_id)}]}, limit=1)
        stored = ""
        if existing and existing.get("ids"):
            result = {"ok": True, "message": "该文献已在本对话证据库中。"}
        else:
            # 私有 PDF 复制到内容寻址目录，避免临时下载目录被清理后引用失效。
            private_dir = os.path.join(BASE_DIR, "scholar_workspace", "private_pdfs")
            os.makedirs(private_dir, exist_ok=True)
            stored = os.path.join(private_dir, f"{file_hash}.pdf")
            if not os.path.isfile(stored):
                tmp = stored + ".part"
                # shutil.copyfile 走 sendfile/copy_file_range（内核态搬运），比
                # Python 逐块读写快得多，尤其对大 PDF。
                shutil.copyfile(source, tmp)
                os.replace(tmp, stored)
                try:
                    os.chmod(stored, 0o600)
                except OSError:
                    pass

            # 登记到 PDF 索引：此后"这篇有没有下过/文件在哪"都是 O(log n) 查询，
            # 不必遍历 private_pdfs 目录或整份重读 manifest。
            try:
                from src.core import pdf_index
                pdf_index.index_pdf(
                    pid, stored,
                    doi=doi, title=str(meta.get("title") or ""),
                    sha256=file_hash, conversation_id=str(conversation_id),
                    origin=str(origin or "manual_upload"))
            except Exception as e:
                logger.debug("PDF index update skipped: %s", e)

            from langchain_text_splitters import RecursiveCharacterTextSplitter
            splitter = RecursiveCharacterTextSplitter(
                chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
                separators=["\n\n", "\n", ". ", " ", ""], length_function=len)
            docs, metas, ids = [], [], []
            for page, text in pages:
                for ci, chunk in enumerate(splitter.split_text(text)):
                    if len(docs) >= MAX_PDF_CHUNKS:
                        break
                    docs.append(chunk)
                    metas.append({
                        "paper_id": pid,
                        "conversation_id": str(conversation_id),
                        "visibility": "private",
                        "origin": str(origin or "manual_upload"),
                        "doi": doi,
                        "title": str(meta.get("title") or os.path.basename(source)),
                        "authors": str(meta.get("authors") or ""),
                        "year": str(meta.get("year") or ""),
                        "journal": str(meta.get("journal") or ""),
                        "url": str(meta.get("url") or (f"https://doi.org/{doi}" if doi else "")),
                        "type": "article",
                        "page": int(page),
                        "chunk_index": ci,
                        "file_path": stored,
                        "has_fulltext": True,
                    })
                    ids.append(f"{pid}_{page}_{ci}")
                if len(docs) >= MAX_PDF_CHUNKS:
                    logger.warning("Private PDF chunk cap reached for %s", source)
                    break
            if not docs:
                raise RuntimeError("PDF 文本无法切块")
            db.add_documents(documents=docs, metadatas=metas, ids=ids)
            _mark_index_ready(kb_id)
            result = {"ok": True,
                      "message": f"已加入本对话证据库：{meta.get('title') or os.path.basename(source)}"}
        record_conversation_papers(conversation_id, [{
            "paper_id": pid,
            "title": meta.get("title") or os.path.basename(source),
            "doi": doi,
            "year": meta.get("year") or "",
            "journal": meta.get("journal") or "",
            "url": meta.get("url") or (f"https://doi.org/{doi}" if doi else ""),
            "path": stored,
            "has_fulltext": True,
            "visibility": "private",
            "origin": origin or "manual_upload",
        }])
        # 私有 PDF 也必须写快照：换 embedding 模型后要能按原文重建索引，且导出
        # 归档时要把这篇文献一起带走。
        _snapshot_conversation_evidence(conversation_id)
    except Exception as e:
        logger.exception("Private PDF ingestion failed: %s", e)
        result = {"ok": False, "message": str(e)}
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
    except OSError:
        pass


def run_private_pdf_ingest(path: str, conversation_id: str,
                           meta: Optional[Dict[str, Any]] = None,
                           origin: str = "manual_upload",
                           cancel_check=None) -> Tuple[bool, str]:
    """父进程入口：子进程解析 + 入库私有 PDF。"""
    fd, tmp_path = tempfile.mkstemp(prefix="private_pdf_", suffix=".json")
    os.close(fd)
    try:
        proc = mp.Process(target=_private_pdf_worker,
                          args=(path, conversation_id, dict(meta or {}),
                                origin, tmp_path), daemon=True)
        proc.start()
        deadline = time.time() + 120.0
        while proc.is_alive():
            if cancel_check is not None and cancel_check():
                proc.terminate()
                break
            if time.time() > deadline:
                proc.terminate()
                break
            time.sleep(0.1)
        proc.join(timeout=2.0)
        if os.path.exists(tmp_path):
            with open(tmp_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            ok = bool(data.get("ok"))
            if ok:
                invalidate_context_cache(conversation_id)
            return ok, str(data.get("message") or "")
    except Exception as e:
        logger.warning("Private PDF process failed: %s", e)
        return False, str(e)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return False, "PDF 入库超时或被取消"


def _private_docs_worker(docs: List[Dict[str, Any]], conversation_id: str,
                         origin: str, out_path: str) -> None:
    """把聊天附件已解析出的页级文本写入当前会话私有证据库。"""
    from src.task.kb_tasks import _setup_worker_env
    _setup_worker_env()
    result = {"ok": False, "message": ""}
    try:
        from src.core.config_manager import ConfigManager
        from src.core.kb_manager import DatabaseManager
        from src.task.kb_tasks import _worker_load_model
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for d in docs or []:
            if not isinstance(d, dict):
                continue
            content = str(d.get("content") or "").strip()
            if len(content) < 40:
                continue
            key = str(d.get("path") or d.get("name") or "attachment")
            grouped.setdefault(key, []).append(d)
        if not grouped:
            result = {"ok": True, "message": "没有可入库的附件文本。"}
            raise StopIteration

        kb_id = ensure_auto_kb()
        if not kb_id:
            raise RuntimeError("无法创建自动证据库")
        cfg = ConfigManager()
        embed_fn = _worker_load_model(kb_id, cfg)
        db = DatabaseManager()
        if not db.switch_kb(kb_id, embedding_function=embed_fn):
            raise RuntimeError("无法连接自动证据库")

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
            separators=["\n\n", "\n", ". ", " ", ""], length_function=len)
        added = 0
        manifest_papers: List[Dict[str, Any]] = []
        for key, items in grouped.items():
            name = str(items[0].get("name") or os.path.basename(key) or key)
            pid = hashlib.sha1(f"upload:{key}".encode("utf-8")).hexdigest()[:24]
            manifest_papers.append({
                "paper_id": pid,
                "title": name,
                "path": str(items[0].get("path") or ""),
                "has_fulltext": True,
                "visibility": "private",
                "origin": origin or "manual_upload",
            })
            try:
                existing = db.collection.get(
                    where={"$and": [{"paper_id": pid},
                                    {"conversation_id": str(conversation_id)}]}, limit=1)
                if existing and existing.get("ids"):
                    continue
            except Exception:
                pass
            docs_batch, metas, ids = [], [], []
            for item in items:
                page = int(item.get("page") or 1)
                for ci, chunk in enumerate(splitter.split_text(str(item.get("content") or ""))):
                    if len(docs_batch) >= MAX_PDF_CHUNKS:
                        break
                    docs_batch.append(chunk)
                    metas.append({
                        "paper_id": pid,
                        "conversation_id": str(conversation_id),
                        "visibility": "private",
                        "origin": str(origin or "manual_upload"),
                        "doi": "",
                        "title": name,
                        "authors": "",
                        "year": "",
                        "journal": "",
                        "url": "",
                        "type": "article",
                        "page": page,
                        "chunk_index": ci,
                        "file_path": str(item.get("path") or ""),
                        "has_fulltext": True,
                    })
                    ids.append(f"{pid}_{page}_{ci}")
                if len(docs_batch) >= MAX_PDF_CHUNKS:
                    break
            if docs_batch:
                db.add_documents(documents=docs_batch, metadatas=metas, ids=ids)
                added += 1
        if manifest_papers:
            record_conversation_papers(conversation_id, manifest_papers)
            _snapshot_conversation_evidence(conversation_id)
        if added:
            _mark_index_ready(kb_id)
        result = {"ok": True, "message": f"已加入 {added} 个附件到本对话证据库。"}
    except StopIteration:
        pass
    except Exception as e:
        logger.exception("Private docs ingestion failed: %s", e)
        result = {"ok": False, "message": str(e)}
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
    except OSError:
        pass


def run_private_docs_ingest(docs: List[Dict[str, Any]], conversation_id: str,
                            origin: str = "manual_upload") -> Tuple[bool, str]:
    """父进程入口：后台把附件页文本写入私有证据库。"""
    if not docs:
        return True, ""
    fd, tmp_path = tempfile.mkstemp(prefix="private_docs_", suffix=".json")
    os.close(fd)
    try:
        proc = mp.Process(target=_private_docs_worker,
                          args=(list(docs), conversation_id, origin, tmp_path),
                          daemon=True)
        proc.start()
        deadline = time.time() + 120.0
        while proc.is_alive():
            if time.time() > deadline:
                proc.terminate()
                break
            time.sleep(0.1)
        proc.join(timeout=2.0)
        if os.path.exists(tmp_path):
            with open(tmp_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            ok = bool(data.get("ok"))
            if ok:
                invalidate_context_cache(conversation_id)
            return ok, str(data.get("message") or "")
    except Exception as e:
        logger.warning("Private docs process failed: %s", e)
        return False, str(e)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return False, "附件入库超时"


def _direct_context(records: List[Dict[str, Any]], main_model_name: str = "",
                    main_base_url: str = "", declared_window: Any = None,
                    query: str = ""
                    ) -> Tuple[str, Dict[int, Dict[str, Any]]]:
    """无 embedding 模型时的降级路径：直接把检索到的摘要放进上下文。

    这保证"自动文献"不会因为本地模型未下载就完全失效；引用仍然是论文级
    （标题/DOI/逐字摘要），只是没有向量库的跨轮缓存。

    这里同样要过滤无关结果：检索接口一次返回 30+ 条，其中大量是同一关键词的
    跨领域命中。不做词面过滤就全塞进上下文（并登记到证据面板），正是"证据里
    全是无关文献"的来源。
    """
    tokens = _query_tokens(query)
    if tokens:
        kept = [r for r in records
                if _lexically_relevant(_record_text(r), query, tokens)]
        if kept:
            logger.info("Direct-abstract fallback kept %d/%d record(s) after lexical filter.",
                        len(kept), len(records))
            records = kept
        else:
            # 全被过滤掉说明查询与检索结果词面对不上（同义词/翻译问题）：
            # 与其一条不留，不如保留少量最好的候选，交由引用门禁约束。
            records = records[:3]
            logger.info("Direct-abstract fallback: lexical filter matched nothing; "
                        "keeping the top 3 record(s).")
    from src.core.token_estimator import (derive_context_budgets, estimate_tokens,
                                          resolve_context_window)
    budgets = derive_context_budgets(
        resolve_context_window(main_model_name, main_base_url, declared_window))
    budget = budgets["kb_token_budget"]
    chunk_chars = budgets["kb_chunk_chars"]
    context_str = ""
    sources_map: Dict[int, Dict[str, Any]] = {}
    used = 0
    ref = 1
    for rec in records:
        if ref > MAX_CONTEXT_DOCS:
            break
        text = _record_text(rec)
        if not text:
            continue
        if len(text) > chunk_chars:
            text = text[:chunk_chars] + "...[chunk truncated]"
        tokens = estimate_tokens(text)
        if used + tokens > budget:
            break
        used += tokens
        # 标题必须是"像标题"的：段落式标题在上下文里会被模型当成正文引用。
        title = _display_title(rec)
        if not title:
            continue
        doi = str(rec.get("doi") or "")
        journal = str(rec.get("journal") or "")
        year = str(rec.get("year") or "")
        authors = rec.get("authors")
        if isinstance(authors, list):
            authors = "; ".join(str(a) for a in authors)
        context_str += (
            f"--- [Document {ref}] ---\n"
            f"Title: {title}\n"
            f"DOI: {doi}\n"
            f"Source: {journal} {year}\n"
            f"Content: {text}\n\n"
        )
        sources_map[ref] = {
            "paper_id": _paper_id(rec),
            "doi": doi, "title": title, "authors": str(authors or ""),
            "year": year, "journal": journal, "url": str(rec.get("url") or ""),
            "kind": str(rec.get("type") or "article"), "page": 1,
            "search_text": _record_text(rec),
        }
        ref += 1
    return context_str, sources_map


def _worker(query: str, conversation_id: str, main_model_name: str,
            main_base_url: str, declared_window: Any, out_path: str) -> None:
    """短命子进程入口：检索 + 入库 + 取回，结果写 JSON。"""
    from src.task.kb_tasks import _setup_worker_env
    _setup_worker_env()
    context_str, sources_map = "", {}
    warnings: List[str] = []
    try:
        records = search_for_turn(query)
        current_ids = [_paper_id(rec) for rec in records
                       if isinstance(rec, dict)] if records else []
        if records:
            model_ready = False
            try:
                kb_id = ensure_auto_kb()
                if kb_id:
                    # 换过模型 / 新导入的对话：先把快照里的正文按当前模型补进索引，
                    # 再入库本轮的检索结果。
                    _ensure_conversation_indexed(conversation_id, kb_id)
                    _ingest_records(records, conversation_id, kb_id)
                    model_ready = True
            except Exception as e:
                # 典型场景：本地 embedding 未下载 / 与库绑定的模型已不存在。
                # 不要让自动文献整个失效，退化为直接使用检索到的摘要；同时把
                # 原因带出去——子进程的日志到不了应用日志，不往回传就等于静默失败。
                warnings.append(f"vector ingestion unavailable: {e}")
                logger.warning("Auto evidence ingestion unavailable (%s); "
                               "falling back to direct abstracts.", e)
            if model_ready:
                try:
                    context_str, sources_map = _retrieve(
                        query, conversation_id, main_model_name,
                        main_base_url, declared_window,
                        paper_ids=current_ids)
                except Exception as e:
                    logger.warning("Auto evidence vector retrieval failed: %s", e)
            # 只有向量路径**一条都没有**时才用直接摘要兜底。原来的"少于 6 条就
            # 补足"会把重排判为无关的结果重新拉回来（补足的那批恰恰是没通过
            # 相关度阈值的），等于绕过过滤。
            if records and not sources_map:
                direct_ctx, direct_sources = _direct_context(
                    records, main_model_name, main_base_url, declared_window,
                    query=query)
                if len(direct_sources) > len(sources_map):
                    logger.info("Auto evidence: vector hits %d < 6; "
                                "using %d direct abstracts.",
                                len(sources_map), len(direct_sources))
                    context_str, sources_map = direct_ctx, direct_sources
            if not context_str:
                context_str, sources_map = _direct_context(
                    records, main_model_name, main_base_url, declared_window,
                    query=query)
        else:
            try:
                context_str, sources_map = _retrieve(
                    query, conversation_id, main_model_name,
                    main_base_url, declared_window)
            except Exception as e:
                logger.debug("Auto evidence retrieval skipped: %s", e)
        if sources_map:
            # 本轮真正进入上下文的论文一律登记到 manifest，**不论**它们来自向量
            # 检索还是降级路径的直接摘要。证据面板只认这份关联：向量入库失败时
            # 不做这一步，用户就会看到"正文有引用，面板永远为空"。
            record_conversation_papers(conversation_id, [
                {
                    "paper_id": str(meta.get("paper_id") or ""),
                    "title": meta.get("title"), "doi": meta.get("doi"),
                    "year": meta.get("year"), "journal": meta.get("journal"),
                    "url": meta.get("url"), "path": meta.get("path"),
                    "type": meta.get("kind") or meta.get("type") or "article",
                    "has_fulltext": meta.get("has_fulltext"),
                    "visibility": meta.get("visibility") or "global",
                    "origin": "auto_search",
                }
                for meta in sources_map.values() if meta.get("paper_id")
            ])
            # 面板/关联收窄到本轮真正进入上下文的论文，避免历史无关文献长期堆着。
            keep_ids = [str(m.get("paper_id") or "")
                        for m in sources_map.values() if m.get("paper_id")]
            if keep_ids:
                prune_conversation_global_evidence(conversation_id, keep_ids)
        # 收尾快照：本轮使用的文献与正文写入会话库（历史 / 导出 / 换模型重建都靠它）
        _snapshot_conversation_evidence(conversation_id)
        payload = {"context_str": context_str,
                   "sources_map": {str(k): v for k, v in sources_map.items()},
                   "warnings": warnings}
    except Exception as e:
        logger.exception("Auto evidence worker failed: %s", e)
        payload = {"context_str": "", "sources_map": {}, "error": str(e),
                   "warnings": warnings}
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except OSError as e:
        logger.warning("Cannot write auto evidence result: %s", e)


_SKIP_QUERIES = {
    "hi", "hello", "hey", "thanks", "thank you", "ok", "okay", "继续", "谢谢",
    "好的", "好", "嗯", "收到", "继续吧", "go on", "continue",
}

#: 会话级短期缓存：追问/同一问题不重复起子进程、不重复加载 embedding。
#: 值是 (时间戳, 原始 query, context_str, sources_map, 有效标志)。
_CONTEXT_CACHE: Dict[str, Tuple[float, str, str, Dict[int, Dict[str, Any]]]] = {}
_CACHE_TTL = 180.0
_FOLLOWUP_HINTS = ("继续", "那", "这个", "上述", "上面", "它", "他们", "该",
                   "continue", "what about", "and ", "then ", "this ", "that ")


def invalidate_context_cache(conversation_id: str) -> None:
    _CONTEXT_CACHE.pop(str(conversation_id or ""), None)


def _cache_hit(conversation_id: str, query: str) -> Optional[Tuple[str, Dict[int, Dict[str, Any]]]]:
    item = _CONTEXT_CACHE.get(str(conversation_id or ""))
    if not item:
        return None
    ts, last_query, context, sources = item
    if not context or time.time() - ts > _CACHE_TTL:
        return None
    low = query.strip().lower()
    if low == last_query or low.startswith(_FOLLOWUP_HINTS) or len(low) <= 12:
        return context, {k: dict(v) for k, v in sources.items()}
    return None


def run_auto_evidence(query: str, conversation_id: str,
                      main_model_name: str = "", main_base_url: str = "",
                      declared_window: Any = None,
                      cancel_check=None) -> Tuple[str, Dict[int, Dict[str, Any]]]:
    """父进程入口：起子进程执行，超时/取消/失败返回空证据。"""
    text = str(query or "").strip()
    if len(text) < 6 or text.lower() in _SKIP_QUERIES:
        return "", {}
    cached = _cache_hit(conversation_id, text)
    if cached is not None:
        logger.info("Auto evidence cache hit for conversation %s", conversation_id)
        return cached
    fd, tmp_path = tempfile.mkstemp(prefix="auto_evidence_", suffix=".json")
    os.close(fd)
    proc = None
    try:
        proc = mp.Process(
            target=_worker,
            args=(query, conversation_id, main_model_name,
                  main_base_url, declared_window, tmp_path),
            daemon=True,
        )
        proc.start()
        deadline = time.time() + WORKER_TIMEOUT
        while proc.is_alive():
            if cancel_check is not None and cancel_check():
                logger.info("Auto evidence worker cancelled by user.")
                proc.terminate()
                break
            if time.time() > deadline:
                logger.warning("Auto evidence worker timed out; terminating.")
                proc.terminate()
                break
            time.sleep(0.1)
        proc.join(timeout=2.0)
        if os.path.exists(tmp_path):
            with open(tmp_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 子进程的日志到不了应用日志：把降级原因带回父进程记一笔，否则
            # "证据面板永远为空"这类问题在日志里连线索都没有。
            for note in (data.get("warnings") or []):
                logger.warning("Auto evidence: %s", note)
            if data.get("error"):
                logger.warning("Auto evidence worker error: %s", data.get("error"))
            sources = {int(k): v for k, v in (data.get("sources_map") or {}).items()}
            context = str(data.get("context_str") or "")
            if context:
                _CONTEXT_CACHE[str(conversation_id or "")] = (
                    time.time(), text, context, sources)
            return context, sources
    except Exception as e:
        logger.warning("Auto evidence process unavailable: %s", e)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return "", {}


__all__ = [
    "AUTO_KB_NAME", "ensure_auto_kb", "search_for_turn",
    "run_auto_evidence", "run_private_pdf_ingest", "run_private_docs_ingest",
    "delete_conversation_evidence", "invalidate_context_cache",
    "list_conversation_evidence", "delete_private_evidence",
    "export_evidence_bundle", "import_evidence_bundle", "EVIDENCE_BUNDLE_VERSION",
]
