"""NCBI/Entrez-based tools: omics datasets, sequences, taxonomy and summaries."""
import json
import os
import re
import urllib.parse
from typing import Literal

from Bio import Entrez

from src.core.academic.base import (
    logger, simple_retry, global_rate_limiter,
    ncbi_api_key, is_ncbi_enabled, WORKSPACE_DIR,
)

__all__ = [
    "search_omics_datasets", "fetch_sequence_fasta",
    "fetch_taxonomy_info", "universal_ncbi_summary",
]


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
