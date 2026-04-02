import ipaddress
import json
import logging
import os
import re
import socket
import sys
import urllib.parse
from functools import wraps
from typing import Literal

from Bio import Entrez

from src.core.config_manager import ConfigManager
from src.core.email_check import verify_email_robust
from src.core.network_worker import setup_global_network_env, create_robust_session, GlobalRateLimiter, global_rate_limiter
from src.core.oa import OAFetcher
from src.task.s2_task import s2_request, is_s2_enabled


def get_app_root():
    if getattr(sys, 'frozen', False) or '__compiled__' in globals():
        return os.path.dirname(sys.executable)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))



APP_ROOT = get_app_root()




class UdpJsonHandler(logging.Handler):
    def __init__(self, server_name="Academic.MCP", host='127.0.0.1'):
        super().__init__()
        self.server_name = server_name
        port_str = os.environ.get("MCP_LOG_PORT")
        self.port = int(port_str) if port_str and port_str.isdigit() else None

        if self.port:
            self.address = (host, self.port)
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        else:
            self.sock = None

    def emit(self, record):
        if not self.sock: return
        try:
            raw_msg = self.format(record)

            if len(raw_msg) > 50000:
                raw_msg = raw_msg[:50000] + "\n...[Log Truncated due to UDP size limit]"

            log_data = {
                "server": self.server_name,
                "level": record.levelname,
                "msg": raw_msg
            }
            self.sock.sendto(json.dumps(log_data).encode('utf-8'), self.address)
        except Exception:
            pass


udp_handler = UdpJsonHandler()

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(udp_handler)

logger = logging.getLogger("Academic.Server")
stderr_handler = logging.StreamHandler(sys.stderr)
root_logger.addHandler(stderr_handler)

ConfigManager()
setup_global_network_env()





def get_setting_or_env(key, env_name):
    """优先读取 GUI 设置，如果为空再读环境变量"""
    val = str(ConfigManager().user_settings.get(key, "")).strip()
    if not val:
        val = os.environ.get(env_name, "").strip()
    return val

ncbi_email = get_setting_or_env("ncbi_email", "NCBI_API_EMAIL")
ncbi_api_key = get_setting_or_env("ncbi_api_key", "NCBI_API_KEY")
openalex_api_key = get_setting_or_env("openalex_api_key", "OPENALEX_API_KEY")
s2_api_key = get_setting_or_env("s2_api_key", "S2_API_KEY")
github_token = get_setting_or_env("github_token", "GITHUB_TOKEN")

_EMAIL_VALID_CACHE = None
def is_ncbi_email_valid():
    global _EMAIL_VALID_CACHE
    if _EMAIL_VALID_CACHE is None:
        if ncbi_email:
            _EMAIL_VALID_CACHE = verify_email_robust(ncbi_email).get("is_valid", False)
        else:
            _EMAIL_VALID_CACHE = False

    if _EMAIL_VALID_CACHE:logger.info(f"NCBI email: {ncbi_email[0:5]}...{ncbi_email[-5:]} is valid.")
    else: logger.error(f"NCBI email: {ncbi_email[0:5]}...{ncbi_email[-5:]} is invalid.")

    return _EMAIL_VALID_CACHE

def is_ncbi_enabled():
    """双重校验：只有 Email 和 API Key 都存在才能使用 NCBI"""
    return is_ncbi_email_valid() and bool(ncbi_api_key)

# 满足你的需求：保持启动时的日志打印！
if ncbi_email: logger.info("Using NCBI Email.")
if ncbi_api_key: logger.info("Using NCBI API Key.")
if openalex_api_key: logger.info("Using OpenALEX API Key.")
if s2_api_key: logger.info("Using S2 API Key.")
if github_token: logger.info("Using GitHub Token.")


WORKSPACE_DIR = os.path.join(APP_ROOT, 'tools',"mcp")
os.makedirs(WORKSPACE_DIR, exist_ok=True)
logger.info(f"Local Workspace initialized at: {WORKSPACE_DIR}")

http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
if http_proxy:
    logger.info(f"MCP Server is running with global proxy: {http_proxy}")

Entrez.email = ncbi_email
Entrez.tool = "ScholarNavis"
if ncbi_api_key:
    Entrez.api_key = ncbi_api_key


def mcp_request(method: str, url: str, **kwargs):
    session = create_robust_session()
    custom_headers = kwargs.pop("headers", {})
    if "User-Agent" in custom_headers and custom_headers["User-Agent"] == "Mozilla/5.0":
        custom_headers.pop("User-Agent")
    session.headers.update(custom_headers)
    try:
        return session.request(method, url, **kwargs)
    except Exception as e:
        err_str = str(e).lower()
        if any(keyword in err_str for keyword in["tls", "closed abruptly", "empty reply", "certificate", "ssl", "time"]):
            logger.warning(f"curl_cffi failed ({e}). Falling back to standard requests for {url}")
            import requests
            req_session = requests.Session()
            http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
            if http_proxy:
                req_session.proxies = {"http": http_proxy, "https": http_proxy}
            req_session.headers.update(custom_headers)
            return req_session.request(method, url, **kwargs)
        raise e
    finally:
        session.close()


def simple_retry(max_attempts=3, delay=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    err_str = str(e).lower()

                    if any(code in err_str for code in ["400", "401", "403", "404", "not found", "bad request"]):
                        logger.warning(f"Client error detected ({err_str}), skipping retry for '{func.__name__}' 喵.")
                        raise e

                    if attempt == max_attempts - 1:
                        raise e
                    wait = delay * (2 ** attempt)
                    logger.warning(f"Attempt {attempt + 1} for '{func.__name__}' failed: {e}. Retrying in {wait}s...")
                    time.sleep(wait)

        return wrapper

    return decorator


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



@simple_retry()
def search_omics_datasets(query: str, db_type: Literal["sra", "geo"] = "sra", max_results: int = 5) -> str:
    logger.info(f"Task: Omics Dataset Search | DB: {db_type} | Query: '{query}'")

    if not is_ncbi_enabled():
        return json.dumps({"status": "error",
                           "message": "NCBI tools are disabled. Both a valid email and NCBI API Key must be configured in Global Settings."})

    ncbi_rps = 9 if ncbi_api_key else 4
    global_rate_limiter.acquire("ncbi", rps=ncbi_rps)

    try:
        db = "gds" if db_type.lower() == "geo" else "sra"
        search_handle = Entrez.esearch(db=db, term=query, retmax=max_results)
        ids = Entrez.read(search_handle).get("IdList", [])
        search_handle.close()

        if not ids: return json.dumps({"status": "success", "results": []})

        summary_handle = Entrez.esummary(db=db, id=",".join(ids))
        summaries = Entrez.read(summary_handle)
        summary_handle.close()

        if isinstance(summaries, list):
            doc_list = summaries
        elif isinstance(summaries, dict):
            ds_set = summaries.get("DocumentSummarySet")
            doc_list = ds_set.get("DocumentSummary", []) if isinstance(ds_set, dict) else []
        else:
            doc_list = []
        if not isinstance(doc_list, list): doc_list = [doc_list]

        parsed_results = []
        for doc in doc_list:
            if db == "sra":
                exp_xml = doc.get("ExpXml", "")
                run_match = re.search(r'acc="([S|E|D]RR\d+)"', exp_xml)
                org_match = re.search(r'<Organism[^>]*>([^<]+)</Organism>', exp_xml)
                parsed_results.append({"accession": run_match.group(1) if run_match else doc.get("Id", ""),
                                       "title": doc.get("ExpTitle", ""), "platform": doc.get("Instrument", ""),
                                       "strategy": doc.get("Library_strategy", ""),
                                       "organism": org_match.group(1) if org_match else ""})
            else:
                parsed_results.append({"accession": doc.get("Accession", ""), "title": doc.get("title", ""),
                                       "summary": doc.get("summary", ""), "study_type": doc.get("gdsType", ""),
                                       "taxon": doc.get("taxon", "")})
        return json.dumps({"status": "success", "db": db_type.upper(), "results": parsed_results})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})




@simple_retry()
def fetch_sequence_fasta(accession_id: str, db_type: Literal["nuccore", "protein"] = "nuccore") -> str:
    logger.info(f"Task: Sequence Fetch | ID: {accession_id} | DB: {db_type}")

    if not is_ncbi_enabled():
        return json.dumps({"status": "error",
                           "message": "NCBI tools are disabled. Both a valid email and NCBI API Key must be configured in Global Settings."})

    ncbi_rps = 9 if ncbi_api_key else 4
    global_rate_limiter.acquire("ncbi", rps=ncbi_rps)

    safe_id = accession_id.strip()
    safe_db = db_type.strip().lower()

    if safe_db in["uniprot", "uniprotkb", "swiss-prot", "trembl"]:
        safe_db = "protein"
    elif safe_db in ["nucleotide", "dna", "rna"]:
        safe_db = "nuccore"

    upper_id = safe_id.upper()
    if upper_id.startswith(("NM_", "NR_", "XM_", "XR_", "NC_", "NG_", "LC_", "MN_", "MT_", "OR_", "PP_", "PQ_")):
        safe_db = "nuccore"
    elif upper_id.startswith(("NP_", "XP_", "WP_", "AP_")):
        safe_db = "protein"

    try:
        fetch_handle = Entrez.efetch(db=safe_db, id=safe_id, rettype="fasta", retmode="text")
        data = fetch_handle.read()
        fetch_handle.close()
        if not data: return json.dumps({"status": "error", "message": "Empty sequence."})
        if len(data) > 15000:
            file_name = f"{safe_id}_{safe_db}.fasta"
            file_path = os.path.join(WORKSPACE_DIR, file_name)
            with open(file_path, "w", encoding='utf-8') as f: f.write(data)
            cite_link = f"cite://view?path={urllib.parse.quote(file_path)}&page=1&name={urllib.parse.quote(file_name)}"
            return json.dumps({
                "status": "success", "message": "Sequence is extremely large. Saved to local workspace.",
                "local_path": file_path, "cite_link": cite_link, "preview_header": data[:500] + "\n..."
            })
        return json.dumps({"status": "success", "accession": accession_id, "fasta": data.strip()})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@simple_retry()
def fetch_taxonomy_info(organism_name: str) -> str:
    logger.info(f"Task: Taxonomy Fetch | Organism: '{organism_name}'")

    if not is_ncbi_enabled():
        return json.dumps({"status": "error",
                           "message": "NCBI tools are disabled. Both a valid email and NCBI API Key must be configured in Global Settings."})

    ncbi_rps = 9 if ncbi_api_key else 4
    global_rate_limiter.acquire("ncbi", rps=ncbi_rps)

    try:
        search_handle = Entrez.esearch(db="taxonomy", term=organism_name, retmax=1)
        ids = Entrez.read(search_handle).get("IdList", [])
        search_handle.close()
        if not ids: return json.dumps({"status": "success", "message": f"Organism '{organism_name}' not found."})

        fetch_handle = Entrez.efetch(db="taxonomy", id=ids[0], retmode="xml")
        tax_records = Entrez.read(fetch_handle)
        fetch_handle.close()
        record = tax_records[0]
        result = {"tax_id": record.get("TaxId", ""), "scientific_name": record.get("ScientificName", ""),
                  "common_name": record.get("OtherNames", {}).get("GenbankCommonName", ""),
                  "rank": record.get("Rank", ""), "lineage": record.get("Lineage", "")}
        return json.dumps({"status": "success", "result": result})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})



@simple_retry(max_attempts=2, delay=1)
def search_gbif_occurrences(scientific_name: str, limit: int = 5) -> str:
    logger.info(f"Task: GBIF Occurrence Search | Species: '{scientific_name}'")
    try:
        match_url = f"https://api.gbif.org/v1/species/match?name={urllib.parse.quote(scientific_name)}"
        match_res = mcp_request("GET", match_url, timeout=10)
        match_res.raise_for_status()
        match_data = match_res.json()

        if match_data.get("matchType") == "NONE" or "usageKey" not in match_data:
            return json.dumps(
                {"status": "error", "message": f"GBIF could not resolve the scientific name '{scientific_name}'."})

        taxon_key = match_data["usageKey"]
        exact_name = match_data.get("scientificName", scientific_name)

        occ_url = f"https://api.gbif.org/v1/occurrence/search?taxonKey={taxon_key}&limit={limit}&hasCoordinate=true"
        occ_res = mcp_request("GET", occ_url, timeout=15)
        occ_res.raise_for_status()
        occ_data = occ_res.json()

        total_records = occ_data.get("count", 0)
        results = []
        for item in occ_data.get("results", []):
            results.append({
                "country": item.get("country", "Unknown"),
                "decimalLatitude": item.get("decimalLatitude"),
                "decimalLongitude": item.get("decimalLongitude"),
                "eventDate": item.get("eventDate", "Unknown"),
                "basisOfRecord": item.get("basisOfRecord", "Unknown"),
                "institutionCode": item.get("institutionCode", "Unknown")
            })

        return json.dumps({
            "status": "success",
            "species": exact_name,
            "taxon_key": taxon_key,
            "total_global_occurrences_with_coordinates": total_records,
            "sample_records": results
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@simple_retry(max_attempts=2, delay=1)
def universal_ncbi_summary(query: str, database: Literal["gene", "protein", "nuccore", "clinvar", "omim", "biosample", "taxonomy", "assembly", "sra"] = "gene", max_results: int = 3) -> str:
    logger.info(f"Task: Universal NCBI Summarize | database: {database} | query: {query}")

    if not is_ncbi_enabled():
        return json.dumps({"status": "error",
                           "message": "NCBI tools are disabled. Both a valid email and NCBI API Key must be configured in Global Settings."})

    ncbi_rps = 9 if ncbi_api_key else 4
    global_rate_limiter.acquire("ncbi", rps=ncbi_rps)


    try:
        search_handle = Entrez.esearch(db=database, term=query, retmax=max_results)
        ids = Entrez.read(search_handle, validate=False).get("IdList",[])
        search_handle.close()
        if not ids: return json.dumps(
            {"status": "success", "results":[], "message": f"No records found in {database}."})

        summary_handle = Entrez.esummary(db=database, id=",".join(ids))
        summaries = Entrez.read(summary_handle, validate=False)
        summary_handle.close()

        if isinstance(summaries, list):
            doc_list = summaries
        elif isinstance(summaries, dict):
            ds_set = summaries.get("DocumentSummarySet")
            doc_list = ds_set.get("DocumentSummary", []) if isinstance(ds_set, dict) else []
        else:
            doc_list = []
        if not isinstance(doc_list, list): doc_list = [doc_list]

        parsed_results =[]
        for d in doc_list:
            if not isinstance(d, dict): continue
            uid = d.get("Id", "")
            parsed_results.append({"id": uid, "url": f"https://www.ncbi.nlm.nih.gov/{database}/{uid}", **{k: str(v) for k, v in d.items() if not k.startswith("Item")}})
        return json.dumps({"status": "success", "database": database, "results": parsed_results})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@simple_retry(max_attempts=2, delay=1)
def fetch_webpage_content(url: str, timeout: int = 15) -> str:
    logger.info(f"Task: Fetch Webpage | URL: '{url}'")
    if not url.startswith(("http://", "https://")): return json.dumps(
        {"status": "error", "message": "Security Error: Only HTTP(S) allowed."})
    parsed_url = urllib.parse.urlparse(url)
    hostname = parsed_url.hostname
    if hostname in ['localhost', 'broadcasthost'] or hostname.endswith('.local'):
        return json.dumps({"status": "error", "message": "Security Error: Local network access forbidden."})
    try:
        ip_obj = ipaddress.ip_address(socket.gethostbyname(hostname))
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
            return json.dumps(
                {"status": "error", "message": "Security Error: Probing internal network IPs is forbidden."})
    except Exception:
        pass
    try:
        res = mcp_request("GET", url, timeout=timeout)
        res.raise_for_status()
        html_content = res.text
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')

        for script_or_style in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
            script_or_style.decompose()

        text_content = soup.get_text(separator=' ', strip=True)

        if len(text_content) > 30000:
            text_content = text_content[:30000] + "\n...[Content truncated]"

        return json.dumps({"status": "success", "url": url, "content": text_content}, ensure_ascii=False)

    except Exception as e:
        err_str = str(e)
        if "403" in err_str or "Forbidden" in err_str:
            err_str += " (Access Denied: The website actively blocks automated access or requires a subscription/captcha.)"
        elif "404" in err_str or "Not Found" in err_str:
            err_str += " (Not Found: The page does not exist.)"
        return json.dumps({"status": "error", "message": err_str})



@simple_retry(max_attempts=2, delay=1)
def search_web(query: str, engine: Literal["duckduckgo", "google", "bing", "baidu"] = "duckduckgo",
               max_results: int = 3) -> str:
    logger.info(f"Task: Web Search ({engine}) | Query: '{query}'")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7"
        }
        results = []
        from bs4 import BeautifulSoup

        safe_query = urllib.parse.quote(query)

        if engine == "duckduckgo":
            # 使用 DuckDuckGo HTML 端点替代 Lite 端点以绕过部分限制
            url = "https://html.duckduckgo.com/html/"
            data = {"q": query}
            headers.update({"Origin": "https://html.duckduckgo.com", "Referer": "https://html.duckduckgo.com/",
                            "Content-Type": "application/x-www-form-urlencoded"})
            res = mcp_request("POST", url, data=data, headers=headers, timeout=10)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, 'html.parser')

            # 更新针对 HTML 端点的 CSS 类名解析逻辑
            for div in soup.find_all('div', class_=re.compile(r'result ')):
                if len(results) >= max_results: break
                a = div.find('a', class_='result__url')
                if not a: continue

                title_elem = div.find('h2', class_='result__title')
                title = title_elem.get_text(strip=True) if title_elem else a.get_text(strip=True)
                link = a.get('href', '')
                snippet_div = div.find('a', class_='result__snippet')
                snippet = snippet_div.get_text(separator=" ", strip=True) if snippet_div else "No abstract."

                if link and not link.startswith(('/', 'duckduckgo.com')):
                    results.append(
                        {"_mcp_cite_id": str(101 + len(results)), "cite_link": link, "title": title, "url": link,
                         "snippet": snippet})

        elif engine == "bing":
            url = f"https://www.bing.com/search?q={safe_query}"
            res = mcp_request("GET", url, headers=headers, timeout=10)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, 'html.parser')
            for li in soup.find_all('li', class_='b_algo'):
                if len(results) >= max_results: break
                h2 = li.find('h2')
                a = h2.find('a') if h2 else None
                if not a or not a.get('href'): continue
                title = a.get_text(strip=True)
                link = a.get('href')
                snippet_div = li.find('div', class_='b_caption') or li.find('p')
                snippet = snippet_div.get_text(separator=" ", strip=True) if snippet_div else "No abstract."
                results.append({"_mcp_cite_id": str(101 + len(results)), "cite_link": link, "title": title, "url": link,
                                "snippet": snippet})


        elif engine == "google":
            url = f"https://www.google.com/search?q={safe_query}"
            res = mcp_request("GET", url, headers=headers, timeout=10)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, 'html.parser')
            for div in soup.find_all('div', class_='g'):
                if len(results) >= max_results: break
                a = div.find('a')
                h3 = div.find('h3')
                if not a or not a.get('href') or not h3: continue
                title = h3.get_text(strip=True)
                link = a.get('href')
                snippet_div = div.find('div', class_='VwiC3b') or div.find('div',
                                                                           style=re.compile(r'-webkit-line-clamp'))
                snippet = snippet_div.get_text(separator=" ", strip=True) if snippet_div else "No abstract."
                results.append({"_mcp_cite_id": str(101 + len(results)), "cite_link": link, "title": title, "url": link,
                                "snippet": snippet})


        elif engine == "baidu":
            url = f"https://www.baidu.com/s?wd={safe_query}"
            res = mcp_request("GET", url, headers=headers, timeout=10)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, 'html.parser')
            for div in soup.find_all('div', class_=re.compile(r'result c-container')):
                if len(results) >= max_results: break
                h3 = div.find('h3')
                a = h3.find('a') if h3 else None
                if not a or not a.get('href'): continue
                title = a.get_text(strip=True)
                link = a.get('href')
                snippet_div = div.find('div', class_=re.compile(r'c-abstract'))
                snippet = snippet_div.get_text(separator=" ", strip=True) if snippet_div else "No abstract."
                results.append({"_mcp_cite_id": str(101 + len(results)), "cite_link": link, "title": title, "url": link,
                                "snippet": snippet})

        if not results:
            return json.dumps({"status": "error",
                               "message": f"{engine.capitalize()} blocked the request or returned no results. Try another engine or 'search_academic_literature'."})

        return json.dumps({
            "status": "success",
            "engine": engine.capitalize(),
            "query": query,
            "results": results
        }, ensure_ascii=False)

    except ImportError:
        logger.error("Missing library: beautifulsoup4")
        return json.dumps({"status": "error", "message": "Please install beautifulsoup4 via pip."})
    except Exception as e:
        logger.error(f"Web search failed: {e}")
        return json.dumps({"status": "error",
                           "message": f"Search engine error: {str(e)}. Consider using 'duckduckgo' if 403 Forbidden occurs."})



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



@simple_retry(max_attempts=2, delay=1)
def fetch_wikipedia_summary(query: str, language: str = "en") -> str:
    logger.info(f"Task: Wikipedia Extract | Query: '{query}'")
    try:
        url = f"https://{language}.wikipedia.org/w/api.php"
        params = {"action": "query", "prop": "extracts", "exchars": 1500, "explaintext": 1, "generator": "search",
                  "gsrsearch": query, "gsrlimit": 1, "format": "json"}
        res = mcp_request("GET", url, params=params, timeout=10)
        res.raise_for_status()
        pages = res.json().get("query", {}).get("pages", {})
        if not pages: return json.dumps({"status": "error", "message": "No Wikipedia article found."})
        page = list(pages.values())[0]
        return json.dumps({
            "status": "success",
            "results": [{
                "title": page.get("title", ""),
                "abstract": page.get("extract", "").strip(),
                "url": f"https://{language}.wikipedia.org/wiki/{urllib.parse.quote(page.get('title', ''))}"
            }]
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})



@simple_retry(max_attempts=2, delay=1)
def search_github_repos(query: str, max_results: int = 5) -> str:
    logger.info(f"Task: GitHub Search | Query: '{query}'")

    github_rph = 4900 if github_token else 50
    global_rate_limiter.acquire("github", rph=github_rph)

    try:
        url = "https://api.github.com/search/repositories"
        params = {"q": query, "sort": "stars", "order": "desc", "per_page": max_results}
        headers = {"Accept": "application/vnd.github.v3+json",
                   "Authorization": f"Bearer {github_token}" if github_token else ""}
        res = mcp_request("GET", url, params=params, headers=headers, timeout=10)
        res.raise_for_status()
        results = [{"name": r.get("full_name", ""), "description": r.get("description", "No description"),
                    "url": r.get("html_url", ""), "stars": r.get("stargazers_count", 0),
                    "language": r.get("language", "Unknown"), "last_updated": r.get("updated_at", "")[:10]} for r in
                   res.json().get("items", [])]
        return json.dumps({"status": "success", "results": results}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})



@simple_retry(max_attempts=2, delay=1)
def query_kegg_database(query: str, action: Literal["search_pathway", "get_record"] = "search_pathway",
                        organism_code: str = "ath") -> str:
    logger.info(f"Task: KEGG Query | Action: '{action}' | Query: '{query}' | Organism: '{organism_code}'")
    safe_query = urllib.parse.quote(query.strip())

    try:
        if action == "search_pathway":
            safe_org_code = organism_code.strip().lower()
            url = f"https://rest.kegg.jp/find/pathway/{safe_query}"
            res = mcp_request("GET", url, timeout=15)

            if res.status_code == 400 or not res.text.strip():
                return json.dumps({"status": "success",
                                   "message": f"0 results found for '{query}'. Verify if organism code '{safe_org_code}' is correct."})
            res.raise_for_status()

            results = []
            for line in res.text.strip().split('\n'):
                if not line: continue
                parts = line.split('\t', 1)
                if len(parts) == 2 and (
                        parts[0].startswith(f"path:{safe_org_code}") or parts[0].startswith("path:map")):
                    results.append({"pathway_id": parts[0], "description": parts[1]})

            return json.dumps(
                {"status": "success", "action": "search_pathway", "organism": safe_org_code, "results": results[:10]},
                ensure_ascii=False)

        elif action == "get_record":
            url = f"https://rest.kegg.jp/get/{safe_query}"
            res = mcp_request("GET", url, timeout=15)

            if res.status_code in [400, 404] or not res.text.strip():
                return json.dumps(
                    {"status": "error", "message": f"KEGG record '{query}' not found. Ensure valid identifier."})
            res.raise_for_status()

            parsed_data = {}
            current_key = None
            for line in res.text.split("\n"):
                if not line: continue
                if not line.startswith(" "):
                    current_key = line[:12].strip()
                    parsed_data[current_key] = [line[12:].strip()]
                elif current_key:
                    parsed_data[current_key].append(line[12:].strip())

            summary = {
                "identifier": query.strip(),
                "name": ", ".join(parsed_data.get("NAME", [])),
                "definition": " ".join(parsed_data.get("DEFINITION", [])),
                "pathways": parsed_data.get("PATHWAY", []),
                "genes": parsed_data.get("GENES", [])[:5]
            }
            return json.dumps({"status": "success", "action": "get_record", "record": summary}, ensure_ascii=False)

        else:
            return json.dumps(
                {"status": "error", "message": "Invalid action. Must be 'search_pathway' or 'get_record'."})

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})





@simple_retry(max_attempts=2, delay=1)
def fetch_go_annotations(uniprot_id: str, limit: int = 10) -> str:
    logger.info(f"Task: QuickGO Annotation Fetch | UniProt ID: '{uniprot_id}'")
    try:
        safe_id = urllib.parse.quote(uniprot_id.strip().upper())
        # 查询包含该 UniProt ID 的所有 GO 注释
        url = f"https://www.ebi.ac.uk/QuickGO/services/annotation/search?geneProductId={safe_id}&limit={limit}"

        res = mcp_request("GET", url, timeout=15)
        res.raise_for_status()

        data = res.json()
        results = []
        for item in data.get("results", []):
            results.append({
                "goId": item.get("goId"),
                "goName": item.get("goName"),
                "aspect": item.get("goAspect"),
                "evidenceCode": item.get("goEvidence"),
                "reference": item.get("reference")
            })

        if not results:
            return json.dumps({"status": "success", "message": f"No GO annotations found for {uniprot_id}."})

        return json.dumps({"status": "success", "source": "QuickGO", "uniprot_id": uniprot_id, "annotations": results},
                          ensure_ascii=False)

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})




@simple_retry(max_attempts=2, delay=1)
def search_chembl_target(query: str, max_results: int = 5) -> str:
    logger.info(f"Task: ChEMBL Target Search | Query: '{query}'")
    try:
        url = "https://www.ebi.ac.uk/chembl/api/data/target/search"
        params = {"q": query, "format": "json", "limit": max_results}
        res = mcp_request("GET", url, params=params, timeout=15)
        res.raise_for_status()
        results = [{"target_chembl_id": t.get("target_chembl_id", ""), "pref_name": t.get("pref_name", ""),
                    "target_type": t.get("target_type", ""), "organism": t.get("organism", ""),
                    "species_group_flag": t.get("species_group_flag", False)} for t in res.json().get("targets", [])]
        return json.dumps({"status": "success", "results": results}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})



@simple_retry(max_attempts=2, delay=1)
def uniprot_id_mapping(from_db: str, to_db: str, ids: str) -> str:
    logger.info(f"Task: UniProt ID Mapping | From: {from_db} | To: {to_db} | IDs: {ids[:20]}...")
    try:
        submit_url = "https://rest.uniprot.org/idmapping/run"
        payload = {"from": from_db, "to": to_db, "ids": ids}
        res = mcp_request("POST", submit_url, data=payload, timeout=15)
        if res.status_code == 400:
            return json.dumps({
                "status": "error",
                "message": f"HTTP 400 Bad Request. Invalid from_db '{from_db}' or to_db '{to_db}'. 'Gene_Name' is NOT supported for mapping; use 'HGNC' or query UniProt directly."
            })
        res.raise_for_status()
        job_id = res.json().get("jobId")
        if not job_id: return json.dumps({"status": "error", "message": "Failed to retrieve jobId from UniProt."})
        status_url = f"https://rest.uniprot.org/idmapping/status/{job_id}"
        status = "NEW"
        for _ in range(10):
            time.sleep(2)
            s_res = mcp_request("GET", status_url, timeout=10)
            s_res.raise_for_status()
            s_data = s_res.json()
            if "jobStatus" in s_data:
                status = s_data["jobStatus"]
                if status in ["FINISHED", "ERROR", "ABORTED"]: break
            elif "results" in s_data:
                status = "FINISHED"
                break
        if status != "FINISHED": return json.dumps(
            {"status": "timeout", "jobId": job_id, "message": f"Job is currently '{status}'."})
        result_url = f"https://rest.uniprot.org/idmapping/results/{job_id}?size=10"
        r_res = mcp_request("GET", result_url, timeout=15)
        r_res.raise_for_status()
        results = []
        for item in r_res.json().get("results", []):
            mapped_to = item.get("to", "")
            if isinstance(mapped_to, dict): mapped_to = mapped_to.get("primaryAccession", str(mapped_to))
            results.append({"from": item.get("from", ""), "to": mapped_to})
        return json.dumps({"status": "success", "jobId": job_id, "mapped_results": results}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})



@simple_retry(max_attempts=2, delay=1)
def query_uniprot_database(query: str, db_type: Literal[
    "uniprotkb", "proteomes", "genecentric", "uniref", "uniparc", "unirule", "arba"] = "uniprotkb",
                           max_results: int = 5) -> str:
    db_type = db_type.lower()
    logger.info(f"Task: Unified UniProt Search | DB: '{db_type}' | Query: '{query}'")
    try:
        # Branch 1: UniProtKB
        if db_type == "uniprotkb":
            is_accession = re.match(r"^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z]([0-9][A-Z][A-Z0-9]{2}){1,2}[0-9])(-\d+)?$",
                                    query.upper())

            if is_accession:
                res = mcp_request("GET", f"https://rest.uniprot.org/uniprotkb/{query.upper()}", timeout=15)
                res.raise_for_status()
                data_list = [res.json()]
            else:
                res = mcp_request("GET", "https://rest.uniprot.org/uniprotkb/search",
                                  params={"query": query, "size": max_results}, timeout=15)
                if res.status_code == 400:
                    return json.dumps({
                        "status": "error",
                        "message": f"HTTP 400 Bad Request: UniProt rejected your query '{query}'. "
                                   f"Syntax Error! You MUST wrap species names with spaces in quotes. "
                                   f"Example: (gene:FLC) AND (organism_name:\"Arabidopsis thaliana\"). "
                                   f"API Response: {res.text}"
                    })
                res.raise_for_status()
                data_list = res.json().get("results", [])

            results = []
            for item in data_list:
                rec_name = item.get("proteinDescription", {}).get("recommendedName", {}).get("fullName", {}).get(
                    "value", "")
                if not rec_name:
                    subs = item.get("proteinDescription", {}).get("submissionNames", [])
                    rec_name = subs[0].get("fullName", {}).get("value", "Unknown") if subs else "Unknown"

                gene_name = "Unknown"
                if item.get("genes") and len(item["genes"]) > 0:
                    gene_name = item["genes"][0].get("geneName", {}).get("value", "Unknown")

                ncbi_gene_ids = []
                for xref in item.get("uniProtKBCrossReferences", []):
                    if xref.get("database") == "GeneID":
                        ncbi_gene_ids.append(xref.get("id"))
                ncbi_id_str = ", ".join(ncbi_gene_ids) if ncbi_gene_ids else "Not Found"

                subcellular_locations = []
                function_texts = []
                for comment in item.get("comments", []):
                    if comment.get("commentType") == "SUBCELLULAR LOCATION":
                        for loc in comment.get("subcellularLocations", []):
                            loc_val = loc.get("location", {}).get("value")
                            if loc_val and loc_val not in subcellular_locations:
                                subcellular_locations.append(loc_val)
                    elif comment.get("commentType") == "FUNCTION":
                        for text_item in comment.get("texts", []):
                            function_texts.append(text_item.get("value", ""))

                sub_loc_str = ", ".join(subcellular_locations) if subcellular_locations else "Not specified"
                function_str = " ".join(function_texts) if function_texts else "Not specified"

                sequence_str = item.get("sequence", {}).get("value", "")

                results.append({
                    "accession": item.get("primaryAccession", ""),
                    "proteinName": rec_name,
                    "geneSymbol": gene_name,
                    "ncbi_gene_id": ncbi_id_str,
                    "organism": item.get("organism", {}).get("scientificName", ""),
                    "sequence_length": item.get("sequence", {}).get("length", 0),
                    "subcellular_localization": sub_loc_str,
                    "function_description": function_str,  # 新增
                    "sequence": sequence_str  # 新增
                })
            return json.dumps({"status": "success", "db": "uniprotkb", "results": results}, ensure_ascii=False)

        # Branch 2: Proteomes
        elif db_type == "proteomes":
            res = mcp_request("GET", "https://rest.uniprot.org/proteomes/search",
                              params={"query": query, "size": max_results}, timeout=15)
            if res.status_code == 400: return json.dumps(
                {"status": "error", "message": f"HTTP 400: Invalid syntax for '{query}'"})
            res.raise_for_status()
            results = [{"id": p.get("id", ""), "taxonomy": p.get("taxonomy", {}).get("scientificName", ""),
                        "proteomeType": p.get("proteomeType", ""), "proteinCount": p.get("proteinCount", 0)} for p in
                       res.json().get("results", [])]
            return json.dumps({"status": "success", "db": "proteomes", "results": results}, ensure_ascii=False)

        # Branch 3: GeneCentric
        elif db_type == "genecentric":
            res = mcp_request("GET", "https://rest.uniprot.org/genecentric/search",
                              params={"query": query, "size": max_results}, timeout=15)
            if res.status_code == 400: return json.dumps(
                {"status": "error", "message": f"HTTP 400: Invalid syntax for '{query}'"})
            res.raise_for_status()
            results = [{"proteomeId": item.get("proteomeId", ""),
                        "canonical_accession": item.get("canonicalProtein", {}).get("id", ""),
                        "geneName": item.get("canonicalProtein", {}).get("geneName", ""),
                        "proteinName": item.get("canonicalProtein", {}).get("proteinName", ""),
                        "organism": item.get("canonicalProtein", {}).get("organism", {}).get("scientificName", "")} for
                       item in res.json().get("results", [])]
            return json.dumps({"status": "success", "db": "genecentric", "results": results}, ensure_ascii=False)

        # Branch 4: UniRef
        elif db_type == "uniref":
            res = mcp_request("GET", "https://rest.uniprot.org/uniref/search",
                              params={"query": query, "size": max_results}, timeout=15)
            if res.status_code == 400: return json.dumps(
                {"status": "error", "message": f"HTTP 400: Invalid syntax for '{query}'"})
            res.raise_for_status()
            results = [
                {"id": item.get("id", ""), "name": item.get("name", ""), "memberCount": item.get("memberCount", 0),
                 "commonTaxon": item.get("commonTaxon", {}).get("scientificName", ""),
                 "representative_accession": item.get("representativeMember", {}).get("memberId", "")} for item in
                res.json().get("results", [])]
            return json.dumps({"status": "success", "db": "uniref", "results": results}, ensure_ascii=False)

        # Branch 5: UniParc
        elif db_type == "uniparc":
            res = mcp_request("GET", "https://rest.uniprot.org/uniparc/search",
                              params={"query": query, "size": max_results}, timeout=15)
            if res.status_code == 400: return json.dumps(
                {"status": "error", "message": f"HTTP 400: Invalid syntax for '{query}'"})
            res.raise_for_status()
            results = [{"upi": item.get("uniParcId", ""), "sequence_length": item.get("sequence", {}).get("length", 0),
                        "most_recent_cross_ref": item.get("mostRecentCrossRefUpdated", "")} for item in
                       res.json().get("results", [])]
            return json.dumps({"status": "success", "db": "uniparc", "results": results}, ensure_ascii=False)

        # Branch 6: Annotations
        elif db_type in ["unirule", "arba"]:
            res = mcp_request("GET", f"https://rest.uniprot.org/{db_type}/search",
                              params={"query": query, "size": max_results}, timeout=15)
            if res.status_code == 400: return json.dumps(
                {"status": "error", "message": f"HTTP 400: Invalid syntax for '{query}'"})
            res.raise_for_status()
            results = [{"ruleId": item.get("uniRuleId", ""),
                        "reviewedProteinCount": item.get("statistics", {}).get("reviewedProteinCount", 0),
                        "unreviewedProteinCount": item.get("statistics", {}).get("unreviewedProteinCount", 0)} for item
                       in res.json().get("results", [])]
            return json.dumps({"status": "success", "db": db_type, "results": results}, ensure_ascii=False)

        else:
            return json.dumps({"status": "error",
                               "message": f"Invalid db_type: {db_type}. Must be one of: uniprotkb, proteomes, genecentric, uniref, uniparc, unirule, arba."})

    except Exception as e:
        logger.error(f"Unified UniProt Search failed: {e}")
        return json.dumps({"status": "error", "message": str(e)})



@simple_retry(max_attempts=2, delay=1)
def fetch_alphafold_structure(uniprot_id: str) -> str:
    logger.info(f"Task: AlphaFold Structure Fetch | UniProt ID: '{uniprot_id}'")
    try:
        safe_id = urllib.parse.quote(uniprot_id.strip().upper())
        url = f"https://alphafold.ebi.ac.uk/api/prediction/{safe_id}"

        res = mcp_request("GET", url, timeout=15)

        if res.status_code == 404:
            return json.dumps({
                "status": "error",
                "message": f"AlphaFold prediction not found for UniProt ID '{uniprot_id}'. The protein might not be in the database or the ID is invalid."
            })
        res.raise_for_status()

        data = res.json()
        if not data:
            return json.dumps({"status": "error", "message": "Empty response from AlphaFold DB."})

        results = []
        for item in data:
            results.append({
                "uniprot_id": item.get("uniprotAccession"),
                "uniprot_description": item.get("uniprotDescription"),
                "organism": item.get("speciesScientificName"),
                "model_created_date": item.get("modelCreatedDate"),
                "latest_version": item.get("latestVersion"),
                "pdb_download_url": item.get("pdbUrl"),
                "cif_download_url": item.get("cifUrl"),
                "pae_image_url": item.get("paeImageUrl"),
                "confidence_score_avg": item.get("fractionConfidentResidues")
            })

        return json.dumps({"status": "success", "source": "AlphaFold DB", "results": results}, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})





@simple_retry(max_attempts=2, delay=1)
def query_pdb_structure(query: str, action: Literal["search", "details"] = "search", max_results: int = 3) -> str:
    logger.info(f"Task: Unified PDB Query | Action: '{action}' | Query: '{query}'")
    try:
        if action == "search":
            url = "https://search.rcsb.org/rcsbsearch/v2/query"

            clean_query = query.replace('"', '').strip()
            payload = {"query": {"type": "terminal", "service": "full_text", "parameters": {"value": clean_query}},
                       "return_type": "entry", "request_options": {"paginate": {"start": 0, "rows": max_results}}}
            res = mcp_request("POST", url, json=payload, timeout=10)

            if res.status_code == 400:
                return json.dumps({
                    "status": "error",
                    "message": f"HTTP 400 Bad Request. PDB API rejected the search query '{clean_query}'. Try using a SIMPLER single keyword without spaces (e.g., 'CRY1' instead of 'Arabidopsis thaliana CRY1')."
                })
            res.raise_for_status()

            pdb_ids = [item["identifier"] for item in res.json().get("result_set", [])]
            if not pdb_ids:
                return json.dumps({"status": "success", "results": [],
                                   "message": f"0 results found for '{clean_query}'. Try a different keyword."})

            results = []
            for pid in pdb_ids:
                det_res = mcp_request("GET", f"https://data.rcsb.org/rest/v1/core/entry/{pid}", timeout=5)
                if det_res.status_code == 200:
                    d = det_res.json()
                    entry_info = d.get("rcsb_entry_info", {})

                    res_val = entry_info.get("resolution_estimated_by_xray")
                    if not res_val:
                        res_val = entry_info.get("resolution_combined", [None])[0]
                    if not res_val:
                        res_val = "N/A (Please check manually)"

                    results.append({
                        "pdb_id": pid,
                        "title": d.get("struct", {}).get("title", ""),
                        "method": d.get("exptl", [{}])[0].get("method", "Unknown"),
                        "resolution": res_val,
                        "organism": d.get("rcsb_entity_source_organism", [{}])[0].get("ncbi_scientific_name", "Unknown")
                    })
            return json.dumps({"status": "success", "action": "search", "results": results})

        elif action == "details":
            url = f"https://data.rcsb.org/rest/v1/core/entry/{query.upper()}"
            res = mcp_request("GET", url, timeout=15)

            if res.status_code == 404:
                return json.dumps(
                    {"status": "error", "message": f"PDB ID '{query.upper()}' not found. Please verify the ID."})

            res.raise_for_status()
            data = res.json()

            entry_info = data.get("rcsb_entry_info", {})
            citation_data = data.get("citation", [{}])[0]
            exptl_data = data.get("exptl", [{}])[0]

            res_val = entry_info.get("resolution_estimated_by_xray")
            if not res_val:
                res_val = entry_info.get("resolution_combined", [None])[0]
            if not res_val:
                res_val = "N/A (Please check manually)"

            macromolecules = []
            for poly in data.get("polymer_entity", []):
                desc = poly.get("rcsb_polymer_entity", {}).get("pdbx_description", "")
                if desc and desc not in macromolecules:
                    macromolecules.append(desc)

            ligands = []
            for nonpoly in data.get("nonpolymer_entity", []):
                comp_id = nonpoly.get("pdbx_entity_nonpoly", {}).get("comp_id", "")
                name = nonpoly.get("pdbx_entity_nonpoly", {}).get("name", "")
                if comp_id and name:
                    ligands.append(f"{name} ({comp_id})")

            results = {
                "pdb_id": data.get("entry", {}).get("id", query.upper()),
                "title": data.get("struct", {}).get("title", ""),
                "method": exptl_data.get("method", "Unknown"),
                "resolution": res_val,
                "molecular_weight_kDa": entry_info.get("molecular_weight", 0),
                "atom_count": entry_info.get("deposited_atom_count", 0),
                "macromolecules": macromolecules,
                "ligands": ligands,
                "primary_citation": {
                    "title": citation_data.get("title", ""),
                    "journal": citation_data.get("journal_abbrev", ""),
                    "year": citation_data.get("year", ""),
                    "pmid": citation_data.get("pdbx_database_id_PubMed", "")
                }
            }
            return json.dumps({"status": "success", "action": "details", "results": results}, ensure_ascii=False)

        else:
            return json.dumps({"status": "error", "message": "Invalid action. Must be 'search' or 'details'."})

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})



@simple_retry(max_attempts=2, delay=1)
def query_metabolite_database(query: str, database: Literal["pubchem", "chebi"] = "pubchem") -> str:
    logger.info(f"Task: Metabolite Query | Database: '{database}' | Query: '{query}'")
    safe_query = urllib.parse.quote(query.strip())

    try:
        if database == "pubchem":
            properties = "MolecularWeight,MolecularFormula,CanonicalSMILES,IsomericSMILES,IUPACName,ExactMass"
            url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{safe_query}/property/{properties}/JSON"
            res = mcp_request("GET", url, timeout=15)

            if res.status_code == 404:
                return json.dumps({"status": "error", "message": f"Compound '{query}' not found in PubChem."})
            res.raise_for_status()

            props = res.json().get("PropertyTable", {}).get("Properties", [])
            if not props: return json.dumps({"status": "error", "message": "No properties returned."})

            props[0]["url"] = f"https://pubchem.ncbi.nlm.nih.gov/compound/{props[0].get('CID', '')}"
            return json.dumps({"status": "success", "database": "PubChem", "results": [props[0]]}, ensure_ascii=False)

        elif database == "chebi":
            ols_url = f"https://www.ebi.ac.uk/ols4/api/search?q={safe_query}&ontology=chebi&exact=false&rows=5"
            res = mcp_request("GET", ols_url, timeout=15)
            res.raise_for_status()

            results = [{"chebi_id": item.get("obo_id", ""), "name": item.get("label", ""),
                        "description": item.get("description", [""])[0]} for item in
                       res.json().get("response", {}).get("docs", [])]

            if not results: return json.dumps(
                {"status": "success", "message": f"No metabolites found in ChEBI for '{query}'."})
            return json.dumps({"status": "success", "database": "ChEBI", "results": results}, ensure_ascii=False)

        else:
            return json.dumps({"status": "error", "message": "Invalid database. Must be 'pubchem' or 'chebi'."})

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@simple_retry(max_attempts=2, delay=1)
def analyze_systems_network(identifiers: str, action: Literal["interactions", "enrichment"] = "interactions",
                            species_id: int = 3702, organism: str = "athaliana", limit: int = 15) -> str:
    logger.info(f"Task: Systems Network | Action: '{action}' | Identifiers: '{identifiers[:30]}'...")
    try:
        clean_identifiers = [x.strip() for x in identifiers.split(",") if x.strip()]

        if action == "interactions":
            url = "https://string-db.org/api/json/interaction_partners"
            payload = {"identifiers": "\r".join(clean_identifiers), "species": species_id, "limit": limit,
                       "caller_identity": "ScholarNavis"}
            res = mcp_request("POST", url, data=payload, timeout=15)

            if res.status_code in [400, 404]:
                return json.dumps(
                    {"status": "error", "message": f"STRING DB failed for '{identifiers}'. Check TaxID {species_id}."})
            res.raise_for_status()

            results = [{"protein_A": item.get("preferredName_A", ""), "protein_B": item.get("preferredName_B", ""),
                        "score": item.get("score", 0), "annotation_A": item.get("annotation_A", ""),
                        "annotation_B": item.get("annotation_B", "")} for item in res.json()]

            results = sorted(results, key=lambda x: x["score"], reverse=True)
            return json.dumps({"status": "success", "action": "interactions", "database": "STRING", "results": results},
                              ensure_ascii=False)

        elif action == "enrichment":
            url = "https://biit.cs.ut.ee/gprofiler/api/gost/profile/"
            payload = {
                "organism": organism.lower(),
                "query": clean_identifiers,
                "sources": ["GO:BP", "GO:MF", "GO:CC", "KEGG", "REAC"],
                "significance_threshold_method": "fdr",
                "user_threshold": 0.05
            }
            res = mcp_request("POST", url, json=payload, timeout=20)
            res.raise_for_status()
            data = res.json().get("result", [])

            if not data:
                return json.dumps(
                    {"status": "success", "message": f"No significant enrichment found for organism '{organism}'."})

            results = [{"source": item.get("source", ""), "term_id": item.get("native", ""),
                        "description": item.get("name", ""), "p_value": item.get("p_value", 1.0),
                        "intersection_size": item.get("intersection_size", 0)} for item in data]

            results = sorted(results, key=lambda x: x["p_value"])[:limit]
            return json.dumps(
                {"status": "success", "action": "enrichment", "database": "g:Profiler", "organism": organism,
                 "enriched_terms": results}, ensure_ascii=False)

        else:
            return json.dumps({"status": "error", "message": "Invalid action. Must be 'interactions' or 'enrichment'."})

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})




@simple_retry(max_attempts=2, delay=1)
def query_plant_multiomics(gene_id: str, action: Literal["annotation", "expression"] = "annotation") -> str:
    logger.info(f"Task: Plant Multiomics | Action: '{action}' | Gene: '{gene_id}'")
    try:
        safe_id = urllib.parse.quote(gene_id.strip())

        if action == "annotation":
            url = f"https://mygene.info/v3/query?q={safe_id}&fields=symbol,name,taxid,ensembl,tair,entrezgene,summary,go,pathway&species=all"
            res = mcp_request("GET", url, timeout=15)
            res.raise_for_status()

            hits = res.json().get("hits", [])
            if not hits:
                return json.dumps({"status": "success", "message": f"No deep annotations found for '{gene_id}'."})

            result = hits[0]
            parsed_data = {
                "query_id": gene_id,
                "symbol": result.get("symbol", ""),
                "name": result.get("name", ""),
                "taxid": result.get("taxid", ""),
                "tair_id": result.get("tair", ""),
                "ncbi_gene_id": result.get("entrezgene", ""),
                "summary": result.get("summary", "No summary available"),
            }
            return json.dumps(
                {"status": "success", "action": "annotation", "source": "MyGene/TAIR", "results": parsed_data},
                ensure_ascii=False)


        elif action == "expression":
            url = f"https://www.ebi.ac.uk/ebisearch/ws/rest/atlas-experiments?query={safe_id}&format=json&fields=name,species"

            res = mcp_request("GET", url, timeout=15)

            res.raise_for_status()

            entries = res.json().get("entries", [])

            if not entries:
                return json.dumps(
                    {"status": "success", "message": f"No EBI Expression Atlas datasets found for '{gene_id}'."})

            results = [{"experiment_id": item.get("id", ""),

                        "name": item.get("fields", {}).get("name", [""])[0],

                        "species": item.get("fields", {}).get("species", [""])[0],

                        "url": f"https://www.ebi.ac.uk/gxa/experiments/{item.get('id')}"} for item in entries[:5]]

            return json.dumps(
                {"status": "success", "action": "expression", "source": "EBI Expression Atlas", "datasets": results},
                ensure_ascii=False)


        else:

            return json.dumps({"status": "error", "message": "Invalid action. Must be 'annotation' or 'expression'."})

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})




@simple_retry(max_attempts=2, delay=1)
def search_jaspar_motifs(query: str, tax_group: Literal["plants", "vertebrates", "insects", "nematodes", "fungi", "urochordates"] = "plants") -> str:
    logger.info(f"Task: JASPAR Motif Search | Query: '{query}'")
    try:
        clean_query = query.strip().split()[0]
        # 回退到官方最标准的基础查询参数，避免高级参数造成的空集
        url = f"https://jaspar.elixir.no/api/v1/matrix/?search={urllib.parse.quote(clean_query)}&tax_group={tax_group}"

        headers = {"Accept": "application/json"}
        res = mcp_request("GET", url, headers=headers, timeout=15)

        if "application/json" not in res.headers.get("Content-Type", "").lower():
            return json.dumps({"status": "error", "message": "JASPAR server returned non-JSON response."})

        res.raise_for_status()
        data = res.json()

        if not data.get("results"):
            return json.dumps({"status": "success", "message": f"No motifs found for '{clean_query}' in {tax_group}."})

        results = []
        for item in data.get("results", []):
            matrix_id = item.get("matrix_id")
            results.append({
                "matrix_id": matrix_id,
                "name": item.get("name"),
                "base_url": f"https://jaspar.elixir.no/matrix/{matrix_id}/",
                "sequence_logo_url": f"https://jaspar.elixir.no/static/logos/all/svg/{matrix_id}.svg"
            })

        return json.dumps({"status": "success", "source": "JASPAR", "results": results}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})






@simple_retry(max_attempts=2, delay=1)
def query_ensembl_database(symbol: str, action: Literal["lookup", "homology"] = "lookup",
                           species: str = "arabidopsis_thaliana", target_species: str = "oryza_sativa") -> str:
    safe_species = species.strip().lower().replace(" ", "_")
    logger.info(f"Task: Ensembl Query | Action: '{action}' | Symbol: '{symbol}' | Species: '{safe_species}'")

    try:
        if action == "lookup":
            url = f"https://rest.ensembl.org/lookup/symbol/{safe_species}/{symbol}?expand=1"
            res = mcp_request("GET", url, headers={"Content-Type": "application/json"}, timeout=15)

            if res.status_code == 400:
                return json.dumps({"status": "error",
                                   "message": f"HTTP 400: Gene '{symbol}' not found in '{safe_species}'. Ensure exact canonical symbol."})
            res.raise_for_status()

            data = res.json()
            synonyms_str = ", ".join(data.get("synonyms", [])) or "None"
            result = {
                "id": data.get("id"), "display_name": data.get("display_name"), "synonyms": synonyms_str,
                "species": data.get("species"), "biotype": data.get("biotype"), "description": data.get("description"),
                "location": f"{data.get('seq_region_name')}:{data.get('start')}-{data.get('end')}",
                "url": f"https://plants.ensembl.org/{safe_species}/Gene/Summary?g={data.get('id')}"
            }
            return json.dumps({"status": "success", "action": "lookup", "results": [result]}, ensure_ascii=False)

        elif action == "homology":
            safe_target = target_species.strip().lower().replace(" ", "_")
            url = f"https://rest.ensembl.org/homology/symbol/{safe_species}/{symbol}?target_species={safe_target}&sequence=none"
            res = mcp_request("GET", url, headers={"Content-Type": "application/json"}, timeout=15)

            if res.status_code == 400:
                return json.dumps(
                    {"status": "error", "message": f"HTTP 400: Gene '{symbol}' not found in '{safe_species}'."})
            res.raise_for_status()

            results = []
            for item in res.json().get("data", []):
                for h in item.get("homologies", []):
                    target = h.get("target", {})
                    results.append({
                        "homology_type": h.get("type", ""), "target_species": target.get("species", ""),
                        "target_gene_id": target.get("id", ""),
                        "query_identity_percent": h.get("source", {}).get("perc_id", 0),
                        "target_identity_percent": target.get("perc_id", 0)
                    })

            if not results: return json.dumps(
                {"status": "success", "message": f"No orthologs found in {target_species}."})
            results = sorted(results, key=lambda x: x["target_identity_percent"], reverse=True)[:10]
            return json.dumps({"status": "success", "action": "homology", "homologs": results}, ensure_ascii=False)

        else:
            return json.dumps({"status": "error", "message": "Invalid action. Must be 'lookup' or 'homology'."})

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

if __name__ == "__main__":
    import sys
    import time

    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        logger.info("Initializing Comprehensive MCP Tool Test Suite...")

        # Define standard academic test parameters for every registered tool
        test_suite = [
            (search_academic_literature, {"query": "CRISPR", "max_results": 1, "source": "openalex"}),
            (traverse_citation_graph, {"doi": "10.1038/nature11577", "direction": "references", "max_results": 1}),
            (fetch_open_access_pdf, {"doi": "10.1038/nature11577"}),
            (search_omics_datasets, {"query": "breast cancer", "db_type": "geo", "max_results": 1}),
            (fetch_sequence_fasta, {"accession_id": "NM_001322051", "db_type": "nuccore"}),
            (fetch_taxonomy_info, {"organism_name": "Arabidopsis thaliana"}),
            (search_gbif_occurrences, {"scientific_name": "Panthera leo", "limit": 1}),
            (universal_ncbi_summary, {"database": "gene", "query": "BRCA1", "max_results": 1}),
            (fetch_webpage_content, {"url": "https://en.wikipedia.org/wiki/Bioinformatics"}),
            (search_web, {"query": "latest advancements in structural biology", "max_results": 1}),
            (search_preprints, {"query": "COVID-19", "max_results": 1}),
            (fetch_wikipedia_summary, {"query": "Gene expression"}),
            (search_github_repos, {"query": "single cell RNA seq pipeline", "max_results": 1}),

            # --- Updated & Merged Tools ---
            (query_ensembl_database, {"symbol": "BRCA1", "action": "lookup", "species": "homo_sapiens"}),
            (query_ensembl_database, {"symbol": "FT", "action": "homology", "species": "arabidopsis_thaliana",
                                      "target_species": "oryza_sativa"}),
            (query_metabolite_database, {"query": "Quercetin", "database": "pubchem"}),
            (query_metabolite_database, {"query": "Quercetin", "database": "chebi"}),
            (analyze_systems_network,
             {"identifiers": "CRY1", "action": "interactions", "species_id": 3702, "limit": 1}),
            (analyze_systems_network,
             {"identifiers": "CRY1,PHYA", "action": "enrichment", "organism": "athaliana", "limit": 1}),
            (query_plant_multiomics, {"gene_id": "AT1G01010", "action": "annotation"}),
            (query_plant_multiomics, {"gene_id": "AT1G01010", "action": "expression"}),
            # ------------------------------

            (query_kegg_database, {"query": "glycolysis", "action": "search_pathway", "organism_code": "ath"}),
            (query_kegg_database, {"query": "map00010", "action": "get_record"}),            (fetch_go_annotations, {"uniprot_id": "P04637", "limit": 1}),
            (search_chembl_target, {"query": "EGFR", "max_results": 1}),
            (query_uniprot_database, {"query": "P04637", "db_type": "uniprotkb", "max_results": 1}),
            (fetch_alphafold_structure, {"uniprot_id": "P04637"}),
            (query_pdb_structure, {"query": "1U3C", "action": "details"}),
            (search_jaspar_motifs, {"query": "MADS", "tax_group": "plants"}),
            (uniprot_id_mapping, {"from_db": "UniProtKB_AC-ID", "to_db": "Ensembl", "ids": "P04637"})
        ]

        success_count = 0

        print("\n" + "=" * 60)
        print("🧬 SCHOLAR NAVIS - SYSTEMATIC API INTEGRITY TEST")
        print("=" * 60 + "\n")

        for func, kwargs in test_suite:
            func_name = func.__name__
            print(f"[TESTING] Executing {func_name} ...")
            try:
                # Direct pythonic invocation of the wrapped tool
                result = func(**kwargs)

                # Check if the returned JSON string contains an explicit error status
                if '"status": "error"' in result or '"status": "timeout"' in result:
                    print(f"⚠️  WARNING: {func_name} executed, but returned an API error state.")
                    print(f"    RESPONSE: {str(result)}\n")
                else:
                    print(f"✅ SUCCESS: {func_name}")
                    print(f"    OUTPUT (Truncated): {str(result)[:150]}...\n")
                    success_count += 1

            except Exception as e:
                print(f"❌ CRITICAL FAILURE: {func_name}")
                print(f"    EXCEPTION: {str(e)}\n")

            # Strictly enforce a delay to respect external academic API rate limits (e.g., NCBI, EBI)
            time.sleep(1.5)

        print("*" * 60)
        print(f"TEST RUN COMPLETED: {success_count} / {len(test_suite)} API endpoints responded successfully.")
        print("*" * 60 + "\n")
        sys.exit(0)

    else:
        print("\n" + "=" * 60)
        print("ℹ️  SCHOLAR NAVIS ACADEMIC AGENT")
        print("=" * 60)
        print("This module now operates as a Zero-Latency Native Skill pool.")
        print("It is dynamically loaded into memory by the SkillManager.")
        print("The legacy FastMCP stdio server has been deprecated for internal tools.")
        print("\nTo run the API integrity test suite, execute:")
        print("    python academic_agent.py --test\n")