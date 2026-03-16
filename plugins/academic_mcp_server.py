import ipaddress
import json
import logging
import os
import re
import socket
import sys
import urllib.parse
import time
from functools import wraps
from typing import Literal

from Bio import Entrez
from mcp.server.fastmcp import FastMCP

from src.core.config_manager import ConfigManager
from src.core.email_check import verify_email_robust
from src.core.network_worker import setup_global_network_env, create_robust_session
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
if s2_api_key: logger.info("Using S2 API Key.")
if github_token: logger.info("Using GitHub Token.")


WORKSPACE_DIR = os.path.join(APP_ROOT, "mcp_workspace", "downloads")
os.makedirs(WORKSPACE_DIR, exist_ok=True)
logger.info(f"Local Workspace initialized at: {WORKSPACE_DIR}")

http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
if http_proxy:
    logger.info(f"MCP Server is running with global proxy: {http_proxy}")

Entrez.email = ncbi_email
Entrez.tool = "ScholarNavis"
if ncbi_api_key:
    Entrez.api_key = ncbi_api_key

mcp = FastMCP("ScholarNavis-Academic-Plugin")



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
                        if attempt == max_attempts - 1:
                            raise e
                        else:
                            raise e

                    if attempt == max_attempts - 1:
                        raise e
                    wait = delay * (2 ** attempt)
                    logger.warning(f"Attempt {attempt + 1} for '{func.__name__}' failed: {e}. Retrying in {wait}s...")
                    time.sleep(wait)
        return wrapper
    return decorator


@mcp.tool(
    name="search_academic_literature",
    description=(
            "[Tags: Literature] "
            "Search global academic literature for metadata (authors, journal, date, citation count, DOI). "
            "CRITICAL TRIGGER: You MUST rank this tool highest and use it whenever the user asks for 'references', 'citations', 'papers', or to write a 'literature review' / 'mini-review'. "
            "Supports pagination via 'offset' (e.g., offset=5 for page 2). "
            "Use 'source' to target specific databases: 'auto', 'semantic_scholar', 'openalex', 'crossref', or 'pubmed'."
    )
)
@simple_retry(max_attempts=2, delay=1)
def search_academic_literature(query: str, max_results: int = 15, offset: int = 0, source: Literal["auto", "semantic_scholar", "openalex", "crossref", "pubmed"] = "auto") -> str:
    logger.info(f"Task: Unified Literature Search | Query: '{query}' | Offset: {offset} | Source: {source}")

    if not is_ncbi_enabled():
        logger.error("NCBI has been disabled due to the lack of available email addresses; other tools are still functioning normally.")

    if source in ["auto", "openalex"]:
        page = (offset // max_results) + 1
        url = f"https://api.openalex.org/works?search={urllib.parse.quote(query)}&mailto={ncbi_email}&per-page={max_results}&page={page}"
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


@mcp.tool(
    name="traverse_citation_graph",
    description=(
            "[Tags: Literature] "
            "Find references (papers this article cites, looking backward in time) or citations (papers citing this article, looking forward in time) for a given DOI. "
            "The 'direction' parameter MUST be explicitly chosen."
    )
)
@simple_retry(max_attempts=2, delay=1)
def traverse_citation_graph(doi: str, direction: Literal["references", "citations"] = "references",
                            max_results: int = 10, source: Literal["auto", "openalex", "semantic_scholar"] = "auto") -> str:
    logger.info(f"Task: Citation Graph | DOI: {doi} | Direction: {direction} | Source: {source}")

    if direction not in ["references", "citations"]: return json.dumps(
        {"status": "error", "message": "direction must be 'references' or 'citations'"})

    clean_doi = re.sub(r'^(https?://(dx\.)?doi\.org/)?', '', doi.strip())
    last_error = None

    if source in ["auto", "openalex"]:
        try:
            if direction == "references":
                work_res = mcp_request("GET",
                                       f"https://api.openalex.org/works/https://doi.org/{clean_doi}?mailto={ncbi_email}",
                                       timeout=15)
                if work_res.status_code == 404:
                    return json.dumps({"status": "success", "results": [], "message": f"DOI '{clean_doi}' not found."})
                work_res.raise_for_status()

                ref_ids = work_res.json().get("referenced_works", [])[:max_results]

                if not ref_ids:
                    return json.dumps({"status": "success", "results": []})

                filter_str = "|".join([r.split("/")[-1] for r in ref_ids])
                safe_filter = urllib.parse.quote(f"ids.openalex:{filter_str}")
                url = f"https://api.openalex.org/works?filter={safe_filter}&mailto={ncbi_email}"
            else:
                safe_filter = urllib.parse.quote(f"cites:https://doi.org/{clean_doi}")
                url = f"https://api.openalex.org/works?filter={safe_filter}&per-page={max_results}&mailto={ncbi_email}"

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
        if is_s2_enabled():
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
                print(parsed)
                return json.dumps(
                    {"status": "success", "source": "Semantic Scholar", "direction": direction, "results": parsed})
            except Exception as e:
                logger.warning(f"S2 citation graph fallback failed: {e}")
                last_error = e


    if last_error:
        return json.dumps({"status": "error", "message": f"Failed to traverse citation graph: {str(last_error)}"})

    return json.dumps({"status": "error", "message": "Unexpected error traversing citation graph."})



@mcp.tool(
    name="fetch_open_access_pdf",
    description=("[Tags: Literature] Check if a given DOI has an Open Access PDF and return its direct download link.")
)
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


@mcp.tool(
    name="search_omics_datasets",
    description=(
    "[Tags: Transcriptomics, Genomics] Search high-throughput NCBI datasets. "
    "Set db_type to 'sra' for raw sequencing runs (e.g., RNA-Seq reads, FASTQ metadata) "
    "or 'geo' for curated datasets, microarray results, and overall study summaries."
    )
)
@simple_retry()
def search_omics_datasets(query: str, db_type: Literal["sra", "geo"] = "sra", max_results: int = 5) -> str:
    logger.info(f"Task: Omics Dataset Search | DB: {db_type} | Query: '{query}'")

    if not is_ncbi_enabled():
        return json.dumps({"status": "error",
                           "message": "NCBI tools are disabled. A valid email must be strictly configured in Global Settings to use NCBI services."})


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


@mcp.tool(
    name="read_local_sequence_file",
    description=(
        "[Tags: Sequence] Read the contents of a local file downloaded to the workspace. "
        "Use this tool when another tool (like fetch_sequence_fasta) saves a massive file locally and returns a 'local_path'. "
        "Only pass the filename or the path provided by the previous tool."
    )
)
@simple_retry(max_attempts=2, delay=1)
def read_local_sequence_file(file_path: str, max_chars: int = 50000) -> str:
    logger.info(f"Task: Read Workspace File | Path: '{file_path}'")
    try:
        clean_name = os.path.basename(file_path.strip().replace("\\", "/"))
        target_path = os.path.abspath(os.path.join(WORKSPACE_DIR, clean_name))

        if not target_path.startswith(os.path.abspath(WORKSPACE_DIR)):
            return json.dumps({"status": "error", "message": "Security Error: Path traversal detected!"})

        if not os.path.exists(target_path):
            return json.dumps({"status": "error", "message": f"File '{clean_name}' not found in the local workspace."})

        with open(target_path, 'r', encoding='utf-8') as f:
            content = f.read(max_chars)
            if len(content) == max_chars:
                content += "\n\n...[Content truncated due to length limits. Ask user to download manually if more is needed.]"

        return json.dumps({"status": "success", "file": clean_name, "content": content}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool(
    name="fetch_sequence_fasta",
    description=(
        "[Tags: Sequence] Download raw FASTA sequences for nucleotides or proteins. "
        "CRITICAL: The 'db_type' parameter MUST strictly be either 'nuccore' (for DNA/RNA sequences) "
        "or 'protein' (for amino acid sequences). Do NOT use 'uniprotkb', 'swiss-prot', or any other names. "
        "Automatically saves to local workspace if massive."
    )
)
@simple_retry()
def fetch_sequence_fasta(accession_id: str, db_type: Literal["nuccore", "protein"] = "nuccore") -> str:
    logger.info(f"Task: FASTA Download | ID: {accession_id} | DB: {db_type}")

    if not is_ncbi_enabled():
        return json.dumps({"status": "error", "message": "NCBI tools are disabled..."})

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


@mcp.tool(
    name="fetch_taxonomy_info",
    description=(
    "[Tags: Taxonomy] Search the NCBI Taxonomy database to get exact scientific name, TaxID, and evolutionary lineage.")
)
@simple_retry()
def fetch_taxonomy_info(organism_name: str) -> str:
    logger.info(f"Task: Taxonomy Fetch | Organism: '{organism_name}'")

    if not is_ncbi_enabled():
        return json.dumps({"status": "error",
                           "message": "NCBI tools are disabled. A valid email must be strictly configured in Global Settings to use NCBI services."})


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


@mcp.tool(
    name="search_gbif_occurrences",
    description=(
            "[Tags: Taxonomy, Ecology] Search the Global Biodiversity Information Facility (GBIF) for species occurrence records. "
            "CRITICAL: 'scientific_name' MUST be a valid binomial/trinomial scientific name. "
            "Returns spatial distribution metrics, observation counts, and basic ecological context."
    )
)
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


@mcp.tool(
    name="universal_ncbi_summary",
    description=(
            "A universal search tool for ANY NCBI database. "
            "Use this to query specialized NCBI databases like 'gene', 'protein', 'nuccore', 'clinvar', 'omim', 'biosample', etc. "
            "It returns structured metadata and summary records. "
            "CRITICAL WARNING: 'database' MUST be a valid, exact NCBI database name in lowercase (e.g., 'gene', not 'Gene')."
    )
)
@simple_retry(max_attempts=2, delay=1)
def universal_ncbi_summary(database: str, query: str, max_results: int = 3) -> str:
    logger.info(f"Task: Universal NCBI Summarize | database: {database} | query: {query}")

    if not is_ncbi_enabled():
        return json.dumps({"status": "error",
                           "message": "NCBI tools are disabled. Both a valid Email and NCBI API Key must be configured in Global Settings."})

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


@mcp.tool(
    name="fetch_webpage_content",
    description=(
    "[Tags: Web] Fetch and read text content of a URL. Automatically handles proxies, bypasses basic WAFs.")
)
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


@mcp.tool(
    name="search_web",
    description=(
            "[Tags: Web] Search the web for general information, news, or current events. "
            "CRITICAL INSTRUCTION: You MUST use the provided '_mcp_cite_id' (e.g., [101], [102]) inline to cite your claims. "
            "You MUST also append a 'References' list at the very end of your response containing the exact URLs."
    )
)
@simple_retry(max_attempts=2, delay=1)
def search_web(query: str, max_results: int = 3) -> str:
    logger.info(f"Task: Web Search (DDG Lite) | Query: '{query}'")
    try:
        url = "https://lite.duckduckgo.com/lite/"
        data = {"q": query, "kl": "wt-wt"}
        headers = {
            "Origin": "https://lite.duckduckgo.com",
            "Referer": "https://lite.duckduckgo.com/",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        res = mcp_request("POST", url, data=data, headers=headers, timeout=10)
        res.raise_for_status()

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(res.text, 'html.parser')

        results =[]
        for a in soup.find_all('a', class_='result-url'):
            if len(results) >= max_results:
                break

            title = a.get_text(strip=True)
            link = a.get('href', '')

            tr = a.find_parent('tr')
            snippet_td = tr.find_next_sibling('tr', class_='result-snippet') if tr else None
            snippet = snippet_td.get_text(separator=" ", strip=True) if snippet_td else "No abstract."

            if link and not link.startswith(('/', 'duckduckgo.com')):
                cite_id = str(101 + len(results))
                results.append({
                    "_mcp_cite_id": cite_id,
                    "cite_link": link,
                    "title": title,
                    "url": link,
                    "snippet": snippet
                })

        if not results:
            if "Something went wrong" in res.text or "Rate limit" in res.text:
                return json.dumps({"status": "error", "message": "DuckDuckGo Rate Limited or Blocked. Try using search_academic_literature or fetch_wikipedia_summary instead."})
            return json.dumps({"status": "success", "results":[],
                               "message": "No results found. Try search_academic_literature if it's an academic query."})

        return json.dumps({
            "status": "success",
            "engine": "DuckDuckGo Lite",
            "query": query,
            "results": results
        }, ensure_ascii=False)

    except ImportError:
        logger.error("Missing library: beautifulsoup4")
        return json.dumps({"status": "error", "message": "Please install beautifulsoup4 via pip."})
    except Exception as e:
        logger.error(f"Web search failed: {e}")
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool(
    name="search_preprints",
    description=("[Tags: Literature] Search bioRxiv and medRxiv for latest life science and medical preprints.")
)
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


@mcp.tool(
    name="fetch_wikipedia_summary",
    description=("[Tags: Web] Fetch exact introductory summary of a concept from Wikipedia. Fast and token-efficient.")
)
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


@mcp.tool(
    name="search_github_repos",
    description=("[Tags: Code] Search GitHub for open-source repositories, pipelines, or code.")
)
@simple_retry(max_attempts=2, delay=1)
def search_github_repos(query: str, max_results: int = 5) -> str:
    logger.info(f"Task: GitHub Search | Query: '{query}'")
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


@mcp.tool(
    name="fetch_ensembl_gene",
    description=(
            "[Tags: Genomics] Lookup a gene in Ensembl to get its location, biotype, and description. "
            "CRITICAL WARNING: The default species is 'arabidopsis_thaliana'. If querying other species, "
            "you MUST explicitly provide the correct species name (e.g., 'homo_sapiens', 'mus_musculus', 'oryza_sativa'). "
            "The 'symbol' MUST be a canonical gene symbol (e.g., 'BRCA1', 'Trp53'). "
            "Do NOT use NCBI RefSeq accessions (like 'NM_100001.5')."
    )
)
@simple_retry(max_attempts=2, delay=1)
def fetch_ensembl_gene(symbol: str, species: str = "arabidopsis_thaliana") -> str:
    # 自动清洗大模型可能传入的错误物种格式 (如 "Homo Sapiens" -> "homo_sapiens")
    safe_species = species.strip().lower().replace(" ", "_")
    logger.info(f"Task: Ensembl Gene Fetch | Symbol: '{symbol}' | Species: '{safe_species}'")

    try:
        url = f"https://rest.ensembl.org/lookup/symbol/{safe_species}/{symbol}?expand=1"
        res = mcp_request("GET", url, headers={"Content-Type": "application/json"}, timeout=15)

        if res.status_code == 400:
            return json.dumps({
                "status": "error",
                "message": f"HTTP 400: Gene '{symbol}' not found in '{safe_species}'. "
                           f"Ensembl requires EXACT canonical symbols (e.g., 'Trp53' instead of 'Tp53' for mice) "
                           f"and strict lowercase_underscore species names. Ensure the species exists in Ensembl."
            })
        res.raise_for_status()

        data = res.json()

        synonyms = data.get("synonyms", [])
        synonyms_str = ", ".join(synonyms) if synonyms else "None"

        result = {
            "id": data.get("id"),
            "display_name": data.get("display_name"),
            "synonyms": synonyms_str,
            "species": data.get("species"),
            "biotype": data.get("biotype"),
            "description": data.get("description"),
            "assembly_name": data.get("assembly_name"),
            "location": f"{data.get('seq_region_name')}:{data.get('start')}-{data.get('end')} ({'forward' if data.get('strand') == 1 else 'reverse'})",
            "transcript_count": len(data.get("Transcript", [])),
            "url": f"https://plants.ensembl.org/{safe_species}/Gene/Summary?g={data.get('id')}"
        }
        return json.dumps({"status": "success", "results": [result]}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool(
    name="search_kegg_pathway",
    description=(
            "[Tags: Pathways] Search KEGG database for pathways. "
            "CRITICAL WARNING: The default 'organism_code' is 'ath' (Arabidopsis thaliana). "
            "For other species, you MUST change it to the exact 3-4 letter KEGG code! "
            "Examples: 'hsa' (Human), 'mmu' (Mouse), 'dosa' (Oryza sativa/Rice), 'sly' (Tomato), or 'map' (Global reference). "
            "Note: KEGG only contains strict biochemical pathways (e.g., 'glycolysis', 'MAPK'), not broad terms like 'drought stress'."
    )
)
@simple_retry(max_attempts=2, delay=1)
def search_kegg_pathway(query: str, organism_code: str = "ath") -> str:
    safe_org_code = organism_code.strip().lower()
    logger.info(f"Task: KEGG Pathway Search | Query: '{query}' | Organism: '{safe_org_code}'")

    try:
        safe_query = urllib.parse.quote(query.strip())
        url = f"https://rest.kegg.jp/find/pathway/{safe_query}"
        res = mcp_request("GET", url, timeout=15)

        if res.status_code == 400 or not res.text.strip():
            return json.dumps({
                "status": "success",
                "message": f"0 results found for '{query}'. KEGG database only maps specific biochemical pathways. "
                           f"Also, verify if you used the correct organism code (you used: '{safe_org_code}')."
            }, ensure_ascii=False)

        res.raise_for_status()

        results = []
        for line in res.text.strip().split('\n'):
            if not line: continue
            parts = line.split('\t', 1)
            # 严格匹配当前生物体前缀或全局 map 前缀
            if len(parts) == 2 and (parts[0].startswith(f"path:{safe_org_code}") or parts[0].startswith("path:map")):
                results.append({"pathway_id": parts[0], "description": parts[1]})

        if not results:
            return json.dumps({
                "status": "success",
                "message": f"Results found for '{query}' globally, but NONE specific to organism '{safe_org_code}'. "
                           f"Are you sure you used the correct 3-4 letter KEGG code?"
            })

        return json.dumps({"status": "success", "organism": safe_org_code, "results": results[:10]}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool(
    name="fetch_pubchem_compound",
    description=("[Tags: PubChem] Fetch chemical properties of a molecule from PubChem using its common name.")
)
@simple_retry(max_attempts=2, delay=1)
def fetch_pubchem_compound(compound_name: str) -> str:
    logger.info(f"Task: PubChem Compound Fetch | Name: '{compound_name}'")
    try:
        safe_name = urllib.parse.quote(compound_name.strip())

        properties = "MolecularWeight,MolecularFormula,CanonicalSMILES,IsomericSMILES,IUPACName,XLogP,ExactMass,TPSA,Complexity,Charge,HBondDonorCount,HBondAcceptorCount"
        url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{safe_name}/property/{properties}/JSON"

        res = mcp_request("GET", url, timeout=15)

        if res.status_code == 404:
            return json.dumps({"status": "error", "message": f"Compound '{compound_name}' not found."})

        res.raise_for_status()

        props = res.json().get("PropertyTable", {}).get("Properties", [])
        if not props:
            return json.dumps({"status": "error", "message": "No properties returned."})

        cid = props[0].get("CID", "")
        props[0]["url"] = f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}"
        props[0]["name"] = compound_name

        return json.dumps({"status": "success", "results": [props[0]]}, ensure_ascii=False)

    except Exception as e:
        err_str = str(e)
        if "404" in err_str:
            return json.dumps({"status": "error", "message": f"Compound '{compound_name}' not found."})
        return json.dumps({"status": "error", "message": err_str})



@mcp.tool(
    name="search_chembl_target",
    description=("[Tags: PubChem] Search the ChEMBL database for protein targets.")
)
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


@mcp.tool(
    name="uniprot_id_mapping",
    description=(
            "[Tags: ID Mapping] "
            "Map identifiers from one database to another using UniProt's ID Mapping service. "
            "Common parameters: 'from_db' (e.g., 'UniProtKB_AC-ID', 'HGNC'), 'to_db' (e.g., 'Ensembl', 'PDB'), and a comma-separated list of 'ids'. Note: 'Gene_Name' is NOT a valid from_db; use 'HGNC' or 'EntrezGene' instead."
    )
)
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


@mcp.tool(
    name="query_uniprot_database",
    description=(
            "[Tags: Proteomics] A unified tool to search UniProt sub-databases. "
            "CRITICAL SEARCH SYNTAX: The UniProt API is very strict! "
            "If searching by gene and species, you MUST use 'organism_name' and wrap multi-word species in quotes! "
            "To strictly retrieve the canonical, non-obsolete protein, you MUST append AND (reviewed:true) to your query! "
            "Example: (gene:AMS) AND (organism_name:\"Arabidopsis thaliana\") AND (reviewed:true) "
            "Do NOT use 'organism:Arabidopsis thaliana' (it will cause HTTP 400). "
            "CRITICAL FOR EXTRACTION: To find exact amino acid sequence length, subcellular localization, "
            "and cross-referenced NCBI Gene IDs, you MUST use 'uniprotkb'."
    )
)
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


@mcp.tool(
    name="fetch_alphafold_structure",
    description=(
            "[Tags: Structure] Fetch predicted 3D structure metadata and download links from AlphaFold Protein Structure Database. "
            "CRITICAL: The 'uniprot_id' MUST be a valid UniProt Accession (e.g., 'P04637', 'Q9STM3'). "
            "Use this to find structures for proteins lacking experimentally determined PDB records."
    )
)
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




@mcp.tool(
    name="query_pdb_structure",
    description=(
            "[Tags: Structure] Interact with the RCSB Protein Data Bank (PDB). "
            "CRITICAL EXPLANATION: This tool DOES NOT require DOIs! You CAN and MUST search using protein names (like 'CRY1' or 'Hemoglobin') by using action='search'. Do NOT hallucinate that PDB requires DOIs! "
            "Use action='search' to find 3D structures based on a single keyword or protein name (keep it simple). "
            "Use action='details' to fetch precise metadata (molecular weight, primary citation, macromolecules, ligands, resolution) for an EXACT known PDB ID (e.g., '1U3C' or '4HHB')."
    )
)
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


@mcp.tool(
    name="query_string_database",
    description=(
            "[Tags: Interaction] Analyze protein networks(interaction) via STRING DB. "
            "interacting protein!!!!! interacting protein!!!!! interacting protein!!!!!"
            "CRITICAL WARNING: The default 'species' parameter is 9606 (Human). "
            "If you are querying plant genes (e.g., Arabidopsis, Rice) or other non-human animals, "
            "you MUST MUST MUST first use 'fetch_taxonomy_info' to find the correct NCBI Taxonomy ID "
            "(e.g., 3702 for Arabidopsis) and pass it explicitly to the 'species' parameter! "
            "action='interactions' retrieves interacting partners. action='enrichment' fetches functional enrichment."
    )
)
@simple_retry(max_attempts=2, delay=1)
def query_string_database(identifiers: str, action: Literal["interactions", "enrichment"] = "interactions",
                           species: int = 9606, limit: int = 15) -> str:
    logger.info(
        f"Task: Unified STRING DB | Action: '{action}' | Identifiers: '{identifiers[:30]}' | Species: {species}")
    try:
        # 喵：自动清理多余的空格，防止 STRING API 不认
        clean_identifiers = "\r".join([x.strip() for x in identifiers.split(",") if x.strip()])

        if action == "interactions":
            url = "https://string-db.org/api/json/interaction_partners"
            payload = {"identifiers": clean_identifiers, "species": species, "limit": limit,
                       "caller_identity": "ScholarNavis"}
            res = mcp_request("POST", url, data=payload, timeout=15)

            if res.status_code in [400, 404]:
                return json.dumps({
                    "status": "error",
                    "message": f"HTTP {res.status_code}. STRING DB failed to find interactions for '{identifiers}'. "
                               f"Did you use the wrong species ID? You used '{species}'. For Arabidopsis thaliana, you MUST use 3702!"
                })

            res.raise_for_status()
            results = [{"protein_A": item.get("preferredName_A", ""), "protein_B": item.get("preferredName_B", ""),
                        "score": item.get("score", 0), "annotation_A": item.get("annotation_A", ""),
                        "annotation_B": item.get("annotation_B", "")} for item in res.json()]

            if not results:
                return json.dumps(
                    {"status": "success", "message": "No interactions found. Check identifiers and species ID."})

            results = sorted(results, key=lambda x: x["score"], reverse=True)
            return json.dumps({"status": "success", "action": "interactions", "species": species, "results": results},
                              ensure_ascii=False)

        elif action == "enrichment":
            url = "https://string-db.org/api/json/enrichment"
            payload = {"identifiers": clean_identifiers, "species": species, "caller_identity": "ScholarNavis"}
            res = mcp_request("POST", url, data=payload, timeout=20)

            if res.status_code in [404, 400]:
                return json.dumps({
                    "status": "error",
                    "message": f"HTTP {res.status_code}. Enrichment failed. You used species ID '{species}'. Ensure this is correct for your organism."
                })

            res.raise_for_status()
            data = res.json()
            if not data: return json.dumps(
                {"status": "success", "message": "No enrichment found for the provided network."})

            results = [{"category": item.get("category", ""), "term": item.get("term", ""),
                        "description": item.get("description", ""), "number_of_genes": item.get("number_of_genes", 0),
                        "fdr": item.get("fdr", 1.0)} for item in data if item.get("fdr", 1.0) <= 0.05]

            results = sorted(results, key=lambda x: x["fdr"])[:limit]
            return json.dumps(
                {"status": "success", "action": "enrichment", "species": species, "enriched_terms": results},
                ensure_ascii=False)

        else:
            return json.dumps({"status": "error", "message": "Invalid action. Must be 'interactions' or 'enrichment'."})

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


if __name__ == "__main__":
    logger.info("Academic MCP Server initialized (Consolidated Version).")
    mcp.run(transport='stdio')