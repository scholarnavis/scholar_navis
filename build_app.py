import multiprocessing
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
    # 仅作保留，供后续可能的手动管理需求
    pass


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

    cpu_count = multiprocessing.cpu_count()
    jobs = max(1, cpu_count - 1)

    cmd = [
        sys.executable, "-m", "nuitka", "--standalone",
        f"--jobs={jobs}",
        "--show-progress", "--show-memory",
        "--enable-plugin=pyside6",
        "--enable-plugin=anti-bloat",
        "--include-package=chromadb",
        "--include-package=onnxruntime",
        "--include-package=optimum",
        "--output-dir=dist",
        "--include-data-dir=Assets=Assets",

        "--nofollow-import-to=chromadb.telemetry",
        "--nofollow-import-to=chromadb.test",
        "--nofollow-import-to=chromadb.migrations",
        "--nofollow-import-to=chromadb.server",
        "--nofollow-import-to=duckdb",
        "--nofollow-import-to=clickhouse_connect",

        "--nofollow-import-to=mcp.testing",

        "--nofollow-import-to=pytest",
        "--nofollow-import-to=unittest",
        "--nofollow-import-to=nose",

        "--nofollow-import-to=tkinter",
        "--nofollow-import-to=PyQt5",
        "--nofollow-import-to=PyQt6",
        "--nofollow-import-to=wx",

        "--nofollow-import-to=IPython",
        "--nofollow-import-to=jupyter",
        "--nofollow-import-to=notebook",
        "--nofollow-import-to=pydoc",

        "--nofollow-import-to=Bio.Graphics",

        "--nofollow-import-to=matplotlib",
        "--nofollow-import-to=seaborn",
        "--nofollow-import-to=plotly",

        "--nofollow-import-to=tensorboard",

        "--nofollow-import-to=setuptools",
        "--nofollow-import-to=pip",
        "--nofollow-import-to=wheel",
    ]

    win_icon = "Assets/icon.ico"
    mac_icon = "Assets/icon.icns"
    linux_icon = "Assets/icon.png"

    if sys_os == "Windows":
        cmd.extend([
            "--windows-console-mode=disable",
            f"--company-name={__company__}",
            f"--product-name={__app_name__}",
            f"--file-description={__description__}",
            f"--file-version={win_version}",
            f"--product-version={win_version}",
            f"--output-filename={__app_name__}.exe"
        ])
        if os.path.exists(win_icon):
            cmd.append(f"--windows-icon-from-ico={win_icon}")
        else:
            print(f"[*] Warning: Windows icon '{win_icon}' not found. Skipping icon packaging.")

        output_archive = f"{build_name}_win_v{__version__}"
        archive_format = "zip"

    elif sys_os == "Darwin":
        cmd.extend([
            "--macos-create-app-bundle",
            f"--macos-app-name={__app_name__}",
        ])
        if os.path.exists(mac_icon):
            cmd.append(f"--macos-app-icon={mac_icon}")
        else:
            print(f"[*] Warning: macOS icon '{mac_icon}' not found. Skipping icon packaging.")

        output_archive = f"{build_name}_mac_v{__version__}"
        archive_format = "gztar"

    elif sys_os == "Linux":
        cmd.extend([
            f"--output-filename={__app_name__}"
        ])
        if os.path.exists(linux_icon):
            cmd.append(f"--linux-icon={linux_icon}")
        else:
            print(f"[*] Warning: Linux icon '{linux_icon}' not found. Skipping icon packaging.")

        output_archive = f"{build_name}_linux_v{__version__}"
        archive_format = "gztar"
    else:
        print(f"[-] Unsupported OS: {sys_os}")
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
            print(f"[+] All done! Packed to {archive_path}")
        else:
            print(f"[-] Build completed, but target folder '{target_folder}' not found for zipping.")
    else:
        print("\n[-] Build failed.")
        return

    # 关键修复 3: 云端与本地逻辑隔离
    print(f"\n[4/4] Cloudflare R2 Operations...")
    is_ci = os.getenv("GITHUB_ACTIONS") == "true"

    if not is_ci:
        print("[*] Local environment detected. Skipping Cloudflare R2 upload.")
        return

    if final_archive_path:
        s3_client = get_r2_client()
        load_dotenv()
        bucket_name = os.getenv("R2_BUCKET_NAME")

        if s3_client and bucket_name:
            print("[*] GitHub Actions environment detected. Auto-uploading...")
            object_name = os.path.basename(final_archive_path)

            upload_to_r2(s3_client, bucket_name, final_archive_path, object_name)
            delete_old_r2_versions(s3_client, bucket_name, object_name)

            print(f"\n[+] All R2 operations completed successfully!")
        else:
            print("[-] Skipping upload. Please verify R2 credentials and bucket name.")


if __name__ == "__main__":
    build_app()