"""
Provenance Tracking
===================

Thread-safe, append-only evidence-chain recorder for the Scholar Navis agent.

Each tool execution produces one :class:`ProvenanceRecord` that links a
*derived conclusion* back to its *source record*, the *database/API* it came
from, and the *exact call parameters*, *timestamp* and *software version* that
produced it. This makes the system's "anti-hallucination" claim auditable and
reproducible — a prerequisite for high-impact publication.

Design principles:
    * High cohesion: one record type + one collector; no UI dependencies.
    * Low coupling: the runtime only calls :meth:`ProvenanceCollector.record`.
    * Performance: in-memory append + optional JSONL flush; no per-call disk
      I/O unless the caller explicitly flushes.

Thread-safety:
    * ``ProvenanceCollector`` guards its internal list with an RLock, so the
      runtime's parallel tool calls can record concurrently without races.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Core.Provenance")


@dataclass
class ProvenanceRecord:
    """A single link in the evidence chain: one tool call -> one result."""

    tool: str
    """Name of the executed tool / skill."""

    pool: str
    """Which pool the tool belongs to: 'academic', 'external', 'mcp', 'builtin'."""

    params: Dict[str, Any]
    """The exact arguments passed to the tool (sanitized)."""

    status: str
    """'success' | 'error' | 'cancelled'."""

    source: str
    """The upstream database/API this result anchors to (e.g. 'OpenAlex',
    'UniProt', 'PubMed', 'g:Profiler'). Empty when unknown."""

    result_summary: str
    """A short, deterministic digest of the result (not the full payload)."""

    timestamp_iso: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    """Wall-clock time of execution (local, ISO-8601, no tz to stay portable)."""

    record_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    """Unique id of this record; referenced by downstream conclusions."""

    app_version: str = ""
    """Application version at execution time (for reproducibility)."""

    database_version: str = ""
    """Optional upstream database release/version (e.g. 'UniProt 2025_03')."""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ProvenanceCollector:
    """Append-only, thread-safe collector of :class:`ProvenanceRecord`."""

    def __init__(self, app_version: str = ""):
        self._lock = threading.RLock()
        self._records: List[ProvenanceRecord] = []
        self.app_version = app_version

    # ------------------------------------------------------------------ #
    #  Recording
    # ------------------------------------------------------------------ #
    def record(
        self,
        tool: str,
        pool: str,
        params: Dict[str, Any],
        status: str,
        source: str = "",
        result_summary: str = "",
        database_version: str = "",
    ) -> ProvenanceRecord:
        """Append a record and return it (callers may use ``record_id``).

        ``params`` is deep-copied and sanitized so mutable caller dicts or
        sensitive values cannot leak or be mutated after the fact.
        """
        rec = ProvenanceRecord(
            tool=tool,
            pool=pool,
            params=self._sanitize(params),
            status=status,
            source=source,
            result_summary=self._summarize(result_summary),
            app_version=self.app_version,
            database_version=database_version,
        )
        with self._lock:
            self._records.append(rec)
        return rec

    # ------------------------------------------------------------------ #
    #  Querying
    # ------------------------------------------------------------------ #
    def snapshot(self) -> List[Dict[str, Any]]:
        """Return an ordered list of all records as plain dicts (JSON-safe)."""
        with self._lock:
            return [r.to_dict() for r in self._records]

    def records_for(self, tool: str) -> List[ProvenanceRecord]:
        """Return all records produced by a specific tool name."""
        with self._lock:
            return [r for r in self._records if r.tool == tool]

    def clear(self):
        """Drop all records (e.g. at the start of a new conversation)."""
        with self._lock:
            self._records.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    # ------------------------------------------------------------------ #
    #  Persistence
    # ------------------------------------------------------------------ #
    def to_jsonl(self, path: str) -> int:
        """Append current records to a JSONL file; returns count written.

        JSONL is append-friendly and line-greppable, making it trivial to audit
        or re-import into downstream reproducibility tooling.
        """
        data = self.snapshot()
        with self._lock:
            try:
                with open(path, "a", encoding="utf-8") as f:
                    for d in data:
                        f.write(json.dumps(d, ensure_ascii=False) + "\n")
            except OSError as e:
                logger.error(f"Failed to flush provenance to {path}: {e}")
                return 0
        return len(data)

    def export_to_dir(self, directory: str, conversation_id: str = "") -> str:
        """Write the current evidence chain to a JSONL file under ``directory``.

        Returns the absolute path of the written file, or an empty string on
        failure. The filename embeds a timestamp + optional conversation id so
        successive conversations never overwrite each other.

        This is the "consumption" entry point: the runtime calls it at the end
        of a conversation to persist the auditable evidence chain.
        """
        records = self.snapshot()
        if not records:
            logger.info("No provenance records to export; skipping.")
            return ""

        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as e:
            logger.error(f"Failed to create provenance dir {directory}: {e}")
            return ""

        safe_cid = "".join(c for c in conversation_id if c.isalnum() or c in "-_")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        fname = f"provenance_{stamp}"
        if safe_cid:
            fname += f"_{safe_cid}"
        path = os.path.join(directory, fname + ".jsonl")

        # Use truncate ('w') semantics: export_to_dir writes the *snapshot* as
        # a complete file, not an append, so each conversation is self-contained.
        with self._lock:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    for d in records:
                        f.write(json.dumps(d, ensure_ascii=False) + "\n")
            except OSError as e:
                logger.error(f"Failed to write provenance to {path}: {e}")
                return ""

        logger.info(f"Exported {len(records)} provenance records to {path}")
        return path

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _sanitize(params: Any, max_str_len: int = 512, _depth: int = 0) -> Any:
        """Recursively copy ``params``, truncating long strings and dropping
        binary/large payloads so records stay compact and safe to serialize."""
        if _depth > 6:
            return "<depth-limit>"
        if isinstance(params, dict):
            out: Dict[str, Any] = {}
            for k, v in params.items():
                if isinstance(k, str) and k.lower() in ("token", "api_key", "password", "secret", "authorization"):
                    out[k] = "<redacted>"
                else:
                    out[str(k)] = ProvenanceCollector._sanitize(v, max_str_len, _depth + 1)
            return out
        if isinstance(params, (list, tuple)):
            return [ProvenanceCollector._sanitize(v, max_str_len, _depth + 1) for v in params]
        if isinstance(params, (bytes, bytearray)):
            return f"<{len(params)} bytes>"
        if isinstance(params, str) and len(params) > max_str_len:
            return params[:max_str_len] + "..."
        return params

    @staticmethod
    def _summarize(result: str, max_len: int = 300) -> str:
        """Produce a compact, deterministic summary of a result payload."""
        if not result:
            return ""
        text = result if isinstance(result, str) else str(result)
        text = " ".join(text.split())
        if len(text) <= max_len:
            return text
        return text[:max_len] + "..."


# ---------------------------------------------------------------------- #
#  Process-wide singleton (one evidence chain per application session)
# ---------------------------------------------------------------------- #
_collector: Optional[ProvenanceCollector] = None
_collector_lock = threading.Lock()


def get_collector() -> ProvenanceCollector:
    """Return the shared, process-wide ProvenanceCollector."""
    global _collector
    with _collector_lock:
        if _collector is None:
            _collector = ProvenanceCollector()
        return _collector
