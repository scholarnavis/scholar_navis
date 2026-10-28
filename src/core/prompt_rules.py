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
    "metadata you submit (the app renders it in the citation popup); in your prose, cite with the "
    "citation token or its [n] number and convey the evidence in the answer language. Reproduce the "
    "original wording ONLY when the user explicitly asks for it, and then keep that quotation in its "
    "own language and punctuation.\n"
    "   - When writing in Chinese: use FULL-WIDTH punctuation — Chinese commas（，）, periods（。）, "
    "semicolons（；）, colons（：）, question/exclamation marks（？！）, Chinese ellipsis（……）, and Chinese "
    "parentheses（）for parenthetical remarks. Use Chinese curly quotes（“” and ‘’）for quotations "
    "instead of straight or half-width quotes.\n"
    "   - When writing in English or other languages: follow that language's standard punctuation "
    "conventions (half-width punctuation and straight quotes for English).\n"
    "   - CRITICAL EXCEPTION: do NOT modify these machine-parsed tokens under any circumstance: "
    "in-text citation tokens and their [n] numbers, the literal [FOLLOW_UPS] header, JSON blocks, "
    "code fences (```...```), mermaid code blocks, tool names, identifiers, and URLs. Keep those "
    "exactly half-width ASCII.\n"
    "   - Terminology is NOT a language switch: keep gene / protein / species names, database ids, "
    "DOIs, URLs and tool names in their canonical form instead of translating them."
)


#: 正文引用 token 的出现方式：**编号由工具生成**，模型只照抄。
#: 主 Agent 系统提示词与 cite_references 工具描述共用这一份——两处口径不一致时，
#: 模型就会自己编数字或写 ASCII 别名，编号与来源随之脱钩。
CITATION_TOKEN_RULE = (
    "Inline citation tokens — the number is assigned by the app, never by you: "
    "(a) A tool result item (literature / MCP / web search, skill output) carries a "
    "'citation_token' field whose value is ready-made hyperlink text such as "
    "<a href=\"ref://cite?n=3\">[3]</a>; paste it VERBATIM at the sentence it supports. "
    "Knowledge-base documents are numbered in the Context as '--- [Document n] ---'; "
    "write [n] with exactly that number. "
    "(b) For a source that carries no number and no token at all, call cite_references "
    "and paste the 'citation_token' values it returns. "
    "(c) NEVER invent, shift, renumber or re-type a number, and NEVER write an ASCII alias "
    "such as [lariguet2004] — only app-assigned numbers and tokens identify a source. "
    "Final numbering (1..K by order of appearance) is applied by the app when the answer "
    "completes; a number no tool handed you is dropped at that point and yields no entry."
)


#: 工具执行失败时的行为约束。
#:
#: 触发场景：本机没装某个生信工具、装了但缺动态库、平台不支持、帮助尚未读取。这些
#: 都意味着"分析根本没有执行"，而模型手里的工具描述依然存在，很容易滑向"我分析了
#: 该文件……"这类编造。放在系统提示里统一约束，而不是在每条错误里重复叮嘱——后者
#: 只要有一条漏写，模型就有空子可钻。
TOOL_EXECUTION_FAILURE_RULE = (
    "### TOOL EXECUTION FAILURES (STRICT):\n"
    "A tool result whose 'status' is 'error', 'unavailable' or 'help_required' means the "
    "analysis did NOT run. In that case you MUST NOT describe, summarise, estimate or imply "
    "any result — no counts, no sequences, no tables, no 'the tool returned ...'.\n"
    "- 'help_required': read it with bio_tool_help, then retry the same call once.\n"
    "- 'unavailable': tell the user which tool is missing and why (the 'message' field "
    "states whether it is not installed or installed but broken), then point to the "
    "External Tools page for installation. If 'alternatives' lists another tool covering "
    "the same job, you may use that instead and say so explicitly.\n"
    "- 'error': quote the failure reason in one line and stop. Retry the same call at most "
    "once, and only when the reason suggests a transient cause.\n"
    "Never present a command you did not successfully run as if it had been executed, and "
    "never fill the gap with numbers from memory or from an earlier unrelated run."
)
