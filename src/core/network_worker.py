import os
import logging
import random
import re
import threading
import time
from typing import TYPE_CHECKING

import requests
import httpx
from PySide6.QtCore import QObject, Signal

from src.core.config_manager import ConfigManager

if TYPE_CHECKING:  # 仅用于类型注解：运行期不导入 chromadb（启动链上约 0.4 s）
    from chromadb import Documents, Embeddings

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


# --------------------------------------------------------------------------- #
#  curl_cffi 环境级故障熔断
# --------------------------------------------------------------------------- #
#: 判定"不是网络抖动，而是本机 curl_cffi 运行环境坏了"的错误特征。
#: 实测样本（NixOS 上注入的 LD_LIBRARY_PATH 与应用自带的 OpenSSL 冲突）：
#:   "curl: (35) TLS connect error: error:00000000:invalid library (0):OPENSSL_internal:invalid library (0)"
#: 这类错误与目标站点无关，重试必然再失败——每个请求都尝试一次只会得到同样
#: 的警告并白等一次 TLS 握手。
_CFFI_FATAL_MARKERS = (
    "invalid library",
    "openssl_internal",
    "undefined symbol",
    "cannot open shared object",
    "wrong elf class",
)

#: 强制禁用 curl_cffi（应急开关，例如某平台的 libcurl-impersonate 不可用）。
_CFFI_DISABLE_ENV = "SCHOLAR_NAVIS_DISABLE_CURL_CFFI"
_TRUTHY = {"1", "true", "yes", "on"}

#: 熔断状态：非空表示本进程内已确认 curl_cffi 不可用（含原因，供日志/UI 展示）。
_cffi_broken_reason = ""
_cffi_state_lock = threading.Lock()


def cffi_fatal_error(error: BaseException | str) -> bool:
    """该错误是否属于 curl_cffi 的环境级故障（应当熔断，而不是逐请求重试）。"""
    err = str(error).lower()
    return any(marker in err for marker in _CFFI_FATAL_MARKERS)


def mark_cffi_broken(reason: str) -> None:
    """把 curl_cffi 标记为"本进程内不可用"（幂等，只在首次记录日志）。

    之后 :func:`create_robust_session` 直接返回标准 ``requests.Session``，
    避免每个请求都重复触发同一个 TLS 库错误（既刷日志又浪费一次握手）。
    """
    global _cffi_broken_reason
    with _cffi_state_lock:
        if _cffi_broken_reason:
            return
        _cffi_broken_reason = reason or "unknown"
    logger.warning(
        "curl_cffi is unusable in this environment (%s). Standard requests will be used "
        "for the rest of this session; strict WAFs may block some sources.", _cffi_broken_reason)


def cffi_disabled() -> bool:
    """curl_cffi 是否已不可用（或在环境变量里被强制停用）。"""
    if os.environ.get(_CFFI_DISABLE_ENV, "").strip().lower() in _TRUTHY:
        return True
    with _cffi_state_lock:
        return bool(_cffi_broken_reason)


def create_robust_session():
    """
    创建一个真正强壮的 Session，
    自动挂载全局代理配置，并使用底层 TLS 指纹 + 严格匹配的 Header 伪装真实浏览器。

    当 curl_cffi 在本机不可用（未安装 / TLS 库加载失败，见 :func:`cffi_disabled`）时，
    退化为标准 ``requests.Session`` + 随机浏览器 Header，行为与"未安装 curl_cffi"
    的既有分支完全一致。
    """
    if HAS_CFFI and not cffi_disabled():
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
        # 这里必须显式用标准 requests：curl_cffi 已安装但熔断/被禁用时，
        # cffi_requests.Session() 仍会走它自带的 libcurl（同一个 TLS 库故障），
        # 等于没退避成功。
        session = requests.Session()
        session.headers.update(get_random_browser_headers())

    proxy_cfg = _get_explicit_proxy_kwargs()
    if "trust_env" in proxy_cfg:
        session.trust_env = False
    elif "proxy" in proxy_cfg:
        session.proxies = {"http": proxy_cfg["proxy"], "https": proxy_cfg["proxy"]}

    return session


def _ensure_ca_bundle() -> None:
    """让 stdlib 的 HTTPS（urllib/ssl）也能找到可用的 CA 根证书。

    Linux 上 ``ssl.get_default_verify_paths()`` 依赖发行版把根证书装进
    ``/etc/ssl/certs``；NixOS（以及本应用在 steam-run 的 FHS 环境里重启之后）
    该路径常常不可用。实测：``cafile=None``、``/etc/ssl/certs`` 无法完成校验，
    于是所有走 urllib 的请求都以
    ``CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain``
    失败。受影响的主要是 Bio.Entrez（PubMed）——它走 urllib，而 requests /
    httpx / curl_cffi 自带 certifi 不受影响，症状就是"Crossref/OpenAlex 正常，
    PubMed 却一条都搜不到"。

    仅在系统未显式配置（``SSL_CERT_FILE`` 为空）且系统 cafile 缺失时，
    把 SSL_CERT_FILE / REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE 指向 certifi 的
    bundle。用户自带的企业 CA（已设置这些变量）会被尊重，不做覆盖。
    """
    if os.environ.get("SSL_CERT_FILE"):
        return  # 用户/系统已显式指定，保持不动

    try:
        import ssl
        default_cafile = ssl.get_default_verify_paths().cafile
    except Exception:
        default_cafile = None

    if default_cafile and os.path.exists(default_cafile):
        return  # 系统根证书可用

    try:
        import certifi
        bundle = certifi.where()
    except Exception as e:  # certifi 未安装：无从兜底，交由 OpenSSL 报错
        logger.debug(f"certifi unavailable, cannot pin CA bundle: {e}")
        return

    if not os.path.exists(bundle):
        return

    applied = False
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        if not os.environ.get(key):
            os.environ[key] = bundle
            applied = True

    if applied:
        logger.info(f"System CA store unavailable; pinned HTTPS CA bundle to certifi: {bundle}")


def setup_global_network_env():
    _ensure_ca_bundle()
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
                                        f"API connectivity is excellent!\nModel '{model_name}' responded successfully:\n'{reply}'")

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
        # 鉴权熔断前置拦截：本轮内该图床已累计 2 次 401/403 时零开销跳过
        # （每个气泡的图片下载独立线程，熔断避免反复撞被拒的图床）
        if url and is_auth_blocked(url):
            self.sig_image_downloaded.emit(
                False, url,
                "Skipped: image host blocked by auth-rejection breaker (current chat round).")
            return
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
            # 401/403 计入全局鉴权熔断器（阈值 2，本轮内生效）
            if res.status_code in (401, 403):
                record_auth_rejection(url, context=f"HTTP {res.status_code} via image download.")
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


class NetworkEmbeddingFunction:
    """Network Embedding Caller (ChromaDB Compatible)。

    刻意**不继承** ``chromadb.EmbeddingFunction``：chromadb 只做鸭子类型校验
    （``chromadb.api.types.validate_embedding_function`` 比较 ``__call__`` 的
    参数名，不做 isinstance 检查），而继承会让本模块在导入期拉起 chromadb——
    本模块是启动链的一环（``setup_global_network_env``），代价约 0.4 s。
    接口要求：``__call__(self, input: Documents) -> Embeddings``，参数名必须是
    ``input``。
    """

    def __init__(self, api_url, api_key, model_name):
        self.api_url = api_url.rstrip('/')
        self.api_key = api_key
        self.model_name = model_name
        self.proxy_kwargs = _get_explicit_proxy_kwargs()

    def __call__(self, input: "Documents") -> "Embeddings":
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
            embed_url = f"{self.api_url}/v1/embeddings"
            # 鉴权熔断：本轮内该 embedding 服务累计 2 次 401/403 后零开销跳过
            # （KB 检索链路，key 失效时避免每轮检索反复撞）
            if is_auth_blocked(embed_url):
                raise RuntimeError(
                    "Embedding request skipped: service blocked by the "
                    "auth-rejection breaker for the current chat round.")
            response = requests.post(
                embed_url,
                headers=headers,
                json=payload,
                timeout=30,
                proxies=proxies,
                verify=True
            )
            if response.status_code in (401, 403):
                record_auth_rejection(
                    embed_url, context=f"HTTP {response.status_code} via embeddings API.")
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
            rerank_url = f"{self.api_url}/v1/rerank"
            # 鉴权熔断：本轮内该 rerank 服务累计 2 次 401/403 后零开销跳过
            if is_auth_blocked(rerank_url):
                # 返回空列表与既有错误路径语义一致（检索降级不崩溃）
                logger.error("Rerank request skipped: service blocked by the "
                             "auth-rejection breaker for the current chat round.")
                return []
            response = requests.post(
                rerank_url,
                headers=headers,
                json=payload,
                timeout=30,
                proxies=proxies,
                verify=True
            )
            if response.status_code in (401, 403):
                record_auth_rejection(
                    rerank_url, context=f"HTTP {response.status_code} via rerank API.")
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
        except requests.exceptions.RequestException as e:
            logger.error(f"Network Reranker Error: {str(e)}")
            # Return empty list to prevent application crash during a search
            return []


class GlobalRateLimiter:
    def __init__(self):
        self.locks = {
            "ncbi": threading.Lock(),
            "openalex": threading.Lock(),
            "github": threading.Lock(),
            "s2": threading.Lock()
        }
        self.last_called = {k: 0.0 for k in self.locks}
        self.waiting_counts = {k: 0 for k in self.locks}
        self.queue_lock = threading.Lock()

    def acquire(self, service, rps=None, rph=None):
        """按服务限速；必要时阻塞到下一个可用时隙。

        日志只在**真的发生等待**时记录：旧实现每次入队都打一条
        ``[RateLimiter] X Queue: N request(s) waiting``，而这条信息在入队时打印、
        并非等待结果，并发检索（一次会话可同时 4~8 路）时会刷出成片无意义的队列
        深度，把真正的告警淹没。未发生等待的请求降为 DEBUG。
        """
        limit_val = rps if rps else rph
        limit_type = "rps" if rps else "rph"

        with self.queue_lock:
            self.waiting_counts[service] += 1
            current_queue = self.waiting_counts[service]
            # 每个服务固定一把锁：旧写法 locks.get(service, threading.Lock()) 在
            # 服务名未登记时每次返回**新锁**，等于完全没有限流。
            lock = self.locks.setdefault(service, threading.Lock())

        try:
            with lock:
                min_interval = (1.0 / rps) if rps else (3600.0 / rph)
                wait = min_interval - (time.time() - self.last_called.get(service, 0.0))
                if wait > 0:
                    logger.info(
                        f"[RateLimiter] {service.upper()}: sleeping {wait:.2f}s "
                        f"(queued={current_queue}, limit={limit_val} {limit_type}).")
                    time.sleep(wait)
                elif current_queue > 1:
                    logger.debug(
                        f"[RateLimiter] {service.upper()}: {current_queue} concurrent, "
                        f"no wait needed (limit={limit_val} {limit_type}).")
                self.last_called[service] = time.time()
        finally:
            with self.queue_lock:
                self.waiting_counts[service] -= 1


global_rate_limiter = GlobalRateLimiter()


class AuthRejectionBreaker:
    """按服务的鉴权拒绝熔断器（401/403 等非网络原因的服务端拒绝）。

    与 GlobalRateLimiter 同层：429/超时等瞬时性问题交由重试与限流处理，
    而鉴权类拒绝（key 无效/被拒/IP 被服务端封禁）在本轮内重试不可能成功，
    继续请求只会白白消耗限流配额并拖慢对话。

    语义（按用户规格）：
    - 同一服务（按 URL host 归一）累计 2 次鉴权拒绝即熔断；
    - 熔断后本轮内所有经过此层的请求对该服务立即失败（零网络开销），
      其他服务不受影响；
    - 计数在每次新对话发送时整体复位（ChatGenerationTask._execute 入口），
      即熔断只在"本轮对话"内生效，不跨对话。

    线程安全：计数操作全程持锁（Agent 工具循环与 Deep Mode 并行子任务
    都会在工作线程触发）。
    """

    THRESHOLD = 2

    def __init__(self):
        self._lock = threading.Lock()
        self._counts = {}  # service(host) -> 拒绝次数

    # 服务商别名归一：同一服务商的多个 host 共享一个熔断键（鉴权按服务商
    # 生效——NCBI key 被 ban 时 eutils（Entrez）与 www（idconv/PMC）同步熔断）
    _HOST_ALIASES = {
        "eutils.ncbi.nlm.nih.gov": "ncbi",
        "www.ncbi.nlm.nih.gov": "ncbi",
    }

    @staticmethod
    def _key_for_url(url):
        """按 URL 推导服务键：host 归一 + 服务商别名映射。

        - 无 scheme 的裸 host（如 S2TaskManager 传入的
          "api.semanticscholar.org"）自动补 https:// 后解析；
        - 未知 host 直接用 host 本身作键。
        """
        try:
            raw = str(url or "").strip()
            if raw and "://" not in raw:
                raw = f"https://{raw}"
            from urllib.parse import urlparse
            host = (urlparse(raw).hostname or "unknown").lower()
            return AuthRejectionBreaker._HOST_ALIASES.get(host, host)
        except Exception:
            return "unknown"

    def reset(self):
        with self._lock:
            self._counts.clear()

    def reset_service(self, url_or_key):
        """仅复位单个服务的计数（如设置更新后恢复该源的可用性）。"""
        key = self._key_for_url(url_or_key) if "://" in str(url_or_key) else str(url_or_key)
        with self._lock:
            self._counts.pop(key, None)

    def record(self, url) -> bool:
        """记录一次鉴权拒绝；返回是否已达到熔断阈值。"""
        key = self._key_for_url(url)
        with self._lock:
            count = self._counts.get(key, 0) + 1
            self._counts[key] = count
            return count >= self.THRESHOLD

    def is_blocked(self, url) -> bool:
        key = self._key_for_url(url)
        with self._lock:
            return self._counts.get(key, 0) >= self.THRESHOLD


auth_breaker = AuthRejectionBreaker()


def record_auth_rejection(url, context=""):
    """向全局熔断器记录一次鉴权拒绝，并在达到阈值时输出明确日志。

    供 mcp_request / s2_task 等网络入口在拿到 401/403 响应时调用；
    调用方自身的 raise/重试语义保持不变。
    """
    blocked_now = auth_breaker.record(url)
    host = AuthRejectionBreaker._key_for_url(url)
    if blocked_now:
        logger.warning(
            f"[AuthBreaker] '{host}' reached {AuthRejectionBreaker.THRESHOLD} auth "
            f"rejections; further requests to it are SKIPPED for the current chat round "
            f"(reset on next conversation). {context}")
    else:
        logger.info(f"[AuthBreaker] '{host}' auth rejection recorded. {context}")
    return blocked_now


def is_auth_blocked(url) -> bool:
    """该 URL 所属服务是否已被本轮鉴权熔断。"""
    return auth_breaker.is_blocked(url)