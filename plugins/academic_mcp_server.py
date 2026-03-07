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

ncbi_email = os.environ.get("NCBI_API_EMAIL", "").strip()
ncbi_api_key = os.environ.get("NCBI_API_KEY", "").strip()
s2_api_key = os.environ.get("S2_API_KEY", "").strip()
github_token = os.environ.get("GITHUB_TOKEN", "").strip()

_EMAIL_VALID_CACHE = None
def is_ncbi_email_valid():
    global _EMAIL_VALID_CACHE
    if _EMAIL_VALID_CACHE is None:
        if ncbi_email:
            _EMAIL_VALID_CACHE = verify_email_robust(ncbi_email).get("is_valid", False)
        else:
            _EMAIL_VALID_CACHE = False

    if _EMAIL_VALID_CACHE:logger.info(f"Email: {ncbi_email[0:5]}... is valid.")
    else: logger.error(f"EMail: {ncbi_email[0:5]}... is invalid.")

    return _EMAIL_VALID_CACHE

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


# 建议修改 retry 装饰器的逻辑
def simple_retry(max_attempts=3, delay=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_attempts - 1: raise e
                    wait = delay * (2 ** attempt)
                    time.sleep(wait)
        return wrapper
    return decorator



@mcp.tool(
    name="search_academic_literature",
    description=(
            "[Tags: Literature] "
            "Search global academic literature for metadata (authors, journal, date, citation count, DOI). "
            "Supports pagination via 'offset' (e.g., offset=5 for page 2). "
            "Use 'source' to target specific databases: 'auto' (cascades automatically), 'semantic_scholar' (computer science/general), 'openalex' (broad/general), 'crossref' (DOI matching), or 'pubmed' (biomedical/life sciences)."
    )
)
@simple_retry(max_attempts=2, delay=1)
def search_academic_literature(query: str, max_results: int = 5, offset: int = 0, source: Literal["auto", "semantic_scholar", "openalex", "crossref", "pubmed"] = "auto") -> str:
    logger.info(f"Task: Unified Literature Search | Query: '{query}' | Offset: {offset} | Source: {source}")

    if not is_ncbi_email_valid():
        logger.error("NCBI has been disabled due to the lack of available email addresses; other tools are still functioning normally.")

    try:
        if s2_api_key and source in ["auto", "semantic_scholar"]:
            url = "https://api.semanticscholar.org/graph/v1/paper/search"
            params = {"query": query, "limit": max_results, "offset": offset,
                      "fields": "title,authors,year,abstract,citationCount,isOpenAccess,url,externalIds"}
            try:
                res = mcp_request("GET", url, params=params, headers={"x-api-key": s2_api_key}, timeout=15)
                res.raise_for_status()
                parsed = []
                for p in res.json().get("data", []):
                    if not isinstance(p, dict): continue
                    authors_raw = p.get("authors") or []
                    if not isinstance(authors_raw, list): authors_raw = []
                    ext_ids = p.get("externalIds")
                    parsed.append({"title": p.get("title", ""), "year": p.get("year", "Unknown"),
                                   "authors": [a.get("name", "") for a in authors_raw if isinstance(a, dict)][:5],
                                   "citation_count": p.get("citationCount", 0),
                                   "abstract": (p.get("abstract") or "No abstract")[:600] + "...",
                                   "doi": ext_ids.get("DOI", "") if isinstance(ext_ids, dict) else "",
                                   "url": p.get("url", ""),
                                   "source_db": "Semantic Scholar"})
                if parsed: return json.dumps({"status": "success", "source": "semantic_scholar", "results": parsed})
            except Exception as e:
                logger.warning(f"S2 search failed: {e}")

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
                        abstract_text = " ".join([w for pos_idx, w in words])[:600] + "..."

                    authors_raw = p.get("authorships") or []
                    if not isinstance(authors_raw, list): authors_raw = []
                    authors = [a.get("author", {}).get("display_name", "") for a in authors_raw if
                               isinstance(a, dict) and isinstance(a.get("author"), dict)][:5]

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
                                   "authors": authors[:5], "citation_count": p.get("is-referenced-by-count", 0),
                                   "abstract": p.get("abstract", "No abstract")[:600].replace("<jats:p>", "").replace(
                                       "</jats:p>", "") + "...",
                                   "doi": p.get("DOI", ""), "url": p.get("URL", ""),
                                   "source_db": "Crossref"})
                if parsed: return json.dumps({"status": "success", "source": "crossref", "results": parsed})
            except Exception as e:
                logger.warning(f"Crossref search failed: {e}")

        if source in ["auto", "pubmed"]:
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
                                   "authors": authors[:5], "abstract": "Fetch via fetch_pubmed_abstract.",
                                   "pmid": d.get("Id", ""),
                                   "doi": doi,
                                   "url": f"https://pubmed.ncbi.nlm.nih.gov/{d.get('Id', '')}/", "source_db": "PubMed"})
                return json.dumps({"status": "success", "source": "pubmed", "results": parsed})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool(
    name="traverse_citation_graph",
    description=(
            "[Tags: Literature] "
            "Find references (papers this article cites, looking backward in time) or citations (papers citing this article, looking forward in time) for a given DOI. "
            "The 'direction' parameter MUST be explicitly chosen."
    )
)
@simple_retry(max_attempts=2, delay=1)
def traverse_citation_graph(doi: str, direction: Literal["references", "citations"] = "references", max_results: int = 10) -> str:
    logger.info(f"Task: Citation Graph | DOI: {doi} | Direction: {direction}")
    if direction not in ["references", "citations"]: return json.dumps(
        {"status": "error", "message": "direction must be 'references' or 'citations'"})
    clean_doi = re.sub(r'^(https?://(dx\.)?doi\.org/)?', '', doi.strip())

    if s2_api_key:
        try:
            url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{clean_doi}/{direction}?fields=title,authors,year,citationCount,externalIds,url&limit={max_results}"
            res = mcp_request("GET", url, headers={"x-api-key": s2_api_key}, timeout=15)
            res.raise_for_status()
            parsed = []
            data_list = res.json().get("data")
            if isinstance(data_list, list):
                for item in data_list:
                    if not isinstance(item, dict): continue
                    p = item.get("citedPaper") if direction == "references" else item.get("citingPaper")
                    if not isinstance(p, dict) or not p.get("title"): continue

                    authors_raw = p.get("authors") or []
                    if not isinstance(authors_raw, list): authors_raw = []

                    ext_ids = p.get("externalIds")
                    doi = ext_ids.get("DOI", "") if isinstance(ext_ids, dict) else ""

                    parsed.append({
                        "title": p.get("title", ""), "year": p.get("year", "Unknown"),
                        "authors": [a.get("name", "") for a in authors_raw if isinstance(a, dict)][:3],
                        "citation_count": p.get("citationCount", 0),
                        "doi": doi,
                        "url": p.get("url", "")
                    })
            if parsed:
                return json.dumps(
                    {"status": "success", "source": "Semantic Scholar", "direction": direction, "results": parsed})
        except Exception as e:
            logger.warning(f"S2 citation graph failed: {e}")

    try:
        if direction == "references":
            work_res = mcp_request("GET",
                                   f"https://api.openalex.org/works/https://doi.org/{clean_doi}?mailto={ncbi_email}",
                                   timeout=15)
            if work_res.status_code == 404:
                return json.dumps({"status": "success", "results": [], "message": f"DOI '{clean_doi}' not found."})
            work_res.raise_for_status()
            ref_ids = work_res.json().get("referenced_works", [])[:max_results]
            if not ref_ids: return json.dumps({"status": "success", "results": []})
            filter_str = "|".join([r.split("/")[-1] for r in ref_ids])
            url = f"https://api.openalex.org/works?filter=openalex:{filter_str}&mailto={ncbi_email}"
        else:
            url = f"https://api.openalex.org/works?filter=cites:https://doi.org/{clean_doi}&per-page={max_results}&mailto={ncbi_email}"

        res = mcp_request("GET", url, timeout=15)
        res.raise_for_status()
        parsed = []
        for p in res.json().get("results", []):
            if not isinstance(p, dict): continue
            parsed.append({"title": p.get("title", ""), "year": p.get("publication_year", "Unknown"),
                           "citation_count": p.get("cited_by_count", 0),
                           "doi": p.get("doi", "").replace("https://doi.org/", "") if p.get("doi") else "",
                           "url": p.get("id", "")})
        return json.dumps({"status": "success", "source": "OpenAlex", "direction": direction, "results": parsed})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool(
    name="fetch_open_access_pdf",
    description=("[Tags: Literature] Check if a given DOI has an Open Access PDF and return its direct download link.")
)
@simple_retry()
def fetch_open_access_pdf(doi: str) -> str:
    logger.info(f"Task: Fetch OA PDF | DOI: '{doi}'")
    fetcher = OAFetcher()
    result = fetcher.fetch_best_oa_pdf(doi, ncbi_email, s2_api_key)
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

    if not is_ncbi_email_valid():
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
    name="fetch_sequence_fasta",
    description=(
        "[Tags: Genomics, Protein] Download raw FASTA sequences for nucleotides or proteins. "
        "CRITICAL: The 'db_type' parameter MUST strictly be either 'nuccore' (for DNA/RNA sequences) "
        "or 'protein' (for amino acid sequences). Do NOT use 'uniprotkb', 'swiss-prot', or any other names. "
        "Automatically saves to local workspace if massive."
    )
)
@simple_retry()
def fetch_sequence_fasta(accession_id: str, db_type: Literal["nuccore", "protein"] = "nuccore") -> str:
    logger.info(f"Task: FASTA Download | ID: {accession_id} | DB: {db_type}")

    if not is_ncbi_email_valid():
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

    if not is_ncbi_email_valid():
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
    name="fetch_pubmed_abstract",
    description=("[Tags: Literature] Fetch the full-text abstract of a specific PubMed article using its PMID.")
)
@simple_retry()
def fetch_pubmed_abstract(pmid: str) -> str:
    logger.info(f"Task: Fetch Abstract | PMID: {pmid}")

    if not is_ncbi_email_valid():
        return json.dumps({"status": "error",
                           "message": "NCBI tools are disabled. A valid email must be strictly configured in Global Settings to use NCBI services."})



    clean_pmid = str(pmid).strip()
    if clean_pmid.lower().startswith("pmid:"):
        clean_pmid = clean_pmid[5:].strip()
    elif clean_pmid.lower().startswith("pmid"):
        clean_pmid = clean_pmid[4:].strip()

    if not clean_pmid.isdigit():
        return json.dumps({
            "status": "error",
            "message": f"Invalid PMID: '{pmid}'. Must be a purely numeric PubMed ID (e.g., '31234567'). If you only have a DOI or title, use search_academic_literature first to find the correct PMID."
        })

    try:
        handle = Entrez.efetch(db="pubmed", id=clean_pmid, rettype="abstract", retmode="text")
        abstract_text = handle.read()
        handle.close()
        return json.dumps({"status": "success", "pmid": clean_pmid, "abstract": abstract_text.strip()})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@simple_retry()
def universal_ncbi_summary(database: str, query: str, max_results: int = 3) -> str:
    logger.info(f"Task: Universal NCBI Summarize | database: {database} | query: {query}")


    if not is_ncbi_email_valid():
        return json.dumps({"status": "error",
                           "message": "NCBI tools are disabled. A valid email must be strictly configured in Global Settings to use NCBI services."})


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
        text_content = re.sub(r'<script.*?>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        text_content = re.sub(r'<style.*?>.*?</style>', '', text_content, flags=re.DOTALL | re.IGNORECASE)
        text_content = re.sub(r'<[^>]+>', ' ', text_content)
        text_content = re.sub(r'\s+', ' ', text_content).strip()
        if len(text_content) > 30000: text_content = text_content[:30000] + "\n...[Content truncated]"
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
             "abstract": p.get("abstractText", "No abstract")[:600] + "...",
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
        res = mcp_request("GET", url, params=params, headers={"Accept": "application/vnd.github.v3+json"}, timeout=10)
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
        "CRITICAL: The 'symbol' MUST be a canonical gene symbol (e.g., 'BRCA1', 'Trp53'). "
        "Do NOT use NCBI RefSeq accessions (like 'NM_100001.5') or protein IDs here."
    )
)
@simple_retry(max_attempts=2, delay=1)
def fetch_ensembl_gene(symbol: str, species: str = "arabidopsis_thaliana") -> str:
    logger.info(f"Task: Ensembl Gene Fetch | Symbol: '{symbol}' | Species: '{species}'")
    try:
        url = f"https://rest.ensembl.org/lookup/symbol/{species}/{symbol}?expand=1"
        res = mcp_request("GET", url, headers={"Content-Type": "application/json"}, timeout=15)
        if res.status_code == 400:
            return json.dumps({"status": "error", "message": f"HTTP 400: Gene '{symbol}' not found in '{species}'. Ensembl requires EXACT canonical symbols (e.g., 'Trp53' instead of 'Tp53' for mice) and strict lowercase_underscore species names (e.g., 'mus_musculus')."})
        res.raise_for_status()
        data = res.json()
        result = {"id": data.get("id"), "display_name": data.get("display_name"), "species": data.get("species"),
                  "biotype": data.get("biotype"), "description": data.get("description"),
                  "assembly_name": data.get("assembly_name"),
                  "location": f"{data.get('seq_region_name')}:{data.get('start')}-{data.get('end')} ({'forward' if data.get('strand') == 1 else 'reverse'})",
                  "transcript_count": len(data.get("Transcript",[])),
                  "url": f"https://uswest.ensembl.org/{species}/Gene/Summary?g={data.get('id')}"}
        return json.dumps({"status": "success", "results": [result]}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool(
    name="search_kegg_pathway",
    description=(
    "[Tags: Pathways] Search KEGG database for pathways. Use organism codes like 'ath' (Arabidopsis), 'hsa' (Human), or 'map'. "
    "Note: KEGG only contains strict biochemical pathways (e.g., 'glycolysis', 'MAPK'), not broad physiological stress terms (like 'drought stress')."
    )
)
@simple_retry(max_attempts=2, delay=1)
def search_kegg_pathway(query: str, organism_code: str = "ath") -> str:
    logger.info(f"Task: KEGG Pathway Search | Query: '{query}' | Organism: '{organism_code}'")
    try:
        safe_query = urllib.parse.quote(query.strip())
        url = f"https://rest.kegg.jp/find/pathway/{safe_query}"
        res = mcp_request("GET", url, timeout=15)

        if res.status_code == 400 or not res.text.strip():
            return json.dumps({
                "status": "success",
                "message": f"0 results found for '{query}'. KEGG database only maps specific biochemical and signaling pathways. General physiological conditions (like 'drought stress') are not pathway names."
            }, ensure_ascii=False)

        res.raise_for_status()

        results = []
        for line in res.text.strip().split('\n'):
            if not line: continue
            parts = line.split('\t', 1)
            if len(parts) == 2 and (parts[0].startswith(f"path:{organism_code}") or parts[0].startswith("path:map")):
                results.append({"pathway_id": parts[0], "description": parts[1]})

        if not results:
            return json.dumps({
                "status": "success",
                "message": f"0 results found for '{query}'. KEGG database only maps specific biochemical and signaling pathways. General physiological conditions (like 'drought stress') are not pathway names."
            })

        return json.dumps({"status": "success", "organism": organism_code, "results": results[:10]}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool(
    name="fetch_pubchem_compound",
    description=("[Tags: Small Molecules] Fetch chemical properties of a molecule from PubChem using its common name.")
)
@simple_retry(max_attempts=2, delay=1)
def fetch_pubchem_compound(compound_name: str) -> str:
    logger.info(f"Task: PubChem Compound Fetch | Name: '{compound_name}'")
    try:
        safe_name = urllib.parse.quote(compound_name.strip())
        url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{safe_name}/property/MolecularWeight,MolecularFormula,CanonicalSMILES/JSON"
        res = mcp_request("GET", url, timeout=15)
        if res.status_code == 404: return json.dumps(
            {"status": "error", "message": f"Compound '{compound_name}' not found."})
        res.raise_for_status()
        properties = res.json().get("PropertyTable", {}).get("Properties", [])
        if not properties: return json.dumps({"status": "error", "message": "No properties returned."})
        cid = properties[0].get("CID", "")
        properties[0]["url"] = f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}"
        properties[0]["name"] = compound_name
        return json.dumps({"status": "success", "results": [properties[0]]}, ensure_ascii=False)
    except Exception as e:
        err_str = str(e)
        if "404" in err_str:
            return json.dumps({"status": "error", "message": f"Compound '{compound_name}' not found."})
        return json.dumps({"status": "error", "message": err_str})



@mcp.tool(
    name="search_chembl_target",
    description=("[Tags: Pharmacology] Search the ChEMBL database for protein targets.")
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
            "[Tags: Genomics, Proteomics] A unified tool to search UniProt sub-databases. "
            "You MUST select a valid 'db_type': "
            "'uniprotkb' (Detailed protein entries, search by gene/name/ID), "
            "'proteomes' (Species-level reference proteomes), "
            "'genecentric' (Canonical proteins grouped under a gene), "
            "'uniref' (Clustered sets), 'uniparc' (Non-redundant sequences), or 'unirule'/'arba'."
    )
)
@simple_retry(max_attempts=2, delay=1)
def query_uniprot_database(query: str, db_type: Literal["uniprotkb", "proteomes", "genecentric", "uniref", "uniparc", "unirule", "arba"] = "uniprotkb", max_results: int = 5) -> str:
    db_type = db_type.lower()
    logger.info(f"Task: Unified UniProt Search | DB: '{db_type}' | Query: '{query}'")
    try:
        # Branch 1: UniProtKB
        if db_type == "uniprotkb":
            if re.match(r"^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z]([0-9][A-Z][A-Z0-9]{2}){1,2}[0-9])(-\d+)?$",
                        query.upper()):
                res = mcp_request("GET", f"https://rest.uniprot.org/uniprotkb/{query.upper()}", timeout=15)
                res.raise_for_status()
                data = res.json()
                gene_name = data["genes"][0]["geneName"].get("value", "Unknown") if data.get("genes") and data["genes"][
                    0].get("geneName") else "Unknown"
                return json.dumps({"status": "success", "results": [{
                    "accession": data.get("primaryAccession"), "proteinExistence": data.get("proteinExistence"),
                    "proteinName": data.get("proteinDescription", {}).get("recommendedName", {}).get("fullName",
                                                                                                     {}).get("value",
                                                                                                             "Unknown"),
                    "gene": gene_name, "organism": data.get("organism", {}).get("scientificName", ""),
                    "sequence_length": data.get("sequence", {}).get("length", 0)
                }]}, ensure_ascii=False)

            res = mcp_request("GET", "https://rest.uniprot.org/uniprotkb/search",
                              params={"query": query, "size": max_results}, timeout=15)
            res.raise_for_status()
            results = []
            for item in res.json().get("results", []):
                rec_name = item.get("proteinDescription", {}).get("recommendedName", {}).get("fullName", {}).get(
                    "value", "")
                if not rec_name:
                    subs = item.get("proteinDescription", {}).get("submissionNames", [])
                    rec_name = subs[0].get("fullName", {}).get("value", "Unknown") if subs else "Unknown"
                gene_name = item["genes"][0]["geneName"].get("value", "Unknown") if item.get("genes") and item["genes"][
                    0].get("geneName") else "Unknown"
                results.append(
                    {"accession": item.get("primaryAccession", ""), "proteinName": rec_name, "gene": gene_name,
                     "organism": item.get("organism", {}).get("scientificName", ""),
                     "sequence_length": item.get("sequence", {}).get("length", 0)})
            return json.dumps({"status": "success", "db": "uniprotkb", "results": results}, ensure_ascii=False)

        # Branch 2: Proteomes
        elif db_type == "proteomes":
            if re.match(r"^UP[0-9]{9}$", query.upper()):
                res = mcp_request("GET", f"https://rest.uniprot.org/proteomes/{query.upper()}", timeout=15)
                res.raise_for_status()
                p = res.json()
                return json.dumps({"status": "success", "results": [
                    {"id": p.get("id"), "taxonomy": p.get("taxonomy", {}).get("scientificName"),
                     "proteomeType": p.get("proteomeType"), "proteinCount": p.get("proteinCount")}]},
                                  ensure_ascii=False)
            res = mcp_request("GET", "https://rest.uniprot.org/proteomes/search",
                              params={"query": query, "size": max_results}, timeout=15)
            res.raise_for_status()
            results = [{"id": p.get("id", ""), "taxonomy": p.get("taxonomy", {}).get("scientificName", ""),
                        "proteomeType": p.get("proteomeType", ""), "proteinCount": p.get("proteinCount", 0)} for p in
                       res.json().get("results", [])]
            return json.dumps({"status": "success", "db": "proteomes", "results": results}, ensure_ascii=False)

        # Branch 3: GeneCentric
        elif db_type == "genecentric":
            res = mcp_request("GET", "https://rest.uniprot.org/genecentric/search",
                              params={"query": query, "size": max_results}, timeout=15)
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
            if re.match(r"^UniRef(100|90|50)_[A-Z0-9]+$", query):
                res = mcp_request("GET", f"https://rest.uniprot.org/uniref/{query}", timeout=15)
                res.raise_for_status()
                data = res.json()
                return json.dumps({"status": "success", "results": [
                    {"id": data.get("id"), "name": data.get("name"), "memberCount": data.get("memberCount"),
                     "commonTaxon": data.get("commonTaxon", {}).get("scientificName"),
                     "representative_accession": data.get("representativeMember", {}).get("memberId")}]},
                                  ensure_ascii=False)
            res = mcp_request("GET", "https://rest.uniprot.org/uniref/search",
                              params={"query": query, "size": max_results}, timeout=15)
            res.raise_for_status()
            results = [
                {"id": item.get("id", ""), "name": item.get("name", ""), "memberCount": item.get("memberCount", 0),
                 "commonTaxon": item.get("commonTaxon", {}).get("scientificName", ""),
                 "representative_accession": item.get("representativeMember", {}).get("memberId", "")} for item in
                res.json().get("results", [])]
            return json.dumps({"status": "success", "db": "uniref", "results": results}, ensure_ascii=False)

        # Branch 5: UniParc
        elif db_type == "uniparc":
            if re.match(r"^UPI[A-F0-9]{10}$", query.upper()):
                res = mcp_request("GET", f"https://rest.uniprot.org/uniparc/{query.upper()}", timeout=15)
                res.raise_for_status()
                data = res.json()
                return json.dumps({"status": "success", "results": [
                    {"upi": data.get("uniParcId"), "sequence_length": data.get("sequence", {}).get("length"),
                     "most_recent_cross_ref": data.get("mostRecentCrossRefUpdated")}]}, ensure_ascii=False)
            res = mcp_request("GET", "https://rest.uniprot.org/uniparc/search",
                              params={"query": query, "size": max_results}, timeout=15)
            res.raise_for_status()
            results = [{"upi": item.get("uniParcId", ""), "sequence_length": item.get("sequence", {}).get("length", 0),
                        "most_recent_cross_ref": item.get("mostRecentCrossRefUpdated", "")} for item in
                       res.json().get("results", [])]
            return json.dumps({"status": "success", "db": "uniparc", "results": results}, ensure_ascii=False)

        # Branch 6: Annotations (unirule/arba)
        elif db_type in ["unirule", "arba"]:
            res = mcp_request("GET", f"https://rest.uniprot.org/{db_type}/search",
                              params={"query": query, "size": max_results}, timeout=15)
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
    name="query_pdb_structure",
    description=(
            "[Tags: Proteomics, Structure] Interact with the RCSB Protein Data Bank (PDB). "
            "Use action='search' to find 3D structures based on a keyword or protein name. "
            "Use action='details' to fetch precise metadata (molecular weight, primary citation, atom count) for an EXACT known PDB ID (e.g., '4HHB')."
    )
)
@simple_retry(max_attempts=2, delay=1)
def query_pdb_structure(query: str, action: Literal["search", "details"] = "search", max_results: int = 3) -> str:
    logger.info(f"Task: Unified PDB Query | Action: '{action}' | Query: '{query}'")
    try:
        if action == "search":
            url = "https://search.rcsb.org/rcsbsearch/v2/query"
            payload = {"query": {"type": "terminal", "service": "full_text", "parameters": {"value": query}},
                       "return_type": "entry", "request_options": {"paginate": {"start": 0, "rows": max_results}}}
            res = mcp_request("POST", url, json=payload, timeout=10)
            if res.status_code == 400:
                return json.dumps({
                    "status": "error",
                    "message": f"HTTP 400 Bad Request. PDB API rejected the search query '{query}'. Try using a single keyword without spaces, or use the 'details' action for a specific ID."
                })
            res.raise_for_status()
            pdb_ids = [item["identifier"] for item in res.json().get("result_set", [])]
            if not pdb_ids: return json.dumps({"status": "success", "results": []})
            results = []
            for pid in pdb_ids:
                det_res = mcp_request("GET", f"https://data.rcsb.org/rest/v1/core/entry/{pid}", timeout=5)
                if det_res.status_code == 200:
                    d = det_res.json()
                    results.append({"pdb_id": pid, "title": d.get("struct", {}).get("title", ""),
                                    "method": d.get("exptl", [{}])[0].get("method", "Unknown"),
                                    "resolution": d.get("rcsb_entry_info", {}).get("resolution_estimated_by_xray",
                                                                                   "N/A"),
                                    "organism": d.get("rcsb_entity_source_organism", [{}])[0].get(
                                        "ncbi_scientific_name", "Unknown")})
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
            results = {
                "pdb_id": data.get("entry", {}).get("id", query.upper()),
                "title": data.get("struct", {}).get("title", ""),
                "method": exptl_data.get("method", "Unknown"),
                "resolution": entry_info.get("resolution_estimated_by_xray"),
                "molecular_weight_kDa": entry_info.get("molecular_weight", 0),
                "atom_count": entry_info.get("deposited_atom_count", 0),
                "primary_citation": {"title": citation_data.get("title", ""),
                                     "journal": citation_data.get("journal_abbrev", ""),
                                     "year": citation_data.get("year", ""),
                                     "pmid": citation_data.get("pdbx_database_id_PubMed", "")}
            }
            return json.dumps({"status": "success", "action": "details", "results": results}, ensure_ascii=False)
        else:
            return json.dumps({"status": "error", "message": "Invalid action. Must be 'search' or 'details'."})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool(
    name="analyze_string_network",
    description=(
            "[Tags: Protein-Protein Interaction] Analyze protein networks via STRING DB. "
            "action='interactions' retrieves interacting partners and connection scores. "
            "action='enrichment' fetches functional enrichment (may be restricted by API, fallback to 'interactions' if it fails). "
            "Identifiers MUST be comma-separated (e.g., 'TP53,BRCA1'). 'species' MUST be a valid NCBI taxonomy ID (e.g., 9606 for Human, 10090 for Mouse)."
    )
)
@simple_retry(max_attempts=2, delay=1)
def analyze_string_network(identifiers: str, action: Literal["interactions", "enrichment"] = "interactions", species: int = 9606, limit: int = 15) -> str:
    logger.info(
        f"Task: Unified STRING DB | Action: '{action}' | Identifiers: '{identifiers[:30]}' | Species: {species}")
    try:
        if action == "interactions":
            url = "https://string-db.org/api/json/interaction_partners"
            payload = {"identifiers": identifiers.strip(), "species": species, "limit": limit,
                       "caller_identity": "ScholarNavis"}
            res = mcp_request("POST", url, data=payload, timeout=15)
            if res.status_code in [400, 404]:
                return json.dumps({"status": "error",
                                   "message": f"HTTP {res.status_code}. The STRING database could not find interactions for '{identifiers}' in species '{species}'. Ensure valid protein names/IDs and correct NCBI taxonomy ID."})
            res.raise_for_status()
            results = [{"protein_A": item.get("preferredName_A", ""), "protein_B": item.get("preferredName_B", ""),
                        "score": item.get("score", 0), "annotation_A": item.get("annotation_A", ""),
                        "annotation_B": item.get("annotation_B", "")} for item in res.json()]
            if not results: return json.dumps(
                {"status": "success", "message": "No interactions found. Check identifiers and species ID."})
            results = sorted(results, key=lambda x: x["score"], reverse=True)
            return json.dumps({"status": "success", "action": "interactions", "species": species, "results": results},
                              ensure_ascii=False)


        elif action == "enrichment":

            url = "https://string-db.org/api/json/enrichment"

            payload = {"identifiers": identifiers.strip(), "species": species, "caller_identity": "ScholarNavis"}

            res = mcp_request("POST", url, data=payload, timeout=20)

            if res.status_code in [404, 400]:
                return json.dumps({

                    "status": "error",

                    "message": f"HTTP {res.status_code}. The STRING database REST API does not expose the full functional enrichment backend via this endpoint, or the identifiers provided were not recognized. Please use 'interactions' action instead."

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