"""工作区产物归属的**唯一事实源**。

"某次对话用到的工作区文件"，指消息里出现过、且真实存在于本机工作区根目录下的
文件。它和"用户上传的附件"是两回事：附件有 ``external_files`` 这类结构化字段，
而工作区产物只会以三种形态出现在消息里：

1. 正文里的裸绝对路径（模型把工具返回的 ``local_path`` / ``run_dir`` 抄进了回答）；
2. 正文里的 ``file://`` 图片地址（AI 生图落盘在 ``output/generated_images``）；
3. 组件标记内的成品文件（R 图卡，走 :mod:`src.ui.components.card_registry`）。

本模块只回答两个问题：哪些目录算工作区产物、一段文本里哪些路径真的落在里面。
归档收集（:func:`src.core.chat_assets.collect_paths`）与导入改写
（:func:`src.core.chat_archive.remap_attachment_paths`）共用同一套判定，
不允许各自再写一份前缀比较。

刻意**不含** ``scholar_workspace/assets``：那里的副本本身就是对话资产的落点，
已被内容寻址资产管线管理；若再当作工作区产物收集，同一份文件会被两条链路各算一次。
"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from typing import List, Tuple

from src.core import BASE_DIR

logger = logging.getLogger("Core.WorkspaceRefs")

#: 工作区产物根目录（绝对路径）。写入方见各函数注释，未知时静默跳过该项。
_TOOLS_SUBDIR = "tools"

#: 候选路径的粗匹配：``file://`` URI、Windows 盘符路径、POSIX 绝对路径。
#:
#: POSIX 分支的负向后顾 ``(?<![\w:/.-])`` 用来排除 URL 内部的斜杠——
#: ``https://example.com/a`` 里 ``/a`` 前面是 ``m``，因此不会被当成文件路径。
_CANDIDATE_RE = re.compile(
    r"""(?P<quoted>"[^"\n]+"|'[^'\n]+'|`[^`\n]+`)"""
    r"""|(?P<file>file://[^\s"'<>`，。；）】]+)"""
    r"""|(?P<win>[A-Za-z]:[\\/][^\s"'<>|?*，。；）】]+)"""
    r"""|(?P<posix>(?<![\w:/.-])/[^\s"'<>`，。；）】]+)"""
)

#: 路径尾部常被正文标点粘连，匹配后需要剥掉。
_TRAILING = ".,;:!?)]}>\"'、，。；：！？）】》"


def artifact_roots() -> Tuple[str, ...]:
    """工作区产物根目录清单（绝对路径，去重保序）。

    * ``<BASE_DIR>/tools`` —— ``tools/mcp``（:data:`src.core.academic.base.WORKSPACE_DIR`，
      NCBI FASTA 等）与 ``tools/skill/<name>_workspace``（外部技能沙箱）都在其下；
    * ``bioenv.env_root()`` —— 生信工具每次运行的 ``runs/<时间戳>-<工具>``；
    * ``output/`` 顶层 —— R 图与 AI 生成图的固定落点（不可写时会回退到临时目录，
      因此这里取 ``plot_output_dir()`` 的父目录，而不是硬编码 ``output``）。
    """
    roots: List[str] = [os.path.join(BASE_DIR, _TOOLS_SUBDIR)]
    try:
        from src.core.bioenv.paths import env_root
        roots.append(env_root())
    except Exception as exc:  # pragma: no cover - 纯防御
        logger.debug("Bioenv root unavailable for artifact detection: %s", exc)
    try:
        from src.core import output_paths
        roots.append(os.path.dirname(output_paths.plot_output_dir()))
    except Exception as exc:  # pragma: no cover - 纯防御
        logger.debug("Output root unavailable for artifact detection: %s", exc)
    return tuple(dict.fromkeys(os.path.abspath(r) for r in roots if r))


def normalize(raw: object) -> str:
    """把路径或 ``file://`` URI 归一成本地路径字符串。

    非 URL 形式原样返回（去空白）；无法解析的 URI 也原样返回，让调用方按
    "远程资源"处理，而不是在这里猜。
    """
    text = str(raw or "").strip()
    if not text or not text.lower().startswith("file:"):
        return text
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return text
    path = urllib.parse.unquote(parsed.path)
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        path = f"//{parsed.netloc}{path}"
    if os.name == "nt" and re.match(r"^/[A-Za-z]:", path):
        # Windows 的 file:///C:/dir 解出来是 /C:/dir
        path = path[1:]
    return os.path.normpath(path)


def _under_roots(local: str, roots: Tuple[str, ...]) -> bool:
    """本地路径是否**真实存在**且落在给定根目录之下。"""
    if not local:
        return False
    try:
        if not os.path.isfile(local):
            return False
        target = os.path.normcase(os.path.abspath(local))
    except (OSError, ValueError):
        return False
    for root in roots:
        if target.startswith(os.path.normcase(root) + os.sep):
            return True
    return False


def is_artifact(path: object) -> bool:
    """路径是否是**本机上真实存在**的工作区产物。"""
    return _under_roots(normalize(path), artifact_roots())


def _trim(candidate: str) -> str:
    return candidate.rstrip(_TRAILING)


def artifacts_in_text(text: object) -> List[str]:
    """从一段正文里提取工作区产物路径（去重保序，返回归一化后的绝对路径）。

    只保留"真实存在且落在工作区"的候选：正文里出现的不存在路径、外部链接、
    示例路径都不会进入归档的资产清单。
    """
    if not isinstance(text, str) or not text:
        return []
    if "/" not in text and "\\" not in text:
        return []
    seen: List[str] = []
    seen_set = set()
    # 根目录只在本次提取里算一次：候选路径可能有几十个，逐个重算是无谓开销。
    roots = artifact_roots()
    for match in _CANDIDATE_RE.finditer(text):
        raw = match.group(0)
        if match.group("quoted") is not None:
            # 引号形态用来覆盖带空格的路径（Windows 用户目录很常见）；
            # 引号本身不是路径的一部分，也不做尾标点裁剪。
            raw = raw[1:-1].strip()
        else:
            raw = _trim(raw)
        if not raw:
            continue
        local = normalize(raw)
        if not local or local in seen_set:
            continue
        if not _under_roots(local, roots):
            continue
        seen_set.add(local)
        seen.append(local)
    return seen
