"""发布产物构建入口（CI 与本地共用）。

产物形态按平台分派
------------------
* **Linux**：**源码包**（不再使用 PyInstaller）。PyPI 上的 PySide6 / torch /
  onnxruntime 都是 manylinux wheel，冻结成可执行产物时必须把 Qt/X11 系统库分析并
  复制进去，代价是构建被绑死在特定发行版镜像上、产物与宿主 glibc 强绑定（详见
  ``.github/workflows/build-release.yml`` 的历史注释），而且用户装不了自己发行版的
  Qt。源码包把这层依赖解析交还给用户机器的 Python 工具链：产物与 glibc 解耦，
  更新包体积从 GB 级降到 MB 级。
* **其它平台（Windows，以及 CI 未覆盖的 macOS）**：PyInstaller ``--onedir`` 冻结包。
  Windows 用户机器上不一定有可用的 Python，因此继续把解释器与依赖一起带上。

两种形态对外完全一致的部分（改名/改结构前先读 ``build_support/r2_release.py`` 的
命名契约）
--------------------------------------------------------------------------
* 产物名 ``{app_name}_{平台}_{通道}_v{版本}.zip``：R2 对象名、Worker 的前缀列举、
  应用内的更新检查全都依赖它，因此**两种形态共用同一个命名**，Worker 无需改动；
* 压缩包顶层目录名 ``<app_name_safe>/``（解压出来是一个目录，不是一个散开的树）；
* 随包分发的许可文本由 ``build_support/notices.py`` 统一提供（见 :func:`stage_notices`
  的 ``mode``：冻结产物收集 wheel 许可正文，源码包只出声明）。
"""
import glob
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from build_support.notices import stage_notices
from build_support.r2_release import ReleaseUploadError, publish_artifact
from src.core.version import __app_name__, __github__, __version__, release_channel

os.environ["TRANSFORMERS_VERBOSITY"] = "error"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

logger = logging.getLogger("BuildApp")

#: 应用入口：冻结脚本与源码包白名单共用同一处定义。
ENTRY_POINT = "main.py"

#: 源码包的内容白名单：运行应用所需的最小集合（顶层名字与仓库一致）。
#: 许可文本（``LICENSE`` / ``LICENSES/``）**不**在这里重复列出——它们由
#: ``stage_notices`` 提供，避免同一份文件有两处来源而漂移。
SOURCE_BUNDLE_PATHS = (
    ENTRY_POINT,
    "src",
    "Assets",       # 图标 / JS / 纹理，运行期按包根解析
    "plugins",      # 内置插件样例
    "docs",         # README 里的相对图片引用（不在包里会变成死链）
    "pyproject.toml",
    "uv.lock",      # 唯一锁定的依赖清单：run.sh 用 uv sync --locked 消费
    "requirements.txt",
    "README.md",
)

#: 复制源码树时跳过的目录名（构建/缓存产物，不属于源码）。
_SOURCE_SKIP_DIRS = {"__pycache__", ".git", ".idea", ".vscode", ".ruff_cache", ".pytest_cache"}

#: 复制源码树时跳过的文件后缀。
_SOURCE_SKIP_SUFFIXES = (".pyc", ".pyo", ".pyz")

#: 启动器模板（仓库内唯一一份）与它在包内的名字。
SOURCE_LAUNCHER = os.path.join("build_support", "source_launcher.sh")
SOURCE_LAUNCHER_NAME = "run.sh"


def platform_tag() -> str:
    """产物命名用的平台标记（win / mac / linux）。"""
    return {"Windows": "win", "Darwin": "mac", "Linux": "linux"}.get(
        platform.system(), "unknown")


def sync_pyproject_version():
    toml_path = "pyproject.toml"
    if not os.path.exists(toml_path):
        return
    with open(toml_path, "r", encoding="utf-8") as f:
        content = f.read()
    new_content = re.sub(
        r'^version\s*=\s*".*?"',
        f'version = "{__version__}"',
        content, count=1, flags=re.MULTILINE
    )
    if content != new_content:
        with open(toml_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"[*] Synced pyproject.toml version to {__version__}")


# --------------------------------------------------------------------------- #
# 源码包（Linux）
# --------------------------------------------------------------------------- #
def _copy_source_tree(target_folder: str) -> tuple:
    """把 :data:`SOURCE_BUNDLE_PATHS` 白名单复制进 ``target_folder``。

    白名单而不是"整个仓库"：仓库里还有 ``.venv`` / ``models`` / ``logs`` 等运行期
    目录，全量复制会把数 GB 的本地状态打进产物。返回 ``(复制文件数, 缺失的顶层条目)``。
    """
    copied = 0
    missing = []
    for name in SOURCE_BUNDLE_PATHS:
        src = Path(name)
        if not src.exists():
            missing.append(name)
            continue
        if src.is_file():
            shutil.copy2(src, Path(target_folder) / src.name)
            copied += 1
            continue

        # 目录：逐文件复制，就地剪掉缓存目录（os.walk 的 dirs 原地修改即剪枝）
        for current, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in _SOURCE_SKIP_DIRS]
            dest_dir = Path(target_folder) / current
            dest_dir.mkdir(parents=True, exist_ok=True)
            for filename in files:
                if filename.endswith(_SOURCE_SKIP_SUFFIXES):
                    continue
                shutil.copy2(os.path.join(current, filename), dest_dir / filename)
                copied += 1
    return copied, missing


def _copy_add_data(src: str, dest: str, target_folder: str) -> None:
    """按 ``stage_notices`` 返回的 ``(源, 目标)`` 落实一个条目。

    目标 ``"."`` 表示包根（与 PyInstaller ``--add-data`` 的语义一致）。
    """
    target_dir = Path(target_folder) if dest in (".", "") else Path(target_folder) / dest
    if os.path.isdir(src):
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, target_dir)
    else:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target_dir / os.path.basename(src))


def _refresh_lockfile(package_dir: str) -> None:
    """把包内 ``uv.lock`` 里根项目的版本号刷成刚写进 ``pyproject.toml`` 的版本。

    uv 会把**根项目自己的版本号**写进 lockfile，所以 ``sync_pyproject_version`` 一改
    ``pyproject.toml`` 的 ``version``，lockfile 立刻被判为过期。而源码包是同时带着
    ``pyproject.toml`` 与 ``uv.lock`` 发给用户的，用户执行 ``run.sh`` 里的
    ``uv sync --locked`` 就会硬失败（"``uv.lock`` needs to be updated, but ``--locked``
    was provided"）——这正是这个函数存在的唯一原因。

    用 ``--offline``：版本号变化不该引起任何重新解析，若离线刷新都失败，说明仓库里的
    lockfile 本来就与依赖声明不一致，这种"用户拿到手也装不上"的产物必须在构建期断掉，
    而不是让它去联网重新解析出一套与 CI 验证环境不同的依赖。
    """
    if not (os.path.exists(os.path.join(package_dir, "pyproject.toml"))
            and os.path.exists(os.path.join(package_dir, "uv.lock"))):
        return

    uv = shutil.which("uv")
    if not uv:
        print("\n[-] uv not found on PATH: the bundled uv.lock cannot be refreshed, and "
              "`uv sync --locked` would refuse to run for users. Install uv and rebuild.")
        sys.exit(1)

    print("[*] Refreshing uv.lock in the bundle (root project version only)...")
    refreshed = subprocess.run([uv, "lock", "--offline"], cwd=package_dir,
                               capture_output=True, text=True, check=False)
    if refreshed.returncode != 0:
        print(f"\n[-] `uv lock --offline` failed inside the bundle. Run `uv lock` in the "
              f"repository and commit the result, then rebuild.\n"
              f"{refreshed.stdout}{refreshed.stderr}")
        sys.exit(1)
    logger.debug("uv lock: %s", (refreshed.stdout + refreshed.stderr).strip().replace("\n", " | "))

    # 后置条件：必须真的能通过 --locked 校验，否则用户第一次运行就会失败。
    checked = subprocess.run([uv, "lock", "--check", "--offline"], cwd=package_dir,
                             capture_output=True, text=True, check=False)
    if checked.returncode != 0:
        print(f"\n[-] The bundled pyproject.toml / uv.lock pair is not self-consistent:\n"
              f"{checked.stdout}{checked.stderr}")
        sys.exit(1)


def build_source_bundle(target_folder: str, build_dir: str) -> None:
    """Linux 产物：源码包 = 源码树 + 启动器 + 许可声明。

    包内布局（解压后即 ``<app_name_safe>/``）::

        run.sh                  # 环境引导与启动（模板见 build_support/source_launcher.sh）
        main.py  src/  Assets/  plugins/  docs/
        pyproject.toml  uv.lock  requirements.txt  README.md
        LICENSE  LICENSES/  THIRD_PARTY_NOTICES.md
    """
    if os.path.exists(target_folder):
        shutil.rmtree(target_folder)
    os.makedirs(target_folder, exist_ok=True)

    copied, missing = _copy_source_tree(target_folder)
    if missing:
        # 缺文件时产物"看起来成功、跑起来报 ModuleNotFoundError"，宁可在这里断掉。
        print(f"\n[-] Source bundle would be incomplete, missing from the repository: "
              f"{', '.join(missing)}")
        sys.exit(1)
    logger.debug("Copied %d source file(s) into %s", copied, target_folder)

    notices = stage_notices(os.path.join(build_dir, "notices"),
                            app_name=__app_name__, version=__version__,
                            source_url=__github__, mode="source")
    for src, dest in notices["add_data"]:
        _copy_add_data(src, dest, target_folder)

    if not os.path.exists(SOURCE_LAUNCHER):
        print(f"\n[-] Launcher template not found: {SOURCE_LAUNCHER}")
        sys.exit(1)
    launcher = os.path.join(target_folder, SOURCE_LAUNCHER_NAME)
    shutil.copy2(SOURCE_LAUNCHER, launcher)
    # 显式设置可执行位：zip 会把它写进 external_attr，用 unzip 解压后可直接 ./run.sh。
    os.chmod(launcher, 0o755)

    # 必须在源码树复制完之后（此时包内才有 pyproject.toml / uv.lock 这一对文件）。
    _refresh_lockfile(target_folder)

    print(f"[*] Source bundle staged: {copied} source file(s) + {SOURCE_LAUNCHER_NAME} "
          f"+ {len(notices['add_data'])} license notice entr(ies).")


# --------------------------------------------------------------------------- #
# 冻结包（Windows / macOS）
# --------------------------------------------------------------------------- #
# R2 发布逻辑抽在 build_support/r2_release.py（独立模块，便于注入客户端做测试）。
# 这里只留调用点，见 build_app() 的 R2 段。
def build_frozen_bundle(app_name_safe: str, sys_os: str, build_dir: str) -> None:
    """Windows 产物：PyInstaller ``--onedir``。失败时非零退出，不进入打包/发布。"""
    is_windows = sys_os == "Windows"

    hook_file = "torch_runtime_hook.py"
    with open(hook_file, "w", encoding="utf-8") as f:
        f.write("import torch\nimport torch.autograd\n")
    print("[*] Generated PyTorch Runtime Hook to prevent circular imports.")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",
        f"--name={app_name_safe}",
        f"--runtime-hook={hook_file}",
    ]

    if sys_os in ("Windows", "Darwin"):
        # Linux 下不加 --windowed：该选项在 Linux 上只是丢弃 stdout/stderr，
        # 而桌面启动器本就无终端，日志改由 logs/ 目录承载（见 setup_logger）。
        # 保留 stdout 便于从终端启动时直接观察启动期异常。
        cmd.append("--windowed")

    packages_to_collect = [
        "optimum", "transformers", "onnxruntime", "onnx", "tokenizers",
        "chromadb", "anyio"
    ]

    data_to_collect = ["docx", "litellm"]

    hidden_imports = [
        "torch",
        "torch.autograd",
        "safetensors",
        "huggingface_hub",
        "ssl",
        "_ssl",
        "tiktoken_ext.openai_public",
        "tiktoken_ext"
    ]

    if is_windows:
        # Windows 需要显式附带 OpenSSL DLL（uv 环境的 Python 不一定带上）。
        # Linux/macOS 的 CPython 由系统或自带的 .so 提供 ssl，无需额外收集。
        ssl_search_paths = [
            sys.prefix,  # venv 根目录
            os.path.join(sys.prefix, "DLLs"),  # venv DLLs
            os.path.join(sys.prefix, "Scripts"),  # venv Scripts
            sys.base_prefix,  # uv 底层基础 Python 根目录
            os.path.join(sys.base_prefix, "DLLs"),  # uv 底层基础 Python DLLs
            os.path.join(sys.base_prefix, "Scripts"),  # uv 底层基础 Python Scripts
        ]

        ssl_dlls_found = False
        for path in set(ssl_search_paths):  # 用 set 去重
            if not os.path.exists(path):
                continue
            dlls = glob.glob(os.path.join(path, "libcrypto*.dll")) + \
                   glob.glob(os.path.join(path, "libssl*.dll"))
            for dll in dlls:
                cmd.append(f"--add-binary={dll};.")
                ssl_dlls_found = True

        if not ssl_dlls_found:
            print("\n[!] Warning: OpenSSL dynamic-link libraries (libcrypto/libssl) were not detected within the uv Python environment. Please verify the environment configuration should runtime errors occur.\n")
        else:
            print("\n[*] The OpenSSL DLLs have been successfully identified and integrated.")

    for pkg in packages_to_collect:
        cmd.extend(["--collect-all", pkg])
    for pkg in data_to_collect:
        cmd.extend(["--collect-data", pkg])
    for hi in hidden_imports:
        cmd.extend(["--hidden-import", hi])

    # 依然需要 Copy Metadata 骗过 transformers 的检查
    cmd.extend(["--copy-metadata", "transformers"])
    cmd.extend(["--copy-metadata", "tqdm"])
    cmd.extend(["--copy-metadata", "regex"])
    cmd.extend(["--copy-metadata", "torch"])
    cmd.extend(["--copy-metadata", "tiktoken"])
    cmd.extend(["--copy-metadata", "onnx"])
    cmd.extend(["--copy-metadata", "onnxruntime"])
    cmd.extend(["--copy-metadata", "optimum"])

    # --add-data 的分隔符是平台相关的：Windows 为 ';'，POSIX 为 ':'。
    cmd.append(f"--add-data=Assets{os.pathsep}Assets")

    # 许可合规：AGPL-3 §4/§6 与 LGPL-3 §4 要求随二进制向接收者提供许可文本与声明，
    # 而 PyInstaller 默认不带任何许可文件（此前发布的产物里连 LICENSE 都没有）。
    notices = stage_notices(os.path.join(build_dir, "notices"),
                            app_name=__app_name__, version=__version__,
                            source_url=__github__)
    for src, dest in notices["add_data"]:
        cmd.append(f"--add-data={src}{os.pathsep}{dest}")
    print(f"[*] Bundled third-party notices: {notices['license_files']} license file(s) "
          f"collected; {len(notices['missing_text'])} distribution(s) ship no text.")

    excludes = [
        "tkinter", "matplotlib", "seaborn", "jupyter", "notebook",
        "IPython", "plotly", "pygame",
        "torchvision", "nvidia", "triton", "torchaudio",
        "PyQt6", "PyQt5"
    ]
    for ex in excludes:
        cmd.append(f"--exclude-module={ex}")

    if is_windows and os.path.exists("Assets/icon.ico"):
        cmd.append("--icon=Assets/icon.ico")
    elif sys_os == "Darwin" and os.path.exists("Assets/icon.icns"):
        cmd.append("--icon=Assets/icon.icns")
    # Linux 的 .desktop 图标不由 PyInstaller 嵌入，运行时使用 Assets/icon.png。

    cmd.append(ENTRY_POINT)

    print("[*] Executing PyInstaller (Packaging PySide6 & ONNXRuntime)...")
    # check=False：失败由下面的 returncode 判定，以便区分"打包失败"与"异常退出"。
    result = subprocess.run(cmd, check=False)

    # 无论打包成功失败，清理掉临时生成的 Hook 文件
    if os.path.exists(hook_file):
        os.remove(hook_file)

    if result.returncode != 0:
        # 必须非零退出。旧写法只 `return`，脚本仍以 0 结束，CI 会继续往下走，
        # 于是真正的失败被后一句 "cp: cannot stat '/app/*.zip'" 掩盖；
        # 这里直接终止，让日志第一条错误就是根因。
        print("\n[-] PyInstaller build failed. Aborting before packaging/publishing.")
        sys.exit(1)


# --------------------------------------------------------------------------- #
# 打包与发布（两种形态共用）
# --------------------------------------------------------------------------- #
def _archive_folder(target_folder: str, dist_dir: str, archive_path: str) -> None:
    """把 ``target_folder`` 压成 ``archive_path``（顶层目录名保留为文件夹名）。"""
    file_count = 0
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as zipf:
        for root, dirs, files in os.walk(target_folder):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, dist_dir)
                zipf.write(file_path, arcname)
                file_count += 1
    if not file_count:
        # 空产物会让"构建成功"变成假象（Worker 照样能列举到对象）。
        print(f"\n[-] No file found under {target_folder}; refusing to publish an empty "
              f"archive.")
        sys.exit(1)
    print(f"[+] Packed {file_count} file(s) to {archive_path}")


def build_app():
    sync_pyproject_version()

    sys_os = platform.system()
    tag = platform_tag()

    dist_dir = "dist"
    build_dir = "build"
    app_name_safe = __app_name__.replace(" ", "_").lower()

    if os.path.exists(dist_dir):
        shutil.rmtree(dist_dir)
    os.makedirs(build_dir, exist_ok=True)

    target_folder = os.path.join(dist_dir, app_name_safe)

    print(f"\n[1/3] Building {__app_name__} v{__version__} for {sys_os}...")
    if sys_os == "Linux":
        build_source_bundle(target_folder, build_dir)
    else:
        build_frozen_bundle(app_name_safe, sys_os, build_dir)

    if not os.path.isdir(target_folder):
        print(f"\n[-] Expected build output directory is missing: {target_folder}")
        sys.exit(1)

    # 产物名里必须带发布通道（stable / dev）：Worker 以 `{平台}_{通道}_v` 为前缀
    # 列举 R2 对象，两条通道因此互不可见——上传 dev 产物时清理历史版本不会误删
    # 稳定版产物（这也是旧命名 `..._{平台}_v...` 无法承载双通道的根因）。
    channel = release_channel(__version__)
    archive_path = f"{app_name_safe}_{tag}_{channel}_v{__version__}.zip"

    print(f"\n[2/3] Creating archive: {archive_path}...")
    _archive_folder(target_folder, dist_dir, archive_path)

    print("\n[3/3] Cloudflare R2 Operations...")
    try:
        # legacy_prefixes：单通道时代的对象名（`..._{平台}_v{版本}.zip`）不会
        # 被新前缀清理到，留一次迁移清理把它带走；该前缀与"新命名"不可能碰撞
        # （新名字符串里 `_{平台}_` 之后紧接通道名，不是 `v`）。
        object_name = publish_artifact(
            archive_path,
            legacy_prefixes=(f"{app_name_safe}_{tag}_v",),
        )
    except ReleaseUploadError as exc:
        # 必须让流水线变红：静默失败会制造"发版绿色但产物没上传"的假象。
        print(f"\n[-] R2 publish failed: {exc}")
        sys.exit(1)

    if object_name:
        print(f"\n[+] Release published: {object_name}")
    else:
        print(f"[*] R2 upload skipped (no credentials configured). "
              f"Artifact kept at: {archive_path}")


if __name__ == "__main__":
    # 构建脚本自身的进度用 print（CI 日志的可读性），文件级细节走 logger。
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    build_app()