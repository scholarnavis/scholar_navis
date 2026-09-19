"""Persistent output directories for artefacts the app produces.

Why this module exists
----------------------
Several features used to write long-lived user-facing artefacts (R charts,
AI-generated images) into the **system temporary directory**. On the platforms
this app runs on that directory is not durable:

* on NixOS the app re-launches itself through ``steam-run`` (see
  :mod:`src.core.platform_env`), whose ``/tmp`` is private to the sandbox and
  vanishes as soon as the process exits;
* elsewhere ``/tmp`` is emptied by ``systemd-tmpfiles`` or on reboot.

The visible symptom was "double-clicking a chart / image no longer opens it":
the chat history still holds the file path, but the file itself is gone, so the
preview cannot load and the external viewer has nothing to open.

Artefacts therefore go to ``<BASE_DIR>/output/...`` — next to ``config/`` and
``tools/mcp/``, which the app already treats as writable project data. If that
location is not writable (e.g. a read-only system-wide install) we fall back to
the temporary directory and log a loud warning, because the artefact lifetime
degrades in that case.

All helpers are cached: the directory probe (create + write test) runs once per
path per process.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from typing import Dict

logger = logging.getLogger("Core.OutputPaths")

#: 顶层输出目录名（位于 BASE_DIR 下，已在 .gitignore 中忽略）。
OUTPUT_ROOT_NAME = "output"
#: 临时目录兜底时使用的子目录名。
TEMP_FALLBACK_NAME = "scholar_navis"

#: 各功能模块的子目录。
PLOT_SUBDIR = "r_plots"
GENERATED_IMAGE_SUBDIR = "generated_images"

_resolved: Dict[str, str] = {}
_lock = threading.Lock()


def _is_writable(path: str) -> bool:
    """真正写一次再删掉：仅靠 os.access 在只读挂载上会误判。"""
    probe = os.path.join(path, ".write_probe")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def output_dir(*parts: str, fallback_name: str = "") -> str:
    """Return a writable, persistent directory for app artefacts.

    ``parts`` are appended to ``<BASE_DIR>/output``. When that tree is not
    writable the temporary directory is used instead (with a warning), so the
    feature still works on read-only installs.

    :param fallback_name: subdirectory name used in the temporary fallback;
                          defaults to the joined ``parts``.
    """
    key = "/".join(parts)
    with _lock:
        cached = _resolved.get(key)
        if cached:
            return cached

        target = ""
        try:
            from src.core import BASE_DIR
            target = os.path.join(BASE_DIR, OUTPUT_ROOT_NAME, *parts)
            os.makedirs(target, exist_ok=True)
            if not _is_writable(target):
                raise OSError("directory is not writable")
            logger.info(f"[output] {key or 'artefacts'} -> {target}")
            _resolved[key] = target
            return target
        except OSError as e:
            name = fallback_name or "_".join(p for p in parts if p) or "artefacts"
            fallback = os.path.join(tempfile.gettempdir(), TEMP_FALLBACK_NAME, name)
            os.makedirs(fallback, exist_ok=True)
            logger.warning(
                f"[output] {target or key} is not writable ({e}); falling back to "
                f"{fallback}. Files there can be removed by the system or by the "
                f"sandbox, so saved charts/images may become unopenable later.")
            _resolved[key] = fallback
            return fallback


def plot_output_dir() -> str:
    """Directory holding R chart artefacts (PNG/SVG/PDF/CSV/scripts/registry)."""
    return output_dir(PLOT_SUBDIR)


def generated_image_dir() -> str:
    """Directory holding AI-generated images that the user may open later."""
    return output_dir(GENERATED_IMAGE_SUBDIR)
