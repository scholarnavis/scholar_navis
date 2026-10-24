"""开发者模式"获取全文"自检的样例来源。

两级来源，先本机后内置：

    * 本机 RSS 文章缓存里"开放获取探测没找到全文"的条目——判定来自应用自己的 OA 探测
      （见 :mod:`src.task.rss_tasks` 的 ``_detect_oa_for_article``），最贴近真实遇到的
      付费墙；
    * 随包分发的内置样例（``src/core/data/fulltext_test_samples.json``）——软件分发后本机
      没有任何订阅时，仍然能测通付费墙取全文链路。

只读：不写任何用户数据；缓存缺失、不可解析或没有候选时安静回退到内置样例。

注意：缓存里"没有 PDF"也可能是 OA 探测当时网络失败，而不是真的付费墙。自检只把这条
条目当作**待请求全文的论文**，取全文窗口照常打开，链路本身不依赖这个判定为真。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Tuple
from urllib.parse import urlsplit

from src.core.platform_env import app_resource_root
from src.core.rss_paths import RSS_CACHE_FILE

logger = logging.getLogger("Core.FulltextSamples")

#: 内置样例：只读资源，随包分发（冻结产物由 build_app.py 以 ``--add-data`` 带上）。
BUNDLED_SAMPLES_FILE = os.path.join(
    app_resource_root(), "src", "core", "data", "fulltext_test_samples.json")

#: 站点付费墙概率分档：订阅制正刊在前。RSS 候选已由 OA 探测筛过一轮，这里只用来排序，
#: 决定"先拿哪一篇当测试数据"。
_HOST_TIERS: Tuple[Tuple[str, int], ...] = (
    ("cell.com", 0),
    ("annualreviews.org", 1),
    ("nature.com", 2),
    ("pnas.org", 3),
)

#: 样例字典的键（两级来源统一成同一形状，调用方不必分支）。
_FIELDS = ("title", "doi", "url", "journal", "publisher", "note")


def _clean(value: Any) -> str:
    """折叠空白并去首尾空白：RSS 标题里常带换行，进提示词前必须收拾干净。"""
    return " ".join(str(value or "").split())


def _host_of(url: str) -> str:
    host = urlsplit(_clean(url)).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _tier_of(url: str) -> int:
    host = _host_of(url)
    for needle, tier in _HOST_TIERS:
        if host == needle or host.endswith("." + needle):
            return tier
    return len(_HOST_TIERS)


def _normalize(entry: Dict[str, Any]) -> Dict[str, str]:
    """把候选统一成样例形状；缺字段留空串，调用方无需判 None。"""
    return {key: _clean(entry.get(key)) for key in _FIELDS}


def _dedupe_key(entry: Dict[str, str]) -> str:
    return (entry.get("url") or entry.get("title") or "").lower().rstrip("/")


def _load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        logger.debug("Sample source missing: %s", path)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Cannot read the sample source %s: %s", path, e)
    return None


def paywalled_from_rss_cache(limit: int = 5) -> List[Dict[str, str]]:
    """从本机 RSS 文章缓存里取付费墙候选（``pdf_url`` 为空的条目）。

    缓存结构为 ``{feed_url: [article, ...], "_meta": {...}}``；条目按"带 DOI 优先 ->
    站点分档 -> 标题"排序——DOI 是模型与取全文窗口定位论文的主键，站点分档只是让订阅制
    正刊排在前面。同一份缓存的排序是确定的，便于复现同一次自检。
    """
    data = _load_json(RSS_CACHE_FILE)
    if not isinstance(data, dict):
        return []

    picked: Dict[str, Dict[str, str]] = {}
    for feed_url, articles in data.items():
        if feed_url == "_meta" or not isinstance(articles, list):
            continue
        for article in articles:
            if not isinstance(article, dict) or _clean(article.get("pdf_url")):
                continue  # 有开放获取全文 -> 测不出取全文链路
            # 缓存条目里落地页字段名是 ``link``（与 feedparser 一致），样例统一叫 ``url``。
            entry = _normalize({
                "title": article.get("title"),
                "doi": article.get("doi"),
                "url": article.get("link") or article.get("url"),
                "note": "本机 RSS 缓存：抓取时未检出开放获取全文",
            })
            if not (entry["url"] or entry["title"]):
                continue
            key = _dedupe_key(entry)
            if key not in picked:
                picked[key] = entry

    ranked = sorted(
        picked.values(),
        key=lambda e: (0 if e["doi"] else 1, _tier_of(e["url"]), e["title"]),
    )
    return ranked[:max(1, int(limit))]


def bundled_samples(limit: int = 5) -> List[Dict[str, str]]:
    """随包分发的内置样例（软件分发后没有 RSS 订阅时的兜底）。"""
    data = _load_json(BUNDLED_SAMPLES_FILE)
    if not isinstance(data, dict):
        return []
    entries = data.get("samples")
    if not isinstance(entries, list):
        logger.warning("Bundled sample file has no 'samples' list: %s", BUNDLED_SAMPLES_FILE)
        return []
    out: List[Dict[str, str]] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        entry = _normalize(raw)
        if not (entry["url"] or entry["title"]):
            continue
        out.append(entry)
    return out[:max(1, int(limit))]


def resolve_samples(limit: int = 5) -> Tuple[List[Dict[str, str]], str]:
    """样例来源解析：返回 ``(样例列表, 来源说明)``，本机缓存优先、内置样例兜底。"""
    cached = paywalled_from_rss_cache(limit=limit)
    if cached:
        origin = f"local RSS cache ({RSS_CACHE_FILE}, {len(cached)} paywalled candidate(s))"
        logger.info("Full-text samples resolved from the RSS cache: %d candidate(s).", len(cached))
        return cached, origin

    bundled = bundled_samples(limit=limit)
    if bundled:
        origin = f"bundled sample file ({BUNDLED_SAMPLES_FILE}, {len(bundled)} sample(s))"
        logger.info("RSS cache gave no paywalled sample; using the bundled samples: %d.",
                    len(bundled))
        return bundled, origin

    logger.warning("No full-text sample available: neither %s nor %s yielded one.",
                   RSS_CACHE_FILE, BUNDLED_SAMPLES_FILE)
    return [], "no sample source available"


__all__ = [
    "BUNDLED_SAMPLES_FILE",
    "paywalled_from_rss_cache",
    "bundled_samples",
    "resolve_samples",
]