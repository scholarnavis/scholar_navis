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

def sync_pyproject_version():
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

def manage_r2_files(s3_client, bucket_name):
    """List files in R2 and provide an interactive deletion prompt."""
    while True:
        print(f"\n--- Current Files in R2 Bucket: {bucket_name} ---")
        try:
            response = s3_client.list_objects_v2(Bucket=bucket_name)
            if 'Contents' not in response:
                print("Bucket is currently empty.")
                return

            objects = response['Contents']
            for idx, obj in enumerate(objects):
                size_mb = obj['Size'] / (1024 * 1024)
                print(f"[{idx}] {obj['Key']} ({size_mb:.2f} MB)")

            print("------------------------------------------------")
            choice = input("Enter file index to DELETE, or 'c' to continue to upload: ").strip()

            if choice.lower() == 'c':
                break

            if choice.isdigit() and 0 <= int(choice) < len(objects):
                target_key = objects[int(choice)]['Key']
                confirm = input(f"Are you sure you want to delete '{target_key}'? (y/n): ").strip()
                if confirm.lower() == 'y':
                    s3_client.delete_object(Bucket=bucket_name, Key=target_key)
                    print(f"[+] Successfully deleted {target_key}")
            else:
                print("[-] Invalid input. Please enter a valid index or 'c'.")

        except ClientError as e:
            print(f"[-] R2 Error: {e}")
            break

def upload_to_r2(s3_client, bucket_name, file_path, object_name):
    """Upload the build archive to Cloudflare R2."""
    print(f"\n[*] Uploading {file_path} to R2 bucket '{bucket_name}'...")
    try:
        s3_client.upload_file(file_path, bucket_name, object_name)
        print(f"[+] Upload complete: {object_name}")
    except ClientError as e:
        print(f"[-] Upload failed: {e}")



def delete_old_r2_versions(s3_client, bucket_name, current_object_name):
    """自动查找并删除 R2 桶中相同平台的旧版本文件"""
    # 通过正则提取前缀，例如从 'scholar_navis_win_v1.0.0.zip' 提取 'scholar_navis_win_v'
    match = re.match(r"^(.*?_v)", current_object_name)
    if not match:
        print(f"[-] Could not parse prefix from {current_object_name}. Skipping cleanup.")
        return

    prefix = match.group(1)
    print(f"\n[*] Scanning for old versions with prefix: '{prefix}'...")

    try:
        response = s3_client.list_objects_v2(Bucket=bucket_name, Prefix=prefix)
        if 'Contents' in response:
            deleted_count = 0
            for obj in response['Contents']:
                old_key = obj['Key']
                if old_key != current_object_name:
                    s3_client.delete_object(Bucket=bucket_name, Key=old_key)
                    print(f"[+] Deleted old version: {old_key}")
                    deleted_count += 1

            if deleted_count == 0:
                print("[*] No older versions found to delete.")
        else:
            print("[*] No older versions found to delete.")
    except ClientError as e:
        print(f"[-] Failed to scan or delete old versions: {e}")



def build_app():
    sync_pyproject_version()

    sys_os = platform.system()
    win_version = f"{__version__}.0" if len(__version__.split('.')) == 3 else __version__
    dist_dir = "dist"
    build_name = __app_name__.replace(" ", "_").lower()
    entry_point = "main.py"

    print(f"\n[1/4] Preparing Nuitka Build for {__app_name__} v{__version__} on {sys_os}...")

    if os.path.exists(dist_dir):
        shutil.rmtree(dist_dir)

    cmd = [
        "python", "-m", "nuitka", "--standalone",
        "--show-progress", "--show-memory",
        "--enable-plugin=pyside6",
        "--enable-plugin=anti-bloat",
        "--include-package=chromadb",
        "--include-package=onnxruntime",
        "--include-package=optimum",
        "--output-dir=dist",
        "--include-data-dir=assets=Assets",
    ]

    if sys_os == "Windows":
        cmd.extend([
            "--windows-console-mode=disable",
            "--windows-icon-from-ico=Assets/icon.ico",
            f"--company-name={__company__}",
            f"--product-name={__app_name__}",
            f"--file-description={__description__}",
            f"--file-version={win_version}",
            f"--product-version={win_version}",
            f"--output-filename={__app_name__}.exe"
        ])
        output_archive = f"{build_name}_win_v{__version__}"
        archive_format = "zip"

    elif sys_os == "Darwin":
        cmd.extend([
            "--macos-create-app-bundle",
            "--macos-app-icon=Assets/icon.icns",
            f"--macos-app-name={__app_name__}",
        ])
        output_archive = f"{build_name}_mac_v{__version__}"
        archive_format = "gztar"

    elif sys_os == "Linux":
        cmd.extend([
            "--linux-icon=Assets/icon.png",
            f"--output-filename={__app_name__}"
        ])
        output_archive = f"{build_name}_linux_v{__version__}"
        archive_format = "gztar"
    else:
        print(f"Unsupported OS: {sys_os}")
        return

    cmd.append(entry_point)

    print(f"\n[2/4] Executing Nuitka (This may take a while)...")
    result = subprocess.run(cmd)

    final_archive_path = None

    if result.returncode == 0:
        print(f"\n[3/4] Build successful. Creating archive: {output_archive}.{archive_format}...")

        target_folder = ""
        if sys_os == "Windows" or sys_os == "Linux":
            target_folder = os.path.join(dist_dir, f"{entry_point.split('.')[0]}.dist")
        elif sys_os == "Darwin":
            target_folder = os.path.join(dist_dir, f"{__app_name__}.app")

        if os.path.exists(target_folder):
            archive_path = shutil.make_archive(
                base_name=output_archive,
                format=archive_format,
                root_dir=dist_dir if sys_os == "Darwin" else target_folder
            )
            final_archive_path = archive_path
            print(f"All done! Packed to {archive_path}")
        else:
            print(f"Build completed, but target folder '{target_folder}' not found for zipping.")
    else:
        print("\nBuild failed.")
        return

    # Phase 4: Cloudflare R2 Integration (适配 CI 并自动清理旧版本)
    print(f"\n[4/4] Cloudflare R2 Operations...")
    if final_archive_path:
        s3_client = get_r2_client()
        load_dotenv()
        bucket_name = os.getenv("R2_BUCKET_NAME")

        if s3_client and bucket_name:
            is_ci = os.getenv("GITHUB_ACTIONS") == "true"

            if not is_ci:
                manage_r2_files(s3_client, bucket_name)
                upload_confirm = input(f"Do you want to upload {final_archive_path} to R2? (y/n): ").strip()
            else:
                print("[*] GitHub Actions environment detected. Skipping prompts and auto-uploading...")
                upload_confirm = 'y'

            if upload_confirm.lower() == 'y':
                object_name = os.path.basename(final_archive_path)

                # 1. 先上传新版本
                upload_to_r2(s3_client, bucket_name, final_archive_path, object_name)

                delete_old_r2_versions(s3_client, bucket_name, object_name)

                print(f"\n[+] All R2 operations completed successfully!")
        else:
            print("[-] Skipping upload. Please verify R2_BUCKET_NAME in .env or GitHub Secrets.")

if __name__ == "__main__":
    build_app()