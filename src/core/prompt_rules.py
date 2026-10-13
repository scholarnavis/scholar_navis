"""跨提示词 / 工具描述复用的规则块 —— **唯一事实来源**。

为什么单独放一个模块
--------------------
这些口径原本分别写在多处模型可见文本里：主 Agent 的系统提示词
（``src/task/chat_tasks.py``）、Deep 模式的综合提示词
（``src/core/agent/synthesizer.py``）、``cite_references`` 工具描述
（``src/core/agent/runtime.py``）。同一件事写两遍，措辞一漂移就会自相矛盾——
例如系统提示词要求"已有编号就用编号"，工具描述却要求"每个来源都自拟 key"，
模型自然两边都不遵守，于是正文里冒出没登记过的 ``[hu2019cotton]``。

凡这些规则只在本模块定义；其他提示词与工具描述一律拼装本模块的常量，不得
再自写一份。
"""

#: 输出语言与标点规则。文案即提示词正文（英文），直接拼进 system prompt。
LANGUAGE_AND_PUNCTUATION_RULE = (
    "### OUTPUT LANGUAGE & PUNCTUATION (STRICT):\n"
    "The user's input may already have been translated to English for retrieval, so do NOT infer the "
    "reply language from the query. Write the ENTIRE answer — prose, headings, list items and quoted "
    "material — in ONE language: the user's own language (declared above as the reply language whenever "
    "it is not English). Do NOT mix language strands: never paste a passage written in another language "
    "into the answer body — a Chinese answer must not carry an English source paragraph, and an English "
    "answer must not carry a Chinese one. A character-for-character excerpt belongs to the citation "
    "metadata you submit (the app renders it in the citation popup); in your prose, cite with [key] or "
    "[n] and convey the evidence in the answer language. Reproduce the original wording ONLY when the "
    "user explicitly asks for it, and then keep that quotation in its own language and punctuation.\n"
    "   - When writing in Chinese: use FULL-WIDTH punctuation — Chinese commas（，）, periods（。）, "
    "semicolons（；）, colons（：）, question/exclamation marks（？！）, Chinese ellipsis（……）, and Chinese "
    "parentheses（）for parenthetical remarks. Use Chinese curly quotes（“” and ‘’）for quotations "
    "instead of straight or half-width quotes.\n"
    "   - When writing in English or other languages: follow that language's standard punctuation "
    "conventions (half-width punctuation and straight quotes for English).\n"
    "   - CRITICAL EXCEPTION: do NOT modify these machine-parsed ASCII tokens under any circumstance: "
    "in-text citation markers written as [key] or [1], the literal [FOLLOW_UPS] header, JSON blocks, "
    "code fences (```...```), mermaid code blocks, tool names, identifiers, and URLs. Keep those "
    "exactly half-width ASCII.\n"
    "   - Terminology is NOT a language switch: keep gene / protein / species names, database ids, "
    "DOIs, URLs and tool names in their canonical form instead of translating them."
)


#: 正文引用 token 的选择规则：已经有编号的来源一律用编号，只有没有编号的来源才
#: 自拟 key。主 Agent 系统提示词与 cite_references 工具描述共用这一份——两处口径
#: 不一致时，模型会既不给已编号来源用编号，也不登记自拟 key。
CITATION_TOKEN_RULE = (
    "Inline citation tokens — STRICT PRIORITY ORDER: "
    "(a) If the source ALREADY carries a number, cite that EXACT number and never invent a key for it. "
    "Knowledge-base documents are numbered in the Context as '--- [Document n] ---'; tool results may "
    "carry a numeric id field (e.g. '_mcp_cite_id') assigned to online / MCP / skill sources. "
    "(b) ONLY for a source that carries no number at all, choose a short unique ASCII key "
    "(e.g. [lariguet2004]) and reuse that exact key everywhere the source is cited. "
    "NEVER invent, shift or renumber a number; NEVER keep an inline key that you do not register with "
    "cite_references in the same reply — an unregistered key is dropped from the answer, so the claim "
    "ends up uncited."
)
