import json
import os
import sys
import time
import shutil
import logging
import threading
import tempfile
from cryptography.fernet import InvalidToken

from src.core import BASE_DIR, secrets_store
from src.core.encryption_service import SystemEncryptionService

# 当前配置导出版本号，用于未来兼容性检查
CONFIG_VERSION = "1.0"


class ConfigManager:
    _instance = None
    _lock = threading.RLock()
    # 由 load_settings/load_mcp_servers 填充，此处仅作类型声明
    user_settings: dict
    mcp_servers: dict

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(ConfigManager, cls).__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.logger = logging.getLogger("ConfigManager")


        self.BASE_DIR = BASE_DIR
        self.CONFIG_DIR = os.path.join(BASE_DIR, "config")
        self.SETTINGS_PATH = os.path.join(self.CONFIG_DIR, "settings.json")
        self.MCP_SERVERS_PATH = os.path.join(self.CONFIG_DIR, "mcp_servers.json")
        self.LLM_CONFIG_PATH = os.path.join(self.CONFIG_DIR, "llm_config.json")
        self.EXTERNAL_MODELS_PATH = os.path.join(self.CONFIG_DIR, "external_models.json")

        # 确保配置目录存在
        os.makedirs(self.CONFIG_DIR, exist_ok=True)

        # 2. LEGACY-CIPHERTEXT：解密套件只为"读历史密文"与"口令型导出"存在。
        #    配置本体一律明文，因此 keyring 不可用不再阻断启动。
        self.encryption_service = SystemEncryptionService(service_name="ScholarNavis")
        try:
            self.encryption_service._get_master_key()
        except Exception as e:
            self.logger.warning(
                f"Master key unavailable; legacy ciphertexts stay unreadable: {e}")

        # 3. LEGACY-CIPHERTEXT：一次性迁移，旧机器绑定密文 → 明文配置 + secrets.json
        self._migrate_legacy_encrypted_config()

        # 4. 加载核心配置
        self.load_settings()
        self.load_mcp_servers()
        # 5. LEGACY-CIPHERTEXT：外部技能源码 .enc 密文 → .py 明文
        self._migrate_legacy_skill_files()

        self._initialized = True

    # ------------------------------------------------------------------ #
    #  密钥文件 secrets.json（分级存储的读写入口）
    # ------------------------------------------------------------------ #
    def _secrets(self) -> dict:
        """当前 ``secrets.json`` 内容（进程内缓存，写入后同步更新）。"""
        cache = getattr(self, "_secrets_cache", None)
        if cache is None:
            cache = secrets_store.load(self.CONFIG_DIR)
            self._secrets_cache = cache
        return cache

    def _merge_secrets(self, section: str, values: dict):
        """把某分区的密钥写入 ``secrets.json``（空值不覆盖已有密钥）。

        分层合并：``settings`` 是扁平键值，``llm`` / ``mcp`` 是"标识 → 字段"两级，
        用一层嵌套合并即可覆盖两者，避免写两套逻辑。
        """
        bundle = self._secrets()
        current = bundle.setdefault(section, {})
        for key, value in (values or {}).items():
            if isinstance(value, dict) and isinstance(current.get(key), dict):
                current[key].update(value)
            elif value not in (None, ""):
                current[key] = value
        secrets_store.save(self.CONFIG_DIR, bundle)

    # ------------------------------------------------------------------ #
    #  LEGACY-CIPHERTEXT 兼容层：读旧密文配置并迁移为明文
    #  （_read_legacy_encrypted_json / _backup_file / _migrate_legacy_encrypted_config /
    #   _migrate_legacy_skill_files 四者只服务于历史数据，最早 v2.2 删除；
    #   见 src/core/encryption_service.py 模块顶部的删除计划）
    # ------------------------------------------------------------------ #
    def _read_legacy_encrypted_json(self, path: str):
        """旧机器绑定密文 → dict；已是明文 JSON 或解不开时返回 ``None``。"""
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)
            return None                     # 合法 JSON：本来就是明文，无需迁移
        except (OSError, ValueError):
            pass
        try:
            with open(path, "rb") as f:
                blob = f.read()
            plain, used_legacy = self.encryption_service.decrypt_with_mode(blob)
            return json.loads(plain)
        except Exception as e:
            self.logger.warning(f"Legacy ciphertext '{path}' cannot be decrypted: {e}")
            return None

    def _backup_file(self, path: str, backup_dir: str) -> bool:
        """迁移前把原文件备份一份；备份失败返回 False（调用方据此放弃迁移该文件）。"""
        try:
            os.makedirs(backup_dir, exist_ok=True)
            shutil.copy2(path, os.path.join(backup_dir, os.path.basename(path)))
            return True
        except OSError as e:
            self.logger.error(f"Backup of '{path}' failed, keeping it as is: {e}")
            return False

    def _migrate_legacy_encrypted_config(self):
        """一次性迁移：旧 Fernet 密文配置 → 明文配置 + ``secrets.json``。

        判定依据是"文件不是合法 JSON"：明文一定可解析，Fernet 密文不是 UTF-8 JSON。
        迁移前把原文件整体备份到 ``config/backup_<ts>/``；备份失败或解密失败的文件
        保持原样（旧版代码仍能读），绝不半途改写。
        """
        targets = (
            (self.SETTINGS_PATH, "settings"),
            (self.LLM_CONFIG_PATH, "llm"),
            (self.MCP_SERVERS_PATH, "mcp"),
        )
        legacy = {path: self._read_legacy_encrypted_json(path) for path, _ in targets}
        if not any(data is not None for data in legacy.values()):
            return

        backup_dir = os.path.join(self.CONFIG_DIR, f"backup_{time.strftime('%Y%m%d_%H%M%S')}")
        bundle = secrets_store.load(self.CONFIG_DIR)
        migrated = []
        for path, kind in targets:
            data = legacy[path]
            if data is None or not self._backup_file(path, backup_dir):
                continue
            if kind == "settings":
                public, secret = secrets_store.split_settings(data)
                bundle["settings"].update(secret)
            elif kind == "llm":
                public, secret = secrets_store.split_llm(data)
                for pid, fields in secret.items():
                    bundle["llm"].setdefault(pid, {}).update(fields)
            else:
                public, secret = secrets_store.split_mcp(data)
                for group, by_name in secret.items():
                    section = bundle["mcp"].setdefault(group, {})
                    for name, fields in by_name.items():
                        section.setdefault(name, {}).update(fields)
            try:
                self.save_json(path, public)
            except Exception as e:
                self.logger.error(f"Failed to write plaintext '{path}': {e}")
                continue
            migrated.append(os.path.basename(path))

        if migrated:
            secrets_store.save(self.CONFIG_DIR, bundle)
            self._secrets_cache = bundle
            self.logger.info(
                f"Migrated legacy encrypted config to plaintext: {', '.join(migrated)}; "
                f"originals backed up to '{backup_dir}'")

    # LEGACY-CIPHERTEXT：技能源码密文迁移，随兼容层一起删除（见 _read_legacy_encrypted_json 上方）
    def _migrate_legacy_skill_files(self):
        """外部技能源码 ``.enc`` 密文 → 同目录 ``.py`` 明文（旧文件保留不动）。

        技能代码常内嵌用户自己的 token，故按 0o600 落盘。迁移失败即保持原状：
        ``skill_manager`` 仍能读旧密文，不影响使用。
        """
        skills = (self.mcp_servers or {}).get("external_skills") or {}
        changed = False
        for name, cfg in skills.items():
            if not isinstance(cfg, dict):
                continue
            old_path = str(cfg.get("command") or "")
            if not old_path.endswith(".enc") or not os.path.exists(old_path):
                continue
            new_path = os.path.splitext(old_path)[0] + ".py"
            try:
                with open(old_path, "rb") as f:
                    code = self.encryption_service.decrypt(f.read())
                fd, tmp_path = tempfile.mkstemp(
                    dir=os.path.dirname(new_path) or ".", prefix=".skill_", text=False)
                os.close(fd)
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(code)
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, new_path)
            except Exception as e:
                self.logger.warning(f"Skill '{name}' stays encrypted ('{old_path}'): {e}")
                continue
            cfg["command"] = new_path
            changed = True
            self.logger.info(f"Skill '{name}' source migrated to plaintext: {new_path}")
        if changed:
            self.save_mcp_servers()

    def save_json(self, path, data):
        """原子性写入明文 JSON 配置。

        配置本体一律明文：托管密钥的 ``config/.secret_fallback.key`` 就在同目录、同为
        0o600，能读密文者必能读密钥，加密只带来"换机即不可读"的代价。敏感字段的隔离
        由 :mod:`src.core.secrets_store` 负责（单独落 ``secrets.json``）。临时文件由
        ``mkstemp`` 创建，天然是 0o600，原子替换后权限保持。
        """
        with self._lock:
            temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(path), text=False)
            os.close(temp_fd)
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    f.write(json.dumps(data, indent=4, ensure_ascii=False))

                for attempt in range(3):
                    try:
                        os.replace(temp_path, path)
                        break
                    except PermissionError:
                        if attempt < 2:
                            time.sleep(0.1)
                        else:
                            raise

            except Exception as e:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception as rm_e:
                        self.logger.error(f"Failed to remove temp file {temp_path}: {rm_e}")
                self.logger.error(f"Failed to save config at {path}: {e}")
                raise


    def load_json(self, path):
        """读取明文 JSON 配置；若是历史机器绑定密文，解密一次并就地改写为明文。

        解密回退只服务于**读旧数据**（旧版把 ``settings.json`` 等整体加密过），
        改造后不再有任何写入侧产生密文，因此这里只读不写密文。
        """
        if not os.path.exists(path):
            return None
        with self._lock:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (OSError, ValueError) as e:
                # LEGACY-CIPHERTEXT：只读旧密文并就地改写为明文，见本类 _read_legacy_encrypted_json 上方的删除计划
                self.logger.warning(
                    f"'{path}' is not plain JSON, trying legacy decryption: {e}")
                try:
                    with open(path, 'rb') as f:
                        plain_data = json.loads(self.encryption_service.decrypt(f.read()))
                    self.save_json(path, plain_data)
                    self.logger.info(f"Migrated legacy encrypted config to plaintext: {path}")
                    return plain_data
                except (OSError, ValueError, InvalidToken, RuntimeError) as legacy_err:
                    self.logger.error(
                        f"Cannot read '{path}' as plain JSON or legacy ciphertext: {legacy_err}")
                    return None


    def load_settings(self):
        default_settings = {
            "current_model_id": "embed_nano_fast",
            "rerank_model_id": "rerank_lite",
            "inference_device": "cpu",
            "proxy_mode": "off",
            "proxy_url": "",
            "hf_mirror": "",
            "hf_token": "",
            "theme": "auto",
            # 界面语言："auto" 跟随系统（既非简体中文也非英语时回退英文）；
            # 亦可显式设为 "en" / "zh_CN"。取值与解析规则见 src/core/i18n.py。
            "language": "auto",
            "log_level": "INFO",
            "is_first_run": True,
            "ncbi_email": "",
            "ncbi_api_key": "",
            "openalex_api_key": "",
            "s2_api_key": "",
            "s2_rate_limit": "1.0",
            "github_token": "",
            "quick_trans_markdown": True,
            "trans_source_lang": "Auto Detect",
            "trans_target_lang": "English",
            "quick_trans_model_name": "",
            "quick_trans_llm_id": "",
            "chat_llm_id": "",
            "chat_model_name": "",
            "chat_trans_llm_id": "",
            "chat_trans_model_name": "",
            "chat_use_academic_agent": True,
            "chat_use_external_tools": False,
            "agent_deep_mode": False,
            "chat_ribbon_state": "Pinned",
            # Rerank 精排参数：top_k 为精排后进入上下文的文档数；min_score 为分数阈值（0 关闭）
            "rerank_top_k": 5,
            "rerank_min_score": 0.25,
            "rerank_auto_load": True,
            "api_server_host": "127.0.0.1",
            "api_server_port": 8000,
            "api_server_key": "",
            # R runtime path used by the visualization engine (empty = auto-detect)
            "r_path": "",
            # Internal tools disabled by default in the academic agent
            # (edit this list to re-enable any of them).
            "deselected_academic_skills": [
                "search_web",
                "fetch_webpage_content",
                "fetch_wikipedia_summary",
                "search_gbif_occurrences",
                "search_chembl_target",
                "fetch_taxonomy_info",
            ],
        }

        # 明文读取；密钥单独存在 secrets.json，此处合回内存结构（读取方无需改动）
        current_settings = self.load_json(self.SETTINGS_PATH)
        if current_settings is not None:
            current_settings = secrets_store.merge_settings(
                current_settings, self._secrets().get("settings"))

        is_modified = False
        if current_settings is None:
            current_settings = default_settings.copy()
            is_modified = True
        else:
            for key, default_val in default_settings.items():
                if key not in current_settings:
                    current_settings[key] = default_val
                    is_modified = True

                # 数据清洗
            keys_to_purge = [
                "quick_trans_is_pinned",
            ]
            for k in keys_to_purge:
                if k in current_settings:
                    del current_settings[k]
                    is_modified = True

        self.user_settings = current_settings
        if is_modified:
            self.save_settings()
        self.apply_env_vars()

    def save_settings(self):
        public, secret = secrets_store.split_settings(self.user_settings)
        self.save_json(self.SETTINGS_PATH, public)
        self._merge_secrets("settings", secret)
        self.apply_env_vars()

    def load_llm_configs(self) -> list:
        default_config = [
            {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1", "model_name": "",
             "api_key": ""},
            {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model_name": "",
             "api_key": ""},
            {"id": "minimax", "name": "MiniMax", "base_url": "https://api.minimaxi.com/anthropic",
             "model_name": "MiniMax-M2.5", "api_key": "",
             "fetched_models": ["MiniMax-M2", "M2-her", "MiniMax-M2.1", "MiniMax-M2.1-lightning", "MiniMax-M2.5",
                                "MiniMax-M2.5-lightning", "MiniMax-M2.7"]},
            {"id": "gemini", "name": "Google Gemini",
             "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model_name": "", "api_key": ""},
            {"id": "anthropic", "name": "Anthropic", "base_url": "https://api.anthropic.com/v1", "model_name": "",
             "api_key": ""},
            {"id": "nvidia", "name": "Nvidia Build", "base_url": "https://integrate.api.nvidia.com/v1",
             "model_name": "", "api_key": ""},
            {"id": "qwen", "name": "Alibaba Qwen", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "model_name": "", "api_key": ""},
            {"id": "mimo", "name": "Xiaomi MiMo", "base_url": "https://api.xiaomimimo.com/v1",
             "model_name": "mimo-v2-pro", "api_key": "",
             "fetched_models": ["mimo-v1", "mimo-v2", "mimo-v2-pro"]},
            {"id": "zhipu", "name": "Zhipu GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model_name": "",
             "api_key": ""},
            {"id": "siliconflow", "name": "SiliconFlow", "base_url": "https://api.siliconflow.cn/v1", "model_name": "",
             "api_key": ""},
            {"id": "lmstudio", "name": "LM Studio", "base_url": "http://localhost:1234/v1", "model_name": "",
             "api_key": "lm-studio"},
            {"id": "ollma", "name": "Ollma", "base_url": "http://localhost:11434/v1", "model_name": "",
             "api_key": "ollama"}
        ]

        # 明文读取；各 provider 的 api_key 从 secrets.json 按 id 合回
        data = self.load_json(self.LLM_CONFIG_PATH)
        if data is not None:
            data = secrets_store.merge_llm(data, self._secrets().get("llm"))

        if data is None:
            loaded_configs = []
        elif isinstance(data, list):
            loaded_configs = data
        else:
            self.logger.warning("llm_config.json format unexpected, resetting to empty list.")
            loaded_configs = []

        # 处理旧版本配置兼容性迁移
        try:
            for cfg in loaded_configs:
                cfg.pop("thinking_model_name", None)
                if "model_params_mode" in cfg and "models_config" not in cfg:
                    m_name = cfg.get("model_name", "default")
                    cfg["models_config"] = {
                        m_name: {
                            "mode": cfg.get("model_params_mode", "inherit"),
                            "params": cfg.get("model_params", [])
                        }
                    }
        except Exception as e:
            self.logger.error(f"Error migrating LLM configurations: {e}")

        # 检查并补全缺失的默认配置
        existing_ids = {c.get("id") for c in loaded_configs}
        needs_resave = False
        missing_ids = []

        for i, dc in enumerate(default_config):
            if dc["id"] not in existing_ids:
                loaded_configs.insert(i, dc)
                missing_ids.append(dc["id"])
                needs_resave = True
            else:
                for cfg in loaded_configs:
                    if cfg.get("id") == dc["id"] and "fetched_models" in dc:
                        current_fetched = cfg.get("fetched_models", [])
                        added_any = False
                        for m in dc["fetched_models"]:
                            if m not in current_fetched:
                                current_fetched.append(m)
                                added_any = True
                        if added_any:
                            cfg["fetched_models"] = current_fetched
                            needs_resave = True
                        break

        if needs_resave:
            if missing_ids:
                self.logger.warning(
                    f"Required default LLM identifiers or models were missing: {missing_ids}. "
                    f"The system has automatically supplemented and persisted these records."
                )
            self.save_llm_configs(loaded_configs)

        return loaded_configs if loaded_configs else default_config

    def save_llm_configs(self, configs: list):
        public, secret = secrets_store.split_llm(configs)
        self.save_json(self.LLM_CONFIG_PATH, public)
        self._merge_secrets("llm", secret)


    def apply_env_vars(self):

        models_dir = os.path.join(self.BASE_DIR, "models")
        os.makedirs(models_dir, exist_ok=True)
        os.environ["HF_HOME"] = models_dir
        os.environ["SENTENCE_TRANSFORMERS_HOME"] = models_dir

        proxy = self.user_settings.get("proxy_url", "").strip()
        mirror = self.user_settings.get("hf_mirror", "").strip()
        token = self.user_settings.get("hf_token", "").strip()

        if mirror:
            os.environ["HF_ENDPOINT"] = mirror
        if token:
            os.environ["HF_TOKEN"] = token
        else:
            os.environ.pop("HF_TOKEN", None)

        if proxy:
            os.environ["HTTP_PROXY"] = proxy
            os.environ["HTTPS_PROXY"] = proxy
        else:
            os.environ.pop("HTTP_PROXY", None)
            os.environ.pop("HTTPS_PROXY", None)

        os.environ["NCBI_API_EMAIL"] = self.user_settings.get("ncbi_email", "")
        os.environ["NCBI_API_KEY"] = self.user_settings.get("ncbi_api_key", "")
        os.environ["OPENALEX_API_KEY"] = self.user_settings.get("openalex_api_key", "")
        os.environ["S2_API_KEY"] = self.user_settings.get("s2_api_key", "")
        os.environ["GITHUB_TOKEN"] = self.user_settings.get("github_token", "")


    def toggle_mcp_tag(self, tag: str, is_checked: bool):
        tags = self.mcp_servers.get("deselected_mcp_tags", [])
        if is_checked and tag in tags:
            tags.remove(tag)
        elif not is_checked and tag not in tags:
            tags.append(tag)
        else:
            return

        self.mcp_servers["deselected_mcp_tags"] = tags
        self.save_mcp_servers()


    def get_deselected_academic_skills(self) -> set:
        """Return the set of currently disabled internal academic tool names."""
        raw = self.user_settings.get("deselected_academic_skills", [])
        if not isinstance(raw, list):
            return set()
        return {str(s) for s in raw}


    def toggle_academic_skill(self, skill_name: str, enabled: bool):
        """Enable/disable an internal academic tool. enabled=True removes it from the disabled list."""
        deselected = self.user_settings.get("deselected_academic_skills", [])
        if not isinstance(deselected, list):
            deselected = []

        if enabled and skill_name in deselected:
            deselected.remove(skill_name)
        elif not enabled and skill_name not in deselected:
            deselected.append(skill_name)
        else:
            return

        self.user_settings["deselected_academic_skills"] = deselected
        self.save_settings()


    def get_r_path(self) -> str:
        """Return the user-configured R runtime path (empty when unset)."""
        raw = self.user_settings.get("r_path", "")
        return str(raw).strip() if raw else ""


    def set_r_path(self, path: str):
        """Persist the user-specified R runtime path."""
        self.user_settings["r_path"] = (path or "").strip()
        self.save_settings()


    def save_external_models(self, data):
        self.save_json(self.EXTERNAL_MODELS_PATH, data)

    def load_external_models_data(self):
        data = self.load_json(self.EXTERNAL_MODELS_PATH)
        return data if data else {"embedding": [], "reranker": []}


    def load_mcp_servers(self):
        py_path = sys.executable

        default_mcp_servers = {
            "mcpServers": {},
            "external_skills": {},
            "deselected_mcp_tags": [],
            "deselected_external_tools": []
        }

        # 明文读取；env / headers 里名字像密钥的字段从 secrets.json 合回
        current_servers = self.load_json(self.MCP_SERVERS_PATH)
        if current_servers is not None:
            current_servers = secrets_store.merge_mcp(
                current_servers, self._secrets().get("mcp"))

        is_modified = False
        if not current_servers or "mcpServers" not in current_servers:
            current_servers = default_mcp_servers.copy()
            is_modified = True
        else:
            legacy_academic_keys = ["academic_agent", "builtin_academic", "scholar_navis_internal"]
            mcp_configs = current_servers.get("mcpServers", {})

            for legacy_key in legacy_academic_keys:
                if legacy_key in mcp_configs:
                    self.logger.info(f"Detected legacy MCP academic tool: {legacy_key}. Removing for SKILL migration.")
                    del mcp_configs[legacy_key]
                    is_modified = True

            for key in ["external_skills", "deselected_mcp_tags", "deselected_external_tools"]:
                if key not in current_servers:
                    current_servers[key] = {} if key == "external_skills" else []
                    is_modified = True

        self.mcp_servers = current_servers
        if is_modified:
            self.save_mcp_servers()

    def save_mcp_servers(self):
        public, secret = secrets_store.split_mcp(self.mcp_servers)
        self.save_json(self.MCP_SERVERS_PATH, public)
        self._merge_secrets("mcp", secret)

    def toggle_mcp_tag(self, tag: str, is_checked: bool):
        tags = self.mcp_servers.get("deselected_mcp_tags", [])
        if is_checked and tag in tags:
            tags.remove(tag)
        elif not is_checked and tag not in tags:
            tags.append(tag)
        else:
            return

        self.mcp_servers["deselected_mcp_tags"] = tags
        self.save_mcp_servers()