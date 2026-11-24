"""WebDAV 备份源：协议客户端、远端清单与恢复暂存位置。

职责边界：

* 这里只做 WebDAV 协议本身（PROPFIND / MKCOL / PUT / GET）与远端文件命名，
  不含配置束的构造与加解密。那两件事分别由 :mod:`src.core.config_bundle` 与
  :class:`src.task.config_task.ExportConfigTask` 负责，因此"导出到本地"与
  "上传到 WebDAV"产出的 byte 完全一致，恢复时也走同一条导入链路。
* 密码只在内存里使用，由 :mod:`src.core.secrets_store` 托管在
  ``config/secrets.json``；本模块不落盘、不写日志、不回显。

TLS：默认校验证书。备份源允许显式勾选"接受自签名证书"（家用 NAS 常见），
该选择随凭据一起由用户确认，降级只影响本客户端的 ``verify`` 参数。
"""

from __future__ import annotations

import logging
import os
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote

import requests

from src.core.network_worker import apply_proxy_to_session

logger = logging.getLogger("Core.WebDAV")

#: 远端备份文件名前缀。列表只认这个前缀，避免把目录里其它文件当成备份。
BACKUP_PREFIX = "scholar_navis_backup_"

#: 备份文件扩展名
BACKUP_SUFFIX = ".json"

#: (连接, 读取) 超时。备份文件只有几百 KB，读超时给足弱网余量。
_TIMEOUT = (10.0, 120.0)

_DAV_NS = "{DAV:}"

_PROPFIND_FILE_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:displayname/><d:getcontentlength/><d:getlastmodified/><d:resourcetype/>"
    "</d:prop></d:propfind>"
)


class WebDAVError(RuntimeError):
    """WebDAV 操作的失败原因（消息直接面向用户，不含凭据）。"""


def normalize_server_url(url: str) -> str:
    """补全协议头并去掉尾部斜杠；空值直接拒绝。"""
    text = str(url or "").strip()
    if not text:
        raise WebDAVError("Backup source URL is empty.")
    if not text.lower().startswith(("http://", "https://")):
        text = "https://" + text
    return text.rstrip("/")


def _remote_segments(profile: Dict[str, Any]) -> List[str]:
    """远端目录按段拆分（忽略空段与多余斜杠）。"""
    raw = str((profile or {}).get("remote_dir") or "").split("/")
    return [seg.strip() for seg in raw if seg.strip()]


def _dir_url(profile: Dict[str, Any]) -> str:
    """远端目录 URL，带尾斜杠（部分服务端把集合当成必须以 / 结尾的资源）。"""
    url = normalize_server_url((profile or {}).get("url"))
    for seg in _remote_segments(profile):
        url += "/" + quote(seg, safe="")
    return url + "/"


def _file_url(profile: Dict[str, Any], name: str) -> str:
    return _dir_url(profile) + quote(str(name), safe="")


def backup_file_name(now: Optional[float] = None) -> str:
    """按时间戳生成备份文件名；同名即同一秒内重复上传，后写覆盖前写。"""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"


def is_backup_name(name: str) -> bool:
    """是否为本程序产生的备份文件名（下载前的第一道校验）。"""
    text = str(name or "")
    return (text.startswith(BACKUP_PREFIX) and text.lower().endswith(BACKUP_SUFFIX)
            and "/" not in text and "\\" not in text)


#: 恢复暂存文件的权限。暂存的是完整配置束，未设口令的备份在盘上是明文，
#: 因此与 ``secrets.json`` 同口径只允许属主读写。
RESTORE_STAGING_MODE = 0o600


def restore_staging_path() -> str:
    """恢复时的本地暂存文件。

    固定文件名而非随机名：恢复失败重试（例如密码输错）要能复用同一个文件，
    也不会在缓存目录里堆出多份配置束。位于 ``cache/`` 下，可被附件缓存清扫
    逻辑按过期时间一并回收；写入方须按 :data:`RESTORE_STAGING_MODE` 落盘。
    """
    from src.core.attachment_parser import cache_root

    return os.path.join(cache_root(), "backup_restore", f"restore_last{BACKUP_SUFFIX}")


class WebDAVClient:
    """单个备份源的 WebDAV 客户端（上下文管理，退出时关闭会话）。"""

    def __init__(self, profile: Dict[str, Any]):
        self.profile = dict(profile or {})
        self._base = normalize_server_url(self.profile.get("url"))
        self._username = str(self.profile.get("username") or "")
        self._password = str(self.profile.get("password") or "")
        self._verify = bool(self.profile.get("verify_ssl", True))
        self._session: Optional[requests.Session] = None
        if not self._verify:
            logger.warning(
                "WebDAV source '%s' is configured to trust self-signed certificates",
                self.profile.get("name") or self._base)

    # ---------- 生命周期 ----------
    def __enter__(self) -> "WebDAVClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception as e:      # noqa: BLE001 - 关闭失败不影响已得到的结果
                logger.debug("Closing WebDAV session failed: %s", e)

    # ---------- 请求 ----------
    def _open_session(self) -> requests.Session:
        if self._session is not None:
            return self._session
        # 用标准 requests 而非浏览器伪装会话：WebDAV 不靠 TLS 指纹，伪装头也只会
        # 让服务端日志难以排查。代理仍然走唯一实现 apply_proxy_to_session。
        session = requests.Session()
        apply_proxy_to_session(session, self._base)
        self._session = session
        return session

    def _request(self, method: str, url: str, **kwargs):
        session = self._open_session()
        auth = (self._username, self._password) if self._username else None
        try:
            return session.request(method, url, auth=auth, verify=self._verify,
                                   timeout=_TIMEOUT, **kwargs)
        except requests.RequestException as e:
            raise WebDAVError(f"Network error while {method} {url}: {e}") from e

    @staticmethod
    def _check(response, action: str) -> None:
        status = int(response.status_code)
        if 200 <= status < 300:
            return
        if status in (401, 403):
            raise WebDAVError(
                f"{action} was rejected by the server ({status}): "
                "check the username and password, and that the account may write here.")
        if status == 404:
            raise WebDAVError(f"{action} failed: remote path not found (404).")
        if status == 405:
            raise WebDAVError(f"{action} failed: method not allowed (405) for this path.")
        if status == 507:
            raise WebDAVError(f"{action} failed: the server reports no free space (507).")
        detail = ""
        try:
            detail = (response.text or "").strip().replace("\n", " ")[:200]
        except Exception:               # noqa: BLE001 - 仅用于补充说明
            detail = ""
        raise WebDAVError(f"{action} failed with HTTP {status}" + (f": {detail}" if detail else "."))

    # ---------- 目录与清单 ----------
    def ensure_dir(self) -> None:
        """逐段创建远端目录；已存在时静默通过。"""
        url = self._base
        for seg in _remote_segments(self.profile):
            url += "/" + quote(seg, safe="")
            response = self._request("PROPFIND", url + "/", data=_PROPFIND_FILE_BODY.encode("utf-8"),
                                     headers={"Depth": "0",
                                              "Content-Type": "application/xml; charset=utf-8"})
            if response.status_code in (200, 207):
                continue
            if response.status_code != 404:
                self._check(response, f"Checking remote folder '{url}'")
            created = self._request("MKCOL", url + "/")
            # 405 = 该集合已存在（部分服务端对 MKCOL 的既有目录返回 405 而非 409）
            if created.status_code not in (200, 201, 204, 405):
                self._check(created, f"Creating remote folder '{url}'")
        logger.info("WebDAV folder ready: %s", _dir_url(self.profile))

    def list_backups(self) -> List[Dict[str, Any]]:
        """列出远端目录下的备份文件，按文件名倒序（时间戳前缀即时间顺序）。"""
        response = self._request(
            "PROPFIND", _dir_url(self.profile),
            data=_PROPFIND_FILE_BODY.encode("utf-8"),
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        self._check(response, "Listing remote backups")

        files: List[Dict[str, Any]] = []
        for entry in _parse_propfind(response.content):
            name = entry.get("name") or ""
            if entry.get("is_dir") or not is_backup_name(name):
                continue
            files.append({
                "name": name,
                "size": int(entry.get("size") or 0),
                "modified": str(entry.get("modified") or ""),
            })
        files.sort(key=lambda item: item["name"], reverse=True)
        return files

    # ---------- 传输 ----------
    def upload_bytes(self, name: str, data: bytes) -> None:
        if not is_backup_name(name):
            raise WebDAVError(f"Refusing to upload a file outside the backup name pattern: {name}")
        response = self._request(
            "PUT", _file_url(self.profile, name), data=data,
            headers={"Content-Type": "application/json; charset=utf-8"})
        self._check(response, f"Uploading '{name}'")

    def download_bytes(self, name: str) -> bytes:
        if not is_backup_name(name):
            raise WebDAVError(f"Refusing to download a file outside the backup name pattern: {name}")
        response = self._request("GET", _file_url(self.profile, name))
        self._check(response, f"Downloading '{name}'")
        return response.content

    def test_connection(self) -> Dict[str, Any]:
        """连通性自检：建目录 + 列清单，返回远端目录与备份数量。"""
        self.ensure_dir()
        files = self.list_backups()
        return {"remote_dir": _dir_url(self.profile), "count": len(files)}


def _parse_propfind(xml_bytes: bytes) -> List[Dict[str, Any]]:
    """解析 PROPFIND 的 207 响应。

    用标准库 :mod:`xml.etree.ElementTree`：它不解析外部实体，配置里也不该出现
    DTD，因此这里不存在 XXE 路径。解析失败按"清单为空"处理并记录，而不是让
    整个设置页报错。
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.warning("Cannot parse the WebDAV PROPFIND response: %s", e)
        return []

    entries: List[Dict[str, Any]] = []
    for node in root.findall(f"{_DAV_NS}response"):
        href = (node.findtext(f"{_DAV_NS}href") or "").strip()
        if not href:
            continue
        prop = node.find(f"{_DAV_NS}propstat/{_DAV_NS}prop")
        if prop is None:
            continue
        is_dir = prop.find(f"{_DAV_NS}resourcetype/{_DAV_NS}collection") is not None
        size_text = (prop.findtext(f"{_DAV_NS}getcontentlength") or "").strip()
        entries.append({
            "name": unquote(href.rstrip("/").rsplit("/", 1)[-1]),
            "is_dir": is_dir,
            "size": int(size_text) if size_text.isdigit() else 0,
            "modified": (prop.findtext(f"{_DAV_NS}getlastmodified") or "").strip(),
        })
    return entries
