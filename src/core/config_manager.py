import json
import os
import sys
import logging
import threading
import tempfile
import keyring
from cryptography.fernet import Fernet, InvalidToken

from src.core import BASE_DIR
from src.core.encryption_service import SystemEncryptionService

# 当前配置导出版本号，用于未来兼容性检查
CONFIG_VERSION = "1.0"


class ConfigManager:
    _instance = None
    _lock = threading.RLock()

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

        # 系统凭据管理器标识
        self.KEYRING_SERVICE = "ScholarNavis"
        self.KEYRING_ACCOUNT = "config_encryption_key"

        # 确保配置目录存在
        os.makedirs(self.CONFIG_DIR, exist_ok=True)

        # 2. 初始化加密套件
        self.encryption_service = SystemEncryptionService(service_name="ScholarNavis")
        self.encryption_service._get_master_key()

        # 3. 加载核心配置
        self.load_settings()
        self.load_mcp_servers()

        self._initialized = True

    def _init_encryption(self):
        """基于系统凭据管理器获取或生成唯一密钥"""
        try:
            key = None
            try:
                # 单独捕获 keyring 异常，防止环境不支持时直接崩溃
                key = keyring.get_password(self.KEYRING_SERVICE, self.KEYRING_ACCOUNT)
            except Exception as e:
                self.logger.warning(f"Keyring unavailable or locked, triggering fallback: {e}")

            fallback_key_path = os.path.join(self.CONFIG_DIR, ".secret_fallback.key")

            if not key:
                # 检查是否存在 Fallback 密钥
                if os.path.exists(fallback_key_path):
                    with open(fallback_key_path, 'r', encoding='utf-8') as f:
                        key = f.read().strip()
                else:
                    key = Fernet.generate_key().decode('utf-8')
                    try:
                        keyring.set_password(self.KEYRING_SERVICE, self.KEYRING_ACCOUNT, key)
                        self.logger.info("New encryption key generated and stored in system keyring.")
                    except Exception as e:
                        # 如果系统凭据管理器不可用，回退到本地隐藏文件存储
                        self.logger.warning(f"Keyring unavailable, falling back to local hidden file: {e}")
                        with open(fallback_key_path, 'w', encoding='utf-8') as f:
                            f.write(key)

            # 初始化 Fernet 加密对象
            self.fernet = Fernet(key.encode('utf-8'))
        except Exception as e:
            self.logger.critical(f"Encryption initialization failed: {e}")
            raise

    def save_json(self, path, data, encrypt=True):
        """原子性写入 JSON 配置文件"""
        with self._lock:
            temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(path), text=False)  # 始终二进制写入
            os.close(temp_fd)
            try:
                json_str = json.dumps(data, indent=4, ensure_ascii=False)
                if encrypt:
                    content = self.encryption_service.encrypt(json_str)
                    with open(temp_path, 'wb') as f:
                        f.write(content)
                else:
                    with open(temp_path, 'w', encoding='utf-8') as f:
                        f.write(json_str)

                import time
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


    def load_json(self, path, encrypt=True):
        if not os.path.exists(path):
            return None
        with self._lock:
            try:
                if encrypt:
                    with open(path, 'rb') as f:
                        raw_data = f.read()

                    try:
                        decrypted_text = self.encryption_service.decrypt(raw_data)
                        return json.loads(decrypted_text)
                    except Exception as e:
                        # 处理旧版本明文迁移逻辑
                        self.logger.warning(f"Decryption failed, trying plain text migration: {e}")
                        try:
                            with open(path, 'r', encoding='utf-8') as f:
                                plain_data = json.load(f)
                            self.save_json(path, plain_data, encrypt=True)
                            return plain_data
                        except Exception:
                            return None
                else:
                    try:
                        with open(path, 'r', encoding='utf-8') as f:
                            return json.load(f)
                    except Exception as e:
                        # 处理之前可能被意外加密的配置文件，尝试解密并迁移至明文
                        self.logger.warning(f"Plain text load failed, trying decryption migration: {e}")
                        try:
                            with open(path, 'rb') as f:
                                raw_data = f.read()
                            decrypted_text = self.encryption_service.decrypt(raw_data)
                            plain_data = json.loads(decrypted_text)
                            self.save_json(path, plain_data, encrypt=False)
                            return plain_data
                        except Exception:
                            return None
            except Exception as e:
                self.logger.error(f"Critical error reading {path}: {e}")
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
            "log_level": "INFO",
            "is_first_run": True,
            "ncbi_email": "",
            "ncbi_api_key": "",
            "openalex_api_key": "",
            "s2_api_key": "",
            "s2_rate_limit": "1.0",
            "github_token": "",
            "quick_trans_is_pinned": True,
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
            "chat_ribbon_state": "Pinned",
            "api_server_host": "127.0.0.1",
            "api_server_port": 8000,
            "api_server_key": "",
        }

        current_settings = self.load_json(self.SETTINGS_PATH, encrypt=True)

        is_modified = False
        if current_settings is None:
            current_settings = default_settings.copy()
            is_modified = True
        else:
            for key, default_val in default_settings.items():
                if key not in current_settings:
                    current_settings[key] = default_val
                    is_modified = True

        self.user_settings = current_settings
        if is_modified:
            self.save_settings()
        self.apply_env_vars()

    def save_settings(self):
        self.save_json(self.SETTINGS_PATH, self.user_settings)
        self.apply_env_vars()

    def load_llm_configs(self) -> list:
        data = self.load_json(self.LLM_CONFIG_PATH, encrypt=True)
        if data is None:
            return []
        if isinstance(data, list):
            return data
        self.logger.warning("llm_config.json format unexpected, resetting to empty list.")
        return []

    def save_llm_configs(self, configs: list):
        self.save_json(self.LLM_CONFIG_PATH, configs, encrypt=True)


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


    def save_external_models(self, data):
        self.save_json(self.EXTERNAL_MODELS_PATH, data, encrypt=False)

    def load_external_models_data(self):
        data = self.load_json(self.EXTERNAL_MODELS_PATH, encrypt=False)
        return data if data else {"embedding": [], "reranker": []}


    def load_mcp_servers(self):
        py_path = sys.executable

        default_mcp_servers = {
            "mcpServers": {},
            "external_skills": {},
            "deselected_mcp_tags": [],
            "deselected_external_tools": []
        }

        current_servers = self.load_json(self.MCP_SERVERS_PATH, encrypt=True)

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
        self.save_json(self.MCP_SERVERS_PATH, self.mcp_servers, encrypt=True)

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