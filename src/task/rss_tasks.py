import os
import json
import traceback
import xml.etree.ElementTree as ET
import requests

from src.core.core_task import BackgroundTask, TaskState


def _setup_worker_env():
    """隔离初始化网络环境变量"""
    try:
        from src.core.network_worker import setup_global_network_env
        setup_global_network_env()
    except Exception as e:
        pass


class FetchRSSTask(BackgroundTask):
    """
    后台抓取 RSS/Atom 订阅任务
    利用多进程防止网络 I/O 阻塞主 UI，同时无缝继承系统的 Proxy 代理设置。
    """

    def _execute(self):
        # 1. 挂载系统的网络/代理环境
        _setup_worker_env()

        feeds = self.kwargs.get('feeds', [])
        save_path = self.kwargs.get('save_path', 'scholar_workspace/rss_cache.json')

        if not feeds:
            self.send_log("WARNING", "No RSS feeds provided to fetch.")
            return

        # 确保工作区目录存在
        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        session = requests.Session()
        # 如果需要显式关闭代理（对应 proxy_mode == "off"）
        if os.environ.get("NO_PROXY") == "*":
            session.trust_env = False

        results = {}
        total = len(feeds)
        success_count = 0

        self.send_log("INFO", f"📡 开始同步 {total} 个 RSS 订阅源...")

        for i, feed in enumerate(feeds):
            url = feed.get('url')
            name = feed.get('name', 'Unknown')

            self.update_progress(int((i / total) * 100), f"Fetching: {name}...")

            try:
                # 伪装请求头，防止部分学术期刊 (如 Nature, PubMed) 屏蔽爬虫
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ScholarNavis/1.0'}
                resp = session.get(url, headers=headers, timeout=15)
                resp.raise_for_status()

                # 简单且鲁棒的 XML 解析 (同时兼容 RSS 2.0 和基础 Atom)
                articles = self._parse_feed(resp.text)
                results[url] = articles
                success_count += 1
                self.send_log("INFO", f"✅ {name}: 获取到 {len(articles)} 篇文章")

            except Exception as e:
                self.send_log("ERROR", f"❌ 获取 {name} 失败: {str(e)}")
                results[url] = []  # 写入空列表防止 UI 报错

        # 2. 将抓取结果落盘，通过文件系统与主进程通信 (避免跨进程传递巨大对象的序列化开销)
        with open(save_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        self.update_progress(100, f"同步完成。成功: {success_count}/{total}")
        self.send_log("INFO", f"🎉 RSS 抓取任务完成，数据已缓存至 {save_path}")

    def _parse_feed(self, xml_string):
        """兼容 RSS 和 Atom 的轻量级解析器"""
        articles = []
        try:
            root = ET.fromstring(xml_string)
            # 简单粗暴去掉 namespace 以方便搜索
            for elem in root.iter():
                if '}' in elem.tag:
                    elem.tag = elem.tag.split('}', 1)[1]

            # RSS 2.0 逻辑
            items = root.findall('.//item')
            # Atom 逻辑
            if not items:
                items = root.findall('.//entry')

            for item in items[:30]:  # 限制每个源最多保留最新的 30 篇
                title = item.find('title')
                link = item.find('link')
                desc = item.find('description')
                if desc is None: desc = item.find('summary')  # Atom
                pub_date = item.find('pubDate')
                if pub_date is None: pub_date = item.find('updated')  # Atom

                # 处理 Atom 的 link 标签通常有 href 属性
                href = ""
                if link is not None:
                    href = link.attrib.get('href', link.text)
                if not href and link is not None:
                    href = link.text

                articles.append({
                    "title": title.text.strip() if title is not None and title.text else "No Title",
                    "link": href.strip() if href else "",
                    "summary": desc.text.strip() if desc is not None and desc.text else "No Description",
                    "pub_date": pub_date.text.strip() if pub_date is not None and pub_date.text else ""
                })
        except Exception as e:
            self.send_log("WARNING", f"XML 解析异常: {str(e)}")

        return articles