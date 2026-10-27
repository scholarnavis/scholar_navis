"""对话与归档的 SQLite 存储后端 —— 落盘口径的**唯一事实来源**。

为什么不再用 JSON / JSON + ZIP
------------------------------
旧实现把"整段对话"当一个 JSON 文档（历史快照 ``tab_<id>.json``、归档容器
``.schat`` = ZIP + ``chat_history.json``）。要记的东西越来越多——正文、标签、
书签、小型附件、图片、参考文献，以及"一个标签下的多个对话"——这种"整篇重写
一个大对象"的模型同时碰到三个问题：

1. **无法局部更新**：改一条书签也要重写整段对话；对话越长，单次保存越慢；
2. **结构靠约定**：参考文献、书签、组件状态都挤在消息字典里，任何一处
   "顺手过滤一下字段"就会静默丢数据，且事后无法用查询发现；
3. **容量与检索**：书签、引用、标签需要按内容检索，JSON 只能全量扫描。

现在统一为 SQLite：

* 历史快照 —— ``scholar_workspace/chat_session/session.sqlite3``；
* 导出归档 —— 单个 ``.schat`` 文件（其内部就是一个 SQLite 数据库）。

两者的表结构相同（本模块 ``_SCHEMA_SQL``），区别只在资产本的存放方式：
会话把图片 / 附件留在内容寻址仓库里（界面按路径显示，仓库跨标签页去重），
归档把资产本体以 BLOB 写进同一个文件，因此"一份 .schat"就是完整可移植的。

结构化优先，其余无损
--------------------
* ``message_references`` —— 参考文献**逐条成行**（编号、DOI、标题等可查询），
  非常规字段进 ``extra``（JSON）。任何一侧"多加一个字段"都不需要迁移，也不会
  因为遍历消息时漏抄而丢失；
* ``message_bookmarks`` —— 书签逐条成行；
* ``messages.payload`` —— 除上述两类以外的全部消息字段（原样 JSON，超长时
  zlib 压缩）；``role`` / ``content`` / ``status`` 另存为冗余列仅供查询；
* ``tags`` / ``tab_tags`` —— 标签与对话的多对多关系（一个标签下多个对话、
  一段对话挂多个标签）；
* ``assets`` / ``asset_links`` / ``placeholders`` —— 内容寻址资产与占位说明。

兼容
----
历史侧由 :mod:`src.core.chat_session` 负责把旧的 ``tabs.json`` / ``tab_*.json``
/ ``autosave.json`` 迁进来；归档侧由 :mod:`src.core.chat_archive` 按魔数分派，
旧的纯 JSON 与 ZIP 归档仍可读。本模块只提供读写原语，不判断容器版本。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
import zlib
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("Core.ChatStore")

#: SQLite 文件头：读取归档时据此判定容器类型（PK = ZIP，其余按 JSON）。
SQLITE_MAGIC = b"SQLite format 3\x00"

#: 表结构版本（``meta.schema_version``）。新增列时要能容忍"旧库没有它"。
#: v3：新增 ``messages.digest``，让每轮保存只需重写真正变化的消息。
SCHEMA_VERSION = 3

#: 逐版本迁移脚本：``{目标版本: (SQL, ...)}``。
#: 新库由 :data:`_SCHEMA_SQL` 直接建出最终结构，这些语句会因"列已存在"被跳过；
#: 旧库则只补差异列，不重建表、不动业务数据。
_MIGRATIONS: Dict[int, Tuple[str, ...]] = {
    3: ("ALTER TABLE messages ADD COLUMN digest TEXT NOT NULL DEFAULT ''",),
}

#: 单条消息解压后的字节上限（思维链通常几百 KB，正常远低于此值）
_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024

#: ``_read_messages`` 里批量补齐引用/书签时的分块大小，避免超长对话触发
#: SQLite 的变量数量上限（旧实现在几百条消息时就会整页读不出来）
_IN_CHUNK = 500

#: ``meta.format``：会话快照与归档各自一个标识，读取侧可据此拒绝错配文件。
FORMAT_CHAT_TABS = "scholar_navis_chat_tabs"
FORMAT_CHAT_HISTORY = "scholar_navis_chat_history"

#: ``meta.kind``：同一套表既可当"本机会话库"，也可当"可移植归档"。
KIND_SESSION = "session"
KIND_ARCHIVE = "archive"

#: 归档内所有资产共用一个 scope；会话里 scope 就是标签页 id。
ARCHIVE_SCOPE = "archive"


#: 由独立表承载的消息字段：**不**再抄进 payload，避免同一份数据两处副本。
_STRUCTURED_KEYS = ("references", "bookmarks")

#: 引用条目的常规列，其余字段整体进 ``extra``。
_REF_COLUMNS = ("kind", "title", "authors", "year", "journal", "doi", "url", "path")

#: payload 超过该长度才压缩（短文本压了反而更大，还要多一次解压）。
_PAYLOAD_COMPRESS_MIN = 2048

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tabs (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    position   INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tab_id    TEXT NOT NULL REFERENCES tabs(id) ON DELETE CASCADE,
    seq       INTEGER NOT NULL,
    role      TEXT NOT NULL DEFAULT '',
    content   TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL DEFAULT '',
    payload   TEXT NOT NULL DEFAULT '{}',
    payload_z INTEGER NOT NULL DEFAULT 0,
    digest    TEXT NOT NULL DEFAULT '',
    UNIQUE(tab_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_messages_tab ON messages(tab_id, seq);
CREATE TABLE IF NOT EXISTS message_references (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    ordinal   INTEGER NOT NULL DEFAULT 0,
    ref_index INTEGER,
    kind      TEXT,
    title     TEXT,
    authors   TEXT,
    year      TEXT,
    journal   TEXT,
    doi       TEXT,
    url       TEXT,
    path      TEXT,
    extra     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_refs_message ON message_references(message_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_refs_doi ON message_references(doi);
CREATE TABLE IF NOT EXISTS message_bookmarks (
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL DEFAULT 0,
    bookmark_id TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (message_id, ordinal)
);
CREATE TABLE IF NOT EXISTS inflight (
    tab_id   TEXT PRIMARY KEY,
    content  TEXT NOT NULL DEFAULT '',
    saved_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS assets (
    sha256  TEXT PRIMARY KEY,
    name    TEXT NOT NULL DEFAULT '',
    size    INTEGER NOT NULL DEFAULT 0,
    entry   TEXT NOT NULL DEFAULT '',
    relpath TEXT NOT NULL DEFAULT '',
    blob    BLOB
);
CREATE TABLE IF NOT EXISTS asset_links (
    scope    TEXT NOT NULL,
    original TEXT NOT NULL,
    sha256   TEXT NOT NULL,
    PRIMARY KEY (scope, original)
);
CREATE TABLE IF NOT EXISTS placeholders (
    scope    TEXT NOT NULL,
    original TEXT NOT NULL,
    meta     TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (scope, original)
);
CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    color      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tab_tags (
    tab_id TEXT NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (tab_id, tag_id)
);
-- 标签页附属负载：目前只有"证据快照"（文献元数据 + 检索正文）。放在会话库
-- 而不是向量库，是因为它必须随历史 / 导出 / 导入一起走，且**不依赖 embedding
-- 模型**——换模型时向量索引可以重建，这份正文不能丢。
CREATE TABLE IF NOT EXISTS tab_payloads (
    tab_id     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    body       TEXT NOT NULL,
    body_z     INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (tab_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_tab_payloads_tab ON tab_payloads(tab_id);
"""


# --------------------------------------------------------------------------- #
#  序列化口径（归档与快照共用）
# --------------------------------------------------------------------------- #
def sanitize(value):
    """把任意对象降级为 JSON 可序列化结构（不可序列化的转 str）。

    历史快照与归档写的是同一批消息条目，降级规则必须一致，否则会出现
    "导得出去、存不下来"。这是全应用唯一的落盘降级实现。
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    try:
        return str(value)
    except Exception:  # pragma: no cover - 纯防御
        return None


def _dumps(value: Any) -> str:
    return json.dumps(sanitize(value), ensure_ascii=False)


def _loads(text: Any, default):
    if isinstance(text, (dict, list)):
        return text
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
#  连接与建库
# --------------------------------------------------------------------------- #
def is_sqlite_file(path: str) -> bool:
    """路径是否是一个（至少看起来像）SQLite 数据库。"""
    try:
        with open(path, "rb") as f:
            return f.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def connect(path: str, *, readonly: bool = False, wal: bool = False) -> sqlite3.Connection:
    """打开数据库连接。

    :param readonly: 只读打开（读取外部归档时用，避免对用户文件产生副作用）。
    :param wal: 会话库使用 WAL：单文件、并发读友好，崩溃后自动回滚未提交事务。
        归档库刻意**不用** WAL——它是要被复制 / 移动的单个文件，不能让
        ``-wal`` / ``-shm`` 伴生文件留在用户目录里。
    """
    directory = os.path.dirname(os.path.abspath(path))
    if not readonly:
        os.makedirs(directory, exist_ok=True)
    if readonly:
        absolute = os.path.abspath(path)
        conn = sqlite3.connect(f"file:{absolute}?mode=ro", uri=True, timeout=10.0)
    else:
        conn = sqlite3.connect(path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if wal and not readonly:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.DatabaseError as e:  # pragma: no cover - 纯防御
            logger.warning("Cannot enable WAL on '%s': %s", path, e)
    return conn


def init_schema(conn: sqlite3.Connection, *, fmt: str, kind: str) -> None:
    """建表、补齐缺失列并写入格式标识。

    已就绪的库直接返回：这个函数在**每次**打开会话库时都会被调用（每轮对话都要
    落盘一次），重复执行建表脚本会白白多花几毫秒。

    旧库升级只补差异列，不重建表、不触碰业务数据；升级前的库文件由
    :func:`create_database` 备份为 ``<path>.bak_v<旧版本>``。
    """
    if _schema_ready(conn):
        return

    previous = _schema_version(conn)
    conn.executescript(_SCHEMA_SQL)

    # 新库由建表脚本直接产出最终结构，逐版本迁移语句会因"列已存在"被跳过；
    # 旧库借此只补差异列。两者共用同一段逻辑，避免出现两套升级路径。
    for version in range(max(previous, 0) + 1, SCHEMA_VERSION + 1):
        for statement in _MIGRATIONS.get(version, ()):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    logger.error("Schema migration to v%d failed: %s", version, e)
                    raise

    now = time.time()
    with conn:
        # 版本号必须**无条件**推进：否则旧库永远停在旧版本，`_schema_ready` 恒为
        # False，每次打开都要重跑一遍建表脚本。其余元数据只补空值，不覆盖既有值。
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (str(SCHEMA_VERSION),))
        for key, value in (("format", str(fmt)),
                           ("kind", str(kind)),
                           ("created_at", str(now))):
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = CASE WHEN meta.value = '' "
                "THEN excluded.value ELSE meta.value END", (key, value))
    if previous and previous < SCHEMA_VERSION:
        logger.info("Database schema upgraded: v%d -> v%d", previous, SCHEMA_VERSION)


def _schema_version(conn: sqlite3.Connection) -> int:
    """库内记录的表结构版本；无 ``meta`` 表（全新库）返回 0。"""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.DatabaseError:
        return 0
    if row is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def _schema_ready(conn: sqlite3.Connection) -> bool:
    """库内表结构是否已是当前版本（缺表 / 版本落后都返回 ``False``）。"""
    return _schema_version(conn) == SCHEMA_VERSION


def _backup_before_migration(conn: sqlite3.Connection, path: str, version: int) -> Optional[str]:
    """升级表结构前整库备份，返回备份路径。

    会话库是用户不可再生的数据，升级一旦出问题必须有办法退回去。WAL 库要先
    checkpoint，否则复制出来的文件可能缺少最近提交的事务。
    """
    try:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
    except sqlite3.DatabaseError as e:
        logger.warning("Cannot checkpoint before migration: %s", e)

    target = f"{path}.bak_v{version}"
    if os.path.exists(target):
        return target
    try:
        shutil.copy2(path, target)
        logger.info("Database backed up before migration: %s -> %s", path, target)
        return target
    except OSError as e:
        # 备份失败不阻断升级：单条 ALTER 失败会随事务回滚，且日志明确指出无备份可用
        logger.error("Cannot back up database before migration, proceeding: %s", e)
        return None


def create_database(path: str, *, fmt: str, kind: str, wal: bool = False) -> sqlite3.Connection:
    """新建（或打开）一个已建好表结构的数据库文件，必要时自动升级旧库。"""
    conn = connect(path, wal=wal)
    try:
        previous = _schema_version(conn)
        if 0 < previous < SCHEMA_VERSION and os.path.isfile(path):
            _backup_before_migration(conn, path, previous)
        init_schema(conn, fmt=fmt, kind=kind)
    except BaseException:
        conn.close()
        raise
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (str(key),)).fetchone()
    return str(row["value"]) if row is not None else default


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    with conn:
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (str(key), str(value)))


# --------------------------------------------------------------------------- #
#  消息：字典 <-> 行
# --------------------------------------------------------------------------- #
def _split_message(msg: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict], List[Dict]]:
    """拆成 ``(payload, references, bookmarks)``：结构化字段走独立表。"""
    payload = {k: v for k, v in msg.items() if k not in _STRUCTURED_KEYS}
    refs = [r for r in (msg.get("references") or []) if isinstance(r, dict)]
    bookmarks = [b for b in (msg.get("bookmarks") or []) if isinstance(b, dict)]
    return payload, refs, bookmarks


def _encode_payload(payload: Dict[str, Any]) -> Tuple[Any, int]:
    """序列化消息体；超长时压缩，返回 ``(text_or_blob, compressed_flag)``。

    压缩态按 BLOB 落盘（SQLite 允许 TEXT 列存 BLOB，且不会做隐式转换）：思维链
    动辄数百 KB，不压缩会让库文件比原来的 ZIP 归档大出数倍。
    """
    text = _dumps(payload)
    raw = text.encode("utf-8")
    if len(raw) < _PAYLOAD_COMPRESS_MIN:
        return text, 0
    packed = zlib.compress(raw, 6)
    if len(packed) >= len(raw):
        return text, 0
    return packed, 1


def _decode_payload(text: Any, compressed: int) -> Dict[str, Any]:
    """解出消息体。压缩态用有上限的解压器。

    ``.schat`` 是不可信输入，一条几十 KB 的压缩数据可以膨胀到 GB 级；
    ``zlib.decompress`` 没有长度参数，会直接把内存吃光。
    """
    raw = text if isinstance(text, (bytes, bytearray)) else str(text or "")
    if compressed:
        try:
            blob = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode("utf-8")
            obj = zlib.decompressobj()
            chunk = obj.decompress(bytes(blob), _MAX_PAYLOAD_BYTES + 1)
            if len(chunk) > _MAX_PAYLOAD_BYTES or obj.unconsumed_tail:
                raise ValueError(f"消息体解压后超过 {_MAX_PAYLOAD_BYTES} 字节上限")
            tail = obj.flush()
            if len(chunk) + len(tail) > _MAX_PAYLOAD_BYTES:
                raise ValueError(f"消息体解压后超过 {_MAX_PAYLOAD_BYTES} 字节上限")
            raw = (chunk + tail).decode("utf-8")
        except (zlib.error, UnicodeDecodeError, ValueError) as e:
            logger.warning("Corrupt compressed message payload skipped: %s", e)
            return {}
    value = _loads(raw, {})
    return value if isinstance(value, dict) else {}


def _message_digest(body: Any, compressed: int, refs: List[Dict],
                    bookmarks: List[Dict]) -> str:
    """消息内容指纹，用于判断某条消息是否需要重写。

    引用与书签走独立表、不参与 ``payload`` 编码，因此必须一并计入；否则
    "只加了一条书签"会被判定为未变化而跳过写入。
    """
    raw = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
    extra = _dumps([refs, bookmarks]).encode("utf-8")
    return hashlib.sha1(
        bytes(raw) + b"|" + str(int(compressed)).encode() + b"|" + extra).hexdigest()


def _ref_values(ref: Dict[str, Any]) -> Tuple:
    index = ref.get("index")
    try:
        index = int(index) if index is not None else None
    except (TypeError, ValueError):
        index = None
    row = [index]
    for column in _REF_COLUMNS:
        value = ref.get(column)
        row.append(None if value is None else str(value))
    extra = {k: v for k, v in ref.items() if k not in _REF_COLUMNS and k != "index"}
    row.append(_dumps(extra) if extra else "{}")
    return tuple(row)


def _ref_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    """还原一条引用：常规列 + ``extra``；缺失列（NULL）不补键，保证往返等值。"""
    entry: Dict[str, Any] = {}
    if row["ref_index"] is not None:
        entry["index"] = int(row["ref_index"])
    for column in _REF_COLUMNS:
        value = row[column]
        if value is not None:
            entry[column] = value
    extra = _loads(row["extra"], {})
    if isinstance(extra, dict):
        for key, value in extra.items():
            entry.setdefault(key, value)
    return entry


def _write_message(conn: sqlite3.Connection, tab_id: str, seq: int,
                   payload: Dict[str, Any], refs: List[Dict], bookmarks: List[Dict],
                   body: Any, compressed: int, digest: str) -> int:
    """按 ``(tab_id, seq)`` 写入一条消息，返回消息 id。

    用 upsert 而不是"删光重插"，是为了保住主键：``message_references`` 与
    ``message_bookmarks`` 通过 ``ON DELETE CASCADE`` 挂在消息上，主键一变，
    这两张表的历史行就要跟着重建。

    ``content`` 冗余列刻意留空：正文已经完整存在于（可压缩的）``payload`` 里，
    再存一份明文副本等于把压缩收益抵消一半，而这一列没有任何读取方。
    """
    conn.execute(
        "INSERT INTO messages(tab_id, seq, role, content, status, payload, payload_z, digest) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(tab_id, seq) DO UPDATE SET "
        "role = excluded.role, content = excluded.content, status = excluded.status, "
        "payload = excluded.payload, payload_z = excluded.payload_z, digest = excluded.digest",
        (tab_id, int(seq), str(payload.get("role") or ""), "",
         str(payload.get("status") or ""), body, int(compressed), digest))

    row = conn.execute("SELECT id FROM messages WHERE tab_id = ? AND seq = ?",
                       (tab_id, int(seq))).fetchone()
    message_id = int(row["id"])

    conn.execute("DELETE FROM message_references WHERE message_id = ?", (message_id,))
    for ordinal, ref in enumerate(refs):
        conn.execute(
            "INSERT INTO message_references(message_id, ordinal, ref_index, kind, title, "
            "authors, year, journal, doi, url, path, extra) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, ordinal) + _ref_values(ref))

    conn.execute("DELETE FROM message_bookmarks WHERE message_id = ?", (message_id,))
    for ordinal, bookmark in enumerate(bookmarks):
        conn.execute(
            "INSERT INTO message_bookmarks(message_id, ordinal, bookmark_id, title, payload) "
            "VALUES(?, ?, ?, ?, ?)",
            (message_id, ordinal, str(bookmark.get("id") or ""),
             str(bookmark.get("title") or ""), _dumps(bookmark)))
    return message_id


def _write_messages(conn: sqlite3.Connection, tab_id: str, msgs: List[Dict[str, Any]]) -> None:
    """增量写入某标签页的消息：只重写内容变化的行，并删除尾部多余行。

    旧实现每轮 ``DELETE + 全量 INSERT``，第 k 轮的写入量正比于前 k-1 轮全文，
    累计 O(n²)，表现为"对话越长，收尾越卡"。这里用 ``digest`` 列比对，
    常规追加轮次只写新增的两条。
    """
    stored = {int(row["seq"]): str(row["digest"] or "") for row in conn.execute(
        "SELECT seq, digest FROM messages WHERE tab_id = ?", (tab_id,))}

    count = len(msgs)
    if len(stored) > count:
        conn.execute("DELETE FROM messages WHERE tab_id = ? AND seq >= ?", (tab_id, count))

    for seq, msg in enumerate(msgs):
        payload, refs, bookmarks = _split_message(sanitize(msg))
        body, compressed = _encode_payload(payload)
        digest = _message_digest(body, compressed, refs, bookmarks)
        if stored.get(seq) == digest:
            continue
        _write_message(conn, tab_id, seq, payload, refs, bookmarks, body, compressed, digest)


def _read_messages(conn: sqlite3.Connection, tab_id: str) -> List[Dict[str, Any]]:
    """按 seq 顺序读回某标签页的全部消息（引用 / 书签整批补齐，避免 N+1 查询）。

    用 JOIN 而不是 ``message_id IN (?,?,…)``：后者在消息数超过 SQLite 的变量
    上限时会直接抛错，导致整个标签页读不出来。
    """
    rows = conn.execute(
        "SELECT id, payload, payload_z FROM messages WHERE tab_id = ? ORDER BY seq",
        (tab_id,)).fetchall()
    if not rows:
        return []

    refs: Dict[int, List[Dict[str, Any]]] = {}
    for row in conn.execute(
            "SELECT r.* FROM message_references r "
            "JOIN messages m ON m.id = r.message_id "
            "WHERE m.tab_id = ? ORDER BY r.message_id, r.ordinal", (tab_id,)):
        refs.setdefault(int(row["message_id"]), []).append(_ref_from_row(row))

    bookmarks: Dict[int, List[Dict[str, Any]]] = {}
    for row in conn.execute(
            "SELECT b.message_id, b.payload FROM message_bookmarks b "
            "JOIN messages m ON m.id = b.message_id "
            "WHERE m.tab_id = ? ORDER BY b.message_id, b.ordinal", (tab_id,)):
        entry = _loads(row["payload"], {})
        if isinstance(entry, dict):
            bookmarks.setdefault(int(row["message_id"]), []).append(entry)

    messages: List[Dict[str, Any]] = []
    for row in rows:
        msg = _decode_payload(row["payload"], int(row["payload_z"] or 0))
        message_id = int(row["id"])
        if message_id in refs:
            msg["references"] = refs[message_id]
        if message_id in bookmarks:
            msg["bookmarks"] = bookmarks[message_id]
        messages.append(msg)
    return messages


# --------------------------------------------------------------------------- #
#  标签页清单
# --------------------------------------------------------------------------- #
#: 清单外标签页的位置标记：内容保留、但不参与启动恢复（见 :func:`put_manifest`）。
DETACHED_POSITION = -1


def put_manifest(conn: sqlite3.Connection, active_id: Optional[str],
                 tabs: Iterable[Dict[str, Any]]) -> bool:
    """写入标签页清单（顺序 / 名称 / 活动项）；空清单拒绝写入。

    只增改清单内标签页的位置与名称，**不删除**不在清单里的对话：删除是
    :func:`delete_tab` 的职责（用户明确关闭标签页）。清单外的标签页位置置为
    :data:`DETACHED_POSITION`，下次启动不再恢复，内容仍留在库里可被找回——
    宁可多留一份数据，也不在"清单暂时不全"的瞬间误删对话。
    """
    entries: List[Dict[str, str]] = []
    seen = set()
    for tab in tabs or []:
        if not isinstance(tab, dict):
            continue
        tab_id = str(tab.get("id") or "")
        if not is_valid_id(tab_id) or tab_id in seen:
            continue
        seen.add(tab_id)
        entries.append({"id": tab_id, "name": str(tab.get("name") or "")})
    if not entries:
        return False

    active = str(active_id or "")
    if active not in seen:
        active = entries[0]["id"]
    now = time.time()
    with conn:
        for position, entry in enumerate(entries):
            conn.execute(
                "INSERT INTO tabs(id, name, position, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
                "position = excluded.position, updated_at = excluded.updated_at",
                (entry["id"], entry["name"], position, now, now))
        marks = ",".join("?" * len(entries))
        conn.execute(f"UPDATE tabs SET position = ? WHERE id NOT IN ({marks})",
                     [DETACHED_POSITION] + [e["id"] for e in entries])
        conn.execute("INSERT INTO meta(key, value) VALUES('active_tab', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (active,))
    return True


def read_manifest(conn: sqlite3.Connection) -> Tuple[Optional[str], List[Dict[str, str]]]:
    """读回 ``(活动标签页 id, [{"id", "name"}, …])``；空库返回 ``(None, [])``。"""
    rows = conn.execute(
        "SELECT id, name FROM tabs WHERE position >= 0 ORDER BY position, rowid").fetchall()
    tabs = [{"id": str(row["id"]), "name": str(row["name"] or "")} for row in rows]
    if not tabs:
        return None, []
    active = get_meta(conn, "active_tab", "")
    known = {tab["id"] for tab in tabs}
    if active not in known:
        active = tabs[0]["id"]
    return active, tabs


def is_valid_id(value: Any) -> bool:
    """标签页 id 白名单：32 位十六进制（与 :func:`src.core.chat_session.new_tab_id` 一致）。

    定义在这里是为了让存储层自己就能拒绝来自外部归档的非法 id，而不是依赖调用方
    先校验——存储层一旦接受 ``../``，后面每一次拼路径都可能被利用。
    """
    text = str(value or "")
    return (len(text) == 32
            and all(ch in "0123456789abcdef" for ch in text))


# --------------------------------------------------------------------------- #
#  标签页内容
# --------------------------------------------------------------------------- #
def ensure_tab(conn: sqlite3.Connection, tab_id: str, name: str = "") -> None:
    """确保标签页行存在（不存在则以"清单末尾"的位置插入）。"""
    now = time.time()
    with conn:
        conn.execute(
            "INSERT INTO tabs(id, name, position, created_at, updated_at) "
            "VALUES(?, ?, (SELECT COALESCE(MAX(position), -1) + 1 FROM tabs), ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at, "
            "name = CASE WHEN ? <> '' THEN ? ELSE tabs.name END",
            (tab_id, name, now, now, name, name))


def put_tab(conn: sqlite3.Connection, tab_id: str, messages: Iterable[Dict[str, Any]],
            *, name: str = "", assets: Optional[Iterable[Dict[str, Any]]] = None,
            placeholders: Optional[Dict[str, Dict[str, Any]]] = None,
            tags: Optional[Iterable[str]] = None) -> int:
    """覆盖写入某标签页的完整会话，返回写入的消息条数。

    * ``assets``：``[{"original", "relpath", "sha256", "name", "size"}, …]``。
      会话库存仓库相对路径，归档库存 BLOB 本体（``blob`` 字段）。
    * ``placeholders``：``{原路径: 占位信息}``——未能内嵌的文件说明。
    * ``tags``：标签名列表（大小写不敏感去重）。

    写入是增量的：按 ``(tab_id, seq)`` upsert，只重写内容变化的行，并删除尾部
    多余行。结果与内存状态一致，但不产生"每轮重写整段历史"的 O(n²) 写放大。
    """
    msgs = [sanitize(m) for m in (messages or []) if isinstance(m, dict)]
    ensure_tab(conn, tab_id, name)
    with conn:
        _write_messages(conn, tab_id, msgs)
        conn.execute("DELETE FROM asset_links WHERE scope = ?", (tab_id,))
        for asset in assets or []:
            _link_asset(conn, tab_id, asset)
        conn.execute("DELETE FROM placeholders WHERE scope = ?", (tab_id,))
        for original, meta in (placeholders or {}).items():
            if isinstance(meta, dict):
                conn.execute("INSERT OR REPLACE INTO placeholders(scope, original, meta) "
                             "VALUES(?, ?, ?)", (tab_id, str(original), _dumps(meta)))
        if tags is not None:
            _set_tab_tags(conn, tab_id, tags)
        conn.execute("UPDATE tabs SET updated_at = ? WHERE id = ?", (time.time(), tab_id))
    logger.debug("Tab %s stored: %d message(s).", tab_id, len(msgs))
    return len(msgs)


def read_tab(conn: sqlite3.Connection, tab_id: str
             ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """读回 ``(messages, assets, placeholders)``；标签页不存在时返回空三元组。"""
    messages = _read_messages(conn, tab_id)
    assets = [dict(row) for row in conn.execute(
        "SELECT l.original AS original, a.sha256 AS sha256, a.name AS name, "
        "a.size AS size, a.relpath AS relpath, a.entry AS entry "
        "FROM asset_links l JOIN assets a ON a.sha256 = l.sha256 "
        "WHERE l.scope = ? ORDER BY l.original", (tab_id,))]
    placeholders = {str(row["original"]): _loads(row["meta"], {})
                    for row in conn.execute(
                        "SELECT original, meta FROM placeholders WHERE scope = ?", (tab_id,))}
    return messages, assets, placeholders


def delete_tab(conn: sqlite3.Connection, tab_id: str) -> None:
    """删除标签页及其消息、资产链接、占位说明、半条、标签关系与附属负载。

    附属负载（证据快照）必须一起删：否则"清除对话"只清掉了消息，证据面板还能
    列出这个已不存在的对话用过的文献。
    """
    with conn:
        conn.execute("DELETE FROM messages WHERE tab_id = ?", (tab_id,))
        conn.execute("DELETE FROM asset_links WHERE scope = ?", (tab_id,))
        conn.execute("DELETE FROM placeholders WHERE scope = ?", (tab_id,))
        conn.execute("DELETE FROM inflight WHERE tab_id = ?", (tab_id,))
        conn.execute("DELETE FROM tab_tags WHERE tab_id = ?", (tab_id,))
        conn.execute("DELETE FROM tab_payloads WHERE tab_id = ?", (tab_id,))
        conn.execute("DELETE FROM tabs WHERE id = ?", (tab_id,))


def list_tabs(conn: sqlite3.Connection) -> List[str]:
    """全部标签页 id（含清单外的游离标签页），供迁移与排障使用。"""
    return [str(row["id"]) for row in conn.execute("SELECT id FROM tabs ORDER BY position")]


# --------------------------------------------------------------------------- #
#  半条回答（流式中途）
# --------------------------------------------------------------------------- #
def put_inflight(conn: sqlite3.Connection, tab_id: str, content: str) -> None:
    """写入流式半成品。

    顺带确保 ``tabs`` 行存在：调用方此前会先单独调 :func:`ensure_tab`，那是
    同一次逻辑写入里的第二个独立事务（两次提交），而这里只是补一行。
    """
    now = time.time()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO tabs(id, name, position, created_at, updated_at) "
            "VALUES(?, '', (SELECT COALESCE(MAX(position), -1) + 1 FROM tabs), ?, ?)",
            (tab_id, now, now))
        conn.execute("INSERT INTO inflight(tab_id, content, saved_at) VALUES(?, ?, ?) "
                     "ON CONFLICT(tab_id) DO UPDATE SET content = excluded.content, "
                     "saved_at = excluded.saved_at",
                     (tab_id, str(content or ""), now))


def read_inflight(conn: sqlite3.Connection, tab_id: str) -> str:
    row = conn.execute("SELECT content FROM inflight WHERE tab_id = ?", (tab_id,)).fetchone()
    return str(row["content"] or "") if row is not None else ""


def delete_inflight(conn: sqlite3.Connection, tab_id: str) -> None:
    with conn:
        conn.execute("DELETE FROM inflight WHERE tab_id = ?", (tab_id,))


# --------------------------------------------------------------------------- #
#  资产与占位
# --------------------------------------------------------------------------- #
def _link_asset(conn: sqlite3.Connection, scope: str, asset: Dict[str, Any]) -> None:
    """登记"原路径 -> 资产"并补一行 assets（已存在则只补空缺字段）。"""
    sha = str(asset.get("sha256") or "")
    original = str(asset.get("original") or "")
    if not sha or not original:
        return
    blob = asset.get("blob")
    conn.execute(
        "INSERT INTO assets(sha256, name, size, entry, relpath, blob) VALUES(?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(sha256) DO UPDATE SET "
        "name = CASE WHEN assets.name = '' THEN excluded.name ELSE assets.name END, "
        "size = CASE WHEN assets.size = 0 THEN excluded.size ELSE assets.size END, "
        "entry = CASE WHEN assets.entry = '' THEN excluded.entry ELSE assets.entry END, "
        "relpath = CASE WHEN excluded.relpath <> '' THEN excluded.relpath ELSE assets.relpath END, "
        "blob = CASE WHEN excluded.blob IS NOT NULL THEN excluded.blob ELSE assets.blob END",
        (sha, str(asset.get("name") or ""), int(asset.get("size") or 0),
         str(asset.get("entry") or ""), str(asset.get("relpath") or ""),
         sqlite3.Binary(blob) if blob is not None else None))
    conn.execute("INSERT OR REPLACE INTO asset_links(scope, original, sha256) VALUES(?, ?, ?)",
                 (scope, original, sha))


def put_assets(conn: sqlite3.Connection, scope: str,
               assets: Iterable[Dict[str, Any]]) -> int:
    """批量登记资产（``blob`` 为 None 表示本体在仓库里，只记路径）。"""
    written = 0
    with conn:
        for asset in assets or []:
            if isinstance(asset, dict):
                _link_asset(conn, scope, asset)
                written += 1
    return written


def read_assets(conn: sqlite3.Connection, scope: str) -> List[Dict[str, Any]]:
    """读回某 scope 的资产清单（含 BLOB；导出解包时按需取用）。"""
    return [dict(row) for row in conn.execute(
        "SELECT l.original AS original, a.sha256 AS sha256, a.name AS name, "
        "a.size AS size, a.entry AS entry, a.relpath AS relpath, a.blob AS blob "
        "FROM asset_links l JOIN assets a ON a.sha256 = l.sha256 "
        "WHERE l.scope = ? ORDER BY l.original", (scope,))]


def put_placeholders(conn: sqlite3.Connection, scope: str,
                     placeholders: Dict[str, Dict[str, Any]]) -> None:
    with conn:
        conn.execute("DELETE FROM placeholders WHERE scope = ?", (scope,))
        for original, meta in (placeholders or {}).items():
            if isinstance(meta, dict):
                conn.execute("INSERT OR REPLACE INTO placeholders(scope, original, meta) "
                             "VALUES(?, ?, ?)", (scope, str(original), _dumps(meta)))


def read_placeholders(conn: sqlite3.Connection, scope: str) -> Dict[str, Dict[str, Any]]:
    return {str(row["original"]): _loads(row["meta"], {})
            for row in conn.execute(
                "SELECT original, meta FROM placeholders WHERE scope = ?", (scope,))}


# --------------------------------------------------------------------------- #
#  标签
# --------------------------------------------------------------------------- #
def _set_tab_tags(conn: sqlite3.Connection, tab_id: str, tags: Iterable[str]) -> None:
    """覆盖写入某对话的标签（调用方已在一个事务里）。"""
    conn.execute("DELETE FROM tab_tags WHERE tab_id = ?", (tab_id,))
    for name in normalize_tag_names(tags):
        tag_id = _ensure_tag(conn, name)
        conn.execute("INSERT OR IGNORE INTO tab_tags(tab_id, tag_id) VALUES(?, ?)",
                     (tab_id, tag_id))


def normalize_tag_names(tags: Iterable[str]) -> List[str]:
    """标签名归一：折叠空白、限长 64、大小写不敏感去重。"""
    names: List[str] = []
    seen = set()
    for raw in tags or []:
        name = " ".join(str(raw or "").split())
        if not name or len(name) > 64:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _ensure_tag(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM tags WHERE name = ? COLLATE NOCASE",
                       (name,)).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute("INSERT INTO tags(name, color, created_at) VALUES(?, '', ?)",
                       (name, time.time()))
    return int(cur.lastrowid)


def set_tab_tags(conn: sqlite3.Connection, tab_id: str, tags: Iterable[str]) -> List[str]:
    """设置某对话的标签，返回归一后的标签名列表（一个对话可挂多个标签）。"""
    with conn:
        _set_tab_tags(conn, tab_id, tags)
    return tab_tags(conn, tab_id)


def tab_tags(conn: sqlite3.Connection, tab_id: str) -> List[str]:
    """某对话的标签名（按标签首次创建顺序）。"""
    return [str(row["name"]) for row in conn.execute(
        "SELECT t.name AS name FROM tab_tags tt JOIN tags t ON t.id = tt.tag_id "
        "WHERE tt.tab_id = ? ORDER BY t.id", (tab_id,))]


def all_tags(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """全部标签及其对话数量（供"按标签浏览多个对话"使用）。"""
    return [{"name": str(row["name"]), "color": str(row["color"] or ""),
             "tabs": int(row["n"])}
            for row in conn.execute(
                "SELECT t.name AS name, t.color AS color, COUNT(tt.tab_id) AS n "
                "FROM tags t LEFT JOIN tab_tags tt ON tt.tag_id = t.id "
                "GROUP BY t.id ORDER BY t.id")]


def tabs_with_tag(conn: sqlite3.Connection, name: str) -> List[str]:
    """某标签下的全部对话 id（"一个标签下的多个内容"的查询入口）。"""
    return [str(row["tab_id"]) for row in conn.execute(
        "SELECT tt.tab_id AS tab_id FROM tab_tags tt JOIN tags t ON t.id = tt.tag_id "
        "WHERE t.name = ? COLLATE NOCASE ORDER BY tt.tab_id", (str(name),))]


__all__ = [
    "SQLITE_MAGIC", "SCHEMA_VERSION", "FORMAT_CHAT_TABS", "FORMAT_CHAT_HISTORY",
    "KIND_SESSION", "KIND_ARCHIVE", "ARCHIVE_SCOPE", "DETACHED_POSITION",
    "sanitize", "is_sqlite_file", "connect", "create_database", "init_schema",
    "get_meta", "set_meta", "is_valid_id",
    "put_manifest", "read_manifest", "ensure_tab", "put_tab", "read_tab",
    "delete_tab", "list_tabs",
    "put_inflight", "read_inflight", "delete_inflight",
    "put_assets", "read_assets", "put_placeholders", "read_placeholders",
    "normalize_tag_names", "set_tab_tags", "tab_tags", "all_tags", "tabs_with_tag",
]
