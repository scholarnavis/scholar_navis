import glob
import os
import sys
import shutil
import platform
import subprocess
import re
import zipfile

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from src.core.version import __version__, __app_name__

os.environ["TRANSFORMERS_VERBOSITY"] = "error"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

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

def get_r2_client():
    load_dotenv()
    account_id = os.getenv("R2_ACCOUNT_ID")
    access_key = os.getenv("R2_ACCESS_KEY_ID")
    secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
    if not all([account_id, access_key, secret_key]):
        print("[-] R2 credentials missing. Skipping R2 operations.")
        return None
    return boto3.client(
        service_name="s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto"
    )

def upload_to_r2(s3_client, bucket_name, file_path, object_name):
    print(f"\n[*] Uploading {file_path} to R2 bucket '{bucket_name}'...")
    try:
        s3_client.upload_file(file_path, bucket_name, object_name)
        print(f"[+] Upload complete: {object_name}")
    except ClientError as e:
        print(f"[-] Upload failed: {e}")

def delete_old_r2_versions(s3_client, bucket_name, current_object_name):
    match = re.match(r"^(.*?_v)", current_object_name)
    if not match:
        return
    prefix = match.group(1)
    print(f"\n[*] Scanning for old versions with prefix: '{prefix}'...")
    try:
        response = s3_client.list_objects_v2(Bucket=bucket_name, Prefix=prefix)
        if 'Contents' in response:
            for obj in response['Contents']:
                old_key = obj['Key']
                if old_key != current_object_name:
                    s3_client.delete_object(Bucket=bucket_name, Key=old_key)
                    print(f"[+] Deleted old version: {old_key}")
    except ClientError as e:
        print(f"[-] Failed to delete old versions: {e}")

def build_app():
    sync_pyproject_version()

    sys_os = platform.system()
    if sys_os != "Windows":
        print(f"\n[-] Official packaging for {sys_os} is currently suspended. Please run from source.")
        return

    dist_dir = "dist"
    app_name_safe = __app_name__.replace(" ", "_").lower()
    entry_point = "main.py"
    # Nuitka 默认会将独立打包结果放在 xxx.dist 文件夹中
    nuitka_dist_dir = f"{entry_point.replace('.py', '')}.dist"

    print(f"\n[1/4] Preparing Nuitka Build for {__app_name__} v{__version__} on Windows...")

    if os.path.exists(dist_dir):
        shutil.rmtree(dist_dir)
    os.makedirs(dist_dir, exist_ok=True)

    # Nuitka 打包命令构建
    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",                  # 创建独立运行的文件夹
        "--assume-yes-for-downloads",    # 自动允许下载 ccache 等加速依赖
        "--jobs=MAX",                    # 利用 CPU 所有核心加速编译
        "--lto=no",                      # 关闭 LTO 优化（大幅缩短编译时间）
        f"--output-dir={dist_dir}",      # 指定输出根目录
        "--enable-plugin=pyside6",       # 自动处理 PySide6 依赖
        "--enable-plugin=anti-bloat",    # 排除常见的冗余库
    ]

    # 不显示终端黑框 (如需调试可注释掉这行)
    # cmd.append("--windows-disable-console")

    if os.path.exists("Assets/icon.ico"):
        cmd.append("--windows-icon-from-ico=Assets/icon.ico")

    # 1. 包含数据文件
    cmd.append("--include-data-dir=Assets=Assets")

    # 2. 必须包含的完整包
    packages_to_include = [
        "optimum", "transformers", "onnxruntime", "onnx", "tokenizers",
        "chromadb", "anyio", "docx", "litellm"
    ]
    for pkg in packages_to_include:
        cmd.append(f"--include-package={pkg}")

    # 3. 处理 Metadata (防止 transformers 等报错)
    metadata_packages = [
        "transformers", "tqdm", "regex", "torch", "tiktoken",
        "onnx", "onnxruntime", "optimum"
    ]
    for pkg in metadata_packages:
        cmd.append(f"--include-package-data={pkg}")

    # 4. 处理特定的隐藏导入 (隐式动态加载)
    hidden_imports = ["tiktoken_ext.openai_public", "tiktoken_ext"]
    for hi in hidden_imports:
        cmd.append(f"--include-module={hi}")

    # 5. 排除不需要的模块，防止 Nuitka 陷入深度解析导致的极慢编译
    excludes = [
        "tkinter", "matplotlib", "seaborn", "jupyter", "notebook",
        "IPython", "plotly", "pygame",
        "torchvision", "nvidia", "triton", "torchaudio",
        "PyQt6", "PyQt5"
    ]
    for ex in excludes:
        cmd.append(f"--nofollow-import-to={ex}")

    cmd.append(entry_point)

    print(f"\n[2/4] Executing Nuitka (Packaging PySide6 & ONNXRuntime)...")
    result = subprocess.run(cmd)

    if result.returncode != 0:
        print("\n[-] Nuitka build failed.")
        return

    output_archive_name = f"{app_name_safe}_win_v{__version__}"
    target_folder = os.path.join(dist_dir, nuitka_dist_dir)
    archive_path = f"{output_archive_name}.zip"

    print(f"\n[3/4] Build successful. Creating archive: {archive_path}...")

    # 打包为 ZIP
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zipf:
        for root, dirs, files in os.walk(target_folder):
            for file in files:
                file_path = os.path.join(root, file)
                # 压缩包内以 main.dist 为根目录
                arcname = os.path.relpath(file_path, target_folder)
                zipf.write(file_path, arcname)

    print(f"[+] Packed to {archive_path}")

    print(f"\n[4/4] Cloudflare R2 Operations...")
    if os.getenv("GITHUB_ACTIONS") != "true":
        print("[*] Local environment detected. Skipping R2 upload.")
        return

    s3_client = get_r2_client()
    bucket_name = os.getenv("R2_BUCKET_NAME")

    if s3_client and bucket_name:
        object_name = os.path.basename(archive_path)
        upload_to_r2(s3_client, bucket_name, archive_path, object_name)
        delete_old_r2_versions(s3_client, bucket_name, object_name)
        print(f"\n[+] All GitHub Actions workflows completed successfully!")

if __name__ == "__main__":
    build_app()