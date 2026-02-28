import os
import logging
import random
import re

import requests
import httpx
from PySide6.QtCore import QObject, Signal
from chromadb import EmbeddingFunction, Documents, Embeddings

from src.core.config_manager import ConfigManager

logger = logging.getLogger("NetworkWorker")

def get_random_browser_headers():
    chrome_v = random.randint(120, 124)
    ff_v = random.randint(120, 125)
    mac_minor = random.randint(14, 15)
    mac_patch = random.randint(1, 7)

    templates = [
        # Windows Chrome
        {
            "ua": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_v}.0.0.0 Safari/537.36",
            "browser": "chrome",
            "os": "Windows"
        },
        # Mac Chrome
        {
            "ua": f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_{mac_minor}_{mac_patch}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_v}.0.0.0 Safari/537.36",
            "browser": "chrome",
            "os": "macOS"
        },
        # Windows Edge
        {
            "ua": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_v}.0.0.0 Safari/537.36 Edg/{chrome_v}.0.0.0",
            "browser": "edge",
            "os": "Windows"
        },
        # Windows Firefox
        {
            "ua": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{ff_v}.0) Gecko/20100101 Firefox/{ff_v}.0",
            "browser": "firefox",
            "os": "Windows"
        },
        # Mac Firefox
        {
            "ua": f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_{mac_minor}_{mac_patch}; rv:{ff_v}.0) Gecko/20100101 Firefox/{ff_v}.0",
            "browser": "firefox",
            "os": "macOS"
        }
    ]

    choice = random.choice(templates)

    headers = {
        'User-Agent': choice["ua"],
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0'
    }

    # 如果是 Chromium 内核 (Chrome/Edge)，动态注入对应的 Sec-Ch-Ua 指纹
    if choice["browser"] in ["chrome", "edge"]:
        brand = "Microsoft Edge" if choice["browser"] == "edge" else "Google Chrome"
        headers['Sec-Ch-Ua'] = f'"Chromium";v="{chrome_v}", "Not(A:Brand";v="24", "{brand}";v="{chrome_v}"'
        headers['Sec-Ch-Ua-Mobile'] = '?0'
        headers['Sec-Ch-Ua-Platform'] = f'"{choice["os"]}"'

    return headers


# 引入支持 TLS 指纹伪装的库
try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    import requests as cffi_requests
    HAS_CFFI = False
    logger.warning("curl_cffi is not installed. Falling back to standard requests. Strict WAFs may block access.")


def create_robust_session():
    """
    创建一个真正强壮的 Session，
    自动挂载全局代理配置，并使用底层 TLS 指纹 + 严格匹配的 Header 伪装真实浏览器。
    """
    if HAS_CFFI:
        targets = ["chrome110", "chrome116", "chrome120"]
        target = random.choice(targets)

        v_match = re.search(r'\d+', target)
        chrome_v = int(v_match.group()) if v_match else 110

        try:
            session = cffi_requests.Session(impersonate=target)
        except Exception as e:
            logger.warning(f"Impersonate target '{target}' not supported, falling back to chrome110: {e}")
            session = cffi_requests.Session(impersonate="chrome110")
            chrome_v = 110

        session.headers.update({
            'Accept': 'application/rss+xml, application/xml, text/xml, text/html;q=0.9, image/avif, image/webp, */*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Ch-Ua': f'"Chromium";v="{chrome_v}", "Google Chrome";v="{chrome_v}", "Not:A-Brand";v="99"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"'
        })
    else:
        session = cffi_requests.Session()
        session.headers.update(get_random_browser_headers())

    proxy_cfg = _get_explicit_proxy_kwargs()
    if "trust_env" in proxy_cfg:
        session.trust_env = False
    elif "proxy" in proxy_cfg:
        session.proxies = {"http": proxy_cfg["proxy"], "https": proxy_cfg["proxy"]}

    return session


def setup_global_network_env():
    cfg = ConfigManager().user_settings
    proxy_mode = cfg.get("proxy_mode", "off")
    proxy_url = cfg.get("proxy_url", "").strip()
    hf_mirror = cfg.get("hf_mirror", "").strip()

    if hf_mirror:
        os.environ["HF_ENDPOINT"] = hf_mirror
        logger.debug(f"HF_ENDPOINT set to: {hf_mirror}")
    else:
        os.environ.pop("HF_ENDPOINT", None)

    if hf_mirror:
        os.environ["HF_ENDPOINT"] = hf_mirror
        logger.debug(f"HF_ENDPOINT set to: {hf_mirror}")
    else:
        os.environ.pop("HF_ENDPOINT", None)

    if proxy_mode == "custom" and proxy_url:
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        os.environ["ALL_PROXY"] = proxy_url
        os.environ.pop("NO_PROXY", None)
        logger.debug(f"Global custom proxy set to: {proxy_url}")
    else:
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("ALL_PROXY", None)
        os.environ["NO_PROXY"] = "*"
        logger.debug("Global proxy disabled.")


def _get_explicit_proxy_kwargs():
    """Reads configuration to generate explicit proxy arguments for request libraries."""
    cfg = ConfigManager().user_settings
    proxy_mode = cfg.get("proxy_mode", "off")
    proxy_url = cfg.get("proxy_url", "").strip()

    if proxy_mode == "custom" and proxy_url:
        return {"proxy": proxy_url}
    return {"trust_env": False}


class LightNetworkWorker(QObject):
    sig_models_fetched = Signal(bool, list, str)
    sig_test_finished = Signal(bool, str)
    sig_image_downloaded = Signal(bool, str, str)

    def __init__(self):
        super().__init__()
        self._is_cancelled = False
        self._req_session = None
        self._httpx_client = None

    def cancel(self):
        logger.info("Network operation cancelled by user.")
        self._is_cancelled = True
        if self._req_session:
            try:
                self._req_session.close()
            except Exception as e:
                logger.debug(f"Error closing requests session: {e}")
        if self._httpx_client:
            try:
                self._httpx_client.close()
            except Exception as e:
                logger.debug(f"Error closing httpx client: {e}")

    def fetch_models(self, base_url, api_key):
        self._is_cancelled = False
        self._req_session = requests.Session()

        proxy_cfg = _get_explicit_proxy_kwargs()
        if "trust_env" in proxy_cfg:
            self._req_session.trust_env = False
        elif "proxy" in proxy_cfg:
            self._req_session.proxies = {"http": proxy_cfg["proxy"], "https": proxy_cfg["proxy"]}

        url = f"{base_url.rstrip('/')}/models"
        logger.info(f"Fetching models from: {url}")

        try:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            res = self._req_session.get(url, headers=headers, timeout=8)
            res.raise_for_status()

            data = res.json()
            models = sorted([m['id'] for m in data.get('data', [])])

            if not models:
                logger.warning("API returned an empty model list.")
                self.sig_models_fetched.emit(False, [],
                                             "API returned an empty list. Please check your key or enter manually.")
                return

            logger.info(f"Successfully fetched {len(models)} models.")
            self.sig_models_fetched.emit(True, models, f"Successfully fetched {len(models)} available models!")

        except requests.exceptions.RequestException as e:
            if self._is_cancelled:
                self.sig_models_fetched.emit(False, [], "Operation cancelled by user.")
            else:
                logger.error(f"Failed to fetch models: {str(e)}")
                self.sig_models_fetched.emit(False, [], f"Network request failed: {str(e)}")
        finally:
            self._req_session.close()

    def test_api(self, base_url, api_key, model_name, custom_params=None):
        self._is_cancelled = False
        custom_params = custom_params or {}

        httpx_kwargs = {"timeout": 15.0}
        proxy_cfg = _get_explicit_proxy_kwargs()

        if "proxy" in proxy_cfg:
            httpx_kwargs["proxy"] = proxy_cfg["proxy"]
        elif "trust_env" in proxy_cfg:
            httpx_kwargs["trust_env"] = proxy_cfg["trust_env"]

        self._httpx_client = httpx.Client(**httpx_kwargs)
        url = f"{base_url.rstrip('/')}/chat/completions"
        logger.info(f"Testing API endpoint: {url} with model: {model_name}")

        try:
            headers = {
                "Authorization": f"Bearer {api_key or 'sk-test'}",
                "Content-Type": "application/json"
            }

            payload = {
                "model": model_name,
                "messages": [{"role": "user", "content": "Hello. Please reply with exactly one word: 'OK'."}],
                "max_tokens": 5
            }

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
            logger.info(f"API Test successful. Model replied: {reply}")
            self.sig_test_finished.emit(True,
                                        f"✅ API connectivity is excellent!\nModel '{model_name}' responded successfully:\n'{reply}'")

        except httpx.HTTPStatusError as e:
            err_text = e.response.text
            logger.error(f"API Test failed with HTTP {e.response.status_code}: {err_text}")
            self.sig_test_finished.emit(False, f"Test failed: HTTP {e.response.status_code}\n{err_text}")
        except Exception as e:
            if self._is_cancelled or "closed" in str(e).lower():
                logger.info("API Test cancelled.")
                self.sig_test_finished.emit(False, "Operation cancelled by user.")
            else:
                logger.error(f"API Test encountered an exception: {str(e)}")
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

    def download_image(self, url, save_path):
        self._is_cancelled = False
        self._req_session = requests.Session()

        proxy_cfg = _get_explicit_proxy_kwargs()
        if "trust_env" in proxy_cfg:
            self._req_session.trust_env = False
        elif "proxy" in proxy_cfg:
            self._req_session.proxies = {"http": proxy_cfg["proxy"], "https": proxy_cfg["proxy"]}

        logger.info(f"Downloading image from: {url}")

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            res = self._req_session.get(url, timeout=30, headers=headers)
            res.raise_for_status()

            with open(save_path, 'wb') as f:
                f.write(res.content)

            logger.info(f"Image successfully downloaded to: {save_path}")
            self.sig_image_downloaded.emit(True, url, save_path)

        except Exception as e:
            if not self._is_cancelled:
                logger.error(f"Image download failed for {url}: {str(e)}")
                self.sig_image_downloaded.emit(False, url, f"Download failed: {str(e)}")
        finally:
            self._req_session.close()

    def do_download_image(self):
        self.download_image(getattr(self, 'img_url', ''), getattr(self, 'img_save_path', ''))


class NetworkEmbeddingFunction(EmbeddingFunction):
    """Network Embedding Caller (ChromaDB Compatible)"""

    def __init__(self, api_url, api_key, model_name):
        self.api_url = api_url.rstrip('/')
        self.api_key = api_key
        self.model_name = model_name
        self.proxy_kwargs = _get_explicit_proxy_kwargs()

    def __call__(self, input: Documents) -> Embeddings:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {"input": input, "model": self.model_name}

        proxies = {}
        if "proxy" in self.proxy_kwargs:
            proxies = {"http": self.proxy_kwargs["proxy"], "https": self.proxy_kwargs["proxy"]}

        logger.debug(f"Requesting embeddings for {len(input)} documents using {self.model_name}")

        try:
            response = requests.post(
                f"{self.api_url}/v1/embeddings",
                headers=headers,
                json=payload,
                timeout=30,
                proxies=proxies,
                verify=True
            )
            response.raise_for_status()
            data = response.json()
            return [item["embedding"] for item in data["data"]]
        except requests.exceptions.RequestException as e:
            logger.error(f"Network Embedding Error: {str(e)}")
            raise RuntimeError(f"Failed to fetch embeddings from network: {str(e)}")


class NetworkRerankerFunction:
    """Network Reranker Caller (Compatible with standard Rerank APIs)"""

    def __init__(self, api_url, api_key, model_name):
        self.api_url = api_url.rstrip('/')
        self.api_key = api_key
        self.model_name = model_name
        self.proxy_kwargs = _get_explicit_proxy_kwargs()

    def rerank(self, query: str, docs: list) -> list:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model_name,
            "query": query,
            "documents": docs
        }

        proxies = {}
        if "proxy" in self.proxy_kwargs:
            proxies = {"http": self.proxy_kwargs["proxy"], "https": self.proxy_kwargs["proxy"]}

        logger.debug(f"Requesting rerank for {len(docs)} documents using {self.model_name}")

        try:
            response = requests.post(
                f"{self.api_url}/v1/rerank",
                headers=headers,
                json=payload,
                timeout=30,
                proxies=proxies,
                verify=True
            )
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
        except requests.exceptions.RequestException as e:
            logger.error(f"Network Reranker Error: {str(e)}")
            # Return empty list to prevent application crash during a search
            return []