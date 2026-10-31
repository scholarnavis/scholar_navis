"""生信工具清单（唯一事实源）。

为什么用清单而不是代码
--------------------
"新增一个工具"应当只改数据，不改任何调用路径。清单里描述的是**能力**：这个工具
叫什么、属于哪个前缀、在哪些平台可用、许可是什么、版本怎么探测、大致跑多久。
封装函数只消费这些字段，不重复硬编码工具名。

平台可用性必须显式声明
--------------------
bioconda 只有 linux-64 与 osx-64/osx-arm64，win-64 几乎是空的。因此"某工具在
Windows 上能不能用"不能靠猜测，只能靠清单显式列出；缺失即视为不可用，调用方据此
给出替代路径而不是发一个必然失败的命令。两个字段分工明确，不能互相顶替：

* ``platforms`` —— **conda 通道**有该平台构建。只有列在这里的平台才可能装进私有
  前缀（否则求解必然失败）。
* ``native_platforms`` —— 上游项目自己发布该平台的预编译产物（seqkit 的
  ``seqkit_windows_amd64.exe`` 就是典型）。这类产物可以探测与运行，但**装不了**
  到私有前缀里，因此不能合并进 ``platforms``：那会让安装动作去请求一个不存在的
  包，把"用户装过"与"我们能装"两件事混成一件。

许可底线
--------
AGPL 与非商业条款的工具一律不进入清单：前者会让整个分发形态受牵连，后者根本
不允许随应用获取。拒绝发生在加载阶段并记入 problems，避免"清单里有一条"变成
"某天被自动装上"。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.core.bioenv import paths
from src.core.platform_env import app_resource_root

logger = logging.getLogger("BioEnv.Catalog")

#: 随包分发的内置清单（只读资源；冻结产物由 build_app.py 以 ``--add-data`` 带上）。
BUNDLED_CATALOG_FILE = os.path.join(
    app_resource_root(), "src", "core", "data", "bioenv_catalog.json")

#: 清单条目的必填字段。工具名不在此列：它取自 ``tools`` 映射的键，条目里不必重复
#: 写一遍（重复写会带来"键与字段不一致时以谁为准"的歧义）。
REQUIRED_FIELDS = ("prefix", "conda_name", "license", "summary", "platforms")

#: 生信工具在"工具过滤"菜单里的分类标签。必须与 schema 描述里的 ``[Tags: ...]``
#: 完全一致，否则用户勾掉了分类、工具却仍在清单里。
TOOL_FILTER_TAG = "Bioinformatics"

#: 该分类默认不勾选：每个工具的 schema 都占上下文，默认加载等于为大概率用不到的
#: 能力付费。用户需要时在过滤菜单里勾上即可，勾选状态会持久化。
TOOL_FILTER_DEFAULT_ON = False

#: 许可底线：命中任一标记的条目直接拒绝加载。
_BLOCKED_LICENSE_MARKERS = (
    "AGPL", "NONCOMMERCIAL", "NON-COMMERCIAL", "CC-BY-NC", "CC BY-NC",
    "BUSL", "SSPL", "RESEARCH-ONLY", "ACADEMIC-ONLY",
)


def _is_blocked_license(text: str) -> bool:
    """许可标识是否触碰底线（大小写与连字符写法都归一后比较）。"""
    upper = str(text).upper().replace("_", "-")
    return any(marker in upper for marker in _BLOCKED_LICENSE_MARKERS)


def _as_tuple(value: Any) -> Tuple[str, ...]:
    """把清单里的列表（或单个字符串）统一成字符串元组。"""
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if str(v).strip())
    return ()


@dataclass(frozen=True)
class ToolEntry:
    """一个工具条目的只读视图。"""

    name: str
    prefix: str
    conda_name: str
    license: str
    summary: str
    platforms: Tuple[str, ...]
    #: 上游自行分发预编译产物的平台（不走 conda）；见模块 docstring 的字段分工。
    native_platforms: Tuple[str, ...] = ()
    #: 该条目有原生构建、但 conda 通道没有时，给用户的获取指引（清单里的唯一文案）。
    native_hint: str = ""
    executables: Dict[str, str] = field(default_factory=dict)
    version_args: Tuple[str, ...] = ("--version",)
    #: 抓取帮助信息的参数。空元组表示该工具不提供（或不需要）帮助探测，此时不做
    #: 前置观察，也不阻断调用。
    help_args: Tuple[str, ...] = ("--help",)
    #: 执行形态：``sync`` 应用内直接跑、``job`` 提交后轮询、``external`` 只产出脚本。
    #: 路由判定读这个字段，不在代码里按工具名分支。
    run_mode: str = "sync"
    #: 系统安装版本低于此值时回退到私有前缀；留空表示不设下限。
    min_version: str = ""
    license_source: str = ""
    category: str = ""
    tags: Tuple[str, ...] = ()
    typical_seconds: int = 30
    docs: str = ""

    def available_on(self, platform: str) -> bool:
        """该工具在给定平台（micromamba 标识）上是否有 **conda 通道**构建。

        只有为真的平台才值得尝试装进私有前缀；判定原生可执行文件能否运行请用
        :meth:`host_runnable`。
        """
        return platform in self.platforms

    def native_on(self, platform: str) -> bool:
        """上游是否自行分发该平台的预编译产物（conda 通道不给这些平台）。"""
        return platform in self.native_platforms

    def host_runnable(self, platform: str) -> bool:
        """宿主能否直接运行：conda 构建与厂商预编译产物，任一条路都算。"""
        return self.available_on(platform) or self.native_on(platform)

    def reachable_on(self, platform: str) -> bool:
        """该工具在给定平台上是否可达。

        三种情形：conda 通道有该平台构建；上游有该平台的预编译产物；或者当前是
        Windows 而清单声明了 ``linux-64``——此时唯一能拿到权威工具的路子是 WSL，
        所以"没有 win-64 构建"不等于"用不了"。判定集中在这里，避免解析层与展示层
        各写一份。
        """
        if self.host_runnable(platform):
            return True
        return platform.startswith("win-") and self.available_on("linux-64")

    def executable_relpath(self, platform: str) -> str:
        """前缀内可执行文件的相对路径（按平台取，缺省回落到 ``default``）。"""
        return (self.executables.get(platform)
                or self.executables.get("default")
                or os.path.join("bin", self.name))


@dataclass
class Catalog:
    """全部工具条目与前缀分组的集合。"""

    tools: Dict[str, ToolEntry] = field(default_factory=dict)
    prefixes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    channels: Tuple[str, ...] = ("bioconda", "conda-forge")
    #: 系统已装工具的额外搜索目录，键为 ``posix`` / ``windows``。
    system_search: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)

    def tool(self, name: str) -> Optional[ToolEntry]:
        """按工具名取条目。"""
        return self.tools.get(name)

    def for_platform(self, platform: str) -> List[ToolEntry]:
        """在给定平台上有 conda 构建的全部工具（保持清单顺序）。"""
        return [entry for entry in self.tools.values() if entry.available_on(platform)]

    def for_reachable(self, platform: str) -> List[ToolEntry]:
        """在给定平台上可达的全部工具。

        包含三种来源：conda 构建、厂商预编译产物、以及 Windows 经 WSL 借用
        linux-64 的情形。
        """
        return [entry for entry in self.tools.values() if entry.reachable_on(platform)]

    def tools_of(self, prefix: str) -> List[ToolEntry]:
        """属于某个前缀的全部工具。"""
        return [entry for entry in self.tools.values() if entry.prefix == prefix]

    def prefix_packages(self, prefix: str) -> Tuple[str, ...]:
        """某个前缀需要安装的 conda 包名（清单声明优先，兜底为条目的 conda_name）。"""
        declared = self.prefixes.get(prefix, {}).get("packages")
        if declared:
            return _as_tuple(declared)
        return tuple(entry.conda_name for entry in self.tools_of(prefix))

    def installable_packages(self, prefix: str, platform: str) -> Tuple[str, ...]:
        """某个前缀在给定平台上**真能装**的包（conda 通道有该平台构建的那些）。

        与 :meth:`prefix_packages` 的分工：后者回答"这个前缀要什么"，这里回答"在
        这台机器上能装什么"。Windows 上 bioconda 几乎没有 win-64 产物，照搬整份
        清单只会让求解必然失败，因此安装动作必须先过这一关。

        无法与任何工具条目对应的包（用户清单里自己加的）原样保留：判断不了就不拦。
        """
        out: List[str] = []
        for package in self.prefix_packages(prefix):
            owners = [e for e in self.tools_of(prefix) if e.conda_name == package]
            if owners and not any(e.available_on(platform) for e in owners):
                continue
            out.append(package)
        return tuple(out)

    def prefix_names(self) -> List[str]:
        """清单中出现过的全部前缀名（含只在前缀表里声明的空前缀）。"""
        names = set(self.prefixes)
        names.update(entry.prefix for entry in self.tools.values())
        return sorted(names)

    def is_complete(self) -> bool:
        """是否加载成功且没有任何问题条目。"""
        return not self.problems


def _read_json(path: str) -> Optional[dict]:
    """读取清单文件；不存在或不可解析时返回 None 并记录原因。"""
    if not os.path.isfile(path):
        logger.debug(f"Catalog file not found: {path}")
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Cannot read catalog {path}: {e}")
        return None
    if not isinstance(data, dict):
        logger.warning(f"Catalog {path} is not a JSON object")
        return None
    return data


def _merge(target: Dict[str, Any], extra: Dict[str, Any]) -> None:
    """把用户清单合并进内置清单：工具按名字覆盖，前缀与通道按 key 覆盖。"""
    for name, entry in (extra.get("tools") or {}).items():
        if isinstance(entry, dict):
            target.setdefault("tools", {})[name] = entry
    for name, entry in (extra.get("prefixes") or {}).items():
        if isinstance(entry, dict):
            target.setdefault("prefixes", {})[name] = entry
    search = extra.get("system_search")
    if isinstance(search, dict):
        target.setdefault("system_search", {}).update(search)
    channels = _as_tuple(extra.get("channels"))
    if channels:
        target["channels"] = channels


def _parse_tool(name: str, raw: Dict[str, Any], problems: List[str]) -> Optional[ToolEntry]:
    """把一条原始记录解析成 :class:`ToolEntry`，缺字段或许可越界时返回 None。"""
    missing = [key for key in REQUIRED_FIELDS if not raw.get(key)]
    if missing:
        problems.append(f"tool '{name}' is missing field(s): {', '.join(missing)}")
        logger.warning(f"Catalog entry '{name}' skipped: missing {missing}")
        return None

    license_id = str(raw.get("license") or "").strip()
    if _is_blocked_license(license_id):
        problems.append(f"tool '{name}' rejected: license '{license_id}' is not distributable")
        logger.warning(f"Catalog entry '{name}' rejected by license policy: {license_id}")
        return None

    platforms = _as_tuple(raw.get("platforms"))
    if not platforms:
        problems.append(f"tool '{name}' declares no platforms")
        return None

    executables = raw.get("executables")
    return ToolEntry(
        name=str(name),
        prefix=str(raw.get("prefix") or "main"),
        conda_name=str(raw.get("conda_name")),
        license=license_id,
        summary=str(raw.get("summary") or ""),
        platforms=platforms,
        native_platforms=_as_tuple(raw.get("native_platforms")),
        native_hint=str(raw.get("native_hint") or ""),
        executables=dict(executables) if isinstance(executables, dict) else {},
        # 显式写空数组表示"该工具没有版本参数"（bwa / seqtk）。与 help_args 同理，
        # 不能用 ``or`` 兜底：那会把"声明为空"静默改回 --version，随后探测必然失败，
        # 已安装的工具被误判为不可用。
        version_args=(_as_tuple(raw.get("version_args"))
                      if "version_args" in raw else ("--version",)),
        # 显式写空数组表示"该工具不提供帮助探测"；未写则用默认参数。两者不能合并，
        # 否则"声明为不支持"会被静默改回默认值。
        help_args=(_as_tuple(raw.get("help_args"))
                   if "help_args" in raw else ("--help",)),
        run_mode=str(raw.get("run_mode") or "sync"),
        min_version=str(raw.get("min_version") or ""),
        license_source=str(raw.get("license_source") or ""),
        category=str(raw.get("category") or ""),
        tags=_as_tuple(raw.get("tags")),
        typical_seconds=int(raw.get("typical_seconds") or 30),
        docs=str(raw.get("docs") or ""),
    )


def load(override_path: str = "") -> Catalog:
    """加载内置清单，并叠加用户清单（若存在）。

    :param override_path: 自定义清单路径；省略时用 :func:`paths.catalog_override_path`。
    """
    catalog = Catalog()
    problems: List[str] = []

    bundled = _read_json(BUNDLED_CATALOG_FILE)
    if bundled is None:
        # 内置清单缺失是部署问题，必须显式记录：空清单会让所有工具都"不可用"，
        # 而用户看到的只是"没有工具"，很难定位到打包漏了文件。
        problems.append(f"bundled catalog is unavailable: {BUNDLED_CATALOG_FILE}")
        logger.error(f"Bundled bioenv catalog missing: {BUNDLED_CATALOG_FILE}")

    merged: Dict[str, Any] = {}
    if isinstance(bundled, dict):
        merged.update({
            "tools": dict(bundled.get("tools") or {}),
            "prefixes": dict(bundled.get("prefixes") or {}),
            "system_search": dict(bundled.get("system_search") or {}),
            "channels": _as_tuple(bundled.get("channels")),
        })

    override_file = override_path or paths.catalog_override_path()
    extra = _read_json(override_file)
    if isinstance(extra, dict):
        _merge(merged, extra)
        logger.info(f"User catalog override applied: {override_file}")

    for name, raw in (merged.get("tools") or {}).items():
        if not isinstance(raw, dict):
            problems.append(f"tool '{name}' is not an object")
            continue
        entry = _parse_tool(name, raw, problems)
        if entry is not None:
            catalog.tools[entry.name] = entry

    catalog.prefixes = {k: v for k, v in (merged.get("prefixes") or {}).items()
                        if isinstance(v, dict)}
    catalog.system_search = {str(k): _as_tuple(v)
                             for k, v in (merged.get("system_search") or {}).items()
                             if isinstance(v, (list, tuple, str))}
    channels = _as_tuple(merged.get("channels"))
    if channels:
        catalog.channels = channels
    catalog.problems = problems

    logger.info(f"bioenv catalog loaded: {len(catalog.tools)} tool(s), "
                f"{len(catalog.prefixes)} prefix(es), {len(problems)} problem(s)")
    return catalog


#: 进程内共享的清单实例；工具调用可能在同一轮里反复发生，不必每次重读 JSON。
_DEFAULT: Optional[Catalog] = None
_DEFAULT_LOCK = threading.Lock()


def default(refresh: bool = False) -> Catalog:
    """取进程共享的清单（``refresh`` 为真时重新加载）。"""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None or refresh:
            _DEFAULT = load()
        return _DEFAULT


def platform_tools(catalog: Catalog, platform: str) -> List[Dict[str, Any]]:
    """给 LLM 的工具说明用的精简描述（只含当前平台可用的条目）。"""
    return [
        {
            "name": entry.name,
            "category": entry.category,
            "summary": entry.summary,
            "run_mode": entry.run_mode,
            "typical_seconds": entry.typical_seconds,
            "docs": entry.docs,
        }
        for entry in catalog.for_reachable(platform)
    ]


def describe(catalog: Catalog, platform: str) -> str:
    """把清单渲染成一段纯文本，供系统提示或设置页展示。"""
    available = catalog.for_reachable(platform)
    if not available:
        return "No bioinformatics tool is available on this platform."
    lines = [f"Available bioinformatics tools ({len(available)}) on {platform}:"]
    for entry in available:
        ratio = f"~{entry.typical_seconds}s" if entry.typical_seconds else "unknown cost"
        lines.append(f"- {entry.name} [{entry.category or 'general'}, {ratio}] {entry.summary}")
    return "\n".join(lines)


def all_platforms(catalog: Catalog) -> Sequence[str]:
    """清单中出现过的全部平台标识（供设置页做能力矩阵展示）。"""
    seen = set()
    for entry in catalog.tools.values():
        seen.update(entry.platforms)
    return sorted(seen)
