# plugins/bio_ncbi_server.py
import json
import logging
import os
import socket
import urllib

import requests
from Bio import Entrez
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("Academic.Server")


ncbi_email = os.environ.get("NCBI_API_EMAIL", "scholar.navis.admin@example.com").strip()
ncbi_api_key = os.environ.get("NCBI_API_KEY", "").strip()
s2_api_key = os.environ.get("S2_API_KEY", "").strip()

Entrez.email = ncbi_email
Entrez.tool = "ScholarNavis"
if ncbi_api_key:
    Entrez.api_key = ncbi_api_key
    logger.info("NCBI API Key authenticated.")
else:
    logger.warning("No NCBI API Key. Rate limits apply.")

mcp = FastMCP("ScholarNavis-Academic-Plugin")

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
    logger.info(f"NCBI Email detected. Enabling NCBI MCP tools.")
    Entrez.email = ncbi_email
    if ncbi_api_key:
        Entrez.api_key = ncbi_api_key
    Entrez.tool = "ScholarNavis"


    @mcp.tool()
    def search_pubmed_literature(query: str, max_results: int = 5) -> str:
        """
        [Requires API Key] Search PubMed for scientific literature.
        Use this to search for broad topics (e.g., "male sterility") OR specific exact article titles to retrieve precise metadata (authors, journal, publication date, DOI).
        Returns metadata including PMID, Title, Abstract, DOI, and PMC links.
        CRITICAL: If the user asks for Open Access (OA) articles and download links, first use this tool to find relevant papers and extract their DOIs, then IMMEDIATELY pass those DOIs to the `fetch_open_access_pdf` tool.
        """
        logger.info(f"Task: PubMed Search | Query: '{query}'")
        try:
            search_handle = Entrez.esearch(db="pubmed", term=query, retmax=max_results)
            search_results = Entrez.read(search_handle)
            search_handle.close()

            ids = search_results.get("IdList", [])
            if not ids: return json.dumps({"status": "success", "results": [], "count": 0})

            summary_handle = Entrez.esummary(db="pubmed", id=",".join(ids))
            summaries = Entrez.read(summary_handle)
            summary_handle.close()

            parsed_results = []
            if isinstance(summaries, list):
                doc_list = summaries
            elif isinstance(summaries, dict) and "DocumentSummarySet" in summaries:
                doc_list = summaries["DocumentSummarySet"].get("DocumentSummary", [])
            else:
                doc_list = [summaries]

            for doc in doc_list:
                # 修复：增加 isinstance(aid, dict) 防御性检查
                doi_val = next(
                    (aid.get("Value", "") for aid in doc.get("ArticleIds", []) if
                     isinstance(aid, dict) and aid.get("IdType") == "doi"), "")
                pmc_val = next(
                    (aid.get("Value", "") for aid in doc.get("ArticleIds", []) if
                     isinstance(aid, dict) and aid.get("IdType") == "pmc"), "")
                final_doi = doc.get("DOI", "") or doi_val

                parsed_results.append({
                    "pmid": str(doc.get("Id", "")),
                    "title": doc.get("Title", ""),
                    "authors": list(doc.get("AuthorList", [])),
                    "journal": doc.get("Source", ""),
                    "pub_date": doc.get("PubDate", ""),
                    "doi": final_doi,
                    "pmc_id": pmc_val
                })
            return json.dumps({"status": "success", "results": parsed_results})
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})




    @mcp.tool()
    def fetch_sequence_fasta(accession_id: str, db_type: str = "nuccore") -> str:
        """
        Download raw FASTA sequences for nucleotides (DNA/RNA) or proteins.
        Use this WHENEVER the user requests exact genetic sequences, nucleotide codes (A, T, C, G), or protein amino acid sequences for specific accession IDs (e.g., NM_100000).
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
        Search the Sequence Read Archive (SRA) for high-throughput sequencing metadata.
        Use this to find raw multi-omics datasets (e.g., RNA-Seq, ChIP-Seq, WGS), biosample details, and Run IDs (SRR) associated with specific phenotypes, treatments, or organisms.
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
            if isinstance(summaries, list):
                doc_list = summaries
            elif isinstance(summaries, dict) and "DocumentSummarySet" in summaries:
                doc_list = summaries["DocumentSummarySet"].get("DocumentSummary", [])
            else:
                doc_list = [summaries]

            for doc in doc_list:
                exp_xml = doc.get("ExpXml", "")
                import re

                # Extract Run ID
                run_match = re.search(r'acc="([S|E|D]RR\d+)"', exp_xml)
                run_id = run_match.group(1) if run_match else "Unknown"

                # Safely extract Organism directly from ExpXml
                org_match = re.search(r'<Organism[^>]*>([^<]+)</Organism>', exp_xml)
                organism_name = org_match.group(1) if org_match else ""

                # Safely handle Biosample (which is usually a string)
                biosample_val = doc.get("Biosample", "")
                biosample_id = biosample_val if isinstance(biosample_val, str) else str(biosample_val)

                parsed_results.append({
                    "sra_id": doc.get("Id", ""),
                    "run_accession": run_id,
                    "title": doc.get("ExpTitle", ""),
                    "platform": doc.get("Instrument", ""),
                    "strategy": doc.get("Library_strategy", ""),
                    "organism": organism_name,
                    "biosample": biosample_id,
                    "create_date": doc.get("CreateDate", "")
                })

            return json.dumps({"status": "success", "results": parsed_results})
        except Exception as e:
            logger.error(f"SRA search encountered an error: {str(e)}")
            return json.dumps({"status": "error", "message": str(e)})


    @mcp.tool()
    def fetch_protein_summary(protein_query: str, organism: str = "") -> str:
        """
        Retrieve structural metadata, lengths, and basic summaries from the NCBI Protein database.
        Use this when the user asks about protein structure, sequence size, or taxonomy mapping for a specific protein target.
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
            if isinstance(summaries, list):
                doc_list = summaries
            elif isinstance(summaries, dict) and "DocumentSummarySet" in summaries:
                doc_list = summaries["DocumentSummarySet"].get("DocumentSummary", [])
            else:
                doc_list = [summaries]
            for doc in doc_list:
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
            if isinstance(summaries, list):
                doc_list = summaries
            elif isinstance(summaries, dict) and "DocumentSummarySet" in summaries:
                doc_list = summaries["DocumentSummarySet"].get("DocumentSummary", [])
            else:
                doc_list = [summaries]
            for doc in doc_list:
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
        [Requires API Key] Search Semantic Scholar for global academic papers.
        Highly effective for searching specific exact article titles (e.g., "Triptycene as a scaffold in metallocene catalyzed olefin polymerization") to retrieve exact metadata (authors, journal, date, citation count).
        Useful for cross-disciplinary queries.
        """
        logger.info(f"Task: S2 Search | Query: '{query}'")
        try:
            base_url = "https://api.semanticscholar.org/graph/v1/paper/search"
            headers = {"x-api-key": s2_api_key}
            params = {"query": query, "limit": max_results,
                      "fields": "title,authors,year,abstract,citationCount,isOpenAccess,url"}

            response = requests.get(base_url, params=params, headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()

            parsed_results = []
            for p in data.get("data", []):
                parsed_results.append({
                    "title": p.get("title", ""),
                    "year": p.get("year", "Unknown"),
                    "authors": [a["name"] for a in p.get("authors", [])][:5],
                    "citation_count": p.get("citationCount", 0),
                    "abstract": p.get("abstract", "No abstract")[:600] + "...",
                    "url": p.get("url", "")
                })
            return json.dumps({"status": "success", "results": parsed_results})
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})




else:
    logger.warning("S2_API_KEY is missing. Semantic Scholar MCP tool is disabled and hidden from LLM.")



@mcp.tool()
def search_pmc_literature_fallback(query: str, max_results: int = 5) -> str:
    """
    [Always Enabled / Fallback] Search PubMed Central (PMC) for Open Access literature.
    Use this if specific API keys for other search tools are missing or fail.
    """
    logger.info(f"Task: PMC Fallback Search | Query: '{query}'")
    try:
        search_handle = Entrez.esearch(db="pmc", term=query, retmax=max_results)
        search_results = Entrez.read(search_handle)
        search_handle.close()

        ids = search_results.get("IdList", [])
        if not ids: return json.dumps({"status": "success", "results": []})

        summary_handle = Entrez.esummary(db="pmc", id=",".join(ids))
        summaries = Entrez.read(summary_handle)
        summary_handle.close()

        if isinstance(summaries, list):
            doc_list = summaries
        elif isinstance(summaries, dict) and "DocumentSummarySet" in summaries:
            doc_list = summaries["DocumentSummarySet"].get("DocumentSummary", [])
        else:
            doc_list = [summaries]

        parsed_results = []
        for doc in doc_list:
            # 修复：增加 isinstance(aid, dict) 防御性检查
            doi_val = next(
                (aid.get("Value", "") for aid in doc.get("ArticleIds", []) if
                 isinstance(aid, dict) and aid.get("IdType") == "doi"), "")

            parsed_results.append({
                "pmcid": str(doc.get("Id", "")),
                "title": doc.get("Title", ""),
                "authors": list(doc.get("AuthorList", [])),
                "journal": doc.get("Source", ""),
                "pub_date": doc.get("PubDate", ""),
                "doi": doi_val
            })
        return json.dumps({"status": "success", "results": parsed_results})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

# OA 全文下载统一兜底 (PDF Retrieval Cascade)
@mcp.tool()
def fetch_open_access_pdf(doi: str) -> str:
    """
    [Always Enabled] Find a direct Open Access PDF download link for a given DOI.
    CRITICAL: Use this tool WHENEVER the user asks for full-text download links, OA links, or PDF links for an article.
    Cascade Strategy: Semantic Scholar (if Key) -> Unpaywall -> PubMed OA Web Service (PMC).
    """
    logger.info(f"Task: Fetch OA PDF | DOI: '{doi}'")
    try:
        clean_doi = doi.replace("https://doi.org/", "").replace("http://dx.doi.org/", "").strip()
        encoded_doi = urllib.parse.quote(clean_doi)
        landing_url = f"https://doi.org/{clean_doi}"  # 默认跳转链接

        # --- 1. Semantic Scholar (S2) 优先级最高 ---
        if s2_api_key:
            try:
                s2_url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{clean_doi}?fields=isOpenAccess,openAccessPdf"
                res = requests.get(s2_url, headers={"x-api-key": s2_api_key}, timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    if data.get("isOpenAccess") and data.get("openAccessPdf"):
                        pdf_url = data["openAccessPdf"].get("url", "")
                        if pdf_url:
                            return json.dumps({
                                "status": "success", "is_oa": True,
                                "pdf_url": pdf_url, "landing_page_url": landing_url,
                                "source": "Semantic Scholar"
                            })
            except Exception as e:
                logger.warning(f"S2 PDF fetch failed/missed: {e}")

        # --- 2. Unpaywall (免 Key，覆盖极广) ---
        try:
            unpaywall_url = f"https://api.unpaywall.org/v2/{encoded_doi}?email={ncbi_email}"
            response = requests.get(unpaywall_url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                if data.get("is_oa"):
                    best_oa = data.get("best_oa_location", {})
                    if best_oa and best_oa.get("url_for_pdf"):
                        return json.dumps({
                            "status": "success", "is_oa": True,
                            "pdf_url": best_oa.get("url_for_pdf"),
                            "landing_page_url": best_oa.get("url_for_landing_page", landing_url),
                            "source": "Unpaywall"
                        })
        except Exception as e:
            logger.warning(f"Unpaywall PDF fetch failed/missed: {e}")

        # --- 3. PubMed OA Web Service (PMC 官方极客保底) ---
        logger.info("Falling back to PubMed OA Web Service API...")
        try:
            conv_url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids={encoded_doi}&format=json&email={ncbi_email}"
            conv_res = requests.get(conv_url, timeout=5)

            if conv_res.status_code == 200:
                conv_data = conv_res.json()
                records = conv_data.get("records", [])
                if records and "pmcid" in records[0]:
                    pmcid = records[0]["pmcid"]

                    # 真正调用 PubMed OA API 获取官方直链
                    oa_api_url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmcid}"
                    oa_res = requests.get(oa_api_url, timeout=5)

                    if oa_res.status_code == 200 and "<OA>" in oa_res.text:
                        import re
                        official_pdf_url = ""
                        # 正则解析 XML 中官方给定的 PDF 下载节点
                        match = re.search(r'<link[^>]+format="pdf"[^>]+href="([^"]+)"', oa_res.text)

                        if match:
                            # NCBI 的 FTP 现在支持 HTTPS 直连，替换以兼容更多下载器
                            official_pdf_url = match.group(1).replace("ftp://", "https://")
                        else:
                            # 极端情况：OA 存在但 API 没给 pdf_url，用网页拼接保底
                            official_pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"

                        return json.dumps({
                            "status": "success", "is_oa": True,
                            "pdf_url": official_pdf_url,
                            "landing_page_url": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/",
                            "source": "PubMed_OA_WebService"
                        })
        except Exception as e:
            logger.warning(f"PMC fetch failed: {e}")

        # --- 最终结果：非 OA，仅提供跳转 ---
        return json.dumps({
            "status": "success",
            "is_oa": False,
            "landing_page_url": landing_url,
            "message": "The paper is paywalled (Closed Access). Only publisher redirect link is available."
        })

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

# 生物学基础数据工具 (基因、物种、组学等)
@mcp.tool()
def fetch_gene_information(gene_symbol: str, organism: str = "") -> str:
    """
    Query the NCBI Gene database to retrieve detailed gene descriptions, official symbols, and functional summaries.
    Use this when the user asks "What is the function of gene X?" or needs basic biological context and pathways about a specific gene in a given organism.
    """
    try:
        term = f"{gene_symbol}[Gene Name]" + (f" AND {organism}[Organism]" if organism else "")
        search_handle = Entrez.esearch(db="gene", term=term, retmax=3)
        ids = Entrez.read(search_handle).get("IdList", [])
        search_handle.close()
        if not ids: return json.dumps({"status": "success", "results": []})

        summary_handle = Entrez.esummary(db="gene", id=",".join(ids))
        summaries = Entrez.read(summary_handle).get("DocumentSummarySet", {}).get("DocumentSummary", [])
        summary_handle.close()

        res = [{"symbol": d.get("Name"), "summary": d.get("Summary")} for d in summaries]
        return json.dumps({"status": "success", "results": res})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def fetch_pubmed_abstract(pmid: str) -> str:
    """
    [Core Academic Tool] Fetch the full-text abstract of a specific PubMed article.
    Use this when the user asks for details or a summary of a specific paper.
    """
    logger.info(f"Task: Fetch Abstract | PMID: {pmid}")
    try:
        handle = Entrez.efetch(db="pubmed", id=pmid, rettype="abstract", retmode="text")
        abstract_text = handle.read()
        handle.close()

        if not abstract_text.strip():
            return json.dumps({"status": "warning", "message": "No abstract available for this PMID."})

        return json.dumps({"status": "success", "pmid": pmid, "abstract": abstract_text.strip()})
    except Exception as e:
        logger.error(f"Failed to fetch abstract for {pmid}: {e}")
        return json.dumps({"status": "error", "message": str(e)})

if __name__ == "__main__":
    logger.info("Academic MCP Server initialized.")
    mcp.run(transport='stdio')