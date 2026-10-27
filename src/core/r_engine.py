"""
R Runtime Detector
==================

Locates an installed R / Rscript interpreter for the visualization engine.

The Scholar Navis visualization layer renders charts exclusively via R (no
LLM-generated SVG, no Python matplotlib), which keeps the plotting pipeline
deterministic, reproducible and free of extra licensing surface.

Detection strategy (in order):
    1. A user-specified path (``set_custom_path``) — highest priority.
    2. The application's private runtime (``r_env/runtime``), when present: it is
       created together with the private library, so it always matches the
       packages we install ourselves.
    3. ``Rscript`` / ``R`` found on the system ``PATH``.
    4. Common install locations (Windows ``R_HOME``, macOS, Linux).

Plotting packages are never installed into the system R; they live in the
private library under ``<BASE_DIR>/r_env`` (see :mod:`src.core.r_env`), which is
injected through ``R_LIBS_USER`` for every R subprocess.

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
import re
import shutil
import subprocess
import threading
from typing import Optional, Sequence

from src.core.platform_env import (
    PLATFORM_MACOS, PLATFORM_WINDOWS, distro_family, no_window_flags, os_family,
)

logger = logging.getLogger("Core.REngine")

# Official download landing pages, shown to users when R is absent.
R_DOWNLOAD_URL = "https://cran.r-project.org/"

#: R 脚本缺包时 ``stop()`` 的固定前缀（见 plot_engine._package_load_block）。
#: 用它反解出"到底缺哪个包"，以覆盖核心包之外、按图型按需加载的扩展包。
_MISSING_PKG_RE = re.compile(r"Required R package '([^']+)' is not installed")


def platform_family() -> str:
    """归一化平台标识：``win`` / ``mac`` / ``nix`` / ``apt`` / ``dnf`` /
    ``pacman`` / ``apk`` / ``unknown``。

    Windows / macOS 无发行版概念，先按 :func:`src.core.platform_env.os_family`
    短路；Linux 交给 :func:`src.core.platform_env.distro_family`
    （NixOS 优先识别为 ``nix``）。
    """
    family = os_family()
    if family == PLATFORM_WINDOWS:
        return "win"
    if family == PLATFORM_MACOS:
        return "mac"
    try:
        return distro_family()
    except Exception as e:  # 探测失败只影响提示精度，不影响功能
        logger.debug(f"distro_family() unavailable: {e}")
        return "unknown"


def r_install_hint() -> str:
    """按平台给出"安装 R 运行时"的可执行建议（纯文本，可多行）。"""
    family = platform_family()
    if family == "nix":
        return (
            "  NixOS: nix-shell -p R   (or add pkgs.R to environment.systemPackages)\n"
            "  For plotting you additionally need the R packages — see the R.withPackages "
            "command in the package guidance."
        )
    if family == "win":
        return (
            "  Windows: winget install RProject.R   "
            f"(or download the installer from {R_DOWNLOAD_URL}bin/windows/base/)"
        )
    if family == "mac":
        return "  macOS: brew install r"
    hints = {
        "apt": "  Debian/Ubuntu: sudo apt install -y r-base",
        "dnf": "  Fedora/RHEL: sudo dnf install -y R",
        "pacman": "  Arch: sudo pacman -S --needed r",
        "apk": "  Alpine: sudo apk add R",
    }
    return hints.get(family, f"  Download and install R from {R_DOWNLOAD_URL}")


def package_install_guidance(missing: Sequence[str]) -> str:
    """给出"缺哪些 R 包、该怎么装"的可执行指引（多行纯文本）。

    首选应用内安装：包装进私有库，系统 R 与只读的系统路径一概不动，删除
    ``r_env`` 目录即完全回退。因为不同平台的可写性差异很大——NixOS 的 R 在
    只读 /nix/store 下，手动 ``install.packages()`` 必然失败；apt/dnf/pacman 则
    另有 ``r-cran-*`` 系统包可选——所以平台自带手段作为备选一并给出，而不是
    笼统建议一条在部分平台上根本跑不通的命令。

    :param missing: 缺失的包名列表。
    :return: 纯文本指引（调用方自行决定按行渲染）。
    """
    pkgs = [str(p).strip() for p in missing if str(p).strip()]
    if not pkgs:
        return ""

    from src.core import r_env

    family = platform_family()
    lines = [
        "How to fix:",
        "  Settings > R Environment > Install missing packages",
        f"  Packages are installed into the private library: {r_env.library_dir()}",
        "  Your system R stays untouched; R_LIBS_USER points at that library, so the "
        "packages are used without changing the system environment.",
    ]

    if family == "nix":
        nix_expr = " ".join(f"ps.{p}" for p in pkgs)
        lines += [
            "",
            "The R interpreter on PATH here lives in a read-only store, so running "
            "install.packages() by hand cannot work; the in-app install writes to the "
            "private library instead.",
            f"Alternative: nix-shell -p 'R.withPackages (ps: with ps; [ {nix_expr} ])'",
        ]
    else:
        lines += [
            "",
            "Alternative: install your distribution's packaged R libraries "
            "(apt: r-cran-*, dnf: R-*, pacman: r-*).",
        ]

    lines += [
        "",
        "If this machine has no usable R runtime at all: Settings > R Environment > "
        "Download private R runtime installs a prebuilt runtime that needs no compiler.",
    ]
    return "\n".join(lines)


def plot_failure_missing_packages(error_text: str) -> list:
    """从 R 的 stderr 中反解出缺失的包名（含按图型按需加载的扩展包）。"""
    return sorted(set(_MISSING_PKG_RE.findall(error_text or "")))


def plot_failure_payload(error_text: str = "",
                         packages: Optional[Sequence[str]] = None) -> dict:
    """把"绘图失败"归一化为统一错误面板 payload（title / body / details）。

    用户可见的提醒必须是**在该平台照做就能修**的，而不是把 R 的原始 stderr
    丢给模型转述——模型会照抄 R 里的 ``install.packages()``，在 NixOS 上那是
    一条必然失败的命令。三级判定，由外到内：

      1. 未检测到 R        -> 平台相关的安装命令（Windows/mac/Linux 发行版/NixOS）；
      2. 有 R 但缺绘图包    -> 平台相关的装包指引（NixOS 走 R.withPackages）；
      3. R 正常但脚本失败   -> 通用排查建议 + 原始 stderr（折叠详情）。

    :param error_text: R 的原始报错（stderr / PlotResult.error_message）。
    :param packages:    需要探测的核心包清单；缺省取 plot_engine.CORE_R_PACKAGES。
    :return: ``{"title", "body", "details"}``，可直接交给 error_marker()。
    """
    from src.core.llm_errors import friendly_payload

    detail = (error_text or "").strip()
    engine = get_r_engine()
    info = engine.detect()

    if not info.get("available"):
        body = (
            "Visualization needs an R runtime, and none was found on this machine.\n\n"
            "How to fix:\n"
            f"{r_install_hint()}\n"
            "Then set the Rscript path under Settings > R Environment, or add it to PATH."
        )
        return friendly_payload("R Runtime Not Found", body,
                                details=detail or engine.install_guidance())

    if packages is None:
        try:
            from src.core.plot_engine import CORE_R_PACKAGES
            packages = CORE_R_PACKAGES
        except Exception as e:  # 拿不到清单时仍可给出通用排查建议
            logger.warning(f"Core R package list unavailable: {e}")
            packages = ()

    # 缺包判定有两个来源：核心包探测（解释器可用但从未装包）+ R 自己报出来的
    # 包名（能抓到 pheatmap / ggridges 这类按图型按需加载的扩展包）。
    status = {}
    if packages:
        try:
            status = engine.check_packages(packages)
        except Exception as e:
            logger.warning(f"R package probe failed: {e}")
    missing = sorted({p for p in packages if not status.get(p)}
                     | set(plot_failure_missing_packages(detail)))

    if missing:
        body = (
            "The figure was not rendered: R is installed, but these plotting packages "
            "are missing:\n"
            f"  {', '.join(missing)}\n\n"
            f"{package_install_guidance(missing)}"
        )
        return friendly_payload("R Plotting Packages Missing", body, details=detail)

    body = (
        "R could not render this figure. Typical causes: an optional package for this "
        "chart type is absent, a column/type mismatch in the data, a timeout, or an "
        "error in the generated plot spec.\n\n"
        f"R: {info.get('version', 'unknown')} ({info.get('executable', '')})\n"
        "See Technical Details for R's own error output."
    )
    return friendly_payload("Chart Rendering Failed", body, details=detail)


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

    def refresh(self):
        """作废探测与包检查缓存。

        R 环境本身发生变化时调用：私有运行时刚装好、私有库被清空等——否则
        ``detect()`` 会一直返回旧解释器（例如"明明装了运行时却仍在用系统 R"）。
        """
        with self._cache_lock:
            self._cached = None
            self._package_cache.clear()
        logger.info("R engine caches invalidated.")

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

        # 2) 应用私有运行时（若用户装过）：与私有库配套，因此优先于系统 R。
        from src.core.r_runtime import private_rscript

        private = private_rscript()
        if private:
            info = self._validate(private)
            if info:
                logger.info(f"R engine resolved via private runtime: {private}")
                return info
            # 装了却跑不起来（缺兼容层等）：回落到系统 R，并把原因留在日志里。
            logger.warning(f"Private R runtime unusable, falling back: {private}")

        # 3) Executable on PATH.
        for name in ("Rscript", "R"):
            found = shutil.which(name)
            if found:
                info = self._validate(found)
                if info:
                    logger.info(f"R engine found on PATH: {found}")
                    return info

        # 4) Common install locations.
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
        system = os_family()

        if system == PLATFORM_WINDOWS:
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

        elif system == PLATFORM_MACOS:
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

    def invalidate_packages(self):
        """作废包检查缓存（装包/卸载后必须调用）。"""
        with self._cache_lock:
            self._package_cache.clear()
        logger.info("R package cache invalidated.")

    def check_packages(self, packages, executable: str = "") -> dict:
        """批量检查 R 包是否已安装，返回 ``{包名: 是否可用}``。

        可视化引擎依赖 ggplot2 等 R 包；Windows 用户通常按官方引导一次装好，
        而 Linux 发行版的 R 包往往是分离的（打包为 r-cran-* 或需自行装包），
        因此把"缺哪个包"提前暴露给用户，比等到出图时才报错更友好。结果按
        （解释器 + 私有库 + 包集合）缓存，避免重复拉起 R 进程；私有库路径是
        缓存键的一部分，否则换过私有环境后仍会拿到旧结论。

        子进程带上 ``R_LIBS_USER``（见 :func:`src.core.r_env.child_env`），
        因此这里看到的是"系统库 + 私有库"的真实可用状态。

        :param executable: 指定检查用的解释器；省略时用 :meth:`detect` 解析出的
            那个。显式指定只用于"确认某个**特定**运行时是否已备齐包"（私有运行时
            补齐包），此时即便 :meth:`detect` 还没选它也要如实探测。
        """
        pkg_list = [str(p).strip() for p in packages if str(p).strip()]
        if not pkg_list:
            return {}

        from src.core import r_env

        exe = (executable or "").strip() or self.detect().get("executable", "")
        if not exe:
            return {p: False for p in pkg_list}
        cache_key = (exe, r_env.library_signature(), tuple(pkg_list))
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

        result = {p: False for p in pkg_list}
        try:
            proc = subprocess.run(
                [exe, "--vanilla", "-e", snippet],
                capture_output=True, text=True, timeout=60,
                env=r_env.child_env(),
                creationflags=no_window_flags(),
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

        try:
            proc = subprocess.run(
                [exe, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=no_window_flags(),
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
        """A concise, actionable message shown when R is missing.

        安装命令按平台给出（Windows/macOS/Linux 发行版/NixOS），而不是让所有
        用户都去 CRAN 找安装包——Linux 用户通常直接用发行版的包管理器。
        """
        return (
            "No R runtime detected. Visualization requires R.\n"
            "How to install:\n"
            f"{r_install_hint()}\n"
            f"Official downloads: {R_DOWNLOAD_URL}\n"
            "After installation, specify the R path in Settings > R Environment, "
            "or add it to PATH."
        )


# Module-level convenience singleton (mirrors DeviceManager usage).
_engine = REngine()


def get_r_engine() -> REngine:
    """Return the shared REngine singleton."""
    return _engine
