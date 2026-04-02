import os
import urllib.parse
import re
import logging

from src.core.config_manager import ConfigManager
from src.core.network_worker import create_robust_session, global_rate_limiter
from src.task.s2_task import is_s2_enabled, s2_request


class OAFetcher:
    def __init__(self):
        self.logger = logging.getLogger("OAFetcher")

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

    def fetch_best_oa_pdf(self, doi: str, user_email: str = '', ncbi_api_key: str = "", request_func=None,
                          source: str = "auto") -> dict:
        if not doi:
            self.logger.warning("Empty DOI provided. Aborting fetch.")
            return {"is_oa": False}

        clean_doi = doi.replace("https://doi.org/", "").replace("http://dx.doi.org/", "").strip()
        encoded = urllib.parse.quote(clean_doi)
        landing_url = f"https://doi.org/{clean_doi}"

        self.logger.info(f"Starting OA search for DOI: {clean_doi} | Forced Source: {source}")

        session = None
        if request_func is None:
            session = create_robust_session()

            def default_request(url, headers=None, timeout=15):
                req_headers = session.headers.copy()
                if headers:
                    req_headers.update(headers)
                return session.get(url, headers=req_headers, timeout=timeout)

            request_func = default_request

        # OpenAlex
        if source in ["auto", "openalex"]:
            self.logger.debug("Querying OpenAlex...")
            openalex_api_key = os.environ.get("OPENALEX_API_KEY", "")

            openalex_rps = 9 if openalex_api_key else 2
            global_rate_limiter.acquire("openalex", rps=openalex_rps)

            try:
                url = f"https://api.openalex.org/works/https://doi.org/{clean_doi}"
                if openalex_api_key:
                    url += f"?api_key={openalex_api_key}"

                res = request_func(url, timeout=15)
                if res.status_code == 200:
                    data = res.json()
                    if data.get("open_access", {}).get("is_oa") and data.get("open_access", {}).get("oa_url"):
                        oa_url = data["open_access"]["oa_url"]
                        if not self.is_supplement(oa_url) and self.is_valid_pdf_link(oa_url):
                            self.logger.info(f"OA PDF found via OpenAlex: {oa_url}")
                            return {"is_oa": True, "pdf_url": oa_url, "landing_page_url": landing_url, "source": "OpenAlex"}
                        else:
                            self.logger.debug("OpenAlex returned a fake/supplementary PDF link. Skipping.")
            except Exception as e:
                self.logger.warning(f"OpenAlex query failed: {e}")

        # Unpaywall
        if source in ["auto", "unpaywall"]:
            self.logger.debug("Querying Unpaywall...")
            global_rate_limiter.acquire("unpaywall", rps=2)

            try:
                res = request_func(f"https://api.unpaywall.org/v2/{encoded}?email={user_email}", timeout=15)
                if res.status_code == 200:
                    data = res.json()
                    if data.get("is_oa"):
                        candidates = []
                        best = data.get("best_oa_location", {})
                        if best and best.get("url_for_pdf"):
                            candidates.append(best["url_for_pdf"])
                        for loc in data.get("oa_locations", []):
                            if loc and loc.get("url_for_pdf"):
                                candidates.append(loc["url_for_pdf"])

                        normalized_candidates = [self.normalize_pdf_url(c) for c in candidates]

                        main_pdfs = [c for c in normalized_candidates if
                                     not self.is_supplement(c) and self.is_valid_pdf_link(c)]
                        pdf_url = main_pdfs[0] if main_pdfs else ""

                        if pdf_url:
                            lp_url = best.get("url_for_landing_page", landing_url) if best else landing_url
                            self.logger.info(f"OA PDF found via Unpaywall: {pdf_url}")
                            return {"is_oa": True, "pdf_url": pdf_url, "landing_page_url": lp_url, "source": "Unpaywall"}
            except Exception as e:
                self.logger.warning(f"Unpaywall query failed: {e}")

        # PubMed Central (PMC) API
        if source in ["auto", "pubmed", "pmc"] and ncbi_api_key and user_email:
            self.logger.debug("Querying PMC API...")

            ncbi_rps = 9 if ncbi_api_key else 4
            global_rate_limiter.acquire("ncbi", rps=ncbi_rps)

            try:
                idconv_url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids={encoded}&format=json&email={user_email}"
                if ncbi_api_key:
                    idconv_url += f"&api_key={ncbi_api_key}"

                conv_res = request_func(idconv_url, timeout=15)

                if conv_res.status_code == 200:
                    records = conv_res.json().get("records", [])
                    if records and "pmcid" in records[0]:
                        pmcid = records[0]["pmcid"]

                        # 组装 OA 嗅探接口 URL
                        oa_url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmcid}"
                        if ncbi_api_key:
                            oa_url += f"&api_key={ncbi_api_key}"

                        oa_res = request_func(oa_url, timeout=15)

                        if oa_res.status_code == 200 and "<OA>" in oa_res.text:
                            matches = re.findall(r'<link[^>]+format="pdf"[^>]+href="([^"]+)"', oa_res.text)

                            main_pdfs = [m for m in matches if not self.is_supplement(m) and self.is_valid_pdf_link(m)]

                            if main_pdfs:
                                pdf_url = main_pdfs[0].replace("ftp://", "https://")
                            else:
                                pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"

                            lp_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"
                            self.logger.info(f"OA PDF found via PubMed Central: {pdf_url}")
                            return {"is_oa": True, "pdf_url": pdf_url, "landing_page_url": lp_url,
                                    "source": "PubMed_OA"}

            except Exception as e:
                self.logger.warning(f"PMC query failed: {e}")

        if source in ["auto", "semantic_scholar"]:
            from src.task.s2_task import is_s2_enabled, s2_request

            if is_s2_enabled():
                self.logger.debug("Querying Semantic Scholar...")

                config_mgr = ConfigManager()
                s2_rate_str = config_mgr.user_settings.get("s2_rate_limit") or os.environ.get("S2_RATE_LIMIT") or "1.0"
                s2_rate = float(s2_rate_str)
                global_rate_limiter.acquire("s2", rps=s2_rate)

                try:
                    res = s2_request("GET",
                                     f"https://api.semanticscholar.org/graph/v1/paper/DOI:{clean_doi}?fields=isOpenAccess,openAccessPdf")

                    if res is None:
                        self.logger.warning("S2 OA request returned None")
                        raise ValueError("S2 request failed - response is None")

                    res.raise_for_status()

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
                        pdf_url = self.normalize_pdf_url(raw_pdf_url)

                        if pdf_url and not self.is_supplement(pdf_url) and self.is_valid_pdf_link(pdf_url):
                            self.logger.info(f"OA PDF found via Semantic Scholar: {pdf_url}")
                            return {"is_oa": True, "pdf_url": pdf_url, "landing_page_url": landing_url,
                                    "source": "Semantic Scholar"}
                except Exception as e:
                    self.logger.warning(f"Semantic Scholar query failed: {e}")
            else:
                self.logger.debug("S2 OA skipped: S2 not enabled")
        self.logger.info(f"No Open Access PDF found (Tested source: {source}).")
        return {"is_oa": False, "landing_page_url": landing_url, "source": source}


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