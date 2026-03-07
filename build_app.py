import os
import sys
import shutil
import platform
import subprocess
import re
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from src.version import __version__, __app_name__, __description__, __company__

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
    dist_dir = "dist"
    build_dir = "build"
    app_name_safe = __app_name__.replace(" ", "_").lower()
    entry_point = "main.py"

    print(f"\n[1/4] Preparing PyInstaller Build for {__app_name__} v{__version__} on {sys_os}...")

    for d in [dist_dir, build_dir]:
        if os.path.exists(d):
            shutil.rmtree(d)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--windowed",
        f"--name={app_name_safe}",
    ]

    packages_to_collect = [
        "optimum", "transformers", "onnxruntime", "tokenizers",
        "sklearn", "scipy", "chardet",

        "chromadb",
        "pydantic",

        "langchain_text_splitters",
        "pymupdf4llm",
        "docx",
        "markdown"
    ]

    for pkg in packages_to_collect:
        cmd.extend(["--collect-all", pkg])

    cmd.extend(["--copy-metadata", "transformers"])
    cmd.extend(["--copy-metadata", "tqdm"])

    path_sep = ";" if sys_os == "Windows" else ":"
    cmd.append(f"--add-data=Assets{path_sep}Assets")

    excludes = [
        "tkinter", "matplotlib", "seaborn", "jupyter", "notebook",
        "IPython", "plotly", "pygame", "setuptools", "wheel",
        "torchvision"
    ]
    for ex in excludes:
        cmd.append(f"--exclude-module={ex}")

    # 图标处理
    if sys_os == "Windows" and os.path.exists("Assets/icon.ico"):
        cmd.append("--icon=Assets/icon.ico")
    elif sys_os == "Linux" and os.path.exists("Assets/icon.png"):
        cmd.append("--icon=Assets/icon.png")

    cmd.append(entry_point)

    print(f"\n[2/4] Executing PyInstaller (Packaging PySide6 & ONNXRuntime)...")
    result = subprocess.run(cmd)

    if result.returncode != 0:
        print("\n[-] PyInstaller build failed.")
        return

    # 压缩打包
    output_archive_name = f"{app_name_safe}_{sys_os.lower()}_v{__version__}"
    archive_format = "zip" if sys_os == "Windows" else "gztar"
    target_folder = os.path.join(dist_dir, app_name_safe)

    print(f"\n[3/4] Build successful. Creating archive: {output_archive_name}.{archive_format}...")

    archive_path = shutil.make_archive(
        base_name=output_archive_name,
        format=archive_format,
        root_dir=dist_dir,
        base_dir=app_name_safe
    )
    print(f"[+] Packed to {archive_path}")

    # R2 云端上传逻辑
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