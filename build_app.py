import os
import sys
import shutil
import platform
import subprocess
from src.version import __version__, __app_name__, __description__, __company__


def build_app():
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
            print("⚠Build completed, but target folder not found for zipping.")
    else:
        print("\nBuild failed.")


if __name__ == "__main__":
    build_app()