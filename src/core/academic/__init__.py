"""Academic agent tool pool, split from the former academic_agent.py monolith."""
from src.core.academic.base import (
    UdpJsonHandler, logger, get_setting_or_env,
    ncbi_email, ncbi_api_key, openalex_api_key, s2_api_key, github_token,
    is_ncbi_email_valid, is_ncbi_enabled, WORKSPACE_DIR, mcp_request, simple_retry,
)
from src.core.academic.literature import (
    search_academic_literature, traverse_citation_graph,
    fetch_open_access_pdf, search_preprints,
)
from src.core.academic.ncbi import (
    search_omics_datasets, fetch_sequence_fasta,
    fetch_taxonomy_info, universal_ncbi_summary,
)
from src.core.academic.bio_databases import (
    search_gbif_occurrences, query_kegg_database, fetch_go_annotations,
    search_chembl_target, uniprot_id_mapping, query_uniprot_database,
    fetch_alphafold_structure, query_pdb_structure, query_metabolite_database,
    analyze_systems_network, query_plant_multiomics, search_jaspar_motifs,
    query_ensembl_database,
)
from src.core.academic.web_tools import (
    fetch_webpage_content, search_web, fetch_wikipedia_summary, search_github_repos,
)
from src.core.academic.plotting import plot_chart

__all__ = [
    "UdpJsonHandler", "logger", "get_setting_or_env",
    "ncbi_email", "ncbi_api_key", "openalex_api_key", "s2_api_key", "github_token",
    "is_ncbi_email_valid", "is_ncbi_enabled", "WORKSPACE_DIR",
    "mcp_request", "simple_retry",
    "search_academic_literature", "traverse_citation_graph",
    "fetch_open_access_pdf", "search_preprints",
    "search_omics_datasets", "fetch_sequence_fasta",
    "fetch_taxonomy_info", "universal_ncbi_summary",
    "search_gbif_occurrences", "query_kegg_database", "fetch_go_annotations",
    "search_chembl_target", "uniprot_id_mapping", "query_uniprot_database",
    "fetch_alphafold_structure", "query_pdb_structure", "query_metabolite_database",
    "analyze_systems_network", "query_plant_multiomics", "search_jaspar_motifs",
    "query_ensembl_database",
    "fetch_webpage_content", "search_web", "fetch_wikipedia_summary", "search_github_repos",
    "plot_chart",
]
