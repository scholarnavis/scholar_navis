"""
Agent Runtime
=============

The modern plan-execute-observe loop for Scholar Navis.

Responsibilities:
    1. Build the effective tool set for a query (via IntentPlanner).
    2. Drive the LLM tool-calling loop with **native function calling as the
       primary path**, and a structured (JSON) fallback for reasoning models
       that cannot expose native tool calls.
    3. Execute Skill calls through SkillManager (local, zero-latency) or
       MCPManager (remote), normalizing every result into a compact string.
    4. Stream final text tokens to the UI and attach a cited-sources block.

Design principles:
    - High cohesion: each concern (routing / planning / execute / observe) is
      a separate method or small module.
    - Low coupling: the runtime depends on narrow interfaces (SkillRegistry,
      SkillManager, MCPManager, main LLM), not on UI widgets.
    - Performance: the heavyweight LLM-routing is optional; the lightweight
      router runs in microseconds. Tool results are truncated before being
      re-fed to the LLM to conserve the context window.
    - Bounded: a hard iteration cap prevents runaway tool loops.
"""

from __future__ import annotations

import copy
import html as _html_mod
import json
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional


def _html_escape(text) -> str:
    """Escape a value for safe inline HTML injection (used by plot results)."""
    return _html_mod.escape("" if text is None else str(text), quote=False)

logger = logging.getLogger("Agent.Runtime")

# Safety limits
MAX_ITERATIONS = 12
_MAX_TOOL_RESULT_CHARS = 6000
_MAX_REASONING_CHARS = 4000
# 同一轮内并发执行的工具数量上限；避免一次性拉起过多线程挤占本地推理资源
_MAX_PARALLEL_TOOLS = 6
_ALWAYS_TOOLS = {
    "generate_image": {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "Generates an image based on a text prompt. Use this tool ONLY when the user "
                "explicitly asks to draw, create, or generate a picture/image."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "A highly detailed English prompt describing the image to be generated.",
                    }
                },
                "required": ["prompt"],
            },
        },
    },
    "modify_chart": {
        "type": "function",
        "function": {
            "name": "modify_chart",
            "description": (
                "Modifies a previously drawn chart and re-renders it with the same data. "
                "Use this tool when the user asks to change, adjust, re-style, or re-plot an "
                "existing chart that was drawn earlier in this conversation (e.g. change colors, "
                "axis labels, title, legend, chart type, theme, statistics, or add annotations). "
                "The user may express the request in natural language (any language); pass the "
                "modification_request through unchanged. The target chart is identified by its "
                "plot_id, which is returned in the tool result each time a chart is drawn. "
                "After a successful edit the new chart is returned as a new plot and can be "
                "edited again in later turns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plot_id": {
                        "type": "string",
                        "description": "The plot_id of the chart to modify (e.g. 'plot_1', 'plot_2').",
                    },
                    "modification_request": {
                        "type": "string",
                        "description": "The user's natural-language request describing the desired changes (keep the original language).",
                    },
                },
                "required": ["plot_id", "modification_request"],
            },
        },
    },
    "propose_plot_plan": {
        "type": "function",
        "function": {
            "name": "propose_plot_plan",
            "description": (
                "Proposes a concrete data-visualization plan (English) to the user and waits for "
                "their confirmation before rendering. Use this tool INSTEAD of plot_chart whenever "
                "the user asks to visualize/plot some data but has NOT clearly specified the chart "
                "type, the x/y columns, the title, styling, or other plotting requirements. You should "
                "analyze the data structure (column names, column types, sample rows, the general "
                "plotting direction the user hinted at) and generate a clear, journal-style English "
                "plotting proposal (chart type, x, y, title, style/palette/theme, and a short "
                "rationale). After this tool runs, a suggestion card is shown to the user so they can "
                "review, edit, translate, and confirm. You MUST NOT call plot_chart until the user "
                "has confirmed the plan.\n\n"
                "REFERENCE LAYOUT HINTS — pick the proposal that matches these standards:\n"
                "  * Enrichment Dotplot (GO / KEGG / GSEA / Reactome / clusterProfiler style): "
                "chart_type='bubble', x=Gene Ratio (numeric, e.g. gene_ratio / rich_factor) plotted "
                "HORIZONTALLY at the bottom, y=Pathway/Term name on the LEFT, sorted by Gene Ratio "
                "DESCENDING so the largest ratio sits at the TOP, size=Gene Count, color=FDR (BH-"
                "corrected p_value) using a blue-white-red continuous gradient. Right-side legend: "
                "vertical color bar 'FDR' + 'Count' size legend with discrete reference dots. Do NOT "
                "coord_flip; do NOT use -log10(FDR) as the color column.\n"
                "  * Volcano plot: x=log2(fold_change), y=-log10(p_value), color=regulation group "
                "(Up/Down/NS) via set1.\n"
                "  * Heatmap / corrplot / density / ridge: viridis sequential palette.\n"
                "  * Boxplot / violin / dotplot: set2 categorical palette.\n"
                "  * Pie / donut / alluvial / network: set1 high-contrast.\n"
                "  * Line / area: nature colorblind-safe palette."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "request": {
                        "type": "string",
                        "description": "The user's original natural-language request about visualizing the data (keep the original language).",
                    },
                    "data_context": {
                        "type": "string",
                        "description": "A JSON string describing the data to plot: column names, column types, a few sample rows, and any hint about the intended chart direction.",
                    },
                },
                "required": ["request", "data_context"],
            },
        },
    },
}


class AgentRuntime:
    """Plan -> Execute -> Observe loop."""

    def __init__(self, main_llm, skill_manager, mcp_manager, planner=None, cite_collector=None, log_fn=None,
                 plot_registry: Optional[Dict[str, Dict]] = None, plot_seq: int = 0):
        self.main_llm = main_llm
        self.skill_manager = skill_manager
        self.mcp_manager = mcp_manager
        self.registry = None
        if planner is not None:
            # 兼容两种命名：IntentPlanner.registry（公开属性）或旧式 _registry（私有）。
            self.registry = getattr(planner, "registry", None) or getattr(planner, "_registry", None)
        self.planner = planner
        self._is_cancelled = False
        # cite_collector(metadata: dict) -> int : registers an online source for the
        # 'Cited Sources' UI block; returns a citation id or None.
        self.cite_collector = cite_collector
        # log_fn(level: str, msg: str) : optional logger hook (e.g. ChatTask.send_log)
        self.log_fn = log_fn or (lambda level, msg: None)
        # plot_id -> {script_path, code_path, data_path, chart_title, chart_type,
        #             columns, column_types, preview, total_rows, extra_packages}
        # 记录会话内已绘制的图，供 modify_chart 工具按 plot_id 定位并重绘。
        # 可跨轮次注入（由调用方传入上一轮缓存），且每次新图注册都会落盘
        # （plot_registry.json），因此即使进程重启、runtime 重建也能恢复。
        self._plot_registry: Dict[str, Dict] = dict(plot_registry or {})
        self._plot_seq = int(plot_seq) if plot_seq else (max((int(k.split("_")[-1]) for k in self._plot_registry if k.startswith("plot_")), default=0) if self._plot_registry else 0)

    # ------------------------------------------------------------------ #
    #  Public entry
    # ------------------------------------------------------------------ #
    def run(
        self,
        query: str,
        rag_messages: List[Dict],
        system_prompt: str,
        candidate_tools: List[Dict],
        emit_token: Callable[[str], None],
        is_cancelled: Callable[[], bool],
        max_iterations: int = MAX_ITERATIONS,
    ) -> str:
        """
        Drive the agent loop and return the final generated text.

        Args:
            query:             the (translated) user query.
            rag_messages:      message history + system prompt (index 0) + user content.
            system_prompt:     the base system prompt. Reserved for reference; the tool
                               directive is injected by the caller via the system message
                               (dynamic_tool_prompt), so the runtime never mutates user
                               content (which may be multimodal).
            candidate_tools:   the raw schemas to expose to the model.
            emit_token:        UI stream callback.
            is_cancelled:      cancellation probe.
            max_iterations:    hard safety cap on tool-calling rounds.
        """
        self._is_cancelled = False
        full_response_cache: List[str] = []

        def _emit(tok: str):
            full_response_cache.append(tok)
            emit_token(tok)

        # Fresh working copy; never mutate the caller's message list.
        # NOTE: the tool directive is already injected into the system prompt by
        # the caller (dynamic_tool_prompt). We must NOT prepend to messages[-1]
        # because its content may be a multimodal list, not a plain string.
        messages = [dict(m) for m in rag_messages]

        # Local mutable copy of the candidate tools. Each round we refresh the
        # modify_chart description with the CURRENT plot catalog so the LLM can
        # distinguish one chart from another (semantic labels) when it decides
        # which plot_id to modify — this is how a single conversation can draw
        # multiple visualizations and keep them mapped to the right requirement.
        # Deep-copy so we never mutate the caller's / shared _ALWAYS_TOOLS dicts.
        candidate_tools = copy.deepcopy(list(candidate_tools or []))

        iterations = 0
        plot_leak_retries = 0
        while iterations < max_iterations:
            if is_cancelled():
                self._emit_cancel_notice(_emit)
                break
            iterations += 1

            # Keep the modify_chart schema's plot_id description in sync with the
            # current set of charts (grows as charts are drawn within this run).
            self._refresh_modify_chart_tools(candidate_tools)

            response = self._llm_step(messages, candidate_tools)
            tool_calls = self._extract_tool_calls(response)
            reasoning = (response or {}).get("reasoning_content", "") or ""
            content = (response or {}).get("content", "") or ""

            if reasoning:
                _emit(f"<think>\n{reasoning[: _MAX_REASONING_CHARS]}\n</think>\n\n")

            # No tools requested -> final answer text.
            if not tool_calls:
                if content:
                    # Reasoning 模型可能把 plot_chart 的参数以纯文本“泄漏”到答案里，
                    # 而不是真正调用工具。检测到后强制它调用 plot_chart 重新绘图。
                    if (
                        plot_leak_retries < 2
                        and self._has_tool(candidate_tools, "plot_chart")
                        and self._looks_like_plot_leak(content)
                    ):
                        plot_leak_retries += 1
                        self.log_fn(
                            "WARN",
                            "Detected plot parameter leak; forcing a plot_chart tool call.",
                        )
                        messages.append({"role": "assistant", "content": content})
                        messages.append({
                            "role": "user",
                            "content": (
                                "[System Notification: You described chart parameters in plain text "
                                "instead of calling the plot_chart tool. Invoke the plot_chart tool NOW "
                                "to render the figure. If native function calling is unavailable, output "
                                "exactly this JSON block and nothing else:\n"
                                "```json {\"name\": \"plot_chart\", \"arguments\": {\"chart_type\": \"bubble\", \"data\": \"[...]\", \"x\": \"...\", \"y\": \"...\"}} ```]"
                            ),
                        })
                        _emit("[CLEAR_SEARCH]")
                        _emit("[START_LLM_NETWORK]")
                        continue

                    _emit("[CLEAR_SEARCH]")
                    self._stream_final_text(content, _emit, is_cancelled)
                    return "".join(full_response_cache)
                # Empty answer with no tool calls: force the model to produce a
                # real final answer instead of returning an empty response.
                messages.append({
                    "role": "user",
                    "content": (
                        "[System Notification: You did not produce any output. Analyze the conversation "
                        "and answer the user's original query now. FINAL OUTPUT RULE: You MUST NOT invoke "
                        "any tools. Output your final response directly in plain Markdown.]"
                    ),
                })
                _emit("[CLEAR_SEARCH]")
                _emit("[START_LLM_NETWORK]")
                for token in self._final_stream(messages, [], is_cancelled):
                    _emit(token)
                return "".join(full_response_cache)

            # Execute requested tools, then loop back to the model.
            messages.append(self._assistant_tool_msg(content, reasoning, tool_calls))
            self._execute_tool_calls(tool_calls, messages, _emit, is_cancelled)

        # Loop exhausted: force a final plain-text answer.
        if not messages or messages[-1].get("role") != "tool":
            messages.append({
                "role": "user",
                "content": (
                    "[System Notification: Tool execution limit reached. Analyze the tool results "
                    "above and answer the user's original query. FINAL OUTPUT RULE: You MUST NOT "
                    "invoke any more tools. Output your final response directly in plain Markdown.]"
                ),
            })
        else:
            messages[-1]["content"] = messages[-1]["content"] + (
                "\n\n[System Notification: Tool execution limit reached. Analyze the tool results "
                "above and answer the user's original query. FINAL OUTPUT RULE: You MUST NOT "
                "invoke any more tools. Output your final response directly in plain Markdown.]"
            )

        _emit("[CLEAR_SEARCH]")
        _emit("[START_LLM_NETWORK]")
        for token in self._final_stream(messages, candidate_tools, is_cancelled):
            _emit(token)
        return "".join(full_response_cache)

    # ------------------------------------------------------------------ #
    #  Step / observe helpers
    # ------------------------------------------------------------------ #
    def _llm_step(self, messages: List[Dict], tools: List[Dict]) -> Dict:
        kwargs = {"messages": messages, "tools": tools, "tool_choice": "auto"} if tools else {"messages": messages}
        return self.main_llm.chat(**kwargs)

    @staticmethod
    def _has_tool(tools: List[Dict], name: str) -> bool:
        """Return True when ``name`` is present in the candidate tool list."""
        for tool in tools or []:
            if (tool or {}).get("function", {}).get("name") == name:
                return True
        return False

    @staticmethod
    def _looks_like_plot_leak(content: str) -> bool:
        """Detect a reasoning model leaking plot_chart parameters as plain text.

        A chart-parameter leak is characterized by content that starts with a
        chart-type value (bubble/bar/scatter/volcano/heatmap) and contains a JSON
        data array. Such output is never a legitimate final answer, so the caller
        forces a real plot_chart tool invocation instead of returning it verbatim.
        """
        text = (content or "").strip()
        if not text or len(text) > _MAX_REASONING_CHARS:
            return False
        chart_types = ("bubble", "bar", "scatter", "volcano", "heatmap")
        first_line = text.splitlines()[0].strip().lower()
        if first_line not in chart_types:
            return False
        return bool(re.search(r"\[\s*\{.*?\}\s*\]", text, re.DOTALL))

    def _extract_tool_calls(self, response: Dict) -> List[Dict]:
        """Native tool calls first; then structured fallbacks (JSON / XML / DSML)."""
        raw = (response or {}).get("tool_calls")
        if raw:
            calls = []
            for tc in raw:
                if hasattr(tc, "model_dump"):
                    calls.append(tc.model_dump())
                elif isinstance(tc, dict):
                    calls.append(tc)
                else:
                    calls.append({
                        "id": getattr(tc, "id", f"call_{uuid.uuid4().hex[:8]}"),
                        "type": getattr(tc, "type", "function"),
                        "function": {
                            "name": getattr(getattr(tc, "function", None), "name", "unknown"),
                            "arguments": getattr(getattr(tc, "function", None), "arguments", "{}"),
                        },
                    })
            return calls

        content = (response or {}).get("content", "") or ""
        # Structured JSON block.
        json_calls = self._parse_json_tool_blocks(content)
        if json_calls:
            return json_calls
        # XML / DSML invoke tags (reasoning models).
        xml_calls = self._parse_xml_tool_blocks(content)
        if xml_calls:
            return xml_calls
        # Raw / HTML-escaped JSON tool call (reasoning models may emit
        # {"name": "...", "arguments": {...}} as plain, possibly escaped text).
        raw_calls = self._parse_raw_json_tool_blocks(content)
        if raw_calls:
            return raw_calls
        return []

    @staticmethod
    def _parse_json_tool_blocks(content: str) -> List[Dict]:
        calls = []
        for block in re.findall(r"```json\s*\n?(.*?)\n?\s*```", content, re.DOTALL):
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and "name" in data:
                args = data.get("arguments", {})
                if not isinstance(args, dict):
                    args = {}
                calls.append({
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {"name": data["name"], "arguments": json.dumps(args, ensure_ascii=False)},
                })
        return calls

    @staticmethod
    def _parse_xml_tool_blocks(content: str) -> List[Dict]:
        calls = []
        for m in re.finditer(r'<｜DSML｜invoke name=["\'](.*?)["\'](?:>(.*?)</｜DSML｜invoke>| />)', content, re.DOTALL):
            name, args_raw = m.group(1), m.group(2) or ""
            arg_dict = {}
            for p in re.finditer(r'<｜DSML｜parameter name=["\'](.*?)["\'][^>]*>(.*?)</｜DSML｜parameter>',
                                 args_raw, re.DOTALL):
                val = p.group(2).strip()
                if val.lower() == "true":
                    val = True
                elif val.lower() == "false":
                    val = False
                arg_dict[p.group(1)] = val
            calls.append({
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arg_dict, ensure_ascii=False)},
            })
        return calls

    @staticmethod
    def _iter_json_objects(text: str):
        """Yield candidate JSON object substrings via balanced-brace matching."""
        n = len(text)
        i = 0
        while i < n:
            start = text.find("{", i)
            if start == -1:
                return
            depth = 0
            in_str = False
            escaped = False
            j = start
            while j < n:
                ch = text[j]
                if in_str:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            yield text[start:j + 1]
                            break
                j += 1
            i = start + 1

    @staticmethod
    def _parse_raw_json_tool_blocks(content: str) -> List[Dict]:
        """Detect raw (possibly HTML-escaped) tool-call JSON in model output.

        Reasoning models sometimes emit ``{"name": "...", "arguments": {...}}``
        directly as content, and the JSON may be HTML-escaped (``&quot;``,
        ``&amp;``). This parser unescapes and extracts any JSON object carrying
        a ``name`` key so the runtime executes it instead of printing it.
        """
        if not content:
            return []
        text = _html_mod.unescape(content)
        candidates = []
        stripped = text.strip()
        if stripped.startswith("{"):
            candidates.append(stripped)
        for obj in AgentRuntime._iter_json_objects(text):
            if obj not in candidates:
                candidates.append(obj)

        calls: List[Dict] = []
        seen = set()
        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(data, dict) or "name" not in data:
                continue
            name = data.get("name")
            if not isinstance(name, str) or not name:
                continue
            args = data.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, ValueError):
                    args = {}
            if not isinstance(args, dict):
                args = {}
            key = json.dumps({"name": name, "arguments": args}, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            calls.append({
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            })
        return calls

    @staticmethod
    def _assistant_tool_msg(content: str, reasoning: str, tool_calls: List[Dict]) -> Dict:
        msg = {"role": "assistant", "content": content or "", "tool_calls": tool_calls}
        if reasoning:
            msg["reasoning_content"] = reasoning
        return msg

    def _execute_tool_calls(
        self,
        tool_calls: List[Dict],
        messages: List[Dict],
        emit_token: Callable[[str], None],
        is_cancelled: Callable[[], bool],
    ):
        # 先解析所有调用参数（保持与 tool_calls 的原始顺序一致）。
        parsed: List[Dict] = []
        for tc in tool_calls:
            t_id = tc.get("id", f"call_{uuid.uuid4().hex[:8]}")
            t_func = tc.get("function", {})
            t_name = t_func.get("name", "unknown")
            raw_args = t_func.get("arguments", "{}")
            try:
                tool_args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                if not isinstance(tool_args, dict):
                    tool_args = {}
            except json.JSONDecodeError:
                tool_args = {}
            parsed.append({"id": t_id, "name": t_name, "args": tool_args})

        # 并行执行互不依赖的工具调用。结果按原始顺序回填，
        # 以符合 OpenAI 协议对 tool 消息顺序的要求。
        results: List[str] = [""] * len(parsed)

        def _run_one(idx: int) -> int:
            if is_cancelled():
                results[idx] = "[Cancelled] Tool execution aborted by user."
                return idx
            name = parsed[idx]["name"]
            args = parsed[idx]["args"]
            self._emit_tool_start(name, args, emit_token)
            try:
                results[idx] = self._truncate(
                    self._dispatch_tool(name, args, emit_token), _MAX_TOOL_RESULT_CHARS
                )
            except Exception as e:
                logger.error(f"Tool '{name}' raised unexpectedly: {e}")
                results[idx] = f"[TOOL ERROR] Execution of '{name}' raised: {e}"
            return idx

        if len(parsed) == 1:
            _run_one(0)
        else:
            workers = min(_MAX_PARALLEL_TOOLS, len(parsed))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_run_one, i) for i in range(len(parsed))]
                # 等待全部任务结束（取消时任务会各自返回取消占位结果）。
                # 不主动 shutdown(wait=False) 中断，避免线程状态不一致。
                for _ in as_completed(futures):
                    pass

        # 顺序回填 tool 消息，保证与 assistant.tool_calls 对齐。
        for i, p in enumerate(parsed):
            messages.append({
                "role": "tool",
                "tool_call_id": p["id"],
                "name": p["name"],
                "content": results[i],
            })

    def _dispatch_tool(self, name: str, args: dict, emit_token: Optional[Callable[[str], None]] = None) -> str:
        # 1. Built-in image generator.
        if name == "generate_image":
            try:
                prompt_text = args.get("prompt", "")
                if emit_token is not None:
                    emit_token(f"<mcp_process>🎨 Generating image (Prompt: {prompt_text[:30]}...)</mcp_process>\n")
                img_url = self.main_llm.generate_image(prompt=prompt_text)
                if emit_token is not None:
                    emit_token(
                        f"<br><img src='{img_url}' style='max-width: 100%; border-radius: 8px; "
                        f"border: 1px solid #444;' alt='Generated Image'/><br>\n\n"
                    )
                return f"Image generated successfully. URL: {img_url}"
            except Exception as e:
                logger.error(f"Internal Image Tool failed: {e}")
                return f"Image generation failed: {str(e)}"

        # 1b. Built-in chart modifier (re-render an existing plot with new code).
        if name == "modify_chart":
            return self._handle_modify_chart(args, emit_token)

        # 1c. Built-in plotting-plan proposer (show a confirmation card before rendering).
        if name == "propose_plot_plan":
            return self._handle_propose_plot_plan(args, emit_token)

        # 2. Local Skills (academic + external) — zero latency.
        if self.skill_manager.is_skill_available(name):
            prefix = "[ACADEMIC]" if getattr(self.skill_manager, "academic_skills", None) and \
                name in self.skill_manager.academic_skills else "[SKILL]"
            self.log_fn("INFO", f"{prefix} Executing skill: {name}")
            try:
                result = self.skill_manager.call_skill(name, args)
                self._record_provenance(name, "academic" if prefix == "[ACADEMIC]" else "external",
                                        args, "success", result)
                if name == "plot_chart":
                    return self._handle_plot_result(result, emit_token)
                if isinstance(result, (dict, list)):
                    return json.dumps(result, ensure_ascii=False)
                return str(result)
            except Exception as e:
                logger.error(f"Local Skill '{name}' execution failed: {e}")
                self._record_provenance(name, "academic" if prefix == "[ACADEMIC]" else "external",
                                        args, "error", str(e))
                return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

        # 3. Remote MCP tools.
        if self.mcp_manager and self.mcp_manager.is_tool_available(name):
            self.log_fn("INFO", f"[MCP] Requesting external MCP service: {name}")
            try:
                result = self.mcp_manager.call_tool_sync(name, args)
                self._record_provenance(name, "mcp", args, "success", result)
                if self.cite_collector is not None:
                    result = self._collect_mcp_citations(name, result)
                # Instruct the LLM to explain any access/API-key failure to the user.
                if isinstance(result, str) and ("API Key" in result or "missing" in result.lower()):
                    result = (
                        f"[TOOL EXECUTION FAILED] The tool '{name}' returned an error:\n\"{result}\"\n\n"
                        f"INSTRUCTION TO AI:\n1. Explain to the user EXACTLY why the access failed."
                    )
                return result
            except Exception as e:
                logger.error(f"MCP tool {name} failed: {e}")
                self._record_provenance(name, "mcp", args, "error", str(e))
                return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

        # 4. Unknown / disabled.
        logger.warning(f"Model hallucinated or attempted to call unavailable tool: {name}")
        return (
            f"[TOOL ERROR] The tool '{name}' does not exist or is disabled. "
            "Please answer the user using only your current knowledge or valid tools."
        )

    def _final_stream(self, messages: List[Dict], tools: List[Dict], is_cancelled) -> List[str]:
        """Stream the final answer after the loop (used only when the loop exhausts)."""
        kwargs = {}
        if tools:
            kwargs["tools"] = tools
        error_buffer = ""
        for token in self.main_llm.stream_chat(messages, **kwargs):
            if is_cancelled():
                break
            if self._is_error_token(token) or error_buffer:
                error_buffer += token
                continue
            yield token
        if error_buffer:
            yield self._format_error(error_buffer)

    @staticmethod
    def _is_error_token(token: str) -> bool:
        markers = (
            "[API Request Error", "[System Error", "[Context Exceeded Error]",
            "[Rate Limit Error]", "[Timeout Error]",
        )
        return any(m in token for m in markers)

    @staticmethod
    def _format_error(error_buffer: str) -> str:
        """把底层流式错误统一为 ``<error_panel>`` 标记。

        UI 侧（chat_bubble._extract_error_panels）将标记渲染为统一
        错误面板：title/body 面向用户，details（原始错误全文）进入
        折叠栏；真实错误同时写入日志。
        """
        from src.core.llm_errors import friendly_payload, error_marker

        m = re.match(r"^\s*\[(.*?)\]\s*\n*(.*)", error_buffer, re.DOTALL)
        if m:
            payload = friendly_payload(m.group(1).strip(), m.group(2).strip(),
                                       details=error_buffer.strip())
        else:
            payload = friendly_payload("Provider Error", error_buffer.strip()[:400],
                                       details=error_buffer.strip())
        logger.error(f"LLM stream error captured.\n{payload['details']}")
        # 前置空行确保标记独占块级位置，Markdown 原样透传给 UI 提取
        return "\n\n" + error_marker(payload)

    # ------------------------------------------------------------------ #
    #  Provenance tracking
    # ------------------------------------------------------------------ #
    @staticmethod
    def _record_provenance(tool: str, pool: str, args: dict, status: str, result: str):
        """Record one evidence-chain link into the shared ProvenanceCollector.

        Failure to record must never break the conversation, so any error is
        logged and swallowed. ``source`` is left empty here; upstream databases
        are inferred downstream when a tool's result is parsed into citations.
        """
        try:
            from src.core.provenance import get_collector
            get_collector().record(
                tool=tool,
                pool=pool,
                params=args or {},
                status=status,
                result_summary=result if status == "success" else "",
            )
        except Exception as e:
            logger.warning(f"Provenance recording skipped for '{tool}': {e}")

    # ------------------------------------------------------------------ #
    #  Plot result handling
    # ------------------------------------------------------------------ #
    def _handle_plot_result(self, result: str, emit_token) -> str:
        """Handle a ``plot_chart`` skill result.

        On success, a dedicated ``<rplot_card>`` marker (carrying the plot
        paths as base64-encoded JSON) is streamed directly to the UI via
        ``emit_token``. The UI turns it into a fixed R plot card widget with
        preview + downloads — it must NOT depend on the LLM re-echoing it.
        A short confirmation is returned to the LLM so it can narrate the
        result without re-printing the chart.
        """
        try:
            payload = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result

        if not isinstance(payload, dict) or payload.get("status") != "success":
            return result

        title = payload.get("chart_title", "Chart")
        total_rows = payload.get("total_rows", 0)
        chart_type = payload.get("chart_type", "")

        # 为每个图分配唯一 plot_id，并生成一个稳定、人类可读的语义标签
        # plot_label（如 plot_2 "volcano - Differential expression"）。
        # AI 靠 plot_label 而非单纯的递增编号来区分多张图，从而更准确地
        # 回应用户"改第X张/那个散点图/火山图"之类的后续需求。
        self._plot_seq += 1
        plot_id = f"plot_{self._plot_seq}"
        plot_label = self._build_plot_label(plot_id, title, chart_type)
        self._plot_registry[plot_id] = {
            "script_path": payload.get("script_path", ""),
            # 纯绘图代码路径：修改图时优先基于它改写，避免重复包裹 prelude。
            "code_path": payload.get("code_path", ""),
            "data_path": payload.get("data_path", ""),
            "chart_title": title,
            "chart_type": chart_type,
            "plot_label": plot_label,
            "columns": payload.get("columns", []),
            "column_types": payload.get("column_types", {}),
            "preview": payload.get("preview", [])[:5],
            "total_rows": total_rows,
            "extra_packages": payload.get("extra_packages", []),
        }
        # 落盘持久化：runtime 每轮重建，但 registry 保留在磁盘上，后续轮次 /
        # 新会话仍可对这张图用自然语言继续修改。
        self._persist_plot_registry()

        # Pass everything the UI card needs in one base64-encoded JSON blob.
        # Base64 avoids any HTML-escaping / markdown mangling of the paths.
        import base64
        card_payload = {
            "plot_id": plot_id,
            "plot_label": plot_label,
            "chart_title": title,
            "svg_path": payload.get("svg_path", ""),
            "png_path": payload.get("png_path", ""),
            "pdf_path": payload.get("pdf_path", ""),
            "script_path": payload.get("script_path", ""),
            "data_path": payload.get("data_path", ""),
            "columns": payload.get("columns", []),
            "preview": payload.get("preview", [])[:5],
            "total_rows": total_rows,
        }
        encoded = base64.b64encode(
            json.dumps(card_payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        if emit_token is not None:
            emit_token(f'<rplot_card data="{encoded}"></rplot_card>\n')

        # Short confirmation for the LLM (do not re-print the whole chart).
        # 注入 plot_label 与已绘制的图目录，让 LLM 能清晰区分多图并支持后续修改。
        plot_list = self._plot_catalog_text()
        return json.dumps({
            "status": "success",
            "message": (
                f"Chart '{title}' rendered (SVG/PNG/PDF) and streamed to the user. "
                f"Its plot_id is '{plot_id}' (semantic label: {plot_label}). "
                f"Data has {total_rows} rows; full CSV saved locally. "
                f"Plots drawn so far in this conversation:\n{plot_list}\n"
                "Briefly summarize the chart's key finding for the user; do not re-print the data. "
                "If the user later asks to modify this chart, call the modify_chart tool with the "
                "matching plot_id above."
            ),
        }, ensure_ascii=False)

    @staticmethod
    def _build_plot_label(plot_id: str, title: str, chart_type: str) -> str:
        """Build a stable, human-readable semantic label for a chart.

        Instead of a bare incrementing id (``plot_2``), the label combines the
        id with the chart type and title, e.g. ``plot_2 [volcano] Differential
        expression``. This gives both the AI and the user a way to tell which
        chart corresponds to which requirement, which is essential when a single
        conversation draws several visualizations.
        """
        title = (title or "").strip() or "Chart"
        ctype = (chart_type or "").strip().lower()
        if ctype:
            return f'{plot_id} [{ctype}] {title}'
        return f'{plot_id} {title}'

    def _plot_catalog_text(self) -> str:
        """Render a compact catalog of every chart drawn in this conversation.

        Each line is ``<plot_id> [<type>] <title>`` so the AI can distinguish
        charts precisely when choosing which one to modify or describe.
        """
        if not self._plot_registry:
            return "none"
        lines = []
        for pid, info in self._plot_registry.items():
            label = info.get("plot_label") or self._build_plot_label(
                pid, info.get("chart_title", ""), info.get("chart_type", "")
            )
            lines.append(f"- {label}")
        return "\n".join(lines)

    def _ensure_disk_registry_loaded(self) -> None:
        """Merge the persisted plot registry into memory if this runtime was
        rebuilt (a fresh runtime starts with an empty registry even though
        charts drawn in previous turns/sessions exist on disk)."""
        if self._plot_registry:
            return
        disk = self._load_persisted_plot_registry()
        if not disk:
            return
        for pid, val in disk.items():
            self._plot_registry.setdefault(pid, val)
        seqs = [
            int(p.split("_")[-1])
            for p in disk
            if p.startswith("plot_") and p.split("_")[-1].isdigit()
        ]
        if seqs:
            self._plot_seq = max(self._plot_seq, max(seqs))

    def _build_modify_chart_catalog(self) -> str:
        """A single-line catalog string embedded into the modify_chart tool
        description so the LLM can pick the correct plot_id even across turns
        (when this runtime was rebuilt and only the persisted registry remains).
        """
        self._ensure_disk_registry_loaded()
        if not self._plot_registry:
            return "none yet (this will be the first chart)"
        return "; ".join(
            self._plot_registry[pid].get("plot_label")
            or self._build_plot_label(pid, self._plot_registry[pid].get("chart_title", ""),
                                      self._plot_registry[pid].get("chart_type", ""))
            for pid in self._plot_registry
        )

    def _refresh_modify_chart_tools(self, tools: List[Dict]) -> None:
        """Dynamically inject the current plot catalog into the modify_chart
        tool's ``plot_id`` parameter description.

        A single conversation can draw multiple charts; the plain incrementing
        plot_id is meaningless to the LLM. Embedding the live catalog (semantic
        labels like ``plot_2 [volcano] Differential expression``) lets the model
        distinguish one chart from another and pick the right one to modify.
        The catalog is refreshed each round because new charts may be drawn in
        earlier iterations of the same run.
        """
        catalog = self._build_modify_chart_catalog()
        for tool in tools or []:
            func = (tool or {}).get("function", {})
            if (func or {}).get("name") != "modify_chart":
                continue
            props = (func.get("parameters") or {}).get("properties", {})
            pid_desc = props.get("plot_id")
            if isinstance(pid_desc, dict):
                pid_desc["description"] = (
                    f"The plot_id of the chart to modify. Charts drawn in this conversation:\n"
                    f"{catalog}\n"
                    "If the user refers to a chart by its type, title, or the data it shows, "
                    "match it to the most relevant entry above and use that plot_id."
                )
            return

    # ------------------------------------------------------------------ #
    #  Chart modification
    # ------------------------------------------------------------------ #
    def _handle_modify_chart(self, args: dict, emit_token) -> str:
        """Modify a previously drawn chart and re-render it with the same data.

        Reads the original *pure plotting* R code (persisted alongside the chart),
        asks the LLM to produce a modified version honoring the user's natural
        language request, then re-runs the plot engine against the same data.
        The new result is streamed as a fresh ``<rplot_card>`` and registered as
        a new plot (so it can be modified again in a later turn).
        """
        plot_id = (args or {}).get("plot_id", "")
        modification_request = (args or {}).get("modification_request", "")
        # 兼容跨轮次：本轮 runtime 是新建的，registry 可能为空，
        # 从磁盘加载持久化注册表并合并到内存。
        if plot_id and plot_id not in self._plot_registry:
            self._ensure_disk_registry_loaded()

        if not plot_id or plot_id not in self._plot_registry:
            return json.dumps({
                "status": "error",
                "message": (
                    f"Unknown plot_id '{plot_id}'. Available plots in this conversation:\n"
                    f"{self._plot_catalog_text()}\n"
                    "Tell the user which chart to modify and ask them to clarify."
                ),
            }, ensure_ascii=False)

        info = self._plot_registry[plot_id]
        data_path = info.get("data_path", "")
        if not data_path or not os.path.exists(data_path):
            return json.dumps({
                "status": "error",
                "message": f"Data file for '{plot_id}' is missing ({data_path}). Cannot re-render.",
            }, ensure_ascii=False)

        # 优先读取"纯绘图代码"（无 prelude / 输出指令）；旧版注册只有完整脚本
        # 时，回退读取并剥离出其中的绘图部分。
        code_source = ""
        code_path = info.get("code_path", "")
        if code_path and os.path.exists(code_path):
            try:
                with open(code_path, "r", encoding="utf-8") as f:
                    code_source = f.read()
            except Exception as e:
                logger.warning(f"modify_chart: failed to read code_path {code_path}: {e}")
        if not code_source:
            script_path = info.get("script_path", "")
            if script_path and os.path.exists(script_path):
                try:
                    with open(script_path, "r", encoding="utf-8") as f:
                        code_source = self._extract_plot_code(f.read())
                except Exception as e:
                    logger.warning(f"modify_chart: failed to read script_path {script_path}: {e}")
        if not code_source:
            return json.dumps({
                "status": "error",
                "message": f"Source code for '{plot_id}' is missing. Cannot modify.",
            }, ensure_ascii=False)

        # 让 LLM 基于原绘图代码 + 修改需求生成新代码，并可选更新标题/类型。
        new_code, new_title, new_chart_type = self._generate_modified_script(
            code_source, modification_request, info
        )
        if not new_code:
            return json.dumps({
                "status": "error",
                "message": "Failed to generate a modified R script. Please try rephrasing the request.",
            }, ensure_ascii=False)

        # 用同一份数据重新渲染（沿用原图的扩展包，避免 pheatmap 等包未加载）。
        try:
            from src.core.plot_engine import PlotEngine
            engine = PlotEngine()
            plot_data = engine.load_plot_data(data_path)
            job_id = f"mod_{plot_id}_{int(time.time())}"
            result = engine.run_plot(
                r_code=new_code,
                plot_data=plot_data,
                job_id=job_id,
                extra_packages=info.get("extra_packages") or None,
            )
        except Exception as e:
            logger.error(f"modify_chart re-render failed: {e}")
            return json.dumps({
                "status": "error",
                "message": f"Re-render failed: {e}",
            }, ensure_ascii=False)

        if result.success:
            # 组装与 plot_chart 一致的 payload，复用 rplot_card 流式输出逻辑。
            # 新标题/类型优先取 LLM 更新值，否则沿用旧值。
            payload = {
                "status": "success",
                "chart_title": new_title or info.get("chart_title", "Modified Chart"),
                "chart_type": new_chart_type or info.get("chart_type", ""),
                "svg_path": result.svg_path,
                "png_path": result.png_path,
                "pdf_path": result.pdf_path,
                "script_path": result.script_path,
                "code_path": result.code_path,
                "data_path": plot_data.data_path,
                "columns": plot_data.columns,
                "column_types": dict(plot_data.column_types or {}),
                "preview": plot_data.preview[:5],
                "total_rows": plot_data.total_rows,
                "extra_packages": info.get("extra_packages") or [],
            }
            return self._handle_plot_result(json.dumps(payload, ensure_ascii=False), emit_token)
        return json.dumps({
            "status": "error",
            "message": f"Re-render failed: {result.error_message}",
        }, ensure_ascii=False)

    def _handle_propose_plot_plan(self, args: dict, emit_token) -> str:
        """Propose an English data-visualization plan to the user and wait for confirmation.

        Triggered via the ``propose_plot_plan`` tool whenever the user asks to visualize
        data but did not specify the chart type / axes / title / styling. The handler:

        1. Asks the main LLM to analyze the data structure + the user's direction and
           produce a concrete, journal-style English plotting proposal (chart type, x,
           y, title, style/palette/theme, and a short rationale).
        2. Streams a ``<plot_plan data="...">`` marker (base64 JSON) to the UI, which
           renders a confirmation card the user can review / edit / translate / confirm.
        3. Returns an instruction to the LLM to NOT render yet and to wait for the user's
           confirmation.
        """
        request = (args or {}).get("request", "")
        data_context = (args or {}).get("data_context", "")

        plan = self._generate_plot_plan_text(request, data_context)
        if not plan:
            return json.dumps({
                "status": "error",
                "message": (
                    "Failed to produce a plotting plan. Please ask the user for more detail "
                    "about the data structure and the intended chart direction, then retry."
                ),
            }, ensure_ascii=False)

        # Stream the plan card to the UI (base64-encoded JSON, same pattern as <rplot_card>).
        import base64
        card_payload = {
            "request": request,
            "data_context": data_context,
            "plan_text": plan,
        }
        encoded = base64.b64encode(
            json.dumps(card_payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        if emit_token is not None:
            emit_token(f'<plot_plan data="{encoded}"></plot_plan>\n')

        return json.dumps({
            "status": "success",
            "message": (
                "A plotting plan has been proposed to the user via a confirmation card. "
                "The user will review, edit, translate, and confirm it. You MUST NOT call "
                "plot_chart (or any other rendering tool) now. Wait for the user's next "
                "message; once they confirm the plan, follow their finalized requirements "
                "and call plot_chart to render the figure."
            ),
        }, ensure_ascii=False)

    def _generate_plot_plan_text(self, request: str, data_context: str) -> str:
        """Ask the LLM to produce an English plotting proposal (natural-language text)."""
        prompt = (
            "You are an expert bioinformatician and data-visualization designer. "
            "The user wants to visualize some data but did NOT fully specify the chart. "
            "Based on the data structure below and the user's general direction, propose a "
            "concrete, publication-quality plotting plan.\n\n"
            "Output requirements:\n"
            "- Respond ONLY with a concise English plan using short bullet lines. Do NOT add "
            "any markdown fences, headers, or extra prose.\n"
            "- Explicitly state: the recommended chart_type, the x column, the y column, an "
            "optional size/label column, a suggested title, the style/palette/theme, and one "
            "short rationale sentence.\n"
            "- Keep it actionable so the AI can later translate it into a plot_chart call.\n"
            "- If the user hinted a specific direction, honor it; otherwise pick the most "
            "academically appropriate chart for the data.\n\n"
            f"User's visualization request: {request or '(not specified)'}\n\n"
            f"Data structure (columns, types, preview rows):\n{data_context or '(not provided)'}"
        )
        try:
            resp = self.main_llm.chat(messages=[{"role": "user", "content": prompt}])
            content = (resp or {}).get("content", "") or ""
            content = re.sub(r"^```(?:text|markdown)?\s*", "", content.strip())
            content = re.sub(r"\s*```$", "", content)
            content = re.sub(r"^#+\s*", "", content, flags=re.MULTILINE)
            return content.strip()
        except Exception as e:
            logger.error(f"propose_plot_plan generation failed: {e}")
            return ""

    def _generate_modified_script(self, original_code: str, request: str, info: dict) -> tuple:
        """Ask the LLM to produce a modified R script for an existing chart.

        Returns ``(script, new_title, new_chart_type)``. The LLM may declare an
        updated title / chart type via trailing ``# CHART_TITLE:`` /
        ``# CHART_TYPE:`` comment lines (parsed and stripped); otherwise the
        original values are kept.
        """
        preview_rows = info.get("preview", [])
        preview_text = "\n".join(
            " | ".join(str(c) for c in row) for row in preview_rows
        ) or "(no rows)"
        col_types = ", ".join(
            f"{k}={v}" for k, v in (info.get("column_types") or {}).items()
        ) or "unknown"
        prompt = (
            "You are an expert R/ggplot2 developer. Below is the R plotting code that "
            "produced a chart, together with the user's requested modification. "
            "Rewrite the code so the chart reflects the user's request.\n\n"
            "Environment contract (MUST follow):\n"
            "- The dataset is ALREADY loaded as a data.frame named `.data`. Do NOT read "
            "any CSV file, do NOT change/load the data, do NOT write any file.\n"
            "- The final plot object MUST be assigned to a variable named `p`; the "
            "runtime prints `p` onto SVG/PNG/PDF devices for you, so do NOT open any "
            "device, do NOT call `print()`, `dev.off()`, `svg()`, `png()`, `pdf()`.\n"
            "- Use the existing column names exactly as shown below; coerce types if "
            "needed (e.g. `as.factor(...)`, `as.numeric(...)`) but never rename data.\n"
            "- Keep the overall data-to-geometry mapping unless the user asks otherwise.\n"
            "- If the user asks to change the title or chart type, reflect it in code "
            "and ALSO declare it with comment lines at the END of the script:\n"
            "  `# CHART_TITLE: <new title>` and/or `# CHART_TYPE: <new chart type>` "
            "(e.g. bar, line, scatter, box, heatmap, histogram). These comment lines "
            "are optional; omit them if unchanged.\n"
            "Output ONLY the R code, no markdown fences, no explanations.\n\n"
            f"Chart title: {info.get('chart_title', '')}\n"
            f"Chart type: {info.get('chart_type', '')}\n"
            f"Columns: {', '.join(info.get('columns', [])) or 'unknown'}\n"
            f"Column types: {col_types}\n"
            f"Data preview (first rows):\n{preview_text}\n\n"
            f"User modification request: {request}\n\n"
            "Current R plotting code:\n```r\n" + original_code + "\n```"
        )
        try:
            resp = self.main_llm.chat(messages=[{"role": "user", "content": prompt}])
            content = (resp or {}).get("content", "") or ""
            # 去除可能的 markdown 代码围栏。
            content = re.sub(r"^```(?:r|R)?\s*", "", content.strip())
            content = re.sub(r"\s*```$", "", content)
            # 解析可选的标题/类型声明行，并从代码中剥离。
            new_title = ""
            new_chart_type = ""
            m_title = re.search(r"^#\s*CHART_TITLE:\s*(.+?)\s*$", content, re.MULTILINE)
            m_type = re.search(r"^#\s*CHART_TYPE:\s*(.+?)\s*$", content, re.MULTILINE)
            if m_title:
                new_title = m_title.group(1).strip()
            if m_type:
                new_chart_type = m_type.group(1).strip().lower()
            content = re.sub(r"^#\s*CHART_(TITLE|TYPE):.*$\n?", "", content, flags=re.MULTILINE)
            return content.strip(), new_title, new_chart_type
        except Exception as e:
            logger.error(f"modify_chart script generation failed: {e}")
            return "", "", ""

    @staticmethod
    def _extract_plot_code(full_script: str) -> str:
        """Strip the sandbox prelude and output directives from a composed
        script, leaving only the user-visible plotting code."""
        marker = full_script.find("# --- output device directives ---")
        if marker != -1:
            full_script = full_script[:marker]
        end_prelude = full_script.rfind("# --- end prelude ---")
        if end_prelude != -1:
            full_script = full_script[end_prelude + len("# --- end prelude ---"):]
        return full_script.strip()

    # -- plot registry persistence -------------------------------------- #
    def _persist_plot_registry(self) -> None:
        """Persist the in-memory plot registry to disk (best-effort)."""
        try:
            from src.core.plot_engine import PlotEngine
            PlotEngine().save_plot_registry(self._plot_registry)
        except Exception as e:
            logger.warning(f"persist plot registry failed: {e}")

    def _load_persisted_plot_registry(self) -> dict:
        """Load the plot registry persisted on disk by previous turns/sessions."""
        try:
            from src.core.plot_engine import PlotEngine
            return PlotEngine().load_plot_registry()
        except Exception as e:
            logger.warning(f"load plot registry failed: {e}")
            return {}

    # ------------------------------------------------------------------ #
    #  Citation extraction
    # ------------------------------------------------------------------ #
    def _collect_mcp_citations(self, tool_name: str, result: str) -> str:
        """
        Parse MCP JSON results for online sources (url / title) and register
        them through ``cite_collector`` so the UI 'Cited Sources' block works.
        Injects the assigned citation id back into each item, then returns the
        (possibly annotated) result string.
        """
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False)
        try:
            res_data = json.loads(result)
            if isinstance(res_data, dict) and "results" in res_data:
                for item in res_data["results"]:
                    if not isinstance(item, dict):
                        continue
                    source_url = item.get("url") or item.get("pdf_url") or item.get("landing_page_url")
                    source_title = (
                        item.get("title") or item.get("name") or item.get("pref_name")
                        or item.get("display_name") or item.get("scientific_name")
                        or f"Result from {tool_name}"
                    )
                    if source_url:
                        ref_id = self.cite_collector({
                            "path": source_url,
                            "page": 1,
                            "name": f"[Online] {source_title}",
                            "search_text": item.get("abstract", "")[:100],
                        })
                        if ref_id:
                            item["_mcp_cite_id"] = ref_id
                return json.dumps(res_data, ensure_ascii=False)
        except json.JSONDecodeError:
            pass
        return result

    # ------------------------------------------------------------------ #
    #  Small helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _emit_tool_start(name: str, args: dict, emit_token: Callable[[str], None]):
        args_str = json.dumps(args, ensure_ascii=False)
        short = args_str if len(args_str) < 120 else args_str[:120] + "..."
        emit_token(
            f"<mcp_process><b>{name}</b><br>"
            f"<span style='font-size:12px; color:#888;'>[Status: Executing] Args: {short}</span></mcp_process>\n"
        )

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        text = text or ""
        if len(text) <= limit:
            return text
        return text[:limit] + "\n...[truncated]"

    @staticmethod
    def _stream_final_text(content: str, emit_token: Callable[[str], None], is_cancelled):
        for i in range(0, len(content), 5):
            if is_cancelled():
                break
            emit_token(content[i:i + 5])
            time.sleep(0.015)

    @staticmethod
    def _emit_cancel_notice(emit_token: Callable[[str], None]):
        emit_token("\n\n[⛔ Generation halted by user.]")

    def cancel(self):
        self._is_cancelled = True
        if hasattr(self.main_llm, "cancel"):
            self.main_llm.cancel()
