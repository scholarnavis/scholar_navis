# AGENTS.md

本文件是进入本仓库的第一入口。目标是让协作者在不通读全仓的前提下，知道每个模块做什么、依赖什么、能提供什么能力、涉及哪些文件。

使用方式：

1. 先读「模块地图」定位到目标模块，再读该模块的「文件清单」确定具体文件。
2. 只读需要的文件片段，不要因为不确定而整文件通读或整目录扫描。
3. 改动前先读「单一事实源」与「已知坑」两节，多数改错都源于绕过了这两处约定。

---

## 1. 项目概览

Scholar Navis 是一个基于 PySide6 的本地化学术研究与文献工具桌面应用，面向计算生物学与分子植物科学。核心能力是本地 RAG、学术数据源工具调用（MCP / Skill）、证据链与引用核对、R 绘图，以及一个可脱离 GUI 运行的 OpenAI 兼容 HTTP API。

- Python 版本：3.12（`pyproject.toml` 限定 `>=3.12, <3.13`）
- 依赖管理：uv，`required-version = ">=0.13.0"`
- 入口：`main.py`（GUI 与 `--api-server` 两种模式）
- 打包：`build_app.py`（Linux 出源码包，Windows/macOS 出 PyInstaller 冻结包）
- 无自动化测试：`tests/` 目录存在但为空，全仓无 pytest/unittest

---

## 2. 目录地图

```
main.py              应用入口，启动链与单实例锁
build_app.py         打包与发布
AGENTS.md            本文件
README.md            对外说明与平台依赖
pyproject.toml       依赖、uv 配置、安全下限
src/core/            无 UI 的核心逻辑
src/core/academic/   LLM 可直接调用的学术与生物数据工具
src/core/agent/      Agent 运行时（plan → execute → observe）
src/task/            后台任务（QThread / 子进程）
src/tools/           UI 面板层，按侧边栏页组织
src/ui/              窗口外壳与组件
src/services/        少量跨层工具
src/api/             本地 HTTP API 与自测脚本
src/core/data/       随包内置的静态样例
Assets/              图标、SVG、前端 JS
build_support/       打包脚本、许可、发布
docs/                仅 4 张 README 配图，无文档
tests/               空目录
```

分层依赖方向：`src/ui` 与 `src/tools` 依赖 `src/task` 与 `src/core`；`src/task` 依赖 `src/core`；`src/core` 不反向依赖 UI。`src/api` 与 GUI 复用同一个 `ChatGenerationTask`，不另写一份管线。

---

## 3. src/core 根目录

模块定位：全部业务逻辑的实现层，不含 Qt 界面代码，但大量使用 Qt 的 `QObject` 与信号。几乎所有模块都直接或间接依赖 `config_manager`。

### 3.1 平台基础与通用工具

| 文件 | 作用 | 关键入口 |
|---|---|---|
| `__init__.py` | 把 `platform_env.app_root()` 固化为 `BASE_DIR`，是可写数据根目录的唯一来源 | `BASE_DIR` |
| `platform_env.py` | 跨平台预检与原语唯一来源：Qt 缺库提示、WebEngine/NixOS 重启、可写根与只读资源根、无窗口标志 | `app_root`、`os_family`、`no_window_flags` |
| `version.py` | 版本号与发布通道判定 | `__version__`、`is_newer_version` |
| `output_paths.py` | 用户可见产物目录（R 图、生成图），带可写性探测与 /tmp 兜底 | `plot_output_dir`、`generated_image_dir` |
| `file_types.py` | 扩展名到文件类别的唯一清单，供引用路由、附件校验、查看器共用 | `IMAGE_EXTS` |
| `image_utils.py` | 图片 MIME 推断、data URL 编码、单图体积上限 | `guess_mime`、`encode_data_url` |
| `lang_detect.py` | 输入语种粗判，用于回答语言与翻译策略 | `detect_primary_language` |
| `network_worker.py` | 网络底座：浏览器头、健壮 Session、全局限速、鉴权熔断、后台 worker、走网络端点的 embedding/rerank | `create_robust_session`、`GlobalRateLimiter` |
| `oa.py` | 开放获取全文探测与可下载 PDF 定位 | `OAFetcher` |
| `rss_paths.py` | RSS 订阅清单与文章缓存落盘路径 | `RSS_CACHE_FILE`、`RSS_FEEDS_FILE` |
| `email_check.py` | 邮箱校验，满足 NCBI 邮箱要求 | `verify_email_robust` |

### 3.2 配置、密钥与开关

依赖 `BASE_DIR`，被全仓依赖。

| 文件 | 作用 | 关键入口 |
|---|---|---|
| `config_manager.py` | 全局配置单例，托管 settings / mcp_servers / llm_config / external_models 四份 JSON，负责内存含密钥、落盘拆明文的边界与环境变量注入 | `ConfigManager`、`apply_env_vars` |
| `secrets_store.py` | 「哪些字段算密钥」的唯一事实来源，拆分与合并两侧都只在这里 | `split_settings`、`merge_settings` |
| `encryption_service.py` | 加解密，生产用途只剩口令型配置导入导出，其余为带删除条件的兼容层 | `SystemEncryptionService` |
| `skill_store.py` | 外部技能源码读写口径，现行明文 `.py`，兼容只读 `.enc` | `read_skill_source` |
| `security_analyzer.py` | AST 静态给技能脚本打分。注意：当前无任何 import 引用，实际生效的是 `ui/components/dialogs/mcp_skill_dialogs.py` 内的同名类，改动前先确认是否死代码 | `SkillSecurityAnalyzer` |
| `feature_flags.py` | 界面功能开关唯一来源，关闭只隐藏入口不改管线 | `TOOLS_FILTER_CHAT_ENABLED` 等常量 |

### 3.3 LLM、模型能力与本地推理

关键约束：`token_estimator` 顶层不得反向 import `model_capabilities`，否则循环导入。

| 文件 | 作用 | 关键入口 |
|---|---|---|
| `llm_impl.py` | LLM 调用主实现，兼容 OpenAI 协议的流式与非流式、think 块分流、参数按能力裁剪、上下文溢出检测、翻译缓存、AI 生图。改动同时影响聊天、Agent、翻译、生图四条链路 | `OpenAICompatibleLLM`、`is_context_overflow_error` |
| `llm_errors.py` | 错误归一化为 `{title, body, details}` 并产出内联错误标记 | `friendly_payload`、`error_marker` |
| `model_capabilities.py` | 上下文窗口、输出上限、不支持参数的唯一合成点，落盘 `config/model_capabilities.json`。调用方只查这里，不要自行推断 | `resolve_window`、`record_unsupported_param` |
| `token_estimator.py` | token 估算与上下文预算推导，tiktoken 预热、CJK 启发式兜底 | `estimate_tokens`、`derive_context_budgets` |
| `models_registry.py` | 本地与外部模型清单、ONNX 下载校验、自动选型。缺失模型抛错，禁止静默联网下载。刻意惰性 import `kb_manager` | `ModelManager`、`ensure_onnx_model` |
| `device_manager.py` | 设备自检单例，GPU 探测与最优设备选择 | `DeviceManager` |
| `onnx_provider.py` | ONNX provider 支持与生效探测，不可用时明确降级 CPU | `resolve_provider`、`probe_provider` |
| `onnx_tokenizer.py` | 轻量 tokenizer，只依赖 `tokenizers` 与 numpy | `OnnxTokenizer` |
| `rerank_engine.py` | 本地 ONNX 交叉编码器重排单例，四态加载状态机，重量级库延迟导入 | `RerankEngine` |

### 3.4 知识库、PDF 与全文

| 文件 | 作用 | 关键入口 |
|---|---|---|
| `kb_manager.py` | `KBManager` 管工作区与元数据，`DatabaseManager` 持 chromadb 客户端与 collection。chromadb 只在真正建客户端时 import | `KBManager`、`DatabaseManager` |
| `pdf_engine.py` | 单篇 PDF 入库流水线：转 Markdown、切块、写向量库 | `PDFEngine.process_pdf` |
| `pdf_index.py` | PDF 本体索引（SQLite/WAL），按 paper_id、doi、sha256 查重与定位，与向量库解耦 | 模块级 lookup/register |
| `pdf_text.py` | PDF 正文抽取唯一实现，markdown 失败降级按页纯文本 | `extract_pdf`、`probe_pdf` |
| `fulltext_store.py` | DOI 与本地 PDF 关联表，指纹与内容双级校验，校验失败即解除关联 | `resolve`、`link`、`unlink` |
| `fulltext_dirs.py` | 收件目录与额外监听目录定义 | `inbox_dir`、`dir_identity` |
| `fulltext_bridge.py` | 「同轮等待」通道：Agent 请求全文后挂起，UI 入库后回填，模型带全文续跑同一轮 | `begin`、`wait`、`signal` |
| `fulltext_samples.py` | 开发者模式取全文自检的样例来源 | `resolve_samples` |

### 3.5 R 引擎与绘图

| 文件 | 作用 | 关键入口 |
|---|---|---|
| `r_engine.py` | Rscript 定位，用户指定优先，惰性检测并缓存，线程安全单例 | `REngine` |
| `r_env.py` | R 包私有库布局与 `R_LIBS_USER` 注入点，流式装包 | `child_env`、`install_packages` |
| `r_runtime.py` | 按需下载 conda-forge 私有 R 运行时，装完实跑验证 | `create_runtime`、`runtime_ready` |
| `r_sandbox.py` | 绘图子进程隔离：ulimit、私有 TMPDIR、`--vanilla`、失败原因翻译 | `plot_argv`、`describe_failure` |
| `plot_engine.py` | R 绘图流水线：数据落 CSV、生成 R 代码、单次调用、收集 SVG/PNG/PDF | `PlotEngine` |
| `plot_styles.py` | 出版级图表外观注册表，LLM 描述与 R 代码共用的唯一事实源 | `resolve_style`、`theme_block` |

### 3.6 证据、引用与提示词

这一组决定输出质量口径，同一规则不允许在两处各写一份。

| 文件 | 作用 | 关键入口 |
|---|---|---|
| `evidence.py` | 会话级逐字证据库，snippet 经归一化后做子串校验，保证引用原文一致 | `EvidenceStore`、`is_usable_source_text` |
| `evidence_gate.py` | 证据门禁，同一份 `unsupported_targets` 同时供给补来源轮与未核实标记 | `unsupported_targets`、`apply_evidence_gate` |
| `provenance.py` | 追加式证据链记录，可选 JSONL 落盘 | `ProvenanceCollector`、`get_collector` |
| `prompt_rules.py` | 跨处复用的提示词规则块唯一来源，其他提示词只能拼装这里的常量 | `LANGUAGE_AND_PUNCTUATION_RULE`、`CITATION_TOKEN_RULE` |
| `references.py` | 参考文献注册表：编号分配、`ref://` token、页脚渲染、悬停卡数据 | `ReferenceRegistry`、`citation_token` |
| `think_tags.py` | 思维链标记唯一解析口径，支持整段 strip 与流式 splitter | `ThinkStreamSplitter`、`strip` |
| `tool_call_leak.py` | 文本形态工具调用的唯一处理口径，展示侧从严剥离、执行侧从宽提取 | `ToolCallStreamFilter` |
| `follow_ups.py` | 回复末尾追问建议块的稳健分离 | `split_follow_ups` |

### 3.7 会话、归档与书签

| 文件 | 作用 | 关键入口 |
|---|---|---|
| `chat_store.py` | 对话与归档共用的 SQLite schema 与读写口径，超长正文 zlib 压缩，资产内容寻址 | `connect`、`put_tab`、`read_tab` |
| `chat_session.py` | 多标签页自动快照，轮次边界整页覆盖、流式半成品节流落盘、flush 钩子 | `save_tab`、`save_inflight`、`flush_hooks` |
| `chat_archive.py` | `.schat` 单文件归档读写，兼容旧 ZIP/JSON | `write_archive`、`read_archive` |
| `chat_assets.py` | 对话资产内嵌策略与体积上限，超限只留占位符与原因码 | `plan_assets`、`store_blob` |
| `chat_typography.py` | 聊天气泡排版参数规格，配置键名唯一生成处，UI 与落盘共用 | `PARAM_BY_NAME`、`config_key`、`heading_metrics` |
| `bookmarks.py` | 书签数据模型与锚点重定位，纯函数算滚动条标注 | `make_bookmark`、`find_anchor`、`layout_marks` |

### 3.8 任务、信号、日志与注册中心

| 文件 | 作用 | 关键入口 |
|---|---|---|
| `core_task.py` | 后台任务框架：`BackgroundTask` 双模式、`TaskManager` 队列与取消、子进程日志 IPC。是任务层与多数 UI 工具的统一入口 | `BackgroundTask`、`TaskManager`、`TaskMode` |
| `signals.py` | 全局 Qt 信号总线单例，跨层通信唯一通道 | `GlobalSignals`、`global_signals` |
| `logger.py` | 日志装配：落盘、GUI 面板信号、Qt 消息处理器、faulthandler | `get_qt_log_handler` |
| `i18n.py` | 界面多语言，英文原串即键，切换需重启 | `tr`、`resolve_language` |
| `locales.py` | 译文静态表，刻意不用数据文件以适配冻结包 | `TRANSLATIONS` |
| `theme_manager.py` | 主题管理：QSS、图标、字体、Windows 标题栏深浅色 | `ThemeManager` |
| `mcp_manager.py` | MCP 客户端管理，stdio/SSE 会话、工具清单、同步调用、状态广播 | `MCPManager`、`call_tool_sync` |
| `skill_manager.py` | 技能注册中心，内置学术技能与外部导入技能，按类型注解生成 OpenAI schema | `SkillManager`、`register_skill` |
| `academic_agent.py` | 兼容层，把 `src/core/academic/` 的函数全部 re-export，保持旧导入路径可用 | 见 `src/core/academic/__init__.py` |

---

## 4. src/core/academic

模块定位：LLM 可直接调用的工具函数池。工具名即函数名，schema 由类型注解自动生成。

依赖：`base.py` 在导入时即执行配置加载与网络环境设置，是全模块的引导层。

| 文件 | 作用 | 主要入口 |
|---|---|---|
| `base.py` | 共享基础设施：UDP 日志、凭据读取、`mcp_request` 统一请求与熔断、指数退避重试。不定义数据源抽象 | `get_setting_or_env`、`mcp_request`、`simple_retry` |
| `ncbi.py` | NCBI E-utilities 封装：序列、分类、组学数据、通用库检索 | `fetch_sequence_fasta`、`fetch_taxonomy_info`、`search_omics_datasets` |
| `literature.py` | 多库文献检索、引用图谱、OA 全文获取 | `search_academic_literature`、`traverse_citation_graph`、`fetch_paper_fulltext` |
| `bio_databases.py` | 13 个生物数据库查询：GBIF、KEGG、QuickGO、ChEMBL、UniProt、AlphaFold、PDB、PubChem/ChEBI、STRING、g:Profiler、MyGene、JASPAR、Ensembl | `query_uniprot_database`、`analyze_systems_network` 等 |
| `web_tools.py` | 通用网络能力：网页正文、搜索、维基、GitHub | `fetch_webpage_content`、`search_web` |
| `plotting.py` | 用 R 渲染出版级图表，接收声明式 spec | `plot_chart` |
| `__init__.py` | 统一 re-export 全部对外函数 | `__all__` |

新增文献数据源的约定：在 `source` 字面量与 `wanted` 组装处登记，新增 `_parse_X()` 返回同构记录 dict，在 `_query_db` 加分派并共用 `global_rate_limiter`，异常自行吞掉返回空列表。若要暴露成独立工具，还需在 `__init__.py` 的 `__all__`、`academic_agent.py` 转发清单、`skill_manager.core_tools` 三处登记。

---

## 5. src/core/agent

模块定位：单轮对话内的 Agent 执行器。不碰 UI，输出通过注入的回调。

| 文件 | 作用 | 关键入口 |
|---|---|---|
| `runtime.py` | Agent 主循环，驱动多轮工具调用、分派、引用收集、流式出图、上下文溢出降级。分派顺序为内置工具、本地 Skill、MCP | `AgentRuntime.run` |
| `planner.py` | 意图分析与语义聚焦提示，不裁剪工具 | `IntentPlanner.plan` |
| `decomposer.py` | 深度模式拆分子任务 | `TaskDecomposer.decompose` |
| `synthesizer.py` | 深度模式合成多路子任务结果并包装引用 | `Synthesizer.synthesize` |
| `skill_registry.py` | 为每个 Skill 生成自描述元数据 | `SkillRegistry.build` |

依赖注入：`main_llm`、`skill_manager`、`mcp_manager`、`planner`、`cite_collector`、`log_fn`、`plot_registry`、`reference_registry`、`evidence_store`，运行期再注入 `fulltext_waiter`。

与任务层的衔接：`src/task/chat_tasks.py` 的 `ChatGenerationTask` 组装工具清单并构造 `AgentRuntime`，深度模式为每个子任务新建一个 runtime 并行执行，再由 `Synthesizer` 合成。

---

## 6. src/tools

模块定位：侧边栏每个页面对应一个 UI 面板，面板再组合 mixin。注意这里有两套「工具」概念，不要混淆：`BaseTool` 是 UI 面板，LLM 可调用的工具在 `skill_manager` 注册。

| 文件 | 作用 |
|---|---|
| `base_tool.py` | 所有面板的抽象基类，只需实现 `get_ui_widget()` |
| `chat_tool.py` | 对话助手页容器，管理多标签页 |
| `chat_conversation.py` | 单个会话页，组合 7 个 chat mixin |
| `chat_input_widgets.py` | 输入区控件：附件 chip、拖放、自适应文本框 |
| `import_tool.py` | 知识库导入管理面板，已从主界面下线，代码保留 |
| `rss_tool.py` | 文献订阅追踪 |
| `settings_tool.py` | 全局设置外壳，组合 7 个 section mixin |
| `log_tool.py` | 系统日志面板 |
| `about_tool.py` | 关于与版本信息 |

`chat_mixins/`：`send_flow.py` 派发查询并启动后台任务，`response_flow.py` 流式渲染与收尾，`bubbles.py` 气泡与滚动，`search.py` 会话内搜索，`bookmarks.py` 书签，`attachments.py` 附件与导出，`session.py` 自动保存与恢复。

`settings_sections/`：`env_section.py` 环境与凭据，`llm_section.py` 与 `llm_model_ops.py` 模型配置，`model_section.py` 模型下载队列，`mcp_section.py` MCP 与技能管理，`fulltext_section.py` 全文监听目录，`save_flow.py` 保存流程，`transfer.py` 配置导入导出。

新增面板：写一个继承 `BaseTool` 的类并实现 `get_ui_widget()`，然后在 `src/ui/main_window.py` 的 `tool_classes` 中加入三元组，并在 `icon_map` 补齐图标。

---

## 7. src/ui

模块定位：窗口外壳与可复用组件，不含业务逻辑。

| 文件 | 作用 |
|---|---|
| `main_window.py` | 窗口外壳：侧边栏与堆叠页、懒加载面板、主题套用、跨面板路由、启动检查、退出时持久化会话。自身不 import 任务层 |
| `components/` | 见下 |

`components/` 分组：

- 聊天与消息渲染：`chat_bubble.py`（单条消息渲染与交互，含卡片标记抽取）、`text_formatter.py`（Markdown 到富文本的唯一渲染管线）、`chat_tabs.py`（对话标签栏，只发信号不认业务）、`card_registry.py`（卡片标记协议与快照恢复）、`pill_button.py`（追问建议）、`error_panel.py`（统一错误面板）
- 输入与选择：`combo.py`、`kb_combo.py`、`model_selector.py`、`param_editor.py`、`HoverRevealLineEdit.py`、`copy_button.py`、`search.py`
- 对话框：`dialog.py` 兼容层、`dialogs/` 子包（`base.py` 基类、`common.py` 通用框、`about_dialogs.py`、`feed_dialogs.py`、`kb_project_dialogs.py`、`mcp_skill_dialogs.py`、`r_env_dialogs.py`、`chat_typography_dialog.py`）。`dialogs/__init__.py` 用 PEP 562 惰性再导出，避免启动期拉起重依赖
- 文档查看：`pdf_viewer.py`、`text_viewer.py`、`document_search.py`、`mermaid_viewer.py`、`source_code_viewer.py`、`fulltext_request.py`、`image_viewer.py`、`file_dialogs.py`
- 可视化与卡片：`plot_plan_card.py`、`r_plot_card.py`、`deep_plan_card.py`、`ask_user_card.py`
- 通用：`toast.py`、`RotatingSpinner.py`、`bookmark_ui.py`、`developer_dialog.py`、`citation_popup.py`

三个高风险大文件的边界：

- `developer_dialog.py` 是测试工装，不是产品 UI，由 About 页版本号连点 5 次进入。它直接 import 任务层做真实链路验证，任何 core 或 task 的接口改名都会在这里集中报错。
- `chat_bubble.py` 只管一条消息怎么显示以及交互后发什么信号，不做存储、不发网络请求、不定义卡片协议，但建卡逻辑仍在其中。
- `text_formatter.py` 是无状态排版管线，改动会同时影响聊天气泡、文本查看器、Markdown 导出三条链路。

排版参数全部来自 `src/core/chat_typography.py`，面板显示值、气泡渲染值、落盘键名必须同源，任何一处硬编码都会漂移。

---

## 8. src/task

模块定位：后台任务层。基类不在 `common_task.py`，而在 `src/core/core_task.py`。

任务约定：

- 继承 `BackgroundTask`，只实现 `_execute()`，构造函数签名固定为 `(task_id, task_queue, kwargs)`
- 不要 import 或触碰 Qt，任务可能跑在 spawn 出来的子进程里
- 反馈只用 `update_progress`、`send_log`、`stream_token`；长循环里插 `if self.is_cancelled()`
- `TaskManager` 是单任务模型，`start_task` 会先取消上一个任务，因此每个面板各持一个实例
- 网络 IO 与轻交互用 `TaskMode.THREAD`，吃 CPU、载模型、可能崩溃的用 `TaskMode.PROCESS`
- 大 payload 用 `{"_is_temp_file": True, "path": ...}` 交给后台反序列化

| 文件 | 作用 |
|---|---|
| `common_task.py` | 版本检查与模型完整性校验 |
| `chat_tasks.py` | 对话主管线 `ChatGenerationTask`，覆盖附件装载、翻译策略、知识库检索与重排、深度研究、流式输出、引用定稿、证据链落盘；另有导出导入会话、图片下载、硬件状态任务 |
| `config_task.py` | 配置导入导出 |
| `dialog_task.py` | MCP 连接测试与对话框任务薄壳 |
| `hf_download_task.py` | 模型下载，桥接进度与测速 |
| `kb_tasks.py` | 知识库全生命周期任务，每个 `_execute` 首行都要重建子进程环境 |
| `r_env_task.py` | R 包与运行时安装 |
| `rss_tasks.py` | RSS 抓取、检索、导入导出 |
| `s2_task.py` | 不是任务类，是 Semantic Scholar 限流与熔断单例 |
| `settings_tasks.py` | 设置页所有测试与探测动作 |
| `startup_tasks.py` | 硬件探测预热 |

---

## 9. src/services 与 src/api

- `src/services/file_service.py`：纯静态工具，打开文件、定位目录、读取 docx 与文本。注意其文本解析与 `chat_tasks.py` 内的附件解析是两份重复实现，属待清理点。
- `src/api/api_server.py`：FastAPI 应用，OpenAI 兼容外壳加本家扩展。直接在 `threading.Thread` 里跑 `ChatGenerationTask`，再把内部 token 流翻译成 SSE。鉴权在 `api_server_key` 为空时不校验。
- `src/api/api_diagnostic_tool.py`：独立 CLI 自测脚本，含端点健康检查与 SSE 对话回放，可作手工验证入口。
- API 模式只能通过 `main.py --api-server` 启动，GUI 内无开关，且与 GUI 共享单实例锁。

---

## 10. 启动与打包

`main.py` 的关键顺序：配置 Qt 环境变量 → 平台预检与可能的接管重启 → 异常钩子 → `spawn` 启动方式 → 图形会话与提权检查 → 创建应用与单实例锁 → 日志与主题 → 启动 worker 分阶段预热 → 建主窗口 → 延迟触发首次运行检查与 MCP 引导。

两条必须遵守的线程约束：`GlobalSignals()` 必须在主线程创建；`HardwareInitTask` 必须由主线程发起。原因都是 `QTimer` 与 `QObject` 的线程亲和性。

`build_app.py`：Linux 走源码包白名单复制并附带 `run.sh`，Windows 与 macOS 走 PyInstaller `--onedir`。惰性导入的模块必须显式 `--collect-submodules`，否则静态分析发现不了。构建期有多处硬校验，缺资源直接失败而不是产出残缺包。

---

## 11. 单一事实源

改动下列内容必须同步所有引用点，不允许在各处各写一份：

| 事实 | 唯一来源 | 同步要求 |
|---|---|---|
| 可写数据根目录 | `platform_env.app_root()` | 影响配置、日志、模型缓存、输出、工作区 |
| 配置字段与密钥清单 | `config_manager` + `secrets_store` | 改字段名要同时改密钥键清单 |
| 上下文窗口与参数支持 | `model_capabilities` | 业务侧一律只查，不自行推断 |
| 扩展名分类 | `file_types.py` | 引用路由、附件校验、查看器三方共用 |
| 提示词规则块 | `prompt_rules.py` | 其他提示词只能拼装常量 |
| 证据门禁规则 | `evidence_gate.unsupported_targets` | 补来源与未核实标记必须同一份 |
| 引用编号与 token | `references.py` | 渲染、悬停卡、导出共用 |
| 卡片标记协议 | `ui/components/card_registry.py` | 建卡逻辑在 `chat_bubble`，协议只在这里 |
| 排版参数与配置键 | `chat_typography.py` | 面板、渲染、落盘同源 |
| 图表外观 | `plot_styles.py` | LLM 描述与 R 代码共用 |
| 依赖版本下限 | `pyproject.toml` | 验证过的安全下限，不要随手降低 |

---

## 12. 扩展指南

新增后台任务：在 `src/task/` 对应文件写 `XxxTask(BackgroundTask)`，只实现 `_execute()`，参数走 `kwargs`，需要环境隔离的在首行重建环境变量，返回 dict 作为 payload，调用方自建 `TaskManager` 并连接三个信号。

新增学术工具：在 `src/core/academic/` 对应模块写函数并补类型注解，在 `__init__.py` 的 `__all__` 与 `academic_agent.py` 转发清单登记，再在 `skill_manager.core_tools` 加函数与 `[Tags: ...]` 描述，schema 自动生成。

新增 UI 面板：继承 `BaseTool` 实现 `get_ui_widget()`，在 `main_window.tool_classes` 与 `icon_map` 登记。

新增配置项：在 `config_manager` 加字段，若属密钥同步 `secrets_store`，界面入口加到 `settings_sections` 的对应 mixin。

---

## 13. 已知坑

1. `security_analyzer.py` 无任何 import 引用，实际生效的是 UI 侧同名类，改动前先确认是否死代码。
2. `common_task.py` 名不副实，不含基类；`s2_task.py` 不含任务类。都只是放在该目录下。
3. `file_service.py` 与 `chat_tasks.py` 的附件解析重复，修改解析逻辑时要两处一起看，或先合并。
4. `token_estimator` 与 `model_capabilities` 存在方向性依赖约束，不要反向 import。
5. `dialogs/__init__.py` 与 `dialog.py` 是惰性再导出，静态分析找不到依赖，打包时必须显式收集子模块。
6. `tests/` 为空，`docs/` 只有图片。不要为了找测试或文档去扫描这两个目录。
7. 主界面已下线 `ImportTool`，但代码保留；多处硬编码侧边栏索引，0 是聊天、2 是设置。
8. 侧边栏面板是懒加载的，`main_window` 不会 import 任务层，从窗口往下找任务链路会断在 `ChatTool`。
