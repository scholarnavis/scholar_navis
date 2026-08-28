"""
R Runtime Detector
==================

Locates an installed R / Rscript interpreter for the visualization engine.

The Scholar Navis visualization layer renders charts exclusively via R (no
LLM-generated SVG, no Python matplotlib), which keeps the plotting pipeline
deterministic, reproducible and free of extra licensing surface.

Detection strategy (in order):
    1. A user-specified path (``set_custom_path``) — highest priority.
    2. ``Rscript`` / ``R`` found on the system ``PATH``.
    3. Common install locations (Windows ``R_HOME``, macOS, Linux).

Detection is lazy and cached; call :meth:`detect` explicitly to force a
re-scan. The class is a thread-safe singleton (mirrors ``DeviceManager``).

Public contract:
    * ``available``  — True when a working interpreter was found.
    * ``executable`` — absolute path to the interpreter (Rscript preferred).
    * ``version``    — e.g. "4.3.1".
    * ``home``       — R installation root (``R.home()``), when resolvable.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from typing import Optional

logger = logging.getLogger("Core.REngine")

# Official download landing pages, shown to users when R is absent.
R_DOWNLOAD_URL = "https://cran.r-project.org/"


class REngine:
    """Thread-safe singleton that locates and validates the R interpreter."""

    _instance: Optional["REngine"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "REngine":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._custom_path: Optional[str] = None
                cls._instance._cached: Optional[dict] = None
                cls._instance._cache_lock = threading.RLock()
        return cls._instance

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #
    def set_custom_path(self, path: Optional[str]):
        """Set (or clear) a user-specified Rscript/R executable path.

        Setting a path invalidates any cached detection result.
        """
        with self._cache_lock:
            self._custom_path = path.strip() if path else None
            self._cached = None
        logger.info(f"R engine custom path set: {self._custom_path!r}")

    def detect(self) -> dict:
        """Detect (or return cached) the R interpreter. See module docstring
        for the shape of the returned dict."""
        with self._cache_lock:
            if self._cached is not None:
                return self._cached
            result = self._detect_impl()
            self._cached = result
            return result

    # Convenience accessors --------------------------------------------- #
    @property
    def available(self) -> bool:
        return bool(self.detect().get("available"))

    @property
    def executable(self) -> str:
        return self.detect().get("executable", "")

    @property
    def version(self) -> str:
        return self.detect().get("version", "")

    # ------------------------------------------------------------------ #
    #  Detection internals
    # ------------------------------------------------------------------ #
    def _detect_impl(self) -> dict:
        # 1) User-specified path (highest priority).
        if self._custom_path:
            info = self._validate(self._custom_path)
            if info:
                logger.info(f"R engine resolved via custom path: {self._custom_path}")
                return info
            logger.warning(f"Custom R path invalid: {self._custom_path!r}")

        # 2) Executable on PATH.
        for name in ("Rscript", "R"):
            found = shutil.which(name)
            if found:
                info = self._validate(found)
                if info:
                    logger.info(f"R engine found on PATH: {found}")
                    return info

        # 3) Common install locations.
        for candidate in self._common_locations():
            if os.path.isfile(candidate):
                info = self._validate(candidate)
                if info:
                    logger.info(f"R engine found in common location: {candidate}")
                    return info

        logger.warning("R engine not found; users will be guided to install R.")
        return {
            "available": False,
            "executable": "",
            "version": "",
            "home": "",
            "download_url": R_DOWNLOAD_URL,
        }

    @staticmethod
    def _common_locations() -> list:
        """Return candidate absolute paths to Rscript/R executables."""
        candidates = []
        system = sys.platform

        if system == "win32":
            # R installs under C:\\Program Files\\R\\R-x.y.z\\bin\\Rscript.exe
            roots = []
            pf = os.environ.get("ProgramFiles", r"C:\Program Files")
            for base in (pf, r"C:\Program Files"):
                r_root = os.path.join(base, "R")
                if os.path.isdir(r_root):
                    roots.append(r_root)
            for root in roots:
                try:
                    versions = sorted(
                        [d for d in os.listdir(root) if d.startswith("R-")],
                        reverse=True,
                    )
                except OSError:
                    continue
                for v in versions:
                    candidates.append(os.path.join(root, v, "bin", "Rscript.exe"))
                    candidates.append(os.path.join(root, v, "bin", "R.exe"))

        elif system == "darwin":
            # Homebrew / CRAN framework installs.
            candidates.append("/usr/local/bin/Rscript")
            candidates.append("/opt/homebrew/bin/Rscript")
            candidates.append("/usr/bin/Rscript")
            candidates.append(
                "/Library/Frameworks/R.framework/Resources/bin/Rscript"
            )

        else:  # Linux / other POSIX
            candidates.append("/usr/bin/Rscript")
            candidates.append("/usr/local/bin/Rscript")

        return candidates

    @staticmethod
    def _validate(executable: str) -> Optional[dict]:
        """Run ``<exe> --version`` and return a structured info dict, or None."""
        exe = os.path.abspath(os.path.expanduser(executable))
        if not os.path.isfile(exe):
            return None

        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            proc = subprocess.run(
                [exe, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=creationflags,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning(f"R validation failed for {exe}: {e}")
            return None

        # Rscript prints: "R scripting front-end version 4.3.1 (2023-06-16)"
        out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        version = ""
        for token in out.split():
            if token[0].isdigit() and "." in token:
                version = token
                break
        if not version:
            logger.warning(f"Could not parse R version from output: {out!r}")
            return None

        return {
            "available": True,
            "executable": exe,
            "version": version,
            "home": "",
            "download_url": "",
        }

    # ------------------------------------------------------------------ #
    #  User-facing messaging
    # ------------------------------------------------------------------ #
    @staticmethod
    def install_guidance() -> str:
        """A concise, actionable message shown when R is missing."""
        return (
            "No R runtime detected. Visualization requires R.\n"
            f"Download and install it from: {R_DOWNLOAD_URL}\n"
            "After installation, specify the R path in Settings, or add it to PATH."
        )


# Module-level convenience singleton (mirrors DeviceManager usage).
_engine = REngine()


def get_r_engine() -> REngine:
    """Return the shared REngine singleton."""
    return _engine
