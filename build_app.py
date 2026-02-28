import os
import sys
import shutil
import subprocess
from datetime import datetime
from src.version import __version__, __app_name__, __description__, __company__


def build_windows():
    if sys.platform != "win32":
        print("❌ Error: This script must be run on Windows.")
        return

    win_version = f"{__version__}.0" if len(__version__.split('.')) == 3 else __version__
    dist_dir = "dist"
    build_name = __app_name__.replace(" ", "_").lower()
    output_zip = f"{build_name}_ver.{__version__}.zip"

    # 1. 清理旧构建
    if os.path.exists(dist_dir):
        shutil.rmtree(dist_dir)

    # 2. Nuitka 指令
    cmd = [
        "python", "-m", "nuitka", "--standalone",
        "--show-progress", "--show-memory",
        "--enable-plugin=pyside6",
        "--include-package=chromadb",
        "--include-package=sentence_transformers",
        "--include-package=posthog",
        "--include-package=onnxruntime",
        "--include-package=tokenizers",
        "--output-dir=dist",
        "--include-data-dir=Assets=Assets",
        "--include-data-dir=assets=assets",
        "--windows-icon-from-ico=resources/icon.ico",
        "--windows-console-mode=disable",
        f"--company-name={__company__}",
        f"--product-name={__app_name__}",
        f"--file-description={__description__}",
        f"--file-version={win_version}",
        f"--product-version={win_version}",
        f"--output-filename={__app_name__}",
        "main.py"
    ]

    print(f"\n🚀 [1/2] Starting Nuitka Build for {__app_name__} v{__version__}...")
    result = subprocess.run(cmd)

    if result.returncode == 0:
        print(f"\n✅ [2/2] Build successful. Creating archive: {output_zip}...")

        # 找到生成的可执行目录 (Nuitka 通常会生成 main.dist)
        target_folder = os.path.join(dist_dir, "main.dist")

        # 压缩 target_folder 到 output_zip
        # 使用 shutil.make_archive，它会自动处理
        shutil.make_archive(
            base_name=output_zip.replace(".zip", ""),
            format='zip',
            root_dir=target_folder
        )

        # 移动 zip 到 dist 根目录方便上传
        final_zip_path = os.path.join(dist_dir, output_zip)
        if os.path.exists(output_zip):
            shutil.move(output_zip, final_zip_path)

        print(f"\n🎉 Done! Package ready at: {final_zip_path}")
    else:
        print("\n❌ Build failed.")


if __name__ == "__main__":
    build_windows()