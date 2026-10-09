"""File-type knowledge: 扩展名集合与判定。

纯标准库实现（不依赖 Qt），可被 UI 进程、后台任务与子进程安全导入。

集中维护"哪个扩展名属于哪一类文件"这一份知识，供三处共用，避免各自漂移：

- ``ui.components.text_formatter.handle_link_click``：``cite://`` 链接交给
  内部查看器还是系统默认程序；
- ``tools.chat_mixins.attachments`` / ``tools.chat_input_widgets``：哪些文件
  可以作为聊天附件上传（文件对话框过滤 + 拖拽 / 粘贴校验）；
- ``ui.components.text_viewer.InternalTextViewer``：按 Markdown 渲染、语法
  高亮还是纯文本展示。

新增格式只改本文件，不要再在别处另写一套清单；否则会出现"能打开却传不进去"
或"传进来了却当纯文本显示"这类不一致。
"""
import os

from src.core.image_utils import IMAGE_EXTENSIONS

logger = __import__("logging").getLogger(__name__)

#: 图片扩展名（不含点、小写）。与 :mod:`src.core.image_utils` 同源，
#: 后者保留带点的原始常量供 MIME 推断使用。
IMAGE_EXTS = frozenset(ext.lstrip('.').lower() for ext in IMAGE_EXTENSIONS)

#: 需按 Markdown 渲染的扩展名（:data:`TEXT_VIEWER_EXTS` 的子集）。
MARKDOWN_EXTS = frozenset({"md", "markdown", "mdown", "mkd"})

#: 内部文本查看器（InternalTextViewer）可直接打开的纯文本类扩展名。
TEXT_VIEWER_EXTS = frozenset({
    # Markdown
    "md", "markdown", "mdown", "mkd",
    # 纯文本 / 日志
    "txt", "text", "log", "rst",
    # 分隔符 / 数据交换
    "csv", "tsv", "json", "json5", "jsonl", "ndjson",
    # 配置 / 结构化数据（Pygments 提供语法高亮）
    "yaml", "yml", "toml", "ini", "cfg", "conf", "properties", "env",
    # 标记语言 / 脚本（Pygments 提供语法高亮）
    "xml", "xsd", "xsl", "xslt", "plist", "html", "htm",
    "py", "sh", "bash", "zsh", "r", "sql", "tex", "bib", "js", "ts", "css",
})

#: 二进制 / 需专用解析器的文档扩展名：PDF 走内部 PDF 查看器，
#: DOCX 走 python-docx（见 task.chat_tasks 的附件解析分支）。
DOCUMENT_EXTS = frozenset({"pdf", "docx"})

#: 允许作为聊天附件上传的扩展名全集（文档 + 纯文本类 + 图片）。
#: 上传后进入 LLM 上下文的解析方式由 task.chat_tasks 决定（PDF/DOCX 专用解析，
#: 其余按 chardet 探测编码当纯文本读取）。
ATTACHABLE_EXTS = TEXT_VIEWER_EXTS | DOCUMENT_EXTS | IMAGE_EXTS


def file_extension(name_or_path: str) -> str:
    """取小写扩展名（不含点）；无扩展名时返回空串。"""
    return os.path.splitext(str(name_or_path or ""))[1].lower().lstrip(".")


def is_markdown(name_or_path: str) -> bool:
    """是否为需按 Markdown 渲染的文件。"""
    return file_extension(name_or_path) in MARKDOWN_EXTS


def is_text_viewable(name_or_path: str) -> bool:
    """是否可由内部文本查看器直接打开。"""
    return file_extension(name_or_path) in TEXT_VIEWER_EXTS


def is_attachable(name_or_path: str) -> bool:
    """是否允许作为聊天附件上传。"""
    return file_extension(name_or_path) in ATTACHABLE_EXTS
