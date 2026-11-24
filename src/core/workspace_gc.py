"""``scholar_workspace`` 过期内容清理的**唯一事实源**。

工作区里真正会无限增长的是四类"可再生或可丢弃"的数据：

* ``provenance/`` —— 每次工具调用落一份证据链 JSONL，只用于事后核查；
* ``assets/`` —— 内容寻址资产仓库。``chat_store.delete_tab`` 只清 ``asset_links``
  行，磁盘副本不会跟着消失，于是"删掉对话"不会释放任何空间；
* ``chat_session/*.bak_v*`` 与 ``chat_session/legacy/`` —— 迁移前备份与旧格式快照；
* ``browser_profile/`` 下的 Chromium 磁盘缓存。

相反，``chat_session/session.sqlite3``、``pdf_index.sqlite3`` 与
``litellm_model_cost.json`` 属于**不可再生**的状态，本模块一律不碰。

两条安全边界：

* **资产按引用清理，不按时间清理**。只有确认没有任何标签页再引用某个摘要，
  且该文件已过宽限期，才允许删除。读不到引用集合（库不存在、被独占锁、
  schema 变更）时整步跳过——宁可留垃圾，不可删数据。
* **删除必须可失败**。Windows 上被占用的文件会抛 ``PermissionError``，
  一律降级为 debug 日志并继续；任何一步异常都不外溢，不影响启动。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import stat as stat_module
import threading
import time
from typing import Any, Dict, Iterator, List, Optional

from src.core import BASE_DIR, safe_io

logger = logging.getLogger("Core.WorkspaceGC")

#: 工作区目录名（位于 :data:`BASE_DIR` 下）。
WORKSPACE_DIRNAME = "scholar_workspace"
#: 清理状态文件（记录上次运行时间，供节流使用）。
STATE_FILENAME = "maintenance.json"

#: 清理策略默认值。用户可在 ``config/settings.json`` 用 ``workspace_gc_<key>``
#: 覆盖任意一项；默认值只在这里定义一份。
DEFAULT_POLICY: Dict[str, Any] = {
    # 总开关。关闭后 :func:`run_in_background` 直接返回，不产生任何 IO。
    "enabled": True,
    # 两次全量清理之间的最小间隔（小时）。频繁启动不会反复扫盘。
    "min_interval_hours": 6,
    # 证据链保留天数与数量上限（两个条件任一命中即删，先保新）。
    "provenance_days": 30,
    "provenance_max_files": 300,
    # 未引用资产的宽限期（天）。留出"刚写入还没登记链接"的时间窗。
    "asset_grace_days": 7,
    # 迁移备份保留天数（天）。无论多旧都至少保留最新一份。
    "backup_days": 30,
    # 旧格式快照目录保留天数（天）。
    "legacy_days": 30,
    # Chromium 磁盘缓存清理间隔（小时）。
    "browser_cache_interval_hours": 24,
    # 半截临时文件（``.asset_*.part``）的保留时间（小时）。
    "stale_part_hours": 6,
}

#: Chromium 纯缓存目录名（大小写不敏感匹配）。**白名单**：Cookies、History、
#: Local Storage 等可能承载登录态，一律不在此列。
#:
#: 用名字集合而不是相对路径：同一个 profile 在不同构建/平台上会写成
#: ``cache/Cache`` 或 ``Cache``，写死路径必然漏掉其中一种。
_CHROMIUM_CACHE_NAMES = frozenset({
    "cache",
    "code cache",
    "gpucache",
    "dawnwebgpucache",
    "dawngraphitecache",
    "grshadercache",
    "shadercache",
    "cachestorage",
    "scriptcache",
})

#: 在 profile 内向下搜索缓存目录的最大层数（缓存目录不可能更深）。
_MAX_PROFILE_DEPTH = 4

#: 证据链文件名形态。
_PROVENANCE_RE = re.compile(r"^provenance_.*\.jsonl$")
#: 迁移备份文件名形态（``session.sqlite3.bak_v2``）。
_BACKUP_RE = re.compile(r"\.bak_v\d+$")
#: 半截临时文件（原子替换未完成时残留）。
_PART_RE = re.compile(r"^\.asset_.*\.part$")
#: 资产仓库文件名：``<sha256><ext>``。
_DIGEST_NAME_RE = re.compile(r"^([0-9a-f]{64})(\.[A-Za-z0-9][A-Za-z0-9._-]{0,9})?$")

#: 同一进程内只允许一个清理线程。
_running = threading.Lock()


# --------------------------------------------------------------------------- #
#  路径
# --------------------------------------------------------------------------- #
def workspace_dir() -> str:
    """工作区根目录的绝对路径。"""
    return os.path.join(BASE_DIR, WORKSPACE_DIRNAME)


def _state_path() -> str:
    return os.path.join(workspace_dir(), STATE_FILENAME)


def _session_module():
    """惰性取会话模块：它连带 ``chat_store`` / ``chat_assets``，不参与启动导入。"""
    from src.core import chat_session
    return chat_session


def _asset_store_dir() -> str:
    from src.core import chat_assets
    return chat_assets.asset_store_dir()


# --------------------------------------------------------------------------- #
#  策略
# --------------------------------------------------------------------------- #
def _coerce(default: Any, raw: Any, key: str) -> Any:
    """把用户配置值收敛到默认值的类型；无法收敛时退回默认值。"""
    try:
        if isinstance(default, bool):
            if isinstance(raw, str):
                return raw.strip().lower() in ("1", "true", "yes", "on")
            return bool(raw)
        if isinstance(default, int):
            return max(0, int(float(raw)))
        if isinstance(default, float):
            return max(0.0, float(raw))
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid workspace_gc_%s = %r", key, raw)
    return default


def policy() -> Dict[str, Any]:
    """默认策略叠加用户覆盖。配置读不到时退化为默认值（不抛）。"""
    merged = dict(DEFAULT_POLICY)
    try:
        from src.core.config_manager import ConfigManager
        settings = ConfigManager().user_settings or {}
    except Exception as exc:  # 配置未就绪 / 损坏时不影响清理
        logger.debug("Workspace GC uses default policy: %s", exc)
        return merged
    for key, default in DEFAULT_POLICY.items():
        raw = settings.get(f"workspace_gc_{key}")
        if raw is None:
            continue
        merged[key] = _coerce(default, raw, key)
    return merged


# --------------------------------------------------------------------------- #
#  文件原语
# --------------------------------------------------------------------------- #
def _remove_file(path: str) -> bool:
    """删除单个文件。Windows 下只读属性与占用分别处理，失败返回 ``False``。"""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except PermissionError:
        # Windows：只读属性会导致 PermissionError，去掉属性后重试一次。
        try:
            os.chmod(path, stat_module.S_IWRITE | stat_module.S_IREAD)
            os.remove(path)
            return True
        except OSError as exc:
            logger.debug("Skip locked file %s: %s", path, exc)
            return False
    except OSError as exc:
        logger.debug("Skip file %s: %s", path, exc)
        return False


def _file_age(path: str, now: float) -> Optional[float]:
    try:
        return now - os.path.getmtime(path)
    except OSError:
        return None


def _iter_files(root: str, recursive: bool = True) -> Iterator[str]:
    """遍历文件，不跟随符号链接（避免删到工作区外的目标）。"""
    if not os.path.isdir(root):
        return
    try:
        if recursive:
            for current, _dirs, names in os.walk(root):
                for name in names:
                    yield os.path.join(current, name)
        else:
            with os.scandir(root) as entries:
                for entry in entries:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            yield entry.path
                    except OSError:
                        continue
    except OSError as exc:
        logger.debug("Cannot walk %s: %s", root, exc)


def _prune_empty_dirs(root: str) -> None:
    """自底向上删除空目录（保留 ``root`` 本身）。"""
    if not os.path.isdir(root):
        return
    for current, _dirs, _files in os.walk(root, topdown=False):
        if os.path.abspath(current) == os.path.abspath(root):
            continue
        try:
            if not os.listdir(current):
                os.rmdir(current)
        except OSError:
            continue


def _empty_result(reason: str = "") -> Dict[str, Any]:
    result: Dict[str, Any] = {"removed": 0, "bytes": 0, "skipped": 0}
    if reason:
        result["reason"] = reason
    return result


def _delete(path: str, dry_run: bool) -> int:
    """删除并返回释放的字节数；``dry_run`` 只统计不落盘。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    if dry_run:
        return size
    return size if _remove_file(path) else 0


# --------------------------------------------------------------------------- #
#  各类清理
# --------------------------------------------------------------------------- #
def _gc_ttl_file_set(
    root: str,
    pattern: Optional["re.Pattern[str]"],
    max_age_seconds: float,
    now: float,
    *,
    recursive: bool = True,
    keep_newest: int = 0,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """按"保留最新 N 份 + 超过 TTL 即删"清理一组同类文件。

    ``pattern`` 为 ``None`` 表示目录下所有文件都归本步管辖。
    """
    candidates: List[tuple] = []
    for path in _iter_files(root, recursive):
        name = os.path.basename(path)
        if pattern is not None and not pattern.search(name):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        candidates.append((mtime, path))
    if not candidates:
        return _empty_result()

    # 先保新：按 mtime 从新到旧排序，前 keep_newest 个无条件保留。
    candidates.sort(key=lambda item: item[0], reverse=True)
    stats = _empty_result()
    for index, (mtime, path) in enumerate(candidates):
        if index < keep_newest:
            continue
        if now - mtime <= max_age_seconds:
            continue
        released = _delete(path, dry_run)
        stats["removed"] += 1
        stats["bytes"] += released
    return stats


def _referenced_digests() -> Optional[set]:
    """会话库里仍被引用的资产摘要集合；读不到时返回 ``None``（调用方跳过清理）。

    ``delete_tab`` 会同步删掉该标签页的 ``asset_links``，所以"表里出现过"就等价于
    "某个现存标签页还在用"。只读打开：清理过程绝不改写会话库。
    """
    chat_session = _session_module()
    db = chat_session.db_path()
    if not os.path.isfile(db):
        return None
    conn: Optional[sqlite3.Connection] = None
    try:
        from src.core import chat_store
        conn = chat_store.connect(db, readonly=True)
        rows = conn.execute("SELECT DISTINCT sha256 FROM asset_links").fetchall()
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Asset GC skipped: cannot read references (%s)", exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return {str(row[0]) for row in rows if row and row[0]}


def _gc_assets(grace_seconds: float, now: float, dry_run: bool) -> Dict[str, Any]:
    """删除"已无引用 + 超过宽限期"的资产文件。"""
    root = _asset_store_dir()
    if not os.path.isdir(root):
        return _empty_result("no_store")
    referenced = _referenced_digests()
    if referenced is None:
        return _empty_result("no_reference_set")

    stats = _empty_result()
    for path in _iter_files(root, True):
        match = _DIGEST_NAME_RE.match(os.path.basename(path))
        if not match:
            # 非内容寻址命名：不是本模块的管辖范围，交给半截文件清理。
            continue
        digest = match.group(1)
        if digest in referenced:
            continue
        age = _file_age(path, now)
        if age is None or age <= grace_seconds:
            continue
        released = _delete(path, dry_run)
        stats["removed"] += 1
        stats["bytes"] += released
    if not dry_run:
        _prune_empty_dirs(root)
    logger.debug("Asset GC: %d referenced digests, %d files removed",
                 len(referenced), stats["removed"])
    return stats


def _chromium_cache_dirs(root: str) -> List[str]:
    """列出 profile 下的纯缓存目录（命中即不再下钻，避免误判子目录）。"""
    found: List[str] = []
    root_abs = os.path.abspath(root)
    for current, dirs, _files in os.walk(root_abs):
        depth = 0 if os.path.abspath(current) == root_abs else (
            os.path.relpath(current, root_abs).count(os.sep) + 1)
        if depth >= _MAX_PROFILE_DEPTH:
            dirs[:] = []
            continue
        keep: List[str] = []
        for name in dirs:
            if name.lower() in _CHROMIUM_CACHE_NAMES:
                found.append(os.path.join(current, name))
            else:
                keep.append(name)
        dirs[:] = keep
    return found


def _gc_browser_cache(dry_run: bool) -> Dict[str, Any]:
    """清空 Chromium 白名单缓存目录。

    只删白名单里的纯缓存目录；目录内文件可能正被 WebEngine 打开，
    Linux 上删除是安全的（进程持有 fd），Windows 上失败的条目被跳过。
    """
    root = os.path.join(workspace_dir(), "browser_profile")
    if not os.path.isdir(root):
        return _empty_result("no_profile")

    stats = _empty_result()
    for target in _chromium_cache_dirs(root):
        for path in _iter_files(target, True):
            stats["removed"] += 1
            stats["bytes"] += _delete(path, dry_run)
        if not dry_run:
            _prune_empty_dirs(target)
    return stats


# --------------------------------------------------------------------------- #
#  状态与节流
# --------------------------------------------------------------------------- #
def _load_state() -> Dict[str, Any]:
    try:
        import json
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        safe_io.atomic_write_json(_state_path(), state)
    except (OSError, TypeError, ValueError) as exc:
        logger.debug("Cannot persist workspace GC state: %s", exc)


# --------------------------------------------------------------------------- #
#  对外接口
# --------------------------------------------------------------------------- #
def collect(*, now: Optional[float] = None, dry_run: bool = False) -> Dict[str, Any]:
    """执行一次全量清理，返回可直接写日志 / 展示的统计。

    :param now: 时间基准（测试与回放用），默认取当前时间。
    :param dry_run: 只统计可清理内容，不删除任何文件。
    """
    started = time.perf_counter()
    current = time.time() if now is None else float(now)
    cfg = policy()
    report: Dict[str, Any] = {
        "dry_run": bool(dry_run),
        "enabled": bool(cfg.get("enabled")),
        "kinds": {},
    }
    if not cfg.get("enabled"):
        return report

    chat_session = _session_module()
    session_root = chat_session.session_dir()

    steps = {
        "provenance": lambda: _gc_ttl_file_set(
            os.path.join(workspace_dir(), "provenance"), _PROVENANCE_RE,
            cfg["provenance_days"] * 86400, current,
            keep_newest=int(cfg["provenance_max_files"]), dry_run=dry_run),
        "backups": lambda: _gc_ttl_file_set(
            session_root, _BACKUP_RE, cfg["backup_days"] * 86400, current,
            recursive=False, keep_newest=1, dry_run=dry_run),
        "legacy": lambda: _gc_ttl_file_set(
            os.path.join(session_root, "legacy"), None,
            cfg["legacy_days"] * 86400, current, dry_run=dry_run),
        "stale_parts": lambda: _gc_ttl_file_set(
            _asset_store_dir(), _PART_RE, cfg["stale_part_hours"] * 3600, current,
            dry_run=dry_run),
        "assets": lambda: _gc_assets(
            cfg["asset_grace_days"] * 86400, current, dry_run),
    }

    state = _load_state()
    browser_interval = float(cfg.get("browser_cache_interval_hours") or 0) * 3600
    last_browser = float(state.get("browser_cache") or 0)
    if browser_interval <= 0 or current - last_browser >= browser_interval:
        steps["browser_cache"] = lambda: _gc_browser_cache(dry_run)
    else:
        report["kinds"]["browser_cache"] = _empty_result("not_due")

    for name, step in steps.items():
        try:
            report["kinds"][name] = step()
        except Exception as exc:  # 单步失败不影响其他步骤
            logger.warning("Workspace GC step '%s' failed: %s", name, exc)
            report["kinds"][name] = _empty_result(f"error: {exc}")

    report["removed_files"] = sum(int(v.get("removed", 0)) for v in report["kinds"].values())
    report["removed_bytes"] = sum(int(v.get("bytes", 0)) for v in report["kinds"].values())
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 1)

    # 节流按"跑过"计，不按"删过"计：一次干净的全量扫描同样要落盘，
    # 否则每次启动都会重扫一遍。
    if not dry_run:
        state["last_run"] = current
        if "browser_cache" in steps:
            state["browser_cache"] = current
        _save_state(state)

    if report["removed_files"]:
        logger.info(
            "Workspace GC removed %d files (%.1f MiB) in %.1f ms",
            report["removed_files"], report["removed_bytes"] / 1048576.0,
            report["elapsed_ms"])
    else:
        logger.debug("Workspace GC: nothing to remove (%.1f ms)", report["elapsed_ms"])
    return report


def _run(log_obj: Optional[logging.Logger]) -> None:
    sink = log_obj or logger
    if not _running.acquire(blocking=False):
        sink.debug("Workspace GC already running; skipped.")
        return
    try:
        report = collect()
        if report["removed_files"]:
            sink.info(
                "Workspace cleanup: %d files, %.1f MiB, %.0f ms",
                report["removed_files"], report["removed_bytes"] / 1048576.0,
                report["elapsed_ms"])
    except Exception as exc:  # 清理绝不能影响启动
        sink.warning("Workspace cleanup failed: %s", exc)
    finally:
        _running.release()


def run_in_background(log_obj: Optional[logging.Logger] = None) -> Optional[threading.Thread]:
    """在后台线程执行一次清理；距离上次运行不足 ``min_interval_hours`` 时直接返回。

    :return: 已启动的线程；被节流或禁用时返回 ``None``。
    """
    cfg = policy()
    if not cfg.get("enabled"):
        logger.debug("Workspace GC disabled by configuration.")
        return None

    interval = float(cfg.get("min_interval_hours") or 0) * 3600
    last_run = float(_load_state().get("last_run") or 0)
    now = time.time()
    if interval > 0 and last_run and now - last_run < interval:
        logger.debug("Workspace GC skipped: last run %.1f h ago",
                     (now - last_run) / 3600.0)
        return None

    thread = threading.Thread(target=_run, args=(log_obj,),
                              name="workspace-gc", daemon=True)
    thread.start()
    return thread
