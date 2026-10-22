"""PDF 正文抽取（唯一实现）。

被两处复用：
* OA 全文获取（``src/core/academic/literature.py``）：把下载到的 OA PDF 变成文本；

性能约束（这是全链路最贵的一步）：
* **只打开一次**：元数据、首页 DOI、正文都从同一个 ``fitz.Document`` 取；
  ``pymupdf4llm`` 直接接收已打开的 doc（旧版本不支持该参数时退回传路径，
  但那只在库版本过低时发生一次）。
* **markdown 失败就降级纯文本**：布局分析（表格/列表重建）在几十页的 PDF 上
  是数十秒级；拿不到 markdown 时不重试，直接按页 ``get_text`` —— 对检索来说
  纯文本完全够用，比“再解析一遍”划算得多。
"""
import logging
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("Core.PdfText")

_DOI_RE = None


def extract_pdf(path: str) -> Tuple[List[Tuple[int, str]], Dict[str, Any]]:
    """返回 ``[(页码, 文本)]`` 与 PDF 内嵌元数据；解析失败抛异常。"""
    import fitz  # PyMuPDF

    pages: List[Tuple[int, str]] = []
    meta: Dict[str, Any] = {}
    first_text = ""
    page_data: List[Dict[str, Any]] = []
    doc = None
    try:
        doc = fitz.open(path)
        info = doc.metadata or {}
        meta = {
            "title": str(info.get("title") or "").strip(),
            "authors": str(info.get("author") or "").strip(),
            "journal": str(info.get("subject") or "").strip(),
            "year": "",
        }
        first_text = " ".join(
            doc[i].get_text() for i in range(min(3, doc.page_count)))

        try:
            import pymupdf4llm
            try:
                page_data = pymupdf4llm.to_markdown(doc, page_chunks=True) or []
            except TypeError:           # 旧版只接受路径
                page_data = pymupdf4llm.to_markdown(path, page_chunks=True) or []
        except Exception as e:
            logger.debug("Markdown extraction unavailable (%s); falling back to text.", e)
            page_data = []

        for chunk in page_data:
            text = str(chunk.get("text") or "").strip()
            if not text:
                continue
            page = int((chunk.get("metadata") or {}).get("page", 0)) + 1
            pages.append((page, text))

        if not pages:
            # 降级：按页纯文本，一次遍历即可（C 层取词，比 markdown 快一个量级）
            for index in range(doc.page_count):
                text = (doc[index].get_text("text") or "").strip()
                if text:
                    pages.append((index + 1, text))
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass

    # DOI 通常出现在首页/页眉；用正则兜底，避免依赖 PDF 的 XMP 元数据。
    global _DOI_RE
    if _DOI_RE is None:
        _DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
    match = _DOI_RE.search(first_text)
    if match:
        meta["doi"] = match.group(0).rstrip(".,;).")
    logger.debug("PDF parsed: %s (%d page block(s), doi=%s)",
                 path, len(pages), meta.get("doi", ""))
    return pages, meta


__all__ = ["extract_pdf"]