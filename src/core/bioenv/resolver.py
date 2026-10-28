"""工具解析：优先使用用户机器上已经装好的那份。

解析顺序
--------
1. **用户显式指定** —— ``<BASE_DIR>/bioenv/tool_paths.json`` 里写死了路径；
2. **系统安装** —— 先 ``PATH``，再清单声明的常见安装目录（conda / homebrew /
   ``~/.local/bin`` 等）；版本低于条目的 ``min_version`` 时**不采用**；
3. **私有前缀** —— 只有前两步都拿不到可用版本时才用到它。

为什么不无条件用私有前缀
----------------------
用户已经在系统里装好的版本，往往是他自己核对过、论文里引用过的版本；应用再悄悄
换成自己下载的一份，等于把"结果可复现"这件事搞坏了。反过来，系统版本过旧时也不
能硬用——所以才有 ``min_version`` 这个回退判据，而不是"找到就用"。

缓存
----
版本探测要真的起一次子进程，几十毫秒的量级，但工具调用可能在同一轮里重复发生。
结果按 ``(工具, 平台)`` 缓存，并在命中前确认文件仍然存在；换机器、换环境或用户
在设置页点"重新检测"时用 :func:`clear_cache` 清掉。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from src.core.bioenv import catalog as catalog_mod
from src.core.bioenv import env as env_mod
from src.core.bioenv import paths
from src.core.bioenv import prefix
from src.core.bioenv import wsl
from src.core.platform_env import is_windows
from src.core.toolchain import micromamba as mamba
from src.core.toolchain import probe

logger = logging.getLogger("BioEnv.Resolver")

#: 用户显式指定可执行文件路径的配置文件（键为工具名，值为绝对路径）。
OVERRIDE_FILE_NAME = "tool_paths.json"

#: 解析结果缓存：键为 ``(工具名, 平台)``。
_CACHE: Dict[Tuple[str, str], "ToolLocation"] = {}
#: 最近一次解析失败的原因：``(分类, 说明)``，键同上。
#:
#: 分类用来把"没装"与"装了但跑不起来"分开——后者在 NixOS 等系统上很常见（包在
#: PATH 里，但缺动态库），笼统报"未安装"会让用户去装一个他明明已经装过的东西。
#: 对模型也一样：前者是安装动作，后者是修复动作，给出的指令必须不同。
_FAILURES: Dict[Tuple[str, str], Tuple[str, str]] = {}
_LOCK = threading.Lock()

_RE_ENV_VAR = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%|\$([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class ToolLocation:
    """一个可用工具的实际落点。"""

    name: str
    path: str
    #: ``custom`` 用户指定 / ``system`` 系统安装 / ``private`` 私有前缀 / ``wsl`` 发行版内。
    source: str
    version: str = ""
    prefix_name: str = ""
    #: ``source == "wsl"`` 时使用的发行版名。
    distro: str = ""
    detail: str = ""

    @property
    def semantic_version(self) -> str:
        """从原始版本行里提取的语义版本号（可能为空）。"""
        return probe.parse_version(self.version)

    @property
    def is_system(self) -> bool:
        """是否来自系统安装（含用户指定）。"""
        return self.source in ("system", "custom")


# --------------------------------------------------------------------------- #
#  用户指定与搜索目录
# --------------------------------------------------------------------------- #
def overrides_path() -> str:
    """用户指定路径的配置文件位置。"""
    return os.path.join(paths.env_root(), OVERRIDE_FILE_NAME)


def load_overrides() -> Dict[str, str]:
    """读取用户指定的可执行文件路径（不存在或不可解析时返回空映射）。"""
    path = overrides_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Cannot read tool path overrides {path}: {e}")
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v.strip()}


def _expand(value: str) -> str:
    """展开 ``~`` 与 ``%VAR%`` / ``$VAR``（未定义的变量原样保留）。"""
    text = os.path.expanduser(str(value).strip())

    def _replace(match: "re.Match[str]") -> str:
        name = match.group(1) or match.group(2)
        return os.environ.get(name, match.group(0))

    return _RE_ENV_VAR.sub(_replace, text)


def search_paths(catalog: catalog_mod.Catalog, platform: str = "") -> List[str]:
    """清单声明的系统搜索目录（按当前平台取 key，已做变量展开）。"""
    key = "windows" if is_windows() else "posix"
    declared = catalog.system_search.get(key, ())
    out: List[str] = []
    for item in declared:
        expanded = _expand(item)
        if expanded and expanded not in out:
            out.append(expanded)
    logger.debug(f"System search paths ({key}): {len(out)} entr(ies)")
    return out


def which_tool(name: str, extra_dirs: List[str]) -> str:
    """先在 ``PATH`` 里找，再在额外目录里找（Windows 顺带补 ``.exe``）。"""
    found = shutil.which(name)
    if found:
        return found
    candidates = [name]
    if is_windows():
        candidates.append(name + ".exe")
    for directory in extra_dirs:
        for candidate in candidates:
            path = os.path.join(directory, candidate)
            if os.path.isfile(path):
                return path
    return ""


# --------------------------------------------------------------------------- #
#  解析
# --------------------------------------------------------------------------- #
def _probe_location(entry: catalog_mod.ToolEntry, path: str, source: str,
                    prefix_name: str, env: Optional[Dict[str, str]] = None,
                    detail: str = "") -> Optional[ToolLocation]:
    """跑一次版本探测，可用则返回落点。"""
    info = probe.probe(path, entry.version_args, env=env)
    if not info.get("available"):
        logger.warning(f"Resolved candidate {path} for '{entry.name}' is unusable: "
                       f"{info.get('detail')}")
        return None
    return ToolLocation(name=entry.name, path=path, source=source,
                        version=str(info.get("version") or ""),
                        prefix_name=prefix_name, detail=detail)


def _resolve_wsl(entry: catalog_mod.ToolEntry) -> Tuple[Optional[ToolLocation], str]:
    """在 WSL 发行版内解析工具，返回 ``(落点, 失败原因)``。"""
    status = wsl.detect()
    if not status.usable:
        return None, wsl.describe(status)
    path = wsl.which_in_distro(entry.name, status.distro)
    if not path:
        return None, f"'{entry.name}' is not installed inside WSL distribution '{status.distro}'"
    info = wsl.probe_tool(path, entry.version_args, status.distro)
    if not info.get("available"):
        return None, f"the WSL build at {path} cannot run: {info.get('detail')}"
    version_text = str(info.get("version") or "")
    if not probe.is_at_least(probe.parse_version(version_text), entry.min_version):
        return None, (f"the WSL build reports '{version_text}', "
                      f"below the required {entry.min_version}")
    return ToolLocation(name=entry.name, path=path, source="wsl",
                        version=version_text, distro=status.distro,
                        detail=f"WSL distribution '{status.distro}'"), ""


def resolve(entry: catalog_mod.ToolEntry, catalog: catalog_mod.Catalog,
            platform: str = "", refresh: bool = False) -> Optional[ToolLocation]:
    """解析一个工具的实际落点；不可用时返回 ``None``。

    失败原因连同分类一起记下（见 :func:`failure_reason`）：调用方需要区分"没装"
    与"装了但坏了"，因为前者对应安装动作、后者对应修复动作，给模型的指令不同。
    """
    plat = platform or mamba.runtime_platform()
    key = (entry.name, plat)

    native = entry.available_on(plat)
    # Windows 上绝大多数生信工具没有 win-64 构建。只要清单声明了 linux-64 且 WSL
    # 可用，就把它当成一条可用路径，而不是直接判为"此平台不支持"。
    wsl_capable = (not native) and entry.reachable_on(plat)
    if not native and not wsl_capable:
        logger.info(f"Tool '{entry.name}' is not reachable on platform {plat}")
        with _LOCK:
            _FAILURES[key] = ("unsupported_platform",
                              f"no build for {plat} and no WSL host available")
        return None

    if not refresh:
        with _LOCK:
            cached = _CACHE.get(key)
        if cached and os.path.isfile(cached.path):
            return cached

    location: Optional[ToolLocation] = None
    failure: Tuple[str, str] = ("unknown", "")

    override = load_overrides().get(entry.name, "")
    if override:
        candidate = _expand(override)
        if os.path.isfile(candidate):
            location = _probe_location(entry, candidate, "custom", "",
                                       env=env_mod.for_system(),
                                       detail="user-specified path")
            if location is None:
                failure = ("broken_install",
                           f"the configured path {candidate} cannot be executed")
        else:
            failure = ("configured_path_missing",
                       f"the configured path {candidate} does not exist")
            logger.warning(failure[1])

    if location is None:
        system_path = which_tool(entry.name, search_paths(catalog, plat))
        if system_path:
            info = probe.probe(system_path, entry.version_args, env=env_mod.for_system())
            if info.get("available"):
                version_text = str(info.get("version") or "")
                if probe.is_at_least(probe.parse_version(version_text), entry.min_version):
                    location = ToolLocation(
                        name=entry.name, path=system_path, source="system",
                        version=version_text,
                        detail=f"system install ({entry.min_version or 'no minimum'})")
                else:
                    failure = ("version_too_old",
                               f"the system build at {system_path} reports "
                               f"'{version_text}', below the required {entry.min_version}")
                    logger.warning(f"{failure[1]}; trying the private prefix")
            else:
                failure = ("broken_install",
                           f"the system build at {system_path} cannot run: "
                           f"{info.get('detail')}")
                logger.warning(failure[1])
        elif not failure[1]:
            failure = ("not_installed",
                       "not found on PATH or in the known install directories")

    if location is None and prefix.is_ready(entry.prefix):
        executable = prefix.tool_executable(entry.prefix, entry, plat)
        if executable:
            location = _probe_location(entry, executable, "private", entry.prefix,
                                       env=prefix.child_env(entry.prefix),
                                       detail=f"private prefix '{entry.prefix}'")
            if location is None:
                failure = ("broken_install",
                           f"the private prefix '{entry.prefix}' contains an unusable build")
        else:
            failure = ("prefix_incomplete",
                       f"prefix '{entry.prefix}' exists but has no executable for "
                       f"'{entry.name}'")

    if location is None and wsl_capable:
        location, wsl_note = _resolve_wsl(entry)
        if location is None and wsl_note:
            # WSL 本身不可用与"WSL 里没装这个工具"是两种不同的处置：前者要引导用户
            # 启用 WSL，后者要在他已有的发行版里装工具。
            kind = "not_installed" if wsl.detect().usable else "wsl_unavailable"
            failure = (kind, wsl_note)

    with _LOCK:
        if location is not None:
            _CACHE[key] = location
            _FAILURES.pop(key, None)
        else:
            _CACHE.pop(key, None)
            _FAILURES[key] = failure if failure[1] else ("unknown", "unknown reason")
    if location is not None:
        logger.info(f"Tool '{entry.name}' resolved from {location.source}: {location.path}")
    else:
        logger.info(f"Tool '{entry.name}' is not available on {plat}: {failure[1]}")
    return location


def failure_reason(entry: catalog_mod.ToolEntry,
                   platform: str = "") -> Tuple[str, str]:
    """返回 ``(分类, 说明)``，供调用方决定"安装"还是"修复"。

    分类取值：``not_installed`` / ``broken_install`` / ``version_too_old`` /
    ``unsupported_platform`` / ``wsl_unavailable`` / ``prefix_incomplete`` /
    ``configured_path_missing`` / ``unknown``。
    """
    plat = platform or mamba.runtime_platform()
    if not entry.available_on(plat) and not entry.reachable_on(plat):
        return ("unsupported_platform", f"no build for {plat}")
    with _LOCK:
        record = _FAILURES.get((entry.name, plat))
    if record is not None and record[1]:
        return record
    return ("unknown", "the tool has not been resolved yet")


def resolve_all(catalog: catalog_mod.Catalog, platform: str = "",
                refresh: bool = False) -> Dict[str, ToolLocation]:
    """解析清单里在当前平台可用的全部工具。"""
    plat = platform or mamba.runtime_platform()
    result: Dict[str, ToolLocation] = {}
    for entry in catalog.for_platform(plat):
        location = resolve(entry, catalog, plat, refresh=refresh)
        if location is not None:
            result[entry.name] = location
    logger.info(f"Resolved {len(result)}/{len(catalog.for_platform(plat))} tool(s) on {plat}")
    return result


def clear_cache() -> None:
    """清空解析缓存（设置页"重新检测"与安装动作之后调用）。"""
    with _LOCK:
        count = len(_CACHE)
        _CACHE.clear()
        _FAILURES.clear()
    logger.info(f"Tool resolution cache cleared ({count} entr(ies))")


# --------------------------------------------------------------------------- #
#  环境与说明
# --------------------------------------------------------------------------- #
def env_for(location: ToolLocation) -> Dict[str, str]:
    """按落点来源给出子进程环境。

    系统安装与 WSL 都不注入前缀路径：前者的库由系统自己解决，后者的环境在发行版
    内部，宿主只负责启动 ``wsl.exe``。
    """
    if location.source == "private":
        return prefix.child_env(location.prefix_name)
    return env_mod.for_system()


def unavailable_reason(entry: catalog_mod.ToolEntry, platform: str = "") -> str:
    """工具不可用时的可读原因（供设置页与模型提示使用）。"""
    plat = platform or mamba.runtime_platform()
    kind, text = failure_reason(entry, plat)

    if kind == "unsupported_platform":
        if is_windows() and entry.available_on("linux-64"):
            return (f"'{entry.name}' has no native Windows build. {wsl.describe()} "
                    f"It can also be installed into the private prefix "
                    f"'{entry.prefix}'.")
        return (f"'{entry.name}' has no build for {plat}; "
                f"use WSL or a remote host to run it")

    if kind == "not_installed":
        return (f"'{entry.name}' is not installed ({text}). Installing it into the "
                f"private prefix '{entry.prefix}' would fix this.")

    if kind == "version_too_old":
        return (f"'{entry.name}' is installed but too old: {text}. Upgrade it, or "
                f"install a current build into the private prefix '{entry.prefix}'.")

    return (f"'{entry.name}' is unavailable: {text}. Installing it into the private "
            f"prefix '{entry.prefix}' would fix this.")


def summary(locations: Dict[str, ToolLocation]) -> str:
    """把解析结果渲染成一行一条的清单，供设置页与日志展示。"""
    if not locations:
        return "No bioinformatics tool is available."
    lines = []
    for name in sorted(locations):
        loc = locations[name]
        version = loc.version or "unknown version"
        lines.append(f"- {name}: {loc.source} | {version} | {loc.path}")
    return "\n".join(lines)
