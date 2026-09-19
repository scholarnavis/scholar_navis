"""Bio-database tools: GBIF, KEGG, QuickGO, ChEMBL, UniProt, AlphaFold, PDB,
metabolites (PubChem/ChEBI), STRING/g:Profiler, plant multiomics, JASPAR, Ensembl."""
import json
import re
import time
import urllib.parse
from typing import Literal

from src.core.academic.base import logger, mcp_request, simple_retry

__all__ = [
    "search_gbif_occurrences", "query_kegg_database", "fetch_go_annotations",
    "search_chembl_target", "uniprot_id_mapping", "query_uniprot_database",
    "fetch_alphafold_structure", "query_pdb_structure",
    "query_metabolite_database", "analyze_systems_network",
    "query_plant_multiomics", "search_jaspar_motifs", "query_ensembl_database",
]


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

            query_total = max(len(clean_identifiers), 1)
            results = []
            for item in data:
                inter = item.get("intersection_size", 0)
                results.append({
                    "source": item.get("source", ""),
                    "term_id": item.get("native", ""),
                    "description": item.get("name", ""),
                    "p_value": item.get("p_value", 1.0),
                    "fdr": item.get("p_value", 1.0),
                    "intersection_size": inter,
                    "gene_ratio": round(inter / query_total, 4),
                })

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
