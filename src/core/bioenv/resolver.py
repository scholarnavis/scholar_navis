"""工具解析：优先使用用户机器上已经装好的那份。

解析顺序
--------
1. **用户显式指定** —— ``<BASE_DIR>/bioenv/tool_paths.json`` 里写死了路径；
2. **系统安装** —— 先 ``PATH``，再清单声明的常见安装目录（conda / homebrew /
   ``~/.local/bin`` 等）；版本低于条目的 ``min_version`` 时**不采用**。清单里声明了
   ``native_platforms`` 的条目（Windows 上的 seqkit）也走这条路，只是它装不进私有
   前缀；
3. **私有前缀** —— 只在该平台 conda 通道真有构建时才查（Windows 上没有 win-64
   生信包，去前缀里找只会浪费时间）；
4. **WSL 发行版** —— 仅 Windows：发行版内的 ``PATH`` 与清单声明的目录，工具装在
   用户的 conda 环境里也能找到（见 :func:`src.core.bioenv.wsl.which_in_distro`）。
   WSL 不可用时如实回报状态与下一步，而不是让用户去装一个此平台不存在的包。

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


def wsl_search_paths(catalog: catalog_mod.Catalog) -> List[str]:
    """发行版内的工具搜索目录。

    与宿主侧的那份必须分开：``%USERPROFILE%`` 在发行版里没有意义，而 ``$HOME`` 在
    Windows 上要被展开成宿主路径。因此清单里单独一个 ``wsl`` 键，变量留给发行版的
    shell 展开（这里原样传递，不做 ``os.path.expanduser``）。
    """
    declared = catalog.system_search.get("wsl", ())
    out: List[str] = []
    for item in declared:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    logger.debug(f"WSL search paths: {len(out)} entr(ies)")
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


def _resolve_wsl(entry: catalog_mod.ToolEntry,
                 search_dirs: Tuple[str, ...] = ()) -> Tuple[Optional[ToolLocation], str]:
    """在 WSL 发行版内解析工具，返回 ``(落点, 失败原因)``。"""
    status = wsl.detect()
    if not status.usable:
        return None, wsl.describe(status)
    path = wsl.which_in_distro(entry.name, status.distro, search_dirs)
    if not path:
        # 不重复工具名：调用方会把它拼进 "'x' is not installed (...)" 的句式里。
        return None, f"not installed inside the WSL distribution '{status.distro}'"
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

    # 宿主能否直接跑：conda 通道有该平台构建，或上游发布了该平台的预编译产物
    # （Windows 上 seqkit 这类静态 .exe 就是后者）。
    host_capable = entry.host_runnable(plat)
    # Windows 上绝大多数生信工具没有 win-64 构建。只要清单声明了 linux-64，就把
    # WSL 当成一条可用的兜底路径 而不是直接判为"此平台不支持"；即使该工具在
    # Windows 上有厂商产物（host_capable 为真），用户没装时也一样要靠 WSL。
    wsl_capable = plat.startswith("win-") and entry.available_on("linux-64")
    if not host_capable and not wsl_capable:
        logger.info(f"Tool '{entry.name}' is not reachable on platform {plat}")
        with _LOCK:
            _FAILURES[key] = ("unsupported_platform",
                              f"no build for {plat} and no WSL host available")
        return None

    if not refresh:
        with _LOCK:
            cached = _CACHE.get(key)
        # WSL 落点的路径是**发行版内**的路径，宿主上的 os.path.isfile 永远为假，
        # 拿它做缓存校验等于每次调用都重新探测一遍（wsl.exe 起一次就要几百毫秒到
        # 几秒）。这类落点交给 wsl.detect() 的有效期与 clear_cache 兜底。
        if cached and (cached.source == "wsl" or os.path.isfile(cached.path)):
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

    if location is None and host_capable:
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

    # 私有前缀只在 conda 通道对该平台有构建时才有意义：Windows 上 bioconda 没有
    # win-64 产物，去前缀里找一个永远装不出来的可执行文件只会浪费时间与解释成本。
    if location is None and host_capable and entry.available_on(plat) \
            and prefix.is_ready(entry.prefix):
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
        location, wsl_note = _resolve_wsl(entry, tuple(wsl_search_paths(catalog)))
        if location is None and wsl_note:
            if host_capable:
                # 本平台有内置路径（厂商预编译产物），那就先让用户去装它：下载一个
                # exe 比启用 WSL 便宜得多。这里保留上一步的 not_installed，不要用
                # WSL 的原因把它覆盖成"需要一个 Linux 环境"。
                logger.info(f"'{entry.name}' is not on this host and WSL could not "
                            f"provide it either: {wsl_note}")
            else:
                # WSL 本身不可用与"WSL 里没装这个工具"是两种不同的处置：前者要引导
                # 用户启用 WSL，后者要在他已有的发行版里装工具。
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
    """解析清单里在当前平台**可达**的全部工具。

    用 ``for_reachable`` 而不是 ``for_platform`` 是关键：后者只列 conda 通道有本平台
    构建的条目，而 Windows 上那样的条目基本为零——用它当分母会让设置页与
    ``bio_tool_status`` 在 Windows 上永远"一个工具都没有"，把 WSL 与厂商预编译产物
    两条真实可用的路径一起漏掉。
    """
    plat = platform or mamba.runtime_platform()
    reachable = catalog.for_reachable(plat)
    result: Dict[str, ToolLocation] = {}
    for entry in reachable:
        location = resolve(entry, catalog, plat, refresh=refresh)
        if location is not None:
            result[entry.name] = location
    logger.info(f"Resolved {len(result)}/{len(reachable)} tool(s) on {plat}")
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


def install_hint(entry: catalog_mod.ToolEntry, platform: str = "") -> str:
    """一条照做就能推进的动作说明（设置页与模型看到的是同一句）。

    分支顺序对应三条真实可行的路径，不给"跑不通的命令"：

    1. 该平台 conda 通道有构建 → 装进私有前缀；
    2. 上游发布了该平台的预编译产物 → 去下载并用（Windows 上的 seqkit）；
    3. Windows 且清单有 linux-64 → 在 WSL 发行版里装；WSL 不可用时如实说明，
       而不是让用户去装一个此平台根本不存在的包。
    """
    plat = platform or mamba.runtime_platform()
    if entry.available_on(plat):
        return (f"Install it into the private prefix '{entry.prefix}': "
                f"python -m src.core.bioenv install {entry.prefix}")
    if entry.native_on(plat):
        return entry.native_hint or (f"Download the vendor build of '{entry.name}' "
                                     f"for {plat} and put it on PATH.")
    if is_windows() and plat.startswith("win-") and entry.available_on("linux-64"):
        status = wsl.detect()
        if status.usable:
            return (f"Install it inside the WSL distribution '{status.distro}' "
                    f"(conda or mamba, channel bioconda), then re-detect on the "
                    f"External Tools page.")
        return (f"Windows has no build of '{entry.name}'. {wsl.describe(status)} "
                f"After enabling WSL2 with a distribution, install the toolchain "
                f"inside it and re-detect.")
    return f"No build of '{entry.name}' exists for {plat}."


def unavailable_conclusion(entry: catalog_mod.ToolEntry, platform: str = "") -> str:
    """只有结论的一句（不含下一步动作）。

    分开的理由：工具返回值里 ``message`` 与 ``install_hint`` 是两个字段，模型据此
    区分"为什么不行"与"该做什么"；把两段话粘成一句会在同一份 JSON 里重复一遍。
    设置页那种单行展示则用 :func:`unavailable_reason`。
    """
    plat = platform or mamba.runtime_platform()
    kind, text = failure_reason(entry, plat)
    return {
        "unsupported_platform": f"'{entry.name}' has no build for {plat}",
        "not_installed": f"'{entry.name}' is not installed ({text})",
        "version_too_old": f"'{entry.name}' is installed but too old ({text})",
        "broken_install": f"'{entry.name}' is installed but cannot run ({text})",
        "wsl_unavailable": f"'{entry.name}' needs a Linux environment ({text})",
        "prefix_incomplete": f"the private prefix '{entry.prefix}' is incomplete ({text})",
        "configured_path_missing": f"the configured path is gone ({text})",
    }.get(kind, f"'{entry.name}' is unavailable: {text}")


def unavailable_reason(entry: catalog_mod.ToolEntry, platform: str = "") -> str:
    """工具不可用时的可读原因（供设置页与模型提示使用）。

    结构固定为"结论 + 下一步"：结论区分没装、装了但坏、版本旧与平台不存在，下一步
    统一由 :func:`install_hint` 给出，避免两处各写一套引导。
    """
    plat = platform or mamba.runtime_platform()
    return f"{unavailable_conclusion(entry, plat)}. {install_hint(entry, plat)}"


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
