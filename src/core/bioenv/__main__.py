"""生信工具链的命令行入口（``python -m src.core.bioenv <command>``）。

为什么需要一个模块入口
--------------------
设置页的"复制安装命令"要把命令贴进用户的终端。旧做法是用 ``python -c "...``
拼一长串带引号的代码，而引号转义在三个 shell 里各不相同：``cmd.exe`` 不认
``\\"``、PowerShell 用反引号、bash 又要吃掉一层。结果就是"复制了跑不起来"。
模块入口把这段代码搬进仓库，复制出去的只剩 ``python -m src.core.bioenv install
core``——一条三个 shell 都能原样执行的命令。

两个子命令
----------
``install [prefix]``
    在当前平台把某个前缀装起来。只请求本平台**真有包**的那些（见
    :meth:`src.core.bioenv.catalog.Catalog.installable_packages`）；Windows 上
    几乎没有 win-64 生信包，此时直接说明"应该在 WSL 里装"，而不是让求解器抛一堆
    ``nothing provides``。
``status``
    打印平台、WSL 状态与每个工具的落点，用于排查"为什么这个工具用不了"。

与应用内的分工：应用**从不自动下载**这套东西（几百 MB、要联网求解），命令行入口
只是把同一份实现暴露给用户的终端。退出码 0 表示成功，非 0 表示未完成。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 支持 ``python -m src.core.bioenv``（cwd 在仓库根）与 ``python
# src/core/bioenv/__main__.py`` 两种调用方式：后者的 path[0] 是包目录。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.core.bioenv import catalog as catalog_mod      # noqa: E402
from src.core.bioenv import paths                       # noqa: E402
from src.core.bioenv import prefix as prefix_mod        # noqa: E402
from src.core.bioenv import resolver                    # noqa: E402
from src.core.bioenv import wsl                         # noqa: E402
from src.core.platform_env import is_windows            # noqa: E402
from src.core.toolchain import micromamba as mamba      # noqa: E402

logger = logging.getLogger("BioEnv.Cli")


def _default_prefix(cat: catalog_mod.Catalog) -> str:
    """未指定前缀时用哪一个：清单里的第一个（内置清单是 ``core``）。"""
    names = cat.prefix_names()
    return names[0] if names else "core"


def cmd_status(args: argparse.Namespace) -> int:
    """打印平台、WSL 与每个工具的解析结果。"""
    cat = catalog_mod.load()
    platform = mamba.runtime_platform()
    print(f"platform: {platform}")
    print(f"wsl:      {wsl.describe()}")
    print(f"bioenv:   {paths.env_root()}")
    locations = resolver.resolve_all(cat, platform, refresh=True)
    print(resolver.summary(locations))
    missing = [e.name for e in cat.for_reachable(platform) if e.name not in locations]
    if missing:
        print("unavailable:")
        for name in missing:
            entry = cat.tool(name)
            if entry is not None:
                print(f"- {resolver.unavailable_reason(entry, platform)}")
    if cat.problems:
        print("catalog problems:")
        for problem in cat.problems:
            print(f"- {problem}")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """把某个前缀装进 ``<BASE_DIR>/bioenv/prefixes``。"""
    cat = catalog_mod.load()
    platform = mamba.runtime_platform()
    name = str(args.prefix or "").strip() or _default_prefix(cat)
    if name not in cat.prefix_names():
        print(f"unknown prefix '{name}'; known: {', '.join(cat.prefix_names())}",
              file=sys.stderr)
        return 2

    declared = cat.prefix_packages(name)
    packages = cat.installable_packages(name, platform)
    print(f"platform: {platform}")
    print(f"prefix:   {name}")
    print(f"packages: {' '.join(packages) or '-'}")
    if not packages:
        print("", file=sys.stderr)
        print(f"No package of prefix '{name}' has a build for {platform}.",
              file=sys.stderr)
        if is_windows():
            print(f"{wsl.describe()} Install {' '.join(declared)} inside the "
                  f"distribution (conda or mamba, channel bioconda), then re-detect "
                  f"on the External Tools page.", file=sys.stderr)
        return 2

    result = prefix_mod.create(
        name, packages, channels=cat.channels, tools=cat.tools_of(name),
        platform=platform, log_cb=print)
    if result.get("cancelled"):
        print("cancelled", file=sys.stderr)
        return 130
    if not result.get("success"):
        print(f"failed: {result.get('error') or 'unknown error'}", file=sys.stderr)
        return 1
    skipped = result.get("skipped") or []
    unavailable = result.get("unavailable") or []
    print(f"prefix '{name}' ready at {prefix_mod.prefix_dir(name)}")
    if skipped:
        print(f"solver skipped: {', '.join(skipped)}")
    if unavailable:
        print(f"not available for {platform}: {', '.join(unavailable)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """命令行结构（两个子命令，参数极少是刻意的）。"""
    parser = argparse.ArgumentParser(
        prog="python -m src.core.bioenv",
        description="Manage the private bioinformatics toolchain of Scholar Navis.")
    sub = parser.add_subparsers(dest="command")

    status = sub.add_parser("status", help="show platform, WSL state and tool locations")
    status.set_defaults(func=cmd_status)

    install = sub.add_parser("install", help="install a private prefix")
    install.add_argument("prefix", nargs="?", default="",
                         help="prefix name (default: the first declared prefix)")
    install.set_defaults(func=cmd_install)
    return parser


def main(argv=None) -> int:
    """解析并执行；无子命令时打印帮助并返回 2。"""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    logger.info(f"bioenv CLI: {args.command}")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
