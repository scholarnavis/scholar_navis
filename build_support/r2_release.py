"""构建产物发布到 Cloudflare R2（CI 发版专用）。

命名契约
--------
对象名沿用 ``build_app.py`` 既有约定::

    f"{app_name_safe}_{platform_tag}_v{version}.zip"

应用侧的更新检查（``src/task/common_task.py::VersionCheckTask``）请求
``{__website__}/latest?os=<platform.system().lower()>``，下载走
``{__website__}/dl?os=...``。也就是说：**对象名里的 platform_tag 与请求里的 os
字符串之间的映射由 Cloudflare 侧的 Worker 决定**，本模块只按既有命名把文件放上去，
不参与映射。改动 platform_tag 前必须先确认 Worker 的映射表，否则线上更新会静默失效
（例如构建侧用 ``win`` 而应用侧请求 ``os=windows``）。

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

import boto3
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


class ReleaseUploadError(RuntimeError):
    """R2 发布失败。CI 下未被捕获即代表本次发版不成功。"""


def prune_prefix_of(object_name: str) -> str:
    """从对象名推出"同平台历史版本"的公共前缀。

    ``scholar_navis_linux_v2.0.6.zip`` -> ``scholar_navis_linux_v``

    只认**版本号引导符** ``_v``（``_`` 紧接 ``v`` 才是），因此非贪婪匹配即可：
    ``scholar_navis`` 中的 ``_n`` 不会误判，首个命中位置必定是 ``_<tag>_v``。
    无法识别时返回空串，调用方据此跳过清理而不是误删。
    """
    match = re.match(r"^(.*?_v)", object_name or "")
    return match.group(1) if match else ""


def _missing_env() -> list:
    return [key for key in REQUIRED_ENV if not (os.environ.get(key) or "").strip()]


def _build_client():
    """按 R2 的 S3 兼容接口建客户端（endpoint 由 account_id 推导，region 固定 auto）。"""
    account_id = os.environ["R2_ACCOUNT_ID"].strip()
    return boto3.client(
        service_name="s3",
        endpoint_url=f"https://{account_id}.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"].strip(),
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"].strip(),
        region_name="auto",
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


def _prune(client, bucket: str, object_name: str) -> list:
    """删除同平台历史版本，保留 ``object_name``。

    手动翻页：``list_objects_v2`` 单次最多返回 1000 个键，只取首页会在版本堆积后
    静默留下陈旧对象。
    """
    prefix = prune_prefix_of(object_name)
    if not prefix:
        logger.warning("Cannot derive prune prefix from %r; skip pruning.", object_name)
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
            if not key or key == object_name:
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
                prefix, len(removed), object_name)
    return removed


def publish_artifact(local_path: str, *, strict: bool | None = None, client=None) -> str:
    """把 ``local_path`` 上传到 R2，并清理同平台历史版本。

    Args:
        local_path: 待上传的本地文件（通常是一次打包产出的 zip）。
        strict: 配置缺失时是否报错。``None``（默认）表示按环境判断：
                ``GITHUB_ACTIONS=true`` 即为 True。上传失败在任何取值下都会报错。
        client: 可注入的 S3 客户端，便于测试时替换。

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
        _upload(client, bucket, local_path, object_name)
        _prune(client, bucket, object_name)
    except ReleaseUploadError:
        raise
    except (ClientError, BotoCoreError, OSError) as exc:
        detail = getattr(exc, "response", None) or exc
        raise ReleaseUploadError(
            f"R2 publish failed for {object_name}: {detail}"
        ) from exc

    return object_name
