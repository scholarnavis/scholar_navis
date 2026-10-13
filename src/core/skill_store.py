"""外部技能源码的读写口径（**唯一事实来源**）：明文 ``.py``，兼容旧的 ``.enc`` 密文。

为什么改明文：旧实现用"机器绑定密钥"加密技能源码，而托管密钥的文件（
``config/.secret_fallback.key``，0o600）与密文同目录、同权限层级——机密性约等于零；
代价却是换机标识后源码全部读不出，且无法人工核查、编辑、diff。技能代码里可能内嵌
用户自己的 token，因此明文文件按 0o600 落盘（Windows 上 chmod 基本无效，靠用户级
目录隔离，与密钥文件同一策略）。

历史 ``.enc`` 文件保持**可读**（走 :class:`SystemEncryptionService` 的历史派生），
迁移由 ``ConfigManager._migrate_legacy_skill_files`` 负责，本模块只提供读写。

LEGACY-CIPHERTEXT：``.enc`` 读取分支与 :data:`LEGACY_SUFFIX` 属历史兼容层，最早
v2.2 删除；写入侧永远只产出明文。删除计划见 ``src/core/encryption_service.py`` 顶部。
"""

from __future__ import annotations

import logging
import os
import tempfile

from src.core.encryption_service import SystemEncryptionService

logger = logging.getLogger(__name__)

#: 现行技能源码后缀：明文 Python
PLAIN_SUFFIX = ".py"
#: LEGACY-CIPHERTEXT：历史技能源码后缀，机器绑定密钥的 Fernet 密文（只读兼容）
LEGACY_SUFFIX = ".enc"


def read_skill_source(path: str) -> str:
    """读取技能源码：``.enc`` 走历史解密，其余后缀按 UTF-8 明文读取。"""
    if str(path).endswith(LEGACY_SUFFIX):
        # LEGACY-CIPHERTEXT：只读分支，最早 v2.2 删除
        service = SystemEncryptionService()
        with open(path, "rb") as f:
            return service.decrypt(f.read())
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_skill_source(base_dir: str, name: str, code: str) -> str:
    """把技能源码以明文原子写入 ``<base_dir>/<name>.py``（0o600），返回实际路径。"""
    os.makedirs(base_dir, exist_ok=True)
    target = os.path.join(base_dir, f"{name}{PLAIN_SUFFIX}")
    fd, tmp_path = tempfile.mkstemp(dir=base_dir, prefix=".skill_", text=False)
    os.close(fd)
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(code or "")
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass    # Windows 无 POSIX 权限位，忽略即可
        os.replace(tmp_path, target)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return target
