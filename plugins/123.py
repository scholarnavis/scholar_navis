import json
import urllib.request
import xml.etree.ElementTree as ET

# ==========================================
# 1. 必须定义的 SCHEMA (OpenAI 格式)
# ==========================================
SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetch_arxiv_summary",  # 注意：这个名字会显示在你的 Chat 工具过滤器列表里
        "description": "Fetch the title, authors, and abstract summary of a paper from ArXiv using its ArXiv ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "arxiv_id": {
                    "type": "string",
                    "description": "The exact ArXiv ID of the paper (e.g., '2303.08774' or '1706.03762')."
                }
            },
            "required": ["arxiv_id"]
        }
    }
}


# ==========================================
# 2. 必须定义的 execute 函数
# ==========================================
def execute(arxiv_id: str) -> str:
    """
    实际执行逻辑。
    参数名称必须和 SCHEMA 中的 properties 键名完全一致。
    返回值建议是 JSON 格式的字符串，这样大模型最容易理解。
    """
    # ArXiv API 接口地址
    url = f"http://export.arxiv.org/api/query?id_list={arxiv_id}"

    try:
        # 发送请求 (带上通用 User-Agent 防拦截)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10.0) as response:
            xml_data = response.read()

        # 解析返回的 XML 数据
        root = ET.fromstring(xml_data)
        ns = {'atom': 'http://www.w3.org/2005/Atom'}
        entry = root.find('atom:entry', ns)

        # 没找到论文的异常处理
        if entry is None:
            return json.dumps({"status": "error", "message": f"No paper found for ArXiv ID: {arxiv_id}"})

        # 提取关键信息并清洗换行符
        title = entry.find('atom:title', ns).text.replace('\n', ' ').strip()
        summary = entry.find('atom:summary', ns).text.replace('\n', ' ').strip()
        authors = [author.find('atom:name', ns).text for author in entry.findall('atom:author', ns)]
        published = entry.find('atom:published', ns).text

        # 构建给大模型的结果
        result = {
            "status": "success",
            "arxiv_id": arxiv_id,
            "title": title,
            "published_date": published,
            "authors": ", ".join(authors),
            "abstract": summary
        }

        return json.dumps(result, ensure_ascii=False)

    except urllib.error.URLError as e:
        return json.dumps({"status": "error", "message": f"Network error connecting to ArXiv: {e.reason}"})
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Unexpected error during execution: {str(e)}"})