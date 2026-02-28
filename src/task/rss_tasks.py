import concurrent
import os
import json
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
                resp = session.get(url, timeout=15)
                resp.raise_for_status()

                articles = self._parse_feed(resp.text, session)
                oa_count = sum(1 for a in articles if a.get('pdf_url'))
                return {"success": True, "url": url, "name": name, "articles": articles, "oa_count": oa_count}
            except Exception as e:
                return {"success": False, "url": url, "name": name, "error": str(e)}
            finally:
                session.close()

        # 使用最大 8 线程并发请求
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            future_to_feed = [executor.submit(process_single_feed, f) for f in feeds]

            for future in concurrent.futures.as_completed(future_to_feed):
                # ★ 核心：每次完成一个任务就检查是否被取消 ★
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

            doi = article.get("doi")
            pdf_url = ""
            landing_url = article.get("link", "")
            user_email = os.environ.get("NCBI_API_EMAIL", "scholar.user@example.com")

            if doi:
                s2_key = os.environ.get("S2_API_KEY", "").strip()
                fetcher = OAFetcher()

                # === 优化点：直接调用，让 OA 引擎自己处理网络和防屏蔽 ===
                oa_result = fetcher.fetch_best_oa_pdf(doi, user_email, s2_key)

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
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            final_articles = list(executor.map(_detect_oa_for_article, raw_articles))

        return final_articles


class DownloadOATask(BackgroundTask):
    def _execute(self):
        _setup_worker_env()
        urls_info = self.kwargs.get('urls_info', [])
        save_dir = self.kwargs.get('save_dir', '')

        if not urls_info or not save_dir:
            self.send_log("ERROR", "无效的下载参数。")
            return {"success": False}

        total = len(urls_info)
        success_count = 0
        skip_count = 0
        session = create_robust_session()

        import requests
        session = requests.Session()
        if os.environ.get("NO_PROXY") == "*":
            session.trust_env = False

        for i, info in enumerate(urls_info):
            # 响应取消操作
            if getattr(self, 'is_cancelled', False) or getattr(self, '_is_cancelled', False):
                self.send_log("WARNING", "下载被用户取消。")
                break

            url = info['url']
            raw_name = info.get('filename', 'document')

            # 清理文件名中的非法字符
            safe_name = "".join([c for c in raw_name if c.isalnum() or c in ' .-_()[]']).strip()
            if not safe_name.lower().endswith('.pdf'):
                safe_name += '.pdf'

            filepath = os.path.join(save_dir, safe_name)

            # 文件存在则跳过
            if os.path.exists(filepath):
                self.send_log("INFO", f"已存在，跳过: {safe_name}")
                skip_count += 1
                self.update_progress(int(((i + 1) / total) * 100),
                                     f"[{i + 1}/{total}] 已存在，跳过: {safe_name[:20]}...")
                continue

            self.update_progress(int((i / total) * 100), f"[{i + 1}/{total}] 正在下载: {safe_name[:20]}...")

            try:
                res = session.get(url, timeout=30)
                res.raise_for_status()
                with open(filepath, 'wb') as f:
                    f.write(res.content)
                success_count += 1
                self.send_log("INFO", f"下载成功: {safe_name}")
            except Exception as e:
                self.send_log("ERROR", f"下载失败 {safe_name}: {str(e)}")

            self.update_progress(int(((i + 1) / total) * 100), f"[{i + 1}/{total}] 处理完毕")

        return {"success": True, "downloaded": success_count, "skipped": skip_count}



