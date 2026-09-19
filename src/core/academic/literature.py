"""Literature tools: unified search, citation graph, OA PDF and preprints."""
import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    "fetch_open_access_pdf", "search_preprints",
]


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


def _merge_records(records):
    """多来源聚合去重。

    - 优先按 DOI 去重；无 DOI 时按标题模糊匹配去重。
    - 合并来源列表、取最高引用数与最完整摘要，并补充期刊名。
    - 每个记录计算 ``confidence`` 可信度评分，供上层参考。
    """
    merged = {}
    order = []

    for rec in records:
        if not isinstance(rec, dict) or not rec.get("title"):
            continue
        doi = _normalize_doi(rec.get("doi"))
        ntitle = _norm_title(rec.get("title"))
        key = f"doi:{doi}" if doi else f"title:{ntitle}"

        if key in merged:
            existing = merged[key]
            # 合并来源（保持有序、去重）
            srcs = existing.get("source_dbs") or []
            for s in rec.get("source_db") and [rec["source_db"]] or []:
                if s and s not in srcs:
                    srcs.append(s)
            existing["source_dbs"] = srcs
            # 引用数取各来源最大值
            existing["citation_count"] = max(
                existing.get("citation_count", 0) or 0, rec.get("citation_count", 0) or 0)
            # 摘要优先取非 "No abstract" 的
            if existing.get("abstract") in (None, "", "No abstract") and rec.get("abstract") not in (None, "", "No abstract"):
                existing["abstract"] = rec["abstract"]
            # 期刊名补充
            if not existing.get("journal") and rec.get("journal"):
                existing["journal"] = rec["journal"]
        else:
            srcs = [rec["source_db"]] if rec.get("source_db") else []
            rec.setdefault("source_dbs", srcs)
            rec.setdefault("journal", "")
            rec.setdefault("pmid", "")
            merged[key] = rec
            order.append(key)

    # 计算可信度评分并排序（引用数为主，来源覆盖数为辅）
    ranked = []
    for key in order:
        rec = merged[key]
        score = 0.0
        n_sources = len(rec.get("source_dbs") or [])
        score += 1.0 * min(n_sources, 3)                      # 多来源覆盖
        score += 1.0 if rec.get("doi") else 0.0                # 有 DOI
        score += 1.0 if rec.get("abstract") not in (None, "", "No abstract") else 0.0  # 有摘要
        score += 0.5 if rec.get("journal") else 0.0            # 有期刊名
        score += 0.2 * min(float(rec.get("citation_count", 0) or 0) / 100.0, 2.0)  # 引用量
        rec["confidence"] = round(score, 2)
        ranked.append(rec)

    ranked.sort(key=lambda r: (r.get("citation_count", 0) or 0, r.get("confidence", 0)), reverse=True)
    return ranked


@simple_retry(max_attempts=2, delay=1)
def search_academic_literature(query: str, max_results: int = 15, offset: int = 0,
                               source: Literal["auto", "semantic_scholar", "openalex", "crossref", "pubmed"] = "auto",
                               aggregate: bool = True, min_year: int | None = None,
                               ) -> str:
    """Unified literature search with cross-database aggregation.

    - ``source="auto"`` queries ALL enabled databases (OpenAlex, Crossref,
      PubMed, Semantic Scholar) in parallel and aggregates + dedupes results,
      improving coverage breadth vs. the legacy single-source short-circuit.
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
            abstract_text = "No abstract"
            if isinstance(abs_idx, dict):
                words = [(pos, w) for w, positions in abs_idx.items() if isinstance(positions, list) for pos in
                         positions]
                words.sort()
                abstract_text = " ".join([w for pos_idx, w in words])

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

            parsed.append({"title": p.get("title", ""), "year": p.get("publication_year", "Unknown"),
                           "authors": authors,
                           "citation_count": p.get("cited_by_count", 0), "abstract": abstract_text,
                           "doi": p.get("doi", "").replace("https://doi.org/", "") if p.get("doi") else "",
                           "url": p.get("id", ""), "source_db": "OpenAlex",
                           "journal": source.get("display_name") or ""})
        return parsed

    def _parse_crossref():
        url = f"https://api.crossref.org/works?query={urllib.parse.quote(query)}&mailto={ncbi_email}&rows={max_results}&offset={offset}"
        parsed = []
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
                           "abstract": p.get("abstract", "No abstract").replace("<jats:p>", "").replace(
                               "</jats:p>", ""),
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

            parsed.append({"title": d.get("Title", ""), "year": d.get("PubDate", "")[:4],
                           "authors": authors, "abstract": "Fetch via fetch_pubmed_abstract.",
                           "pmid": d.get("Id", ""),
                           "doi": doi,
                           "journal": d.get("FullJournalName", ""),
                           "url": f"https://pubmed.ncbi.nlm.nih.gov/{d.get('Id', '')}/", "source_db": "PubMed"})
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
                           "abstract": p.get("abstract") or "No abstract",
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

    # 聚合（auto 且 aggregate=True）或多源短路径
    if source == "auto" and aggregate:
        merged = _merge_records(all_records)
        if not merged:
            return json.dumps({"status": "success", "results": [], "source_stats": source_stats,
                               "message": "No results found from any source"})
        return json.dumps({"status": "success", "source": "aggregated",
                           "source_stats": source_stats, "results": merged}, ensure_ascii=False)

    # 单源 / 不聚合：返回第一个非空来源（保持旧语义），否则汇总所有非空来源
    if not aggregate:
        return json.dumps({"status": "success", "results": all_records, "source_stats": source_stats,
                           "message": "No results found" if not all_records else ""}, ensure_ascii=False)

    # 指定单库检索（source=openalex/crossref/pubmed/semantic_scholar 且 aggregate 默认 True）
    # 此前会落到最后一行恒返回空列表：source_stats 里明明有记录，results 却是 []，
    # 调用方只会看到 "No results found from any source"（实测 openalex 取到 1 条却丢失）。
    # 这里按与其他分支一致的契约返回：合并去重 + 补 confidence/source_dbs。
    merged = _merge_records(all_records)
    if merged:
        return json.dumps({"status": "success",
                           "source": wanted[0] if len(wanted) == 1 else "aggregated",
                           "source_stats": source_stats, "results": merged},
                          ensure_ascii=False)

    return json.dumps({"status": "success", "results": [], "source_stats": source_stats,
                       "message": "No results found from any source"})


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
                abstract_text = "No abstract"
                if isinstance(abs_idx, dict):
                    words = [(pos, w) for w, positions in abs_idx.items() if isinstance(positions, list) for pos in
                             positions]
                    words.sort()
                    abstract_text = " ".join([w for pos_idx, w in words])

                authors_raw = p.get("authorships") or []
                if not isinstance(authors_raw, list): authors_raw = []
                authors = [a.get("author", {}).get("display_name", "") for a in authors_raw if
                           isinstance(a, dict) and isinstance(a.get("author"), dict)]

                parsed.append({"title": p.get("title", ""), "year": p.get("publication_year", "Unknown"),
                               "authors": authors,
                               "citation_count": p.get("cited_by_count", 0),
                               "abstract": abstract_text,
                               "doi": p.get("doi", "").replace("https://doi.org/", "") if p.get("doi") else "",
                               "url": p.get("id", "")})

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
                            "abstract": p.get("abstract") or "No abstract",
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




@simple_retry()
def fetch_open_access_pdf(doi: str, source: Literal["auto", "openalex", "unpaywall", "pubmed", "semantic_scholar"] = "auto") -> str:
    logger.info(f"Task: Fetch OA PDF | DOI: '{doi}' | Source: '{source}'")

    fetcher = OAFetcher()
    result = fetcher.fetch_best_oa_pdf(doi, ncbi_email, ncbi_api_key=ncbi_api_key, source=source)
    if result.get("is_oa"):
        return json.dumps({"status": "success", "is_oa": True, "pdf_url": result["pdf_url"],
                           "landing_page_url": result["landing_page_url"], "source": result["source"]})
    else:
        clean_doi = doi.replace("https://doi.org/", "").replace("http://dx.doi.org/", "").strip()
        landing_url = result.get("landing_page_url", f"https://doi.org/{clean_doi}")
        return json.dumps({"status": "success", "is_oa": False, "landing_page_url": landing_url,
                           "message": "Paywalled. No OA PDF found."})


@simple_retry(max_attempts=2, delay=1)
def search_preprints(query: str, max_results: int = 5) -> str:
    logger.info(f"Task: Preprint Search | Query: '{query}'")
    try:
        url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
        params = {"query": f'({query}) AND (SRC:PPR)', "format": "json", "resultType": "core", "pageSize": max_results}
        res = mcp_request("GET", url, params=params, timeout=15)
        res.raise_for_status()
        results = [
            {"title": p.get("title", ""), "year": p.get("pubYear", "Unknown"), "authors": p.get("authorString", ""),
             "doi": p.get("doi", ""), "source": p.get("bookOrReportDetails", {}).get("publisher", "Preprint Server"),
             "abstract": p.get("abstractText", "No abstract"),
             "url": f"https://doi.org/{p.get('doi')}" if p.get("doi") else ""} for p in
            res.json().get("resultList", {}).get("result", [])]
        return json.dumps({"status": "success", "results": results}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
