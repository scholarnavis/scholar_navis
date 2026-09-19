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
4. **LLM 双语摘要**（可选）：把提交列表交给 OpenAI 兼容接口，产出中英两段
   面向用户的变更说明。**未配置或调用失败一律自动退化为"只列提交"**——
   发版不能被一个可选的润色环节卡住。

顺带做一次**产物自检**：给定 ``--assets-dir`` 时，目录里的每个 zip 都必须匹配
``scholar_navis_{平台}_{通道}_v{版本}.zip`` 且通道/版本与 tag 一致。发版流水线里
"产物名与 tag 不一致"意味着 R2 上会出现一个谁也查不到（或查错通道）的对象，
必须在创建 Release 之前就失败。

LLM 配置（全部走环境变量 / 命令行，代码里不留供应商硬编码）
-----------------------------------------------------------
``LLM_API_KEY``   必填，缺失即视为"未启用"，脚本只记一条 INFO 就继续；
``LLM_BASE_URL``  OpenAI 兼容的 ``.../v1``（或直接给 ``.../chat/completions``）；
``LLM_MODEL``     模型名。

只依赖标准库（``urllib``）：Release 任务不需要为了这一次调用去 ``uv sync``
拉整套依赖，也就不会因为某个包解析失败而拖垮发版。

CLI::

    python build_support/release_notes.py --tag v2.0.7-dev-1 \\
        --repo scholarnavis/scholar_navis --assets-dir release-assets --out body.md
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
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

#: LLM 摘要的署名（明示"这段是模型写的"，与原始提交列表区分）。
LLM_NOTE_EN = ("Summarised by an LLM from the commit log. If a statement looks off, "
               "the raw commit list below is authoritative.")
LLM_NOTE_ZH = "本节由 LLM 依据提交日志自动汇总；若表述有出入，以下方原始提交列表为准。"

# --------------------------------------------------------------------------- #
#  LLM 摘要配置
# --------------------------------------------------------------------------- #
#: 提交过多时只把最近 N 条交给模型（更早的变更由上一个版本覆盖）。
DEFAULT_MAX_COMMITS = 100
#: 单次请求超时（秒）与重试次数（重试间隔 = 3s × 第几次）。
LLM_TIMEOUT = 120
LLM_RETRIES = 2
LLM_BACKOFF_SECONDS = 3

#: 模型只依据这些材料写作。措辞刻意保守：宁可少写，也不许编造。
LLM_SYSTEM_PROMPT = """\
You are the release-notes writer for Scholar Navis, an open-source desktop AI
research assistant: a PySide6/Qt desktop application with local ONNX embedding
and reranking models, retrieval over authoritative sources (PubMed, Crossref,
OpenAlex, UniProt, PDB, STRING, KEGG ...), optional R-based figure rendering,
and an MCP tool / skill plugin system. Builds ship for Windows and Linux on a
stable channel and a dev channel.

You receive the git commit subjects of a single release. Produce a concise,
user-facing changelog in English and in Chinese.

Hard rules:
- Use ONLY what the commit subjects state. Never invent features, numbers,
  file names, or user impact that is not implied by them.
- Group by type, in this order, skipping empty groups: Features, Fixes,
  Performance, Refactoring, Documentation, Build & CI, Other.
- Merge duplicates and near-duplicates; one bullet per user-visible change.
- Prefer wording an application user understands over internal symbol names.
- Keep technology, product and database names exactly as written
  (PySide6, ONNX, PubMed, R2, Markdown ...).
- No heading, no download table, no version number, no date, no signature,
  no code fence, no bold-only lines. Top-level bullets only.
- The Chinese list must say exactly the same things as the English list.

Output nothing but the two sections below and nothing else outside them:
<EN>
- bullet
- bullet
</EN>
<ZH>
- 条目
- 条目
</ZH>"""

#: 正文里中英两段的标题（由脚本添加，避免模型自造标题风格不一致）。
SUMMARY_HEADINGS = {
    "en": "### What's Changed",
    "zh": "### 更新内容（中文）",
}

_EN_SECTION_RE = re.compile(r"<EN>(.*?)</EN>", re.S | re.I)
_ZH_SECTION_RE = re.compile(r"<ZH>(.*?)</ZH>", re.S | re.I)


def llm_settings(args) -> dict:
    """从命令行/环境变量取 LLM 配置；缺少任一项返回空字典（= 不启用）。

    环境变量优先于默认值，命令行优先于环境变量——CI 走 env，本地试跑走参数。
    """
    api_key = (args.llm_api_key or os.environ.get("LLM_API_KEY") or "").strip()
    base_url = (args.llm_base_url or os.environ.get("LLM_BASE_URL") or "").strip()
    model = (args.llm_model or os.environ.get("LLM_MODEL") or "").strip()

    if getattr(args, "no_llm", False):
        logger.info("LLM summary disabled by --no-llm.")
        return {}

    missing = [name for name, value in (("LLM_API_KEY", api_key),
                                        ("LLM_BASE_URL", base_url),
                                        ("LLM_MODEL", model)) if not value]
    if missing:
        logger.info("LLM summary disabled (missing: %s). The body will list commits "
                    "verbatim.", ", ".join(missing))
        return {}

    return {"api_key": api_key, "base_url": base_url, "model": model,
            "timeout": int(args.llm_timeout or LLM_TIMEOUT)}


def build_llm_messages(version: str, channel: str, commits: list, previous: str = "") -> list:
    """构造 chat messages：系统提示 + 事实材料（版本/通道/提交列表）。"""
    if previous:
        scope = f"Changes since {previous} (compare against that tag)."
    else:
        scope = "This is the first release; the list covers the whole history."

    commit_lines = "\n".join(f"- {short} {subject}" for short, subject in commits) \
        or "- (no commit metadata available)"

    user_prompt = (
        f"Release: v{version} ({channel} channel)\n"
        f"{scope}\n"
        f"Commit subjects ({len(commits)}):\n{commit_lines}"
    )
    return [{"role": "system", "content": LLM_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}]


def sanitize_bullet_list(text: str) -> str:
    """清洗模型输出：只保留条目行（含缩进的子条目），丢掉标题/围栏/引用等。

    模型偶尔会自作主张补一个 ``### 更新内容`` 标题或包一层代码围栏；这些会被
    正文的既有结构重复表达，因此在此剥掉，而不是靠提示词"祈求"它听话。
    """
    kept = []
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("```", "#", ">", "|")):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def parse_llm_sections(content: str) -> tuple:
    """从模型回复里取出中英两段，返回 ``(english, chinese)``；缺失为 None。"""
    def _extract(pattern):
        match = pattern.search(content or "")
        if not match:
            return None
        body = sanitize_bullet_list(match.group(1))
        # 至少要有一条 "- " 开头的条目，否则视为该段不可用（宁缺毋滥）。
        if not any(line.lstrip().startswith("- ") for line in body.splitlines()):
            return None
        return body

    return _extract(_EN_SECTION_RE), _extract(_ZH_SECTION_RE)


def _post_chat_completion(settings: dict, messages: list) -> str:
    """调用 OpenAI 兼容的 ``/chat/completions``，返回 message.content。"""
    base_url = settings["base_url"].rstrip("/")
    url = base_url if base_url.endswith("/chat/completions") \
        else f"{base_url}/chat/completions"

    payload = {
        "model": settings["model"],
        "messages": messages,
        "temperature": 0.2,   # 发版说明要稳定复现，不要发挥
        "stream": False,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {settings['api_key']}"},
        method="POST")

    with urllib.request.urlopen(request, timeout=settings["timeout"]) as response:
        body = json.loads(response.read().decode("utf-8", errors="replace"))

    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError(f"LLM response carries no choices: {str(body)[:200]}")
    content = (choices[0].get("message") or {}).get("content") or ""
    if not content.strip():
        raise RuntimeError("LLM returned an empty message.")
    return content


def summarize_with_llm(version: str, channel: str, commits: list,
                       settings: dict, previous: str = "") -> tuple:
    """让模型写中英双语摘要；任何失败都返回 ``(None, None)`` 而不抛异常。

    失败即降级：Release 正文退回"原始提交列表"，发版流程继续。
    """
    if not commits:
        logger.info("No commits to summarise; skipping LLM call.")
        return None, None

    messages = build_llm_messages(version, channel, commits, previous)
    logger.info("Requesting bilingual release notes from %s (model=%s, commits=%d).",
                settings["base_url"], settings["model"], len(commits))

    last_error = ""
    for attempt in range(LLM_RETRIES + 1):
        started = time.time()
        try:
            content = _post_chat_completion(settings, messages)
            english, chinese = parse_llm_sections(content)
            elapsed = time.time() - started

            if english and chinese:
                logger.info("Bilingual summary parsed in %.1fs "
                            "(en=%d chars, zh=%d chars, raw=%d chars).",
                            elapsed, len(english), len(chinese), len(content))
                return english, chinese

            last_error = (f"model output lacks valid <EN>/<ZH> bullet sections "
                          f"({len(content)} chars, {elapsed:.1f}s): {content[:200]!r}")
            # 输出结构不对时重试往往无效（温度已很低），但代价低，保留重试。
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:                       # 读取错误体本身失败无关紧要
                detail = ""
            last_error = f"HTTP {exc.code}: {detail or exc.reason}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < LLM_RETRIES:
            delay = LLM_BACKOFF_SECONDS * (attempt + 1)
            logger.warning("LLM summary attempt %d/%d failed (%s); retrying in %ds.",
                           attempt + 1, LLM_RETRIES + 1, last_error, delay)
            time.sleep(delay)

    logger.warning("LLM summary unavailable after %d attempt(s): %s. "
                   "Falling back to the raw commit list.",
                   LLM_RETRIES + 1, last_error)
    return None, None


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


def _raw_commits_block(commits: list, previous: str, collapsed: bool) -> list:
    """原始提交列表。

    ``collapsed=True``（有 LLM 摘要时）收进 ``<details>``：正文给人看结论，
    原始证据仍留在同一条 Release 里可核查——GitHub 上是折叠块，应用内的
    QTextBrowser 不认 ``<details>``，会平铺显示，不会丢内容。
    """
    if previous:
        title = f"Raw commit log since `{previous}` ({len(commits)} commits)"
    else:
        title = f"Raw commit log — initial release ({len(commits)} commits)"

    body = [f"- `{short}` {subject}" for short, subject in commits] \
        or ["- No commit metadata available for this range."]

    if not collapsed:
        heading = (f"### Changes since `{previous}`" if previous
                   else "### Changes (initial release — full history)")
        return [heading, "", *body, ""]

    return ["<details>", f"<summary>{title}</summary>", "", *body, "",
            "</details>", ""]


def build_body(*, version: str, channel: str, repo: str, commits: list,
               previous: str = "", download_base: str = DEFAULT_DOWNLOAD_BASE,
               platforms: tuple = (), english_summary: str | None = None,
               chinese_summary: str | None = None) -> str:
    """组装 Release 正文（纯 Markdown）。

    ``english_summary`` / ``chinese_summary`` 同时给出时才启用双语摘要段；
    否则正文退回"只列提交"（LLM 未配置或调用失败的降级路径）。
    """
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

    if english_summary and chinese_summary:
        lines += [
            SUMMARY_HEADINGS["en"], "",
            english_summary, "",
            f"> {LLM_NOTE_EN}", "",
            SUMMARY_HEADINGS["zh"], "",
            chinese_summary, "",
            f"> {LLM_NOTE_ZH}", "",
        ]
        lines += _raw_commits_block(commits, previous, collapsed=True)
    else:
        lines += _raw_commits_block(commits, previous, collapsed=False)

    return "\n".join(lines)


def _write_step_summary(version: str, channel: str, settings: dict,
                        english: str | None, chinese: str | None,
                        commits: list) -> None:
    """把本次摘要写进 GitHub Actions 运行摘要，便于发布前人工过一眼。

    有的任务摘要里有 LLM 输出与"是否启用了 LLM"的明确结论，出问题（例如模型
    编造内容）时不必翻整段 job 日志。本地运行没有该环境变量，静默跳过。
    """
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    if not settings:
        state = "disabled (LLM_API_KEY / LLM_BASE_URL / LLM_MODEL not all set)"
    elif english and chinese:
        state = f"generated via `{settings['model']}`"
    else:
        state = f"failed (fell back to the raw commit list) via `{settings['model']}`"

    lines = [
        f"## Release notes · v{version} (`{channel}`)",
        "",
        f"- LLM summary: **{state}**",
        f"- Commits in range: {len(commits)}",
        "",
    ]
    if english and chinese:
        lines += ["### English", "", english, "", "### 中文", "", chinese, ""]

    try:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        logger.info("Step summary written to %s (llm=%s).", summary_path, state)
    except OSError as exc:
        logger.warning("Could not write step summary: %s", exc)


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
    parser.add_argument("--max-commits", type=int, default=DEFAULT_MAX_COMMITS,
                        help="Cap on the commit list (defaults to %(default)s).")
    # LLM 摘要（可选）：默认按环境变量启用，任一配置缺失即自动退化。
    parser.add_argument("--no-llm", action="store_true",
                        help="Never call an LLM; keep the raw commit list only.")
    parser.add_argument("--llm-api-key", default="", help="Overrides $LLM_API_KEY.")
    parser.add_argument("--llm-base-url", default="", help="Overrides $LLM_BASE_URL.")
    parser.add_argument("--llm-model", default="", help="Overrides $LLM_MODEL.")
    parser.add_argument("--llm-timeout", type=int, default=0,
                        help="Per-request timeout in seconds (defaults to %d)." % LLM_TIMEOUT)
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
    commits = collect_commits(revision, previous=base, cwd=args.cwd or None,
                              max_commits=args.max_commits)

    settings = llm_settings(args)
    english = chinese = None
    if settings:
        english, chinese = summarize_with_llm(version=version, channel=channel,
                                              commits=commits, settings=settings,
                                              previous=base)

    body = build_body(version=version, channel=channel, repo=args.repo,
                      commits=commits, previous=base,
                      download_base=args.download_base, platforms=platforms,
                      english_summary=english, chinese_summary=chinese)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")
    logger.info("Release body written to %s (%d bytes, %d commit(s), llm_summary=%s).",
                out_path, len(body), len(commits), bool(english and chinese))

    _emit_github_outputs(channel, platforms)
    _write_step_summary(version, channel, settings, english, chinese, commits)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
