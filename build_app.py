import os
import sys
import subprocess
from src.version import __version__, __app_name__, __description__, __company__


def build(target_os):
    current_os = sys.platform

    # Nuitka 跨平台编译拦截与警告
    if target_os == "windows" and current_os != "win32":
        print("⚠️ 警告: Nuitka 无法在非 Windows 系统上直接编译 Windows 版本。跳过该任务。")
        return
    if target_os == "mac" and current_os != "darwin":
        print("⚠️ 警告: Nuitka 无法在非 macOS 系统上直接编译 Mac 版本。跳过该任务。")
        return
    if target_os == "linux" and not current_os.startswith("linux"):
        print("⚠️ 警告: Nuitka 无法在非 Linux 系统上直接编译 Linux 版本。跳过该任务。")
        return

    win_version = f"{__version__}.0" if len(__version__.split('.')) == 3 else __version__

    # 基础通用参数
    cmd = [
        "python", "-m", "nuitka", "--standalone",
        "--show-progress", "--show-memory",
        "--enable-plugin=pyside6",
        "--include-package=chromadb",
        "--include-package=sentence_transformers",
        "--include-package=posthog",
        "--include-package=onnxruntime",
        "--include-package=tokenizers",
        "--include-module=src.plugins.bio_ncbi_server",
        "--nofollow-import-to=plugins_ext",
        "--output-dir=dist",
        "--noinclude-pytest-mode=nofollow",
        "--noinclude-setuptools-mode=nofollow",
        "--include-data-dir=Assets=Assets",
        "--include-data-dir=assets=assets",
        f"--output-filename={__app_name__}",
    ]

    # 平台专属参数
    if target_os == "windows":
        cmd.extend([
            "--mingw64",
            "--windows-icon-from-ico=resources/icon.ico",
            "--windows-console-mode=disable",
            f"--company-name={__company__}",
            f"--product-name={__app_name__}",
            f"--file-description={__description__}",
            f"--file-version={win_version}",
        ])
    elif target_os == "mac":
        cmd.extend([
            "--macos-create-app-bundle",
            "--macos-app-icon=resources/icon.icns",  # Mac 需要 icns 格式图标
            f"--macos-app-name={__app_name__}",
            f"--macos-app-version={__version__}",
        ])
    elif target_os == "linux":
        cmd.extend([
            "--linux-icon=resources/icon.png",  # Linux 一般使用 png
        ])

    cmd.append("main.py")

    print(f"\n🚀 开始构建 {__app_name__} for {target_os.upper()} (v{win_version})...")
    subprocess.run(cmd)


if __name__ == "__main__":
    print("=" * 40)
    print(" Scholar Navis 自动化打包工具")
    print("=" * 40)
    print("请选择要打包的平台 (可多选，用逗号分隔，例如 1,3):")
    print("  1. Windows")
    print("  2. macOS")
    print("  3. Linux")
    print("  4. 全部 (All)")

    choice = input("\n请输入选项: ").strip()

    targets = []
    if "4" in choice or "all" in choice.lower():
        targets = ["windows", "mac", "linux"]
    else:
        if "1" in choice: targets.append("windows")
        if "2" in choice: targets.append("mac")
        if "3" in choice: targets.append("linux")

    if not targets:
        print("未选择任何平台，退出打包。")
        sys.exit(0)

    for t in targets:
        build(t)