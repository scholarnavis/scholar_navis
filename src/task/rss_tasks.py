import os
import json
import xml.etree.ElementTree as ET
import requests
import re
from datetime import datetime
import urllib.parse

from src.core.core_task import BackgroundTask


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
        session = requests.Session()
        if os.environ.get("NO_PROXY") == "*":
            session.trust_env = False

        # 🌟 核心修改：读取旧缓存，实现增量更新
        results = {}
        if os.path.exists(save_path):
            try:
                with open(save_path, 'r', encoding='utf-8') as f:
                    results = json.load(f)
            except Exception:
                pass

        # 更新 meta 信息
        results["_meta"] = {
            "last_fetched": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        total = len(feeds)
        success_count = 0

        self.send_log("INFO", f"📡 开始同步 {total} 个订阅源，并自动嗅探 OA 免费全文...")

        for i, feed in enumerate(feeds):
            if getattr(self, 'is_cancelled', False) or getattr(self, '_is_cancelled', False):
                self.send_log("WARNING", "⚠️ Task cancelled by user. Terminating fetch process.")
                break

            url = feed.get('url')
            name = feed.get('name', 'Unknown')

            self.update_progress(int((i / total) * 100), f"Fetching: {name}...")

            try:
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                resp = session.get(url, headers=headers, timeout=15)
                resp.raise_for_status()

                articles = self._parse_feed(resp.text, session)
                results[url] = articles  # 仅更新或覆盖本次请求的字典键
                success_count += 1

                oa_count = sum(1 for a in articles if a.get('pdf_url'))
                self.send_log("INFO", f"✅ {name}: 获取 {len(articles)} 篇文献 (发现 {oa_count} 篇免费 PDF)")

            except Exception as e:
                self.send_log("ERROR", f"❌ 获取 {name} 失败: {str(e)}")
                # 如果获取失败，为了防止清空旧数据，这里不要强行赋空列表，保留旧 results[url] 即可

        with open(save_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        self.update_progress(100, f"同步完成。成功: {success_count}/{total}")
        self.send_log("INFO", f"🎉 RSS 抓取与全文嗅探任务完成，数据已落盘")

    def _parse_feed(self, xml_string, session):
        # 此处保留原有的 _parse_feed 逻辑完全不变，为了篇幅省略
        articles = []
        user_email = os.environ.get("NCBI_API_EMAIL", "scholar.user@example.com")
        if not user_email: user_email = "scholar.user@example.com"

        try:
            root = ET.fromstring(xml_string)
            for elem in root.iter():
                if '}' in elem.tag:
                    elem.tag = elem.tag.split('}', 1)[1]

            items = root.findall('.//item')
            if not items: items = root.findall('.//entry')

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
                pdf_url = ""
                landing_url = href.strip() if href else ""

                if doi:
                    clean_doi = urllib.parse.quote(
                        doi.replace("https://doi.org/", "").replace("http://dx.doi.org/", "").strip())
                    if not landing_url: landing_url = f"https://doi.org/{clean_doi}"

                    s2_key = os.environ.get("S2_API_KEY", "").strip()

                    # 1. 尝试 S2 (如果有 Key)
                    if s2_key and not pdf_url:
                        try:
                            s2_url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{clean_doi}?fields=isOpenAccess,openAccessPdf"
                            res = session.get(s2_url, headers={"x-api-key": s2_key}, timeout=2.5)
                            if res.status_code == 200:
                                data = res.json()
                                if data.get("isOpenAccess") and data.get("openAccessPdf"):
                                    pdf_url = data["openAccessPdf"].get("url", "")
                        except:
                            pass

                    # 2. 尝试 Unpaywall
                    if not pdf_url:
                        try:
                            req_url = f"https://api.unpaywall.org/v2/{clean_doi}?email={user_email}"
                            res = session.get(req_url, timeout=2.5)
                            if res.status_code == 200:
                                data = res.json()
                                if data.get("is_oa"):
                                    best = data.get("best_oa_location", {})
                                    if best: pdf_url = best.get("url_for_pdf", "")
                        except:
                            pass

                    # 3. PMC API 终极保底
                    if not pdf_url:
                        try:
                            pmc_url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids={clean_doi}&format=json&email={user_email}"
                            pmc_res = session.get(pmc_url, timeout=2.5)
                            if pmc_res.status_code == 200:
                                pmc_data = pmc_res.json()
                                records = pmc_data.get("records", [])
                                if records and "pmcid" in records[0]:
                                    pmcid = records[0]["pmcid"]

                                    oa_api_url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmcid}"
                                    oa_res = session.get(oa_api_url, timeout=2.5)
                                    if oa_res.status_code == 200 and "<OA>" in oa_res.text:
                                        match = re.search(r'<link[^>]+format="pdf"[^>]+href="([^"]+)"', oa_res.text)
                                        if match:
                                            pdf_url = match.group(1).replace("ftp://", "https://")
                                        else:
                                            pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"
                        except:
                            pass

                articles.append({
                    "title": title_text,
                    "link": landing_url,
                    "summary": desc_text or "No abstract provided by publisher.",
                    "pub_date": pub_date.text.strip() if pub_date is not None and pub_date.text else "",
                    "doi": doi or "",
                    "pdf_url": pdf_url,
                    "tags": paper_tags[:5]
                })

                if len(articles) >= 40: break

        except Exception as e:
            self.send_log("WARNING", f"XML 解析异常: {str(e)}")

        return articles