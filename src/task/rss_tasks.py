import concurrent
import os
import json
import random
import time
import xml.etree.ElementTree as ET
import requests
import re
from datetime import datetime
import urllib.parse

from src.core.core_task import BackgroundTask
from src.core.network_worker import create_robust_session
from src.core.oa import OAFetcher


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

        # 读取旧缓存，实现增量更新
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

        self.send_log("INFO", f"📡 开始多线程并发同步 {total} 个订阅源，并自动嗅探 OA 全文...")

        def process_single_feed(feed):
            if getattr(self, 'is_cancelled', False) or getattr(self, '_is_cancelled', False):
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

                # 如果不是 CF 盾，直接报错返回
                if not is_cf:
                    return {"success": False, "url": url, "name": name, "error": err_msg}

                # 策略2：确认是 CF 盾拦截后，触发公共 API 代理通道
                self.send_log("WARNING", f"【{name}】被 Cloudflare JS 盾拦截，正在切换代理通道...")
                try:
                    import urllib.parse
                    # 使用免费的 rss2json API 绕过前端验证
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
                            "error": f"CF拦截，且代理通道也失败: {str(proxy_e)}"}
            finally:
                session.close()


        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_feed = [executor.submit(process_single_feed, f) for f in feeds]

            for future in concurrent.futures.as_completed(future_to_feed):
                if getattr(self, 'is_cancelled', False) or getattr(self, '_is_cancelled', False):
                    self.send_log("WARNING", "⚠️ Task cancelled by user. Terminating fetch pool.")
                    break

                completed_count += 1
                res = future.result()

                if res:
                    if res["success"]:
                        results[res["url"]] = res["articles"]
                        success_count += 1
                        self.send_log("INFO",
                                      f"{res['name']}: 获取 {len(res['articles'])} 篇 (发现 {res['oa_count']} 篇 OA)")
                    else:
                        self.send_log("ERROR", f"获取 {res['name']} 失败: {res['error']}")

                # 更新进度条
                self.update_progress(int((completed_count / total) * 100), f"Syncing... {completed_count}/{total}")

        # 无论成功、失败还是取消，都将现已抓取的数据落盘
        with open(save_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        if getattr(self, 'is_cancelled', False) or getattr(self, '_is_cancelled', False):
            self.send_log("INFO", "🛑 同步被用户主动中断，已保存部分数据。")
        else:
            self.update_progress(100, f"同步完成。成功: {success_count}/{total}")
            self.send_log("INFO", f"🎉 RSS 并发抓取任务完成，数据已落盘")

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

            # --- 第一阶段：纯本地解析 XML 标签，收集原始数据 ---
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
                    "pdf_url": "",  # 待填充
                    "tags": paper_tags[:5]
                })

                if len(raw_articles) >= 40: break

        except Exception as e:
            self.send_log("WARNING", f"XML 解析异常: {str(e)}")
            return []

        # 内层多线程并发探测 OA 全文状态
        def _detect_oa_for_article(article):
            if getattr(self, 'is_cancelled', False) or getattr(self, '_is_cancelled', False):
                return article


            # 加入 0.2 ~ 0.8 秒的随机抖动，防止触发学术 API 的 429 并发限制
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
        """专门用于处理 rss2json 代理返回的数据格式"""
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
            self.send_log("WARNING", f"代理数据解析异常: {str(e)}")
            return []

        def _detect_oa_for_article(article):
            if getattr(self, 'is_cancelled', False) or getattr(self, '_is_cancelled', False):
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






