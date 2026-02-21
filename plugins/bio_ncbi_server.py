# plugins/bio_ncbi_server.py
import os
import json
import logging
import sys
import traceback
from datetime import datetime
from Bio import Entrez
from mcp.server.fastmcp import FastMCP

# Setup logging
log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f"mcp_ncbi_server_{datetime.now().strftime('%Y%m%d')}.log")

logger = logging.getLogger("NCBI.Server")
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setFormatter(formatter)
logger.addHandler(stderr_handler)

file_handler = logging.FileHandler(log_file, encoding='utf-8')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Load NCBI credentials from environment variables injected by the main process
Entrez.email = os.environ.get("NCBI_API_EMAIL", "scholar.navis.admin@example.com")
api_key = os.environ.get("NCBI_API_KEY", "")
if api_key:
    Entrez.api_key = api_key
    logger.info("NCBI API Key successfully authenticated.")
else:
    logger.warning("No NCBI API Key detected. Performance may be restricted by rate limits.")

Entrez.tool = "ScholarNavis"
mcp = FastMCP("ScholarNavis-NCBI-Plugin")


@mcp.tool()
def search_pubmed_literature(query: str, max_results: int = 5) -> str:
    """
    Search PubMed for scientific literature and return detailed metadata.
    Includes DOI, full author lists, and publication dates for academic citations.
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
            parsed_results.append({
                "pmid": doc.get("Id", ""),
                "title": doc.get("Title", ""),
                "authors": list(doc.get("AuthorList", [])),
                "journal": doc.get("Source", ""),
                "pub_date": doc.get("PubDate", ""),
                "doi": doc.get("DOI", ""),
                "full_journal_name": doc.get("FullJournalName", "")
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


if __name__ == "__main__":
    logger.info("NCBI MCP Server initialized and ready for transport.")
    mcp.run(transport='stdio')