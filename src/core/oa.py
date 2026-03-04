import urllib.parse
import re
import logging

from src.core.network_worker import create_robust_session


class OAFetcher:
    def __init__(self):
        self.logger = logging.getLogger("OAFetcher")

    def is_supplement(self, url: str) -> bool:
        if not url:
            return False
        url_lower = url.lower()
        return "supp" in url_lower or "appendix" in url_lower or "dataset" in url_lower

    def is_valid_pdf_link(self, url: str) -> bool:
        if not url:
            return False
        url_lower = url.lower()
        if "doi.org/" in url_lower:
            return False
        if url_lower.endswith(".html") or url_lower.endswith(".htm"):
            return False
        return True


    def fetch_best_oa_pdf(self, doi: str, user_email: str, s2_api_key: str, request_func = None) -> dict:
        """
        Unified OA PDF sniffing engine.
        :param request_func: A callable for making network requests, expecting (url, headers=None, timeout=15)
        """
        if not doi:
            self.logger.warning("Empty DOI provided. Aborting fetch.")
            return {"is_oa": False}

        clean_doi = doi.replace("https://doi.org/", "").replace("http://dx.doi.org/", "").strip()
        encoded = urllib.parse.quote(clean_doi)
        landing_url = f"https://doi.org/{clean_doi}"

        self.logger.info(f"Starting OA search for DOI: {clean_doi}")

        session = None
        if request_func is None:
            session = create_robust_session()

            def default_request(url, headers=None, timeout=15):
                req_headers = session.headers.copy()
                if headers:
                    req_headers.update(headers)
                return session.get(url, headers=req_headers, timeout=timeout)

            request_func = default_request

        # 1. Semantic Scholar (S2)
        if s2_api_key:
            self.logger.debug("Querying Semantic Scholar...")
            try:
                res = request_func(
                    f"https://api.semanticscholar.org/graph/v1/paper/DOI:{clean_doi}?fields=isOpenAccess,openAccessPdf",
                    headers={"x-api-key": s2_api_key},
                    timeout=15
                )
                if res.status_code == 200:
                    data = res.json()
                    if data.get("isOpenAccess") and data.get("openAccessPdf"):
                        pdf_url = data["openAccessPdf"].get("url", "")

                        if pdf_url and not self.is_supplement(pdf_url) and self.is_valid_pdf_link(pdf_url):
                            self.logger.info(f"OA PDF found via Semantic Scholar: {pdf_url}")
                            return {"is_oa": True, "pdf_url": pdf_url,
                                    "landing_page_url": landing_url, "source": "Semantic Scholar"}
                        else:
                            self.logger.debug("Semantic Scholar returned a fake/supplementary PDF link. Skipping.")
            except Exception as e:
                self.logger.warning(f"Semantic Scholar query failed: {e}")

        # 2. OpenAlex
        self.logger.debug("Querying OpenAlex...")
        try:
            res = request_func(f"https://api.openalex.org/works/https://doi.org/{clean_doi}?mailto={user_email}",
                               timeout=15)
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

        # 3. Unpaywall
        self.logger.debug("Querying Unpaywall...")
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

                    main_pdfs = [c for c in candidates if not self.is_supplement(c) and self.is_valid_pdf_link(c)]
                    pdf_url = main_pdfs[0] if main_pdfs else ""

                    if pdf_url:
                        lp_url = best.get("url_for_landing_page", landing_url) if best else landing_url
                        self.logger.info(f"OA PDF found via Unpaywall: {pdf_url}")
                        return {"is_oa": True, "pdf_url": pdf_url, "landing_page_url": lp_url, "source": "Unpaywall"}
        except Exception as e:
            self.logger.warning(f"Unpaywall query failed: {e}")

        # 4. PubMed Central (PMC) API
        self.logger.debug("Querying PMC API...")
        try:
            conv_res = request_func(
                f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids={encoded}&format=json&email={user_email}",
                timeout=15
            )
            if conv_res.status_code == 200:
                records = conv_res.json().get("records", [])
                if records and "pmcid" in records[0]:
                    pmcid = records[0]["pmcid"]
                    oa_res = request_func(f"https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmcid}",
                                          timeout=15)
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


        self.logger.info("No Open Access PDF found across all databases.")
        return {"is_oa": False, "landing_page_url": landing_url}


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