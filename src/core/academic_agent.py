"""Compatibility layer: academic toolchain split into ``src/core/academic/``.

Implementation modules:
- base: env keys, workspace, UDP-MCP bridge, retry helpers
- literature: S2 / OpenAlex / Crossref / citation graph / PDF fetch
- ncbi: E-utilities sequences, taxonomy, omics datasets, GBIF
- bio_databases: KEGG, GO, ChEMBL, UniProt, AlphaFold, PDB, HMDB, JASPAR, Ensembl
- web_tools: webpage fetch, web search, preprints, Wikipedia, GitHub
- plotting: chart rendering helpers

This module re-exports the whole public surface so legacy imports
(``from src.core.academic_agent import X``) keep working.
"""

from src.core.academic import (
    WORKSPACE_DIR,
    UdpJsonHandler,
    analyze_systems_network,
    fetch_alphafold_structure,
    fetch_go_annotations,
    fetch_open_access_pdf,
    fetch_sequence_fasta,
    fetch_taxonomy_info,
    fetch_webpage_content,
    fetch_wikipedia_summary,
    get_setting_or_env,
    github_token,
    is_ncbi_email_valid,
    is_ncbi_enabled,
    logger,
    mcp_request,
    ncbi_api_key,
    ncbi_email,
    openalex_api_key,
    plot_chart,
    query_ensembl_database,
    query_kegg_database,
    query_metabolite_database,
    query_pdb_structure,
    query_plant_multiomics,
    query_uniprot_database,
    s2_api_key,
    search_academic_literature,
    search_chembl_target,
    search_gbif_occurrences,
    search_github_repos,
    search_jaspar_motifs,
    search_omics_datasets,
    search_preprints,
    search_web,
    simple_retry,
    traverse_citation_graph,
    uniprot_id_mapping,
    universal_ncbi_summary,
)

__all__ = [
    "WORKSPACE_DIR",
    "UdpJsonHandler",
    "analyze_systems_network",
    "fetch_alphafold_structure",
    "fetch_go_annotations",
    "fetch_open_access_pdf",
    "fetch_sequence_fasta",
    "fetch_taxonomy_info",
    "fetch_webpage_content",
    "fetch_wikipedia_summary",
    "get_setting_or_env",
    "github_token",
    "is_ncbi_email_valid",
    "is_ncbi_enabled",
    "logger",
    "mcp_request",
    "ncbi_api_key",
    "ncbi_email",
    "openalex_api_key",
    "plot_chart",
    "query_ensembl_database",
    "query_kegg_database",
    "query_metabolite_database",
    "query_pdb_structure",
    "query_plant_multiomics",
    "query_uniprot_database",
    "s2_api_key",
    "search_academic_literature",
    "search_chembl_target",
    "search_gbif_occurrences",
    "search_github_repos",
    "search_jaspar_motifs",
    "search_omics_datasets",
    "search_preprints",
    "search_web",
    "simple_retry",
    "traverse_citation_graph",
    "uniprot_id_mapping",
    "universal_ncbi_summary",
]

if __name__ == "__main__":
    logger.info(
        "academic_agent.py is a compatibility re-export layer; "
        "implementations live in src/core/academic/"
    )
