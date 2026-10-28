"""生信工具的领域封装：参数语义化 + 输出结构化。

为什么封装层不暴露命令行标志
----------------------------
把 ``argv`` 交给模型等于把门槛原样保留：参数错一个字母就白跑一次，而且无法复现。
这里每个函数的参数是领域语义（一个输入文件、一个上限行数），标志由本层决定。

为什么返回值必须是"摘要 + 表格 + 落盘路径"
----------------------------------------
工具调用结果最终会被运行时按字符预算截断，原始输出几十万行必然被切坏。因此这里
只回传结构化后的前若干行与运行目录，完整输出留在 :mod:`src.core.bioenv.runner` 的
现场目录里，随时可核查。

链方向
------
涉及序列与区间的封装一律保留工具给出的 strand 字段，不在这里做任何"默认正链"的
补全。原文件是反链就读出反链，缺信息就如实缺着。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from src.core.bioenv import catalog as catalog_mod
from src.core.bioenv import help as help_mod
from src.core.bioenv import resolver
from src.core.bioenv import runner
from src.core.toolchain import micromamba as mamba

logger = logging.getLogger("BioEnv.Tools")

#: 回传给模型的表格行数上限（再多也会被上下文预算截断，不如主动给摘要）。
_MAX_ROWS = 200

#: 单个单元格的最大字符数。
_MAX_CELL = 200

#: 判定"可替代"所需的最小共享标签数（见 :func:`_alternatives`）。
_MIN_SHARED_TAGS = 2

#: flagstat 的一行：``1000 + 0 primary``，括号里是可选的明细说明。
_RE_FLAGSTAT = re.compile(r"^(\d+)\s*\+\s*(\d+)\s+(.+)$")

#: 本轮进程内已经读过帮助的工具名。每个新进程都要重新读一次：帮助内容与版本绑定，
#: 跨进程复用会让"这次用的到底是哪一版的接口"变得不可追溯。
_SEEN_HELP: set = set()
_SEEN_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
#  公共小工具
# --------------------------------------------------------------------------- #
def _ok(payload: Dict[str, Any]) -> str:
    """成功返回：统一 ``status`` 字段，其余原样序列化。"""
    payload.setdefault("status", "ok")
    return json.dumps(payload, ensure_ascii=False)


def _error(message: str, **extra: Any) -> str:
    """失败返回：结构化错误，调用方不需要解析自然语言。"""
    payload: Dict[str, Any] = {"status": "error", "message": message}
    payload.update(extra)
    logger.warning(f"bioenv tool error: {message}")
    return json.dumps(payload, ensure_ascii=False)


def _clip(value: Any, limit: int = _MAX_CELL) -> str:
    """把单元格压到上限长度，超长保留头部。"""
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _safe_input(path: str) -> Tuple[str, str]:
    """校验输入文件路径，返回 ``(绝对路径, 错误)``。

    以 ``-`` 开头的路径会被工具当成选项，必须挡在调用之前；这不是注入防护的替代
    （这里本来就不拼 shell），而是避免一次必然失败或行为异常的调用。
    """
    text = str(path or "").strip()
    if not text:
        return "", "an input file path is required"
    if text.startswith("-"):
        return "", "the input path must not start with '-'"
    full = os.path.abspath(os.path.expanduser(text))
    if not os.path.isfile(full):
        return "", f"input file not found: {full}"
    return full, ""


def _mark_help_seen(name: str) -> None:
    """标记本轮进程内已读过该工具的帮助。"""
    with _SEEN_LOCK:
        _SEEN_HELP.add(name)


def _help_seen(name: str) -> bool:
    """本轮进程内是否已读过该工具的帮助。"""
    with _SEEN_LOCK:
        return name in _SEEN_HELP


#: 每种失败分类对应的下一步动作。写进返回值而不是只写进系统提示：模型对工具结果
#: 里的字段最敏感，而系统提示里的通用规则容易被长上下文稀释。
_NEXT_STEP = {
    "not_installed": ("Tell the user this tool is not installed and how to install it. "
                      "Do NOT describe any analysis result."),
    "broken_install": ("Tell the user the installed build cannot run, quoting why. "
                       "Do NOT describe any analysis result."),
    "version_too_old": ("Ask the user to upgrade the tool. "
                        "Do NOT describe any analysis result."),
    "unsupported_platform": ("Tell the user this tool needs WSL or a remote host. "
                             "Do NOT describe any analysis result."),
    "wsl_unavailable": ("Tell the user WSL is not usable on this machine yet. "
                        "Do NOT describe any analysis result."),
    "prefix_incomplete": ("Tell the user the private prefix needs to be reinstalled. "
                          "Do NOT describe any analysis result."),
    "configured_path_missing": ("Tell the user the configured path no longer exists. "
                                "Do NOT describe any analysis result."),
    "unknown": "Report the failure. Do NOT describe any analysis result.",
}


def _alternatives(entry: catalog_mod.ToolEntry, cat: catalog_mod.Catalog,
                  platform: str) -> List[str]:
    """标签有交集、且当前确实可用的替代工具。

    只按 ``category`` 匹配会推荐"同类但干不了这件事"的工具：samtools 与 bwa 同属
    alignment，可 bwa 做不了 flagstat。按 ``tags`` 交集判断才能落到具体能力上；
    没有交集时宁可不给，也不给一条错的建议。
    """
    wanted = set(entry.tags)
    if not wanted:
        return []
    names: List[str] = []
    for other in cat.for_reachable(platform):
        if other.name == entry.name:
            continue
        # 至少共享两个标签：单个泛化标签（fasta / fastq）几乎人人都有，交集为 1
        # 时推荐出来的只是"碰巧都处理 FASTA"的工具，而不是"能做同一件事"的工具。
        # 例如 seqtk 与 fastp 只共享 fastq，但前者做格式转换、后者做质控。
        if len(wanted & set(other.tags)) < _MIN_SHARED_TAGS:
            continue
        if resolver.resolve(other, cat, platform) is not None:
            names.append(other.name)
    return names


def _unavailable_payload(entry: catalog_mod.ToolEntry, cat: catalog_mod.Catalog,
                         platform: str) -> str:
    """工具不可用时的返回体。

    ``status`` 用 ``unavailable`` 而不是 ``error``：两者对模型的含义完全不同——
    ``error`` 是一次失败的执行，``unavailable`` 是**根本没有执行**。混用会让模型
    把"没跑"当成"跑失败了"，进而凭印象补一个结果。
    """
    kind, _text = resolver.failure_reason(entry, platform)
    payload: Dict[str, Any] = {
        "status": "unavailable",
        "tool": entry.name,
        "reason_kind": kind,
        "message": resolver.unavailable_reason(entry, platform),
        "next_step": _NEXT_STEP.get(kind, _NEXT_STEP["unknown"]),
        "install_hint": (f"External Tools page -> Copy install command "
                         f"(prefix '{entry.prefix}')"),
    }
    alternatives = _alternatives(entry, cat, platform)
    if alternatives:
        payload["alternatives"] = alternatives
    logger.info(f"Tool '{entry.name}' refused as unavailable ({kind})")
    return _ok(payload)


def _help_required(entry: catalog_mod.ToolEntry, record: Dict[str, Any]) -> str:
    """未读过帮助时的阻断信息（已序列化，可直接作为工具返回值）。"""
    preview = "\n".join(_clip(line, 160) for line in help_mod.read_lines(entry.name, 5))
    return _ok({
        "status": "help_required",
        "tool": entry.name,
        "message": (f"Read the help of '{entry.name}' before using it: call "
                    f"bio_tool_help(tool='{entry.name}'), then retry this call."),
        "next_step": (f"Call bio_tool_help(tool='{entry.name}'), then retry this "
                      f"call once. Nothing has been executed yet."),
        "help_path": record.get("path", ""),
        "help_preview": preview,
    })


def _require_ready(name: str) -> Tuple[Optional[resolver.ToolLocation], str]:
    """解析工具并确保帮助已被读过，返回 ``(落点, 阻断信息)``。

    两道关卡：先把帮助抓到缓存（保证数据在场且与当前版本对应），再要求**本轮进程内
    已经读过它**。第二道是硬要求——模型没看帮助就开始传参数，失败往往表现为"输出少
    了一列"或"选项被忽略"，很难当场归因。阻断信息里带前几行帮助，模型一次就能通过。
    """
    cat = catalog_mod.default()
    platform = mamba.runtime_platform()
    entry = cat.tool(name)
    if entry is None:
        return None, _error(f"'{name}' is not described in the bioinformatics catalog")
    location = resolver.resolve(entry, cat, platform)
    if location is None:
        return None, _unavailable_payload(entry, cat, platform)

    record = help_mod.ensure_help(location, entry)
    if not record.get("observed"):
        # 工具不提供帮助时不阻断：把"抓不到帮助"当成"工具不可用"会误杀本来能跑的工具。
        logger.warning(f"Help observation unavailable for '{name}': "
                       f"{record.get('reason')}")
        return location, ""
    if entry.help_args and not _help_seen(entry.name):
        logger.info(f"Blocked '{name}' until its help has been read in this session")
        return None, _help_required(entry, record)
    return location, ""


def _base_payload(location: resolver.ToolLocation,
                  result: runner.RunResult) -> Dict[str, Any]:
    """每次执行都要带上的溯源信息：命令、版本、来源、现场目录。"""
    return {
        "tool": location.name,
        "tool_version": location.version,
        "tool_source": location.source,
        "command": result.command,
        "elapsed_seconds": round(result.elapsed, 2),
        "run_dir": result.run_dir,
    }


def _read_tsv(path: str, limit: int) -> Tuple[List[List[str]], bool]:
    """读取制表符分隔文件的前 ``limit`` 行，返回 ``(行, 是否被截断)``。"""
    rows: List[List[str]] = []
    truncated = False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for index, line in enumerate(fh):
                if index >= limit:
                    truncated = True
                    break
                rows.append(line.rstrip("\r\n").split("\t"))
    except OSError as e:
        logger.warning(f"Cannot read tabular output {path}: {e}")
    return rows, truncated


# --------------------------------------------------------------------------- #
#  工具封装
# --------------------------------------------------------------------------- #
def bio_tool_status(tool: str = "") -> str:
    """Report which bioinformatics tools are usable on this machine, where each one comes from (system install or private prefix), and its version. Call this before asking the user to install anything.

    :param tool: Optional tool name. Omit to list every tool available on this platform.
    """
    cat = catalog_mod.default()
    platform = mamba.runtime_platform()

    name = str(tool or "").strip()
    if name:
        entry = cat.tool(name)
        if entry is None:
            return _error(f"'{name}' is not described in the bioinformatics catalog",
                          known=sorted(cat.tools))
        if not entry.reachable_on(platform):
            return _ok({"tool": entry.name, "available": False,
                        "reason": resolver.unavailable_reason(entry, platform),
                        "declared_platforms": list(entry.platforms)})
        location = resolver.resolve(entry, cat, platform)
        if location is None:
            kind, _text = resolver.failure_reason(entry, platform)
            return _ok({"tool": entry.name, "available": False,
                        "reason_kind": kind,
                        "reason": resolver.unavailable_reason(entry, platform),
                        "next_step": _NEXT_STEP.get(kind, _NEXT_STEP["unknown"]),
                        "alternatives": _alternatives(entry, cat, platform)})
        return _ok({"tool": entry.name, "available": True, "path": location.path,
                    "source": location.source, "version": location.version,
                    "run_mode": entry.run_mode,
                    "typical_seconds": entry.typical_seconds,
                    "license": entry.license})

    cached_help = set(help_mod.cached_tools())
    rows: List[Dict[str, Any]] = []
    for entry in cat.for_reachable(platform):
        location = resolver.resolve(entry, cat, platform)
        row: Dict[str, Any] = {
            "tool": entry.name,
            "available": location is not None,
            "path": location.path if location else "",
            "source": location.source if location else "",
            "version": location.version if location else "",
            "run_mode": entry.run_mode,
            "typical_seconds": entry.typical_seconds,
            # 帮助是否已落过缓存：模型据此判断要不要先调 bio_tool_help。
            "help_cached": entry.name in cached_help,
        }
        if location is None:
            # 不给原因的话，整张表对模型就只是十行 false：既不知道哪个能修，
            # 也不知道该建议用户做什么。
            kind, _text = resolver.failure_reason(entry, platform)
            row["reason_kind"] = kind
            row["reason"] = resolver.unavailable_reason(entry, platform)
        rows.append(row)
    return _ok({"platform": platform, "tools": rows,
                "catalog_problems": cat.problems[:10]})


def bio_tool_help(tool: str, max_lines: int = 60) -> str:
    """Show the captured help text of a bioinformatics tool.

    A tool's help output is captured the first time that tool is used, because option
    names and defaults drift between versions. Call this before asking the user about
    command-line flags, or when a call failed with an unexpected option. The full text
    is also stored on disk and its path is returned.

    :param tool: Tool name as reported by bio_tool_status.
    :param max_lines: Maximum help lines to return.
    """
    cat = catalog_mod.default()
    entry = cat.tool(str(tool or "").strip())
    if entry is None:
        return _error(f"'{tool}' is not described in the bioinformatics catalog",
                      known=sorted(cat.tools))
    platform = mamba.runtime_platform()
    location = resolver.resolve(entry, cat, platform)
    if location is None:
        return _unavailable_payload(entry, cat, platform)

    record = help_mod.ensure_help(location, entry)
    # 无论帮助是否抓到都算"已经尝试读过"：否则不支持 --help 的工具会永远卡在阻断态。
    _mark_help_seen(entry.name)
    limit = max(1, min(int(max_lines or 60), 400))
    lines = help_mod.read_lines(entry.name, limit)
    if not lines:
        return _ok({"tool": entry.name, "observed": False,
                    "reason": record.get("reason") or "help output is empty",
                    "path": record.get("path", "")})

    total = help_mod.total_lines(entry.name)
    return _ok({"tool": entry.name, "observed": True,
                "tool_version": location.version,
                "tool_source": location.source,
                "path": record.get("path", ""),
                "cached": bool(record.get("cached")),
                "total_lines": total,
                "truncated": total > len(lines),
                "text": "\n".join(_clip(line, 200) for line in lines)})


def bio_seqkit_stats(path: str, max_rows: int = 50) -> str:
    """Summarise a FASTA/FASTQ file with seqkit: format, sequence count, total length, GC content, min/max/mean length. Returns one row per input file plus the full output directory.

    :param path: Path to a FASTA or FASTQ file (.fa, .fna, .faa, .fq, .fastq, optionally gzipped).
    :param max_rows: Maximum rows to return.
    """
    location, blocked = _require_ready("seqkit")
    if location is None:
        return blocked
    target, problem = _safe_input(path)
    if target == "":
        return _error(problem)

    limit = max(1, min(int(max_rows or 50), _MAX_ROWS))
    result = runner.run(location, [location.path, "stats", "-a", "-T", target])
    base = _base_payload(location, result)
    if not result.ok:
        return _error(f"seqkit stats failed: {result.stderr_hint()}", **base,
                      stderr_tail=result.stderr_tail[-10:])

    table, truncated = _read_tsv(result.stdout_path, limit + 1)
    if not table:
        return _error("seqkit produced no tabular output", **base,
                      stdout_tail=result.stdout_tail[-10:])
    header = [_clip(cell) for cell in table[0]]
    rows = [dict(zip(header, (_clip(cell) for cell in row))) for row in table[1:limit + 1]]
    return _ok({**base, "columns": header, "rows": rows,
                "truncated": truncated or len(table) > limit + 1})


def bio_bam_flagstat(bam: str, max_rows: int = 50) -> str:
    """Count reads in a SAM/BAM/CRAM file with samtools flagstat: total, mapped, properly paired, duplicates, secondary, supplementary, and QC pass/fail split. Requires only the alignment file, no index.

    :param bam: Path to a SAM, BAM, or CRAM file.
    :param max_rows: Maximum metric rows to return.
    """
    location, blocked = _require_ready("samtools")
    if location is None:
        return blocked
    target, problem = _safe_input(bam)
    if target == "":
        return _error(problem)

    limit = max(1, min(int(max_rows or 50), _MAX_ROWS))
    result = runner.run(location, [location.path, "flagstat", target])
    base = _base_payload(location, result)
    if not result.ok:
        return _error(f"samtools flagstat failed: {result.stderr_hint()}", **base,
                      stderr_tail=result.stderr_tail[-10:])

    lines, truncated = _read_tsv(result.stdout_path, limit + 1)
    rows: List[Dict[str, Any]] = []
    for cells in lines[:limit]:
        text = cells[0] if cells else ""
        match = _RE_FLAGSTAT.match(text.strip())
        if not match:
            continue
        metric = match.group(3).strip()
        note = ""
        if metric.endswith(")"):
            split = metric.rfind(" (")
            if split > 0:
                note = metric[split + 2:-1]
                metric = metric[:split].strip()
        rows.append({"metric": _clip(metric),
                     "passed": int(match.group(1)),
                     "failed": int(match.group(2)),
                     "note": _clip(note)})
    return _ok({**base, "rows": rows,
                "truncated": truncated or len(lines) > limit})


#: 注册进 SkillManager 的封装函数与它们的说明。说明同时用于生成 schema 与展示。
REGISTERED_TOOLS = (
    bio_tool_status,
    bio_tool_help,
    bio_seqkit_stats,
    bio_bam_flagstat,
)


def describe_availability() -> str:
    """一段纯文本的能力摘要，供系统提示或设置页使用。"""
    cat = catalog_mod.default()
    platform = mamba.runtime_platform()
    locations = resolver.resolve_all(cat, platform)
    if not locations:
        return "No bioinformatics tool is available on this machine."
    return resolver.summary(locations)
