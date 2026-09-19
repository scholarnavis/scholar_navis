"""Web tools: webpage fetching, search engines, Wikipedia and GitHub repos."""
import ipaddress
import json
import re
import socket
import urllib.parse
from typing import Literal

from src.core.academic.base import (
    logger, mcp_request, simple_retry, global_rate_limiter, github_token,
)

__all__ = [
    "fetch_webpage_content", "search_web",
    "fetch_wikipedia_summary", "search_github_repos",
]


@simple_retry(max_attempts=2, delay=1)
def fetch_webpage_content(url: str, timeout: int = 15) -> str:
    logger.info(f"Task: Fetch Webpage | URL: '{url}'")
    if not url.startswith(("http://", "https://")): return json.dumps(
        {"status": "error", "message": "Security Error: Only HTTP(S) allowed."})
    parsed_url = urllib.parse.urlparse(url)
    hostname = parsed_url.hostname
    if hostname in ['localhost', 'broadcasthost'] or hostname.endswith('.local'):
        return json.dumps({"status": "error", "message": "Security Error: Local network access forbidden."})
    try:
        ip_obj = ipaddress.ip_address(socket.gethostbyname(hostname))
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
            return json.dumps(
                {"status": "error", "message": "Security Error: Probing internal network IPs is forbidden."})
    except Exception:
        pass
    try:
        res = mcp_request("GET", url, timeout=timeout)
        res.raise_for_status()
        html_content = res.text
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')

        for script_or_style in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
            script_or_style.decompose()

        text_content = soup.get_text(separator=' ', strip=True)
        if len(text_content) > 30000:
            text_content = text_content[:30000] + "\n...[Content truncated]"

        return json.dumps({"status": "success", "url": url, "content": text_content}, ensure_ascii=False)

    except Exception as e:
        err_str = str(e)
        if "403" in err_str or "Forbidden" in err_str:
            err_str += " (Access Denied: The website actively blocks automated access or requires a subscription/captcha.)"
        elif "404" in err_str or "Not Found" in err_str:
            err_str += " (Not Found: The page does not exist.)"
        return json.dumps({"status": "error", "message": err_str})


@simple_retry(max_attempts=2, delay=1)
def search_web(query: str, engine: Literal["duckduckgo", "google", "bing", "baidu"] = "duckduckgo",
               max_results: int = 3) -> str:
    logger.info(f"Task: Web Search ({engine}) | Query: '{query}'")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7"
        }
        results = []
        from bs4 import BeautifulSoup

        safe_query = urllib.parse.quote(query)

        if engine == "duckduckgo":
            # 使用 DuckDuckGo HTML 端点替代 Lite 端点以绕过部分限制
            url = "https://html.duckduckgo.com/html/"
            data = {"q": query}
            headers.update({"Origin": "https://html.duckduckgo.com", "Referer": "https://html.duckduckgo.com/",
                            "Content-Type": "application/x-www-form-urlencoded"})
            res = mcp_request("POST", url, data=data, headers=headers, timeout=10)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, 'html.parser')

            # 更新针对 HTML 端点的 CSS 类名解析逻辑
            for div in soup.find_all('div', class_=re.compile(r'result ')):
                if len(results) >= max_results: break
                a = div.find('a', class_='result__url')
                if not a: continue

                title_elem = div.find('h2', class_='result__title')
                title = title_elem.get_text(strip=True) if title_elem else a.get_text(strip=True)
                link = a.get('href', '')
                snippet_div = div.find('a', class_='result__snippet')
                snippet = snippet_div.get_text(separator=" ", strip=True) if snippet_div else "No abstract."

                if link and not link.startswith(('/', 'duckduckgo.com')):
                    results.append(
                        {"_mcp_cite_id": str(101 + len(results)), "cite_link": link, "title": title, "url": link,
                         "snippet": snippet})

        elif engine == "bing":
            url = f"https://www.bing.com/search?q={safe_query}"
            res = mcp_request("GET", url, headers=headers, timeout=10)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, 'html.parser')
            for li in soup.find_all('li', class_='b_algo'):
                if len(results) >= max_results: break
                h2 = li.find('h2')
                a = h2.find('a') if h2 else None
                if not a or not a.get('href'): continue
                title = a.get_text(strip=True)
                link = a.get('href')
                snippet_div = li.find('div', class_='b_caption') or li.find('p')
                snippet = snippet_div.get_text(separator=" ", strip=True) if snippet_div else "No abstract."
                results.append({"_mcp_cite_id": str(101 + len(results)), "cite_link": link, "title": title, "url": link,
                                "snippet": snippet})


        elif engine == "google":
            url = f"https://www.google.com/search?q={safe_query}"
            res = mcp_request("GET", url, headers=headers, timeout=10)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, 'html.parser')
            for div in soup.find_all('div', class_='g'):
                if len(results) >= max_results: break
                a = div.find('a')
                h3 = div.find('h3')
                if not a or not a.get('href') or not h3: continue
                title = h3.get_text(strip=True)
                link = a.get('href')
                snippet_div = div.find('div', class_='VwiC3b') or div.find('div',
                                                                           style=re.compile(r'-webkit-line-clamp'))
                snippet = snippet_div.get_text(separator=" ", strip=True) if snippet_div else "No abstract."
                results.append({"_mcp_cite_id": str(101 + len(results)), "cite_link": link, "title": title, "url": link,
                                "snippet": snippet})

        elif engine == "baidu":
            url = f"https://www.baidu.com/s?wd={safe_query}"
            res = mcp_request("GET", url, headers=headers, timeout=10)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, 'html.parser')
            for div in soup.find_all('div', class_=re.compile(r'result c-container')):
                if len(results) >= max_results: break
                h3 = div.find('h3')
                a = h3.find('a') if h3 else None
                if not a or not a.get('href'): continue
                title = a.get_text(strip=True)
                link = a.get('href')
                snippet_div = div.find('div', class_=re.compile(r'c-abstract'))
                snippet = snippet_div.get_text(separator=" ", strip=True) if snippet_div else "No abstract."
                results.append({"_mcp_cite_id": str(101 + len(results)), "cite_link": link, "title": title, "url": link,
                                "snippet": snippet})

        if not results:
            return json.dumps({"status": "error",
                               "message": f"{engine.capitalize()} blocked the request or returned no results. Try another engine or 'search_academic_literature'."})

        return json.dumps({
            "status": "success",
            "engine": engine.capitalize(),
            "query": query,
            "results": results
        }, ensure_ascii=False)

    except ImportError:
        logger.error("Missing library: beautifulsoup4")
        return json.dumps({"status": "error", "message": "Please install beautifulsoup4 via pip."})
    except Exception as e:
        logger.error(f"Web search failed: {e}")
        return json.dumps({"status": "error",
                           "message": f"Search engine error: {str(e)}. Consider using 'duckduckgo' if 403 Forbidden occurs."})


@simple_retry(max_attempts=2, delay=1)
def fetch_wikipedia_summary(query: str, language: str = "en") -> str:
    logger.info(f"Task: Wikipedia Extract | Query: '{query}'")
    try:
        url = f"https://{language}.wikipedia.org/w/api.php"
        params = {"action": "query", "prop": "extracts", "exchars": 1500, "explaintext": 1, "generator": "search",
                  "gsrsearch": query, "gsrlimit": 1, "format": "json"}
        res = mcp_request("GET", url, params=params, timeout=10)
        res.raise_for_status()
        pages = res.json().get("query", {}).get("pages", {})
        if not pages: return json.dumps({"status": "error", "message": "No Wikipedia article found."})
        page = list(pages.values())[0]
        return json.dumps({
            "status": "success",
            "results": [{
                "title": page.get("title", ""),
                "abstract": page.get("extract", "").strip(),
                "url": f"https://{language}.wikipedia.org/wiki/{urllib.parse.quote(page.get('title', ''))}"
            }]
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@simple_retry(max_attempts=2, delay=1)
def search_github_repos(query: str, max_results: int = 5) -> str:
    """
    Search GitHub for reproducible bioinformatics tools and pipelines.

    Helps researchers discover open-source software, analysis pipelines, and
    algorithm implementations for multi-omics data. Results include license,
    last-updated time, and open-issues counts so users can judge maintenance
    status and citability.
    """
    logger.info(f"Task: GitHub Repo Search | Query: '{query}' | max_results: {max_results}")

    github_rph = 4900 if github_token else 50
    global_rate_limiter.acquire("github", rph=github_rph)

    try:
        url = "https://api.github.com/search/repositories"
        params = {"q": query, "sort": "stars", "order": "desc", "per_page": max_results}
        headers = {"Accept": "application/vnd.github.v3+json",
                   "Authorization": f"Bearer {github_token}" if github_token else ""}
        res = mcp_request("GET", url, params=params, headers=headers, timeout=10)
        res.raise_for_status()

        results = []
        for r in res.json().get("items", []) or []:
            if not isinstance(r, dict):
                continue
            license_obj = r.get("license") or {}
            license_name = license_obj.get("spdx_id", "") if isinstance(license_obj, dict) else ""
            topics = r.get("topics", []) or []
            results.append({
                "name": r.get("full_name", ""),
                "description": r.get("description", "No description"),
                "url": r.get("html_url", ""),
                "stars": r.get("stargazers_count", 0),
                "language": r.get("language", "Unknown"),
                "license": license_name or "Not specified",
                "topics": topics if isinstance(topics, list) else [],
                "open_issues": r.get("open_issues_count", 0),
                "last_updated": (r.get("updated_at", "") or "")[:10],
                "reproducibility_note": (
                    "License present; suitable for citation." if license_name
                    else "No license detected; verify before reuse."
                ),
            })

        if not results:
            return json.dumps({"status": "success", "results": [],
                               "message": f"No repositories found for '{query}'."})

        return json.dumps({"status": "success", "results": results}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
