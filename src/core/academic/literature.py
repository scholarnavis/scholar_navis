"""Literature tools: unified search, citation graph, OA PDF and preprints."""
import json
import re
import urllib.parse
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


@simple_retry(max_attempts=2, delay=1)
def search_academic_literature(query: str, max_results: int = 15, offset: int = 0, source: Literal["auto", "semantic_scholar", "openalex", "crossref", "pubmed"] = "auto") -> str:
    logger.info(f"Task: Unified Literature Search | Query: '{query}' | Offset: {offset} | Source: {source}")

    if not is_ncbi_enabled():
        logger.error(
            "NCBI has been disabled due to the lack of a valid email address AND API Key; other tools are still functioning normally.")

    if source in ["auto", "openalex"]:
        page = (offset // max_results) + 1
        url = f"https://api.openalex.org/works?search={urllib.parse.quote(query)}&per-page={max_results}&page={page}"

        openalex_rps = 9 if openalex_api_key else 2
        global_rate_limiter.acquire("openalex", rps=openalex_rps)

        if openalex_api_key:
            url += f"&api_key={openalex_api_key}"

        try:
            res = mcp_request("GET", url, timeout=15)
            res.raise_for_status()
            parsed = []
            for p in res.json().get("results", []):
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
                               "citation_count": p.get("cited_by_count", 0), "abstract": abstract_text,
                               "doi": p.get("doi", "").replace("https://doi.org/", "") if p.get("doi") else "",
                               "url": p.get("id", ""), "source_db": "OpenAlex"})
            if parsed: return json.dumps({"status": "success", "source": "openalex", "results": parsed})
        except Exception as e:
            logger.warning(f"OpenAlex search failed: {e}")

    if source in ["auto", "crossref"]:
        url = f"https://api.crossref.org/works?query={urllib.parse.quote(query)}&mailto={ncbi_email}&rows={max_results}&offset={offset}"
        try:
            res = mcp_request("GET", url, timeout=15)
            res.raise_for_status()
            parsed = []
            msg_dict = res.json().get("message")
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

                parsed.append({"title": title,
                               "year": year,
                               "authors": authors, "citation_count": p.get("is-referenced-by-count", 0),
                               "abstract": p.get("abstract", "No abstract").replace("<jats:p>", "").replace(
                                   "</jats:p>", ""),
                               "doi": p.get("DOI", ""), "url": p.get("URL", ""),
                               "source_db": "Crossref"})
            if parsed: return json.dumps({"status": "success", "source": "crossref", "results": parsed})
        except Exception as e:
            logger.warning(f"Crossref search failed: {e}")

    if source in ["auto", "pubmed"] and is_ncbi_enabled():
        try:
            ncbi_rps = 9 if ncbi_api_key else 4
            global_rate_limiter.acquire("ncbi", rps=ncbi_rps)

            search_handle = Entrez.esearch(db="pubmed", term=query, retstart=offset, retmax=max_results)
            search_res = Entrez.read(search_handle, validate=False)
            ids = search_res.get("IdList", []) if isinstance(search_res, dict) else []
            search_handle.close()
            if ids:
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
                                   "url": f"https://pubmed.ncbi.nlm.nih.gov/{d.get('Id', '')}/", "source_db": "PubMed"})
                if parsed: return json.dumps({"status": "success", "source": "pubmed", "results": parsed})
        except Exception as e:
            logger.warning(f"Pubmed search failed: {e}")

    if source in ["auto", "semantic_scholar"] and is_s2_enabled():
        if not s2_api_key:
            logger.warning("Semantic Scholar is disabled due to missing API Key.")
            if source == "semantic_scholar":
                return json.dumps(
                    {"status": "error", "message": "Semantic Scholar API is disabled. Please configure an API Key."})
        elif is_s2_enabled():
            try:
                url = "https://api.semanticscholar.org/graph/v1/paper/search"
                params = {"query": query, "limit": max_results, "offset": offset,
                          "fields": "title,authors,year,abstract,citationCount,isOpenAccess,url,externalIds"}

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
                for p in res.json().get("data", []):
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
                                   "source_db": "Semantic Scholar"})

                    if parsed: return json.dumps({"status": "success", "source": "semantic_scholar", "results": parsed})
            except Exception as e:
                logger.warning(f"Semantic scholar search failed: {e}")


    return json.dumps({"status": "success", "results": [], "message": "No results found from any source"})


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

                ref_ids = work_res.json().get("referenced_works", [])[:max_results]

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
            for p in res.json().get("results", []):
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
