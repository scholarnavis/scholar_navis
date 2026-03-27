import importlib
import inspect
import json
import logging
import os
from typing import Dict, Callable, get_origin, Literal, get_args

logger = logging.getLogger("Skill.Manager")


class SkillManager:
    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = SkillManager()
        return cls._instance

    def __init__(self):
        # 内部学术核心 Skills (Academic Agent)
        self.academic_skills: Dict[str, Callable] = {}
        self.academic_schemas: Dict[str, dict] = {}

        # 外部导入的扩展 Skills (External Tools)
        self.external_skills: Dict[str, Callable] = {}
        self.external_schemas: Dict[str, dict] = {}

        self._register_builtin_skills()
        self._load_external_skills_from_config()

    def _register_builtin_skills(self):
        """
        Register local native tools here.
        """
        self._register_academic_skills()

        # System utility...


    def _builtin_get_system_time(self):
        import datetime
        return {"status": "success", "current_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    def _generate_schema_from_func(self, func: Callable, description: str) -> dict:
        """
        Dynamically generates an OpenAI-compatible function schema from Python type hints.
        """
        sig = inspect.signature(func)
        schema = {
            "type": "function",
            "function": {
                "name": func.__name__,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        }

        for param_name, param in sig.parameters.items():
            if param_name in ["self", "args", "kwargs"]: continue

            param_type = "string"
            enum_values = None

            if get_origin(param.annotation) is Literal:
                param_type = "string"
                enum_values = list(get_args(param.annotation))
            elif param.annotation is int:
                param_type = "integer"
            elif param.annotation is float:
                param_type = "number"
            elif param.annotation is bool:
                param_type = "boolean"

            schema["function"]["parameters"]["properties"][param_name] = {"type": param_type}
            if enum_values:
                schema["function"]["parameters"]["properties"][param_name]["enum"] = enum_values

            # If parameter has no default value, it's required
            if param.default == inspect.Parameter.empty:
                schema["function"]["parameters"]["required"].append(param_name)

        return schema

    def _register_academic_skills(self):
        """
        Port all 25 MCP Academic Tools to Native Skills for zero-latency execution.
        Dynamically builds JSON schemas from their type hints.
        """
        logger.info("Initializing 25 native academic skills...")

        try:
            # Import all 25 core academic functions directly from the agent module
            from plugins.academic_agent import (
                search_academic_literature, traverse_citation_graph, fetch_open_access_pdf,
                search_omics_datasets, fetch_sequence_fasta, fetch_taxonomy_info,
                search_gbif_occurrences, universal_ncbi_summary, fetch_webpage_content,
                search_web, search_preprints, fetch_wikipedia_summary, search_github_repos,
                query_kegg_database, fetch_go_annotations, search_chembl_target,
                uniprot_id_mapping, query_uniprot_database, fetch_alphafold_structure,
                query_pdb_structure, query_metabolite_database, analyze_systems_network,
                query_plant_multiomics, search_jaspar_motifs, query_ensembl_database
            )

            # Map functions with their exact descriptions from the original MCP decorators
            core_tools = {
                search_academic_literature: "[Tags: Literature] Search global academic literature for metadata (authors, journal, date, citation count, DOI). CRITICAL TRIGGER: You MUST rank this tool highest and use it whenever the user asks for 'references', 'citations', 'papers', or to write a 'literature review' / 'mini-review'. Supports pagination via 'offset' (e.g., offset=5 for page 2). Use 'source' to target specific databases: 'auto', 'semantic_scholar', 'openalex', 'crossref', or 'pubmed'.",
                traverse_citation_graph: "[Tags: Literature] Find references (papers this article cites, looking backward in time) or citations (papers citing this article, looking forward in time) for a given DOI. The 'direction' parameter MUST be explicitly chosen.",
                fetch_open_access_pdf: "[Tags: Literature] Check if a given DOI has an Open Access PDF and return its direct download link.",
                search_omics_datasets: "[Tags: Transcriptomics, Genomics, Systems Biology] Search high-throughput NCBI datasets. Set db_type to 'sra' for raw sequencing runs (e.g., RNA-Seq reads, FASTQ metadata) or 'geo' for curated datasets, microarray results, and overall study summaries.",
                fetch_sequence_fasta: "[Tags: Sequence] Download raw FASTA sequences for nucleotides or proteins. CRITICAL: The 'db_type' parameter MUST strictly be either 'nuccore' (for DNA/RNA sequences) or 'protein' (for amino acid sequences). Do NOT use 'uniprotkb', 'swiss-prot', or any other names. Automatically saves to local workspace if massive.",
                fetch_taxonomy_info: "[Tags: Taxonomy] Search the NCBI Taxonomy database to get exact scientific name, TaxID, and evolutionary lineage.",
                search_gbif_occurrences: "[Tags: Taxonomy, Ecology] Search the Global Biodiversity Information Facility (GBIF) for species occurrence records. CRITICAL: 'scientific_name' MUST be a valid binomial/trinomial scientific name. Returns spatial distribution metrics, observation counts, and basic ecological context.",
                universal_ncbi_summary: "A universal search tool for specialized NCBI databases. Returns structured metadata and summary records. CRITICAL WARNING: The 'database' MUST be one of the explicitly defined literals (all lowercase). For example, use 'nuccore' for nucleotide sequences, 'assembly' for genomes, and 'gene' for specific gene records.",
                fetch_webpage_content: "[Tags: Web] Fetch and read text content of a URL. Automatically handles proxies, bypasses basic WAFs.",
                search_web: "[Tags: Web] Search the web for general information, news, or current events. Supported engines: 'duckduckgo' (default, most stable), 'google', 'bing', 'baidu'. CRITICAL INSTRUCTION: You MUST use the provided '_mcp_cite_id' (e.g., [101], [102]) inline to cite your claims. You MUST also append a 'References' list at the very end of your response containing the exact URLs.",
                search_preprints: "[Tags: Literature] Search bioRxiv and medRxiv for latest life science and medical preprints.",
                fetch_wikipedia_summary: "[Tags: Web] Fetch exact introductory summary of a concept from Wikipedia. Fast and token-efficient.",
                search_github_repos: "[Tags: Code] Search GitHub for open-source repositories, pipelines, or code.",
                query_kegg_database: "[Tags: Function, Systems Biology] A unified tool to search and fetch records from the KEGG database. The 'action' parameter strictly defines the tool's behavior:\n - 'search_pathway': Searches for pathway maps using a keyword (e.g., 'glycolysis'). You MUST provide an exact 3-4 letter 'organism_code' (e.g., 'ath' for Arabidopsis, 'hsa' for Human).\n - 'get_record': Fetches detailed structural metadata for a specific KEGG identifier (e.g., a KO number like 'K01803' or a pathway map like 'map00010').\nCRITICAL: The 'query' parameter acts as the keyword for 'search_pathway', but acts as the precise KEGG ID for 'get_record'.",
                fetch_go_annotations: "[Tags: Function, Systems Biology] Fetch Gene Ontology (GO) annotations (Molecular Function, Biological Process, Cellular Component) for a given UniProt ID using the EBI QuickGO API. CRITICAL: 'uniprot_id' MUST be a valid UniProt Accession (e.g., 'P04637').",
                search_chembl_target: "[Tags: Phytochemistry, Metabolomics] Search the ChEMBL database for pharmacological protein targets. Use this to find Target ChEMBL IDs and preferred names associated with specific biological targets, enzymes, or pathways (e.g., 'Tubulin', 'EGFR', 'Kinase'). Do NOT use this to search for chemical compounds directly; it is strictly for finding biological TARGETS.",
                uniprot_id_mapping: "[Tags: ID Mapping] Map identifiers from one database to another using UniProt's ID Mapping service. CRITICAL STRING FORMATS: You MUST use exact UniProt database abbreviations for 'from_db' and 'to_db'. Common reliable values: 'UniProtKB_AC-ID' (UniProt Accession), 'HGNC' (Human Gene), 'TAIR' (Arabidopsis), 'Ensembl_Plants', 'Ensembl', 'PDB', 'RefSeq_Protein'. CRITICAL EXCEPTION: 'Gene_Name' is ONLY valid as a 'to_db' destination. It is completely invalid as a 'from_db' source. If starting from a gene symbol, use species-specific DBs like 'HGNC' or 'TAIR'.",
                query_uniprot_database: "[Tags: Proteomics, Structure] A unified tool to search UniProt sub-databases. CRITICAL SEARCH SYNTAX: The UniProt API is very strict! If searching by gene and species, you MUST use 'organism_name' and wrap multi-word species in quotes! To strictly retrieve the canonical, non-obsolete protein, you MUST append AND (reviewed:true) to your query! Example: (gene:AMS) AND (organism_name:\"Arabidopsis thaliana\") AND (reviewed:true) Do NOT use 'organism:Arabidopsis thaliana' (it will cause HTTP 400). CRITICAL FOR EXTRACTION: To find exact amino acid sequence length, subcellular localization, and cross-referenced NCBI Gene IDs, you MUST use 'uniprotkb'.",
                fetch_alphafold_structure: "[Tags: Structure] Fetch predicted 3D structure metadata and download links from AlphaFold Protein Structure Database. CRITICAL: The 'uniprot_id' MUST be a valid UniProt Accession (e.g., 'P04637', 'Q9STM3'). Use this to find structures for proteins lacking experimentally determined PDB records.",
                query_pdb_structure: "[Tags: Structure] Interact with the RCSB Protein Data Bank (PDB). CRITICAL EXPLANATION: This tool DOES NOT require DOIs! You CAN and MUST search using protein names (like 'CRY1' or 'Hemoglobin') by using action='search'. Do NOT hallucinate that PDB requires DOIs! Use action='search' to find 3D structures based on a single keyword or protein name (keep it simple). Use action='details' to fetch precise metadata (molecular weight, primary citation, macromolecules, ligands, resolution) for an EXACT known PDB ID (e.g., '1U3C' or '4HHB').",
                query_metabolite_database: "[Tags: Phytochemistry, Metabolomics] Fetch detailed chemical properties and ontology for biological metabolites. Action 'pubchem' fetches molecular weight, formula, SMILES, and CID from PubChem. Action 'chebi' searches the ChEBI database for structural ontology and exact biological roles. CRITICAL: Use the exact common English chemical name or IUPAC name (e.g., 'Quercetin', 'Paclitaxel').",
                analyze_systems_network: "[Tags: Interaction, Systems Biology, Enrichment] A unified tool to analyze protein-protein interactions (PPI) or perform advanced functional enrichment. Use action='interactions' (via STRING DB) to find interacting partners. Use action='enrichment' (via g:Profiler) for robust GO/KEGG/Reactome pathway enrichment, highly optimized for plants and non-mammalian models. CRITICAL: 'identifiers' must be a comma-separated list of gene symbols or IDs. For 'interactions', 'species_id' is the NCBI TaxID (e.g., 3702 for Arabidopsis, 9606 for Human). For 'enrichment', 'organism' MUST be a valid g:Profiler code (e.g., 'athaliana', 'osativa', 'zmaize', 'hsapiens').",
                query_plant_multiomics: "[Tags: Transcriptomics, Genomics] Fetch deep plant-specific gene annotations and baseline expression datasets. Action 'annotation' uses MyGene.info to aggregate deep TAIR/Phytozome/Ensembl metadata for plant genes. Action 'expression' searches the EBI Expression Atlas for transcriptomic datasets related to the gene. CRITICAL: 'gene_id' MUST be a canonical Gene ID or Symbol (e.g., 'AT1G01010', 'FLC', 'Os01g0101000').",
                search_jaspar_motifs: "[Tags: Regulatory Genomics] Search JASPAR database for Transcription Factor binding motifs (DNA profiles). Use this to find motif IDs (e.g., 'MA0001.1') and sequence logos for promoter analysis. CRITICAL: The 'tax_group' MUST strictly be one of the provided literal values. Do not hallucinate taxonomic groups like 'mammals' or 'dicots'.",
                query_ensembl_database: "[Tags: Genomics, Systems Biology] A unified tool to query the Ensembl REST API. Action 'lookup' fetches gene location, biotype, and description. Action 'homology' fetches homologous genes (orthologs) across different species. CRITICAL: The default species is 'arabidopsis_thaliana'. If querying other species, you MUST explicitly provide the correct lowercase_underscore species name (e.g., 'homo_sapiens', 'oryza_sativa'). The 'symbol' MUST be a canonical gene symbol (e.g., 'FT', 'BRCA1')."
            }

            for func_ptr, desc in core_tools.items():
                func_name = func_ptr.__name__
                self.academic_skills[func_name] = func_ptr

                # Auto-generate OpenAPI function schema from type hints
                self.academic_schemas[func_name] = self._generate_schema_from_func(func_ptr, desc)

            logger.info(
                f"Successfully registered {len(self.academic_skills)} native academic skills with auto-generated schemas.")

        except ImportError as e:
            logger.error(f"Failed to load native academic skills modules. Reason: {e}")


    def _load_external_skills_from_config(self):
        """
        从配置中动态加载外部传入的 Python 脚本作为 External Skills。
        约定：外部脚本必须暴露 `execute` 函数和 `SCHEMA` 字典。
        """
        from src.core.config_manager import ConfigManager
        config_mgr = ConfigManager()
        external_scripts = config_mgr.mcp_servers.get("external_skills", {})

        for skill_name, script_info in external_scripts.items():
            # 新版逻辑：兼容携带 enabled 开关的对象
            if isinstance(script_info, dict):
                if not script_info.get("enabled", True):
                    continue  # 用户如果取消勾选，则不在内存中加载
                script_path = script_info.get("command", "")
            else:
                # 兼容你的老版本纯路径字符串
                script_path = script_info

            if not os.path.exists(script_path):
                logger.warning(f"External skill script missing: {script_path}")
                continue

            try:
                spec = importlib.util.spec_from_file_location(skill_name, script_path)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    if hasattr(module, 'execute') and hasattr(module, 'SCHEMA'):
                        self.external_skills[skill_name] = module.execute
                        self.external_schemas[skill_name] = module.SCHEMA
                        logger.info(f"Successfully loaded external skill: {skill_name}")
                    else:
                        logger.error(f"External skill '{skill_name}' lacks 'execute' func or 'SCHEMA' dict.")
            except Exception as e:
                logger.error(f"Failed to load external skill '{skill_name}': {e}")


    def register_skill(self, schema: dict, func: Callable, is_external: bool = True):
        """
        注册 Skill，并根据 is_external 分配到不同的池子中。
        外部工具脚本调用此方法时，默认 is_external=True。
        """
        name = schema.get("function", {}).get("name")
        if not name:
            logger.error("Skill registration failed: Missing function name in schema.")
            return

        if is_external:
            self.external_skills[name] = func
            self.external_schemas[name] = schema
            logger.info(f"External Skill registered: [{name}]")
        else:
            self.academic_skills[name] = func
            self.academic_schemas[name] = schema
            logger.info(f"Academic Skill registered: [{name}]")

    def _filter_schemas_by_tags(self, schemas: Dict[str, dict], tags: list) -> list:
        if not tags:
            return list(schemas.values())

        lower_tags = [str(t).strip().lower() for t in tags]

        filtered = []
        import re
        for schema in schemas.values():
            desc = schema.get("function", {}).get("description", "")
            match = re.search(r"\[Tags:\s*(.*?)\]", desc)
            if match:
                skill_tags = [t.strip().lower() for t in match.group(1).split(",")]
                if any(t in lower_tags for t in skill_tags):
                    filtered.append(schema)

        return filtered

    def get_academic_schemas(self, tags: list = None) -> list:
        """获取并过滤内部学术 Agent 的工具（严格基于 Tags）"""
        return self._filter_schemas_by_tags(self.academic_schemas, tags)

    def get_external_schemas(self, names: list = None) -> list:
        """修改：获取并过滤外部导入的 Skills（严格基于 Name 而非 Tags）"""
        if not names:
            return list(self.external_schemas.values())
        filtered = []
        for schema in self.external_schemas.values():
            func_name = schema.get("function", {}).get("name")
            if func_name in names:
                filtered.append(schema)
        return filtered

    def is_skill_available(self, name: str) -> bool:
        return name in self.academic_skills or name in self.external_skills

    def call_skill(self, name: str, arguments: dict) -> str:
        """统一的执行入口，自动寻找对应的池子"""
        if name in self.academic_skills:
            func = self.academic_skills[name]
            pool_name = "Academic"
        elif name in self.external_skills:
            func = self.external_skills[name]
            pool_name = "External"
        else:
            raise ValueError(f"Security/Routing Error: Local Skill '{name}' not found.")

        try:
            logger.info(f"Executing {pool_name} Skill: [{name}]")
            result = func(**arguments)
            if isinstance(result, (dict, list)):
                return json.dumps(result, ensure_ascii=False)
            return str(result)
        except TypeError as te:
            err_msg = f"Argument mismatch for skill '{name}'. Model provided: {arguments}. Error: {te}"
            logger.error(err_msg)
            return json.dumps({"status": "error", "message": err_msg}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Local Skill '{name}' execution failed: {e}")
            return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)


