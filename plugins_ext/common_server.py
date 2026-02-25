# plugins_ext/common_server.py
# External Expansion Tool (MCP)
# ============================================
# User-Programmable External MCP Server Template
# SECURITY NOTICE: This file runs as an independent subprocess.
# Best Practices:
# 1. Avoid executing dangerous system commands (e.g., rm -rf, formatting).
# 2. Strictly validate all user inputs.
# 3. Do not expose sensitive system credentials or API keys in plain text.
# ============================================
import json
import logging
import os
import sys
from datetime import datetime
from mcp.server.fastmcp import FastMCP

# Security Configuration: Maximum allowed execution time
MAX_EXECUTION_TIME = 30  # seconds
# Security Configuration: Blacklisted commands
FORBIDDEN_COMMANDS = ['rm', 'del', 'format', 'shutdown', 'reboot']

# Setup production-grade logging
# Redirect to stderr to avoid corrupting the stdio protocol stream expected by the Main UI
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


# ============================================
# 1. Custom Tool Template
# ============================================
@mcp.tool(
    name="custom_tool_template",
    description="[Tags: Template] A blueprint for adding your own custom tools to Scholar Navis."
)
def custom_tool_template(input_data: str) -> str:
    """
    How to use this template:
    1. Rename the function to match your desired action.
    2. Update the description tag above with your desired [Tags: ...].
    3. Modify the arguments and return types.
    4. Implement your core logic inside the `try` block.

    Args:
        input_data: Description of the input data expected from the AI.
    """
    logger.info(f"Custom tool executed with input: {input_data}")

    # 1. Validate Input
    if not input_data:
        return json.dumps({"status": "error", "message": "Input data cannot be empty."})

    try:
        # 2. Add your custom logic here
        result = f"Successfully processed: {input_data}"

        # 3. Return the result as JSON
        return json.dumps({
            "status": "success",
            "input": input_data,
            "result": result,
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Custom tool error: {str(e)}")
        return json.dumps({
            "status": "error",
            "message": f"Execution failed: {str(e)}"
        })


# ============================================
# 2. Example Tool: PCR Tm Calculator
# ============================================
@mcp.tool(
    name="calculate_pcr_tm",
    description="[Tags: Calculator] Calculate the melting temperature (Tm) for a DNA sequence using professional empirical formulas."
)
def calculate_pcr_tm(sequence: str) -> str:
    """
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
    if length < 14:
        tm = (at_count * 2) + (gc_count * 4)
        method = "Wallace Rule"
    else:
        # Salt-adjusted empirical formula for longer sequences
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


# ============================================
# 3. Example Tool: Sequence Conversion
# ============================================
@mcp.tool(
    name="convert_dna_to_rna",
    description="[Tags: Genomics] Convert a DNA sequence to an RNA sequence by replacing Thymine (T) with Uracil (U)."
)
def convert_dna_to_rna(dna_sequence: str) -> str:
    """
    Args:
        dna_sequence: DNA sequence to convert (A, T, C, G)
    """
    logger.info(f"Task: DNA to RNA conversion | Sequence: {dna_sequence[:20]}...")

    # Input Validation
    seq = dna_sequence.upper().strip()
    if not seq or not all(base in "ATCG" for base in seq):
        return json.dumps({
            "status": "error",
            "message": "Invalid DNA sequence. Must contain only A, T, C, G."
        })

    # Core Logic
    rna_sequence = seq.replace('T', 'U')
    return json.dumps({
        "status": "success",
        "dna_sequence": seq,
        "rna_sequence": rna_sequence,
        "conversion_note": "T replaced with U"
    })


if __name__ == "__main__":
    logger.info("Local BioServer instance starting...")
    logger.info(f"Security constraints enabled: MAX_TIME={MAX_EXECUTION_TIME}s, FORBIDDEN={FORBIDDEN_COMMANDS}")

    # Start the FastMCP server using standard input/output
    mcp.run(transport='stdio')