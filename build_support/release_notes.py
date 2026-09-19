"""GitHub Release 正文生成（CI 发版专用）。

为什么单独一个模块
------------------
发版正文同时承载三件事，直接写进 workflow 的 heredoc 会让三者互相干扰
（Markdown 表格里的 ``|``、提交标题里的反引号/引号、shell 变量展开）：

1. **版本与通道**：``v2.0.7-dev-1`` → 通道 ``dev``。通道规则只在
   :func:`src.core.version.release_channel` 里实现，本模块**不**重复判定，
   否则"产物名里的通道"与"Release 标记的通道"会漂移；
2. **下载地址**：``https://scholarnavis.com/dl?os={os}&channel={channel}``，
   与应用内更新提示指向同一个入口（Worker 决定实际对象）；
3. **更新日志**：上一个可达 tag 到本 tag 之间的非合并提交；首版则退化为
   全部历史（此时会显式标注，不假装有"变更范围"）。
   调用方同时开启 ``generate_release_notes``，GitHub 会在正文之后追加它自己
   生成的贡献者名单。

顺带做一次**产物自检**：给定 ``--assets-dir`` 时，目录里的每个 zip 都必须匹配
``scholar_navis_{平台}_{通道}_v{版本}.zip`` 且通道/版本与 tag 一致。发版流水线里
"产物名与 tag 不一致"意味着 R2 上会出现一个谁也查不到（或查错通道）的对象，
必须在创建 Release 之前就失败。

CLI::

    python build_support/release_notes.py --tag v2.0.7-dev-1 \\
        --repo scholarnavis/scholar_navis --assets-dir release-assets --out body.md
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

# 支持两种调用方式：`python -m build_support.release_notes`（cwd 在仓库根，path[0]
# 已是根）与 `python build_support/release_notes.py`（path[0] 是脚本所在目录）。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.version import release_channel  # noqa: E402  路径修正后才能导入

logger = logging.getLogger("BuildSupport.ReleaseNotes")

#: 产物文件名 → (平台标记, 通道, 版本)。平台标记由 build_app.platform_tag 生成。
ASSET_RE = re.compile(
    r"^scholar_navis_(?P<platform>[a-z]+)_(?P<channel>[a-z]+)_v(?P<version>.+)\.zip$")

#: 各平台标记 → ``/dl`` 的 ``os`` 查询值（与应用侧 platform.system() 小写一致）。
PLATFORM_OS_PARAM = {"win": "windows", "linux": "linux", "mac": "darwin"}

#: 各平台标记 → 正文里给人看的名字。
PLATFORM_LABELS = {"win": "Windows", "linux": "Linux", "mac": "macOS"}

#: 产物直链前缀，与应用内 ``version.__dl__`` 同源。
DEFAULT_DOWNLOAD_BASE = "https://scholarnavis.com/dl"

#: 通道 → 正文里的一句说明（告诉用户"为什么没收到/收到了这个更新"）。
CHANNEL_NOTES = {
    "stable": ("This is a **stable channel** build. Stable installations compare "
               "against the stable channel only."),
    "dev": ("This is a **dev channel** build. Only installations whose own version "
            "contains `-dev` will be offered this update; stable users are not "
            "notified."),
}


def normalize_version(value: str) -> str:
    """``v2.0.7-dev-1`` / ``2.0.7-dev-1`` → ``2.0.7-dev-1``。"""
    return (value or "").strip().removeprefix("v").removeprefix("V")


def resolve_revision(tag: str, cwd: str | Path | None = None) -> str:
    """``tag`` 在本仓库存在则原样返回，否则退化为 ``HEAD``（本地试跑友好）。

    CI 在 tag 上检出，正常路径不会走到退化分支；本地跑（例如验证正文排版）时
    tag 往往不存在，此时用 HEAD 生成一份结构相同的正文比直接报错有用。
    """
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}"],
        cwd=str(cwd or Path.cwd()), capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return tag
    logger.warning("Tag %s is not present in this checkout; falling back to HEAD.", tag)
    return "HEAD"


def previous_tag(tag: str, cwd: str | Path | None = None) -> str:
    """返回 ``tag`` 之前最近的可达 tag；没有则返回空串（视作首版）。"""
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", f"{tag}^"],
        cwd=str(cwd or Path.cwd()), capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logger.info("No previous tag reachable from %s^; treating as the first release.",
                    tag)
        return ""
    return result.stdout.strip()


def collect_commits(tag: str, previous: str = "", cwd: str | Path | None = None,
                    max_commits: int = 200) -> list:
    """取更新范围内的非合并提交，返回 ``[(短哈希, 标题), ...]``（新→旧）。

    用 ``\\x1f`` 作字段分隔符：提交标题里出现 ``|``、``:`` 甚至制表符都不影响解析。
    """
    revision = f"{previous}..{tag}" if previous else tag
    result = subprocess.run(
        ["git", "log", "--no-merges", f"--max-count={max_commits}",
         "--pretty=format:%h\x1f%s", revision],
        cwd=str(cwd or Path.cwd()), capture_output=True, text=True, check=False)
    if result.returncode != 0:
        message = (result.stderr or "").strip() or "unknown git error"
        raise SystemExit(f"git log failed for {revision!r}: {message}")

    commits = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        short_hash, _, subject = line.partition("\x1f")
        commits.append((short_hash.strip(), subject.strip()))
    logger.info("Collected %d commit(s) for range %r.", len(commits), revision)
    return commits


def inspect_assets(assets_dir: str | Path) -> list:
    """校验产物目录，返回 ``[(文件名, 平台标记, 通道, 版本), ...]``（按文件名排序）。

    这里刻意**不做**任何"容错跳过"：出现不符合命名契约的 zip 说明构建侧与发版侧
    对不齐，继续发版只会把错误固化到线上。
    """
    directory = Path(assets_dir)
    if not directory.is_dir():
        raise SystemExit(f"assets directory not found: {directory}")

    assets = []
    for path in sorted(directory.glob("*.zip")):
        match = ASSET_RE.match(path.name)
        if not match:
            raise SystemExit(
                f"artifact {path.name!r} does not match the release naming contract "
                f"'scholar_navis_{{platform}}_{{channel}}_v{{version}}.zip'")
        assets.append((path.name, match.group("platform"),
                       match.group("channel"), match.group("version")))

    if not assets:
        raise SystemExit(f"no *.zip artifacts found in {directory}")
    return assets


def verify_assets(assets: list, version: str, channel: str) -> None:
    """产物必须与自己所属通道/版本一致（否则线上会出现查不到的对象）。"""
    for name, _platform, asset_channel, asset_version in assets:
        if asset_channel != channel:
            raise SystemExit(f"artifact {name!r} is on channel {asset_channel!r}, "
                             f"but tag implies channel {channel!r}")
        if asset_version != version:
            raise SystemExit(f"artifact {name!r} carries version {asset_version!r}, "
                             f"but tag implies version {version!r}")


def build_body(*, version: str, channel: str, repo: str, commits: list,
               previous: str = "", download_base: str = DEFAULT_DOWNLOAD_BASE,
               platforms: tuple = ()) -> str:
    """组装 Release 正文（纯 Markdown）。"""
    tag = f"v{version}"
    release_url = f"https://github.com/{repo}/releases/tag/{tag}"

    lines = [
        f"## Scholar Navis {tag}",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Channel | `{channel}` |",
        f"| Version | `{version}` |",
        f"| Tag | [`{tag}`]({release_url}) |",
        f"| Source | [{repo}](https://github.com/{repo}) |",
        "",
    ]

    if platforms:
        lines += [
            "### Downloads",
            "",
            "The links below always resolve to the latest build of the given channel "
            "(same endpoint the in-app update prompt uses). The zips built for this "
            "release are attached to this Release as assets.",
            "",
            "| Platform | Link |",
            "| --- | --- |",
        ]
        for platform in platforms:
            os_param = PLATFORM_OS_PARAM.get(platform, platform)
            label = PLATFORM_LABELS.get(platform, platform)
            url = f"{download_base}?os={os_param}&channel={channel}"
            lines.append(f"| {label} | [{url}]({url}) |")
        lines.append("")

    note = CHANNEL_NOTES.get(channel)
    if note:
        lines += [f"> {note}", ""]

    if previous:
        lines += [f"### Changes since `{previous}`", ""]
    else:
        lines += ["### Changes (initial release — full history)", ""]

    if commits:
        lines += [f"- `{short}` {subject}" for short, subject in commits]
    else:
        lines.append("- No commit metadata available for this range.")
    lines.append("")

    return "\n".join(lines)


def _emit_github_outputs(channel: str, platforms: tuple) -> None:
    """把通道信息写给 workflow（避免在 YAML 里重复一遍通道判定规则）。"""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    is_dev = channel == "dev"
    with open(output_path, "a", encoding="utf-8") as fh:
        fh.write(f"channel={channel}\n")
        fh.write(f"prerelease={'true' if is_dev else 'false'}\n")
        # dev 版本绝不能顶掉稳定版的 "Latest release" 标记。
        fh.write(f"make_latest={'false' if is_dev else 'true'}\n")
        fh.write(f"platforms={','.join(platforms)}\n")
    logger.info("Wrote channel outputs to %s (channel=%s).", output_path, channel)


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser(description="Compose the GitHub Release body.")
    parser.add_argument("--tag", default=os.environ.get("GITHUB_REF_NAME", ""),
                        help="Release tag, e.g. v2.0.7-dev-1 (defaults to $GITHUB_REF_NAME).")
    parser.add_argument("--version", default="",
                        help="Explicit version string; overrides --tag when given.")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--previous-tag", default="",
                        help="Force the changelog base tag instead of git describe.")
    parser.add_argument("--assets-dir", default="",
                        help="Directory holding the *.zip artifacts to validate.")
    parser.add_argument("--download-base", default=DEFAULT_DOWNLOAD_BASE)
    parser.add_argument("--out", required=True, help="Where to write the Markdown body.")
    parser.add_argument("--cwd", default="", help="Repository root for git commands.")
    args = parser.parse_args(argv)

    version = normalize_version(args.version or args.tag)
    if not version:
        raise SystemExit("Neither --version nor --tag/$GITHUB_REF_NAME provided.")
    if not args.repo:
        raise SystemExit("--repo (or $GITHUB_REPOSITORY) is required.")

    channel = release_channel(version)          # 通道规则唯一来源
    logger.info("Composing release notes: version=%s channel=%s repo=%s",
                version, channel, args.repo)

    platforms: tuple = ()
    if args.assets_dir:
        assets = inspect_assets(args.assets_dir)
        verify_assets(assets, version=version, channel=channel)
        platforms = tuple(sorted({platform for _n, platform, _c, _v in assets}))
        logger.info("Validated %d artifact(s): %s",
                    len(assets), ", ".join(name for name, *_ in assets))

    revision = resolve_revision(f"v{version}", args.cwd or None)
    base = args.previous_tag or previous_tag(revision, args.cwd or None)
    commits = collect_commits(revision, previous=base, cwd=args.cwd or None)

    body = build_body(version=version, channel=channel, repo=args.repo,
                      commits=commits, previous=base,
                      download_base=args.download_base, platforms=platforms)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")
    logger.info("Release body written to %s (%d bytes, %d commit(s)).",
                out_path, len(body), len(commits))

    _emit_github_outputs(channel, platforms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
