"""PDF 本体索引：paper_id / DOI / SHA-256 → 本地文件路径。

为什么要单独建索引
------------------
PDF 本体按内容寻址存在 ``scholar_workspace/private_pdfs/<sha256>.pdf``，文件名里
没有 DOI、没有标题。要回答"这篇论文的 PDF 在哪""这篇是不是已经下过了"，此前只能
翻 manifest（一个大 JSON，每次改都要整份重写）或者遍历目录——文件一多就是穷举。

本模块把映射关系放进独立的 SQLite（WAL）：
* ``paper_id`` 主键：同一篇论文只有一行，重复获取直接命中；
* ``doi`` / ``sha256`` 二级索引：按 DOI 查、按内容去重都是 O(log n)；
* 与向量库无关：换 embedding 模型、清空向量索引都不影响这里的记录。

口径
----
* 只登记**已经落盘成功**的文件（写、校验、改名之后再登记），不做预留；
* 删除会话时只解除会话关联（``conversation_id`` 置空），文件本身作为缓存保留：
  "同一篇论文只获取一次"依赖它；
* 索引文件不参与 git（``scholar_workspace/`` 已在 .gitignore 中）。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

from src.core import BASE_DIR

logger = logging.getLogger("Core.PdfIndex")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pdfs (
    paper_id        TEXT PRIMARY KEY,
    doi             TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',
    path            TEXT NOT NULL,
    sha256          TEXT NOT NULL DEFAULT '',
    size            INTEGER NOT NULL DEFAULT 0,
    mtime           REAL NOT NULL DEFAULT 0,
    conversation_id TEXT NOT NULL DEFAULT '',
    origin          TEXT NOT NULL DEFAULT '',
    indexed_at      REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pdfs_doi  ON pdfs(doi);
CREATE INDEX IF NOT EXISTS idx_pdfs_sha  ON pdfs(sha256);
CREATE INDEX IF NOT EXISTS idx_pdfs_conv ON pdfs(conversation_id);
"""


def index_path() -> str:
    return os.path.join(BASE_DIR, "scholar_workspace", "pdf_index.sqlite3")


def _connect() -> Optional[sqlite3.Connection]:
    try:
        path = index_path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_SCHEMA)
        return conn
    except Exception as e:
        logger.debug("PDF index unavailable: %s", e)
        return None


def index_pdf(paper_id: str, path: str, *, doi: str = "", title: str = "",
              sha256: str = "", conversation_id: str = "",
              origin: str = "") -> bool:
    """登记一份 PDF；同一 ``paper_id`` 覆盖更新。返回是否写入成功。"""
    pid = str(paper_id or "").strip()
    file_path = str(path or "").strip()
    if not pid or not file_path:
        return False
    try:
        size = os.path.getsize(file_path)
        mtime = os.path.getmtime(file_path)
    except OSError as e:
        logger.debug("PDF index skipped (file missing): %s (%s)", file_path, e)
        return False
    conn = _connect()
    if conn is None:
        return False
    try:
        with conn:
            conn.execute(
                "INSERT INTO pdfs(paper_id, doi, title, path, sha256, size, mtime, "
                "conversation_id, origin, indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(paper_id) DO UPDATE SET "
                "doi=excluded.doi, title=excluded.title, path=excluded.path, "
                "sha256=excluded.sha256, size=excluded.size, mtime=excluded.mtime, "
                "conversation_id=excluded.conversation_id, origin=excluded.origin, "
                "indexed_at=excluded.indexed_at",
                (pid, str(doi or "").strip().lower(), str(title or ""),
                 file_path, str(sha256 or ""), int(size), float(mtime),
                 str(conversation_id or ""), str(origin or ""), time.time()))
        return True
    except Exception as e:
        logger.debug("PDF index write failed for %s: %s", pid, e)
        return False
    finally:
        conn.close()


def lookup(paper_id: str = "", doi: str = "", sha256: str = "") -> Optional[Dict[str, Any]]:
    """按 paper_id / DOI / SHA-256 查一条记录（路径必须仍然存在）。"""
    clauses, params = [], []
    if paper_id:
        clauses.append("paper_id = ?")
        params.append(str(paper_id).strip())
    if doi:
        clauses.append("doi = ?")
        params.append(str(doi).strip().lower())
    if sha256:
        clauses.append("sha256 = ?")
        params.append(str(sha256).strip())
    if not clauses:
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM pdfs WHERE " + " AND ".join(clauses) +
            " ORDER BY indexed_at DESC LIMIT 1", params).fetchone()
        if row is None:
            return None
        item = dict(row)
        # 文件被用户删掉时不要把死路径交出去
        return item if os.path.isfile(item["path"]) else None
    except Exception as e:
        logger.debug("PDF index lookup failed: %s", e)
        return None
    finally:
        conn.close()


def has(paper_id: str = "", doi: str = "") -> bool:
    return lookup(paper_id=paper_id, doi=doi) is not None


def for_conversation(conversation_id: str) -> List[Dict[str, Any]]:
    """某对话登记过的 PDF（供证据面板显示"有全文"与打开文件，不遍历目录）。"""
    cid = str(conversation_id or "")
    if not cid:
        return []
    conn = _connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM pdfs WHERE conversation_id = ? ORDER BY indexed_at DESC",
            (cid,)).fetchall()
        return [dict(r) for r in rows if os.path.isfile(r["path"])]
    except Exception as e:
        logger.debug("PDF index list failed: %s", e)
        return []
    finally:
        conn.close()


def detach_conversation(conversation_id: str) -> int:
    """解除会话关联（文件作为缓存保留）；返回受影响的记录数。"""
    cid = str(conversation_id or "")
    if not cid:
        return 0
    conn = _connect()
    if conn is None:
        return 0
    try:
        with conn:
            cur = conn.execute(
                "UPDATE pdfs SET conversation_id = '' WHERE conversation_id = ?", (cid,))
        return int(cur.rowcount or 0)
    except Exception as e:
        logger.debug("PDF index detach failed: %s", e)
        return 0
    finally:
        conn.close()


__all__ = ["index_path", "index_pdf", "lookup", "has",
           "for_conversation", "detach_conversation"]
