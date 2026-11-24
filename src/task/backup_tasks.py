"""WebDAV 备份相关的后台任务：列清单、上传、下载恢复。

上传任务刻意继承 :class:`src.task.config_task.ExportConfigTask`：序列化与
PBKDF2 加密沿用本地导出的唯一实现，这里只补一次 PUT。因此"本地 .json 备份"
与"WebDAV 上的备份"在字节层面同构，恢复时可以交给同一条导入链路处理。
"""

from __future__ import annotations

import json
import logging

from src.core.core_task import BackgroundTask
from src.core.safe_io import atomic_write_json
from src.core.webdav_backup import (
    RESTORE_STAGING_MODE,
    WebDAVClient,
    WebDAVError,
    backup_file_name,
    restore_staging_path,
)
from src.task.config_task import ExportConfigTask

logger = logging.getLogger("BackupTask")


class ListWebDAVBackupsTask(BackgroundTask):
    """连接备份源并列出远端备份文件。"""

    def _execute(self) -> dict:
        profile = self.kwargs.get("profile") or {}
        self.update_progress(15, "Connecting to the WebDAV server...")
        try:
            with WebDAVClient(profile) as client:
                client.ensure_dir()
                self.update_progress(65, "Listing remote backups...")
                files = client.list_backups()
        except WebDAVError as e:
            logger.warning("Listing WebDAV backups failed: %s", e)
            return {"success": False, "msg": str(e)}
        except Exception as e:          # noqa: BLE001 - 兜底，避免任务线程静默死掉
            logger.exception("Unexpected error while listing WebDAV backups")
            return {"success": False, "msg": f"Unexpected error: {e}"}

        self.update_progress(100, f"{len(files)} backup(s) found")
        return {"success": True, "files": files}


class UploadWebDAVBackupTask(ExportConfigTask):
    """序列化（含可选口令加密）后上传到 WebDAV。"""

    def _execute(self) -> dict:
        serialized = super()._execute()
        if not serialized.get("success"):
            return serialized

        profile = self.kwargs.get("profile") or {}
        name = backup_file_name()
        if self.is_cancelled():
            raise InterruptedError("Backup upload cancelled by user.")

        try:
            payload = json.dumps(serialized, indent=4, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as e:
            return {"success": False, "msg": f"Serialization failed: {e}"}

        self.update_progress(80, "Uploading backup to the WebDAV server...")
        try:
            with WebDAVClient(profile) as client:
                client.ensure_dir()
                client.upload_bytes(name, payload)
        except WebDAVError as e:
            logger.warning("Uploading WebDAV backup failed: %s", e)
            return {"success": False, "msg": str(e)}
        except Exception as e:          # noqa: BLE001
            logger.exception("Unexpected error while uploading a WebDAV backup")
            return {"success": False, "msg": f"Unexpected error: {e}"}

        self.update_progress(100, "Backup uploaded")
        return {"success": True, "file": name, "mode": serialized.get("mode", "plain")}


class RestoreWebDAVBackupTask(BackgroundTask):
    """下载远端备份到本地暂存文件，交由既有导入流程处理。"""

    def _execute(self) -> dict:
        profile = self.kwargs.get("profile") or {}
        name = str(self.kwargs.get("name") or "")

        self.update_progress(20, "Downloading the selected backup...")
        try:
            with WebDAVClient(profile) as client:
                raw = client.download_bytes(name)
        except WebDAVError as e:
            logger.warning("Downloading WebDAV backup failed: %s", e)
            return {"success": False, "msg": str(e)}
        except Exception as e:          # noqa: BLE001
            logger.exception("Unexpected error while downloading a WebDAV backup")
            return {"success": False, "msg": f"Unexpected error: {e}"}

        if self.is_cancelled():
            raise InterruptedError("Backup download cancelled by user.")

        self.update_progress(70, "Validating the downloaded bundle...")
        try:
            bundle = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            return {"success": False, "msg": f"Not a valid JSON bundle: {e}"}
        if not isinstance(bundle, dict):
            return {"success": False, "msg": "Invalid configuration file structure."}

        # 走 safe_io 的统一落盘口径，同时给导入流程一个稳定的本地路径
        path = restore_staging_path()
        try:
            atomic_write_json(path, bundle, mode=RESTORE_STAGING_MODE)
        except OSError as e:
            return {"success": False, "msg": f"Failed to stage the backup locally: {e}"}

        self.update_progress(100, "Backup downloaded")
        return {"success": True, "path": path, "file": name}
