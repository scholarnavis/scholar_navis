"""界面译表（**集中存放，唯一来源**）。

键 = 源码中的英文原串（源语言即键，见 :mod:`src.core.i18n` 的设计说明）；
值 = 目标语言文本。新增界面文案时，只需在调用点写英文原串，
再把对应译文补到本文件；缺条目会自动回退英文并记一条 warning（见 i18n.py）。

约定
----
* ``TRANSLATIONS`` 的顶层键是**语言代码**（与 :data:`src.core.i18n.SUPPORTED_LANGUAGES`
  对齐），源语言 ``en`` 不在此登记。
* 条目按界面区域分组并保持与源码一致的顺序，便于对照维护。
* 含 ``{占位符}`` 的条目（长 HTML 帮助块、动态文案）必须**原样保留占位符名**，
  占位符在调用点以 ``str.format`` 注入，故译文中不得出现多余的裸花括号。
* **不翻译**的内容（日志、LLM 提示词、协议标记、工具内部标识符）不应出现在这里。
"""

TRANSLATIONS: dict[str, dict[str, str]] = {
    "zh_CN": {
        # ================= 主窗口：标题与侧边栏导航 =================
        "Scholar Navis - Research Assistant": "Scholar Navis - 科研助手",
        "Library Manager": "知识库管理",
        "Chat Assistant": "对话助手",
        "Literature Tracker": "文献追踪",
        "Global Settings": "全局设置",
        "System Logs": "系统日志",
        "About": "关于",

        # ================= 启动页（Splash） =================
        "AI-Powered Research Assistant": "AI 驱动的科研助手",
        "Initializing engine...": "正在初始化引擎…",
        "Detecting hardware info...": "正在检测硬件信息…",
        "Loading model registry framework...": "正在加载模型注册框架…",
        "Loading user settings...": "正在加载用户设置…",
        "Loading system configuration & network profiles...": "正在加载系统配置与网络配置…",
        "Scanning local hardware & compute engines (Background)...": "正在扫描本地硬件与计算引擎（后台）…",
        "Mounting theme cache and UI assets...": "正在挂载主题缓存与界面资源…",
        "Loading MCP Subsystem metadata...": "正在加载 MCP 子系统元数据…",
        "Pre-loading UI components & ML libraries...": "正在预加载界面组件与机器学习库…",
        "Ready. Building workspace...": "就绪，正在构建工作区…",
        "Ready. Initializing workspace...": "就绪，正在初始化工作区…",

        # ================= 通用按钮 / 操作 =================
        " Add": " 添加",
        " Delete": " 删除",
        " Download": " 下载",
        " Fetch": " 获取",
        " Refresh": " 刷新",
        " Test": " 测试",
        " Adjust...": " 调整…",
        " Save Settings": " 保存设置",
        " Revert Changes": " 撤销更改",
        " Export Config": " 导出配置",
        " Import Config": " 导入配置",
        "Browse...": "浏览…",
        " Open Model Storage Directory": " 打开模型存储目录",
        " Test Compute Device": " 测试计算设备",
        " Add MCP Server": " 添加 MCP 服务",
        " Import Native Skill": " 导入原生技能",
        "Import Native Skill": "导入原生技能",
        " Refresh Status": " 刷新状态",
        " Parameter Help": " 参数说明",
        " Add Provider Parameter": " 添加 Provider 参数",
        " Copy from Provider": " 从 Provider 复制",
        " Add Model Parameter": " 添加模型参数",
        "Checking...": "检查中…",
        "Success": "成功",
        "Error": "错误",
        "Warning": "警告",
        "Unknown": "未知",
        "Unknown error": "未知错误",

        # ================= 设置页：系统偏好 =================
        "System Preferences": "系统偏好设置",
        "Theme:": "主题：",
        "Log Level:": "日志级别：",
        "Interface Language:": "界面语言：",
        "Follow System": "跟随系统",
        "Chat text:": "聊天气泡：",
        "Adjust font size, letter spacing, line spacing and paragraph spacing for LLM and user chat bubbles (with live preview).":
            "调整 LLM 与用户聊天气泡的字号、字符间距、行距与段间距（带实时预览）。",

        # ================= 设置页：系统硬件信息 =================
        "System Hardware Info": "系统硬件信息",
        "Scanning hardware info... Please wait.": "正在扫描硬件信息，请稍候…",
        "OS:": "操作系统：",
        "CPU:": "CPU：",
        "RAM:": "内存：",
        "GPU(s):": "GPU：",
        "ONNX Engine:": "ONNX 引擎：",
        "Providers:": "执行提供者：",
        "None detected": "未检测到",
        "Hardware Accelerated": "硬件加速",
        "CPU Fallback": "CPU 回退",

        # ================= 设置页：R 环境 =================
        "R Environment (Visualization)": "R 环境（可视化）",
        "Detecting R environment...": "正在检测 R 环境…",
        "Rscript path, e.g. C:\\Program Files\\R\\R-4.3.1\\bin\\Rscript.exe":
            "Rscript 路径，例如 C:\\Program Files\\R\\R-4.3.1\\bin\\Rscript.exe",
        "Rscript path, e.g. /usr/bin/Rscript (leave empty to auto-detect from PATH)":
            "Rscript 路径，例如 /usr/bin/Rscript（留空则从 PATH 自动探测）",
        "Select Rscript executable": "选择 Rscript 可执行文件",
        "R detected": "已检测到 R",
        "R not detected": "未检测到 R",
        "Status:": "状态：",
        "Rscript:": "Rscript：",
        "Version:": "版本：",
        "Visualization requires R.<br>Download and install it from <a href='{url}'>{url}</a>, then specify the Rscript path above or add it to PATH.":
            "可视化功能需要 R。<br>请从 <a href='{url}'>{url}</a> 下载并安装，然后在上方指定 Rscript 路径，或将其加入 PATH。",
        "R packages:": "R 包：",
        "all core packages available": "核心包齐全",
        "R packages missing:": "缺少 R 包：",

        # ================= 设置页：网络代理 =================
        "Network Proxy": "网络代理",
        "Disable Proxy (Direct)": "禁用代理（直连）",
        "Enable Proxy (Custom)": "启用代理（自定义）",
        "Proxy Mode:": "代理模式：",
        "Proxy URL:": "代理地址：",
        "HF Mirror:": "HF 镜像：",
        "e.g. http://127.0.0.1:7890": "例如：http://127.0.0.1:7890",
        "Leave empty for default (huggingface.co)": "留空使用默认（huggingface.co）",

        # ================= 设置页：AI 模型配置 =================
        "AI Models Configuration": "AI 模型配置",
        "Model Storage:": "模型存储：",
        "Verifying...": "校验中…",
        "Embedding:": "嵌入模型：",
        "Reranker:": "重排模型：",
        "Compute Device:": "计算设备：",
        "Detecting devices...": "正在检测设备…",
        "Device Not Available": "设备不可用",
        "This device is listed for information only and cannot be used.":
            "该设备仅作说明展示，无法使用。",
        "Device Connection Test": "设备连接测试",
        "Testing inference device '{device}'...": "正在测试推理设备 '{device}'…",
        "Test Passed": "测试通过",
        "Test Failed": "测试失败",
        "Model configuration not found for {model}": "未找到模型配置：{model}",
        "Confirm Delete": "确认删除",
        "Are you sure you want to delete the local cache for '{repo}'?\nThis will free up disk space by removing the ONNX files.":
            "确定要删除 '{repo}' 的本地缓存吗？\n这将通过移除 ONNX 文件来释放磁盘空间。",
        "Successfully deleted {repo}": "已成功删除 {repo}",
        "Failed to delete model: {err}": "删除模型失败：{err}",
        "Model cache not found locally.": "本地未找到模型缓存。",
        "Model Required": "需要模型",
        "The model '{model}' is required for this operation but is not installed.\n\nIt has been auto-selected in the list. Please click the blue 'Save Settings & Verify Models' button below to download it.":
            "此操作需要模型 '{model}'，但本地尚未安装。\n\n已在下拉列表中自动选中。请点击下方蓝色的“保存设置并校验模型”按钮进行下载。",
        "Ready (Network API) | {info}": "就绪（网络 API）| {info}",
        "Ready (ONNX verified) | {info}": "就绪（ONNX 已校验）| {info}",
        "ONNX Not Found | {info}": "未找到 ONNX | {info}",
        "Target: {v}": "目标：{v}",
        "Repo: {v}": "仓库：{v}",
        "Downloading": "下载中",
        "Initializing...": "初始化…",
        "Complete": "完成",
        "All downloads finished.": "全部下载已完成。",
        "Download Halted": "下载已中止",
        "Task ended: {msg}": "任务结束：{msg}",

        # 显存策略说明（HTML 模板；{muted}/{success}/{danger} 由调用点注入色值）
        "<div style='font-size: 11px; color: {muted}; line-height: 1.5; margin-left: 20px;'>"
        "<b>Turn ON (Low VRAM):</b> Frees up memory immediately after document retrieval.<br>"
        "&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:{success};'>Pros: Maximizes LLM context length, prevents Out-of-Memory (OOM) crashes.</span><br>"
        "&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:{danger};'>Cons: Adds 1~3s loading delay to every new query.</span><br>"
        "<b>Turn OFF (Speed Mode):</b> Keeps RAG models persistently in memory.<br>"
        "&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:{success};'>Pros: Lightning-fast multi-turn conversation.</span><br>"
        "&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:{danger};'>Cons: Embedding + Reranker will constantly occupy VRAM/RAM.</span>"
        "</div>":
            "<div style='font-size: 11px; color: {muted}; line-height: 1.5; margin-left: 20px;'>"
            "<b>开启（低显存）：</b>文档检索完成后立即释放内存。<br>"
            "&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:{success};'>优点：最大化 LLM 上下文长度，避免内存溢出（OOM）崩溃。</span><br>"
            "&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:{danger};'>缺点：每次新提问会增加 1~3 秒的加载延迟。</span><br>"
            "<b>关闭（速度优先）：</b>将 RAG 模型常驻内存。<br>"
            "&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:{success};'>优点：多轮对话响应极快。</span><br>"
            "&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:{danger};'>缺点：嵌入与重排模型会持续占用显存/内存。</span>"
            "</div>",

        # ================= 设置页：LLM 生成 API =================
        "LLM Generation API": "LLM 生成 API",
        "Unnamed Provider": "未命名 Provider",
        "Custom Parameter Guide": "自定义参数指南",
        "You can specify request parameters (e.g., temperature, top_p, max_tokens) for the provider or specifically for a model.\n\n• Priority: Model Custom > Provider Inherit\n• If 'Closed' is selected for a model, no parameters are appended.\n• The model dropdown indicates your configuration with (⚙️ Custom) or (🚫 Closed).":
            "你可以为 Provider 或某个具体模型指定请求参数（例如 temperature、top_p、max_tokens）。\n\n• 优先级：模型自定义 > Provider 继承\n• 若某模型选择 'Closed'，则不附加任何参数。\n• 模型下拉框用 (⚙️ Custom) 或 (🚫 Closed) 标示你的配置。",
        "Service Provider:": "服务提供商：",
        "Provider Name:": "Provider 名称：",
        "API Base URL:": "API 基础地址：",
        "API Key:": "API 密钥：",
        "Provider Params:": "Provider 参数：",
        "Model Name:": "模型名称：",
        "Params Strategy:": "参数策略：",
        "Inherit (Provider)": "继承（Provider）",
        "Custom (Model Only)": "自定义（仅此模型）",
        "Closed (No Params)": "关闭（不加参数）",
        "Copies global provider parameters to the current model.":
            "把 Provider 的全局参数复制到当前模型。",
        "Built-in default providers cannot be deleted.": "内置默认 Provider 不可删除。",
        "New Provider": "新 Provider",
        "Model retrieval is currently unsupported by the MiniMax provider. Click to restore predefined models.":
            "MiniMax 暂不支持自动获取模型列表。点击可恢复预置模型。",
        "MiniMax model list refreshed (defaults restored).": "MiniMax 模型列表已刷新（已恢复预置模型）。",
        "Provider has no parameters to copy.": "Provider 没有可复制的参数。",
        "Parameters copied and merged successfully.": "参数已成功复制并合并。",
        "Duplicate Parameter": "参数重复",
        "Parameter '{name}' already exists in this model.\n\n【Current Model Parameter】\n  • Type: {cur_type}\n  • Value: {cur_value}\n\n【Provider Parameter to Copy】\n  • Type: {new_type}\n  • Value: {new_value}\n\nDo you want to overwrite the model's parameter with the provider's?":
            "参数 '{name}' 在当前模型中已存在。\n\n【当前模型参数】\n  • 类型：{cur_type}\n  • 取值：{cur_value}\n\n【待复制的 Provider 参数】\n  • 类型：{new_type}\n  • 取值：{new_value}\n\n是否用 Provider 的参数覆盖该模型的参数？",
        "Delete Model": "删除模型",
        "Are you sure you want to remove '{model}' from the list?": "确定要从列表中移除 '{model}' 吗？",
        "Please enter API Base URL first.": "请先填写 API 基础地址。",
        "Network Request": "网络请求",
        "Contacting API...": "正在连接 API…",
        "Please ensure Base URL and Model Name are provided.": "请确保已填写基础地址与模型名称。",
        "API Connection Test": "API 连接测试",
        "Sending test prompt to '{model}'...": "正在向 '{model}' 发送测试提示…",
        "Fetch Failed": "获取失败",

        # ================= 设置页：AI Agent 与外部工具（MCP） =================
        "AI Agent & External Tools": "AI Agent 与外部工具",
        "<b>Manage Local & Remote Tools:</b>": "<b>管理本地与远程工具：</b>",
        "Enabled": "启用",
        "Name": "名称",
        "Description": "描述",
        "Type": "类型",
        "Target": "目标",
        "Status": "状态",
        "Action": "操作",
        "💡 <i>Changes to MCP servers require clicking the blue 'Save Settings & Verify' button below to take effect.</i>":
            "💡 <i>对 MCP 服务的更改需要点击下方蓝色的“保存设置并校验”按钮后才会生效。</i>",
        "Ready (Native)": "就绪（原生）",
        "Not Loaded": "未加载",
        "Connected": "已连接",
        "Disabled": "已禁用",
        "Script not found or failed to load. Check logs.": "脚本未找到或加载失败，请查看日志。",
        "Core service must remain enabled.": "核心服务必须保持启用。",
        "Built-in Academic Tools cannot be disabled here.": "内置学术工具无法在此禁用。",
        "Core system service (Read-only)": "核心系统服务（只读）",
        "Core service '{name}' cannot be edited here.": "核心服务 '{name}' 无法在此编辑。",
        "Refreshing external tool states...": "正在刷新外部工具状态…",
        "Are you sure you want to delete tool '{name}'?\nThis will disconnect it immediately.":
            "确定要删除工具 '{name}' 吗？\n该工具会立即断开连接。",
        "Security Warning": "安全警告",
        "The name '{name}' is reserved for core system usage.": "名称 '{name}' 为系统核心用途保留。",
        "⚠️ HIGH RISK OPERATION": "⚠️ 高风险操作",
        "User Imported Native Script": "用户导入的原生脚本",
        "Skill '{name}' staged. Click 'Save Settings' to commit.":
            "技能 '{name}' 已暂存。点击“保存设置”以提交。",
        "Import Successful": "导入成功",
        "Import Failed": "导入失败",
        "Skill '{name}' has been encrypted and secured.": "技能 '{name}' 已加密并妥善保存。",
        "Skill '{name}' imported successfully.": "技能 '{name}' 导入成功。",
        "Unknown error during encryption.": "加密过程中出现未知错误。",
        "Failed to load new script: {err}": "加载新脚本失败：{err}",
        "Failed to decrypt existing skill: {err}": "解密已有技能失败：{err}",

        # 外部 MCP 安全声明（HTML 模板；{danger}/{bold} 由调用点注入）
        "<b>⚠️ Security Disclaimer for External MCP Servers</b><br><br>"
        "You are about to connect a third-party MCP server to Scholar Navis.<br>"
        "External servers are highly privileged and can execute code, read local files, or access the network on your behalf. "
        "<span style='color:{danger}; font-weight:{bold};'>Only connect to servers from trusted developers.</span><br><br>"
        "<i>The Scholar Navis developers are not responsible for any data loss, security breaches, or system damage caused by third-party MCP servers.</i><br><br>"
        "Do you understand the risks and wish to proceed?":
            "<b>⚠️ 外部 MCP 服务安全声明</b><br><br>"
            "你即将把第三方 MCP 服务连接到 Scholar Navis。<br>"
            "外部服务拥有很高权限，可以代表你执行代码、读取本地文件或访问网络。"
            "<span style='color:{danger}; font-weight:{bold};'>请仅连接来自可信开发者的服务。</span><br><br>"
            "<i>对于第三方 MCP 服务造成的任何数据丢失、安全泄露或系统损坏，Scholar Navis 开发者概不负责。</i><br><br>"
            "你是否已了解相关风险并希望继续？",

        # Native Skill 导入的高危提示（HTML 模板；{danger}/{bold} 由调用点注入）
        "<b>🚨 CRITICAL SECURITY WARNING: NATIVE SKILL IMPORT</b><br><br>"
        "You are attempting to import a Native Python Skill (`.py` script) directly into the main process of Scholar Navis.<br><br>"
        "<span style='color:{danger}; font-weight:{bold};'>1. ARBITRARY CODE EXECUTION:</span> These scripts run with the EXACT SAME privileges as the main application. Malicious scripts can steal your data, delete files, or compromise your system.<br>"
        "<span style='color:{danger}; font-weight:{bold};'>2. STRICT SANDBOXING:</span> The script MUST ONLY import Python Standard Library modules (e.g., `os`, `json`, `urllib`). Importing third-party pip packages (like `requests`, `pandas`) that are not packaged with Navis will instantly crash the agent with a `ModuleNotFoundError`.<br><br>"
        "<i>Only import scripts from absolutely trusted sources. Do you accept all risks and wish to proceed?</i>":
            "<b>🚨 严重安全警告：导入原生技能</b><br><br>"
            "你正试图把一个原生 Python 技能（`.py` 脚本）直接导入 Scholar Navis 的主进程。<br><br>"
            "<span style='color:{danger}; font-weight:{bold};'>1. 任意代码执行：</span>这些脚本以与主程序**完全相同**的权限运行。恶意脚本可以窃取你的数据、删除文件或破坏系统。<br>"
            "<span style='color:{danger}; font-weight:{bold};'>2. 严格沙箱限制：</span>脚本只能导入 Python 标准库模块（例如 `os`、`json`、`urllib`）。导入未随 Navis 打包的第三方 pip 包（如 `requests`、`pandas`）会让 Agent 立即以 `ModuleNotFoundError` 崩溃。<br><br>"
            "<i>请仅从绝对可信的来源导入脚本。你是否接受全部风险并希望继续？</i>",

        # ================= 设置页：配置导入 / 导出 =================
        "Save Config": "保存配置",
        "Security Export": "安全导出",
        "Performing compression, encryption & serialization...": "正在压缩、加密并序列化…",
        "Export Successful": "导出成功",
        "Configuration bundle has been securely saved to:\n{path}": "配置包已安全保存至：\n{path}",
        "Write Error": "写入错误",
        "Failed to write file to disk: {err}": "写入文件到磁盘失败：{err}",
        "Export Failed": "导出失败",
        "An analytical error occurred during encryption.": "加密过程中出现解析错误。",
        "Import Config Bundle": "导入配置包",
        "Import Error": "导入错误",
        "Invalid JSON file: {err}": "无效的 JSON 文件：{err}",
        "Importing": "导入中",
        "Reading and decrypting...": "正在读取并解密…",
        "Decryption Failed": "解密失败",
        "Incorrect password or corrupted file.\nWould you like to try entering the password again?":
            "密码错误或文件已损坏。\n要重新输入密码吗？",
        "Failed to restore skill {name}: {err}": "恢复技能 {name} 失败：{err}",
        "Imported device '{device}' is unavailable. Defaulting to {fallback}.":
            "导入的设备 '{device}' 不可用，已回退到 {fallback}。",
        "Configuration bundle ({mode}) has been loaded into the interface.\n\nPlease click 'Save Settings' at the bottom to apply these changes permanently.":
            "配置包（{mode}）已载入界面。\n\n请点击底部的“保存设置”以使这些更改永久生效。",
        "Configuration imported to UI ({mode}). Please save to apply.":
            "配置已载入界面（{mode}）。请保存以生效。",
        "Failed to apply settings to UI:\n{err}": "将设置应用到界面失败：\n{err}",

        # ================= 设置页：保存流程 =================
        "Applying Settings": "正在应用设置",
        "Validating settings and email address...": "正在校验设置与邮箱地址…",
        "Validation Error": "校验错误",
        "Saving configurations...": "正在保存配置…",
        "Initializing background tasks...": "正在初始化后台任务…",
        "Process Halted": "进程已中止",
        "Save process ended: {msg}": "保存流程结束：{msg}",
        "Settings saved successfully.": "设置已成功保存。",
        "Note: Some selected models are missing locally. Please click 'Download' next to the models to fetch and convert them.":
            "注意：部分已选模型在本地缺失。请点击模型旁的“下载”以获取并转换它们。",
        "Note: Interface language has been changed. Please restart the application to apply it.":
            "注意：界面语言已更改。请重启应用以生效。",
        "Settings Saved": "设置已保存",

        # ================= 设置页：本地 API 服务器 =================
        "Local API Server (OpenAI Compatible)": "本地 API 服务器（OpenAI 兼容）",
        "Host Address:": "主机地址：",
        "Server Port:": "服务端口：",
        "Access Key:": "访问密钥：",
        "e.g., 127.0.0.1 or 0.0.0.0": "例如：127.0.0.1 或 0.0.0.0",
        "Default: 8000": "默认：8000",
        "Set a custom API Key to secure your local endpoint (Optional)":
            "设置自定义 API 密钥以保护本地端点（可选）",
        "💡 <i>API Server runs in the background. It shares all active models, RAG, and MCP settings with the GUI. Restart the application to apply port/host changes.</i>":
            "💡 <i>API 服务器在后台运行，与 GUI 共享所有已启用模型、RAG 与 MCP 设置。修改端口/主机后需重启应用才会生效。</i>",

        # ================= 设置页：应用接口（API 密钥） =================
        "Application Interface (API Keys)": "应用接口（API 密钥）",
        "Required for NCBI Tools: e.g. user@university.edu": "NCBI 工具必填：例如 user@university.edu",
        "NCBI API Key (Optional but recommended)": "NCBI API 密钥（可选但推荐）",
        "OpenAlex Premium API Key (Optional)": "OpenAlex 高级 API 密钥（可选）",
        "Semantic Scholar Key (Prevents 429 Errors)": "Semantic Scholar 密钥（可避免 429 错误）",
        "S2 Rate Limit (requests/sec, default: 1.0)": "S2 速率限制（请求/秒，默认 1.0）",
        "GitHub Personal Access Token (Prevents rate limiting)":
            "GitHub 个人访问令牌（可避免速率限制）",
        "NCBI Email:": "NCBI 邮箱：",
        "NCBI API Key:": "NCBI API 密钥：",
        "OpenAlex Key:": "OpenAlex 密钥：",
        "S2 API Key:": "S2 API 密钥：",
        "S2 Rate Limit (req/s):": "S2 速率限制（请求/秒）：",
        "GitHub Token:": "GitHub 令牌：",

        # API 密钥说明（HTML 模板；色值/字重由调用点注入）
        "<div style='line-height: 1.5;'>"
        "<span style='color:{warning}; font-weight:{bold};'>⚠️ NCBI RATE LIMITS:</span> "
        "You MUST provide a valid email address to use NCBI tools. An API Key is "
        "<span style='color:{success}; font-weight:{bold};'>optional but highly recommended</span>. "
        "Without a key, tools will still function but under strict rate limits, which may slow down massive literature retrieval.<br><br>"
        "<span style='color:{accent}; font-weight:{bold};'>INFO & API Keys:</span><br>"
        "• <b>NCBI PubMed:</b> Email is mandatory. Adding an API key increases rate limits from 3 to 10 requests/sec. "
        "<a href='https://account.ncbi.nlm.nih.gov/settings/' style='color:{accent}; text-decoration:none;'>[Apply for NCBI Key]</a><br>"
        "• <b>OpenAlex:</b> Works without a key, but <span style='color:{warning};'>the daily quota is low and 429 Too Many Requests is common</span>. A <b>free</b> API key (sign in with an email, no payment) raises the daily quota 10&times;; paid plans raise it further. "
        "<a href='https://openalex.org/settings/api-key' style='color:{accent}; text-decoration:none;'>[Get OpenAlex API Key]</a><br>"
        "• <b>Semantic Scholar:</b> An API Key severely prevents '429 Too Many Requests' errors during massive literature retrieval. "
        "<a href='https://www.semanticscholar.org/product/api' style='color:{accent}; text-decoration:none;'>[Apply for S2 Key]</a><br>"
        "• <b>GitHub Token:</b> Increases search limits from 10/min to 30/min. "
        "<a href='https://github.com/settings/tokens?type=beta' style='color:{accent}; text-decoration:none;'>[Generate Token]</a>"
        "</div>":
            "<div style='line-height: 1.5;'>"
            "<span style='color:{warning}; font-weight:{bold};'>⚠️ NCBI 速率限制：</span>"
            "使用 NCBI 工具**必须**提供有效的邮箱地址。API 密钥"
            "<span style='color:{success}; font-weight:{bold};'>可选但强烈推荐</span>。"
            "不填密钥时工具仍可用，但会受到严格限流，可能拖慢大批量文献检索。<br><br>"
            "<span style='color:{accent}; font-weight:{bold};'>说明与 API 密钥：</span><br>"
            "• <b>NCBI PubMed：</b>邮箱为必填。添加 API 密钥可把限流从 3 次/秒提升到 10 次/秒。"
            "<a href='https://account.ncbi.nlm.nih.gov/settings/' style='color:{accent}; text-decoration:none;'>[申请 NCBI 密钥]</a><br>"
            "• <b>OpenAlex：</b>无密钥也可用，但<span style='color:{warning};'>每日配额较低，经常出现 429 Too Many Requests</span>。<b>免费</b> API 密钥（用邮箱登录、无需付费）可把每日配额提升 10 倍；付费方案更高。"
            "<a href='https://openalex.org/settings/api-key' style='color:{accent}; text-decoration:none;'>[获取 OpenAlex API 密钥]</a><br>"
            "• <b>Semantic Scholar：</b>API 密钥可大幅减少大批量文献检索时的 '429 Too Many Requests' 错误。"
            "<a href='https://www.semanticscholar.org/product/api' style='color:{accent}; text-decoration:none;'>[申请 S2 密钥]</a><br>"
            "• <b>GitHub 令牌：</b>把搜索限流从 10 次/分提升到 30 次/分。"
            "<a href='https://github.com/settings/tokens?type=beta' style='color:{accent}; text-decoration:none;'>[生成令牌]</a>"
            "</div>",

        # ================= 关于页 =================
        "Current Release: v{version}": "当前版本：v{version}",
        " Website": " 官网",
        " GitHub": " GitHub",
        " Licenses": " 许可证",
        " Data Providers": " 数据提供方",
        "Licensed under AGPL-3.0 | © {year} {company}": "基于 AGPL-3.0 授权 | © {year} {company}",
        "Third-party components are listed under Licenses, including the optional R runtime and its plotting packages.":
            "第三方组件列于“许可证”中，包括可选的 R 运行时及其绘图包。",
        "Click {n} more time(s) to open developer mode": "再点击 {n} 次可打开开发者模式",
        " (dev channel)": "（dev 通道）",
        "New version {version}{note} available!": "发现新版本 {version}{note}！",
        "Release notes": "更新日志",
        "Download": "下载",

        # 「关于」页免责声明（HTML，无占位符）
        "<b>IMPORTANT DISCLAIMER</b><br><br>"
        "Scholar Navis uses Large Language Models (LLMs). While augmented with RAG and MCP, "
        "AI-generated content may still contain <b>inaccuracies or hallucinations</b>. Users are <b>strictly required</b> "
        "to verify information via provided citations/links. Developers are not liable for any research errors or "
        "academic misconduct arising from the use of this tool.":
            "<b>重要免责声明</b><br><br>"
            "Scholar Navis 使用大语言模型（LLM）。尽管叠加了 RAG 与 MCP，"
            "AI 生成的内容仍可能<b>存在不准确或幻觉</b>。用户<b>必须</b>通过所提供的引用/链接自行核实信息。"
            "对于因使用本工具而产生的任何研究错误或学术不端，开发者概不负责。",

        # ================= 系统日志页 =================
        "<h2>System Run Logs</h2>": "<h2>系统运行日志</h2>",
        " Search": " 搜索",
        " Clear": " 清空",
        "Search logs (Ctrl+F)...": "搜索日志（Ctrl+F）…",
        "Previous (Shift+Enter)": "上一个（Shift+Enter）",
        "Next (Enter)": "下一个（Enter）",
        "Close Search": "关闭搜索",

        # ================= 知识库管理页 =================
        "Project / Library Management": "项目 / 知识库管理",
        " New": " 新建",
        " Import .snp": " 导入 .snp",
        " Edit": " 编辑",
        " Add Files": " 添加文件",
        " Export Project": " 导出项目",
        " Save & Apply All Changes": " 保存并应用所有更改",
        "Changes Staging": "更改暂存",
        "Ready.": "就绪。",
        "Select a library...": "请选择一个知识库…",
        "Filename": "文件名",
        "Size": "大小",

        # 知识库状态（tr(status.upper()) 动态取键，三个状态都要登记）
        "READY": "就绪",
        "CORRUPTED": "已损坏",
        "BUILDING": "构建中",
        "[{status}] {text}": "[{status}] {text}",
        "Not Downloaded": "未下载",
        "Unknown/External": "未知/外部",
        "{name}   [Model: {model} | Docs: {docs}]": "{name}   ［模型：{model}｜文档数：{docs}］",
        "{count} files ({size} MB)": "{count} 个文件（{size} MB）",
        "Project:": "项目：",
        "Domain:": "领域：",
        "Model:": "模型：",
        "Storage:": "存储：",

        # 文件表格状态（机器可读 key 见 import_tool._STATUS_LABELS）
        "Indexed": "已索引",
        "Renaming...": "重命名中…",
        "Pending Save": "待保存",
        "Unsupported (.docx required)": "不支持（需要 .docx）",

        # 右键菜单 / 重命名
        "Open Source File": "打开源文件",
        "Rename (Stage)": "重命名（暂存）",
        "Delete {n} items (Stage)": "删除 {n} 项（暂存）",
        "Rename File": "重命名文件",
        "New name (Extension '{ext}' will be auto-added):": "新名称（将自动补全扩展名 '{ext}'）：",
        "Cancel": "取消",
        "Confirm": "确认",

        # 暂存状态条
        "Staged: {added} add, {deleted} del, {renamed} rename": "暂存：新增 {added}，删除 {deleted}，重命名 {renamed}",
        " | Info Edited": " | 信息已修改",
        " | FULL REBUILD": " | 需全量重建",
        "KB IS {status}. Locked. Please click 'Edit' -> 'Save' to rebuild, or 'Del'.":
            "知识库状态为 {status}，已锁定。请点击“编辑”->“保存”以重建，或“删除”。",

        # 提交 / 任务结果
        "Action Required": "需要操作",
        "The selected AI model weights are missing or incomplete. Please go to 'Global Settings' to download the model first.":
            "所选 AI 模型权重缺失或不完整。请先前往“全局设置”下载模型。",
        "Synchronizing": "正在同步",
        "Synchronizing database and file index...": "正在同步数据库与文件索引…",
        "Info Updated": "信息已更新",
        "Project metadata saved successfully.": "项目元数据已成功保存。",
        "Task Terminated": "任务已中止",
        "The process was interrupted. The library may be corrupted and require a full rebuild.":
            "进程被中断。知识库可能已损坏，需要完整重建。",
        "The library has been fully synchronized and indexed.": "知识库已完成同步与索引。",
        "Operation ended: {msg}": "操作已结束：{msg}",
        "Operation failed: {msg}": "操作失败：{msg}",
        "Model Incomplete": "模型不完整",
        "The required AI model files are missing or corrupted. Would you like to go to Settings to download them now?":
            "所需的 AI 模型文件缺失或已损坏。要现在前往设置下载吗？",
        "DANGER": "危险",
        "Confirm deletion of '{name}'?": "确定要删除 '{name}' 吗？",

        # 添加文件 / 查重
        "Select Documents": "选择文档",
        "Documents (*.pdf *.md *.txt *.doc *.docx)": "文档 (*.pdf *.md *.txt *.doc *.docx)",
        "Legacy .doc format detected. It will be skipped. Please convert to .docx":
            "检测到旧版 .doc 格式，将被跳过。请转换为 .docx",
        "Large Batch": "大批量导入",
        "You are importing {n} files. This might take a while. Continue?":
            "你正在导入 {n} 个文件，可能需要一些时间。是否继续？",
        "Checking Duplicates": "正在查重",
        "Scanning file signatures...": "正在扫描文件指纹…",
        "Checking {name}...": "正在检查 {name}…",
        "Cancelled": "已取消",
        "File scanning was cancelled by user.": "文件扫描已被用户取消。",
        "Skipped {n} duplicate files.": "已跳过 {n} 个重复文件。",

        # 导出 / 导入项目
        "Export Project": "导出项目",
        "Scholar Navis Project (*.snp);;Zip Archive (*.zip)":
            "Scholar Navis 项目 (*.snp);;Zip 压缩包 (*.zip)",
        "Exporting Project": "正在导出项目",
        "Preparing to pack...": "正在准备打包…",
        "Export Success": "导出成功",
        "Project has been successfully exported.": "项目已成功导出。",
        "Export Halted": "导出已中止",
        "Import Project": "导入项目",
        "Project Bundle (*.snp *.zip)": "项目包 (*.snp *.zip)",
        "Importing Project": "正在导入项目",
        "Reading archive...": "正在读取压缩包…",
        "Import Success": "导入成功",
        "Project has been successfully imported.": "项目已成功导入。",
        "Import Halted": "导入已中止",

        # 模型下载 / 打开源文件
        "Downloader": "下载器",
        "Connecting...": "正在连接…",
        "Model downloaded successfully.": "模型下载成功。",
        "Download task was cancelled.": "下载任务已被取消。",
        "Status: {text}": "状态：{text}",
        "Failed to open file: {err}": "打开文件失败：{err}",

        # ================= 文献追踪页 =================
        "Manage Subscriptions": "管理订阅",
        " Options": " 选项",
        "Add Custom Source": "添加自定义源",
        "Edit Source": "编辑源",
        "Unsubscribe Selected": "退订所选",
        "Import Feeds": "导入订阅源",
        "Export Feeds": "导出订阅源",
        "Last Fetched: {time}": "上次抓取：{time}",
        "Never": "从未",
        "Sync Selected": "同步所选",
        "Search feeds...": "搜索订阅源…",
        "Select All": "全选",
        "Invert": "反选",
        "Analyze Selected": "分析所选",
        "Please check at least one article to analyze.": "请至少勾选一篇要分析的文献。",
        "Export to PDF": "导出为 PDF",
        "Searching...": "搜索中…",
        "Searching for '{query}'...": "正在搜索 '{query}'…",
        "Search failed: {msg}": "搜索失败：{msg}",
        "No articles found matching '{query}'": "未找到与 '{query}' 匹配的文献。",
        "Found {n} relevant articles.": "找到 {n} 篇相关文献。",

        # 文章卡片
        "Open Access (OA)": "开放获取（OA）",
        "Unknown Date": "未知日期",
        " Quick Translate": " 快速翻译",
        " Send to Chat": " 发送到对话",
        " Download OA Article": " 下载 OA 文献",
        " Publisher Link": " 出版商链接",

        # 订阅源管理
        "Custom source added successfully.": "自定义源添加成功。",
        "Please select a feed from the list on the left to edit.": "请在左侧列表中选择要编辑的订阅源。",
        "Export RSS Feeds": "导出 RSS 订阅源",
        "Import RSS Feeds": "导入 RSS 订阅源",
        "JSON Files (*.json)": "JSON 文件 (*.json)",
        "Feeds exported successfully.": "订阅源导出成功。",
        "Export failed: {err}": "导出失败：{err}",
        "Imported {n} new feeds successfully.": "成功导入 {n} 个新订阅源。",
        "Import failed: {err}": "导入失败：{err}",
        "Fetch Checked / Clicked": "抓取已勾选 / 所点项",
        "Unsubscribe Checked / Clicked": "退订已勾选 / 所点项",
        "Please check the box next to the feeds you want to sync.": "请勾选要同步的订阅源前的复选框。",
        "Confirm Bulk Unsubscribe": "确认批量退订",
        "Are you sure you want to remove {n} feeds from your tracker?": "确定要从追踪列表中移除 {n} 个订阅源吗？",
        "Unsubscribed {n} feeds successfully.": "成功退订 {n} 个订阅源。",
        "Subscriptions updated. Current active feeds: {n}.": "订阅已更新。当前有效订阅源：{n} 个。",
        "Built-in Default Source (Cannot edit)": "内置默认源（不可编辑）",
        "Edit Custom Source": "编辑自定义源",
        "Category: {category}\nSource: {name}\nURL: {url}": "分类：{category}\n来源：{name}\nURL：{url}",

        # 抓取
        "Fetching Literature": "正在抓取文献",
        "Syncing {n} feeds...": "正在同步 {n} 个订阅源…",
        "Cancelling... Waiting for tasks to safely terminate.": "正在取消…等待任务安全终止。",
        "Task Cancelled": "任务已取消",
        "Background fetch task successfully terminated.": "后台抓取任务已成功终止。",
        "Literature synced successfully.": "文献同步成功。",
        "Fetch Halted": "抓取已中止",
        "No data available. Select feed and click 'Sync' to pull data.": "暂无数据。请选择订阅源并点击“同步”以拉取数据。",

        # 导出 PDF
        "Please select at least one article to export.": "请至少选择一篇要导出的文献。",
        "PDF Files (*.pdf)": "PDF 文件 (*.pdf)",
        "Exporting to PDF": "正在导出为 PDF",
        "Preparing document layout...": "正在准备文档排版…",
        "Cancelling export... cleaning up temp files...": "正在取消导出…正在清理临时文件…",
        "Generated: {time}": "生成时间：{time}",
        "Open Access": "开放获取",
        "Calculating pages...": "正在计算页数…",
        "Export Cancelled": "导出已取消",
        "PDF export cancelled. Temporary file cleaned.": "PDF 导出已取消，临时文件已清理。",
        "Export cancelled, but partial file is locked by system.": "导出已取消，但部分文件被系统锁定。",
        "Rendering page {current} of {total}...": "正在渲染第 {current} / {total} 页…",
        "Successfully exported {n} pages to PDF.": "成功导出 {n} 页到 PDF。",

        # ================= 对话助手：输入区 =================
        " Main Model:": " 主模型：",
        " KB:": " 知识库：",
        " Pinned": " 固定",
        " Hover": " 悬停",
        " Collapsed": " 折叠",
        "Academic Agent": "学术 Agent",
        "Enable built-in native academic skills (Zero Latency)": "启用内置原生学术技能（零延迟）",
        "External Tools": "外部工具",
        "Enable external MCP servers and custom Python scripts": "启用外部 MCP 服务与自定义 Python 脚本",
        "Deep Mode": "深度研究",
        "Deep research mode.\nOff (default): single-agent answer, faster.\nOn: decompose the query into parallel sub-investigations, then synthesize a section-by-section answer (broader coverage, higher cost)":
            "深度研究模式。\n关闭（默认）：单 Agent 作答，更快。\n开启：把问题分解为并行子调查，再分节汇总（覆盖更广，成本更高）",
        "Tools Filter": "工具过滤",
        " (Tip: Selecting fewer tools improves accuracy)": "（提示：工具选得越少，准确率越高）",
        "Tools Filter: Fetching...": "工具过滤：获取中…",
        "Tools Filter: None": "工具过滤：无",
        "🏷️ Tools Filter: None": "🏷️ 工具过滤：无",
        "⏳ No active skills or MCP servers...": "⏳ 没有启用的技能或 MCP 服务…",
        "Tools Filter: Error": "工具过滤：错误",
        "Tools Filter: All": "工具过滤：全部",
        "Tools Filter: {n} selected": "工具过滤：已选 {n} 个",
        "Export": "导出",
        "Import": "导入",
        "Clear": "清空",
        "Attach": "附件",
        "Send": "发送",
        "Stop": "停止",
        "Stopping...": "正在停止…",
        "Load a previously exported chat history (.schat / .json lossless, or best-effort .md / .txt / .csv)":
            "载入此前导出的聊天记录（.schat / .json 无损，或尽力还原的 .md / .txt / .csv）",
        "Clear all attached contexts": "清除所有已附加的上下文",
        "Please wait for file upload to complete...": "请等待文件上传完成…",
        "Answer the pending question card in the chat first.": "请先回答对话中的待答问题卡。",
        "Context Attached": "已附加上下文",
        "📎 Context Attached": "📎 已附加上下文",
        "📎 Attached: {name}": "📎 已附加：{name}",
        "Ask a question... (Enter to send, Shift+Enter for new line)":
            "输入问题…（Enter 发送，Shift+Enter 换行）",
        "Knowledge base updated. Clear history to resume chat.": "知识库已更新。请清空历史以继续对话。",
        "The linked knowledge base or model has changed. Continuing may cause context inconsistency. Please click 'Clear' to reset history.":
            "关联的知识库或模型已变更，继续对话可能导致上下文不一致。请点击“清空”重置历史。",

        # ================= 对话助手：拖拽与附件芯片 =================
        "Drop files here to attach": "拖放文件到此处以附加",
        "Unsupported file format.": "不支持的文件格式。",
        "Remove this image": "移除此图片",
        "Remove this attachment": "移除此附件",
        "{name}\n{path}\nClick to open": "{name}\n{path}\n点击打开",

        # ================= 对话助手：附件管理 =================
        "No image found in clipboard.": "剪贴板中未找到图片。",
        "Failed to save clipboard image.": "保存剪贴板图片失败。",
        "Select from Knowledge Base": "从知识库选择",
        "Upload Local File": "上传本地文件",
        "Paste Image from Clipboard": "从剪贴板粘贴图片",
        "Legacy .doc format detected. It may not be fully parsed. Please convert to .docx":
            "检测到旧版 .doc 格式，可能无法完整解析。请转换为 .docx",
        "Some files were skipped: {names}": "部分文件已被跳过：{names}",
        "Attached {n} file(s).": "已附加 {n} 个文件。",
        "{first}, {second} and {rest} more": "{first}、{second} 等 {rest} 个",
        "{n} image(s)": "{n} 张图片",
        "Image not found: {name}": "图片未找到：{name}",
        "Image '{name}' exceeds {limit} MB limit.": "图片 '{name}' 超过 {limit} MB 上限。",
        "Cannot read image '{name}': {err}": "无法读取图片 '{name}'：{err}",
        "Failed to rasterize SVG '{name}'. The file cannot be sent to models.":
            "SVG '{name}' 栅格化失败，该文件无法发送给模型。",
        "Image file not found: {name}": "图片文件未找到：{name}",
        "File not found: {name}": "文件未找到：{name}",

        # ================= 对话助手：导出 / 导入记录 =================
        "There are currently no chat records to export.": "当前没有可导出的聊天记录。",
        "Export as PDF": "导出为 PDF",
        "Export as MD": "导出为 MD",
        "Export as TXT": "导出为 TXT",
        "Export as Archive (.schat, lossless, for re-import)": "导出为归档（.schat，无损，可重新导入）",
        "PDF Document (*.pdf)": "PDF 文档 (*.pdf)",
        "Markdown File (*.md)": "Markdown 文件 (*.md)",
        "Scholar Navis Archive (*.schat)": "Scholar Navis 归档 (*.schat)",
        "Text File (*.txt)": "文本文件 (*.txt)",
        "Export Log": "导出记录",
        "Exporting Chat": "正在导出对话",
        "Processing file in background...": "正在后台处理文件…",
        "Export Complete": "导出完成",
        "Saved to {name}": "已保存至 {name}",
        "{n} attachment(s) embedded.": "已内嵌 {n} 个附件。",
        "{n} attachment(s) not found (original path kept).": "有 {n} 个附件未找到（保留原路径）。",
        "Document successfully exported.": "文档导出成功。",
        "Import Chat History": "导入聊天记录",
        "Chat History (*.schat *.json *.md *.txt *.csv);;Scholar Navis Lossless (*.schat *.json);;Markdown (*.md);;Text (*.txt);;CSV (*.csv)":
            "聊天记录 (*.schat *.json *.md *.txt *.csv);;Scholar Navis 无损 (*.schat *.json);;Markdown (*.md);;文本 (*.txt);;CSV (*.csv)",
        "Importing Chat": "正在导入对话",
        "Reading and parsing chat history...": "正在读取并解析聊天记录…",
        "No chat messages were found in the file.": "文件中未找到任何聊天消息。",
        "Lossless (full fidelity).": "无损（完整保真）。",
        "Best-effort text import (rich content such as citations/images may be reduced to plain text).":
            "尽力还原的文本导入（引用/图片等富内容可能被降级为纯文本）。",
        "Found {n} message(s).\n{note}\n\nThis will replace the current conversation. Continue?":
            "找到 {n} 条消息。\n{note}\n\n这将替换当前对话。是否继续？",
        "Import Complete": "导入完成",
        "Imported {n} message(s) from:\n{name}": "从以下位置导入 {n} 条消息：\n{name}",
        "Failed to render chat history:\n{err}": "渲染聊天记录失败：\n{err}",
        "Imported {n} message(s).": "已导入 {n} 条消息。",

        # ================= 对话助手：对话流程 =================
        "No Knowledge Base (Direct Chat)": "无知识库（直接对话）",
        "The knowledge base was modified. Chat is currently locked.": "知识库已被修改，对话当前已锁定。",
        "Chat history cleared.": "聊天记录已清空。",
        "Please answer the pending question card above first (or press Stop to dismiss it).":
            "请先回答上方待答的问题卡（或按“停止”关闭它）。",
        "Cannot edit: The current library has been modified. Please clear chat.":
            "无法编辑：当前知识库已被修改。请清空对话。",
        "Cannot send: the current library has been modified. Please clear chat.":
            "无法发送：当前知识库已被修改。请清空对话。",
        "You can only edit your most recent message.": "只能编辑你最近的一条消息。",
        "Empty plotting requirement.": "绘图需求为空。",
        "Empty answer.": "答案为空。",
        "The plan is empty.": "计划为空。",
        "Waiting dismissed. You can chat normally now.": "等待已解除，现在可以正常对话。",
        "The AI generation has been stopped by the user.": "AI 生成已被用户停止。",
        "Generation failed due to an error.": "生成因错误而失败。",
    },
}
