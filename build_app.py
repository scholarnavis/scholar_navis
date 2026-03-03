import os
import sys
import shutil
import platform
import subprocess
import re  # 新增正则库
from src.version import __version__, __app_name__, __description__, __company__

def sync_pyproject_version():
    """将 src.version.__version__ 同步到 pyproject.toml 中"""
    toml_path = "pyproject.toml"
    if not os.path.exists(toml_path):
        return

    with open(toml_path, "r", encoding="utf-8") as f:
        content = f.read()

    new_content = re.sub(
        r'^version\s*=\s*".*?"',
        f'version = "{__version__}"',
        content,
        count=1,
        flags=re.MULTILINE
    )

    if content != new_content:
        with open(toml_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"[*] Synced pyproject.toml version to {__version__}")


def build_app():
    # 1. 打包前先同步版本号
    sync_pyproject_version()

    sys_os = platform.system()
    win_version = f"{__version__}.0" if len(__version__.split('.')) == 3 else __version__
    dist_dir = "dist"
    build_name = __app_name__.replace(" ", "_").lower()

    print(f"\n[1/3] Preparing Nuitka Build for {__app_name__} v{__version__} on {sys_os}...")

    if os.path.exists(dist_dir):
        shutil.rmtree(dist_dir)

    cmd = [
        "python", "-m", "nuitka", "--standalone",
        "--show-progress", "--show-memory",
        "--enable-plugin=pyside6",
        "--enable-plugin=anti-bloat",
        "--include-package=chromadb",
        "--include-package=onnxruntime",
        "--include-package=tokenizers",
        "--include-package=optimum",
        "--output-dir=dist",
        "--include-data-dir=assets=assets",
    ]

    # 根据不同系统追加专属参数
    if sys_os == "Windows":
        cmd.extend([
            "--windows-console-mode=disable",
            "--windows-icon-from-ico=resources/icon.ico",
            f"--company-name={__company__}",
            f"--product-name={__app_name__}",
            f"--file-description={__description__}",
            f"--file-version={win_version}",
            f"--product-version={win_version}",
            f"--output-filename={__app_name__}"
        ])
        output_archive = f"{build_name}_win_v{__version__}"
        archive_format = "zip"

    elif sys_os == "Darwin":  # macOS
        cmd.extend([
            "--macos-create-app-bundle",
            "--macos-app-icon=resources/icon.icns",
            "--macos-app-name=ScholarNavis",
        ])
        output_archive = f"{build_name}_mac_v{__version__}"
        archive_format = "gztar"

    elif sys_os == "Linux":
        cmd.extend([
            "--linux-icon=resources/icon.png",
        ])
        output_archive = f"{build_name}_linux_v{__version__}"
        archive_format = "gztar"
    else:
        print(f"Unsupported OS: {sys_os}")
        return

    print(f"\n[2/3] Executing Nuitka (This may take a while)...")
    result = subprocess.run(cmd)

    if result.returncode == 0:
        print(f"\n[3/3] Build successful. Creating archive: {output_archive}.{archive_format}...")

        # 寻找生成的构建文件夹
        target_folder = ""
        if sys_os == "Windows":
            target_folder = os.path.join(dist_dir, "main.dist")
        elif sys_os == "Darwin":
            target_folder = os.path.join(dist_dir, "ScholarNavis.app")
        else:
            target_folder = os.path.join(dist_dir, "main.dist")

        if os.path.exists(target_folder):
            shutil.make_archive(
                base_name=output_archive,
                format=archive_format,
                root_dir=dist_dir if sys_os == "Darwin" else target_folder
            )
            print(f"All done! Packed to {output_archive}.{archive_format}")
        else:
            print("Build completed, but target folder not found for zipping.")
    else:
        print("\nBuild failed.")


if __name__ == "__main__":
    build_app()