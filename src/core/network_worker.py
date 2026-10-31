import os
import logging
import random
import re
import threading
import time
from typing import TYPE_CHECKING, Dict, Optional, Tuple
from urllib.parse import urlsplit

import requests
import httpx
from PySide6.QtCore import QObject, Signal

from src.core.config_manager import ConfigManager
from src.core.platform_env import is_windows

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


#: 默认直连（绕过代理）的机构数据库域名后缀。
#:
#: TAIR（``arabidopsis.org``）这类资源按**机构出口 IP** 授权：机构网内的机器不登录
#: 也能用订阅级功能，而一经代理就换成了代理出口 IP，权限反而消失。因此默认直连，
#: 由设置页的开关与主机列表调整（见 :func:`direct_hosts`）。只放域名后缀，匹配时按
#: 完整域名或子域判断，避免 ``evil-arabidopsis.org`` 这类仿冒后缀蹭到豁免。
DEFAULT_DIRECT_HOSTS = ("arabidopsis.org",)


def direct_hosts() -> Tuple[str, ...]:
    """需要直连（绕过代理）的主机后缀；开关关闭时返回空元组。

    配置键与界面同向：``institutional_use_proxy``（默认 **关** = 直连）与
    ``institutional_hosts``（用户增删，留空即用 :data:`DEFAULT_DIRECT_HOSTS`）。
    刻意不用"institutional_direct"这种反向命名：设置页两个代理开关都写成"使用代理"，
    代码里若一个取反、一个不取反，改一处漏一处就是这类 bug 的来源。
    """
    cfg = ConfigManager().user_settings
    if bool(cfg.get("institutional_use_proxy", False)):
        return ()
    raw = cfg.get("institutional_hosts")
    hosts = [str(item).strip().lower().lstrip(".") for item in raw] if isinstance(raw, list) else []
    hosts = [item for item in hosts if item]
    if not hosts:
        hosts = [host.lower() for host in DEFAULT_DIRECT_HOSTS]
    out: list = []
    for host in hosts:
        if host not in out:
            out.append(host)
    return tuple(out)


def is_direct_host(url: str) -> bool:
    """该 URL 是否属于"按机构 IP 授权、必须直连"的主机。"""
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    for suffix in direct_hosts():
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def create_robust_session(url: str = ""):
    """
    创建一个真正强壮的 Session，
    自动挂载全局代理配置，并使用底层 TLS 指纹 + 严格匹配的 Header 伪装真实浏览器。

    :param url: 即将请求的地址；属于机构数据库主机（见 :func:`is_direct_host`）时
        返回**免代理**会话。这类主机按机构出口 IP 授权，走代理会失去订阅级权限。

    当 curl_cffi 在本机不可用（未安装 / TLS 库加载失败，见 :func:`cffi_disabled`）时，
    退化为标准 ``requests.Session`` + 随机浏览器 Header，行为与"未安装 curl_cffi"
    的既有分支完全一致。
    """
    direct = bool(url) and is_direct_host(url)
    if direct:
        logger.debug(f"Direct (proxy-bypassed) session for institutional host: {url[:120]}")
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

    # 代理挂载只有一处实现（含"机构数据库直连"的豁免）：调用点不重复判断模式。
    # 注意不能只靠 NO_PROXY 环境变量：代理一旦写在 session.proxies 上，requests 就
    # 不再查环境变量，NO_PROXY 对这类会话完全无效。
    apply_proxy_to_session(session, url)
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


#: 代理模式的合法取值（默认 ``system``：跟随操作系统 / 环境变量的代理设置）。
PROXY_MODES = ("system", "off", "custom")

#: 需要被代理设置接管的变量：大小写两份都写，requests / httpx / curl 各读各的。
_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                   "http_proxy", "https_proxy", "all_proxy")
_NO_PROXY_KEYS = ("NO_PROXY", "no_proxy")


def _settings(settings: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    """取配置字典。

    允许调用方直接传入 ``user_settings``：``ConfigManager`` 的初始化链里会调用
    :func:`proxy_env_overrides`，而 ``ConfigManager()`` 在初始化完成前会重新进入
    ``__init__``——那时再去构造新实例就是无限递归。传字典即可完全避开。
    """
    return settings if settings is not None else ConfigManager().user_settings


def proxy_mode(settings: Optional[Dict[str, object]] = None) -> str:
    """当前代理模式；非法值（含旧配置缺失）一律按 ``system`` 处理。"""
    raw = str(_settings(settings).get("proxy_mode", "system") or "").strip().lower()
    return raw if raw in PROXY_MODES else "system"


def _custom_proxy_url(settings: Optional[Dict[str, object]] = None) -> str:
    """用户在 ``custom`` 模式下填写的代理地址。"""
    return str(_settings(settings).get("proxy_url", "") or "").strip()


def _windows_system_proxy() -> str:
    """Windows 的 WinINET 代理（系统设置 → 代理）。

    Windows 的系统代理不体现在环境变量里，只在注册表：
    ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings`` 的
    ``ProxyEnable`` 与 ``ProxyServer``。不读它，“使用系统代理”在 Windows 上等于没说。
    """
    try:
        import winreg
    except ImportError:      # 非 Windows 上不该走到这里
        return ""
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not int(enabled):
                return ""
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except (OSError, ValueError) as e:
        logger.debug(f"Cannot read the Windows proxy settings: {e}")
        return ""

    text = str(server or "").strip()
    if not text:
        return ""
    # 形如 "host:8080" 或 "http=host:1;https=host:2"（按协议分别指定）。
    if "=" in text:
        scheme_map = {}
        for item in text.split(";"):
            name, _, value = item.partition("=")
            if name.strip() and value.strip():
                scheme_map[name.strip().lower()] = value.strip()
        text = scheme_map.get("https") or scheme_map.get("http") or ""
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    return text


def system_proxy_url() -> str:
    """操作系统侧的代理地址（取不到返回空串）。

    Windows 读注册表，其余平台读进程环境变量——命令行程序所说的“系统代理”就是这些
    变量。这里只做发现，不做写入；写入见 :func:`proxy_env_overrides`。
    """
    if is_windows():
        found = _windows_system_proxy()
        if found:
            return found
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "ALL_PROXY", "all_proxy"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def proxy_env_overrides(settings: Optional[Dict[str, object]] = None) -> Dict[str, str]:
    """按当前代理模式给出**要写入的环境变量**；值为空串表示应删除该变量。

    ``system`` 只补 Windows 注册表里发现的代理：其他平台的系统代理本来就是环境变量，
    再“写一遍”会把用户继承来的设置覆盖成自己的，反而丢信息。

    :param settings: 已知的 ``user_settings``；配置初始化过程中必须传，否则会重入
        构造 ``ConfigManager``。
    """
    mode = proxy_mode(settings)
    if mode == "system":
        found = _windows_system_proxy() if is_windows() else ""
        if not found:
            return {}
        return {key: found for key in _PROXY_ENV_KEYS}
    if mode == "custom":
        # 这里也必须带 settings：否则又回到构造 ConfigManager 的老路（见 docstring）。
        url = _custom_proxy_url(settings)
        if not url:
            return {key: "" for key in _PROXY_ENV_KEYS + _NO_PROXY_KEYS}
        overrides = {key: url for key in _PROXY_ENV_KEYS}
        overrides.update({key: "" for key in _NO_PROXY_KEYS})
        return overrides
    # off：连环境变量里的代理也不认，显式声明"全部直连"。
    overrides = {key: "" for key in _PROXY_ENV_KEYS}
    overrides.update({key: "*" for key in _NO_PROXY_KEYS})
    return overrides


def apply_proxy_env(target: Dict[str, str], overrides: Dict[str, str]) -> None:
    """把 :func:`proxy_env_overrides` 的结果应用到任意环境字典（本进程或子进程副本）。"""
    for key, value in overrides.items():
        if value:
            target[key] = value
        else:
            target.pop(key, None)


def apply_proxy_to_session(session, url: str = "") -> None:
    """把代理设置落到 requests / curl_cffi 会话上（唯一实现）。

    :param url: 即将请求的地址；机构数据库主机（TAIR 等）按机构出口 IP 授权，
        必须直连，见 :func:`is_direct_host`。

    ``system`` 模式优先**显式**挂载发现到的代理，而不是指望底层库读环境变量：
    curl_cffi 各版本对 env 代理的支持并不一致，显式设置才可预期。
    """
    if url and is_direct_host(url):
        session.trust_env = False
        session.proxies = {}
        logger.debug(f"Direct (proxy-bypassed) session for institutional host: {url[:120]}")
        return

    mode = proxy_mode()
    if mode == "custom":
        url = _custom_proxy_url()
        session.trust_env = False
        session.proxies = {"http": url, "https": url} if url else {}
        return
    if mode == "off":
        session.trust_env = False
        session.proxies = {}
        return

    discovered = system_proxy_url()
    if discovered:
        session.trust_env = False
        session.proxies = {"http": discovered, "https": discovered}
    else:
        session.trust_env = True
        session.proxies = {}


def httpx_proxy_kwargs() -> Dict[str, object]:
    """httpx 客户端的代理参数（任务层不再各自判断 ``proxy_mode``）。"""
    return dict(_get_explicit_proxy_kwargs())


def requests_proxy_arg() -> Dict[str, str]:
    """``requests`` 单次调用要用的 ``proxies`` 映射。

    用于不便持有会话的零散调用（embedding / rerank）。空串表示"别用代理"，
    ``{}`` 表示"交给 requests 按环境变量决定"——与 :func:`apply_proxy_to_session`
    的三态口径一致。
    """
    mode = proxy_mode()
    if mode == "custom":
        url = _custom_proxy_url()
        return {"http": url, "https": url} if url else {"http": "", "https": ""}
    if mode == "off":
        return {"http": "", "https": ""}
    discovered = system_proxy_url()
    return {"http": discovered, "https": discovered} if discovered else {}


def setup_global_network_env():
    _ensure_ca_bundle()
    hf_mirror = ConfigManager().user_settings.get("hf_mirror", "").strip()
    if hf_mirror:
        os.environ["HF_ENDPOINT"] = hf_mirror
        logger.debug(f"HF_ENDPOINT set to: {hf_mirror}")
    else:
        os.environ.pop("HF_ENDPOINT", None)

    overrides = proxy_env_overrides()
    apply_proxy_env(os.environ, overrides)
    logger.debug("Global proxy mode '%s' applied (%d env var(s) touched).",
                 proxy_mode(), len(overrides))


def _get_explicit_proxy_kwargs():
    """按代理模式给出请求库的参数（唯一判定点，调用方不自己看 proxy_mode）。

    * ``custom`` —— ``{"proxy": url}``；没填地址等同于直连；
    * ``off``    —— ``{"trust_env": False}``：连环境变量都不读；
    * ``system`` —— ``{"trust_env": True}``：交给库去读环境/系统代理。
    """
    mode = proxy_mode()
    if mode == "custom":
        url = _custom_proxy_url()
        return {"proxy": url} if url else {"trust_env": False}
    if mode == "off":
        return {"trust_env": False}
    return {"trust_env": True}


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
        apply_proxy_to_session(self._req_session)

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

        httpx_kwargs = {"timeout": 15.0, **httpx_proxy_kwargs()}
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
        apply_proxy_to_session(self._req_session, url)

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

    def __call__(self, input: "Documents") -> "Embeddings":
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {"input": input, "model": self.model_name}

        # 代理在调用时才算：构造时缓存会让"保存设置后改模式"对已有实例失效。
        proxies = requests_proxy_arg()

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

        # 同 embedding：代理在调用时按当前模式取，不在构造时缓存。
        proxies = requests_proxy_arg()

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
            # 新接入的服务（arxiv / preprints 等）不在初始表里：先登记计数，
            # 否则 waiting_counts[service] += 1 会直接 KeyError。
            self.waiting_counts.setdefault(service, 0)
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