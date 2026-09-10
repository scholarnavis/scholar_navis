import os
import time
import threading
import logging
from src.core.network_worker import create_robust_session, auth_breaker, record_auth_rejection, is_auth_blocked
from src.core.config_manager import ConfigManager

logger = logging.getLogger("S2Task")

# S2 在全局鉴权熔断器中的服务键（与其 API host 一致，
# 与 mcp_request 侧的 host 键体系统一）
_S2_AUTH_KEY = "api.semanticscholar.org"


class S2TaskManager:
    _instance = None
    _lock = threading.Lock()
    # 限流时间戳：延迟创建，reload_config 中可被删除，故仅作类型声明
    _last_request_time: float

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(S2TaskManager, cls).__new__(cls)
            return cls._instance

    def reload_config(self):
        """
        Resets the internal rate limiter state.
        Ensures immediate adoption of updated configurations by clearing legacy timestamps.
        Also clears S2's auth-rejection breaker count so an updated API key
        takes effect immediately (only S2's own counter is reset).
        """
        with self._lock:
            auth_breaker.reset_service(_S2_AUTH_KEY)
            if hasattr(self, '_last_request_time'):
                delattr(self, '_last_request_time')


    def _get_current_config(self):
        try:
            config = ConfigManager().user_settings
            key = config.get("s2_api_key", "").strip()
            if not key:
                key = os.environ.get("S2_API_KEY", "").strip()

            limit_raw = config.get("s2_rate_limit", "")
            if not limit_raw:
                limit_raw = os.environ.get("S2_RATE_LIMIT", 1.0)

            try:
                rate_limit = float(limit_raw)
                if rate_limit <= 0:
                    rate_limit = 1.0
            except (ValueError, TypeError):
                rate_limit = 1.0

            return key, rate_limit
        except Exception as e:
            logger.error(f"Failed to load S2 config: {e}")
            return "", 1.0

    def is_enabled(self):
        if is_auth_blocked(_S2_AUTH_KEY):
            return False
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

        # 鉴权熔断前置拦截（防御性：正常路径已由 is_enabled 拦截）
        if is_auth_blocked(_S2_AUTH_KEY):
            raise RuntimeError(
                "S2 request skipped: source blocked by auth-rejection breaker "
                "for the current chat round.")

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

                if res.status_code == 403:
                    # 403 = key 无效/被拒或 IP 被封，计入全局鉴权熔断器（阈值 2，
                    # 本轮对话内生效）；达到阈值后后续请求零开销跳过，其他
                    # 来源不受影响。429 属临时限流，仍走下方重试。
                    # 熔断键统一按 URL host 归一（与 mcp_request 侧一致），
                    # S2 的 host 即 _S2_AUTH_KEY。
                    record_auth_rejection(url, context="HTTP 403 via S2 API.")
                    res.raise_for_status()

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