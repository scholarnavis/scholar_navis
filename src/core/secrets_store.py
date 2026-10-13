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
import tempfile
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

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
    """把映射里"名字像密钥"的**字符串**字段分流成 ``(公开, 密钥)``。"""
    public: Dict[str, Any] = {}
    secret: Dict[str, Any] = {}
    for key, value in (mapping or {}).items():
        if isinstance(value, str) and is_secret_name(key) and value.strip():
            secret[key] = value
        else:
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
    """把密钥合回 ``settings``（键位齐全，缺失即为空串，读取方行为不变）。"""
    merged = dict(settings or {})
    secret = secret or {}
    for key in SECRET_SETTING_KEYS:
        value = secret.get(key)
        if value not in (None, ""):
            merged[key] = value
        else:
            merged.setdefault(key, "")
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
    """把 ``api_key`` 合回各 provider（键位齐全，缺失即为空串）。"""
    secret = secret or {}
    merged: List[dict] = []
    for raw in configs or []:
        entry = dict(raw or {})
        pid = _provider_id(entry)
        for field in SECRET_LLM_FIELDS:
            value = (secret.get(pid) or {}).get(field)
            entry[field] = value if value not in (None, "") else str(entry.get(field) or "")
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
    """读取 ``secrets.json``；缺失或损坏时返回空结构（绝不因它中断启动）。"""
    path = secrets_path(config_dir)
    bundle = copy.deepcopy(_EMPTY_BUNDLE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return bundle
    except (OSError, ValueError) as e:
        logger.error(f"Cannot read secrets file '{path}': {e}")
        return bundle

    if not isinstance(data, dict):
        logger.error(f"Secrets file '{path}' is not an object; ignoring it.")
        return bundle
    for key in bundle:
        section = data.get(key)
        if isinstance(section, dict):
            bundle[key] = section
    return bundle


def save(config_dir: str, bundle: Dict[str, Any]) -> bool:
    """原子写入 ``secrets.json``（0o600）。失败只记日志，不打断调用方主流程。

    Windows 上 ``chmod`` 基本无效（无 POSIX 权限位），按用户级目录隔离即可；
    Linux / macOS 上 0o600 是这里唯一真正的访问控制，务必保留。
    """
    os.makedirs(config_dir, exist_ok=True)
    path = secrets_path(config_dir)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=config_dir, prefix=".secrets_", text=False)
        os.close(fd)
    except OSError as e:
        logger.error(f"Cannot create temp file for secrets in '{config_dir}': {e}")
        return False
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(bundle or {}, f, indent=4, ensure_ascii=False)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        return True
    except OSError as e:
        logger.error(f"Failed to write secrets file '{path}': {e}")
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False
