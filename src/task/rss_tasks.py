import concurrent
import json
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime

from src.core.core_task import BackgroundTask
from src.core.network_worker import create_robust_session


def _setup_worker_env():
    try:
        from src.core.network_worker import setup_global_network_env
        setup_global_network_env()
    except Exception:
        pass


def extract_doi(text):
    if not text: return None
    match = re.search(r'(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)', text)
    return match.group(1) if match else None


class FetchRSSTask(BackgroundTask):
    def _execute(self):
        _setup_worker_env()

        feeds = self.kwargs.get('feeds', [])
        save_path = self.kwargs.get('save_path', 'scholar_workspace/rss_cache.json')

        if not feeds:
            self.send_log("WARNING", "No RSS feeds provided.")
            return

        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        # Read existing cache for incremental updates
        results = {}
        if os.path.exists(save_path):
            try:
                with open(save_path, 'r', encoding='utf-8') as f:
                    results = json.load(f)
            except Exception:
                pass

        results["_meta"] = {"last_fetched": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

        total = len(feeds)
        success_count = 0
        completed_count = 0

        self.send_log("INFO",
                      f"Starting multi-threaded synchronization of {total} feeds with automated OA full-text sniffing...")

        def process_single_feed(feed):
            if self.is_cancelled():
                return None

            session = create_robust_session()
            url = feed.get('url')
            name = feed.get('name', 'Unknown')

            try:
                session.headers[
                    'User-Agent'] = 'Feedly/1.0 (+http://www.feedly.com/fetcher.html; like FeedFetcher-Google)'

                resp = session.get(url, timeout=15)
                resp.raise_for_status()

                articles = self._parse_feed(resp.text, session)
                oa_count = sum(1 for a in articles if a.get('pdf_url'))
                return {"success": True, "url": url, "name": name, "articles": articles, "oa_count": oa_count}
            except Exception as e:
                err_msg = str(e)
                is_cf = False
                if hasattr(e, 'response') and e.response is not None:
                    body = e.response.text[:250].replace('\n', ' ').strip()
                    if e.response.status_code in (403, 503) and (
                            "Just a moment" in body or "cloudflare" in body.lower()):
                        is_cf = True
                    else:
                        err_msg = f"HTTP {e.response.status_code} | Server Reply: {body}"

                # If not a Cloudflare challenge, return error directly
                if not is_cf:
                    return {"success": False, "url": url, "name": name, "error": err_msg}

                # Strategy 2: Trigger public API proxy channel upon Cloudflare interception
                self.send_log("WARNING", f"[{name}] Intercepted by Cloudflare JS challenge; switching proxy channel...")
                try:
                    import urllib.parse
                    # Bypass frontend validation using free rss2json API
                    proxy_url = f"https://api.rss2json.com/v1/api.json?rss_url={urllib.parse.quote(url)}"
                    proxy_resp = session.get(proxy_url, timeout=20)
                    proxy_resp.raise_for_status()

                    data = proxy_resp.json()
                    if data.get("status") != "ok":
                        raise ValueError("Proxy API returned error.")

                    articles = self._parse_proxy_json(data)
                    oa_count = sum(1 for a in articles if a.get('pdf_url'))
                    return {"success": True, "url": url, "name": name, "articles": articles, "oa_count": oa_count}
                except Exception as proxy_e:
                    return {"success": False, "url": url, "name": name,
                            "error": f"Cloudflare interception persists and proxy failover failed: {str(proxy_e)}"}
            finally:
                session.close()


        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_feed = [executor.submit(process_single_feed, f) for f in feeds]

            for future in concurrent.futures.as_completed(future_to_feed):
                if self.is_cancelled():
                    self.send_log("WARNING", "⚠️ Task cancelled by user. Terminating fetch pool.")
                    break

                completed_count += 1
                res = future.result()

                if res:
                    if res["success"]:
                        results[res["url"]] = res["articles"]
                        success_count += 1
                        self.send_log("INFO",
                                      f"{res['name']}: Fetched {len(res['articles'])} articles ({res['oa_count']} OA papers found)")
                    else:
                        self.send_log("ERROR", f"Failed to fetch {res['name']}: {res['error']}")

                # Update progress bar
                self.update_progress(int((completed_count / total) * 100), f"Syncing... {completed_count}/{total}")

        # Persist captured data regardless of success, failure, or cancellation
        with open(save_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        if self.is_cancelled():
            self.send_log("INFO", "Sync interrupted by user; partial data saved.")
        else:
            self.update_progress(100, f"Sync complete. Success: {success_count}/{total}")
            self.send_log("INFO", f"RSS concurrent fetch task completed; data persisted to disk.")

    def _parse_feed(self, xml_string, base_session):
        user_email = os.environ.get("NCBI_API_EMAIL", "scholar.user@example.com")
        if not user_email: user_email = "scholar.user@example.com"

        raw_articles = []
        try:
            root = ET.fromstring(xml_string)
            for elem in root.iter():
                if '}' in elem.tag:
                    elem.tag = elem.tag.split('}', 1)[1]

            items = root.findall('.//item')
            if not items: items = root.findall('.//entry')

            # --- Phase 1: Local XML parsing to collect raw data ---
            for item in items:
                title_elem = item.find('title')
                title_text = title_elem.text.strip() if title_elem is not None and title_elem.text else "No Title"

                desc_text = ""
                content_elem = item.find('encoded')
                if content_elem is not None and content_elem.text:
                    desc_text = content_elem.text.strip()
                else:
                    desc_elem = item.find('description')
                    if desc_elem is None: desc_elem = item.find('summary')
                    if desc_elem is not None and desc_elem.text:
                        desc_text = desc_elem.text.strip()

                paper_tags = []
                for cat in item.findall('category') + item.findall('subject'):
                    if cat.text: paper_tags.append(cat.text.strip())

                link_elem = item.find('link')
                pub_date = item.find('pubDate')
                if pub_date is None: pub_date = item.find('updated')

                href = ""
                if link_elem is not None:
                    href = link_elem.attrib.get('href', link_elem.text)
                if not href and link_elem is not None: href = link_elem.text

                guid_text = item.findtext('guid') or ""
                doi = extract_doi(href) or extract_doi(desc_text) or extract_doi(guid_text)

                raw_articles.append({
                    "title": title_text,
                    "link": href.strip() if href else "",
                    "summary": desc_text or "No abstract provided by publisher.",
                    "pub_date": pub_date.text.strip() if pub_date is not None and pub_date.text else "",
                    "doi": doi or "",
                    "pdf_url": "",  # To be filled
                    "tags": paper_tags[:5]
                })

                if len(raw_articles) >= 40: break

        except Exception as e:
            self.send_log("WARNING", f"XML parsing exception: {str(e)}")
            return []

        # Internal multi-threading to detect OA full-text status
        def _detect_oa_for_article(article):
            if self.is_cancelled():
                return article

            # Add 0.2 ~ 0.8s random jitter to avoid triggering rate limits (429) on scholarly APIs
            time.sleep(random.uniform(0.2, 0.8))

            doi = article.get("doi")
            pdf_url = ""
            landing_url = article.get("link", "")

            if doi:
                s2_key = os.environ.get("S2_API_KEY", "").strip()
                from src.core.oa import OAFetcher
                fetcher = OAFetcher()

                oa_result = fetcher.fetch_best_oa_pdf(doi, user_email, s2_key, None)

                if oa_result.get("is_oa"):
                    pdf_url = oa_result["pdf_url"]
                    if not landing_url:
                        landing_url = oa_result["landing_page_url"]
                elif not landing_url and "landing_page_url" in oa_result:
                    landing_url = oa_result["landing_page_url"]

            article["pdf_url"] = pdf_url
            article["link"] = landing_url
            return article

        final_articles = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            final_articles = list(executor.map(_detect_oa_for_article, raw_articles))

        return final_articles

    def _parse_proxy_json(self, json_data):
        """Specifically for handling data formats returned by the rss2json proxy"""
        raw_articles = []
        try:
            for item in json_data.get('items', []):
                title_text = item.get('title', 'No Title')
                desc_text = item.get('description', item.get('content', ''))
                href = item.get('link', '')
                pub_date = item.get('pubDate', '')
                guid_text = item.get('guid', '')

                doi = extract_doi(href) or extract_doi(desc_text) or extract_doi(guid_text)
                tags = item.get('categories', [])[:5]

                raw_articles.append({
                    "title": title_text,
                    "link": href.strip() if href else "",
                    "summary": desc_text or "No abstract provided by publisher.",
                    "pub_date": pub_date.strip() if pub_date else "",
                    "doi": doi or "",
                    "pdf_url": "",
                    "tags": tags
                })
                if len(raw_articles) >= 40: break
        except Exception as e:
            self.send_log("WARNING", f"Proxy data parsing exception: {str(e)}")
            return []

        def _detect_oa_for_article(article):
            if self.is_cancelled():
                return article

            time.sleep(random.uniform(0.2, 0.8))

            doi = article.get("doi")
            pdf_url = ""
            landing_url = article.get("link", "")
            user_email = os.environ.get("NCBI_API_EMAIL", "scholar.user@example.com")

            if doi:
                s2_key = os.environ.get("S2_API_KEY", "").strip()
                from src.core.oa import OAFetcher
                fetcher = OAFetcher()
                oa_result = fetcher.fetch_best_oa_pdf(doi, user_email, s2_key, None)

                if oa_result.get("is_oa"):
                    pdf_url = oa_result["pdf_url"]
                    if not landing_url: landing_url = oa_result["landing_page_url"]
                elif not landing_url and "landing_page_url" in oa_result:
                    landing_url = oa_result["landing_page_url"]

            article["pdf_url"] = pdf_url
            article["link"] = landing_url
            return article

        import concurrent.futures
        final_articles = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            final_articles = list(executor.map(_detect_oa_for_article, raw_articles))

        return final_articles