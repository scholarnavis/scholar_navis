import json
import os
import sys
import logging



class ConfigManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConfigManager, cls).__new__(cls)
            cls._instance.logger = logging.getLogger("ConfigManager")

            # 1. 确定程序根目录
            if getattr(sys, 'frozen', False):
                base_dir = os.path.dirname(sys.executable)
            else:
                base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

            cls._instance.BASE_DIR = base_dir
            cls._instance.SETTINGS_PATH = os.path.join(base_dir, "config", "settings.json")
            cls._instance.MCP_SERVERS_PATH = os.path.join(base_dir, "config", "mcp_servers.json")

            # 2. 加载配置
            cls._instance.load_settings()
            cls._instance.load_mcp_servers()

        return cls._instance

    def load_settings(self):
        """
        加载用户配置，如果不存在则创建默认配置
        """
        # 默认配置模板
        default_settings = {
            "current_model_id": "embed_nano_fast",
            "rerank_model_id": "rerank_lite",
            "inference_device": "Auto",
            "proxy_url": "",
            "hf_mirror": "",
            "hf_token": "",
            "theme": "Dark",
            "log_level": "INFO",
            "is_first_run": True,
            "network_embed_url": "",
            "network_embed_key": "",
            "network_rerank_url": "",
            "network_rerank_key": ""
        }

        current_settings = {}

        # 尝试读取配置
        if os.path.exists(self.SETTINGS_PATH):
            try:
                with open(self.SETTINGS_PATH, 'r', encoding='utf-8') as f:
                    current_settings = json.load(f)
            except Exception as e:
                self.logger.error(f"Settings file corrupted, using defaults: {e}")
                current_settings = {}

        # 补全缺失字段 (合并默认配置)
        is_modified = False
        if not current_settings:
            current_settings = default_settings.copy()
            is_modified = True
        else:
            for key, default_val in default_settings.items():
                if key not in current_settings:
                    current_settings[key] = default_val
                    is_modified = True

        self.user_settings = current_settings

        # 如果有修补或新建，立即保存
        if is_modified:
            self.save_settings()

        # 应用代理环境变量
        self.apply_env_vars()

    def save_settings(self):
        """保存配置到磁盘"""

        # 确保 config 目录存在
        os.makedirs(os.path.dirname(self.SETTINGS_PATH), exist_ok=True)
        try:
            with open(self.SETTINGS_PATH, 'w', encoding='utf-8') as f:
                json.dump(self.user_settings, f, indent=4, ensure_ascii=False)

            # 保存后立即应用新的代理设置
            self.apply_env_vars()
        except Exception as e:
            self.logger.error(f"Failed to save settings: {e}")

    def apply_env_vars(self):
        proxy = self.user_settings.get("proxy_url", "").strip()
        mirror = self.user_settings.get("hf_mirror", "").strip()
        token = self.user_settings.get("hf_token", "").strip()

        if mirror: os.environ["HF_ENDPOINT"] = mirror

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

    def load_mcp_servers(self):
        """
        加载 MCP 服务器统一配置，如果不存在则创建默认配置
        """
        # 默认的 MCP 服务器字典，将 builtin 和 external 整合在一起
        default_mcp_servers = {
            "mcpServers": {
                "builtin": {
                    "type": "stdio",
                    "command": "python",
                    "args": ["-c", "from plugins.academic_mcp_server import mcp; mcp.run(transport='stdio')"],
                    "env": {"SCARF_NO_ANALYTICS": "true"},
                    "enabled": True,
                    "always_on": True,
                    "description": "Core Academic Tools (Built-in)"
                },
                "external": {
                    "type": "stdio",
                    "command": "python",
                    "args": ["plugins_ext/common_server.py"],
                    "env": {},
                    "enabled": False,
                    "always_on": False,
                    "description": "Local External Tools (common_server.py)"
                }
            },
            "deselected_mcp_tags": []
        }

        current_servers = {}

        if os.path.exists(self.MCP_SERVERS_PATH):
            try:
                with open(self.MCP_SERVERS_PATH, 'r', encoding='utf-8') as f:
                    current_servers = json.load(f)
            except Exception as e:
                self.logger.error(f"MCP servers config corrupted, using defaults: {e}")
                current_servers = {}

        is_modified = False
        if not current_servers or "mcpServers" not in current_servers:
            current_servers = default_mcp_servers.copy()
            is_modified = True
        else:
            for essential_server in ["builtin", "external"]:
                if essential_server not in current_servers["mcpServers"]:
                    current_servers["mcpServers"][essential_server] = default_mcp_servers["mcpServers"][
                        essential_server]
                    is_modified = True

            if "deselected_mcp_tags" not in current_servers:
                current_servers["deselected_mcp_tags"] = []
                is_modified = True

        self.mcp_servers = current_servers

        if is_modified:
            self.save_mcp_servers()

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

    def save_mcp_servers(self):
        os.makedirs(os.path.dirname(self.MCP_SERVERS_PATH), exist_ok=True)
        try:
            with open(self.MCP_SERVERS_PATH, 'w', encoding='utf-8') as f:
                json.dump(self.mcp_servers, f, indent=4, ensure_ascii=False)
        except Exception as e:
            self.logger.error(f"Failed to save MCP servers config: {e}")


