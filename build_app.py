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
    build_dir = "build"
    app_name_safe = __app_name__.replace(" ", "_").lower()
    entry_point = "main.py"

    print(f"\n[1/4] Preparing PyInstaller Build for {__app_name__} v{__version__} on Windows...")

    for d in [dist_dir, build_dir]:
        if os.path.exists(d):
            shutil.rmtree(d)


    hook_file = "torch_runtime_hook.py"
    with open(hook_file, "w", encoding="utf-8") as f:
        f.write("import torch\nimport torch.autograd\n")
    print("[*] Generated PyTorch Runtime Hook to prevent circular imports.")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--windowed",
        f"--name={app_name_safe}",
        f"--runtime-hook={hook_file}",  # 注入 Hook
    ]


    packages_to_collect = [
        "optimum", "transformers", "onnxruntime", "tokenizers",
        "chromadb"
    ]

    data_to_collect = ["docx","litellm"]

    hidden_imports = [
        "torch",
        "torch.autograd",
        "safetensors",
        "huggingface_hub"
    ]

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

    cmd.append("--add-data=Assets;Assets")

    excludes = [
        "tkinter", "matplotlib", "seaborn", "jupyter", "notebook",
        "IPython", "plotly", "pygame",
        "torchvision", "nvidia", "triton", "torchaudio",
        "PyQt6", "PyQt5"
    ]
    for ex in excludes:
        cmd.append(f"--exclude-module={ex}")

    if os.path.exists("Assets/icon.ico"):
        cmd.append("--icon=Assets/icon.ico")

    cmd.append(entry_point)

    print(f"\n[2/4] Executing PyInstaller (Packaging PySide6 & ONNXRuntime)...")
    result = subprocess.run(cmd)

    # 无论打包成功失败，清理掉临时生成的 Hook 文件
    if os.path.exists(hook_file):
        os.remove(hook_file)

    if result.returncode != 0:
        print("\n[-] PyInstaller build failed.")
        return

    output_archive_name = f"{app_name_safe}_win_v{__version__}"
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