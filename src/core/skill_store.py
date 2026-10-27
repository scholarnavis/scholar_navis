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
import re
import shutil

from src.core import safe_io
from src.core.encryption_service import SystemEncryptionService

logger = logging.getLogger(__name__)

#: 现行技能源码后缀：明文 Python
PLAIN_SUFFIX = ".py"
#: LEGACY-CIPHERTEXT：历史技能源码后缀，机器绑定密钥的 Fernet 密文（只读兼容）
LEGACY_SUFFIX = ".enc"

#: 技能名长度上限与字符白名单
_SKILL_NAME_MAX = 64
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_\-][A-Za-z0-9_\-.]*$")


def validate_skill_name(name: str) -> str:
    """校验技能名并返回去掉首尾空白后的结果；不合法时抛 ``ValueError``。

    技能名来自设置页的文本输入，随后被直接拼进文件路径。不加限制时，
    一个名为 ``../../x`` 的技能可以写到技能目录之外并覆盖任意 ``.py``。
    """
    text = str(name or "").strip()
    if not text:
        raise ValueError("技能名不能为空")
    if len(text) > _SKILL_NAME_MAX:
        raise ValueError(f"技能名过长（上限 {_SKILL_NAME_MAX} 字符）")
    if not _SKILL_NAME_RE.match(text):
        raise ValueError(f"技能名含非法字符：{text!r}")
    return text


def skill_path(base_dir: str, name: str) -> str:
    """技能源码 ``<base_dir>/<name>.py`` 的路径；名字不合法时抛 ``ValueError``。"""
    target = os.path.join(base_dir, f"{validate_skill_name(name)}{PLAIN_SUFFIX}")
    if not safe_io.ensure_within(base_dir, target):
        raise ValueError(f"技能路径越界：{name!r}")
    return target


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
    target = skill_path(base_dir, name)
    os.makedirs(base_dir, exist_ok=True)
    safe_io.atomic_write_text(target, code or "", mode=0o600)
    return target


def delete_skill_source(base_dir: str, name: str) -> None:
    """删除技能源码与其工作区目录。

    删除比写入更危险：配置里的 ``command`` 字段可被手工改写，直接拿它去
    ``os.remove`` 等于提供任意文件删除能力。因此本函数只接受技能名，
    路径由 :func:`skill_path` 重新构造并校验是否仍在技能目录内。
    """
    try:
        target = skill_path(base_dir, name)
    except ValueError as e:
        logger.warning(f"Refusing to delete skill with invalid name {name!r}: {e}")
        return

    for candidate in (target, os.path.join(base_dir, f"{str(name).strip()}{LEGACY_SUFFIX}")):
        try:
            os.remove(candidate)
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.warning(f"Cannot remove skill file '{candidate}': {e}")

    workspace = os.path.join(base_dir, f"{str(name).strip()}_workspace")
    if os.path.isdir(workspace) and safe_io.ensure_within(base_dir, workspace):
        shutil.rmtree(workspace, ignore_errors=True)
