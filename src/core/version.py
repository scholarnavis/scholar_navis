"""版本号、发布通道与版本比较的**唯一真相来源**。

发布通道（channel）
-------------------
规则只有一条：**版本号里出现 ``-dev`` 即为开发通道（``dev``），其余一律为
``stable``**。这条规则被三处消费，必须保持一致，因此只在这里实现：

* :mod:`build_app` —— 决定产物文件名 ``scholar_navis_{平台}_{通道}_v{版本}.zip``；
* Cloudflare Worker —— 按 ``{通道}`` 前缀列举 R2 对象，隔离两条通道的下载与清理；
* :class:`src.task.common_task.VersionCheckTask` —— 只与"本机所属通道"的最新版本比较。

版本比较
--------
:func:`parse_version` / :func:`is_newer_version` 集中在此，UI 层与网络层共用一份，
避免各写一套解析（旧实现里解析逻辑只存在于 UI 层，网络层无法复用）。

支持的写法：``2.0.6``、``2.0.6-dev-1``、``2.0.6-dev``、``2.0.6-dev1``、``2.2.4-beta-2``、``2.0``。

本项目统一用 **``a.b.c-dev-N``**（如 ``2.0.6-dev-1``）：``-dev`` 决定通道，尾部
``N`` 参与比较。序号是必需的——没有它，同一主版本内连发多轮 dev 会得到完全相同的
版本号与产物文件名，应用端判定"相等即无更新"，后上传的包还会直接覆盖前一个。
"""
from __future__ import annotations

import re

__version__ = "2.0.6-dev-2"
__app_name__ = "Scholar Navis"
__description__ = "AI-Powered Research Assistant"
__company__ = "Scholar Navis Studio"
__website__ = "https://scholarnavis.com"
__github__ = "https://github.com/scholarnavis/scholar_navis"

#: 本机版本所属通道（"dev" / "stable"），见模块文档。
__channel__ = "dev" if "-dev" in __version__.lower() else "stable"

#: 发布端点：全部由部署在 Cloudflare 侧的发布 Worker 提供（Worker 与官网页不属于
#: 本仓库，只共享这里的路径与产物命名契约，见 README「Packaging」一节）
#: - /versions  一次性返回该平台两条通道的最新版本（JSON）
#: - /latest    仅返回指定通道的最新版本（纯文本，保留给旧客户端）
#: - /dl        指定通道的产物下载（浏览器直接下载 zip）
#: - /changelog 指定版本的更新日志（由 Worker 代理 GitHub Release，减少国内直连 GitHub 的失败率）
__dl__ = f"{__website__}/dl"
__latest__ = f"{__website__}/latest"
__versions__ = f"{__website__}/versions"
__changelog__ = f"{__website__}/changelog"

#: 版本后缀 → 阶段权重。dev 与 alpha 同级（都低于 beta），比未知后缀高。
_STAGE_WEIGHTS = {"dev": 1, "alpha": 1, "beta": 2, "rc": 3, "final": 4}

#: ``a.b.c`` 主版本 + 可选 ``-stage[-]n``（本项目用 ``2.0.6-dev-1``；连字符与序号
#: 都可省略，``2.0.6-dev`` / ``2.0.6-dev1`` 同样能解析，只是不推荐）。
_VERSION_RE = re.compile(
    r"^(?P<main>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<stage>[A-Za-z]+)[-_.]?(?P<num>\d+)?)?$"
)

#: 解析失败时的返回值：严格小于任何合法版本，保证"脏数据不会触发更新提示"。
_UNPARSABLE = (0, 0, 0, -1, 0)


def release_channel(version: str | None = None) -> str:
    """按版本号判定发布通道：含 ``-dev`` → ``"dev"``，否则 ``"stable"``。

    :param version: 待判定的版本号；为 ``None`` 时使用 :data:`__version__`。
    """
    raw = (version if version is not None else __version__) or ""
    return "dev" if "-dev" in raw.lower() else "stable"


def parse_version(version: str) -> tuple:
    """把版本号解析成可比较元组 ``(major, minor, patch, stage_weight, stage_num)``。

    无法解析时返回 :data:`_UNPARSABLE`（最小元组），使脏数据永远被判定为"不更新"。
    """
    match = _VERSION_RE.match(str(version or "").strip())
    if not match:
        return _UNPARSABLE

    numbers = [int(x) for x in match.group("main").split(".")][:3]
    while len(numbers) < 3:          # 兼容 "2.0" 这类省略写法
        numbers.append(0)

    stage = (match.group("stage") or "final").lower()
    stage_num = int(match.group("num") or 0)
    return tuple(numbers + [_STAGE_WEIGHTS.get(stage, 0), stage_num])


def is_newer_version(latest: str, current: str) -> bool:
    """``latest`` 是否严格新于 ``current``（同通道内比较才有意义）。"""
    return parse_version(latest) > parse_version(current)
