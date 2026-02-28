import json
import os
import sys
import logging
import threading
import tempfile
import keyring
from cryptography.fernet import Fernet, InvalidToken


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

        # 1. 确定根目录
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            current_path = os.path.abspath(__file__)
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(current_path)))

        self.BASE_DIR = base_dir
        self.CONFIG_DIR = os.path.join(base_dir, "config")
        self.SETTINGS_PATH = os.path.join(self.CONFIG_DIR, "settings.json")
        self.MCP_SERVERS_PATH = os.path.join(self.CONFIG_DIR, "mcp_servers.json")

        # 系统凭据管理器标识
        self.KEYRING_SERVICE = "ScholarNavis"
        self.KEYRING_ACCOUNT = "config_encryption_key"

        # 确保配置目录存在
        os.makedirs(self.CONFIG_DIR, exist_ok=True)

        # 2. 初始化加密套件
        self._init_encryption()

        # 3. 加载核心配置
        self.load_settings()
        self.load_mcp_servers()

        self._initialized = True

    def _init_encryption(self):
        """基于系统凭据管理器获取或生成唯一密钥"""
        try:
            key = keyring.get_password(self.KEYRING_SERVICE, self.KEYRING_ACCOUNT)

            fallback_key_path = os.path.join(self.CONFIG_DIR, ".secret_fallback.key")

            if not key:
                # 检查是否存在 Fallback 密钥
                if os.path.exists(fallback_key_path):
                    with open(fallback_key_path, 'r', encoding='utf-8') as f:
                        key = f.read().strip()
                else:
                    # 如果都没有，生成一个全新的高强度安全密钥
                    key = Fernet.generate_key().decode('utf-8')
                    try:
                        # 存入系统凭据管理器
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
            temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(path), text=not encrypt)
            try:
                json_str = json.dumps(data, indent=4, ensure_ascii=False)

                if encrypt:
                    content = self.fernet.encrypt(json_str.encode('utf-8'))
                    with os.fdopen(temp_fd, 'wb') as f:
                        f.write(content)
                else:
                    with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
                        f.write(json_str)

                os.replace(temp_path, path)
            except Exception as e:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                self.logger.error(f"Failed to save config at {path}: {e}")

    def load_json(self, path, encrypt=True):
        """
        完整定义的 load_json：
        1. 处理文件不存在
        2. 处理加密解密（包含 InvalidToken 容错）
        3. 处理从明文到密文的自动迁移（真实触发保存）
        """
        if not os.path.exists(path):
            return None

        with self._lock:
            try:
                if encrypt:
                    with open(path, 'rb') as f:
                        raw_data = f.read()

                    try:
                        # 尝试解密
                        decrypted_text = self.fernet.decrypt(raw_data).decode('utf-8')
                        return json.loads(decrypted_text)
                    except (InvalidToken, Exception) as e:
                        # 重点：如果是密钥错误或非密文格式，尝试作为明文读取（兼容迁移）
                        self.logger.warning(f"Decryption failed for {path}, checking if plain text: {e}")
                        try:
                            with open(path, 'r', encoding='utf-8') as f:
                                plain_data = json.load(f)

                            # 优化点 2：如果明文读取成功，利用 RLock 直接安全地触发加密覆盖保存！
                            self.logger.info(f"Migrating plain-text config to encrypted: {path}")
                            self.save_json(path, plain_data, encrypt=True)
                            return plain_data

                        except Exception:
                            self.logger.error(f"Config file is corrupted or not valid JSON: {path}")
                            return None
                else:
                    # 非加密模式读取
                    with open(path, 'r', encoding='utf-8') as f:
                        return json.load(f)
            except Exception as e:
                self.logger.error(f"Critical error reading {path}: {e}")
                return None

    def load_settings(self):
        default_settings = {
            "current_model_id": "embed_nano_fast",
            "rerank_model_id": "rerank_lite",
            "inference_device": "cpu",
            "proxy_url": "",
            "hf_mirror": "",
            "hf_token": "",
            "theme": "Dark",
            "log_level": "INFO",
            "is_first_run": True,
            "ncbi_email": "",
            "ncbi_api_key": "",
            "s2_api_key": ""
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

    def apply_env_vars(self):
        """将配置注入环境变量，影响后续库的行为"""
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
        os.environ["S2_API_KEY"] = self.user_settings.get("s2_api_key", "")

    def load_mcp_servers(self):
        py_path = sys.executable

        default_mcp_servers = {
            "mcpServers": {
                "builtin": {
                    "type": "stdio",
                    "command": py_path,
                    "args": ["-c", "from plugins.academic_mcp_server import mcp; mcp.run(transport='stdio')"],
                    "enabled": True, "always_on": True, "description": "Core Tools"
                },
                "external": {
                    "type": "stdio", "command": py_path, "args": ["plugins_ext/common_server.py"],
                    "enabled": False, "always_on": False, "description": "External Tools"
                }
            },
            "deselected_mcp_tags": []
        }

        current_servers = self.load_json(self.MCP_SERVERS_PATH, encrypt=True)

        is_modified = False
        if not current_servers or "mcpServers" not in current_servers:
            current_servers = default_mcp_servers.copy()
            is_modified = True
        else:
            for key in ["builtin", "external"]:
                if key not in current_servers["mcpServers"]:
                    current_servers["mcpServers"][key] = default_mcp_servers["mcpServers"][key]
                    is_modified = True
                else:
                    current_servers["mcpServers"][key]["command"] = py_path

            if "deselected_mcp_tags" not in current_servers:
                current_servers["deselected_mcp_tags"] = []
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