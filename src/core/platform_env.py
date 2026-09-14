"""跨平台运行环境预检与启动期修正。

**必须在任何 PySide6 导入之前调用**（见 ``main.py`` 顶部）。Linux 上有三类
高频启动故障，本模块集中处理，避免平台判断散落在入口文件里：

1. 缺少 Qt 运行期系统库（``libX11`` / ``libxkbcommon`` / ``libnss3`` …）时
   ``import PySide6`` 抛出的原始 ImportError 完全看不出"该装哪个包"；
2. QtWebEngine 在容器、无用户命名空间或 ``/dev/shm`` 过小的环境中启动即崩
   （Chromium 沙箱 / 共享内存限制），需要注入 Chromium 启动参数；
3. 无图形会话（SSH、纯终端）时直接创建 QApplication 会让 Qt 以原生错误码
   中断，用户看不到任何可操作提示。

所有函数都是幂等的，且只依赖标准库，保证调用点尽可能靠前。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import platform
import re
import sys

logger = logging.getLogger("Core.PlatformEnv")

#: Linux 上 Qt / QtWebEngine 需要的系统库 → 各发行版包名。
#: 键为 soname（ImportError 中出现的名字），值为包名映射。
_LINUX_LIB_PACKAGES = {
    "libX11.so.6": {"apt": "libx11-6", "dnf": "libX11", "pacman": "libx11", "apk": "libx11"},
    "libX11-xcb.so.1": {"apt": "libx11-xcb1", "dnf": "libX11-xcb", "pacman": "libx11", "apk": "libx11"},
    "libxcb.so.1": {"apt": "libxcb1", "dnf": "libxcb", "pacman": "libxcb", "apk": "libxcb"},
    "libxkbcommon.so.0": {"apt": "libxkbcommon0", "dnf": "libxkbcommon", "pacman": "libxkbcommon", "apk": "libxkbcommon"},
    "libxkbcommon-x11.so.0": {"apt": "libxkbcommon-x11-0", "dnf": "libxkbcommon-x11", "pacman": "libxkbcommon-x11", "apk": "libxkbcommon"},
    "libEGL.so.1": {"apt": "libegl1", "dnf": "mesa-libEGL", "pacman": "libglvnd", "apk": "mesa-egl"},
    "libGL.so.1": {"apt": "libgl1", "dnf": "mesa-libGL", "pacman": "libglvnd", "apk": "mesa-gl"},
    "libfontconfig.so.1": {"apt": "libfontconfig1", "dnf": "fontconfig", "pacman": "fontconfig", "apk": "fontconfig"},
    "libglib-2.0.so.0": {"apt": "libglib2.0-0", "dnf": "glib2", "pacman": "glib2", "apk": "glib"},
    "libz.so.1": {"apt": "zlib1g", "dnf": "zlib", "pacman": "zlib", "apk": "zlib"},
    "libxcb-cursor.so.0": {"apt": "libxcb-cursor0", "dnf": "xcb-util-cursor", "pacman": "xcb-util-cursor", "apk": "xcb-util-cursor"},
    "libxcb-icccm.so.4": {"apt": "libxcb-icccm4", "dnf": "xcb-util-wm", "pacman": "xcb-util-wm", "apk": "xcb-util-wm"},
    "libxcb-keysyms.so.1": {"apt": "libxcb-keysyms1", "dnf": "xcb-util-keysyms", "pacman": "xcb-util-keysyms", "apk": "xcb-util-keysyms"},
    "libxcb-randr.so.0": {"apt": "libxcb-randr0", "dnf": "libxcb", "pacman": "libxcb", "apk": "libxcb"},
    "libxcb-render-util.so.0": {"apt": "libxcb-render-util0", "dnf": "xcb-util-renderutil", "pacman": "xcb-util-renderutil", "apk": "xcb-util-renderutil"},
    "libxcb-shape.so.0": {"apt": "libxcb-shape0", "dnf": "libxcb", "pacman": "libxcb", "apk": "libxcb"},
    "libxcb-xkb.so.1": {"apt": "libxcb-xkb1", "dnf": "libxcb", "pacman": "libxcb", "apk": "libxcb"},
    "libxcb-xinerama.so.0": {"apt": "libxcb-xinerama0", "dnf": "libxcb", "pacman": "libxcb", "apk": "libxcb"},
    "libdbus-1.so.3": {"apt": "libdbus-1-3", "dnf": "dbus-libs", "pacman": "dbus", "apk": "dbus-libs"},
    "libnss3.so": {"apt": "libnss3", "dnf": "nss", "pacman": "nss", "apk": "nss"},
    "libnspr4.so": {"apt": "libnspr4", "dnf": "nspr", "pacman": "nspr", "apk": "nspr"},
    "libxcomposite.so.1": {"apt": "libxcomposite1", "dnf": "libXcomposite", "pacman": "libxcomposite", "apk": "libxcomposite"},
    "libxdamage.so.1": {"apt": "libxdamage1", "dnf": "libXdamage", "pacman": "libxdamage", "apk": "libxdamage"},
    "libxrandr.so.2": {"apt": "libxrandr2", "dnf": "libXrandr", "pacman": "libxrandr", "apk": "libxrandr"},
    "libxshmfence.so.1": {"apt": "libxshmfence1", "dnf": "libxshmfence", "pacman": "libxshmfence", "apk": "libxshmfence"},
    "libxtst.so.6": {"apt": "libxtst6", "dnf": "libXtst", "pacman": "libxtst", "apk": "libxtst"},
    "libasound.so.2": {"apt": "libasound2", "dnf": "alsa-lib", "pacman": "alsa-lib", "apk": "alsa-lib"},
    "libcups.so.2": {"apt": "libcups2", "dnf": "cups-libs", "pacman": "libcups", "apk": "cups-libs"},
    "libdrm.so.2": {"apt": "libdrm2", "dnf": "libdrm", "pacman": "libdrm", "apk": "libdrm"},
    "libgbm.so.1": {"apt": "libgbm1", "dnf": "mesa-libgbm", "pacman": "mesa", "apk": "mesa-gbm"},
}

#: 从错误文本中直接提取"看起来像库文件名"的片段。
#: 不使用 "cannot open shared object file: X" 这类措辞匹配——X 在 Linux 上恒为
#: "No such file or directory"，按措辞提取只会得到 "No"。
_LIB_NAME_RE = re.compile(r"([A-Za-z0-9._+-]+\.(?:so(?:\.\d+)*|dylib|dll))")

_SHM_MIN_BYTES = 64 * 1024 * 1024
_TRUTHY = {"1", "true", "yes", "on"}

# --------------------------------------------------------------------------- #
#  NixOS：steam-run 自动重启
# --------------------------------------------------------------------------- #
#: 判定"已由本模块重新拉起"的标记，防止无限重启。
_FHS_LAUNCHED_ENV = "SCHOLAR_NAVIS_FHS_LAUNCHED"

#: NixOS 上 PySide6 / QtWebEngine 仍缺少的库（按 soname）。
#: 清单由 `ldd` 逐个校验 libQt6Gui / libQt6Widgets / libQt6WebEngineCore /
#: libQt6WebEngineWidgets / plugins/platforms/libqxcb.so / libexec/QtWebEngineProcess
#: 得出；`steam-run` 提供的 FHS 环境已覆盖 glib、X11、fontconfig、alsa 等大部分
#: 依赖，剩下的这些必须以额外路径补齐，否则 PySide6 导入仍会失败。
_NIX_FHS_EXTRA_SONAMES = (
    # NSS / NSPR（QtWebEngine 使用）
    "libnss3.so", "libnssutil3.so", "libsmime3.so",
    "libnspr4.so", "libplc4.so", "libplds4.so",
    # X11 / xcb 扩展
    "libXcomposite.so.1", "libXtst.so.6", "libxkbfile.so.1",
    "libxcb-cursor.so.0", "libxcb-icccm.so.4", "libxcb-image.so.0",
    "libxcb-render-util.so.0", "libxcb-util.so.1",
)

_NIX_LIB_CACHE_ENV = "SCHOLAR_NAVIS_NIX_LIB_CACHE"

#: 缓存格式版本。架构过滤是 v2 才加入的：v1 缓存可能含 32 位库目录
#: （症状为 "libsmime3.so: wrong ELF class: ELFCLASS32"），必须整体作废。
_NIX_LIB_CACHE_VERSION = "2"


def _is_compatible_elf(path: str) -> bool:
    """目标文件是否与本进程架构匹配。

    Nix store 里同一个包常同时存在 32 位与 64 位副本（如 i686-linux 变体），
    仅按文件名匹配会挑到 32 位目录，进而导致 "wrong ELF class: ELFCLASS32"。
    这里直接读 ELF 头校验类（32/64 位）与字节序，与本进程一致才接受。
    """
    try:
        with open(path, "rb") as f:
            header = f.read(6)
    except OSError:
        return False
    if header[:4] != b"\x7fELF":
        return False
    expected_class = 2 if sys.maxsize > 2 ** 32 else 1
    expected_data = 1 if sys.byteorder == "little" else 2
    return header[4] == expected_class and header[5] == expected_data


def _dir_has_compatible_lib(lib_dir: str) -> bool:
    """目录中是否至少提供了一个*架构匹配*的目标库。"""
    return any(
        _is_compatible_elf(os.path.join(lib_dir, soname))
        for soname in _NIX_FHS_EXTRA_SONAMES
    )


def _nix_lib_cache_path() -> str:
    custom = os.environ.get(_NIX_LIB_CACHE_ENV, "").strip()
    if custom:
        return os.path.expanduser(custom)
    cache_home = os.environ.get("XDG_CACHE_HOME", "").strip() or os.path.expanduser("~/.cache")
    return os.path.join(cache_home, "scholar_navis", "nixos_libs.txt")


def _scan_nix_store_lib_dirs() -> list:
    """在 ``/nix/store`` 中按 soname 定位库目录（不写死 store hash）。

    store 路径的 hash 会随重建/GC 变化，因此按"存在目标文件"匹配，而不是硬编码
    ``/nix/store/<hash>-nss-3.112.5/lib``。

    同一 soname 在 store 里常有多个副本（不同闭包各带一份）。这里用贪心集合覆盖
    挑选目录：优先选能一次覆盖最多未命中 soname 的目录。这样同一套库
    （如 nss 的 libnss3/libsmime3/libnssutil3）会来自同一个构建，避免把不同版本的
    副本混进 ``LD_LIBRARY_PATH``（同名库先命中者生效，混版本可能符号不匹配）。
    """
    import glob

    # soname -> 提供它的候选目录（保持稳定顺序，便于结果可复现）
    providers: dict = {}
    for soname in _NIX_FHS_EXTRA_SONAMES:
        candidates = sorted({
            os.path.dirname(path)
            for path in glob.glob(f"/nix/store/*/lib/{soname}")
            if _is_compatible_elf(path)          # 排除 32 位副本
        })
        if candidates:
            providers[soname] = candidates

    selected: list = []
    remaining = set(providers)
    while remaining:
        coverage: dict = {}
        for soname in remaining:
            for lib_dir in providers[soname]:
                coverage[lib_dir] = coverage.get(lib_dir, 0) + 1
        if not coverage:
            break
        # 覆盖最多者优先；并列时取字典序较小的路径，保证结果稳定
        best = max(coverage, key=lambda d: (coverage[d], -len(d)))
        selected.append(best)
        remaining = {s for s in remaining if best not in providers[s]}
    return selected


def _read_lib_cache(cache_path: str) -> list:
    """读取缓存；版本不符、目录消失或架构不再匹配时返回空（触发重扫）。"""
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
    except OSError:
        return []

    if not lines or lines[0] != f"#v{_NIX_LIB_CACHE_VERSION}":
        return []

    dirs = lines[1:]
    if dirs and all(os.path.isdir(d) and _dir_has_compatible_lib(d) for d in dirs):
        return dirs
    return []


def _write_lib_cache(cache_path: str, dirs: list) -> None:
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tmp_path = f"{cache_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(f"#v{_NIX_LIB_CACHE_VERSION}\n")
            f.write("\n".join(dirs) + "\n")
        os.replace(tmp_path, cache_path)
    except OSError as e:
        logger.debug(f"Cannot persist Nix library cache: {e}")


def _nix_store_lib_dirs() -> list:
    """带缓存的库目录定位结果（缓存失效时重新扫描）。"""
    cache_path = _nix_lib_cache_path()
    cached = _read_lib_cache(cache_path)
    if cached:
        return cached

    dirs = _scan_nix_store_lib_dirs()
    if dirs:
        _write_lib_cache(cache_path, dirs)
    return dirs


def is_nixos() -> bool:
    return platform.system() == "Linux" and os.path.isdir("/nix/store")


def maybe_relaunch_in_fhs(exc: BaseException) -> bool:
    """NixOS 上 PySide6 找不到系统库时，用 ``steam-run`` 重新拉起本进程。

    PyPI 的 PySide6 wheel 是动态链接到"标准路径"的预编译二进制，NixOS 没有
    /lib、/usr/lib，因此在 NixOS 上直接 ``uv run main.py`` 必然在导入阶段失败。
    系统里通常已有 ``steam-run``（提供 FHS 环境与 glib/X11/fontconfig 等库），
    这里再叠加少量 QtWebEngine 需要的库目录后重新执行自身，使用户无需理解
    Nix 细节即可启动。

    仅在**动态库缺失**导致的 ImportError 下触发，并通过环境变量标记防止循环；
    无法接管时返回 False，由调用方输出安装指引。

    :return: True 表示进程已被替换（正常情况下不会返回 True）。
    """
    if os.environ.get(_FHS_LAUNCHED_ENV) == "1":
        return False  # 已经由本模块拉起过，避免无限重启
    if not is_nixos() or not is_shared_library_error(exc):
        return False

    import shutil

    steam_run = shutil.which("steam-run")
    if not steam_run:
        return False

    lib_dirs = _nix_store_lib_dirs()
    if not lib_dirs:
        return False

    print(
        "[NixOS] PySide6 could not find the system Qt/X11 libraries.\n"
        "[NixOS] Relaunching inside the FHS environment provided by 'steam-run'...",
        file=sys.stderr,
        flush=True,
    )

    env = dict(os.environ)
    env[_FHS_LAUNCHED_ENV] = "1"
    existing = env.get("LD_LIBRARY_PATH", "").strip()
    # steam-run 会保留外部传入的 LD_LIBRARY_PATH，因此在其外层追加即可
    env["LD_LIBRARY_PATH"] = ":".join(lib_dirs + ([existing] if existing else []))

    try:
        os.execve(steam_run, [steam_run, sys.executable, *sys.argv], env)
    except OSError as e:
        logger.warning(f"Failed to relaunch under steam-run: {e}")
        return False
    return True  # 仅为类型完整性；execve 成功后不会执行到这里


# --------------------------------------------------------------------------- #
#  发行版识别
# --------------------------------------------------------------------------- #
def _os_release() -> dict:
    info = {}
    try:
        with open("/etc/os-release", "r", encoding="utf-8") as f:
            for line in f:
                if "=" not in line:
                    continue
                key, _, value = line.strip().partition("=")
                info[key.strip()] = value.strip().strip('"')
    except OSError:
        pass
    return info


def distro_family() -> str:
    """返回包管理器家族标识：apt / dnf / pacman / apk / nix / unknown。"""
    if os.path.isdir("/nix/store"):
        return "nix"

    info = _os_release()
    tokens = f"{info.get('ID', '')} {info.get('ID_LIKE', '')}".lower()
    if any(t in tokens for t in ("debian", "ubuntu", "linuxmint", "pop", "kali")):
        return "apt"
    if any(t in tokens for t in ("fedora", "rhel", "centos", "rocky", "almalinux", "suse", "opensuse")):
        return "dnf"
    if any(t in tokens for t in ("arch", "manjaro", "endeavouros", "cachyos")):
        return "pacman"
    if any(t in tokens for t in ("alpine", "postmarketos")):
        return "apk"

    for binary, family in (("apt-get", "apt"), ("dnf", "dnf"), ("pacman", "pacman"), ("apk", "apk")):
        if ctypes.util.find_library(binary) or os.path.exists(f"/usr/bin/{binary}"):
            return family
    return "unknown"


def _install_command(packages: list) -> str:
    pkgs = " ".join(sorted({p for p in packages if p}))
    if not pkgs:
        return "install the Qt/X11 runtime packages provided by your distribution"
    family = distro_family()
    if family == "apt":
        return f"sudo apt install -y {pkgs}"
    if family == "dnf":
        return f"sudo dnf install -y {pkgs}"
    if family == "pacman":
        return f"sudo pacman -S --needed {pkgs}"
    if family == "apk":
        return f"sudo apk add {pkgs}"
    return f"install these packages with your package manager: {pkgs}"


# --------------------------------------------------------------------------- #
#  Qt 导入失败提示
# --------------------------------------------------------------------------- #
def _extract_missing_libs(error_text: str) -> list:
    """提取错误文本中出现的库文件名（去重、保持出现顺序）。"""
    found = []
    for match in _LIB_NAME_RE.findall(error_text):
        name = match.strip()
        if name and name not in found:
            found.append(name)
    return found


def format_qt_import_error(exc: BaseException) -> str:
    """把 Qt 导入失败整理成可直接照做的操作指引。"""
    error_text = str(exc)
    system = platform.system()
    lines = [
        "=" * 72,
        "Scholar Navis cannot start: the Qt GUI runtime could not be loaded.",
        f"Original error: {type(exc).__name__}: {error_text}",
        "=" * 72,
    ]

    if system != "Linux":
        lines += [
            "",
            "Windows: install/repair the Microsoft Visual C++ Redistributable (x64) and",
            "make sure no anti-virus is quarantining DLLs inside the application folder.",
            "macOS: reinstall the application bundle; do not move individual files out of it.",
        ]
        return "\n".join(lines)

    missing = _extract_missing_libs(error_text)
    if missing:
        lines.append("")
        lines.append("Missing system libraries:")
        for lib in missing:
            lines.append(f"  - {lib}")

    family = distro_family()
    packages = []
    for lib in missing:
        mapping = _LINUX_LIB_PACKAGES.get(lib)
        if mapping and mapping.get(family):
            packages.append(mapping[family])
    if not packages:
        # 无法定位具体库时，给出覆盖 Qt + QtWebEngine 的完整依赖清单
        packages = [m.get(family, "") for m in _LINUX_LIB_PACKAGES.values()]
        packages = [p for p in packages if p]

    lines.append("")
    if family == "nix":
        lines += [
            "NixOS detected. PySide6 / QtWebEngine wheels are linked against libraries",
            "at standard paths (/lib, /usr/lib), which NixOS does not provide.",
            "",
            "Option 1 (recommended, no root): install steam-run and start the app again.",
            "Scholar Navis then detects the missing libraries and relaunches itself inside",
            "the FHS environment that steam-run provides:",
            "    nix profile install nixpkgs#steam-run",
            "    uv run main.py",
            "",
            "Option 2 (system-wide, needs root): let every program load these libraries by",
            "adding them to programs.nix-ld.libraries in /etc/nixos/configuration.nix",
            "(glib, libx11, libxcb, libxkbcommon, libxcomposite, libxdamage, libxrandr,",
            "libxshmfence, libxtst, libxkbfile, nss, nspr, alsa-lib, cups, libdrm, mesa),",
            "then run:  sudo nixos-rebuild switch",
        ]
    else:
        lines += [
            "Install the missing libraries and start the application again:",
            f"  {_install_command([p for p in packages if p])}",
        ]

    lines += [
        "",
        "Notes:",
        "  - QtWebEngine (PDF/Mermaid viewers) additionally needs libnss3, libxcomposite,",
        "    libxdamage, libxrandr and libxshmfence.",
        "  - Headless servers without any desktop session cannot run the GUI; use API mode:",
        "      python main.py --api-server",
    ]
    return "\n".join(lines)


#: 动态库加载失败在三个平台上的措辞
_SHARED_LIB_ERROR_PATTERNS = (
    re.compile(r"DLL load failed", re.IGNORECASE),
    re.compile(r"cannot open shared object file", re.IGNORECASE),
    re.compile(r"image not found", re.IGNORECASE),
    re.compile(r"symbol not found", re.IGNORECASE),
)


def is_shared_library_error(exc: BaseException) -> bool:
    """判断异常是否为"缺少动态库"这一类（各平台措辞不同，统一识别）。"""
    text = str(exc)
    return any(pattern.search(text) for pattern in _SHARED_LIB_ERROR_PATTERNS)


def shared_library_hint(exc: BaseException) -> str:
    """崩溃对话框中展示的简短可操作提示（按平台给结论）。"""
    system = platform.system()
    if system == "Linux":
        missing = _extract_missing_libs(str(exc))
        packages = []
        family = distro_family()
        for lib in missing:
            mapping = _LINUX_LIB_PACKAGES.get(lib)
            if mapping and mapping.get(family):
                packages.append(mapping[family])
        detail = f"Missing: {', '.join(missing)}" if missing else "A required Linux library is missing."
        if family == "nix":
            action = ("NixOS: run the app inside an FHS environment "
                      "(nix-shell -p steam-run, then steam-run python main.py) "
                      "or provide the libs via nix-ld / nixGL.")
        else:
            action = f"Install them, e.g.: {_install_command(packages)}" if packages else \
                "Install the Qt/X11 runtime libraries for your distribution."
        return (
            "A required system library could not be loaded.\n"
            f"{detail}\n{action}\n"
            "See the console output for the full dependency list."
        )
    if system == "Darwin":
        return ("A required macOS framework/library could not be loaded.\n"
                "Reinstall the application bundle without moving files out of it.")
    return ("A required Windows library (DLL) could not be loaded.\n"
            "Install or repair the Microsoft Visual C++ Redistributable (x64) and check "
            "that anti-virus software is not quarantining files inside the app folder.")


def no_display_message() -> str:
    """无图形会话时的启动指引。"""
    return "\n".join([
        "=" * 72,
        "Scholar Navis needs a graphical session, but none was detected.",
        f"DISPLAY={os.environ.get('DISPLAY', '')!r} "
        f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY', '')!r} "
        f"XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE', '')!r}",
        "=" * 72,
        "",
        "Options:",
        "  1) Start it from a desktop session (or over X11 forwarding: ssh -X).",
        "  2) Use the headless API server instead:",
        "       python main.py --api-server",
        "  3) Force a specific Qt platform plugin, e.g.:",
        "       QT_QPA_PLATFORM=wayland python main.py",
        "       QT_QPA_PLATFORM=xcb python main.py",
    ])


def gui_display_available() -> bool:
    """当前会话是否存在可用的图形显示（Linux 上 SSH/纯终端场景返回 False）。"""
    if platform.system() != "Linux":
        return True
    if os.environ.get("QT_QPA_PLATFORM", "").strip().lower() in ("offscreen", "minimal", "vnc"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


# --------------------------------------------------------------------------- #
#  QtWebEngine / Chromium 启动参数
# --------------------------------------------------------------------------- #
def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def in_container() -> bool:
    """是否运行在容器中（Chromium 沙箱与 /dev/shm 都受此影响）。"""
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8", errors="replace") as f:
            cgroup = f.read()
    except OSError:
        return False
    return any(tok in cgroup for tok in
               ("docker", "kubepods", "containerd", "podman", "lxc", "buildkit"))


def user_namespaces_disabled() -> bool:
    """内核是否禁用了非特权用户命名空间（Chromium 沙箱会因此起不来）。"""
    try:
        with open("/proc/sys/user/max_user_namespaces", "r") as f:
            if int(f.read().strip()) == 0:
                return True
    except (OSError, ValueError):
        pass
    try:  # Debian/Ubuntu 专用开关
        with open("/proc/sys/kernel/unprivileged_userns_clone", "r") as f:
            if int(f.read().strip()) == 0:
                return True
    except (OSError, ValueError):
        pass
    return False


def shm_too_small() -> bool:
    """``/dev/shm`` 是否小于 Chromium 需要的下限（容器默认 64MB）。"""
    try:
        stat = os.statvfs("/dev/shm")
        return stat.f_blocks * stat.f_frsize < _SHM_MIN_BYTES
    except OSError:
        return False


def _merge_chromium_flags(extra: list) -> list:
    """把额外参数合并进 ``QTWEBENGINE_CHROMIUM_FLAGS``，返回本次新增的参数。"""
    current = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    existing = current.split()
    added = [flag for flag in extra if flag not in existing]
    if added:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(existing + added)
    return added


def configure_qt_environment() -> list:
    """按运行环境修正 Qt 相关环境变量，返回本次实际生效的调整项（供日志）。

    幂等，可在导入 PySide6 之前安全重复调用。
    """
    applied = []
    if platform.system() != "Linux":
        return applied

    if _truthy_env("SCHOLAR_NAVIS_FORCE_SANDBOX"):
        # 显式要求保留沙箱：仅处理共享内存
        if shm_too_small():
            added = _merge_chromium_flags(["--disable-dev-shm-usage"])
            applied += added
        return applied

    chromium_flags = []
    if in_container() or user_namespaces_disabled():
        # Chromium 的 SUID/命名空间沙箱在容器或禁用 userns 的内核上不可用，
        # 不加 --no-sandbox 会导致 QtWebEngine 进程直接崩溃（PDF/Mermaid 白屏）。
        chromium_flags.append("--no-sandbox")
    if shm_too_small():
        chromium_flags.append("--disable-dev-shm-usage")

    if chromium_flags:
        added = _merge_chromium_flags(chromium_flags)
        if added:
            applied += added
            logger.info(f"Applied Chromium flags for this environment: {added}")

    return applied


def summarize_environment() -> dict:
    """采集与启动相关的环境事实，便于问题排查（写入日志）。"""
    return {
        "platform": platform.platform(),
        "distro_family": distro_family(),
        "session": os.environ.get("XDG_SESSION_TYPE", "unknown"),
        "display": bool(os.environ.get("DISPLAY")),
        "wayland": bool(os.environ.get("WAYLAND_DISPLAY")),
        "container": in_container(),
        "userns_disabled": user_namespaces_disabled(),
        "shm_ok": not shm_too_small(),
        "chromium_flags": os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", ""),
        "python": sys.version.split()[0],
    }


def log_environment() -> None:
    """把环境事实写进日志（在日志系统就绪后调用）。"""
    facts = summarize_environment()
    logger.info("Runtime environment: " + " | ".join(f"{k}={v}" for k, v in facts.items()))
