# plugins/bio_ncbi_server.py
import os
import json
import logging
import socket
import sys
import traceback
import urllib
from datetime import datetime

import requests
from Bio import Entrez
from mcp.server.fastmcp import FastMCP


logger = logging.getLogger("Academic.Server")


# Load NCBI credentials from environment variables injected by the main process
Entrez.email = os.environ.get("NCBI_API_EMAIL", "scholar.navis.admin@example.com")
api_key = os.environ.get("NCBI_API_KEY", "")
if api_key:
    Entrez.api_key = api_key
    logger.info("NCBI API Key successfully authenticated.")
else:
    logger.warning("No NCBI API Key detected. Performance may be restricted by rate limits.")

Entrez.tool = "ScholarNavis"
mcp = FastMCP("ScholarNavis-Academic-Plugin")


ncbi_email = os.environ.get("NCBI_API_EMAIL", "").strip()
ncbi_api_key = os.environ.get("NCBI_API_KEY", "").strip()
s2_api_key = os.environ.get("S2_API_KEY", "").strip()


class UDPLogHandler(logging.Handler):
    def __init__(self, port):
        super().__init__()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.address = ('127.0.0.1', port)

    def emit(self, record):
        try:
            msg = record.getMessage()
            if record.exc_info:
                if not record.exc_text:
                    self.formatter = logging.Formatter()
                    record.exc_text = self.formatter.formatException(record.exc_info)
                msg += f"\n{record.exc_text}"

            payload = f"{record.levelname}|{msg}"
            payload = payload[:65000]
            self.sock.sendto(payload.encode('utf-8'), self.address)
        except Exception:
            pass


log_port_str = os.environ.get("SCHOLAR_NAVIS_LOG_PORT", "")
if log_port_str.isdigit():
    udp_handler = UDPLogHandler(int(log_port_str))
    logger.addHandler(udp_handler)
    logger.info(f"Connected to Main UI Log System via UDP port {log_port_str}.")

if ncbi_email:
    logger.info(f"NCBI Email detected ({ncbi_email}). Enabling NCBI MCP tools.")
    Entrez.email = ncbi_email
    if ncbi_api_key:
        Entrez.api_key = ncbi_api_key
    Entrez.tool = "ScholarNavis"


    @mcp.tool()
    def search_pubmed_literature(query: str, max_results: int = 5) -> str:
        """
        Search PubMed for scientific literature.
        Returns highly detailed metadata including PMID, Title, Abstract, DOI, publisher link (paywall), and PMC (free full-text) links.
        """
        logger.info(f"Task: PubMed Search | Query: '{query}'")
        try:
            search_handle = Entrez.esearch(db="pubmed", term=query, retmax=max_results)
            search_results = Entrez.read(search_handle)
            search_handle.close()

            ids = search_results.get("IdList", [])
            if not ids:
                logger.info("No records found in PubMed.")
                return json.dumps({"status": "success", "results": [], "count": 0})

            summary_handle = Entrez.esummary(db="pubmed", id=",".join(ids))
            summaries = Entrez.read(summary_handle)
            summary_handle.close()

            parsed_results = []
            for doc in summaries:
                # 1. 安全提取 DOI 和 PMCID
                article_ids = doc.get("ArticleIds", [])
                doi_val = ""
                pmc_val = ""

                if isinstance(article_ids, list):
                    for aid in article_ids:
                        id_type = aid.get("IdType", "")
                        if id_type == "doi":
                            doi_val = aid.get("Value", "")
                        elif id_type == "pmc":
                            pmc_val = aid.get("Value", "")

                final_doi = doc.get("DOI", "") or doi_val

                # 2. 构造各类跳转链接
                pmid = str(doc.get("Id", ""))
                pubmed_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
                pmc_free_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_val}/" if pmc_val else ""

                # 🆕 构造出版商的 Paywall 官方链接
                doi_url = f"https://doi.org/{final_doi}" if final_doi else ""

                parsed_results.append({
                    "pmid": pmid,
                    "title": doc.get("Title", ""),
                    "authors": list(doc.get("AuthorList", [])),
                    "journal": doc.get("Source", ""),
                    "pub_date": doc.get("PubDate", ""),
                    "doi": final_doi,
                    "pmc_id": pmc_val,
                    "pubmed_url": pubmed_url,
                    "doi_url": doi_url,  # 出版商 Paywall 链接
                    "pmc_free_url": pmc_free_url  # PMC 免费全文链接
                })

            logger.info(f"Retrieved {len(parsed_results)} PubMed records.")
            return json.dumps({"status": "success", "results": parsed_results, "count": len(parsed_results)})
        except Exception as e:
            logger.error(f"PubMed search failed: {str(e)}")
            return json.dumps({"status": "error", "message": str(e)})


    @mcp.tool()
    def fetch_gene_information(gene_symbol: str, organism: str = "") -> str:
        """
        Query the NCBI Gene database for functional summaries, genomic coordinates,
        and chromosome mapping.
        """
        logger.info(f"Task: Gene Information Fetch | Gene: {gene_symbol} | Organism: {organism}")
        try:
            term = f"{gene_symbol}[Gene Name]"
            if organism:
                term += f" AND {organism}[Organism]"

            search_handle = Entrez.esearch(db="gene", term=term, retmax=3)
            search_results = Entrez.read(search_handle)
            search_handle.close()

            ids = search_results.get("IdList", [])
            if not ids:
                return json.dumps({"status": "success", "results": [], "message": "No matching gene records found."})

            summary_handle = Entrez.esummary(db="gene", id=",".join(ids))
            summaries = Entrez.read(summary_handle)
            summary_handle.close()

            doc_set = summaries.get("DocumentSummarySet", {}).get("DocumentSummary", [])
            parsed_results = []
            for doc in doc_set:
                parsed_results.append({
                    "ncbi_id": doc.get("Id", ""),
                    "official_symbol": doc.get("Name", ""),
                    "description": doc.get("Description", ""),
                    "organism": doc.get("OrgXml", {}).get("CommonName", ""),
                    "scientific_name": doc.get("OrgXml", {}).get("ScientificName", ""),
                    "chromosome": doc.get("Chromosome", ""),
                    "map_location": doc.get("MapLocation", ""),
                    "summary": doc.get("Summary", ""),
                    "status": doc.get("Status", "")
                })

            return json.dumps({"status": "success", "results": parsed_results})
        except Exception as e:
            logger.error(f"NCBI Gene query failed: {str(e)}")
            return json.dumps({"status": "error", "message": str(e)})


    @mcp.tool()
    def fetch_sequence_fasta(accession_id: str, db_type: str = "nuccore") -> str:
        """
        Download raw FASTA sequences for nucleotides (nuccore) or proteins (protein).
        """
        logger.info(f"Task: FASTA Download | ID: {accession_id} | DB: {db_type}")
        try:
            if db_type not in ["nuccore", "protein"]:
                return json.dumps({"status": "error", "message": "Invalid database. Use 'nuccore' or 'protein'."})

            fetch_handle = Entrez.efetch(db=db_type, id=accession_id, rettype="fasta", retmode="text")
            data = fetch_handle.read()
            fetch_handle.close()

            if not data:
                return json.dumps({"status": "error", "message": "Empty sequence returned."})

            return json.dumps({
                "status": "success",
                "accession": accession_id,
                "db": db_type,
                "fasta": data.strip()
            })
        except Exception as e:
            logger.error(f"Sequence retrieval failed: {str(e)}")
            return json.dumps({"status": "error", "message": str(e)})


    @mcp.tool()
    def search_sra_datasets(query: str, max_results: int = 5) -> str:
        """
        Access the Sequence Read Archive (SRA) to find high-throughput sequencing datasets (SRR IDs).
        Essential for multi-omics data mining.
        """
        logger.info(f"Task: SRA Metadata Search | Query: '{query}'")
        try:
            search_handle = Entrez.esearch(db="sra", term=query, retmax=max_results)
            search_results = Entrez.read(search_handle)
            search_handle.close()

            ids = search_results.get("IdList", [])
            if not ids:
                return json.dumps({"status": "success", "results": []})

            summary_handle = Entrez.esummary(db="sra", id=",".join(ids))
            summaries = Entrez.read(summary_handle)
            summary_handle.close()

            parsed_results = []
            for doc in summaries:
                exp_xml = doc.get("ExpXml", "")
                import re
                run_match = re.search(r'acc="([S|E|D]RR\d+)"', exp_xml)
                run_id = run_match.group(1) if run_match else "Unknown"

                parsed_results.append({
                    "sra_id": doc.get("Id", ""),
                    "run_accession": run_id,
                    "title": doc.get("ExpTitle", ""),
                    "platform": doc.get("Instrument", ""),
                    "strategy": doc.get("Library_strategy", ""),
                    "organism": doc.get("Biosample", {}).get("Organism", ""),
                    "create_date": doc.get("CreateDate", "")
                })

            return json.dumps({"status": "success", "results": parsed_results})
        except Exception as e:
            logger.error(f"SRA search encountered an error: {str(e)}")
            return json.dumps({"status": "error", "message": str(e)})


    @mcp.tool()
    def fetch_protein_summary(protein_query: str, organism: str = "") -> str:
        """
        Retrieve structural metadata and summaries from the NCBI Protein database.
        """
        logger.info(f"Task: Protein Summary Retrieval | Query: {protein_query}")
        try:
            term = f"{protein_query}"
            if organism:
                term += f" AND {organism}[Organism]"

            search_handle = Entrez.esearch(db="protein", term=term, retmax=3)
            search_results = Entrez.read(search_handle)
            search_handle.close()

            ids = search_results.get("IdList", [])
            if not ids:
                return json.dumps({"status": "success", "results": []})

            summary_handle = Entrez.esummary(db="protein", id=",".join(ids))
            summaries = Entrez.read(summary_handle)
            summary_handle.close()

            parsed_results = []
            for doc in summaries:
                parsed_results.append({
                    "accession": doc.get("AccessionVersion", ""),
                    "title": doc.get("Title", ""),
                    "length_aa": doc.get("Length", ""),
                    "update_date": doc.get("UpdateDate", ""),
                    "tax_id": doc.get("TaxId", "")
                })

            return json.dumps({"status": "success", "results": parsed_results})
        except Exception as e:
            logger.error(f"Protein retrieval error: {str(e)}")
            return json.dumps({"status": "error", "message": str(e)})



    @mcp.tool()
    def universal_ncbi_summary(database: str, query: str, max_results: int = 3) -> str:
        """
        A universal tool to search ANY NCBI database (e.g., 'omim', 'clinvar', 'assembly', 'mesh', 'genome').
        Returns the basic summary metadata for the matching records.
        Use this when a specific tool for the database does not exist.
        """
        logger.info(f"Task: Universal NCBI Search | DB: {database} | Query: '{query}'")
        try:
            # Step 1: Search to get IDs
            search_handle = Entrez.esearch(db=database, term=query, retmax=max_results)
            search_results = Entrez.read(search_handle)
            search_handle.close()

            ids = search_results.get("IdList", [])
            if not ids:
                return json.dumps({"status": "success", "results": [], "message": f"No records found in {database}."})

            # Step 2: Fetch summaries for those IDs
            summary_handle = Entrez.esummary(db=database, id=",".join(ids))
            summaries = Entrez.read(summary_handle)
            summary_handle.close()

            # DocumentSummarySet structure varies wildly between databases,
            # so we extract the raw dictionaries and stringify them safely.
            parsed_results = []
            # Handle different Entrez return structures
            if isinstance(summaries, list):
                doc_list = summaries
            elif isinstance(summaries, dict) and "DocumentSummarySet" in summaries:
                doc_list = summaries["DocumentSummarySet"].get("DocumentSummary", [])
            else:
                doc_list = [summaries]

            for doc in doc_list:
                # Clean up un-serializable Entrez objects
                clean_doc = {k: str(v) for k, v in doc.items() if not k.startswith("Item")}
                parsed_results.append(clean_doc)

            return json.dumps({"status": "success", "database": database, "results": parsed_results})
        except Exception as e:
            logger.error(f"Universal search failed on db '{database}': {str(e)}")
            return json.dumps({"status": "error", "message": str(e)})


    @mcp.tool()
    def search_geo_datasets(query: str, max_results: int = 3) -> str:
        """
        Search the GEO DataSets (gds) database for gene expression studies, RNA-seq,
        and microarray datasets. Returns accession numbers (e.g., GSE IDs) and study summaries.
        """
        logger.info(f"Task: GEO Search | Query: '{query}'")
        try:
            search_handle = Entrez.esearch(db="gds", term=query, retmax=max_results)
            search_results = Entrez.read(search_handle)
            search_handle.close()

            ids = search_results.get("IdList", [])
            if not ids:
                return json.dumps({"status": "success", "results": []})

            summary_handle = Entrez.esummary(db="gds", id=",".join(ids))
            summaries = Entrez.read(summary_handle)
            summary_handle.close()

            parsed_results = []
            for doc in summaries:
                parsed_results.append({
                    "accession": doc.get("Accession", ""),
                    "title": doc.get("title", ""),
                    "summary": doc.get("summary", ""),
                    "platform": doc.get("GPL", ""),
                    "sample_count": doc.get("n_samples", ""),
                    "study_type": doc.get("gdsType", ""),
                    "taxon": doc.get("taxon", "")
                })

            return json.dumps({"status": "success", "results": parsed_results})
        except Exception as e:
            logger.error(f"GEO search failed: {str(e)}")
            return json.dumps({"status": "error", "message": str(e)})


    @mcp.tool()
    def fetch_taxonomy_info(organism_name: str) -> str:
        """
        Search the NCBI Taxonomy database to get the exact scientific name,
        TaxID, and evolutionary lineage of an organism.
        """
        logger.info(f"Task: Taxonomy Fetch | Organism: '{organism_name}'")
        try:
            search_handle = Entrez.esearch(db="taxonomy", term=organism_name, retmax=1)
            search_results = Entrez.read(search_handle)
            search_handle.close()

            ids = search_results.get("IdList", [])
            if not ids:
                return json.dumps({"status": "success", "message": f"Organism '{organism_name}' not found."})

            fetch_handle = Entrez.efetch(db="taxonomy", id=ids[0], retmode="xml")
            tax_records = Entrez.read(fetch_handle)
            fetch_handle.close()

            if not tax_records:
                return json.dumps({"status": "error", "message": "Failed to retrieve taxonomy details."})

            record = tax_records[0]
            result = {
                "tax_id": record.get("TaxId", ""),
                "scientific_name": record.get("ScientificName", ""),
                "common_name": record.get("OtherNames", {}).get("GenbankCommonName", ""),
                "rank": record.get("Rank", ""),
                "lineage": record.get("Lineage", ""),
                "genetic_code": record.get("GeneticCode", {}).get("GCId", "")
            }

            return json.dumps({"status": "success", "result": result})
        except Exception as e:
            logger.error(f"Taxonomy fetch failed: {str(e)}")
            return json.dumps({"status": "error", "message": str(e)})

else:
    logger.warning("NCBI_API_EMAIL is missing. NCBI MCP tools are disabled and hidden from LLM.")




if s2_api_key:
    logger.info("Semantic Scholar API Key detected. Enabling S2 MCP tool.")

    @mcp.tool()
    def search_semantic_scholar(query: str, max_results: int = 5) -> str:
        """
        Search the Semantic Scholar database for global academic papers.
        Returns highly structured metadata including title, authors, year, abstract, citation count, and paper URL.
        This is the primary tool for general literature retrieval across all scientific domains.
        """
        logger.info(f"Task: Semantic Scholar Search | Query: '{query}'")
        try:
            base_url = "https://api.semanticscholar.org/graph/v1/paper/search"
            params = {
                "query": query,
                "limit": max_results,
                "fields": "title,authors,year,abstract,citationCount,isOpenAccess,url"
            }

            headers = {}
            s2_api_key = os.environ.get("S2_API_KEY", "")
            if s2_api_key:
                headers["x-api-key"] = s2_api_key
                logger.info("Using authenticated Semantic Scholar request.")
            else:
                logger.warning("No Semantic Scholar API Key found. 429 limits may apply.")

            response = requests.get(base_url, params=params, headers=headers, timeout=15)
            response.raise_for_status()

            data = response.json()
            if not data.get("data"):
                return json.dumps({"status": "success", "results": [], "message": "No papers found."})

            parsed_results = []
            for paper in data["data"]:
                authors = [author["name"] for author in paper.get("authors", [])]

                parsed_results.append({
                    "title": paper.get("title", ""),
                    "year": paper.get("year", "Unknown"),
                    "authors": authors[:5],
                    "citation_count": paper.get("citationCount", 0),
                    "is_open_access": paper.get("isOpenAccess", False),
                    "abstract": paper.get("abstract", "No abstract available.")[:600] + "...",
                    "url": paper.get("url", "")
                })

            return json.dumps({"status": "success", "results": parsed_results})
        except Exception as e:
            logger.error(f"Semantic Scholar search failed: {str(e)}")
            return json.dumps({"status": "error", "message": str(e)})


    @mcp.tool()
    def fetch_open_access_pdf(doi: str) -> str:
        """
        Attempt to find a direct Open Access PDF download link for a given DOI.
        Uses the Unpaywall API, the academic standard for legally accessing free full-text papers.
        When the user wants to read or download a paper, use this tool with the paper's DOI.
        """
        logger.info(f"Task: Fetch Open Access PDF | DOI: '{doi}'")
        try:
            # 巧妙复用已有的 NCBI 邮箱配置，无需用户额外设置
            email = os.environ.get("NCBI_API_EMAIL", "scholar.navis.user@example.com")
            if not email:
                email = "scholar.navis.default@example.com"

            # 清理 DOI 字符串（去除可能包含的 https://doi.org/ 前缀）
            clean_doi = doi.replace("https://doi.org/", "").replace("http://dx.doi.org/", "").strip()

            # Unpaywall REST API
            url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(clean_doi)}?email={email}"

            # Unpaywall 响应通常很快，设置 10 秒超时
            response = requests.get(url, timeout=10)

            if response.status_code == 404:
                return json.dumps({
                    "status": "success",
                    "is_oa": False,
                    "message": "Paper not found in Unpaywall database or DOI is invalid."
                })

            response.raise_for_status()
            data = response.json()

            is_oa = data.get("is_oa", False)
            best_oa_location = data.get("best_oa_location", {})

            # 优先提取直接指向 PDF 的链接
            pdf_url = best_oa_location.get("url_for_pdf", "") if best_oa_location else ""
            landing_page_url = best_oa_location.get("url_for_landing_page", "") if best_oa_location else ""

            if is_oa and (pdf_url or landing_page_url):
                return json.dumps({
                    "status": "success",
                    "is_oa": True,
                    "pdf_url": pdf_url,
                    "landing_page_url": landing_page_url,
                    "publisher": data.get("publisher", "Unknown"),
                    "oa_status": data.get("oa_status", "Unknown")
                })
            else:
                return json.dumps({
                    "status": "success",
                    "is_oa": False,
                    "message": "The paper is paywalled (Closed Access). No legal Open Access PDF found."
                })

        except Exception as e:
            logger.error(f"Unpaywall PDF fetch failed: {str(e)}")
            return json.dumps({"status": "error", "message": str(e)})

else:
    logger.warning("S2_API_KEY is missing. Semantic Scholar MCP tool is disabled and hidden from LLM.")

if __name__ == "__main__":
    logger.info("Academic MCP Server initialized.")
    mcp.run(transport='stdio')