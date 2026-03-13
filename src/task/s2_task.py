import os
import time
import threading
import logging
from src.core.network_worker import create_robust_session
from src.core.config_manager import ConfigManager

logger = logging.getLogger("S2Task")


class S2TaskManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(S2TaskManager, cls).__new__(cls)
            return cls._instance

    def _get_current_config(self):
        try:
            config = ConfigManager().user_settings
            key = config.get("s2_api_key", "").strip()
            if not key:
                key = os.environ.get("S2_API_KEY", "").strip()

            limit_str = str(config.get("s2_rate_limit", "")).strip()
            if not limit_str:
                limit_str = os.environ.get("S2_RATE_LIMIT", "1.0").strip()

            if not limit_str or not limit_str.replace('.', '', 1).isdigit():
                limit_str = "1.0"
            return key, float(limit_str)
        except Exception as e:
            logger.error(f"Failed to load S2 config: {e}")
            return "", 1.0

    def is_enabled(self):
        key, limit = self._get_current_config()
        return bool(key and limit > 0)

    def execute_request(self, method, url, max_retries=3, **kwargs):
        api_key, rate_limit = self._get_current_config()

        if not api_key:
            logger.error("S2 Request Rejected: Missing S2_API_KEY in settings.")
            return None

        if rate_limit <= 0:
            logger.error("S2 Request Rejected: Invalid S2_RATE_LIMIT.")
            return None

        min_interval = 1.0 / rate_limit

        session = create_robust_session()
        headers = kwargs.pop("headers", {})
        headers["x-api-key"] = api_key
        session.headers.update(headers)
        req_timeout = kwargs.pop("timeout", 15)
        try:
            for attempt in range(max_retries):
                with self._lock:
                    now = time.time()

                    if hasattr(self, '_last_request_time'):
                        elapsed = now - self._last_request_time
                        if elapsed < min_interval:
                            time.sleep(min_interval - elapsed)
                    self._last_request_time = time.time()

                res = session.request(method, url, timeout=req_timeout, **kwargs)

                if res.status_code == 429:
                    wait = 2 ** attempt
                    logger.warning(f"S2 Rate Limited (429). Retrying in {wait}s...")
                    time.sleep(wait)
                    continue

                res.raise_for_status()
                return res

            raise Exception("S2 request failed after max retries due to server rejections.")
        finally:
            session.close()


s2_manager = S2TaskManager()


def is_s2_enabled():
    return s2_manager.is_enabled()


def s2_request(method, url, params=None, headers=None, timeout=15):

    if not is_s2_enabled():
        logger.warning("S2 API Key not configured")
        raise ValueError("S2 API Key not configured")

    res = s2_manager.execute_request(method, url, params=params, headers=headers or {}, timeout=timeout)
    if res is None:
        raise Exception("S2 request failed or was rejected by rate limit manager.")
    return res