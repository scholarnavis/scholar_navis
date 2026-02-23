import os
import requests
import httpx
from PySide6.QtCore import QObject, Signal
from src.core.config_manager import ConfigManager


def setup_global_network_env():
    cfg = ConfigManager().user_settings
    proxy_mode = cfg.get("proxy_mode", "system")
    proxy_url = cfg.get("proxy_url", "").strip()
    hf_mirror = cfg.get("hf_mirror", "").strip()

    if hf_mirror:
        os.environ["HF_ENDPOINT"] = hf_mirror
    else:
        os.environ.pop("HF_ENDPOINT", None)

    if proxy_mode == "off":
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("ALL_PROXY", None)
        os.environ["NO_PROXY"] = "*"
    elif proxy_mode == "custom" and proxy_url:
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        os.environ["ALL_PROXY"] = proxy_url
        os.environ.pop("NO_PROXY", None)
    else:
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("ALL_PROXY", None)
        os.environ.pop("NO_PROXY", None)


def _get_explicit_proxy_kwargs():
    """读取配置，生成请求库所需的显式代理参数"""
    cfg = ConfigManager().user_settings
    proxy_mode = cfg.get("proxy_mode", "system")
    proxy_url = cfg.get("proxy_url", "").strip()

    if proxy_mode == "custom" and proxy_url:
        return {"proxy": proxy_url}
    elif proxy_mode == "off":
        return {"trust_env": False}
    return {}


class LightNetworkWorker(QObject):
    sig_models_fetched = Signal(bool, list, str)
    sig_test_finished = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self._is_cancelled = False
        self._req_session = None
        self._httpx_client = None

    def cancel(self):
        self._is_cancelled = True
        if self._req_session:
            try:
                self._req_session.close()
            except:
                pass
        if self._httpx_client:
            try:
                self._httpx_client.close()
            except:
                pass

    def fetch_models(self, base_url, api_key):
        self._is_cancelled = False
        self._req_session = requests.Session()

        proxy_cfg = _get_explicit_proxy_kwargs()
        if "trust_env" in proxy_cfg:
            self._req_session.trust_env = False
        elif "proxy" in proxy_cfg:
            self._req_session.proxies = {"http": proxy_cfg["proxy"], "https": proxy_cfg["proxy"]}

        try:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            clean_url = base_url.rstrip('/')
            url = f"{clean_url}/models"

            res = self._req_session.get(url, headers=headers, timeout=8)
            res.raise_for_status()

            data = res.json()
            models = sorted([m['id'] for m in data.get('data', [])])

            if not models:
                self.sig_models_fetched.emit(False, [],
                                             "API returned an empty list. Please check your key or enter manually.")
                return

            self.sig_models_fetched.emit(True, models, f"Successfully fetched {len(models)} available models!")

        except requests.exceptions.RequestException as e:
            if self._is_cancelled:
                self.sig_models_fetched.emit(False, [], "Operation cancelled by user.")
            else:
                self.sig_models_fetched.emit(False, [], f"Network request failed: {str(e)}")
        finally:
            self._req_session.close()

    def test_api(self, base_url, api_key, model_name, custom_params=None):
        self._is_cancelled = False
        custom_params = custom_params or {}

        # 配置代理环境
        httpx_kwargs = {"timeout": 15.0}
        httpx_kwargs.update(_get_explicit_proxy_kwargs())

        if "proxy" in httpx_kwargs:
            httpx_kwargs["proxies"] = httpx_kwargs.pop("proxy")

        self._httpx_client = httpx.Client(**httpx_kwargs)

        try:
            url = f"{base_url.rstrip('/')}/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key or 'sk-test'}",
                "Content-Type": "application/json"
            }

            payload = {
                "model": model_name,
                "messages": [{"role": "user", "content": "Hello. Please reply with exactly one word: 'OK'."}],
                "max_tokens": 5
            }

            # 融合自定义参数（跳过影响测试的保留字段）
            for k, v in custom_params.items():
                if k not in ["model", "messages", "stream"]:
                    payload[k] = v

            response = self._httpx_client.post(url, headers=headers, json=payload)
            response.raise_for_status()

            data = response.json()
            choices = data.get("choices", [])

            if not choices:
                raise ValueError("No choices returned from API.")

            msg_obj = choices[0].get("message", {})
            raw_content = msg_obj.get("content")

            if raw_content is None:
                if msg_obj.get("reasoning_content"):
                    raw_content = str(msg_obj.get("reasoning_content"))
                else:
                    raw_content = "[Empty Response / Filtered by Provider]"

            reply = raw_content.strip()
            self.sig_test_finished.emit(True,
                                        f"✅ API connectivity is excellent!\nModel '{model_name}' responded successfully:\n'{reply}'")

        except httpx.HTTPStatusError as e:
            err_text = e.response.text
            self.sig_test_finished.emit(False, f"Test failed: HTTP {e.response.status_code}\n{err_text}")
        except Exception as e:
            if self._is_cancelled or "closed" in str(e).lower():
                self.sig_test_finished.emit(False, "Operation cancelled by user.")
            else:
                self.sig_test_finished.emit(False, f"Test failed: {str(e)}")
        finally:
            self._httpx_client.close()

    def do_fetch_models(self):
        self.fetch_models(getattr(self, 'base_url', ''), getattr(self, 'api_key', ''))

    def do_test_api(self):
        self.test_api(
            getattr(self, 'base_url', ''),
            getattr(self, 'api_key', ''),
            getattr(self, 'model_name', ''),
            getattr(self, 'custom_params', {})
        )