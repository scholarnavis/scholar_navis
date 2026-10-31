"""开放获取（OA）PDF 定位：DOI -> 一组**候选下载直链**。

设计口径
--------
* **多候选，不赌第一个**：同一篇论文在不同索引里的 OA 位置常常不同（出版社站点、
  机构库、PMC 镜像），其中任意一个都可能被反爬、限流或返回 HTML 中间页。旧实现
  每个来源只取一个链接就返回，第一个链接一旦不可用，整次取全文就失败——这会把
  覆盖范围悄悄收窄到"PMC 收录 + 出版社不设防"的那批期刊。现在每个来源返回全部
  可用候选，由调用方按顺序试。
* **出版社落地页兜底**：很多出版社（MDPI、Frontiers、PLOS、Springer/BMC、Wiley OA、
  IOP、SAGE 等）不在 PDF 元数据里给直链，但在落地页写了 ``citation_pdf_url``——这是
  出版社专门给文献管理器用的标准元标签。只在索引已声明"该文 OA"时才去读落地页，
  不去碰付费墙页面。
* **来源结论分两栏**：``checked`` 是明确回答过的主机，``failures`` 是请求失败/被熔断
  的主机。调用方据此区分"确实没有 OA 副本"与"这次没查到"，绝不把故障讲成付费墙。

公开返回值（:meth:`OAFetcher.fetch_best_oa_pdf`）保持向后兼容：
``is_oa`` / ``pdf_url`` / ``landing_page_url`` / ``source`` / ``checked`` / ``failures``
以外，新增 ``pdf_urls``（按推荐顺序排列的全部候选）与 ``sources``（与之一一对应的
来源标签）。
"""

import os
import re
import urllib.parse
import logging
from html import unescape

from src.core.config_manager import ConfigManager
from src.core.network_worker import (
    create_robust_session, global_rate_limiter, is_direct_host,
    is_auth_blocked, record_auth_rejection,
)
from src.task.s2_task import is_s2_enabled, s2_request

#: 出版社落地页里指向 PDF 的元标签/链接（按命中概率排序）。
#: ``citation_pdf_url`` 是文献管理器的通用约定，覆盖 MDPI / Frontiers / PLOS /
#: Springer(Nature) / Wiley / IOP / SAGE / Copernicus 等主流出版社的 OA 页面。
_PDF_META_PATTERNS = (
    re.compile(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
               re.IGNORECASE),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
               re.IGNORECASE),
    re.compile(r'<link[^>]+type=["\']application/pdf["\'][^>]+href=["\']([^"\']+)["\']',
               re.IGNORECASE),
    re.compile(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+type=["\']application/pdf["\']',
               re.IGNORECASE),
)

#: 落地页最多读这么多字符再解析（有的出版社把整本刊的目录塞进首页）。
_LANDING_HTML_CHARS = 400_000


class OAFetcher:
    def __init__(self):
        self.logger = logging.getLogger("OAFetcher")
        #: 机构主机用的免代理会话（懒创建）。同一次取全文会命中多个主机，直接复用
        #: 一个会话会沿用主会话的代理设置，所以这里单独持有。
        self._direct_session = None

    def is_supplement(self, url: str) -> bool:
        if not url:
            return False

        # 使用 urlparse 拆分 URL，仅在路径和参数中查找，防止域名导致误判
        parsed = urllib.parse.urlparse(url.lower())
        path_and_query = parsed.path + "?" + parsed.query
        filename = parsed.path.split('/')[-1]

        # 1. 扩充的补充文件路径/参数特征词
        supp_keywords = [
            "supp",  # 涵盖 supplementary, supplement, suppinfo 等
            "appendix",  # 附录
            "dataset",  # 数据集
            "attachment",  # 附件
            "media",  # 多媒体
            "extended",  # 扩展数据 (extended data)
            "supporting"  # 支持材料
        ]

        for kw in supp_keywords:
            if kw in path_and_query:
                return True

        # 2. 针对补充文件常见命名习惯的正则匹配
        if filename:
            if re.search(r'(?:^|[-_])(mmc\d*|s\d+|sm|som|esm\d*)\.pdf$', filename):
                return True

        return False

    def normalize_pdf_url(self, url: str) -> str:

        if not url:
            return ""

        match = re.search(r'ncbi\.nlm\.nih\.gov/articles/(PMC\d+)', url, re.IGNORECASE)
        if match:
            pmcid = match.group(1).upper()
            return f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"

        return url

    def is_valid_pdf_link(self, url: str) -> bool:
        if not url:
            return False
        url_lower = url.lower()
        if "doi.org/" in url_lower:
            return False
        if url_lower.endswith(".html") or url_lower.endswith(".htm"):
            return False
        return True

    # ------------------------------------------------------------------ 候选排序

    @staticmethod
    def looks_like_pdf_url(url: str) -> bool:
        """链接形态上是否像 PDF 直链（不做网络请求）。

        只用于**排序与短路**：像 PDF 的候选排在前面，全部候选都像 PDF 时跳过落地页
        探测。真正的判定永远是下载后看文件头，这里不承担"是不是 PDF"的结论。
        """
        parsed = urllib.parse.urlparse(str(url or "").lower())
        path = parsed.path
        if path.endswith(".pdf"):
            return True
        if "/pdf/" in path or path.endswith("/pdf"):
            return True
        query = parsed.query
        return "pdf" in query and ("format=pdf" in query or "type=pdf" in query
                                   or query.rstrip("&").endswith("pdf"))

    def _dedupe(self, items):
        """去重并排序候选 ``[(url, source), ...]``：像 PDF 的在前，顺序稳定。"""
        seen = set()
        kept = []
        for raw_url, source in items:
            url = self.normalize_pdf_url(str(raw_url or "").strip())
            if not url or url in seen:
                continue
            if self.is_supplement(url) or not self.is_valid_pdf_link(url):
                continue
            seen.add(url)
            kept.append((url, source))
        # sorted 是稳定排序：同组内保持来源给出的原始优先顺序
        kept.sort(key=lambda item: 0 if self.looks_like_pdf_url(item[0]) else 1)
        return kept

    # ------------------------------------------------------------------ 单来源

    def _openalex_candidates(self, clean_doi, request_func, openalex_api_key=""):
        """OpenAlex 的全部 OA 位置；返回 ``(候选, 是否声明 OA, 落地页)``。

        旧实现只读 ``open_access.oa_url``——那是**落地页**而非 PDF，下载必然拿到 HTML，
        于是"OpenAlex 说有 OA"却永远取不到正文。``locations[]`` 里的 ``pdf_url`` 才是
        论文直链，这里一并收集。
        """
        candidates = []
        is_oa = False
        landing = ""
        rps = 9 if openalex_api_key else 2
        try:
            global_rate_limiter.acquire("openalex", rps=rps)
            url = f"https://api.openalex.org/works/https://doi.org/{clean_doi}"
            if openalex_api_key:
                url += f"?api_key={openalex_api_key}"
            res = request_func(url, timeout=15)
            if res.status_code != 200:
                return candidates, is_oa, landing
            data = res.json() or {}
            open_access = data.get("open_access") or {}
            is_oa = bool(open_access.get("is_oa"))
            best = data.get("best_oa_location") or {}
            for loc in [best] + list(data.get("locations") or []):
                if not isinstance(loc, dict):
                    continue
                for key in ("pdf_url", "url_for_pdf"):
                    if loc.get(key):
                        candidates.append((loc[key], "OpenAlex"))
                        landing = landing or str(loc.get("landing_page_url") or "")
            # oa_url 排在最后：多数出版社给的是落地页，只有在没有真直链时才有意义。
            if open_access.get("oa_url"):
                candidates.append((open_access["oa_url"], "OpenAlex"))
            self.logger.debug("OpenAlex returned %d OA candidate(s) (is_oa=%s).",
                              len(candidates), is_oa)
        except Exception as e:
            self.logger.warning(f"OpenAlex query failed: {e}")
        return candidates, is_oa, landing

    def _unpaywall_candidates(self, encoded_doi, user_email, request_func, failures=None):
        """Unpaywall 的全部 OA 位置；返回 ``(候选, 是否声明 OA, 落地页)``。

        Unpaywall 的 ``oa_locations`` 天然是"同一篇论文的所有合法副本"，正是多候选
        的依据：出版社站点被墙时，机构库或 PMC 镜像往往还能下。

        Unpaywall 要求一个**真实可联系的邮箱**：没配置时直接跳过（它必然回 422），
        否则每次取全文都会多出一条与论文无关的"失败"，调用方就会把"没配邮箱"讲成
        "这篇论文取不到全文"。
        """
        candidates = []
        is_oa = False
        landing = ""
        if not str(user_email or "").strip():
            self.logger.info("Unpaywall skipped: no contact email configured "
                             "(Settings -> NCBI email).")
            return candidates, is_oa, landing
        try:
            global_rate_limiter.acquire("unpaywall", rps=2)
            email = urllib.parse.quote(str(user_email or ""))
            res = request_func(f"https://api.unpaywall.org/v2/{encoded_doi}?email={email}",
                               timeout=15)
            if res.status_code == 422:
                # 邮箱被判定无效（占位邮箱/格式不对）：属于配置问题，不算检索失败。
                self.logger.warning("Unpaywall rejected the contact email (HTTP 422); "
                                    "set a real email in Settings to use it.")
                if failures is not None:
                    failures[:] = [f for f in failures
                                   if not str(f).startswith("api.unpaywall.org")]
                return candidates, is_oa, landing
            if res.status_code != 200:
                return candidates, is_oa, landing
            data = res.json() or {}
            is_oa = bool(data.get("is_oa"))
            best = data.get("best_oa_location") or {}
            for loc in [best] + list(data.get("oa_locations") or []):
                if not isinstance(loc, dict):
                    continue
                if loc.get("url_for_pdf"):
                    candidates.append((loc["url_for_pdf"], "Unpaywall"))
                landing = landing or str(loc.get("url_for_landing_page") or "")
            self.logger.debug("Unpaywall returned %d OA candidate(s) (is_oa=%s).",
                              len(candidates), is_oa)
        except Exception as e:
            self.logger.warning(f"Unpaywall query failed: {e}")
        return candidates, is_oa, landing

    # ------------------------------------------------------------------ 落地页

    def _landing_page_pdf(self, clean_doi, request_func, landing_url=""):
        """读论文落地页，取出版社声明的 PDF 直链（``citation_pdf_url`` 等）。

        只在索引已声明该文 OA 时调用：这既避免去读付费墙页面，也避免把"没有权限"
        的 401/403 记成全文探测失败。任何异常都只记 debug——这是兜底通道，
        不该让主流程的"来源是否失败"结论变脏。
        """
        target = landing_url or f"https://doi.org/{clean_doi}"
        try:
            res = request_func(
                target,
                headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"},
                timeout=20)
            if res.status_code != 200:
                self.logger.debug("Landing page %s returned HTTP %s.", target,
                                  res.status_code)
                return ""
            html = str(getattr(res, "text", "") or "")[:_LANDING_HTML_CHARS]
            base = str(getattr(res, "url", "") or target)
            for pattern in _PDF_META_PATTERNS:
                match = pattern.search(html)
                if not match:
                    continue
                raw = unescape(match.group(1)).strip()
                if not raw:
                    continue
                url = urllib.parse.urljoin(base, raw)
                if self.is_supplement(url) or not self.is_valid_pdf_link(url):
                    continue
                self.logger.info("Publisher landing page exposes a PDF link: %s", url)
                return url
        except Exception as e:
            self.logger.debug("Landing-page PDF probe failed for %s: %s", clean_doi, e)
        return ""

    # ------------------------------------------------------------------ 主入口

    def fetch_best_oa_pdf(self, doi: str, user_email: str = '', ncbi_api_key: str = "", request_func=None,
                          source: str = "auto") -> dict:
        """按 DOI 找 OA PDF：返回首个推荐链接与**全部候选**（``pdf_urls``）。

        ``source="auto"`` 时先合并 OpenAlex + Unpaywall 的候选（两者都是廉价的元数据
        接口），再按需补 PMC / Semantic Scholar；任何一个来源给出候选就立即返回，
        不再为了"多问一家"而拖慢调用。真正的失败与否由调用方逐个试下载决定。
        """
        if not doi:
            self.logger.warning("Empty DOI provided. Aborting fetch.")
            return {"is_oa": False}

        clean_doi = doi.replace("https://doi.org/", "").replace("http://dx.doi.org/", "").strip()
        encoded = urllib.parse.quote(clean_doi)
        landing_url = f"https://doi.org/{clean_doi}"

        self.logger.info(f"Starting OA search for DOI: {clean_doi} | Forced Source: {source}")

        checked: list = []
        failures: list = []

        if request_func is None:
            session = create_robust_session()

            def default_request(url, headers=None, timeout=15):
                # 鉴权熔断：本轮内该 host 已累计 2 次 401/403 时零开销跳过
                # （OpenAlex/Unpaywall/NCBI idconv/PMC 各 host 独立计数；
                # NCBI 的 www host 与 Entrez 在熔断器内归一为同一服务键）。
                host = urllib.parse.urlsplit(url).netloc
                if is_auth_blocked(url):
                    failures.append(f"{host} (auth-rejected earlier this round; skipped)")
                    raise RuntimeError(
                        f"Request skipped: '{url}' blocked by the "
                        f"auth-rejection breaker for the current chat round.")
                # 机构主机（TAIR 等）按机构出口 IP 授权，必须直连：代理出口 IP 会
                # 让订阅级权限消失。会话按主机选，头部沿用主会话那份浏览器伪装。
                active = session
                if is_direct_host(url):
                    if self._direct_session is None:
                        self._direct_session = create_robust_session(url)
                        self._direct_session.headers.update(session.headers)
                        self.logger.info("Institutional host detected; using a "
                                         "proxy-bypassed session for it.")
                    active = self._direct_session
                req_headers = active.headers.copy()
                if headers:
                    req_headers.update(headers)
                res = active.get(url, headers=req_headers, timeout=timeout)
                # 401/403 计入全局鉴权熔断器（阈值 2，本轮内生效）。
                # 注：若调用方传入自定义 request_func（如 mcp_request），
                # 计数由该底层实现负责，此处不重复计数。
                if res.status_code in (401, 403):
                    record_auth_rejection(
                        url, context=f"HTTP {res.status_code} via OAFetcher.")
                if res.status_code == 200:
                    if host not in checked:
                        checked.append(host)
                else:
                    failures.append(f"{host} (HTTP {res.status_code})")
                return res

            request_func = default_request

        candidates = []
        is_oa_declared = False
        resolver_landing = ""

        # ---- 元数据索引：OpenAlex + Unpaywall（合并候选，不早退）----
        if source in ["auto", "openalex"]:
            self.logger.debug("Querying OpenAlex...")
            found, oa, landing = self._openalex_candidates(
                clean_doi, request_func, os.environ.get("OPENALEX_API_KEY", ""))
            candidates.extend(found)
            is_oa_declared = is_oa_declared or oa
            resolver_landing = resolver_landing or landing

        if source in ["auto", "unpaywall"]:
            self.logger.debug("Querying Unpaywall...")
            found, oa, landing = self._unpaywall_candidates(
                encoded, user_email, request_func, failures)
            candidates.extend(found)
            is_oa_declared = is_oa_declared or oa
            resolver_landing = resolver_landing or landing

        deduped = self._dedupe(candidates)

        # 索引声明 OA，但没有任何一条像 PDF 直链（典型：只给了出版社落地页）：
        # 去落地页读 citation_pdf_url，这是覆盖非 PMC 期刊的关键一步。
        if is_oa_declared and not any(self.looks_like_pdf_url(u) for u, _ in deduped):
            extra = self._landing_page_pdf(clean_doi, request_func, resolver_landing)
            if extra:
                deduped = self._dedupe(deduped + [(extra, "Publisher landing page")])

        if deduped:
            return self._oa_result(deduped, landing_url, checked, failures)

        # ---- PubMed Central（NCBI 的 OA 嗅探）：只需要邮箱，API key 可选 ----
        if source in ["auto", "pubmed", "pmc"] and user_email:
            self.logger.debug("Querying PMC API...")
            found = self._pmc_candidates(encoded, user_email, ncbi_api_key, request_func)
            deduped = self._dedupe(found)
            if deduped:
                return self._oa_result(deduped, landing_url, checked, failures)

        # ---- Semantic Scholar ----
        if source in ["auto", "semantic_scholar"]:
            self.logger.debug("Querying Semantic Scholar...")
            found = self._s2_candidates(clean_doi, request_func, checked, failures)
            deduped = self._dedupe(found)
            if deduped:
                return self._oa_result(deduped, landing_url, checked, failures)

        # 落地页只在上面探测过一次（此处不再重复请求同一个页面）：走完 PMC / S2
        # 仍无候选，就说明这篇确实没有可下载的 OA 副本。

        self.logger.info(f"No Open Access PDF found (Tested source: {source}); "
                         f"checked={checked} failures={failures}")
        return {"is_oa": False, "pdf_url": "", "pdf_urls": [], "sources": [],
                "landing_page_url": resolver_landing or landing_url,
                "source": source, "checked": checked, "failures": failures}

    def _oa_result(self, candidates, landing_url, checked, failures) -> dict:
        """把候选列表整成对外契约（``pdf_url`` 取推荐顺序里的第一条）。"""
        urls = [url for url, _ in candidates]
        sources = [source for _, source in candidates]
        self.logger.info("OA candidates for %s: %d -> %s", landing_url,
                         len(urls), " | ".join(urls[:4]))
        return {"is_oa": True,
                "pdf_url": urls[0],
                "pdf_urls": urls,
                "sources": sources,
                "landing_page_url": landing_url,
                "source": sources[0] if sources else "auto",
                "checked": checked, "failures": failures}

    def _pmc_candidates(self, encoded_doi, user_email, ncbi_api_key, request_func):
        """PMC 的 OA 服务给出的 PDF 候选（拿不到 PMCID 或没有 OA 包时返回空表）。"""
        candidates = []
        ncbi_rps = 9 if ncbi_api_key else 4
        try:
            global_rate_limiter.acquire("ncbi", rps=ncbi_rps)
            idconv_url = (f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
                          f"?ids={encoded_doi}&format=json&email={user_email}")
            if ncbi_api_key:
                idconv_url += f"&api_key={ncbi_api_key}"

            conv_res = request_func(idconv_url, timeout=15)
            if conv_res.status_code != 200:
                return candidates
            records = conv_res.json().get("records", [])
            if not records or "pmcid" not in records[0]:
                return candidates
            pmcid = records[0]["pmcid"]

            oa_url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmcid}"
            if ncbi_api_key:
                oa_url += f"&api_key={ncbi_api_key}"

            oa_res = request_func(oa_url, timeout=15)
            if oa_res.status_code == 200 and "<OA>" in oa_res.text:
                matches = re.findall(r'<link[^>]+format="pdf"[^>]+href="([^"]+)"', oa_res.text)
                for match in matches:
                    candidates.append((match.replace("ftp://", "https://"), "PubMed_OA"))
                if not candidates:
                    candidates.append((
                        f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/",
                        "PubMed_OA"))
                self.logger.info(f"OA candidates via PMC: {pmcid} ({len(candidates)})")
        except Exception as e:
            self.logger.warning(f"PMC query failed: {e}")
        return candidates

    def _s2_candidates(self, clean_doi, request_func, checked, failures):
        """Semantic Scholar 的 OA PDF 候选。"""
        candidates = []
        try:
            if not is_s2_enabled():
                self.logger.debug("S2 OA skipped: S2 not enabled")
                return candidates

            config_mgr = ConfigManager()
            s2_rate_str = config_mgr.user_settings.get("s2_rate_limit") or os.environ.get(
                "S2_RATE_LIMIT") or "1.0"
            s2_rate = float(s2_rate_str)
            global_rate_limiter.acquire("s2", rps=s2_rate)

            res = s2_request(
                "GET",
                f"https://api.semanticscholar.org/graph/v1/paper/DOI:{clean_doi}"
                f"?fields=isOpenAccess,openAccessPdf")

            if res is None:
                self.logger.warning("S2 OA request returned None")
                raise ValueError("S2 request failed - response is None")

            res.raise_for_status()
            if "api.semanticscholar.org" not in checked:
                checked.append("api.semanticscholar.org")

            response_text = res.text
            if not response_text or len(response_text.strip()) == 0:
                self.logger.warning("S2 OA response is empty")
                raise ValueError("S2 response is empty")

            data = res.json()
            if not data or not isinstance(data, dict):
                self.logger.warning("S2 OA response data is empty or invalid")
                raise ValueError("S2 response data is invalid")

            if data.get("isOpenAccess") and data.get("openAccessPdf"):
                raw_pdf_url = data["openAccessPdf"].get("url", "")
                if raw_pdf_url:
                    candidates.append((self.normalize_pdf_url(raw_pdf_url),
                                       "Semantic Scholar"))
        except Exception as e:
            failures.append(f"api.semanticscholar.org ({e})")
            self.logger.warning(f"Semantic Scholar check/query failed: {repr(e)}")
        return candidates


if __name__ == "__main__":
    from src.core.network_worker import setup_global_network_env
    setup_global_network_env()

    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

    print("\n=== OA PDF Sniffer Testing Tool ===")
    test_doi = input("Enter DOI to test (e.g., 10.1016/j.xplc.2025.101617): ").strip()
    test_email = input(
        "Enter your NCBI/Unpaywall Email (or leave blank for test@example.com): ").strip() or "test@example.com"
    test_s2_key = input("Enter Semantic Scholar API Key (or leave blank to skip S2): ").strip()

    fetcher = OAFetcher()
    print("\n[Executing Searches...]")

    result = fetcher.fetch_best_oa_pdf(test_doi, test_email, test_s2_key)

    print("\n=== Final Result ===")
    import json
    print(json.dumps(result, indent=2))
