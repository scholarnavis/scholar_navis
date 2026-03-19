import requests
import json
import sys

# --- API Configuration Parameters ---
BASE_URL = "http://127.0.0.1:8000"
HEADERS = {"Content-Type": "application/json"}
HEADERS["Authorization"] = "Bearer 123456"


def execute_automated_health_checks():
    """
    Executes automated GET requests against peripheral RESTful endpoints
    to verify system state, model providers, knowledge bases, and MCP tools.
    """
    print("\n" + "=" * 60)
    print(" PHASE 1: AUTOMATED API ENDPOINT VERIFICATION")
    print("=" * 60)

    try:
        # 1. System State Validation
        res_state = requests.get(f"{BASE_URL}/api/state", headers=HEADERS).json()
        print(f"[✔] System State   : Active Device -> {res_state.get('active_device', 'Unknown')}")

        # 2. LLM Providers Verification
        res_prov = requests.get(f"{BASE_URL}/api/providers", headers=HEADERS).json()
        providers = res_prov.get("providers", [])
        active_prov = next((p['name'] for p in providers if p.get('is_active')), 'None')
        print(f"[✔] Providers      : {len(providers)} detected (Active: {active_prov})")

        # 3. Knowledge Base Indexing
        res_kbs = requests.get(f"{BASE_URL}/api/kbs", headers=HEADERS).json()
        kbs = res_kbs.get("knowledge_bases", [])
        print(f"[✔] Knowledge Bases: {len(kbs)} ready for RAG operations")

        # 4. MCP Tools Schema
        res_mcp = requests.get(f"{BASE_URL}/api/mcp/tools", headers=HEADERS).json()
        mcp_tags = res_mcp.get("available_tags", [])
        print(f"[✔] MCP Tools      : {res_mcp.get('tools_count', 0)} tools registered across tags: {mcp_tags}")

        return providers, kbs, mcp_tags

    except requests.exceptions.ConnectionError:
        print(f"[✘] Connection Refused: Ensure the API server is running at {BASE_URL}")
        sys.exit(1)
    except Exception as e:
        print(f"[✘] Automated check failed during execution: {e}")
        sys.exit(1)


def interactive_parameter_configuration(providers, kbs, mcp_tags):
    """
    Facilitates interactive terminal selection for the generation payload.
    """
    print("\n" + "=" * 60)
    print(" PHASE 2: INTERACTIVE PARAMETER CONFIGURATION")
    print("=" * 60)

    config = {
        "provider_id": None,
        "model": None,
        "trans_provider_id": None,
        "trans_model": None,
        "force_translate": False,
        "kb_id": "none",
        "use_mcp": False,
        "mcp_tags": []
    }

    # --- Main Model Selection ---
    print("\n[ Primary LLM Provider ]")
    for idx, p in enumerate(providers):
        print(f"  {idx + 1}. {p['name']} (ID: {p['id']})")
    p_idx = int(input("Select Provider (Number): ")) - 1
    config["provider_id"] = providers[p_idx]["id"]

    print("\n[ Primary Model ]")
    models = providers[p_idx]["models"]
    for idx, m in enumerate(models):
        print(f"  {idx + 1}. {m}")
    m_idx = int(input("Select Model (Number): ")) - 1
    config["model"] = models[m_idx]

    # --- Pre-Translation Module ---
    enable_trans = input("\nEnable Pre-translation Module? (y/n): ").strip().lower() == 'y'
    if enable_trans:
        config["force_translate"] = True

        print("\n[ Translation LLM Provider ]")
        for idx, p in enumerate(providers):
            print(f"  {idx + 1}. {p['name']} (ID: {p['id']})")
        t_idx = int(input("Select Translation Provider (Number): ")) - 1
        config["trans_provider_id"] = providers[t_idx]["id"]

        print("\n[ Translation Model ]")
        t_models = providers[t_idx]["models"]
        for idx, m in enumerate(t_models):
            print(f"  {idx + 1}. {m}")
        tm_idx = int(input("Select Translation Model (Number): ")) - 1
        config["trans_model"] = t_models[tm_idx]

    # --- Knowledge Base Assignment ---
    if kbs:
        print("\n[ Vector Knowledge Base ]")
        print("  0. None (Direct Chat)")
        for idx, kb in enumerate(kbs):
            print(f"  {idx + 1}. {kb['name']} (Domain: {kb.get('domain', 'General')})")
        kb_idx = int(input("Select Knowledge Base (Number): "))
        if kb_idx > 0:
            config["kb_id"] = kbs[kb_idx - 1]["id"]

    # --- MCP Tool Routing ---
    if mcp_tags:
        print("\n[ MCP Tool Tags (Filter Tools) ]")
        print("  0. None (Disable MCP entirely)")
        print("  A. All (Enable all available tools)")
        for idx, tag in enumerate(mcp_tags):
            print(f"  {idx + 1}. {tag}")

        mcp_input = input("Select Tags (comma-separated numbers, e.g., 1,3 or 'A' or '0'): ").strip().upper()

        if mcp_input == 'A':
            config["use_mcp"] = True
            config["mcp_tags"] = mcp_tags
        elif mcp_input and mcp_input != "0":
            config["use_mcp"] = True
            try:
                indices = [int(i.strip()) - 1 for i in mcp_input.split(",")]
                config["mcp_tags"] = [mcp_tags[i] for i in indices if 0 <= i < len(mcp_tags)]
            except ValueError:
                print("[!] Invalid input. Defaulting to no MCP tools.")
                config["use_mcp"] = False

    return config


def execute_semantic_filter(query, mcp_tags, top_k=8):
    """
    Tests the standalone semantic filtering endpoint to visualize
    which tools the Reranker selects for the given query.
    """
    print("\n" + "=" * 60)
    print(" PHASE 3: SEMANTIC MCP TOOL FILTERING (RERANKER)")
    print("=" * 60)

    payload = {
        "query": query,
        "history_context": "",
        "top_k": top_k,
        "mcp_tags": mcp_tags if mcp_tags else None
    }

    print("[Analyzing user intent and reranking tools...]\n")
    try:
        response = requests.post(f"{BASE_URL}/api/mcp/filter", json=payload, headers=HEADERS)
        response.raise_for_status()
        data = response.json()

        status = data.get("status")
        if status == "success":
            print(
                f"[✔] Reranker Success: Filtered {data.get('original_count')} tools down to Top {data.get('filtered_count')}:")
            for idx, tool in enumerate(data.get("filtered_tools", []), 1):
                func_name = tool.get("function", {}).get("name", "Unknown")
                desc = tool.get("function", {}).get("description", "")[:80].replace("\n", " ")
                print(f"  {idx}. {func_name}\n     -> {desc}...")
        elif status == "bypassed_insufficient_tools":
            print(
                f"[~] Reranker Bypassed: Total available tools ({len(data.get('filtered_tools', []))}) is less than or equal to Top-K ({top_k}). Using all tools.")
        elif status == "degraded_error":
            print(f"[✘] Reranker Error: {data.get('error')}. Silently degrading to full selected toolset.")

    except requests.exceptions.RequestException as e:
        print(f"[✘] Semantic filter request failed: {e}")


def stream_chat_execution(config):
    """
    Dispatches the streaming POST request and meticulously parses the
    reasoning, content, citations, and follow-ups from the SSE chunks.
    """
    query = input("\nEnter your analytical query: ")

    # Execute the semantic filter diagnostic first if MCP is enabled
    if config["use_mcp"]:
        execute_semantic_filter(query, config["mcp_tags"])

    print("\n" + "=" * 60)
    print(" PHASE 4: AGENTIC CHAT EXECUTION (SSE STREAM)")
    print("=" * 60)

    payload = {
        "model": config["model"],
        "messages": [{"role": "user", "content": query}],
        "stream": True,
        "provider_id": config["provider_id"],
        "kb_id": config["kb_id"],
        "use_mcp": config["use_mcp"]
    }

    if config["force_translate"]:
        payload["force_translate"] = True
        payload["trans_provider_id"] = config["trans_provider_id"]
        payload["trans_model"] = config["trans_model"]

    if config["use_mcp"]:
        payload["mcp_tags"] = config["mcp_tags"]

    print("\n[Transmitting Payload to API...]\n")

    try:
        response = requests.post(f"{BASE_URL}/v1/chat/completions", json=payload, headers=HEADERS, stream=True)
        response.raise_for_status()

        has_printed_content_header = False
        cited_sources_data = []
        follow_ups_data = []

        print("--- Reasoning Trace ---")
        for line in response.iter_lines():
            if line:
                decoded_line = line.decode('utf-8')
                if decoded_line.startswith("data: "):
                    data_str = decoded_line[6:]
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                        if "error" in chunk:
                            print(f"\n[API Runtime Error]: {chunk['error']}")
                            return

                        delta = chunk.get("choices", [{}])[0].get("delta", {})

                        # 1. Output Reasoning Token
                        if "reasoning_content" in delta:
                            sys.stdout.write(f"\033[90m{delta['reasoning_content']}\033[0m")
                            sys.stdout.flush()

                        # 2. Output Standard Content Token
                        if "content" in delta:
                            if not has_printed_content_header:
                                print("\n\n--- Primary Content ---")
                                has_printed_content_header = True

                            sys.stdout.write(delta["content"])
                            sys.stdout.flush()

                        # 3. Capture Structural Metadata (Citations & Follow-ups)
                        if "cited_sources" in delta:
                            cited_sources_data = delta["cited_sources"]
                        if "follow_ups" in delta:
                            follow_ups_data = delta["follow_ups"]

                    except json.JSONDecodeError:
                        continue

        # --- Metadata Rendering ---
        print("\n\n" + "-" * 40)
        print(" METADATA EXTRACTION RESULTS")
        print("-" * 40)

        if cited_sources_data:
            print("\n[📚 Cited Sources Extracted]:")
            for idx, source in enumerate(cited_sources_data, 1):
                print(f"  {idx}. {source}")
        else:
            print("\n[📚 Cited Sources]: None detected.")

        if follow_ups_data:
            print("\n[💡 Follow-up Inquiries Extracted]:")
            for idx, follow_up in enumerate(follow_ups_data, 1):
                print(f"  {idx}. {follow_up}")
        else:
            print("\n[💡 Follow-up Inquiries]: None generated.")

    except requests.exceptions.RequestException as e:
        print(f"\n[✘] Network execution failed: {e}")


if __name__ == "__main__":
    providers_list, kbs_list, mcp_tags_list = execute_automated_health_checks()
    req_config = interactive_parameter_configuration(providers_list, kbs_list, mcp_tags_list)
    stream_chat_execution(req_config)