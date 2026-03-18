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
        try:
            from src.core.config_manager import ConfigManager
            ConfigManager().save_json(save_path, results, encrypt=False)
            self.send_log("INFO", f"Data persisted via ConfigManager to: {save_path}")
        except Exception as e:
            self.send_log("ERROR", f"Failed to persist cache: {e}")

        results["_meta"] = {"last_fetched": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

        total = len(feeds)
        success_count = 0
        completed_count = 0

        self.send_log("INFO",
                      f"Starting multi-threaded synchronization of {total} feeds with automated OA full-text sniffing...")

        def process_single_feed(feed):
            # 完全剥离对 self.is_cancelled() 的调用，规避悬垂指针风险
            session = create_robust_session()
            url = feed.get('url')
            name = feed.get('name', 'Unknown')
            local_logs = []  # 建立局部日志缓冲队列

            try:
                session.headers['User-Agent'] = 'Feedly/1.0 (+http://www.feedly.com/fetcher.html; like FeedFetcher-Google)'

                resp = session.get(url, timeout=15)
                resp.raise_for_status()

                # 将日志缓冲队列下发至子解析函数
                articles = self._parse_feed(resp.text, session, local_logs)
                oa_count = sum(1 for a in articles if a.get('pdf_url'))
                return {"success": True, "url": url, "name": name, "articles": articles, "oa_count": oa_count, "logs": local_logs}
            except Exception as e:
                err_msg = str(e)
                is_cf = False
                if hasattr(e, 'response') and e.response is not None:
                    status_code = e.response.status_code
                    body = e.response.text[:250].replace('\n', ' ').strip()

                    if status_code in (403, 503) and ("Just a moment" in body or "cloudflare" in body.lower()):
                        is_cf = True
                    else:
                        err_msg = f"HTTP {status_code} | Server Reply: {body}"
                else:
                    err_msg = f"Network Error: {type(e).__name__} - Check your proxy/internet settings."

                if not is_cf:
                    return {"success": False, "url": url, "name": name, "error": err_msg, "logs": local_logs}

                # 将直接信号发射替换为缓冲队列写入
                local_logs.append(("WARNING", f"[{name}] Intercepted by Cloudflare JS challenge; switching proxy channel..."))
                try:
                    import urllib.parse
                    proxy_url = f"https://api.rss2json.com/v1/api.json?rss_url={urllib.parse.quote(url)}"
                    proxy_resp = session.get(proxy_url, timeout=20)
                    proxy_resp.raise_for_status()

                    data = proxy_resp.json()
                    if data.get("status") != "ok":
                        raise ValueError("Proxy API returned error.")

                    articles = self._parse_proxy_json(data, local_logs)
                    oa_count = sum(1 for a in articles if a.get('pdf_url'))
                    return {"success": True, "url": url, "name": name, "articles": articles, "oa_count": oa_count, "logs": local_logs}
                except Exception as proxy_e:
                    return {"success": False, "url": url, "name": name,
                            "error": f"Cloudflare interception persists and proxy failover failed: {str(proxy_e)}", "logs": local_logs}
            finally:
                session.close()

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        future_to_feed = [executor.submit(process_single_feed, f) for f in feeds]

        try:
            for future in concurrent.futures.as_completed(future_to_feed):
                if self.is_cancelled():
                    self.send_log("WARNING", "⚠️ Task cancelled by user. Terminating fetch pool.")
                    break

                completed_count += 1
                res = future.result()

                if res:
                    # 主执行线程接管缓冲日志发射任务，确保线程亲和性
                    for level, msg in res.get("logs", []):
                        self.send_log(level, msg)

                    if res["success"]:
                        results[res["url"]] = res["articles"]
                        success_count += 1
                        self.send_log("INFO", f"{res['name']}: Fetched {len(res['articles'])} articles ({res['oa_count']} OA papers found)")
                    else:
                        self.send_log("ERROR", f"Failed to fetch {res['name']}: {res['error']}")

                self.update_progress(int((completed_count / total) * 100), f"Syncing... {completed_count}/{total}")
        finally:
            executor.shutdown(wait=False)

        try:
            with open(save_path, 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False, indent=2)

            if self.is_cancelled():
                self.send_log("INFO", "Sync interrupted by user; partial data saved.")
            else:
                self.update_progress(100, f"Sync complete. Success: {success_count}/{total}")
                self.send_log("INFO", f"RSS concurrent fetch task completed; data persisted to disk.")
        except RuntimeError:
            pass

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
            try:
                if self.is_cancelled():
                    return article
            except RuntimeError:
                return article

            time.sleep(random.uniform(0.2, 0.8))

            doi = article.get("doi")
            pdf_url = ""
            landing_url = article.get("link", "")

            if doi:
                s2_key = os.environ.get("S2_API_KEY", "").strip()
                ncbi_key = os.environ.get("NCBI_API_KEY", "").strip()
                from src.core.oa import OAFetcher
                fetcher = OAFetcher()

                oa_result = fetcher.fetch_best_oa_pdf(doi, user_email, ncbi_api_key=ncbi_key)

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
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        try:
            final_articles = list(executor.map(_detect_oa_for_article, raw_articles))
        finally:
            executor.shutdown(wait=True)

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
            try:
                if self.is_cancelled():
                    return article
            except RuntimeError:
                return article

            time.sleep(random.uniform(0.2, 0.8))

            doi = article.get("doi")
            pdf_url = ""
            landing_url = article.get("link", "")
            user_email = os.environ.get("NCBI_API_EMAIL", "scholar.user@example.com")

            if doi:
                ncbi_key = os.environ.get("NCBI_API_KEY", "").strip()
                from src.core.oa import OAFetcher
                fetcher = OAFetcher()
                oa_result = fetcher.fetch_best_oa_pdf(doi, user_email, ncbi_api_key=ncbi_key)

                if oa_result.get("is_oa"):
                    pdf_url = oa_result["pdf_url"]
                    if not landing_url: landing_url = oa_result["landing_page_url"]
                elif not landing_url and "landing_page_url" in oa_result:
                    landing_url = oa_result["landing_page_url"]

            article["pdf_url"] = pdf_url
            article["link"] = landing_url
            return article

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            final_articles = list(executor.map(_detect_oa_for_article, raw_articles))

        return final_articles


class SearchArticlesTask(BackgroundTask):
    def _execute(self):
        query = self.kwargs.get('query', '').lower().strip()
        cache_file = self.kwargs.get('cache_file', 'scholar_workspace/rss_cache.json')

        if not query:
            return []

        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            self.send_log("ERROR", f"Search failed: Unable to read cache. {e}")
            return []

        data.pop("_meta", None)
        all_articles = []
        for url, articles in data.items():
            all_articles.extend(articles)

        total_articles = len(all_articles)
        self.send_log("INFO", f"Starting search across {total_articles} cached articles...")

        query_terms = [t for t in re.split(r'\W+', query) if len(t) > 1]
        results = []

        for i, article in enumerate(all_articles):
            if self.is_cancelled():
                break

            if i % max(1, (total_articles // 20)) == 0:
                self.update_progress(int((i / total_articles) * 100), f"Analyzing {i}/{total_articles}...")

            title = article.get('title', '').lower()
            summary = article.get('summary', '').lower()

            score = 0

            if query in title:
                score += 50
            if query in summary:
                score += 20

            for term in query_terms:
                title_hits = title.count(term)
                summary_hits = summary.count(term)

                if title_hits > 0 or summary_hits > 0:
                    score += (title_hits * 10) + (summary_hits * 2)

            if score > 0:
                res_art = article.copy()
                res_art['_search_score'] = score
                results.append(res_art)

        results.sort(key=lambda x: x['_search_score'], reverse=True)

        top_results = results[:150]
        self.update_progress(100, "Search complete.")


        return top_results


class ExportRssTask(BackgroundTask):

    def _execute(self):
        feeds = self.kwargs.get('feeds', [])
        export_path = self.kwargs.get('export_path')
        try:
            self.update_progress(50, "Exporting feeds to file...")
            from src.core.config_manager import ConfigManager
            ConfigManager().save_json(export_path, feeds, encrypt=False)
            return {"success": True, "path": export_path}
        except Exception as e:
            return {"success": False, "error": str(e)}


class ImportRssTask(BackgroundTask):

    def _execute(self):
        import_path = self.kwargs.get('import_path')
        try:
            self.update_progress(50, "Reading feeds from file...")
            from src.core.config_manager import ConfigManager
            imported_feeds = ConfigManager().load_json(import_path, encrypt=False)

            if not isinstance(imported_feeds, list):
                raise ValueError("Invalid format: expected a list of feeds.")
            return {"success": True, "feeds": imported_feeds}
        except Exception as e:
            return {"success": False, "error": str(e)}