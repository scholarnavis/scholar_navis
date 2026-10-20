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
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Core.AutoEvidence")

#: 自动库显示名。用户不直接选择库；它始终在后台存在。
AUTO_KB_NAME = "Auto Literature"
#: meta.json 里的标记，避免把用户手工建的库误当成自动库。
AUTO_KB_MARKER = "auto_evidence"

#: 单轮最多新入库的论文数 / 进入上下文的论文数。
MAX_NEW_PAPERS = 12
MAX_CONTEXT_DOCS = 20
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


def ensure_auto_kb() -> Optional[str]:
    """返回自动库 id；不存在时创建。失败返回 ``None``（调用方静默降级）。"""
    try:
        from src.core.kb_manager import KBManager
        mgr = KBManager()
        for info in mgr.get_all_kbs():
            meta_path = os.path.join(info.get("root_path", ""), "meta.json")
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                continue
            if not (meta.get(AUTO_KB_MARKER)
                    or str(meta.get("name") or "") == AUTO_KB_NAME):
                continue
            # 自动库可能是在另一个模型上创建的；如果当前模型不可用且库还没有
            # 任何向量，直接切到本机已有的 embedding，避免"模型未下载 -> 永远
            # 无法入库"。一旦写入过 chunk，就锁定模型，不能中途换（维度会变）。
            if int(info.get("doc_count") or 0) == 0 and not meta.get("auto_index_ready"):
                alt = _available_embedding_id()
                if alt != "embed_auto" and meta.get("model_id") != alt:
                    from src.core.models_registry import get_model_conf
                    meta["model_id"] = alt
                    meta["model_info"] = get_model_conf(alt, "embedding")
                    try:
                        with open(meta_path, "w", encoding="utf-8") as f:
                            json.dump(meta, f, indent=4)
                        logger.info("Auto literature KB switched to available embedding: %s", alt)
                    except OSError as e:
                        logger.warning("Cannot update auto KB model: %s", e)
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


def list_conversation_evidence(conversation_id: str) -> List[Dict[str, Any]]:
    """列出某对话已入库的证据（公开自动文献 + 私有 PDF/附件）。

    只按 metadata 读取，不做向量检索，因此不需要加载 embedding 模型。
    """
    cid = str(conversation_id or "")
    if not cid:
        return []
    try:
        root = _auto_kb_root()
        manifest = (_load_manifest(root).get(cid) or {}) if root else {}
        grouped: Dict[str, Dict[str, Any]] = {}
        # manifest 先铺底：全局文献可能只在别的对话写过 chunk，但当前对话
        # 确实使用过它，面板仍应列出来。
        for pid, entry in manifest.items():
            if not isinstance(entry, dict):
                continue
            grouped[str(pid)] = {
                "paper_id": str(pid),
                "title": str(entry.get("title") or "Untitled"),
                "doi": str(entry.get("doi") or ""),
                "year": str(entry.get("year") or ""),
                "journal": str(entry.get("journal") or ""),
                "url": str(entry.get("url") or ""),
                "path": str(entry.get("path") or ""),
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
                item = grouped.setdefault(pid, {
                    "paper_id": pid,
                    "title": str(meta.get("title") or "Untitled"),
                    "doi": str(meta.get("doi") or ""),
                    "year": str(meta.get("year") or ""),
                    "journal": str(meta.get("journal") or ""),
                    "url": str(meta.get("url") or ""),
                    "path": str(meta.get("file_path") or ""),
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
    """标记自动库已有向量：此后不再允许切换 embedding 模型。"""
    try:
        from src.core.kb_manager import KBManager
        meta_path = os.path.join(KBManager().WORKSPACE_DIR, kb_id, "meta.json")
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        if not meta.get("auto_index_ready"):
            meta["auto_index_ready"] = True
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
    manifest_papers: List[Dict[str, Any]] = []
    for rec in records:
        pid = _paper_id(rec)
        manifest_papers.append({
            "paper_id": pid,
            "title": rec.get("title"),
            "doi": rec.get("doi"),
            "year": rec.get("year"),
            "journal": rec.get("journal"),
            "url": rec.get("url"),
            "has_fulltext": bool(rec.get("_fulltext")),
            "visibility": "global",
            "origin": "auto_search",
        })
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
    if manifest_papers:
        record_conversation_papers(conversation_id, manifest_papers)
    if records:
        _mark_index_ready(kb_id)
    logger.info("Auto evidence ingest: %d new paper(s) from %d record(s)",
                added, len(records))
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
            # BGE 交叉编码器输出 logits：相关为正、无关为负；概率模型则在 0–1。
            threshold = 0.05 if top <= 1.0 else 0.0
            docs = [d for d in scored if float(d.get("score") or 0) >= threshold]
        else:
            docs = (ranked or candidates)[:MAX_CONTEXT_DOCS]
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
    """返回 [(页码, markdown 文本)] 与 PDF 内嵌元数据；解析失败抛异常。"""
    import fitz  # PyMuPDF
    import pymupdf4llm

    pages: List[Tuple[int, str]] = []
    meta: Dict[str, Any] = {}
    try:
        with fitz.open(path) as doc:
            info = doc.metadata or {}
            meta = {
                "title": str(info.get("title") or "").strip(),
                "authors": str(info.get("author") or "").strip(),
                "journal": str(info.get("subject") or "").strip(),
                "year": "",
            }
            first_text = " ".join(
                doc[i].get_text() for i in range(min(3, doc.page_count)))
    except Exception:
        first_text = ""

    try:
        page_data = pymupdf4llm.to_markdown(path, page_chunks=True)
    except Exception:
        page_data = []
    for chunk in page_data or []:
        text = str(chunk.get("text") or "").strip()
        if not text:
            continue
        page = int((chunk.get("metadata") or {}).get("page", 0)) + 1
        pages.append((page, text))

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
        digest = hashlib.sha256()
        with open(source, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        file_hash = digest.hexdigest()
        meta = dict(meta or {})
        pages, pdf_meta = _extract_pdf(source)
        if not pages:
            raise RuntimeError("PDF 没有可提取的文本（可能是扫描件，需 OCR）")
        for key in ("title", "authors", "journal", "year", "doi"):
            if not meta.get(key) and pdf_meta.get(key):
                meta[key] = pdf_meta[key]
        doi = str(meta.get("doi") or "")
        pid = _paper_id({"doi": doi, "title": meta.get("title") or file_hash})

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
                with open(source, "rb") as src, open(tmp, "wb") as dst:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        dst.write(chunk)
                os.replace(tmp, stored)
                try:
                    os.chmod(stored, 0o600)
                except OSError:
                    pass

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
                    main_base_url: str = "", declared_window: Any = None
                    ) -> Tuple[str, Dict[int, Dict[str, Any]]]:
    """无 embedding 模型时的降级路径：直接把检索到的摘要放进上下文。

    这保证"自动文献"不会因为本地模型未下载就完全失效；引用仍然是论文级
    （标题/DOI/逐字摘要），只是没有向量库的跨轮缓存。
    """
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
        title = str(rec.get("title") or "")
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
    try:
        records = search_for_turn(query)
        current_ids = [_paper_id(rec) for rec in records
                       if isinstance(rec, dict)] if records else []
        if records:
            model_ready = False
            try:
                kb_id = ensure_auto_kb()
                if kb_id:
                    _ingest_records(records, conversation_id, kb_id)
                    model_ready = True
            except Exception as e:
                # 典型场景：BGE-M3 等本地 embedding 未下载。不要让自动文献
                # 整个失效，退化为直接使用检索到的摘要。
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
            # 向量库可能因重排阈值只留下 3–4 条；本轮 API 检索到的摘要通常
            # 更多且与查询直接相关。证据太少时用直接摘要补足，避免为了“纯净”
            # 把可用信息砍到无法回答。
            if records and len(sources_map) < 6:
                direct_ctx, direct_sources = _direct_context(
                    records, main_model_name, main_base_url, declared_window)
                if len(direct_sources) > len(sources_map):
                    logger.info("Auto evidence: vector hits %d < 6; "
                                "using %d direct abstracts.",
                                len(sources_map), len(direct_sources))
                    context_str, sources_map = direct_ctx, direct_sources
            if not context_str:
                context_str, sources_map = _direct_context(
                    records, main_model_name, main_base_url, declared_window)
        else:
            try:
                context_str, sources_map = _retrieve(
                    query, conversation_id, main_model_name,
                    main_base_url, declared_window)
            except Exception as e:
                logger.debug("Auto evidence retrieval skipped: %s", e)
        if sources_map:
            # 面板/关联收窄到本轮真正进入上下文的论文，避免历史无关文献长期堆着。
            keep_ids = [str(m.get("paper_id") or "")
                        for m in sources_map.values() if m.get("paper_id")]
            if keep_ids:
                prune_conversation_global_evidence(conversation_id, keep_ids)
        payload = {"context_str": context_str,
                   "sources_map": {str(k): v for k, v in sources_map.items()}}
    except Exception as e:
        logger.exception("Auto evidence worker failed: %s", e)
        payload = {"context_str": "", "sources_map": {}, "error": str(e)}
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
]
