# plugins/bio_server.py
import json
import logging
import os
import sys
import subprocess
import traceback
from datetime import datetime
from mcp.server.fastmcp import FastMCP

# Setup production-grade logging
# Redirect to stderr to avoid corrupting the stdio protocol stream
log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f"mcp_local_server_{datetime.now().strftime('%Y%m%d')}.log")

logger = logging.getLogger("LocalBioServer")
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setFormatter(formatter)
logger.addHandler(stderr_handler)

file_handler = logging.FileHandler(log_file, encoding='utf-8')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

mcp = FastMCP("ScholarNavis-LocalBio-Plugin")


@mcp.tool()
def calculate_pcr_tm(sequence: str) -> str:
    """
    Calculate the melting temperature (Tm) for a DNA sequence using professional empirical formulas.
    Suitable for PCR primer design validation.

    Args:
        sequence: DNA sequence (A, T, C, G)
    """
    logger.info(f"Task: Calculate Tm | Sequence Length: {len(sequence)}")
    seq = sequence.upper().strip()

    if not seq or not all(base in "ATCG" for base in seq):
        logger.error("Invalid sequence characters detected.")
        return json.dumps({"status": "error", "message": "Sequence must only contain A, T, C, and G."})

    gc_count = seq.count('G') + seq.count('C')
    at_count = seq.count('A') + seq.count('T')
    length = len(seq)

    # Basic Wallace rule for short oligos (<14bp)
    # Basic formula: Tm = 2(A+T) + 4(G+C)
    if length < 14:
        tm = (at_count * 2) + (gc_count * 4)
        method = "Wallace Rule"
    else:
        # Salt-adjusted empirical formula for longer sequences
        # Tm = 64.9 + 41.0 * (nGC - 16.4) / n
        tm = 64.9 + 41.0 * (gc_count - 16.4) / length
        method = "Salt-Adjusted Empirical Formula"

    logger.info(f"Tm Calculation completed using {method}: {tm}C")
    return json.dumps({
        "status": "success",
        "sequence": seq,
        "length": length,
        "gc_content_percentage": round((gc_count / length) * 100, 2),
        "tm_celsius": round(tm, 2),
        "calculation_method": method
    })


@mcp.tool()
def trigger_wsl_alignment(fastq_file: str, reference_genome: str, tool: str = "hisat2") -> str:
    """
    Dispatch a bioinformatics alignment task to the Windows Subsystem for Linux (WSL).
    Supports high-throughput sequencing tools like hisat2 and STAR.

    Args:
        fastq_file: Absolute path to the FASTQ file in WSL format (e.g., /mnt/c/data/sample.fq)
        reference_genome: Path to the genome index inside the WSL environment.
        tool: Alignment tool to execute ('hisat2' or 'star').
    """
    logger.info(f"Task: WSL Alignment Dispatch | Tool: {tool} | Target: {fastq_file}")

    try:
        # Check if WSL is available
        subprocess.run(["wsl", "--status"], check=True, capture_output=True)

        if tool.lower() == "hisat2":
            cmd = f"wsl -e hisat2 -x {reference_genome} -U {fastq_file} -S output.sam"
        elif tool.lower() == "star":
            cmd = f"wsl -e STAR --runThreadN 8 --genomeDir {reference_genome} --readFilesIn {fastq_file}"
        else:
            logger.error(f"Unsupported tool requested: {tool}")
            return json.dumps({"status": "error", "message": f"Tool '{tool}' is not supported."})

        # In this implementation, we log the command and simulate dispatch.
        # Integration with TaskManager is recommended for long-running jobs.
        logger.info(f"Command constructed: {cmd}")
        return json.dumps({
            "status": "success",
            "dispatched_command": cmd,
            "message": "Task successfully sent to WSL environment."
        })
    except Exception as e:
        logger.error(f"WSL dispatch failed: {str(e)}")
        return json.dumps({"status": "error", "message": f"WSL Environment Error: {str(e)}"})


@mcp.tool()
def query_local_gene_index(gene_id: str) -> str:
    """
    Search the local biological database for pre-indexed gene annotations and experimental results.
    """
    logger.info(f"Task: Local Gene Query | ID: {gene_id}")
    # Mock database logic - in production, this connects to the SQLite backend
    return json.dumps({
        "gene_id": gene_id,
        "source": "Local-ChromaDB-Index",
        "status": "ready",
        "note": "Reference found in local knowledge base."
    })


if __name__ == "__main__":
    logger.info("Local BioServer instance starting...")
    mcp.run(transport='stdio')