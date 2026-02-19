import os
import requests
import httpx
from openai import OpenAI
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
        """Asynchronously fetch model list"""
        self._is_cancelled = False
        self._req_session = requests.Session()
        try:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            clean_url = base_url.rstrip('/')
            url = f"{clean_url}/models"

            # Use the bound session to send the request.
            # Once self.cancel() is called, this will immediately raise an exception.
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

    def test_api(self, base_url, api_key, model_name):
        """Test LLM connectivity"""
        self._is_cancelled = False
        self._httpx_client = httpx.Client(timeout=15.0)
        try:
            # Pass the custom http_client to the OpenAI client
            client = OpenAI(
                api_key=api_key or "sk-test",
                base_url=base_url,
                http_client=self._httpx_client
            )

            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": "Hello. Please reply with exactly one word: 'OK'."}],
                max_tokens=5
            )

            reply = response.choices[0].message.content.strip()
            self.sig_test_finished.emit(True,
                                        f"✅ API connectivity is excellent!\nModel '{model_name}' responded successfully: '{reply}'")

        except Exception as e:
            if self._is_cancelled or "closed" in str(e).lower():
                self.sig_test_finished.emit(False, "Operation cancelled by user.")
            else:
                self.sig_test_finished.emit(False, f"Test failed: {str(e)}")
        finally:
            self._httpx_client.close()
