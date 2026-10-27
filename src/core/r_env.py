"""
R Private Environment (packages)
================================
可视化引擎需要的 R 包统一装进应用私有目录，不改动系统 R 的库路径、不写只读的
系统路径（NixOS 的 /nix/store 等）、也不需要管理员权限。目录布局：

    <BASE_DIR>/r_env/library    私有库：R 包的唯一落点（R_LIBS_USER 指向这里）
    <BASE_DIR>/r_env/logs       每次装包生成的 R 脚本与完整日志（可复现）
    <BASE_DIR>/r_env/cache      下载缓存（私有运行时的安装包）
    <BASE_DIR>/r_env/bin        私有工具（micromamba 等）
    <BASE_DIR>/r_env/runtime    可选的私有 R 运行时（见 src.core.r_runtime）

设计约束：

* **一处注入**：所有 R 子进程都经 :func:`child_env` 拿环境变量，``R_LIBS_USER``、
  ``TZ`` 与（可选的）私有 ``HOME``/``TMPDIR`` 只在这里定义，绘图、探测、装包三条
  路径不会各写一份。
* **优先复用**：装包前先让 R 解析依赖闭包，已在系统库里的包直接复用、不重装；
  Windows / macOS 走 CRAN 二进制，Linux 发行版优先二进制源（Posit PPM），
  只有确实没有二进制时才落到源码编译。
* **源按可达性选**：官方 CRAN 在部分网络下不可达（实测 000），镜像可达性也因
  网络而异，因此候选源逐个探测，不写死单一 URL。
* **可取消**：装包是长任务，:func:`install_packages` 流式回传日志并支持中断。

本模块只负责"把包装进私有库"；解释器定位在 :mod:`src.core.r_engine`，
私有运行时的下载在 :mod:`src.core.r_runtime`。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from src.core.platform_env import (
    app_root, distro_family, is_macos, is_windows, no_window_flags,
)

logger = logging.getLogger("Core.REnv")

#: 私有环境目录名（相对 :func:`src.core.platform_env.app_root`）。
ENV_DIR_NAME = "r_env"

_LIBRARY_SUBDIR = "library"
_LOGS_SUBDIR = "logs"
_CACHE_SUBDIR = "cache"
_BIN_SUBDIR = "bin"
_RUNTIME_SUBDIR = "runtime"

# 生成的 R 脚本会打印的机器可读标记：Python 侧据此得到确切的安装计划，
# 进而给出准确进度（包数未知时进度条只能是假动作）。
MARK_PLAN = "[[RNV-PLAN]]"
MARK_DONE = "[[RNV-DONE]]"

#: R 日志里"正在安装/已完成某包"的行，用于把包级别进度同步到 UI。
_RE_BEGIN_INSTALL = re.compile(r"^\*?\s*(?:begin installing package|installing \*?(?:source|binary)\*? package)\s*[‘'\"]([^’'\"]+)[’'\"]")
_RE_DONE_INSTALL = re.compile(r"^\*\s*DONE \(([^)]+)\)")

#: R 的 stderr 里"缺包/装不上"的典型写法，用于把失败原因提炼成可读结论。
_RE_FAIL_PKG = re.compile(r"(?:dependency|package)\s+[‘'\"]([^’'\"]+)[’'\"]\s+is not available")
_RE_ERROR_PKG = re.compile(r"ERROR:\s*dependenc(?:y|ies)\s+[‘'\"]([^’'\"]+)[’'\"]", re.IGNORECASE)


# --------------------------------------------------------------------------- #
#  目录布局
# --------------------------------------------------------------------------- #
def env_root() -> str:
    """私有环境根目录（``<BASE_DIR>/r_env``）。"""
    return os.path.join(app_root(), ENV_DIR_NAME)


def library_dir() -> str:
    """私有库目录：所有自装 R 包的落点。"""
    return os.path.join(env_root(), _LIBRARY_SUBDIR)


def logs_dir() -> str:
    """装包日志与生成的 R 脚本存放目录。"""
    return os.path.join(env_root(), _LOGS_SUBDIR)


def cache_dir() -> str:
    """下载缓存目录。"""
    return os.path.join(env_root(), _CACHE_SUBDIR)


def bin_dir() -> str:
    """私有工具目录（micromamba 等）。"""
    return os.path.join(env_root(), _BIN_SUBDIR)


def runtime_dir() -> str:
    """可选的私有 R 运行时根目录。"""
    return os.path.join(env_root(), _RUNTIME_SUBDIR)


def ensure_dirs() -> None:
    """建齐私有环境目录（幂等，失败只记日志不抛）。"""
    for path in (library_dir(), logs_dir(), cache_dir(), bin_dir()):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            logger.warning(f"Cannot create R environment directory {path}: {e}")


def library_signature() -> tuple:
    """私有环境的身份标识（解释器 + 私有库路径）。

    包检查结果按它缓存：换了私有运行时或私有库，旧结论必须失效。
    """
    from src.core.r_runtime import private_rscript

    return (library_dir(), private_rscript())


# --------------------------------------------------------------------------- #
#  子进程环境（唯一注入点）
# --------------------------------------------------------------------------- #
def child_env(base: Optional[Dict[str, str]] = None, tmpdir: Optional[str] = None,
              timezone: str = "") -> Dict[str, str]:
    """返回给 R 子进程用的环境变量：``R_LIBS_USER`` 指向私有库。

    ``Rscript --vanilla`` 含 ``--no-environ``，它只忽略 ``~/.Renviron`` 等文件，
    **进程环境变量仍然生效**（实测 ``.libPaths()[1]`` 即私有库），因此绘图、
    包探测、装包三条路径都用这个函数拿环境，不各写一份。

    :param tmpdir: 非空时把 ``TMPDIR`` / ``TEMP`` / ``TMP`` 指向它，用于"一次
        绘图一个私有临时目录"（见 :mod:`src.core.r_sandbox`）。刻意**不动**
        ``HOME``：fontconfig 靠 ``$HOME`` 找用户自装字体（``~/.local/share/fonts``
        等），改掉会让图里的中文字形退化成缺字方框；而"不读用户的 R 配置"已经由
        ``--vanilla`` 保证，改 ``HOME`` 没有额外收益。
    :param timezone: 非空时写入 ``TZ``（绘图固定 UTC，让日期轴不随时区漂移）。
    """
    env = dict(base if base is not None else os.environ)
    env["R_LIBS_USER"] = library_dir()
    env["R_LIBS_SITE"] = env.get("R_LIBS_SITE", "")
    if timezone:
        env["TZ"] = timezone
    if tmpdir:
        for key in ("TMPDIR", "TEMP", "TMP"):
            env[key] = tmpdir
    return env


def clear_stale_locks() -> List[str]:
    """清理私有库里遗留的 ``00LOCK-*`` 目录（装包中断会留下）。

    只动私有库：残留锁目录会让下一次装包直接失败，而它并非用户数据。
    """
    removed = []
    lib = library_dir()
    if not os.path.isdir(lib):
        return removed
    try:
        names = os.listdir(lib)
    except OSError as e:
        logger.warning(f"Cannot list R library {lib}: {e}")
        return removed
    for name in names:
        if not name.startswith("00LOCK"):
            continue
        path = os.path.join(lib, name)
        try:
            shutil.rmtree(path)
            removed.append(path)
            logger.info(f"Removed stale R install lock: {path}")
        except OSError as e:
            logger.warning(f"Cannot remove stale R install lock {path}: {e}")
    return removed


# --------------------------------------------------------------------------- #
#  包源（按可达性选择）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PackageSource:
    """一个 CRAN 兼容的包源。"""

    key: str
    label: str
    url: str
    binary: bool = False


#: 镜像候选顺序：官方 CRAN 在受限网络下不可达（实测），国内镜像优先。
_MIRRORS = (
    ("tuna", "TUNA mirror", "https://mirrors.tuna.tsinghua.edu.cn/CRAN/"),
    ("ustc", "USTC mirror", "https://mirrors.ustc.edu.cn/CRAN/"),
    ("nju", "NJU mirror", "https://mirror.nju.edu.cn/CRAN/"),
    ("cran", "CRAN", "https://cloud.r-project.org/"),
)

#: Posit Package Manager 的 Linux 二进制源（apt/dnf 系），按发行版代号取值。
_PPM_TEMPLATE = "https://packagemanager.posit.co/cran/__linux__/{codename}/latest"

#: 探测包源时对外声明的身份：必须是 R，不能是浏览器。
#:
#: :func:`src.core.network_worker.create_robust_session` 默认给会话挂上 curl_cffi 的
#: Chrome 指纹与 ``Sec-Ch-Ua`` / ``Sec-Fetch-*`` 系列头。实测（TUNA/USTC/NJU 三个
#: CRAN 镜像）带浏览器伪装时 TUNA 与 USTC 直接返回 403、NJU 200；换成 R 的身份并
#: 摘掉浏览器指纹头后三个源全部 200。也就是说用默认身份探测会把本来可用的源判成
#: 不可达，进而把包源选择逼到唯一幸存的镜像上。
#:
#: 真实装包时取索引的是 R 自己（默认 UA 形如 ``R (<version> <platform>)``），探测
#: 就该用同样的身份，结论才与"R 能不能装上"一致。这里只声明"是 R"、不复制版本与
#: 平台：它们不参与可达性判定，复制反而会随 R 版本变化而失效。
R_USER_AGENT = "R"


def _os_release_value(key: str) -> str:
    """从 /etc/os-release 读一个字段（不存在返回空串）。"""
    try:
        with open("/etc/os-release", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                name, _, value = line.partition("=")
                if name.strip() == key:
                    return value.strip().strip('"').strip("'")
    except OSError as e:
        logger.debug(f"os-release unavailable: {e}")
    return ""


def ppm_codename() -> str:
    """返回 Posit 二进制源所需的发行版代号（不适用时为空串）。"""
    if is_windows() or is_macos():
        return ""
    try:
        family = distro_family()
    except Exception as e:  # 探测失败只影响"能否用二进制源"
        logger.debug(f"distro_family() unavailable: {e}")
        return ""
    if family not in ("apt", "dnf"):
        return ""
    return _os_release_value("VERSION_CODENAME") or _os_release_value("VERSION_ID")


def candidate_sources(user_repo: str = "") -> List[PackageSource]:
    """按优先级给出候选包源：用户自定义 -> Posit 二进制 -> 镜像 -> CRAN。

    二进制源只对 apt/dnf 系给出：其他发行版（含 NixOS）的 glibc/路径与 Posit
    预编译产物不匹配，装了也加载不起来。
    """
    sources: List[PackageSource] = []
    user = (user_repo or "").strip()
    if user:
        sources.append(PackageSource("user", "User-defined repository", user))

    codename = ppm_codename()
    if codename:
        sources.append(PackageSource(
            "ppm", "Posit Package Manager (prebuilt Linux binaries)",
            _PPM_TEMPLATE.format(codename=codename), binary=True,
        ))

    for key, label, url in _MIRRORS:
        sources.append(PackageSource(key, label, url))

    if is_windows():
        # Windows / macOS 的 CRAN 源本身按二进制分发，binary 标记只影响 options(pkgType)。
        sources.insert(0, PackageSource("win-cran", "CRAN (binaries)", _MIRRORS[-1][2], binary=True))
    elif is_macos():
        sources.insert(0, PackageSource("mac-cran", "CRAN (binaries)", _MIRRORS[-1][2], binary=True))
    return sources


def probe_session():
    """构造探测包源用的会话：沿用应用代理/CA 配置，身份是 R 而不是浏览器。

    :func:`src.core.network_worker.create_robust_session` 提供代理与 CA 设置，
    但它默认伪装 Chrome；CRAN 镜像会因此拒绝请求（见 :data:`R_USER_AGENT`）。
    这里摘掉浏览器指纹头并改成 R 的身份，其余行为不变。
    """
    from src.core.network_worker import create_robust_session

    session = create_robust_session()
    for name in [k for k in session.headers
                 if k.lower().startswith(("sec-", "upgrade-insecure"))]:
        session.headers.pop(name, None)
    session.headers["Accept"] = "*/*"
    session.headers["User-Agent"] = R_USER_AGENT
    return session


def close_session(session) -> None:
    """静默关闭探测会话（关闭失败不影响探测结论）。"""
    if session is None:
        return
    try:
        session.close()
    except Exception as e:
        logger.debug(f"Closing probe session failed: {e}")


def probe_source(source: PackageSource, timeout: float = 8.0, session=None) -> bool:
    """探测包源是否可用（取一次 PACKAGES.gz 的开头即可，不下载整个索引）。

    会话由 :func:`probe_session` 给出，因此既沿用应用设置里的代理与 CA 配置，
    又不会用浏览器伪装去撞 CRAN 镜像的 403（详见 :data:`R_USER_AGENT`）。

    :param session: 复用的探测会话；省略时临时建一个并负责关闭。
    """
    url = source.url.rstrip("/") + "/src/contrib/PACKAGES.gz"
    own_session = session is None
    if own_session:
        session = probe_session()
    try:
        # curl_cffi 的 Response 不支持上下文管理器（实测 TypeError），
        # 因此显式关闭，避免连接泄漏。
        resp = session.get(url, timeout=timeout, stream=True)
        try:
            if resp.status_code >= 400:
                logger.info(f"R package source {source.key} unreachable: HTTP {resp.status_code}")
                return False
            next(resp.iter_content(1024), b"")
        finally:
            try:
                resp.close()
            except Exception as e:  # 关闭失败不代表源不可用
                logger.debug(f"Closing probe response failed: {e}")
        logger.info(f"R package source {source.key} available: {source.url}")
        return True
    except Exception as e:  # 网络异常一律视为"不可用"，交由下一个候选
        logger.info(f"R package source {source.key} probe failed: {e}")
        return False
    finally:
        if own_session:
            close_session(session)


def select_source(user_repo: str = "", timeout: float = 8.0,
                  log: Optional[Callable[[str], None]] = None) -> Optional[PackageSource]:
    """按顺序探测并返回第一个可达的包源；全部不可达返回 None。

    所有候选共用同一个会话：源之间只是 URL 不同，复用连接省掉重复的 TLS 握手。
    """
    candidates = candidate_sources(user_repo)
    session = probe_session()
    try:
        for source in candidates:
            if log:
                log(f"Probing R package repository: {source.label} ({source.url})")
            if probe_source(source, timeout=timeout, session=session):
                if log:
                    log(f"Using repository: {source.url}"
                        + (" (prebuilt binaries)" if source.binary else " (source packages)"))
                return source
    finally:
        close_session(session)
    logger.warning("No reachable R package repository found.")
    return None


# --------------------------------------------------------------------------- #
#  装包脚本
# --------------------------------------------------------------------------- #
def _r_literal(value: str) -> str:
    """把路径转成 R 字符串字面量（统一正斜杠，转义反斜杠与引号）。"""
    text = str(value).replace("\\", "/")
    return '"' + text.replace('"', '\\"') + '"'


def _r_vector(values: Iterable[str]) -> str:
    return "c(" + ", ".join(_r_literal(v) for v in values) + ")"


def build_install_script(packages: Sequence[str], repo_url: str, binary: bool = False,
                         ncpus: int = 0) -> str:
    """生成装包用的 R 脚本。

    脚本在**同一个 R 会话**里先解析依赖闭包再安装：既复用系统库里已有的包
    （``.libPaths()`` 里私有库在前、系统库在后），又避免为算进度而重复下载索引。
    ``[[RNV-PLAN]]`` 标记让 Python 侧拿到确切计划，进度条据此推进。
    """
    lib = _r_literal(library_dir())
    want = _r_vector(packages)
    repos = _r_vector([repo_url])
    cpu = int(ncpus) if ncpus > 0 else 0
    pkg_type = '"binary"' if binary else 'getOption("pkgType")'
    return f"""# 由 Scholar Navis 生成：把绘图所需的 R 包装进私有库。
# 不使用 --no-save 之外的特殊选项，保证用户可原样重跑本脚本复现安装过程。
.libPaths(c({lib}, .libPaths()))
dir.create({lib}, recursive = TRUE, showWarnings = FALSE)

options(
  repos = {repos},
  pkgType = {pkg_type},
  Ncpus = {cpu if cpu else 'max(1L, min(4L, parallel::detectCores()))'},
  timeout = 1800,
  warn = 1
)

want <- {want}
installed <- vapply(want, function(p) requireNamespace(p, quietly = TRUE), logical(1))
todo <- want[!installed]

cat("Library :", {lib}, "\\n")
cat("Repo    :", getOption("repos")[[1]], "\\n")
cat("Targets :", if (length(todo)) paste(todo, collapse = ", ") else "(nothing to do)", "\\n")

if (length(todo)) {{
  db <- tryCatch(available.packages(), error = function(e) NULL)
  if (is.null(db) || !nrow(db)) {{
    cat("ERROR: repository index unavailable\\n")
    quit(status = 2)
  }}
  deps <- tools::package_dependencies(todo, db = db, recursive = TRUE)
  plan <- setdiff(unique(c(todo, unlist(deps))), rownames(installed.packages()))
  cat("{MARK_PLAN}", length(plan), " ", paste(plan, collapse = " "), "\\n", sep = "")

  ok <- tryCatch({{
    install.packages(plan, lib = {lib}, dependencies = NA, quiet = FALSE)
    TRUE
  }}, error = function(e) {{
    cat("ERROR:", conditionMessage(e), "\\n")
    FALSE
  }})
  if (!ok) quit(status = 3)
}}

still <- want[!vapply(want, function(p) requireNamespace(p, quietly = TRUE), logical(1))]
if (length(still)) {{
  cat("MISSING:", paste(still, collapse = ", "), "\\n")
  quit(status = 4)
}}
cat("{MARK_DONE}\\n")
"""


def _new_log_path(prefix: str) -> str:
    ensure_dirs()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(logs_dir(), f"{prefix}-{stamp}.log")


def install_packages(packages: Sequence[str], *, interpreter: str, repo_url: str,
                     binary: bool = False, log_cb: Optional[Callable[[str], None]] = None,
                     progress_cb: Optional[Callable[[int, str], None]] = None,
                     cancel_cb: Optional[Callable[[], bool]] = None,
                     ncpus: int = 0) -> dict:
    """把 ``packages`` 装进私有库，流式回传日志，支持取消。

    :param interpreter: 用于装包的 Rscript（私有运行时优先，其次是系统 R）。
    :param repo_url:    已选定的包源 URL（见 :func:`select_source`）。
    :param log_cb:      每读到一行 R 输出调用一次。
    :param progress_cb: ``(百分比, 说明)``，包级别推进。
    :param cancel_cb:   返回 True 时终止子进程并清理残留锁目录。
    :return: ``{"success", "cancelled", "log_path", "plan", "error"}``
    """
    pkgs = [str(p).strip() for p in packages if str(p).strip()]
    result = {"success": False, "cancelled": False, "log_path": "", "plan": [], "error": ""}
    if not pkgs:
        result.update(success=True, error="")
        return result

    ensure_dirs()
    script_path = os.path.join(logs_dir(),
                               f"install-{time.strftime('%Y%m%d-%H%M%S')}.R")
    try:
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(build_install_script(pkgs, repo_url, binary=binary, ncpus=ncpus))
    except OSError as e:
        result["error"] = f"cannot write install script: {e}"
        logger.error(result["error"])
        return result

    log_path = _new_log_path("install")
    result["log_path"] = log_path
    clear_stale_locks()

    if log_cb:
        log_cb(f"R package install started with {os.path.basename(interpreter)}")
        log_cb(f"Packages: {', '.join(pkgs)}")
        log_cb(f"Repository: {repo_url}")
        log_cb(f"Script: {script_path}")
        log_cb(f"Log: {log_path}")

    total = 0
    done = 0
    cancelled = False
    tail: List[str] = []

    try:
        with open(log_path, "w", encoding="utf-8", buffering=1) as log_file:
            proc = subprocess.Popen(
                [interpreter, "--vanilla", script_path],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=child_env(),
                creationflags=no_window_flags(),
            )
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    line = raw.rstrip("\n")
                    log_file.write(raw)
                    tail.append(line)
                    if len(tail) > 40:
                        tail.pop(0)

                    if line.startswith(MARK_PLAN):
                        parts = line[len(MARK_PLAN):].strip().split(" ", 1)
                        total = int(parts[0]) if parts and parts[0].isdigit() else 0
                        result["plan"] = parts[1].split() if len(parts) > 1 else []
                        if progress_cb:
                            progress_cb(10, f"Installing {total} package(s)")
                        continue

                    match = _RE_DONE_INSTALL.match(line)
                    if match and total:
                        done += 1
                        if progress_cb:
                            pct = min(95, 10 + int(80 * done / max(1, total)))
                            progress_cb(pct, f"{done}/{total} installed: {match.group(1)}")
                    elif _RE_BEGIN_INSTALL.match(line) and log_cb:
                        pass  # 规划已给出包级进度，这里不重复刷行

                    if log_cb:
                        log_cb(line)

                    if cancel_cb and cancel_cb():
                        cancelled = True
                        proc.terminate()
                        break
            finally:
                if proc.poll() is None:
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                code = proc.returncode
    except (OSError, subprocess.SubprocessError) as e:
        result["error"] = f"cannot run R: {e}"
        logger.error(result["error"])
        if log_cb:
            log_cb(result["error"])
        return result

    if cancelled:
        result["cancelled"] = True
        result["error"] = "cancelled by user"
        clear_stale_locks()
        if progress_cb:
            progress_cb(0, "Cancelled")
        if log_cb:
            log_cb("Install cancelled; stale lock directories cleaned.")
        return result

    if code == 0:
        result["success"] = True
        if progress_cb:
            progress_cb(100, f"{done or total} package(s) installed")
        if log_cb:
            log_cb("R package install finished successfully.")
        return result

    result["error"] = _summarize_failure(code, tail)
    if progress_cb:
        progress_cb(0, "Install failed")
    if log_cb:
        log_cb(f"R package install failed (R exit code {code}): {result['error']}")
    return result


def _summarize_failure(code: int, tail: Sequence[str]) -> str:
    """从 R 输出尾部提炼一句可读的失败原因。"""
    for line in reversed(list(tail)):
        text = line.strip()
        if not text or text.startswith("environment:"):
            continue
        if text.startswith("ERROR") or "not available" in text or "non-zero exit" in text:
            for regex in (_RE_FAIL_PKG, _RE_ERROR_PKG):
                match = regex.search(text)
                if match:
                    return f"package '{match.group(1)}' unavailable from this repository"
            return text[:300]
    return f"R exited with code {code}"


def installed_packages() -> List[str]:
    """列出私有库里已安装的包名（只读目录，不启动 R 进程）。"""
    lib = library_dir()
    try:
        names = os.listdir(lib)
    except OSError:
        return []
    return sorted(n for n in names
                  if not n.startswith("00LOCK") and os.path.isdir(os.path.join(lib, n)))


def environment_size_bytes() -> int:
    """私有环境的磁盘占用（用于设置页展示，便于用户判断是否清理）。"""
    total = 0
    for root, _dirs, files in os.walk(env_root()):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total