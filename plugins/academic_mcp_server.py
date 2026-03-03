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
from logging.handlers import RotatingFileHandler

import requests
from Bio import Entrez
from mcp.server.fastmcp import FastMCP

from src.core.config_manager import ConfigManager
from src.core.network_worker import setup_global_network_env, create_robust_session
from src.core.oa import OAFetcher


def get_app_root():
    """Nuitka 安全的根目录解析"""
    # 如果被 Nuitka 打包（或者 PyInstaller）
    if getattr(sys, 'frozen', False) or '__compiled__' in globals():
        return os.path.dirname(sys.executable)
    # 如果是源码运行（假设该文件在 src/plugins 或类似子目录下，向上退一级）
    # 请根据你实际的目录层级调整这里的 ".." 数量
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

APP_ROOT = get_app_root()


logger = logging.getLogger("Academic.Server")
logger.setLevel(logging.INFO)

log_dir = os.path.join(APP_ROOT, "logs", "mcp", "academic")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "academic_mcp.log")

formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

file_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# 标准错误输出 (避免污染 MCP 需要的 stdout 协议流)
stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setFormatter(formatter)
logger.addHandler(stderr_handler)

ConfigManager()
setup_global_network_env()

ncbi_email = os.environ.get("NCBI_API_EMAIL", "scholar.navis.admin@example.com").strip()
ncbi_api_key = os.environ.get("NCBI_API_KEY", "").strip()
s2_api_key = os.environ.get("S2_API_KEY", "").strip()


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
                    if attempt == max_attempts - 1: raise e
                    time.sleep(delay)

        return wrapper

    return decorator



# 1. 统一文献检索工具
@mcp.tool(
    name="search_academic_literature",
    description=(
            "[Tags: Literature] "
            "Search global academic literature for metadata (authors, journal, date, citation count, DOI). "
            "Supports pagination via the 'offset' parameter (e.g., offset=5 for page 2). "
            "Automatically cascades to Semantic Scholar, OpenAlex, Crossref, or PubMed based on query context. "
            "Use this for broad topic searches or exact article title matching."
    )
)
@simple_retry(max_attempts=2, delay=1)
def search_academic_literature(query: str, max_results: int = 5, offset: int = 0, source: str = "auto") -> str:
    logger.info(f"Task: Unified Literature Search | Query: '{query}' | Offset: {offset} | Source: {source}")
    try:
        # 优先级 1: Semantic Scholar
        if s2_api_key and source in ["auto", "semantic_scholar"]:
            url = "https://api.semanticscholar.org/graph/v1/paper/search"
            params = {"query": query, "limit": max_results, "offset": offset,
                      "fields": "title,authors,year,abstract,citationCount,isOpenAccess,url,externalIds"}
            try:
                res = mcp_request("GET", url, params=params, headers={"x-api-key": s2_api_key}, timeout=15)
                res.raise_for_status()
                parsed = [{"title": p.get("title", ""), "year": p.get("year", "Unknown"),
                           "authors": [a["name"] for a in p.get("authors", [])][:5],
                           "citation_count": p.get("citationCount", 0),
                           "abstract": (p.get("abstract") or "No abstract")[:600] + "...",
                           "doi": p.get("externalIds", {}).get("DOI", ""), "url": p.get("url", ""),
                           "source_db": "Semantic Scholar"} for p in res.json().get("data", [])]
                if parsed: return json.dumps({"status": "success", "source": "semantic_scholar", "results": parsed})
            except Exception as e:
                logger.warning(f"S2 search failed: {e}")

        # 优先级 2: OpenAlex
        if source in ["auto", "openalex"]:
            page = (offset // max_results) + 1
            url = f"https://api.openalex.org/works?search={urllib.parse.quote(query)}&mailto={ncbi_email}&per-page={max_results}&page={page}"
            try:
                res = mcp_request("GET", url, timeout=15)
                res.raise_for_status()
                parsed = []
                for p in res.json().get("results", []):
                    abs_idx = p.get("abstract_inverted_index")
                    abstract_text = "No abstract"
                    if abs_idx:
                        words = [(pos, w) for w, positions in abs_idx.items() for pos in positions]
                        words.sort()
                        abstract_text = " ".join([w for p, w in words])[:600] + "..."
                    parsed.append({"title": p.get("title", ""), "year": p.get("publication_year", "Unknown"),
                                   "authors": [a.get("author", {}).get("display_name", "") for a in
                                               p.get("authorships", [])][:5],
                                   "citation_count": p.get("cited_by_count", 0), "abstract": abstract_text,
                                   "doi": p.get("doi", "").replace("https://doi.org/", "") if p.get("doi") else "",
                                   "url": p.get("id", ""), "source_db": "OpenAlex"})
                if parsed: return json.dumps({"status": "success", "source": "openalex", "results": parsed})
            except Exception as e:
                logger.warning(f"OpenAlex search failed: {e}")

        # 优先级 3: Crossref
        if source in ["auto", "crossref"]:
            url = f"https://api.crossref.org/works?query={urllib.parse.quote(query)}&mailto={ncbi_email}&rows={max_results}&offset={offset}"
            try:
                res = mcp_request("GET", url, timeout=15)
                res.raise_for_status()
                parsed = []
                for p in res.json().get("message", {}).get("items", []):
                    authors = [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in p.get("author", [])]
                    parsed.append({"title": p.get("title", [""])[0],
                                   "year": p.get("created", {}).get("date-parts", [["Unknown"]])[0][0],
                                   "authors": authors[:5], "citation_count": p.get("is-referenced-by-count", 0),
                                   "abstract": p.get("abstract", "No abstract")[:600].replace("<jats:p>", "").replace(
                                       "</jats:p>", "") + "...", "doi": p.get("DOI", ""), "url": p.get("URL", ""),
                                   "source_db": "Crossref"})
                if parsed: return json.dumps({"status": "success", "source": "crossref", "results": parsed})
            except Exception as e:
                logger.warning(f"Crossref search failed: {e}")

        # 优先级 4: PubMed 兜底
        if ncbi_email and source in ["auto", "pubmed"]:
            search_handle = Entrez.esearch(db="pubmed", term=query, retstart=offset, retmax=max_results)
            ids = Entrez.read(search_handle).get("IdList", [])
            search_handle.close()
            if ids:
                summary_handle = Entrez.esummary(db="pubmed", id=",".join(ids))
                doc_list = Entrez.read(summary_handle)
                if isinstance(doc_list, dict): doc_list = doc_list.get("DocumentSummarySet", {}).get("DocumentSummary",
                                                                                                     [])
                summary_handle.close()
                parsed = [{"title": d.get("Title", ""), "year": d.get("PubDate", "")[:4],
                           "authors": list(d.get("AuthorList", [])), "abstract": "Fetch via fetch_pubmed_abstract.",
                           "doi": next(
                               (a.get("Value", "") for a in d.get("ArticleIds", []) if a.get("IdType") == "doi"), ""),
                           "url": f"https://pubmed.ncbi.nlm.nih.gov/{d.get('Id', '')}/", "source_db": "PubMed"} for d in
                          doc_list]
                return json.dumps({"status": "success", "source": "pubmed", "results": parsed})

        return json.dumps(
            {"status": "success", "results": [], "message": "No results found across available databases."})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


# 2. 引文追踪引擎 (Citation Graph)
@mcp.tool(
    name="traverse_citation_graph",
    description=(
            "[Tags: Literature] "
            "Find the references (papers this article cites) or citations (papers that cite this article) for a given DOI. "
            "Direction MUST be either 'references' or 'citations'. "
            "Use this for tracing the history of a technique or finding follow-up research."
    )
)
@simple_retry(max_attempts=2, delay=1)
def traverse_citation_graph(doi: str, direction: str = "references", max_results: int = 10) -> str:
    logger.info(f"Task: Citation Graph | DOI: {doi} | Direction: {direction}")
    if direction not in ["references", "citations"]: return json.dumps(
        {"status": "error", "message": "direction must be 'references' or 'citations'"})
    clean_doi = doi.replace("https://doi.org/", "").strip()

    if s2_api_key:
        try:
            url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{clean_doi}/{direction}?fields=title,authors,year,citationCount,externalIds&limit={max_results}"
            res = mcp_request("GET", url, headers={"x-api-key": s2_api_key}, timeout=15)
            res.raise_for_status()
            parsed = []
            for item in res.json().get("data", []):
                p = item.get("citedPaper") if direction == "references" else item.get("citingPaper")
                if not p or not p.get("title"): continue
                parsed.append({
                    "title": p.get("title", ""), "year": p.get("year", "Unknown"),
                    "authors": [a["name"] for a in p.get("authors", [])][:3],
                    "citation_count": p.get("citationCount", 0),
                    "doi": p.get("externalIds", {}).get("DOI", "")
                })
            return json.dumps(
                {"status": "success", "source": "Semantic Scholar", "direction": direction, "results": parsed})
        except Exception as e:
            logger.warning(f"S2 citation graph failed: {e}")

    try:
        if direction == "references":
            work_res = mcp_request("GET",
                                   f"https://api.openalex.org/works/https://doi.org/{clean_doi}?mailto={ncbi_email}").json()
            ref_ids = work_res.get("referenced_works", [])[:max_results]
            if not ref_ids: return json.dumps({"status": "success", "results": []})
            filter_str = "|".join([r.split("/")[-1] for r in ref_ids])
            url = f"https://api.openalex.org/works?filter=openalex:{filter_str}&mailto={ncbi_email}"
        else:
            url = f"https://api.openalex.org/works?filter=cites:https://doi.org/{clean_doi}&per-page={max_results}&mailto={ncbi_email}"

        res = mcp_request("GET", url, timeout=15).json()
        parsed = [{"title": p.get("title", ""), "year": p.get("publication_year", "Unknown"),
                   "citation_count": p.get("cited_by_count", 0),
                   "doi": p.get("doi", "").replace("https://doi.org/", "") if p.get("doi") else ""} for p in
                  res.get("results", [])]
        return json.dumps({"status": "success", "source": "OpenAlex", "direction": direction, "results": parsed})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


# 3. 开放获取链接提取
@mcp.tool(
    name="fetch_open_access_pdf",
    description=(
            "[Tags: Literature] "
            "[Always Enabled] Check if a given DOI has an Open Access PDF and return its direct download link."
    )
)
@simple_retry()
def fetch_open_access_pdf(doi: str) -> str:
    logger.info(f"Task: Fetch OA PDF | DOI: '{doi}'")

    def request_adapter(url, headers=None, timeout=5):
        return mcp_request("GET", url, headers=headers, timeout=timeout)

    fetcher = OAFetcher()

    result = fetcher.fetch_best_oa_pdf(doi, ncbi_email, s2_api_key)

    if result.get("is_oa"):
        return json.dumps({
            "status": "success",
            "is_oa": True,
            "pdf_url": result["pdf_url"],
            "landing_page_url": result["landing_page_url"],
            "source": result["source"]
        })
    else:
        clean_doi = doi.replace("https://doi.org/", "").replace("http://dx.doi.org/", "").strip()
        landing_url = result.get("landing_page_url", f"https://doi.org/{clean_doi}")
        return json.dumps({
            "status": "success",
            "is_oa": False,
            "landing_page_url": landing_url,
            "message": "Paywalled. No OA PDF found."
        })




# 5. 生物大分子检索 (NCBI + UniProt 整合)
@mcp.tool(
    name="search_biological_entity",
    description=(
            "[Tags: Genomics, Protein] "
            "Query biological entities (Genes or Proteins) to retrieve functional summaries, lengths, taxonomic info, and metadata. "
            "Combines NCBI Gene and UniProt databases. "
            "Use this when the user asks 'What is the function of gene/protein X?'"
    )
)
@simple_retry(max_attempts=2, delay=1)
def search_biological_entity(query: str, entity_type: str = "gene", organism: str = "") -> str:
    logger.info(f"Task: Biol Entity Search | Type: {entity_type} | Query: '{query}' | Org: '{organism}'")
    try:
        if entity_type.lower() == "protein":
            search_query = f"({query})"
            if organism: search_query += f" AND (organism_name:{organism})"
            url = f"https://rest.uniprot.org/uniprotkb/search?query={urllib.parse.quote(search_query)}&format=json&size=3"
            res = mcp_request("GET", url, timeout=10)
            res.raise_for_status()
            results = []
            for p in res.json().get("results", []):
                results.append({
                    "accession": p.get("primaryAccession", ""),
                    "protein_name": p.get("proteinDescription", {}).get("recommendedName", {}).get("fullName", {}).get(
                        "value", ""),
                    "gene_name": p.get("genes", [{}])[0].get("geneName", {}).get("value", "") if p.get("genes") else "",
                    "organism": p.get("organism", {}).get("scientificName", ""),
                    "sequence_length": p.get("sequence", {}).get("length", 0),
                    "function_summary": next((c.get("texts", [{}])[0].get("value", "") for c in p.get("comments", []) if
                                              c.get("commentType") == "FUNCTION"), "No function summary.")
                })
            return json.dumps({"status": "success", "db": "UniProt", "results": results})
        else:
            term = f"{query}[Gene Name]" + (f" AND {organism}[Organism]" if organism else "")
            search_handle = Entrez.esearch(db="gene", term=term, retmax=3)
            ids = Entrez.read(search_handle).get("IdList", [])
            search_handle.close()
            if not ids: return json.dumps({"status": "success", "results": []})

            summary_handle = Entrez.esummary(db="gene", id=",".join(ids))
            summaries = Entrez.read(summary_handle).get("DocumentSummarySet", {}).get("DocumentSummary", [])
            summary_handle.close()

            results = [{"symbol": d.get("Name"), "description": d.get("Description"),
                        "organism": d.get("Organism", {}).get("ScientificName", ""),
                        "summary": d.get("Summary", "No summary available."), "map_location": d.get("MapLocation", "")}
                       for d in summaries]
            return json.dumps({"status": "success", "db": "NCBI Gene", "results": results})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


# 6. 植物组学引擎 (JGI Phytozome/PhytoMine)
@mcp.tool(
    name="search_phytozome_phytomine",
    description=(
            "[Tags: Genomics, Plant] "
            "Search the JGI Phytozome (PhytoMine) database for plant genomics data. "
            "Use this to find plant-specific genes, proteins, orthologs, and families across assembled plant genomes without requiring a login."
    )
)
@simple_retry()
def search_phytozome_phytomine(query: str) -> str:
    logger.info(f"Task: Phytozome Search | Query: '{query}'")
    try:
        url = f"https://phytozome-next.jgi.doe.gov/phytomine/service/search?q={urllib.parse.quote(query)}&format=json"
        res = mcp_request("GET", url, timeout=15)
        res.raise_for_status()

        parsed = [{"type": r.get("type", ""), "name": r.get("fields", {}).get("name", ""),
                   "primaryIdentifier": r.get("fields", {}).get("primaryIdentifier", ""),
                   "description": r.get("fields", {}).get("description", ""),
                   "organism": r.get("fields", {}).get("organism.shortName", "")} for r in
                  res.json().get("results", [])[:5]]
        return json.dumps({"status": "success", "db": "Phytozome", "results": parsed})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


# 7. 组学数据集联搜 (SRA + GEO)
@mcp.tool(
    name="search_omics_datasets",
    description=(
            "[Tags: Genomics] "
            "Search high-throughput sequencing and microarray datasets (SRA / GEO). "
            "Use this to find raw multi-omics datasets (RNA-Seq, ChIP-Seq, WGS), biosample details, and accession IDs associated with specific phenotypes, treatments, or organisms. "
            "Provide db_type as 'sra' for raw sequencing reads or 'geo' for expression/array studies."
    )
)
@simple_retry()
def search_omics_datasets(query: str, db_type: str = "sra", max_results: int = 5) -> str:
    logger.info(f"Task: Omics Dataset Search | DB: {db_type} | Query: '{query}'")
    try:
        db = "gds" if db_type.lower() == "geo" else "sra"
        search_handle = Entrez.esearch(db=db, term=query, retmax=max_results)
        ids = Entrez.read(search_handle).get("IdList", [])
        search_handle.close()

        if not ids: return json.dumps({"status": "success", "results": []})

        summary_handle = Entrez.esummary(db=db, id=",".join(ids))
        summaries = Entrez.read(summary_handle)
        summary_handle.close()

        doc_list = summaries if isinstance(summaries, list) else summaries.get("DocumentSummarySet", {}).get(
            "DocumentSummary", [])
        if isinstance(doc_list, dict): doc_list = [doc_list]

        parsed_results = []
        for doc in doc_list:
            if db == "sra":
                import re
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


# 8. 蛋白质结构检索 (RCSB PDB)
@mcp.tool(
    name="search_protein_structure",
    description=(
            "[Tags: Protein, Structure] "
            "Search the RCSB PDB (Protein Data Bank) for 3D protein structures, experimental methods (X-ray, Cryo-EM), and resolution details. "
            "Use this when the user needs structural biology information."
    )
)
@simple_retry(max_attempts=2, delay=1)
def search_protein_structure(query: str, max_results: int = 3) -> str:
    logger.info(f"Task: PDB Structure Search | Query: '{query}'")
    try:
        url = "https://search.rcsb.org/rcsbsearch/v2/query"
        payload = {"query": {"type": "terminal", "service": "text", "parameters": {"value": query}},
                   "return_type": "entry", "request_options": {"paginate": {"start": 0, "rows": max_results}}}
        res = mcp_request("POST", url, json=payload, timeout=10)
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
                                "resolution": d.get("rcsb_entry_info", {}).get("resolution_estimated_by_xray", "N/A"),
                                "organism": d.get("rcsb_entity_source_organism", [{}])[0].get("ncbi_scientific_name",
                                                                                              "Unknown")})
        return json.dumps({"status": "success", "results": results})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


# 9. FASTA 序列下载 (大文件落盘保护)
@mcp.tool(
    name="fetch_sequence_fasta",
    description=(
            "[Tags: Genomics, Protein] "
            "Download raw FASTA sequences for nucleotides or proteins. "
            "If the sequence is massive (e.g., full genome), it automatically saves to the local workspace and returns a viewable link."
    )
)
@simple_retry()
def fetch_sequence_fasta(accession_id: str, db_type: str = "nuccore") -> str:
    logger.info(f"Task: FASTA Download | ID: {accession_id} | DB: {db_type}")

    safe_id = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', accession_id)
    safe_db = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', db_type)


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
                "status": "success",
                "message": "Sequence is extremely large. Saved to local workspace.",
                "local_path": file_path,
                "cite_link": cite_link,
                "preview_header": data[:500] + "\n..."
            })

        return json.dumps({"status": "success", "accession": accession_id, "fasta": data.strip()})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


# 10. NCBI 分类学信息
@mcp.tool(
    name="fetch_taxonomy_info",
    description=(
            "[Tags: Taxonomy] "
            "Search the NCBI Taxonomy database to get the exact scientific name, TaxID, and evolutionary lineage of an organism."
    )
)
@simple_retry()
def fetch_taxonomy_info(organism_name: str) -> str:
    logger.info(f"Task: Taxonomy Fetch | Organism: '{organism_name}'")
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


# 11. PubMed 摘要提取
@mcp.tool(
    name="fetch_pubmed_abstract",
    description=(
            "[Tags: Literature] "
            "[Core Academic Tool] Fetch the full-text abstract of a specific PubMed article using its PMID."
    )
)
@simple_retry()
def fetch_pubmed_abstract(pmid: str) -> str:
    logger.info(f"Task: Fetch Abstract | PMID: {pmid}")
    try:
        handle = Entrez.efetch(db="pubmed", id=pmid, rettype="abstract", retmode="text")
        abstract_text = handle.read()
        handle.close()
        return json.dumps({"status": "success", "pmid": pmid, "abstract": abstract_text.strip()})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


# 12. NCBI 全局兜底
@mcp.tool(
    name="universal_ncbi_summary",
    description=(
            "[Tags: any NCBI] "
            "A universal tool to search ANY NCBI database (e.g., 'omim', 'clinvar', 'assembly', 'mesh', 'genome'). "
            "Returns the basic summary metadata for the matching records."
    )
)
@simple_retry()
def universal_ncbi_summary(database: str, query: str, max_results: int = 3) -> str:
    logger.info(f"Task: Universal NCBI Summarize | database: {database} | query: {query} | max_results: {max_results}")
    try:
        search_handle = Entrez.esearch(db=database, term=query, retmax=max_results)
        ids = Entrez.read(search_handle).get("IdList", [])
        search_handle.close()

        if not ids: return json.dumps(
            {"status": "success", "results": [], "message": f"No records found in {database}."})

        summary_handle = Entrez.esummary(db=database, id=",".join(ids))
        summaries = Entrez.read(summary_handle)
        summary_handle.close()

        doc_list = summaries if isinstance(summaries, list) else summaries.get("DocumentSummarySet", {}).get(
            "DocumentSummary", [])
        if isinstance(doc_list, dict): doc_list = [doc_list]

        parsed_results = [{"id": d.get("Id", ""), **{k: str(v) for k, v in d.items() if not k.startswith("Item")}} for d
                          in doc_list]
        return json.dumps({"status": "success", "database": database, "results": parsed_results})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool(
    name="fetch_webpage_content",
    description=(
            "[Tags: Web] "
            "Fetch and read the text content of a user-provided URL. "
            "Automatically handles proxies, bypasses WAFs using robust browser headers (TLS fingerprinting), and enforces a timeout. "
            "Trigger this ONLY when the user explicitly provides a URL in their prompt and asks to read, summarize, or extract information from it."
    )
)
@simple_retry(max_attempts=2, delay=1)
def fetch_webpage_content(url: str, timeout: int = 15) -> str:
    logger.info(f"Task: Fetch Webpage | URL: '{url}' | Timeout: {timeout}s")

    if not url.startswith(("http://", "https://")):
        logger.warning(f"Security Block: Invalid URL scheme requested -> {url}")
        return json.dumps({"status": "error", "message": "Security Error: Only HTTP and HTTPS protocols are allowed."})


    parsed_url = urllib.parse.urlparse(url)
    hostname = parsed_url.hostname

    # 1. 拦截常见本地及内网域名
    if hostname in ['localhost', 'broadcasthost'] or hostname.endswith('.local'):
        logger.warning(f"Security Block: Local network access forbidden -> {url}")
        return json.dumps(
            {"status": "error", "message": "Security Error: Access to local network addresses is forbidden."})

    # 2. 深度拦截：将域名解析为 IP 并判断是否为私有局域网 IP
    try:
        ip_obj = ipaddress.ip_address(socket.gethostbyname(hostname))
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
            logger.warning(f"Security Block: Private IP access forbidden -> {ip_obj}")
            return json.dumps(
                {"status": "error", "message": "Security Error: Probing internal network IPs is forbidden."})
    except Exception:
        # 如果域名无法解析或格式错误，忽略此步，让后面的 requests 去抛出正常错误
        pass

    try:

        res = mcp_request("GET", url, timeout=timeout)
        res.raise_for_status()

        html_content = res.text

        # 简单清洗 HTML，去除 script 和 style 标签，提取纯文本，防止撑爆上下文
        text_content = re.sub(r'<script.*?>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        text_content = re.sub(r'<style.*?>.*?</style>', '', text_content, flags=re.DOTALL | re.IGNORECASE)
        text_content = re.sub(r'<[^>]+>', ' ', text_content)

        text_content = re.sub(r'\s+', ' ', text_content).strip()

        max_chars = 30000
        if len(text_content) > max_chars:
            text_content = text_content[:max_chars] + "\n...[Content truncated due to length limits]"

        return json.dumps({
            "status": "success",
            "url": url,
            "content": text_content
        }, ensure_ascii=False)

    except Exception as e:
        logger.error(f"Webpage fetch failed for {url}: {e}")
        return json.dumps({
            "status": "error",
            "message": f"Failed to fetch URL. It might be unreachable, strictly protected by WAF, or timed out. Error: {str(e)}"
        })



if __name__ == "__main__":
    logger.info("Academic MCP Server initialized.")
    mcp.run(transport='stdio')