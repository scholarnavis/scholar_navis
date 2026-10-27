"""配置分级存储：密钥集中到 ``config/secrets.json``，其余配置为可核查的明文 JSON。

为什么这么分（而不是整份配置加密）：

* 旧实现用"机器绑定密钥"加密 ``settings.json`` / ``llm_config.json`` /
  ``mcp_servers.json``，但托管该密钥的 ``config/.secret_fallback.key``（0o600）
  就在同目录——能读密文者必能读密钥，机密性约等于零；
* 代价却是真实的：换机标识（重装系统、容器镜像重建、克隆虚拟机）后密钥还在而
  密文全部解不开；配置文件也不是文本，无法人工核查、diff、git 或手改。

于是改为：**普通配置明文、密钥单独一个文件**。本模块是"哪些字段算密钥"的
唯一事实来源，``ConfigManager`` 只负责调用它。

内存结构约定：``user_settings`` / ``llm_configs`` / ``mcp_servers`` 在进程内
**保持原样**（照旧含 ``ncbi_api_key``、``api_key``、``env`` 里的 token），
拆分只发生在落盘与读取的边界上，因此所有既有读取方无需改动。
"""

from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from src.core import safe_io

logger = logging.getLogger(__name__)

#: ``secrets.json`` 只允许属主读写
_SECRETS_MODE = 0o600

#: 密钥文件名（与 ``settings.json`` 同级）
SECRETS_FILENAME = "secrets.json"

#: ``settings.json`` 中需要搬到 ``secrets.json`` 的键（与默认配置里的密钥字段一一对应）
SECRET_SETTING_KEYS = (
    "ncbi_api_key",
    "openalex_api_key",
    "s2_api_key",
    "github_token",
    "hf_token",
    "api_server_key",
)

#: LLM provider 配置中需要搬走的字段（按 provider ``id`` 归位）
SECRET_LLM_FIELDS = ("api_key",)

#: MCP / 技能条目里"字段名含这些词"的字符串值视为密钥（用于 ``env`` / ``headers``）
SECRET_NAME_HINTS = (
    "key", "token", "secret", "password", "passwd", "credential", "auth",
)

#: ``secrets.json`` 的三个分区（与三份配置一一对应）
_EMPTY_BUNDLE: Dict[str, Dict[str, Any]] = {"settings": {}, "llm": {}, "mcp": {}}


def secrets_path(config_dir: str) -> str:
    """``config/secrets.json`` 的绝对路径。"""
    return os.path.join(config_dir, SECRETS_FILENAME)


def is_secret_name(name: Any) -> bool:
    """字段名是否像密钥（``OPENAI_API_KEY`` / ``Authorization`` / ``gh_token`` …）。"""
    low = str(name or "").lower()
    return any(hint in low for hint in SECRET_NAME_HINTS)


def _split_mapping(mapping: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """把映射里"名字像密钥"的**字符串**字段分流成 ``(公开, 密钥)``。

    空值的密钥字段两边都不保留：它既不构成有效凭据，也不应继续以明文留在
    ``mcp_servers.json`` 里。用户清空某个 token 时，下一次保存即彻底消失。
    """
    public: Dict[str, Any] = {}
    secret: Dict[str, Any] = {}
    for key, value in (mapping or {}).items():
        if isinstance(value, str) and is_secret_name(key):
            if value.strip():
                secret[key] = value
            continue
        public[key] = value
    return public, secret


# --------------------------------------------------------------------------- #
#  settings.json
# --------------------------------------------------------------------------- #
def split_settings(settings: Optional[dict]) -> Tuple[dict, dict]:
    """拆出 ``settings.json`` 里的密钥字段，返回 ``(公开配置, 密钥)``。"""
    public = dict(settings or {})
    secret: Dict[str, Any] = {}
    for key in SECRET_SETTING_KEYS:
        if key in public:
            value = public.pop(key)
            if value not in (None, ""):
                secret[key] = value
    return public, secret


def merge_settings(settings: Optional[dict], secret: Optional[dict]) -> dict:
    """把密钥合回 ``settings``（键位齐全，缺失即为空串，读取方行为不变）。

    密钥文件里没有的键一律置空串，而不是保留公开侧的残留值：否则用户在界面
    清空密钥后，旧值仍会从明文残留里被注回，表现为"删不掉、换不掉"。
    """
    merged = dict(settings or {})
    secret = secret or {}
    for key in SECRET_SETTING_KEYS:
        value = secret.get(key)
        merged[key] = value if value not in (None, "") else ""
    return merged


# --------------------------------------------------------------------------- #
#  llm_config.json
# --------------------------------------------------------------------------- #
def _provider_id(entry: dict) -> str:
    return str(entry.get("id") or entry.get("name") or "")


def split_llm(configs: Optional[list]) -> Tuple[list, dict]:
    """拆出各 provider 的 ``api_key``，返回 ``(公开列表, {provider_id: {...}})``。"""
    public_list: List[dict] = []
    secret: Dict[str, Any] = {}
    for raw in configs or []:
        entry = dict(raw or {})
        pid = _provider_id(entry)
        for field in SECRET_LLM_FIELDS:
            if field not in entry:
                continue
            value = entry.pop(field)
            if value not in (None, ""):
                secret.setdefault(pid, {})[field] = value
        public_list.append(entry)
    return public_list, secret


def merge_llm(configs: Optional[list], secret: Optional[dict]) -> list:
    """把 ``api_key`` 合回各 provider（键位齐全，缺失即为空串）。

    与 :func:`merge_settings` 同口径：密钥文件里没有就置空，不回退到明文残留。
    """
    secret = secret or {}
    merged: List[dict] = []
    for raw in configs or []:
        entry = dict(raw or {})
        pid = _provider_id(entry)
        for field in SECRET_LLM_FIELDS:
            value = (secret.get(pid) or {}).get(field)
            entry[field] = value if value not in (None, "") else ""
        merged.append(entry)
    return merged


# --------------------------------------------------------------------------- #
#  mcp_servers.json
# --------------------------------------------------------------------------- #
def split_mcp(servers: Optional[dict]) -> Tuple[dict, dict]:
    """拆出 MCP 服务器 / 外部技能 ``env`` 与 ``headers`` 里的密钥。"""
    public = copy.deepcopy(servers or {})
    secret: Dict[str, Any] = {}
    for group in ("mcpServers", "external_skills"):
        for name, entry in (public.get(group) or {}).items():
            if not isinstance(entry, dict):
                continue
            for field in ("env", "headers"):
                mapping = entry.get(field)
                if not isinstance(mapping, dict):
                    continue
                pub_map, sec_map = _split_mapping(mapping)
                if sec_map:
                    entry[field] = pub_map
                    secret.setdefault(group, {}).setdefault(name, {})[field] = sec_map
    return public, secret


def merge_mcp(servers: Optional[dict], secret: Optional[dict]) -> dict:
    """把 ``env`` / ``headers`` 里的密钥合回对应条目。"""
    merged = copy.deepcopy(servers or {})
    for group, by_name in (secret or {}).items():
        for name, fields in (by_name or {}).items():
            entry = ((merged.get(group) or {}).get(name))
            if not isinstance(entry, dict):
                continue
            for field, mapping in (fields or {}).items():
                if not isinstance(mapping, dict):
                    continue
                current = entry.get(field)
                merged_map = dict(current) if isinstance(current, dict) else {}
                merged_map.update(mapping)
                entry[field] = merged_map
    return merged


# --------------------------------------------------------------------------- #
#  文件读写
# --------------------------------------------------------------------------- #
def load(config_dir: str) -> Dict[str, Dict[str, Any]]:
    """读取 ``secrets.json``；缺失或损坏时返回空结构（绝不因它中断启动）。

    损坏时先把原文件移到 ``corrupt_<时间戳>_secrets.json`` 再返回空结构：
    否则下一次保存会用空内容覆盖，用户的全部 API key 就此永久消失。
    """
    path = secrets_path(config_dir)
    bundle = copy.deepcopy(_EMPTY_BUNDLE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return bundle
    except (OSError, ValueError) as e:
        logger.error(f"Cannot read secrets file '{path}': {e}")
        backup = safe_io.quarantine_corrupt(path)
        if backup:
            logger.error(f"Secrets file preserved at '{backup}'; starting with empty bundle.")
        return bundle

    if not isinstance(data, dict):
        logger.error(f"Secrets file '{path}' is not an object; ignoring it.")
        safe_io.quarantine_corrupt(path)
        return bundle
    for key in bundle:
        section = data.get(key)
        if isinstance(section, dict):
            bundle[key] = section
    return bundle


def save(config_dir: str, bundle: Dict[str, Any]) -> bool:
    """原子写入 ``secrets.json``（0o600）。

    返回 ``False`` 仅用于兼容既有调用方；新代码应当检查返回值并向上报错，
    否则磁盘满或权限不足时会表现为"保存成功、重启后密钥消失"。

    Windows 上 ``chmod`` 基本无效（无 POSIX 权限位），按用户级目录隔离即可；
    Linux / macOS 上 0o600 是这里唯一真正的访问控制，务必保留。
    """
    path = secrets_path(config_dir)
    try:
        safe_io.atomic_write_json(path, bundle or {}, mode=_SECRETS_MODE)
        return True
    except (OSError, TypeError, ValueError) as e:
        logger.error(f"Failed to write secrets file '{path}': {e}")
        return False
