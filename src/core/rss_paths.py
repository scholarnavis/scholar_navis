"""RSS 工具的落盘位置（唯一定义处）。

订阅清单属于用户配置（``config/``），抓取到的文章缓存属于工作数据（``RSS/``）。这两处
路径原先只在 :mod:`src.tools.rss_tool` 里就地拼接，而取全文自检的样例解析要读同一份缓存
（见 :mod:`src.core.fulltext_samples`），因此集中到这里，避免两处各写一遍后漂移。
"""

from __future__ import annotations

import os

from src.core import BASE_DIR

#: RSS 工作目录（文章缓存所在）
RSS_DIR = os.path.join(BASE_DIR, "RSS")

#: 文章缓存：``{feed_url: [article, ...], "_meta": {...}}``
RSS_CACHE_FILE = os.path.join(RSS_DIR, "rss_cache.json")

#: 用户配置目录（与其它 ``config/*.json`` 同级）
CONFIG_DIR = os.path.join(BASE_DIR, "config")

#: 订阅清单
RSS_FEEDS_FILE = os.path.join(CONFIG_DIR, "rss_feed.json")

__all__ = ["RSS_DIR", "RSS_CACHE_FILE", "CONFIG_DIR", "RSS_FEEDS_FILE"]