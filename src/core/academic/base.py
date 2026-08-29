"""Academic agent infrastructure: config bootstrap, logging, HTTP and retry helpers.

This module keeps all module-level side effects (config init, network env setup,
NCBI Entrez configuration) so importing any academic sub-module triggers the
exact same bootstrap sequence as the original ``academic_agent`` monolith.
"""
import ipaddress  # noqa: F401  (kept for backward-compat imports)
import json
import logging
import os
import re  # noqa: F401
import socket
import time
import urllib.parse
from functools import wraps
from typing import Literal  # noqa: F401

from Bio import Entrez

from src.core import BASE_DIR
from src.core.config_manager import ConfigManager
from src.core.email_check import verify_email_robust
from src.core.network_worker import setup_global_network_env, create_robust_session, GlobalRateLimiter, global_rate_limiter

__all__ = [
    "UdpJsonHandler", "logger", "ConfigManager", "BASE_DIR",
    "get_setting_or_env", "ncbi_email", "ncbi_api_key", "openalex_api_key",
    "s2_api_key", "github_token", "is_ncbi_email_valid", "is_ncbi_enabled",
    "WORKSPACE_DIR", "mcp_request", "simple_retry",
    "global_rate_limiter", "GlobalRateLimiter", "create_robust_session",
    "Entrez",
]


class UdpJsonHandler(logging.Handler):
    def __init__(self, server_name="Academic.MCP", host='127.0.0.1'):
        super().__init__()
        self.server_name = server_name
        port_str = os.environ.get("MCP_LOG_PORT")
        self.port = int(port_str) if port_str and port_str.isdigit() else None

        if self.port:
            self.address = (host, self.port)
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        else:
            self.sock = None

    def emit(self, record):
        if not self.sock: return
        try:
            raw_msg = self.format(record)

            if len(raw_msg) > 50000:
                raw_msg = raw_msg[:50000] + "\n...[Log Truncated due to UDP size limit]"

            log_data = {
                "server": self.server_name,
                "level": record.levelname,
                "msg": raw_msg
            }
            self.sock.sendto(json.dumps(log_data).encode('utf-8'), self.address)
        except Exception:
            pass


logger = logging.getLogger("Academic.Server")
logger.setLevel(logging.INFO)

ConfigManager()
setup_global_network_env()


def get_setting_or_env(key, env_name):
    """优先读取 GUI 设置，如果为空再读环境变量"""
    val = str(ConfigManager().user_settings.get(key, "")).strip()
    if not val:
        val = os.environ.get(env_name, "").strip()
    return val

ncbi_email = get_setting_or_env("ncbi_email", "NCBI_API_EMAIL")
ncbi_api_key = get_setting_or_env("ncbi_api_key", "NCBI_API_KEY")
openalex_api_key = get_setting_or_env("openalex_api_key", "OPENALEX_API_KEY")
s2_api_key = get_setting_or_env("s2_api_key", "S2_API_KEY")
github_token = get_setting_or_env("github_token", "GITHUB_TOKEN")

_EMAIL_VALID_CACHE = None
def is_ncbi_email_valid():
    global _EMAIL_VALID_CACHE
    if _EMAIL_VALID_CACHE is None:
        if ncbi_email:
            _EMAIL_VALID_CACHE = verify_email_robust(ncbi_email).get("is_valid", False)
        else:
            _EMAIL_VALID_CACHE = False

    if _EMAIL_VALID_CACHE:logger.info(f"NCBI email: {ncbi_email[0:5]}...{ncbi_email[-5:]} is valid.")
    else: logger.error(f"NCBI email: {ncbi_email[0:5]}...{ncbi_email[-5:]} is invalid.")

    return _EMAIL_VALID_CACHE

def is_ncbi_enabled():
    """双重校验：只有 Email 和 API Key 都存在才能使用 NCBI"""
    return is_ncbi_email_valid() and bool(ncbi_api_key)

# 满足你的需求：保持启动时的日志打印！
if ncbi_email: logger.info("Using NCBI Email.")
if ncbi_api_key: logger.info("Using NCBI API Key.")
if openalex_api_key: logger.info("Using OpenALEX API Key.")
if s2_api_key: logger.info("Using S2 API Key.")
if github_token: logger.info("Using GitHub Token.")


WORKSPACE_DIR = os.path.join(BASE_DIR, 'tools',"mcp")
os.makedirs(WORKSPACE_DIR, exist_ok=True)
logger.info(f"Local Workspace initialized at: {WORKSPACE_DIR}")

http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
if http_proxy:
    logger.info(f"MCP Server is running with global proxy: {http_proxy}")

Entrez.email = ncbi_email
Entrez.tool = "ScholarNavis"
if ncbi_api_key:
    Entrez.api_key = ncbi_api_key


def mcp_request(method: str, url: str, **kwargs):
    session = create_robust_session()
    custom_headers = kwargs.pop("headers", {})
    if "User-Agent" in custom_headers and custom_headers["User-Agent"] == "Mozilla/5.0":
        custom_headers.pop("User-Agent")
    session.headers.update(custom_headers)
    try:
        return session.request(method, url, **kwargs)
    except Exception as e:
        err_str = str(e).lower()
        if any(keyword in err_str for keyword in["tls", "closed abruptly", "empty reply", "certificate", "ssl", "time"]):
            logger.warning(f"curl_cffi failed ({e}). Falling back to standard requests for {url}")
            import requests
            req_session = requests.Session()
            http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
            if http_proxy:
                req_session.proxies = {"http": http_proxy, "https": http_proxy}
            req_session.headers.update(custom_headers)
            return req_session.request(method, url, **kwargs)
        raise e
    finally:
        session.close()


def simple_retry(max_attempts=3, delay=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    err_str = str(e).lower()

                    if any(code in err_str for code in ["400", "401", "403", "404", "not found", "bad request"]):
                        logger.warning(f"Client error detected ({err_str}), skipping retry for '{func.__name__}' 喵.")
                        raise e

                    if attempt == max_attempts - 1:
                        raise e
                    wait = delay * (2 ** attempt)
                    logger.warning(f"Attempt {attempt + 1} for '{func.__name__}' failed: {e}. Retrying in {wait}s...")
                    time.sleep(wait)

        return wrapper

    return decorator
