"""Literature tools: unified search, citation graph, OA PDF and preprints."""
import json
import os
import re
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from typing import Literal

from Bio import Entrez

from src.core.academic.base import (
    logger, mcp_request, simple_retry, global_rate_limiter,
    ncbi_email, ncbi_api_key, openalex_api_key, s2_api_key,
    is_ncbi_enabled,
)
from src.core.oa import OAFetcher
from src.task.s2_task import s2_request, is_s2_enabled

__all__ = [
    "search_academic_literature", "traverse_citation_graph",
    "fetch_open_access_pdf", "search_preprints", "fetch_paper_fulltext",
    "fulltext_text",
]

#: OA 全文的最小可用长度：低于此值说明只拿到摘要页/封面，按"没有全文"处理。
_MIN_FULLTEXT_CHARS = 500
#: 单篇全文下载上限（12 MB）：更大的"PDF"基本是出版社错误页或整刊合订本。
_FULLTEXT_MAX_BYTES = 12 * 1024 * 1024
#: 交给模型的全文字符上限：一篇正文约 4-6 万字符，超出部分截断并显式标记。
_FULLTEXT_CHARS = 60000
#: 检索结果里自动附全文的条数与每条字符上限：保证"有全文就用全文"，又不至于
#: 一次工具调用把上下文挤爆（30 条记录 × 全文 ≈ 上百万字符，必须设上限）。
_FULLTEXT_ATTACH_LIMIT = 2
_FULLTEXT_ATTACH_CHARS = 20000
#: 检索时扫描的候选条数：其中可能只有少数进了 PMC，所以窗口要大于附带条数。
_FULLTEXT_SCAN_LIMIT = 8
#: PMC 没补满时，额外用 OA 解析器补全的条数上限（每条最多试 _FULLTEXT_OA_URLS 个直链）。
#: 设为 0 即退回"检索阶段只走 Europe PMC"的旧行为——非 PMC 期刊（MDPI / Frontiers /
#: Elsevier OA / Springer OA …）的正文就只能等模型显式调用 fetch_paper_fulltext。
_FULLTEXT_OA_FALLBACK = 1
#: 检索阶段每条记录最多试几个候选直链：这里要给检索让路，不做穷举。
_FULLTEXT_OA_URLS = 2
#: 显式取全文时每条记录最多试几个候选直链（含出版社落地页直链，可放宽）。
_FULLTEXT_PDF_CANDIDATES = 4



def _normalize_doi(doi):
    """归一化 DOI：剥离 URL 前缀、去除空白，返回小写标准化 DOI（保留原样便于展示）。"""
    if not doi:
        return ""
    raw = str(doi).strip()
    raw = re.sub(r'^(https?://(dx\.)?doi\.org/|http://)', '', raw, flags=re.IGNORECASE)
    return raw


def _get_json(url, source, timeout=15, retries=1):
    """请求 JSON 接口并返回解析后的对象；瞬时故障短退避重试一次。

    旧实现在调用处直接 ``res.json()``：OpenAlex / Crossref 经代理时偶发返回**空
    body**，于是抛 ``JSONDecodeError: Expecting value: line 1 column 1 (char 0)``，
    被上层记成 "{db} search failed"，整个来源的结果被丢弃——而日志里连状态码、响应
    体积、内容类型都没有，事后无从判断是限流、代理故障还是真的没结果。这里统一：

    1. 检查状态码、响应体长度与 Content-Type；
    2. 空体 / 429 / 5xx 视为瞬时故障，退避 1s 重试一次（实测能挽回多数网络抖动）；
    3. 仍失败时抛出带 status / content-type / body 前缀的异常，便于定位。
    """
    last_err = None
    for attempt in range(retries + 1):
        res = mcp_request("GET", url, timeout=timeout)
        status = getattr(res, "status_code", 0)
        body = res.content or b""
        ctype = (res.headers.get("content-type") or "").lower() if res.headers else ""
        if status in (429, 500, 502, 503, 504) or not body:
            last_err = RuntimeError(
                f"{source}: HTTP {status}, {len(body)} bytes, content-type={ctype or 'n/a'}"
                + (f", body[:80]={body[:80]!r}" if body else " (empty body)"))
        else:
            try:
                return res.json()
            except ValueError as e:
                last_err = RuntimeError(
                    f"{source}: non-JSON response (HTTP {status}, "
                    f"content-type={ctype or 'n/a'}, {len(body)} bytes, "
                    f"body[:80]={body[:80]!r}): {e}")
        res.close()
        if attempt < retries:
            logger.warning(f"{last_err} — retrying in {attempt + 1}s.")
            time.sleep(1.0 * (attempt + 1))
    raise last_err


def _norm_title(title):
    """归一化标题用于去重：小写、去标点、压缩空白。"""
    if not title:
        return ""
    t = re.sub(r'[^a-z0-9 ]', ' ', str(title).lower())
    return re.sub(r'\s+', ' ', t).strip()


def _clean_abstract(raw):
    """外部摘要 -> 逐字纯文本（唯一入口）。

    Crossref 返回 JATS 标记（``<jats:p>`` 等），OpenAlex 由倒排索引还原，
    S2 返回纯文本；三种形态都必须收敛成"可逐字展示的原文"，否则引用卡上的
    "Cited passage" 会带上标签残渣。

    无摘要时返回**空串**——绝不写 ``"No abstract"`` 这类占位串：占位串会被
    引用登记链路当成原文（历史故障：引用卡的 Cited passage 显示成了
    ``Fetch via fetch_pubmed_abstract.``）。空串的语义是"该来源没有摘要原文"，
    由 UI 明确提示"未记录支撑原文"。

    摘要**不截断**：它是模型判断相关性、核对数值与结论的唯一依据，截断等于让它
    凭猜测挑文章。上下文体积由 :func:`token_estimator.derive_context_budgets`
    与工具结果上限统一治理，不在这一层做局部裁剪。
    """
    text = re.sub(r"<[^>]+>", " ", str(raw or ""))
    return re.sub(r"\s+", " ", text).strip()


def _has_abstract(rec):
    """记录是否带可展示的摘要原文（判空，不再判定占位串字面量）。"""
    return bool(str((rec or {}).get("abstract") or "").strip())


def _merge_records(records):
    """多来源聚合去重。

    - 同一篇论文可能在一个来源里有 DOI、另一个来源里只有标题；用 DOI 索引 +
      标题索引双路匹配，后到的 DOI 会补进已有记录，不再出现两条重复。
    - 合并来源列表、取最高引用数与最完整摘要，并把缺失的作者/年份/期刊/URL
      逐项补全。
    - 保持首次出现顺序（检索源的 relevance 顺序）；不再按引用数重排，避免
      老综述把真正相关的新论文压到后面。
    - 每个记录计算 ``confidence`` 可信度评分，供上层参考。
    """
    merged = {}
    order = []
    doi_index = {}
    title_index = {}

    def _fill(existing, rec):
        for key in ("doi", "url", "journal", "year", "pmid", "type"):
            if not existing.get(key) and rec.get(key):
                existing[key] = rec[key]
        if not existing.get("authors") and rec.get("authors"):
            existing["authors"] = rec["authors"]
        if not _has_abstract(existing) and _has_abstract(rec):
            existing["abstract"] = rec["abstract"]
        existing["citation_count"] = max(
            existing.get("citation_count", 0) or 0, rec.get("citation_count", 0) or 0)
        if rec.get("is_retracted"):
            existing["is_retracted"] = True
        srcs = existing.setdefault("source_dbs", [])
        src = rec.get("source_db")
        if src and src not in srcs:
            srcs.append(src)

    for rec in records:
        if not isinstance(rec, dict) or not rec.get("title"):
            continue
        doi = _normalize_doi(rec.get("doi"))
        ntitle = _norm_title(rec.get("title"))
        key = (doi_index.get(doi) if doi else None) or title_index.get(ntitle)

        if key is None:
            key = f"doi:{doi}" if doi else f"title:{ntitle}"
            order.append(key)
            rec.setdefault("source_dbs", [rec["source_db"]] if rec.get("source_db") else [])
            rec.setdefault("journal", "")
            rec.setdefault("pmid", "")
            merged[key] = rec
        else:
            _fill(merged[key], rec)

        if doi:
            doi_index[doi] = key
        title_index[ntitle] = key
        # 有 DOI 的记录后来补进无 DOI 的记录时，已有 DOI 也应能被后续记录命中
        existing_doi = _normalize_doi(merged[key].get("doi"))
        if existing_doi:
            doi_index[existing_doi] = key

    ranked = []
    for key in order:
        rec = merged[key]
        score = 0.0
        n_sources = len(rec.get("source_dbs") or [])
        score += 1.0 * min(n_sources, 3)                      # 多来源覆盖
        score += 1.0 if rec.get("doi") else 0.0                # 有 DOI
        score += 1.0 if _has_abstract(rec) else 0.0             # 有真实摘要原文
        score += 0.5 if rec.get("journal") else 0.0            # 有期刊名
        score += 0.2 * min(float(rec.get("citation_count", 0) or 0) / 100.0, 2.0)
        rec["confidence"] = round(score, 2)
        ranked.append(rec)

    return ranked


#: EFetch 单次请求的 PMID 上限（NCBI 允许更多，这里按检索页大小批量取，够用且稳）。
_PUBMED_EFETCH_MAX_IDS = 200


def _pubmed_abstract_text(record):
    """从 EFetch 记录里取出逐字摘要（分段摘要用 Label 前缀拼接，保持原顺序）。"""
    citation = record.get("MedlineCitation") if isinstance(record, dict) else None
    article = citation.get("Article") if isinstance(citation, dict) else None
    abstract = article.get("Abstract") if isinstance(article, dict) else None
    if not isinstance(abstract, dict):
        return ""
    raw = abstract.get("AbstractText")
    chunks = raw if isinstance(raw, list) else ([raw] if raw is not None else [])
    parts = []
    for chunk in chunks:
        text = str(chunk).strip()
        if not text:
            continue
        label = str((getattr(chunk, "attributes", None) or {}).get("Label") or "").strip()
        parts.append(f"{label}: {text}" if label else text)
    return " ".join(parts)


def _iter_pubmed_records(records) -> list:
    """把 EFetch 的解析结果展平成"一条记录一个元素"的列表。

    实测（Biopython，``rettype="abstract"``）：XML 根节点 ``PubmedArticleSet`` 会被解析成
    一个字典，键是 ``PubmedArticle`` / ``PubmedBookArticle``，值为记录列表——直接按列表
    语义迭代只会拿到这个字典本身，``MedlineCitation`` 取不到，摘要全部丢失
    （修复前实测 0/30）。这里统一展平，并兼容单篇返回与平铺列表返回。
    """
    if isinstance(records, dict):
        for key in ("PubmedArticle", "PubmedBookArticle"):
            value = records.get(key)
            if value is None:
                continue
            return list(value) if isinstance(value, list) else [value]
        logger.debug(f"PubMed EFetch payload lacks PubmedArticle key: {list(records.keys())[:6]}")
        return [records]
    return list(records)


def _fetch_pubmed_abstracts(pmids, ncbi_rps):
    """用 EFetch 批量取 PubMed 摘要原文，返回 ``{pmid: abstract}``（唯一取原文入口）。

    为什么必须有这一步：esummary（检索列表）**不返回摘要**。旧实现往 ``abstract``
    字段塞占位串 ``"Fetch via fetch_pubmed_abstract."`` 提示模型再去取——该占位串
    随后被引用登记链路当作"支撑原文"注册，引用卡上就出现了一句不是原文的
    "Cited passage"。改为检索阶段直接用 EFetch 取回逐字摘要，占位串不存在了，
    引用卡展示的必然来自出版社摘要本身。

    容错：网络 / 解析 / 单篇无摘要都退化为"该 PMID 无摘要"，绝不影响检索结果本身；
    调用方据缺键判定该来源没有可展示的原文。
    """
    ids = [str(p).strip() for p in pmids if str(p).strip()]
    if not ids:
        return {}
    abstracts = {}
    for start in range(0, len(ids), _PUBMED_EFETCH_MAX_IDS):
        batch = ids[start:start + _PUBMED_EFETCH_MAX_IDS]
        global_rate_limiter.acquire("ncbi", rps=ncbi_rps)
        try:
            handle = Entrez.efetch(db="pubmed", id=",".join(batch),
                                   rettype="abstract", retmode="xml")
            try:
                records = Entrez.read(handle, validate=False)
            finally:
                handle.close()
        except Exception as e:
            logger.warning(f"PubMed EFetch failed for {len(batch)} id(s): {e}")
            continue
        for record in _iter_pubmed_records(records):
            if not isinstance(record, dict):
                continue
            text = _pubmed_abstract_text(record)
            citation = record.get("MedlineCitation")
            pmid = str((citation or {}).get("PMID", "") or "").strip() if isinstance(citation, dict) else ""
            if text and pmid:
                abstracts[pmid] = text
    logger.info(f"PubMed EFetch: abstracts retrieved for {len(abstracts)}/{len(ids)} record(s).")
    return abstracts


_ARXIV_NS = {"a": "http://www.w3.org/2005/Atom",
             "arxiv": "http://arxiv.org/schemas/atom"}


_ARXIV_FAILURES = 0
_ARXIV_COOLDOWN_UNTIL = 0.0


def _arxiv_records(query: str, max_results: int = 10):
    """arXiv 官方 API：返回标题/作者/摘要/DOI/abs 链接（预印本）。

    arXiv 要求请求间隔约 3 秒；限速器按 rps=0.33 排队，避免被临时封禁。
    ``export.arxiv.org`` 在部分代理/大陆网络会整体超时；连续失败两次后
    本轮/近期直接跳过，避免每次检索都白等 curl_cffi 20s + requests 8s。
    """
    global _ARXIV_FAILURES, _ARXIV_COOLDOWN_UNTIL
    now = time.time()
    if now < _ARXIV_COOLDOWN_UNTIL:
        raise RuntimeError(
            f"arXiv temporarily skipped for {int(_ARXIV_COOLDOWN_UNTIL - now)}s "
            "after repeated network timeouts.")
    global_rate_limiter.acquire("arxiv", rps=0.33)
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    try:
        # 8s 足够正常响应；代理不通时尽快失败，避免拖慢整个并行检索。
        res = mcp_request("GET", "https://export.arxiv.org/api/query",
                          params=params, timeout=8)
        res.raise_for_status()
    except Exception:
        _ARXIV_FAILURES += 1
        if _ARXIV_FAILURES >= 2:
            _ARXIV_COOLDOWN_UNTIL = time.time() + 600
            logger.warning("arXiv disabled for 10 minutes after repeated timeouts.")
        raise
    _ARXIV_FAILURES = 0
    root = ET.fromstring(res.text)
    records = []
    for entry in root.findall("a:entry", _ARXIV_NS):
        title = " ".join((entry.findtext("a:title", "", _ARXIV_NS) or "").split())
        if not title:
            continue
        published = entry.findtext("a:published", "", _ARXIV_NS) or ""
        authors = [a.findtext("a:name", "", _ARXIV_NS)
                   for a in entry.findall("a:author", _ARXIV_NS)]
        abs_url = entry.findtext("a:id", "", _ARXIV_NS) or ""
        doi = (entry.findtext("arxiv:doi", "", _ARXIV_NS) or "").strip()
        journal = (entry.findtext("arxiv:journal_ref", "", _ARXIV_NS) or "arXiv").strip()
        records.append({
            "title": title,
            "year": published[:4] if published else "Unknown",
            "authors": [a for a in authors if a],
            "citation_count": 0,
            "abstract": _clean_abstract(entry.findtext("a:summary", "", _ARXIV_NS)),
            "doi": doi,
            "url": f"https://doi.org/{doi}" if doi else abs_url,
            "source_db": "arXiv",
            "journal": journal,
            "type": "preprint",
        })
    return records


#: Crossref 与 Europe PMC 都没有硬性 rps 上限，但都明确要求"礼貌速率"。这里取保守
#: 值并统一交给进程级 GlobalRateLimiter：多标签同时检索时它们排的是同一条队，
#: 不会因为开了三个标签就三倍速打过去。
_CROSSREF_RPS = 5.0
_EUROPEPMC_RPS = 3.0


def _preprint_records(query: str, max_results: int = 10):
    """Europe PMC 预印本检索（bioRxiv / medRxiv / 部分 arXiv）。"""
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    params = {"query": f"({query}) AND (SRC:PPR)", "format": "json",
              "resultType": "core", "pageSize": max_results}
    global_rate_limiter.acquire("europepmc", rps=_EUROPEPMC_RPS)
    res = mcp_request("GET", url, params=params, timeout=15)
    res.raise_for_status()
    records = []
    for p in res.json().get("resultList", {}).get("result", []):
        title = " ".join(str(p.get("title") or "").split())
        if not title:
            continue
        doi = (p.get("doi") or "").strip()
        details = p.get("bookOrReportDetails") or {}
        server = (details.get("publisher") if isinstance(details, dict) else "") or "Preprint"
        published = str(p.get("firstPublicationDate") or p.get("pubYear") or "")
        records.append({
            "title": title,
            "year": published[:4] if published else "Unknown",
            "authors": [a.strip() for a in str(p.get("authorString") or "").split(",") if a.strip()],
            "citation_count": 0,
            "abstract": _clean_abstract(p.get("abstractText")),
            "doi": doi,
            "url": f"https://doi.org/{doi}" if doi else (
                f"https://europepmc.org/article/PPR/{p.get('id')}" if p.get("id") else ""),
            "source_db": "Europe PMC Preprint",
            "journal": server,
            "type": "preprint",
        })
    return records


def _cite_count(record) -> int:
    """安全读取引用数：各库给的类型不一致（int / str / None）。"""
    try:
        return int(record.get("citation_count") or 0)
    except (TypeError, ValueError):
        return 0


def _thin_result_set(records, max_results: int) -> bool:
    """命中过少：检索式太具体或太口语，值得换个说法再扫一遍。"""
    return len(records) < max(5, max_results // 3)


def _refine_query(text: str) -> str:
    """把口语化 / 疑问式的检索式改写成关键词式；与原串等价时返回空串（表示无需再搜）。"""
    cleaned = re.sub(
        r"(?:请问|什么是|是什么|如何|为什么|有哪些|怎么样|怎么回事|"
        r"what\s+is|what\s+are|how\s+does|why\s+does|tell\s+me\s+about|"
        r"[？?！!。，,、；;：:])",
        " ", str(text or ""), flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) < 8 or cleaned.lower() == str(text or "").strip().lower():
        return ""
    return cleaned


def _attach_fulltext(records, limit: int = _FULLTEXT_ATTACH_LIMIT,
                     chars: int = _FULLTEXT_ATTACH_CHARS,
                     scan: int = _FULLTEXT_SCAN_LIMIT) -> int:
    """给最值得读的若干条记录补 OA 全文，返回补到全文的条数。

    "有全文就用全文"要花真实网络时间，所以只给最可能被引用的记录补全文：有 DOI、
    正式论文优先，引用数高的优先；其余记录保留摘要。拿不到全文时绝不编造内容。
    """
    pool = [r for r in records if isinstance(r, dict) and r.get("doi")]
    pool.sort(key=lambda r: (-_cite_count(r),
                             str(r.get("type") or "article") != "article"))
    window = pool[:max(0, int(scan))]
    # 检索阶段先用 Europe PMC（一次批量解析 PMCID + 少量 XML 请求，快且无反爬）；
    # 高引论文多数是付费墙经典文献，没有 PMC 副本，所以扫描窗口要比附带条数大，
    # 否则每次都白跑。
    pmcids = europe_pmc_pmcids([r.get("doi") for r in window])
    want = max(0, int(limit))
    filled = 0
    for record in window:
        if filled >= want:
            break
        pmcid = pmcids.get(_normalize_doi(record.get("doi")))
        if not pmcid:
            continue
        text = _europe_pmc_xml_text(pmcid)
        if len(text) < _MIN_FULLTEXT_CHARS:
            continue
        _set_record_fulltext(record, text, chars, "Europe PMC")
        filled += 1
        logger.info(f"Full text attached for {record.get('doi')} via {pmcid} "
                    f"({len(text)} chars)")
    # PMC 没补满时，用 OA 解析器给非 PMC 期刊（MDPI / Frontiers / Elsevier OA /
    # Springer OA …）补一条：只补 1 条、每条最多 2 个候选，检索延迟可控。否则这些
    # 期刊在检索结果里永远只有摘要，用户看到的"有正文"就只剩 PNAS / 牛津系。
    if filled < want and _FULLTEXT_OA_FALLBACK > 0:
        filled += _attach_oa_fulltext(window, pmcids, min(_FULLTEXT_OA_FALLBACK,
                                                          want - filled), chars)
    logger.info(f"Full text attached to {filled} record(s); scanned {len(window)}")
    return filled


def _set_record_fulltext(record, text: str, chars: int, source: str):
    """把正文写进检索记录（字段名与截断标记的唯一写法）。"""
    record["full_text"] = text[:chars]
    record["full_text_source"] = source
    record["full_text_truncated"] = len(text) > chars


def _attach_oa_fulltext(window, pmcids, want: int, chars: int) -> int:
    """给"不在 PMC 里"的高价值记录补 OA PDF 正文；返回补到的条数。

    成本上限写死在常量里（条数 + 每条候选数），因为它跑在检索主路径上：宁可少补
    一条，也不该让一次文献检索多花十几秒。
    """
    if want <= 0:
        return 0
    filled = 0
    for record in window:
        if filled >= want:
            break
        doi = _normalize_doi(record.get("doi"))
        if not doi or pmcids.get(doi):
            continue      # PMC 已有副本：上面那一轮已经处理过（或已尝试过）
        try:
            found = OAFetcher().fetch_best_oa_pdf(doi, ncbi_email,
                                                  ncbi_api_key=ncbi_api_key)
        except Exception as e:
            logger.debug(f"Search-time OA lookup failed for {doi}: {e}")
            continue
        urls = _oa_candidate_urls(found)[:_FULLTEXT_OA_URLS]
        for url in urls:
            text = _pdf_url_to_text(url, label="Search OA PDF")
            if len(text) < _MIN_FULLTEXT_CHARS:
                continue
            _set_record_fulltext(record, text, chars, "Open access PDF")
            filled += 1
            logger.info(f"Search-time OA full text attached for {doi} ({len(text)} chars)")
            break
    return filled


@simple_retry(max_attempts=2, delay=1)
def search_academic_literature(query: str, max_results: int = 30, offset: int = 0,
                               source: Literal["auto", "semantic_scholar", "openalex",
                                               "crossref", "pubmed", "arxiv", "preprints"] = "auto",
                               aggregate: bool = True, min_year: int | None = None,
                               include_preprints: bool = True,
                               include_fulltext: bool = True,
                               ) -> str:
    """Unified literature search with cross-database aggregation.

    - ``source="auto"`` queries ALL enabled databases (OpenAlex, Crossref,
      PubMed, Semantic Scholar) in parallel and aggregates + dedupes results,
      improving coverage breadth vs. the legacy single-source short-circuit.
      Coverage is venue-agnostic: PNAS / Nature / Science / Cell class journals
      are indexed by OpenAlex, Crossref and PubMed alike, so do NOT narrow
      ``source`` unless the user explicitly asks for one database.
    - ``max_results`` defaults to 30 (breadth first). If the aggregated pool
      comes back thin, one extra round runs with a keyword-refined rewrite of
      the query.
    - ``include_fulltext`` attaches open-access full text (Europe PMC XML, then
      OA PDF) to the most citable hits so answers can quote the paper body
      instead of the abstract. Records with no OA copy keep the abstract only.
    - ``aggregate=False`` keeps the legacy per-source behavior (first non-empty
      source wins) for backward compatibility.
    - ``min_year`` optionally filters out papers published before a given year.
    """
    logger.info(f"Task: Unified Literature Search | Query: '{query}' | Offset: {offset} | Source: {source}")

    if not is_ncbi_enabled():
        logger.error(
            "NCBI has been disabled due to the lack of a valid email address AND API Key; other tools are still functioning normally.")

    def _parse_openalex():
        page = (offset // max_results) + 1
        url = f"https://api.openalex.org/works?search={urllib.parse.quote(query)}&per-page={max_results}&page={page}"

        openalex_rps = 9 if openalex_api_key else 2
        global_rate_limiter.acquire("openalex", rps=openalex_rps)

        if openalex_api_key:
            url += f"&api_key={openalex_api_key}"

        parsed = []
        payload = _get_json(url, "OpenAlex", timeout=15)
        # OpenAlex 正常返回 dict；空 body / 代理错误页可能给出 null，
        # 旧写法直接 .get() 会抛 AttributeError 并丢掉整库结果。
        if not isinstance(payload, dict):
            logger.warning(
                f"Unexpected OpenAlex payload ({type(payload).__name__}); treating as empty result set.")
            return parsed

        for p in payload.get("results", []):
            if not isinstance(p, dict): continue
            abs_idx = p.get("abstract_inverted_index")
            abstract_text = ""
            if isinstance(abs_idx, dict):
                words = [(pos, w) for w, positions in abs_idx.items() if isinstance(positions, list) for pos in
                         positions]
                words.sort()
                abstract_text = _clean_abstract(" ".join([w for pos_idx, w in words]))

            authors_raw = p.get("authorships") or []
            if not isinstance(authors_raw, list): authors_raw = []
            authors = [a.get("author", {}).get("display_name", "") for a in authors_raw if
                       isinstance(a, dict) and isinstance(a.get("author"), dict)]

            # primary_location.source 允许为 null（无宿主期刊的预印本/数据集等）。
            # 旧写法 (p["primary_location"] or {}).get("source", {}) 在 source 为 null 时
            # 会拿到 None 再 .get()，抛 "'NoneType' object has no attribute 'get'"
            # —— 实测该查询命中的唯一记录正是 source=null，整个 OpenAlex 来源因此被丢弃。
            primary = p.get("primary_location")
            primary = primary if isinstance(primary, dict) else {}
            source = primary.get("source")
            source = source if isinstance(source, dict) else {}

            clean_doi = (p.get("doi") or "").replace("https://doi.org/", "")
            best = p.get("best_oa_location")
            best = best if isinstance(best, dict) else {}
            # url 必须是论文落地页/DOI，不能是 OpenAlex 的 API id（旧实现会把它
            # 直接塞进引用卡的"打开链接"）。
            landing = (primary.get("landing_page_url")
                       or best.get("landing_page_url") or "")
            url = landing or (f"https://doi.org/{clean_doi}" if clean_doi else p.get("id", ""))

            parsed.append({"title": p.get("title", ""), "year": p.get("publication_year", "Unknown"),
                           "authors": authors,
                           "citation_count": p.get("cited_by_count", 0), "abstract": abstract_text,
                           "doi": clean_doi,
                           "url": url, "source_db": "OpenAlex",
                           "journal": source.get("display_name") or "",
                           "is_retracted": bool(p.get("is_retracted")),
                           "type": "preprint" if not source.get("display_name") else "article"})
        return parsed

    def _parse_crossref():
        url = f"https://api.crossref.org/works?query={urllib.parse.quote(query)}&mailto={ncbi_email}&rows={max_results}&offset={offset}"
        parsed = []
        global_rate_limiter.acquire("crossref", rps=_CROSSREF_RPS)
        payload = _get_json(url, "Crossref", timeout=15)
        msg_dict = payload.get("message") if isinstance(payload, dict) else None
        items = msg_dict.get("items", []) if isinstance(msg_dict, dict) else []
        for p in items:
            if not isinstance(p, dict): continue
            authors_raw = p.get("author") or []
            if not isinstance(authors_raw, list): authors_raw = []
            authors = [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in authors_raw if
                       isinstance(a, dict)]

            title_raw = p.get("title")
            title = title_raw[0] if isinstance(title_raw, list) and len(title_raw) > 0 else (
                title_raw if isinstance(title_raw, str) else "")

            created = p.get("created")
            year = "Unknown"
            if isinstance(created, dict):
                date_parts = created.get("date-parts")
                if isinstance(date_parts, list) and len(date_parts) > 0 and isinstance(date_parts[0],
                                                                                       list) and len(
                        date_parts[0]) > 0:
                    year = str(date_parts[0][0])

            journal_raw = p.get("container-title")
            journal = journal_raw[0] if isinstance(journal_raw, list) and journal_raw else (
                journal_raw if isinstance(journal_raw, str) else "")

            parsed.append({"title": title,
                           "year": year,
                           "authors": authors, "citation_count": p.get("is-referenced-by-count", 0),
                           "abstract": _clean_abstract(p.get("abstract")),
                           "doi": p.get("DOI", ""), "url": p.get("URL", ""),
                           "source_db": "Crossref", "journal": journal})
        return parsed

    def _parse_pubmed():
        ncbi_rps = 9 if ncbi_api_key else 4
        global_rate_limiter.acquire("ncbi", rps=ncbi_rps)

        search_handle = Entrez.esearch(db="pubmed", term=query, retstart=offset, retmax=max_results)
        search_res = Entrez.read(search_handle, validate=False)
        ids = search_res.get("IdList", []) if isinstance(search_res, dict) else []
        search_handle.close()
        if not ids:
            return []
        summary_handle = Entrez.esummary(db="pubmed", id=",".join(ids))
        doc_list = Entrez.read(summary_handle, validate=False)
        summary_handle.close()

        if isinstance(doc_list, dict):
            ds_set = doc_list.get("DocumentSummarySet")
            doc_list = ds_set.get("DocumentSummary", []) if isinstance(ds_set, dict) else []
        if not isinstance(doc_list, list): doc_list = [doc_list]

        # esummary 不带摘要，必须再走一次 EFetch 取逐字原文（见 _fetch_pubmed_abstracts）。
        abstracts = _fetch_pubmed_abstracts(
            [d.get("Id", "") for d in doc_list if isinstance(d, dict)], ncbi_rps)

        parsed = []
        for d in doc_list:
            if not isinstance(d, dict): continue

            authors_raw = d.get("AuthorList", [])
            if isinstance(authors_raw, dict): authors_raw = authors_raw.get("Author", [])
            if not isinstance(authors_raw, list): authors_raw = []
            authors = [a.get("Name", str(a)) if isinstance(a, dict) else str(a) for a in authors_raw]

            article_ids = d.get("ArticleIds", [])
            if not isinstance(article_ids, list): article_ids = []
            doi = next(
                (a.get("Value", "") for a in article_ids if isinstance(a, dict) and a.get("IdType") == "doi"),
                "")

            pmid = str(d.get("Id", "") or "").strip()
            parsed.append({"title": d.get("Title", ""), "year": d.get("PubDate", "")[:4],
                           "authors": authors,
                           # 真实摘要；取不到就是空串（没有原文，不编造）
                           "abstract": abstracts.get(pmid, ""),
                           "pmid": pmid,
                           "doi": doi,
                           "journal": d.get("FullJournalName", ""),
                           "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "source_db": "PubMed"})
        return parsed

    def _parse_s2():
        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {"query": query, "limit": max_results, "offset": offset,
                  "fields": "title,authors,year,abstract,citationCount,isOpenAccess,url,externalIds,venue"}

        res = s2_request("GET", url, params=params)
        if res is None:
            logger.warning("S2 request returned None (likely API key missing or rate limited).")
            raise ValueError("S2 request failed silently")
        res.raise_for_status()
        response_text = res.text
        if not response_text or len(response_text.strip()) == 0:
            logger.warning("S2 response is empty")
            raise ValueError("S2 response is empty")
        json_data = res.json()
        if not isinstance(json_data, dict):
            raise ValueError("S2 response is not a dictionary")
        parsed = []
        for p in json_data.get("data", []):
            if not isinstance(p, dict): continue
            authors_raw = p.get("authors") or []
            if not isinstance(authors_raw, list): authors_raw = []
            ext_ids = p.get("externalIds")
            parsed.append({"title": p.get("title", ""), "year": p.get("year", "Unknown"),
                           "authors": [a.get("name", "") for a in authors_raw if isinstance(a, dict)],
                           "citation_count": p.get("citationCount", 0),
                           "abstract": _clean_abstract(p.get("abstract")),
                           "doi": ext_ids.get("DOI", "") if isinstance(ext_ids, dict) else "",
                           "url": p.get("url", ""),
                           "journal": p.get("venue", "") or "",
                           "source_db": "Semantic Scholar"})
        return parsed

    # 决定需要查询哪些数据库
    wanted = []
    if source in ["auto", "openalex"]:
        wanted.append("openalex")
    if source in ["auto", "crossref"]:
        wanted.append("crossref")
    if source in ["auto", "pubmed"] and is_ncbi_enabled():
        wanted.append("pubmed")
    if source in ["auto", "semantic_scholar"]:
        wanted.append("semantic_scholar")
    if source == "arxiv":
        wanted.append("arxiv")
    if source == "preprints":
        wanted.append("preprints")
    if source == "auto" and include_preprints:
        wanted.extend(["arxiv", "preprints"])

    all_records = []
    source_stats = {}

    # ---- 数据库并行查询：OA/Crossref/PubMed/S2 各库延迟独立（1-3s），串行时
    # 单个工具调用的墙钟时间是各库之和（~7s），并行后降至最慢库延迟（~2-3s）。
    # 一次会话常含 10+ 次检索调用，累计节省 30-60s 墙钟时间。
    # 线程安全：各 parser 均经 mcp_request/s2_request 每调用新建 HTTP session，
    # 限流器内部持锁，Entrez 基于 urllib，可安全并发；失败隔离沿用逐库 try/except。
    def _query_db(db):
        """查询单库，返回 (db, recs)。S2 配置门控保持原语义。"""
        if db == "openalex":
            return db, _parse_openalex()
        if db == "crossref":
            return db, _parse_crossref()
        if db == "pubmed":
            return db, _parse_pubmed()
        if db == "semantic_scholar":
            if not s2_api_key:
                logger.debug("Semantic Scholar skipped: no API key configured.")
                return db, []
            if not is_s2_enabled():
                return db, []
            return db, _parse_s2()
        if db == "arxiv":
            return db, _arxiv_records(query, max_results=max(5, max_results // 2))
        if db == "preprints":
            return db, _preprint_records(query, max_results=max(5, max_results // 2))
        return db, []

    # 显式指定 semantic_scholar 但未配 key：保持原提前返回语义（错误直达调用方）
    if source == "semantic_scholar" and not s2_api_key:
        logger.warning("Semantic Scholar is disabled due to missing API Key.")
        return json.dumps(
            {"status": "error",
             "message": "Semantic Scholar API is disabled. Please configure an API Key."})

    db_results = {}
    if wanted:
        with ThreadPoolExecutor(max_workers=len(wanted),
                                thread_name_prefix="litsearch") as pool:
            futures = {pool.submit(_query_db, db): db for db in wanted}
            for fut in as_completed(futures):
                db = futures[fut]
                try:
                    _, recs = fut.result()
                    db_results[db] = recs
                except Exception as e:
                    logger.warning(f"{db} search failed: {e}")
                    db_results[db] = []
    # 按 wanted 原序合并：聚合输出顺序与串行版保持一致（确定性）
    for db in wanted:
        recs = db_results.get(db, [])
        source_stats[db] = len(recs)
        all_records.extend(recs)

    # 按年份过滤（可选）
    if min_year is not None:
        def _keep(r):
            try:
                return int(r.get("year", 0)) >= min_year
            except (TypeError, ValueError):
                return False
        filtered = [r for r in all_records if _keep(r)]
        dropped = len(all_records) - len(filtered)
        if dropped:
            logger.info(f"min_year={min_year}: dropped {dropped} older records")
        all_records = filtered

    # 范围兜底：命中过少说明检索式太具体或太口语，用关键词式改写再扫一遍。
    # 只补一轮，且改写与原串相同时不再重试，避免工具调用被无限放大。
    if source == "auto" and wanted and _thin_result_set(all_records, max_results):
        retry_query = _refine_query(query)
        if retry_query:
            logger.info(f"Thin result set ({len(all_records)} records); retrying with "
                        f"refined query: '{retry_query}'")
            try:
                extra_raw = search_academic_literature(
                    retry_query, max_results=max_results, source=source,
                    aggregate=False, min_year=min_year,
                    include_preprints=include_preprints, include_fulltext=False)
                extra = json.loads(extra_raw).get("results") or []
            except Exception as e:
                logger.warning(f"Refined-query retry failed: {e}")
                extra = []
            if extra:
                source_stats["refined_retry"] = len(extra)
                all_records.extend(extra)

    # 聚合（auto 且 aggregate=True）或多源短路径
    if source == "auto" and aggregate:
        results = _merge_records(all_records)
        source_name = "aggregated"
    elif not aggregate:
        # 保持旧语义：不聚合时直接给出原始记录
        results = all_records
        source_name = ""
    else:
        # 指定单库检索（source=openalex/crossref/pubmed/semantic_scholar 且 aggregate 默认 True）
        # 此前会落到最后一行恒返回空列表：source_stats 里明明有记录，results 却是 []，
        # 调用方只会看到 "No results found from any source"（实测 openalex 取到 1 条却丢失）。
        # 这里按与其他分支一致的契约返回：合并去重 + 补 confidence/source_dbs。
        results = _merge_records(all_records)
        source_name = wanted[0] if len(wanted) == 1 else "aggregated"

    # 有全文就用全文：只有拿到可核验的 OA 正文才会写入 full_text 字段
    if results and include_fulltext:
        _attach_fulltext(results)

    payload = {"status": "success", "results": results, "source_stats": source_stats}
    if source_name:
        payload["source"] = source_name
    if not results:
        payload["message"] = "No results found from any source"
    return json.dumps(payload, ensure_ascii=False)


@simple_retry(max_attempts=2, delay=1)
def traverse_citation_graph(doi: str, direction: Literal["references", "citations"] = "references",
                            max_results: int = 10,
                            source: Literal["auto", "openalex", "semantic_scholar"] = "auto") -> str:
    logger.info(f"Task: Citation Graph | DOI: {doi} | Direction: {direction} | Source: {source}")

    if direction not in ["references", "citations"]: return json.dumps(
        {"status": "error", "message": "direction must be 'references' or 'citations'"})

    clean_doi = re.sub(r'^(https?://(dx\.)?doi\.org/)?', '', doi.strip())
    last_error = None

    if source in ["auto", "openalex"]:
        openalex_rps = 9 if openalex_api_key else 2
        global_rate_limiter.acquire("openalex", rps=openalex_rps)

        try:
            if direction == "references":
                url = f"https://api.openalex.org/works/https://doi.org/{clean_doi}"
                if openalex_api_key:
                    url += f"?api_key={openalex_api_key}"

                work_res = mcp_request("GET", url, timeout=15)
                if work_res.status_code == 404:
                    return json.dumps({"status": "success", "results": [], "message": f"DOI '{clean_doi}' not found."})
                work_res.raise_for_status()

                # referenced_works 可能为 null / 非列表，json() 也可能给出 null
                work_payload = work_res.json()
                ref_ids = work_payload.get("referenced_works") if isinstance(work_payload, dict) else None
                ref_ids = ref_ids[:max_results] if isinstance(ref_ids, list) else []

                if not ref_ids:
                    return json.dumps({"status": "success", "results": []})

                filter_str = "|".join([r.split("/")[-1] for r in ref_ids])
                safe_filter = urllib.parse.quote(f"ids.openalex:{filter_str}")
                url = f"https://api.openalex.org/works?filter={safe_filter}"
                if openalex_api_key:
                    url += f"&api_key={openalex_api_key}"
            else:
                safe_filter = urllib.parse.quote(f"cites:https://doi.org/{clean_doi}")
                url = f"https://api.openalex.org/works?filter={safe_filter}&per-page={max_results}"
                if openalex_api_key:
                    url += f"&api_key={openalex_api_key}"

            res = mcp_request("GET", url, timeout=15)
            res.raise_for_status()
            parsed = []
            payload = res.json()
            if not isinstance(payload, dict):
                logger.warning(
                    f"Unexpected OpenAlex payload ({type(payload).__name__}); treating as empty result set.")
                payload = {}
            for p in payload.get("results") or []:
                if not isinstance(p, dict): continue

                abs_idx = p.get("abstract_inverted_index")
                abstract_text = ""
                if isinstance(abs_idx, dict):
                    words = [(pos, w) for w, positions in abs_idx.items() if isinstance(positions, list) for pos in
                             positions]
                    words.sort()
                    abstract_text = _clean_abstract(" ".join([w for pos_idx, w in words]))

                authors_raw = p.get("authorships") or []
                if not isinstance(authors_raw, list): authors_raw = []
                authors = [a.get("author", {}).get("display_name", "") for a in authors_raw if
                           isinstance(a, dict) and isinstance(a.get("author"), dict)]

                clean_doi = (p.get("doi") or "").replace("https://doi.org/", "")
                primary = p.get("primary_location")
                primary = primary if isinstance(primary, dict) else {}
                src = primary.get("source")
                src = src if isinstance(src, dict) else {}
                landing = primary.get("landing_page_url") or ""
                parsed.append({"title": p.get("title", ""), "year": p.get("publication_year", "Unknown"),
                               "authors": authors,
                               "citation_count": p.get("cited_by_count", 0),
                               "abstract": abstract_text,
                               "doi": clean_doi,
                               "url": landing or (f"https://doi.org/{clean_doi}" if clean_doi else p.get("id", "")),
                               "journal": src.get("display_name") or "",
                               "is_retracted": bool(p.get("is_retracted")),
                               "source_db": "OpenAlex citation graph"})

            return json.dumps({"status": "success", "source": "OpenAlex", "direction": direction, "results": parsed})
        except Exception as e:
            logger.warning(f"OpenAlex citation graph failed: {e}. Falling back to S2 if configured...")
            last_error = e

    if source in ["auto", "semantic_scholar"]:
        if not s2_api_key:
            logger.warning("Semantic Scholar citation graph is disabled due to missing API Key.")
            if source == "semantic_scholar":
                return json.dumps(
                    {"status": "error", "message": "Semantic Scholar API is disabled. Please configure an API Key."})
        elif is_s2_enabled():
            try:
                url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{clean_doi}/{direction}?fields=title,authors,year,abstract,citationCount,externalIds,url&limit={max_results}"

                res = s2_request("GET", url, timeout=15)

                if res is None:
                    raise ValueError("S2 request returned None")
                res.raise_for_status()

                response_text = res.text
                if not response_text or len(response_text.strip()) == 0:
                    logger.warning("S2 citation graph response is empty")
                    raise ValueError("S2 response is empty")

                json_data = res.json()

                if not isinstance(json_data, dict):
                    logger.warning(f"S2 citation graph response is not a dict: {type(json_data)}")
                    raise ValueError("S2 response is not a dictionary")
                parsed = []
                data_list = json_data.get("data")

                if isinstance(data_list, list):
                    for item in data_list:
                        if not isinstance(item, dict): continue
                        p = item.get("citedPaper") if direction == "references" else item.get("citingPaper")
                        if not isinstance(p, dict) or not p.get("title"): continue

                        authors_raw = p.get("authors") or []
                        if not isinstance(authors_raw, list): authors_raw = []

                        ext_ids = p.get("externalIds")
                        doi_str = ext_ids.get("DOI", "") if isinstance(ext_ids, dict) else ""

                        parsed.append({
                            "title": p.get("title", ""), "year": p.get("year", "Unknown"),
                            "authors": [a.get("name", "") for a in authors_raw if isinstance(a, dict)],
                            "citation_count": p.get("citationCount", 0),
                            "abstract": _clean_abstract(p.get("abstract")),
                            "doi": doi_str,
                            "url": p.get("url", "")
                        })

                return json.dumps(
                    {"status": "success", "source": "Semantic Scholar", "direction": direction, "results": parsed})
            except Exception as e:
                logger.warning(f"S2 citation graph fallback failed: {e}")
                last_error = e


    if last_error:
        return json.dumps({"status": "error", "message": f"Failed to traverse citation graph: {str(last_error)}"})

    return json.dumps({"status": "error", "message": "Unexpected error traversing citation graph."})




def europe_pmc_pmcids(dois, report: dict | None = None) -> dict:
    """批量把 DOI 解析成 PMCID，返回 ``{归一化 DOI: PMCID}``；解析不到的 DOI 不出现。

    一次请求即可解析整批 DOI（Europe PMC 支持 ``OR`` 组合查询）。这很关键：逐条
    查询会把"顺手附全文"拖成几十次往返，检索工具就废了。

    ``report`` 给出时记录请求失败：查不到的 DOI 既可能真没进 PMC，也可能这次查询
    失败，调用方需要能区分（见 :func:`fetch_paper_fulltext`）。
    """
    wanted = {}
    for doi in dois:
        clean = _normalize_doi(doi)
        if clean:
            wanted[clean.lower()] = clean
    if not wanted:
        return {}
    clause = " OR ".join(f'DOI:"{d}"' for d in wanted.values())
    try:
        global_rate_limiter.acquire("europepmc", rps=_EUROPEPMC_RPS)
        res = mcp_request(
            "GET", "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": clause, "format": "json", "resultType": "core",
                    "pageSize": len(wanted)},
            timeout=20)
        if res.status_code != 200:
            if report is not None:
                report["failures"].append(f"Europe PMC search (HTTP {res.status_code})")
            return {}
        found = {}
        for rec in ((res.json().get("resultList") or {}).get("result") or []):
            pmcid = str(rec.get("pmcid") or "")
            doi = _normalize_doi(str(rec.get("doi") or "")).lower()
            if pmcid and doi in wanted:
                found[wanted[doi]] = pmcid
        logger.debug(f"Europe PMC resolved {len(found)}/{len(wanted)} PMCID(s)")
        return found
    except Exception as e:
        if report is not None:
            report["failures"].append(f"Europe PMC search ({e})")
        logger.debug(f"Europe PMC PMCID lookup failed: {e}")
        return {}


def _europe_pmc_xml_text(pmcid: str) -> str:
    """按 PMCID 取全文 XML 的纯文本；失败返回空串。"""
    try:
        global_rate_limiter.acquire("europepmc", rps=_EUROPEPMC_RPS)
        xml_res = mcp_request(
            "GET", f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML",
            timeout=20)
        if xml_res.status_code != 200 or not xml_res.text:
            return ""
        text = unescape(re.sub(r"<[^>]+>", " ", xml_res.text))
        return re.sub(r"\s+", " ", text).strip()
    except Exception as e:
        logger.debug(f"Europe PMC XML fetch failed for {pmcid}: {e}")
        return ""


def europe_pmc_fulltext(doi: str, report: dict | None = None) -> str:
    """Europe PMC 的 OA 全文（XML 纯文本）。

    PMC 收录的 PNAS / Nature 子刊命中率最高，且不经过出版社页面与反爬，是取全文的
    首选路径；拿不到时返回空串（调用方退回摘要）。

    ``report`` 给出时把两种"空"分开记：查不到 PMCID 是"该文未进 PMC"的确定结论，
    PMCID 有了却取不回 XML 属故障——后者不能用来断言该文没有全文。
    """
    clean = _normalize_doi(doi)
    pmcid = europe_pmc_pmcids([clean], report=report).get(clean) if clean else ""
    if not pmcid:
        if report is not None:
            report["checked"].append("Europe PMC (no PMCID: not in PMC)")
        return ""
    text = _europe_pmc_xml_text(pmcid)
    if text:
        logger.info(f"Europe PMC full text: {clean} -> {pmcid} ({len(text)} chars)")
    elif report is not None:
        report["failures"].append(f"Europe PMC full-text XML ({pmcid})")
    return text


def _pdf_url_to_text(url: str, report: dict | None = None, label: str = "OA PDF") -> str:
    """下载一个 PDF 直链并抽出正文；下载或解析失败返回空串，原因记进 ``report``。

    OA 解析器给的链接与预印本的官方直链走同一条下载/解析链路，所以只留这一份实现；
    失败一律记 failures —— 那是故障，不能据此断言该文没有正文。
    """
    tmp = ""
    host = urllib.parse.urlsplit(str(url or "")).netloc
    try:
        from src.core.pdf_text import extract_pdf
        resp = mcp_request("GET", url, timeout=30)
        if resp.status_code != 200:
            if report is not None:
                # 带上主机：403/503 是"某出版社拦了自动下载"还是"整站挂了"，
                # 模型与用户看这一句就能分清（也便于判断是否该转为手工交 PDF）。
                report["failures"].append(
                    f"{label} download ({host or 'host unknown'}: HTTP {resp.status_code})")
            return ""
        content = resp.content or b""
        # PDF 头允许出现在开头 1KB 内：部分出版社/网关会在文件前加 BOM 或空行，
        # 旧实现的严格 startswith 会把这类可读文件判成"不是 PDF"。
        if len(content) > _FULLTEXT_MAX_BYTES or b"%PDF" not in content[:1024]:
            if report is not None:
                report["failures"].append(
                    f"{label} download ({host or 'host unknown'}: response is not a PDF)")
            return ""
        fd, tmp = tempfile.mkstemp(prefix="fulltext_", suffix=".pdf")
        os.close(fd)
        with open(tmp, "wb") as handle:
            handle.write(content)
        pages, _meta = extract_pdf(tmp)
        return "\n\n".join(t for _page, t in pages if t.strip()).strip()
    except Exception as e:
        if report is not None:
            report["failures"].append(f"{label} pipeline ({e})")
        logger.debug(f"{label} fetch failed for {url}: {e}")
        return ""
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _oa_candidate_urls(found: dict):
    """从 OAFetcher 的结果里取候选直链（新契约 ``pdf_urls``，旧契约 ``pdf_url`` 兜底）。"""
    urls = [str(u) for u in (found.get("pdf_urls") or []) if u]
    first = str(found.get("pdf_url") or "")
    if first and first not in urls:
        urls.insert(0, first)
    return urls


def _oa_pdf_fulltext(doi: str, report: dict | None = None) -> str:
    """DOI -> 合法 OA PDF -> 正文文本；没有 OA 副本或全部候选都不可用时返回空串。

    逐个试候选直链而不是只赌第一个：同一篇论文的 OA 副本常分散在出版社站点、机构库
    与 PMC 镜像上，其中任意一个被反爬拦下都不该让整次取全文失败（这正是原先"只有
    PNAS / 牛津系期刊取得到"的原因之一——它们的正文在 Europe PMC 里有 XML，绕开了
    下载环节）。

    ``report`` 给出时汇总 OAFetcher 各来源的结论（谁明确回答过、谁失败了）；下载环节
    的失败由 :func:`_pdf_url_to_text` 一并记入。
    """
    clean = _normalize_doi(doi)
    if not clean:
        return ""
    try:
        found = OAFetcher().fetch_best_oa_pdf(clean, ncbi_email, ncbi_api_key=ncbi_api_key)
        if report is not None:
            report["checked"].extend(found.get("checked") or [])
            report["failures"].extend(found.get("failures") or [])
        urls = _oa_candidate_urls(found)
        if not found.get("is_oa") or not urls:
            return ""
        for index, url in enumerate(urls[:_FULLTEXT_PDF_CANDIDATES], 1):
            text = _pdf_url_to_text(url, report=report, label=f"OA PDF #{index}")
            if len(text) >= _MIN_FULLTEXT_CHARS:
                if index > 1:
                    logger.info(f"OA PDF candidate #{index} worked for {clean} "
                                f"(previous candidate(s) unusable).")
                return text
            logger.debug(f"OA PDF candidate #{index} unusable for {clean}: {url[:120]}")
        # 每个候选的失败已由 _pdf_url_to_text 记进 report，这里不再重复记一条：
        # 有了失败记录，调用方给模型的说法就是"本次没拿到"，而不是"该文没有 OA"。
        return ""
    except Exception as e:
        if report is not None:
            report["failures"].append(f"OA resolver ({e})")
        logger.debug(f"OA PDF full text failed for {clean}: {e}")
        return ""


def _preprint_pdf_url(doi: str) -> tuple:
    """DOI 本身就是预印本时给出官方 PDF 直链，返回 ``(url, 标签)``；否则 ``("", "")``。

    预印本正文始终免费，不必等 OA 解析器判断"有没有免费版本"：期刊版本可能落在付费墙，
    预印本本身却一直可读。bioRxiv / medRxiv 要先问 API 拿最新版本号（``...v3.full.pdf``），
    arXiv 的 id 直接嵌在 DOI 里。

    只认"DOI 即预印本"这一种情况：期刊论文"另有预印本"的反查不做——两者 DOI 不同，按
    标题去配容易配错论文，宁可不给。
    """
    clean = _normalize_doi(doi).lower()
    if clean.startswith("10.1101/"):
        for server, host in (("biorxiv", "www.biorxiv.org"), ("medrxiv", "www.medrxiv.org")):
            try:
                res = mcp_request("GET",
                                  f"https://api.biorxiv.org/details/{server}/{clean}", timeout=15)
                if res.status_code != 200:
                    continue
                collection = (res.json() or {}).get("collection") or []
                versions = [int(c.get("version") or 0) for c in collection]
                if not versions:
                    continue
                latest = max(versions)
                return (f"https://{host}/content/{clean}v{latest}.full.pdf",
                        f"{server} preprint v{latest}")
            except Exception as e:
                logger.debug(f"{server} preprint lookup failed for {clean}: {e}")
        return "", ""
    if clean.startswith("10.48550/arxiv."):
        arxiv_id = clean.split("arxiv.", 1)[1]
        return f"https://arxiv.org/pdf/{arxiv_id}", f"arXiv preprint {arxiv_id}"
    return "", ""


def fulltext_text(doi: str, report: dict | None = None):
    """取一篇论文的合法全文，返回 ``(正文, 来源标签)``；都拿不到时返回 ``("", "")``。

    顺序：Europe PMC XML（最稳） -> 预印本官方 PDF（DOI 即预印本时） -> OA PDF
    （OpenAlex / Unpaywall / PMC / Semantic Scholar 的候选，以及出版社落地页
    ``citation_pdf_url`` 兜底，逐个尝试）。这些都是合法来源；出版社付费墙不在其中——
    需要付费墙正文时由 ``request_fulltext`` 请用户交回自己订阅得到的 PDF。

    拿不到全文**不等于**工具失败：调用方应退回摘要，并如实说明"仅有摘要"。

    ``report``（``{"checked": [...], "failures": [...]}``）给出时逐来源记录：谁明确
    回答过"没有 OA"、谁请求失败或被熔断。调用方据此决定措辞——只有 failures 为空
    时才可以断言该文没有开放获取版本。
    """
    text = europe_pmc_fulltext(doi, report=report)
    if len(text) >= _MIN_FULLTEXT_CHARS:
        return text, "Europe PMC"
    url, label = _preprint_pdf_url(doi)
    if url:
        text = _pdf_url_to_text(url, report=report, label=label)
        if len(text) >= _MIN_FULLTEXT_CHARS:
            logger.info(f"Preprint full text: {doi} -> {label} ({len(text)} chars)")
            return text, f"Preprint ({label})"
    text = _oa_pdf_fulltext(doi, report=report)
    if len(text) >= _MIN_FULLTEXT_CHARS:
        return text, "Open access PDF"
    return "", ""


@simple_retry(max_attempts=2, delay=1)
def fetch_paper_fulltext(doi: str, max_chars: int = _FULLTEXT_CHARS) -> str:
    """按 DOI 取一篇论文的 OA 全文；没有 OA 副本时明确告知"只有摘要可用"。

    取不到时分两种说法回话，因为模型只能照工具给的话解释原因：全部来源都正常回答过，
    才可以说"该文没有开放获取版本"；只要有来源请求失败或被熔断，"没拿到"就不等于
    "不存在"（否则模型会把网络故障或 key 失效讲成付费墙）。
    """
    logger.info(f"Task: Fetch Paper Full Text | DOI: '{doi}'")
    clean = _normalize_doi(doi)
    if not clean:
        return json.dumps({
            "status": "error",
            "message": "A DOI is required. Take it from a search result's 'doi' field."})
    report = {"checked": [], "failures": []}
    text, origin = fulltext_text(clean, report=report)
    if not text:
        checked = list(dict.fromkeys(report["checked"])) or ["Europe PMC", "OA resolvers"]
        failed = list(dict.fromkeys(report["failures"]))
        if failed:
            message = (
                "No open-access full text could be RETRIEVED for this DOI, but some sources "
                "failed or were skipped, so this search was incomplete: "
                f"{'; '.join(failed)}. Do NOT claim the paper has no open-access "
                "version, and do NOT guess the reason (paywall, licence and publisher policy "
                "are all unverified here). Use the abstract, tell the user the body could not "
                "be fetched in this run, and say the PDF may be obtainable from their own "
                "subscription.")
        else:
            message = (
                "No open-access full text is available for this DOI "
                f"(checked: {', '.join(checked)}). Use the abstract from the search result "
                "and state that only the abstract was available.")
        return json.dumps({
            "status": "success", "doi": clean, "full_text": "", "source": "",
            "conclusion": ("incomplete_sources_failed" if failed
                           else "no_open_access_copy"),
            "sources_checked": checked, "sources_failed": failed,
            "message": message}, ensure_ascii=False)
    truncated = len(text) > max_chars
    logger.info(f"Full text fetched: {clean} via {origin} ({len(text)} chars, truncated={truncated})")
    return json.dumps({"status": "success", "doi": clean, "source": origin,
                       "full_text": text[:max_chars], "truncated": truncated,
                       "chars": len(text)}, ensure_ascii=False)


@simple_retry()
def fetch_open_access_pdf(doi: str, source: Literal["auto", "openalex", "unpaywall", "pubmed", "semantic_scholar"] = "auto") -> str:
    logger.info(f"Task: Fetch OA PDF | DOI: '{doi}' | Source: '{source}'")

    fetcher = OAFetcher()
    result = fetcher.fetch_best_oa_pdf(doi, ncbi_email, ncbi_api_key=ncbi_api_key, source=source)
    if result.get("is_oa"):
        return json.dumps({
            "status": "success", "is_oa": True, "pdf_url": result["pdf_url"],
            "pdf_urls": result.get("pdf_urls") or [result["pdf_url"]],
            "landing_page_url": result["landing_page_url"], "source": result["source"],
            "message": ("An open-access PDF exists. Call fetch_paper_fulltext to read its "
                        "text; this tool only reports the location."),
        }, ensure_ascii=False)

    clean_doi = doi.replace("https://doi.org/", "").replace("http://dx.doi.org/", "").strip()
    landing_url = result.get("landing_page_url", f"https://doi.org/{clean_doi}")
    failed = [str(x) for x in (result.get("failures") or []) if str(x).strip()]
    payload = {
        "status": "success", "is_oa": False, "doi": clean_doi,
        "landing_page_url": landing_url,
        # conclusion 与 fetch_paper_fulltext 共用同一套取值：运行时据此把"没拿到正文"
        # 转成交给用户的取全文窗口（见 AgentRuntime._escalate_fulltext）。
        "conclusion": ("incomplete_sources_failed" if failed
                       else "no_open_access_copy"),
        "sources_failed": list(dict.fromkeys(failed)),
        "message": ("No open-access PDF link could be resolved, so the body is not "
                    "readable from here. Do NOT state that the paper has no full text: "
                    "the user may have it through their own subscription."),
    }
    return json.dumps(payload, ensure_ascii=False)


@simple_retry(max_attempts=2, delay=1)
def search_preprints(query: str, max_results: int = 5) -> str:
    logger.info(f"Task: Preprint Search | Query: '{query}'")
    try:
        return json.dumps({"status": "success",
                           "results": _preprint_records(query, max_results)},
                          ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
