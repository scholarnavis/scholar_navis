"""构建产物发布到 Cloudflare R2（CI 发版专用）。

命名契约
--------
对象名由 ``build_app.py`` 生成，形状为::

    f"{app_name_safe}_{platform_tag}_{channel}_v{version}.zip"

``channel`` 取 ``stable`` / ``dev``（``src.core.version.release_channel`` 按版本号
里是否含 ``-dev`` 判定）。通道必须落在**文件名**里：历史版本清理依据"同前缀"
（:func:`prune_prefix_of`），把通道并进前缀才能让两条通道的产物互不干涉——
否则上传一个 dev 构建会把同平台的稳定版一并删掉。

单通道时代的旧对象名 ``..._{platform_tag}_v{version}.zip`` 不会被任何前缀命中，
交给 :func:`publish_artifact` 的 ``legacy_prefixes`` 参数做一次性迁移清理。

应用侧的更新检查（``src/task/common_task.py::VersionCheckTask``）请求
``{__website__}/versions?os=<platform.system().lower()>`` 取两条通道的最新版本
（旧客户端仍可用 ``/latest?os=...&channel=...``），下载走
``{__website__}/dl?os=...&channel=...``。也就是说：**对象名里的 platform_tag /
channel 与请求里的 os / channel 字符串之间的映射由 Cloudflare 侧的 Worker 决定**，
本模块只按既有命名把文件放上去，不参与映射。改动 platform_tag 前必须先确认 Worker
的映射表，否则线上更新会静默失效（例如构建侧用 ``win`` 而应用侧请求
``os=windows``）。

失败语义
--------
历史实现的异常被 ``print`` 吞掉，后果是：R2 凭证过期、bucket 名写错、上传被拒时，
CI 依然是绿色，但产物根本没上去（或停留在旧版本），要等用户反馈才发现。

现在收紧为：

* **配置缺失**：CI（``GITHUB_ACTIONS=true``）下抛 :class:`ReleaseUploadError` 让
  流水线变红；本地运行（``strict=False``）只记日志并跳过，返回空对象名。
* **上传/校验/清理失败**：一律抛 :class:`ReleaseUploadError`。宽松只针对"没配置"，
  不针对"配置了却传失败"——那在任何环境下都是需要立刻知道的事实。
"""
from __future__ import annotations

import logging
import os
import re
import socket
import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

# `dotenv` 由 python-dotenv 提供（模块名 ≠ 发行名），类型检查器解析不到这层映射，
# 但运行期导入已实测可用（.venv/lib/python3.12/site-packages/dotenv/__init__.py）。
from dotenv import load_dotenv  # pyrefly: ignore[missing-import]

logger = logging.getLogger("BuildSupport.R2Release")

__all__ = ["ReleaseUploadError", "prune_prefix_of", "publish_artifact"]

#: 发布所需的环境变量；任一缺失即视为配置不完整
REQUIRED_ENV = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME",
)

#: Cloudflare 账户 ID：32 位十六进制。用于在连不上之前拦掉"粘错内容"的 secret。
_ACCOUNT_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


class ReleaseUploadError(RuntimeError):
    """R2 发布失败。CI 下未被捕获即代表本次发版不成功。"""


def prune_prefix_of(object_name: str) -> str:
    """从对象名推出"同平台同通道历史版本"的公共前缀。

    ``scholar_navis_linux_dev_v2.0.7-dev-1.zip`` -> ``scholar_navis_linux_dev_v``

    只认**版本号引导符** ``_v``（``_`` 紧接 ``v`` 才是），因此非贪婪匹配即可：
    ``scholar_navis`` 中的 ``_n`` 与通道名后的 ``_v`` 之前的片段都不会误判，
    首个 ``_v`` 命中位置必定是 ``_<平台>_<通道>_v``。
    无法识别时返回空串，调用方据此跳过清理而不是误删。
    """
    match = re.match(r"^(.*?_v)", object_name or "")
    return match.group(1) if match else ""


def _missing_env() -> list:
    return [key for key in REQUIRED_ENV if not (os.environ.get(key) or "").strip()]


def _configure_dns_family() -> None:
    """``R2_FORCE_IPV4=1`` 时把 DNS 解析限制为 IPv4（默认不干预）。

    背景：Cloudflare 的 R2 端点同时发布 A 与 AAAA 记录。Windows（以及不少 CI
    容器）没有 IPv6 出口，而 Python 的 ``socket`` 不像浏览器那样做 Happy
    Eyeballs——它会先在无法到达的 IPv6 地址上一直等到超时，整句报错只剩
    "Could not connect to the endpoint URL"，与 DNS/凭证问题无法区分。
    关掉 AAAA 之后连接会直接走 IPv4，失败也会立刻失败。
    """
    if (os.environ.get("R2_FORCE_IPV4") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return

    original_getaddrinfo = socket.getaddrinfo

    def ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = ipv4_only
    logger.info("R2_FORCE_IPV4 is set: R2 endpoint resolution is restricted to IPv4.")


def _build_client():
    """按 R2 的 S3 兼容接口建客户端（endpoint 由 account_id 推导，region 固定 auto）。

    做了两件比"直接丢给 boto3"更值的事：

    1. **校验 account id 的形状**。secret 里多带一对引号或一个换行是极常见的粘贴
       事故，而症状是"Could not connect to the endpoint URL"——一句与根因毫无关系
       的话。这里提前拦下并指出具体问题。
    2. **显式指定 path-style**。boto3 对自定义 endpoint 默认走 virtual-hosted
       style（``<bucket>.<account>.r2.cloudflarestorage.com``），而 R2 的 S3 端点
       约定是 ``<account>.r2.cloudflarestorage.com/<bucket>``；不指定就等于把
       bucket 名塞进主机名，结果同样是"连不上"。
    """
    _configure_dns_family()

    account_id = os.environ["R2_ACCOUNT_ID"].strip()
    if not _ACCOUNT_ID_RE.match(account_id):
        raise ReleaseUploadError(
            "R2_ACCOUNT_ID is not a Cloudflare account id: expected 32 hex characters, "
            f"got {len(account_id)} characters starting with {account_id[:2]!r} "
            f"(non_hex={any(c not in '0123456789abcdefABCDEF' for c in account_id)}). "
            "Re-set the secret by copying the Account ID from the dashboard URL or "
            "R2 -> overview, without quotes, spaces or line breaks."
        )

    return boto3.client(
        service_name="s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"].strip(),
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"].strip(),
        region_name="auto",
        config=Config(
            s3={"addressing_style": "path"},
            # 连接层失败通常是瞬时的，而 botocore 默认连接超时是 60s：一个 run 会先
            # 卡好几分钟才报错。收紧到 10s 并显式重试 5 次，失败得快、重试得多。
            connect_timeout=10,
            read_timeout=60,
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


def _upload(client, bucket: str, local_path: str, object_name: str) -> None:
    """上传并按 ContentLength 回读校验。

    只 print 一句 "Upload complete" 是不够的：上传被截断、权限只读导致的部分写入、
    网关提前返回 200 等情况都会留下损坏对象。回读一次大小即可拦住这类静默失败。
    """
    local_size = os.path.getsize(local_path)
    logger.info("Uploading %s (%d bytes) -> r2://%s/%s",
                local_path, local_size, bucket, object_name)
    client.upload_file(local_path, bucket, object_name)

    head = client.head_object(Bucket=bucket, Key=object_name)
    remote_size = int(head.get("ContentLength") or 0)
    if remote_size != local_size:
        raise ReleaseUploadError(
            f"uploaded object size mismatch: local={local_size} remote={remote_size} "
            f"(r2://{bucket}/{object_name})"
        )
    logger.info("Upload verified: r2://%s/%s (%d bytes)", bucket, object_name, remote_size)


def _prune_prefix(client, bucket: str, prefix: str, keep: str) -> list:
    """删除 ``prefix`` 下的历史版本，保留 ``keep``。

    手动翻页：``list_objects_v2`` 单次最多返回 1000 个键，只取首页会在版本堆积后
    静默留下陈旧对象。
    """
    if not prefix:
        logger.warning("Empty prune prefix; skip pruning (keep=%r).", keep)
        return []

    removed = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for obj in page.get("Contents") or []:
            key = obj.get("Key")
            if not key or key == keep:
                continue
            client.delete_object(Bucket=bucket, Key=key)
            removed.append(key)
            logger.info("Pruned old release: r2://%s/%s", bucket, key)
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token:                      # 防御：标记截断却没给游标
            logger.warning("list_objects_v2 reported truncation without a continuation "
                           "token; pruning may be incomplete.")
            break
    logger.info("Prune done under prefix %r: removed %d, kept %r",
                prefix, len(removed), keep)
    return removed


def _prune(client, bucket: str, object_name: str) -> list:
    """按对象名推导同通道前缀并清理历史版本（保留 ``object_name``）。"""
    prefix = prune_prefix_of(object_name)
    if not prefix:
        logger.warning("Cannot derive prune prefix from %r; skip pruning.", object_name)
        return []
    return _prune_prefix(client, bucket, prefix, object_name)


def _probe_endpoint(host: str, port: int = 443, timeout: float = 10.0) -> str:
    """连接失败后补做一次 DNS + TCP 探测，把"连不上"拆成可行动的事实。

    CI 里只有一句 "Could not connect to the endpoint URL"：DNS 不解析、TCP 被拒/
    超时、TLS 握手失败都会长成同一句话，而三者的修法完全不同（改 secret、查网络
    策略、查证书）。这里直接给出结论，省掉在 CI 上反复试错。
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        addresses = sorted({info[4][0] for info in infos})
    except OSError as exc:
        return f"DNS lookup of {host} failed ({type(exc).__name__}: {exc})."

    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as exc:
        return (f"DNS resolved {host} -> {', '.join(addresses[:3])}, but TCP connect to "
                f"port {port} failed ({type(exc).__name__}: {exc}).")

    return (f"DNS and TCP to {host}:{port} are fine ({', '.join(addresses[:3])}), so the "
            "failure sits above the socket layer (TLS / handshake / read timeout).")


def _connection_hint(exc: BaseException, bucket: str) -> str:
    """连接级失败时补一句"该查什么"，返回空串表示"错误已足够自解释"。

    为什么需要：CI 日志里的 URL 会被 GitHub 掩码成
    ``https://***.cloudflarestorage.com/***/....zip``，看不出账号 ID 是哪一段，
    而"连不上端点"与"凭证不对"是两类完全不同的问题（前者查 R2_ACCOUNT_ID，
    后者查 Key/Secret），报错文案不区分时很容易查错方向。
    """
    if isinstance(exc, ClientError):
        return ""                       # 服务端已明确回话（AccessDenied 等），无需提示
    if not isinstance(exc, (BotoCoreError, OSError)):
        return ""

    account_id = (os.environ.get("R2_ACCOUNT_ID") or "").strip()
    if account_id:
        endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        probe = _probe_endpoint(f"{account_id}.r2.cloudflarestorage.com")
    else:
        endpoint = "<R2_ACCOUNT_ID missing>"
        probe = "R2_ACCOUNT_ID is empty, so there is no endpoint to probe."

    return (f" [endpoint={endpoint} bucket={bucket}] Connection-level failure: {probe} "
            "If the value looks right, re-check that the secret holds exactly the "
            "32-char hex account id (no quotes/spaces/line breaks) and that R2 is "
            "enabled on that account. Wrong credentials fail differently "
            "(InvalidAccessKeyId / SignatureDoesNotMatch), so do not look there first. "
            "On hosts without IPv6 connectivity (Windows CI runners, some containers) set "
            "R2_FORCE_IPV4=1: the endpoint publishes AAAA records too, and Python waits "
            "for the unreachable address instead of falling back.")


def publish_artifact(local_path: str, *, strict: bool | None = None, client=None,
                     legacy_prefixes: tuple = ()) -> str:
    """把 ``local_path`` 上传到 R2，并清理同平台同通道的历史版本。

    Args:
        local_path: 待上传的本地文件（通常是一次打包产出的 zip）。
        strict: 配置缺失时是否报错。``None``（默认）表示按环境判断：
                ``GITHUB_ACTIONS=true`` 即为 True。上传失败在任何取值下都会报错。
        client: 可注入的 S3 客户端，便于测试时替换。
        legacy_prefixes: 需要**额外**清理的历史命名前缀（如双通道改造前的
                ``scholar_navis_win_v``）。默认不清理：只有调用方明确知道自己
                在迁移时才传入，避免误删仍在服务的对象。

    Returns:
        上传成功后的对象名；本地运行且未配置凭证而跳过时返回空字符串。

    Raises:
        ReleaseUploadError: strict 模式下配置缺失，或上传/校验/清理失败。
    """
    load_dotenv()                          # 不覆盖已有变量，CI 的 secrets 优先

    if strict is None:
        strict = (os.environ.get("GITHUB_ACTIONS") or "").strip() == "true"

    object_name = os.path.basename(local_path)
    missing = _missing_env()
    if missing:
        message = "R2 configuration incomplete; missing: " + ", ".join(missing)
        if strict:
            raise ReleaseUploadError(message)
        logger.info("%s -> skip R2 upload.", message)
        return ""

    bucket = os.environ["R2_BUCKET_NAME"].strip()
    if client is None:
        client = _build_client()

    try:
        started = time.time()
        _upload(client, bucket, local_path, object_name)
        _prune(client, bucket, object_name)
        # 迁移清理：旧命名下的对象不在任何"新前缀"里，只能按调用方给的前缀显式删。
        for legacy in legacy_prefixes:
            if legacy and legacy == prune_prefix_of(object_name):
                continue                   # 与新前缀相同则上面已经清过，别重复列举
            _prune_prefix(client, bucket, legacy, object_name)
    except ReleaseUploadError:
        raise
    except (ClientError, BotoCoreError, OSError) as exc:
        detail = getattr(exc, "response", None) or exc
        # 带上耗时：秒级失败 = 连接被拒/立即失败，几分钟才失败 = 超时（对定位
        # 网络策略与 IPv6 之类的环境问题很关键）。
        raise ReleaseUploadError(
            f"R2 publish failed for {object_name} after {time.time() - started:.1f}s: {detail}"
            + _connection_hint(exc, bucket)
        ) from exc

    return object_name
