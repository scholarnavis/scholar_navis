import glob
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile

from build_support.notices import stage_notices
from build_support.r2_release import ReleaseUploadError, publish_artifact
from src.core.version import __app_name__, __github__, __version__

os.environ["TRANSFORMERS_VERBOSITY"] = "error"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"


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

# R2 发布逻辑抽在 build_support/r2_release.py（独立模块，便于注入客户端做测试）。
# 这里只留调用点，见 build_app() 的 [4/4] 段。

def build_app():
    sync_pyproject_version()

    sys_os = platform.system()
    tag = platform_tag()
    is_windows = sys_os == "Windows"

    dist_dir = "dist"
    build_dir = "build"
    app_name_safe = __app_name__.replace(" ", "_").lower()
    entry_point = "main.py"

    print(f"\n[1/4] Preparing PyInstaller Build for {__app_name__} v{__version__} on {sys_os}...")

    if os.path.exists(dist_dir):
        shutil.rmtree(dist_dir)

    os.makedirs(build_dir, exist_ok=True)

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

    cmd.append(entry_point)

    print("\n[2/4] Executing PyInstaller (Packaging PySide6 & ONNXRuntime)...")
    # check=False：失败由下面的 returncode 判定，以便区分"打包失败"与"异常退出"。
    result = subprocess.run(cmd, check=False)

    # 无论打包成功失败，清理掉临时生成的 Hook 文件
    if os.path.exists(hook_file):
        os.remove(hook_file)

    if result.returncode != 0:
        print("\n[-] PyInstaller build failed.")
        return

    output_archive_name = f"{app_name_safe}_{tag}_v{__version__}"
    target_folder = os.path.join(dist_dir, app_name_safe)
    archive_path = f"{output_archive_name}.zip"

    print(f"\n[3/4] Build successful. Creating archive: {archive_path}...")

    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zipf:
        for root, dirs, files in os.walk(target_folder):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, dist_dir)
                zipf.write(file_path, arcname)

    print(f"[+] Packed to {archive_path}")

    print("\n[4/4] Cloudflare R2 Operations...")
    try:
        object_name = publish_artifact(archive_path)
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
    build_app()