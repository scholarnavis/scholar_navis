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

    # 由 __new__ 初始化为实例属性；类体声明提供类型与缺省值，满足静态检查
    _custom_path: Optional[str] = None
    _cached: Optional[dict] = None

    def __new__(cls) -> "REngine":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._custom_path = None
                cls._instance._cached = None
                cls._instance._package_cache = {}
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
            # 解释器变了，包检查结果不再有效
            self._package_cache.clear()
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

    def check_packages(self, packages) -> dict:
        """批量检查 R 包是否已安装，返回 ``{包名: 是否可用}``。

        可视化引擎依赖 ggplot2 等 R 包；Windows 用户通常按官方引导一次装好，
        而 Linux 发行版的 R 包往往是分离的（打包为 r-cran-* 或需自行
        ``install.packages``），因此把"缺哪个包"提前暴露给用户，比等到出图时
        才报错更友好。结果按（解释器 + 包集合）缓存，避免重复拉起 R 进程。
        """
        pkg_list = [str(p).strip() for p in packages if str(p).strip()]
        if not pkg_list:
            return {}

        info = self.detect()
        if not info.get("available"):
            return {p: False for p in pkg_list}

        exe = info.get("executable", "")
        cache_key = (exe, tuple(pkg_list))
        with self._cache_lock:
            cached = self._package_cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        quoted = ", ".join(f'"{p}"' for p in pkg_list)
        snippet = (
            f".pkgs <- c({quoted});"
            ".ok <- vapply(.pkgs, function(p) "
            "if (requireNamespace(p, quietly = TRUE)) '1' else '0', character(1));"
            "cat(paste(.ok, collapse = ''))"
        )

        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        result = {p: False for p in pkg_list}
        try:
            proc = subprocess.run(
                [exe, "--vanilla", "-e", snippet],
                capture_output=True, text=True, timeout=60,
                creationflags=creationflags,
            )
            flags = (proc.stdout or "").strip().splitlines()
            flags = flags[-1].strip() if flags else ""
            if len(flags) == len(pkg_list) and set(flags) <= {"0", "1"}:
                result = {p: flag == "1" for p, flag in zip(pkg_list, flags)}
            else:
                logger.warning(
                    f"Unexpected R package probe output: {proc.stdout!r} / {proc.stderr!r}")
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning(f"R package check failed: {e}")

        missing = [p for p, ok in result.items() if not ok]
        if missing:
            logger.warning(f"Missing R packages: {', '.join(missing)}")
        else:
            logger.info(f"All {len(pkg_list)} checked R packages are available.")

        with self._cache_lock:
            self._package_cache[cache_key] = dict(result)
        return result

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
