import os
import platform
import logging
import base64
import subprocess
import keyring
from typing import Optional, Tuple

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.backends import default_backend

from src.core import BASE_DIR

SYSTEM = platform.system()
if SYSTEM == "Windows":
    try:
        import win32crypt  # Used for hardware/user-bound DPAPI encryption
    except ImportError:
        win32crypt = None
else:
    win32crypt = None


class SystemEncryptionService:
    def __init__(self, service_name: str = "ScholarNavis"):
        self.logger = logging.getLogger("EncryptionService")
        self.service_name = service_name
        self.account_name = "system_bound_master_key"
        self._master_fernet: Optional[Fernet] = None

    def _get_machine_id(self) -> str:
        """Generates a hardware-specific identifier for the current machine."""
        try:
            if SYSTEM == "Windows":
                import subprocess
                cmd = 'wmic csproduct get uuid'
                uuid = subprocess.check_output(cmd, shell=True).decode().split('\n')[1].strip()
                return uuid
            elif SYSTEM == "Linux":
                with open("/etc/machine-id", "r") as f:
                    return f.read().strip()
            elif SYSTEM == "Darwin":
                import subprocess
                cmd = "ioreg -rd1 -c IOPlatformExpertDevice | grep -E '(UUID)'"
                uuid = subprocess.check_output(cmd, shell=True).decode().split('"')[-2]
                return uuid
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return platform.node()
        return "fallback-id"

    def _get_master_fernet(self) -> Fernet:
        """Lazy initialization of the Fernet instance using the system-bound key."""
        if self._master_fernet is None:
            raw_key = self._get_master_key()
            # Derive a standard 32-byte Fernet key from the system-bound raw key
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=self._get_machine_id().encode(),
                iterations=1000,
                backend=default_backend()
            )
            derived_key = base64.urlsafe_b64encode(kdf.derive(raw_key))
            self._master_fernet = Fernet(derived_key)
        return self._master_fernet

    def encrypt(self, data: str) -> bytes:
        """Encrypts a string using the hardware-bound master key."""
        return self._get_master_fernet().encrypt(data.encode())

    def decrypt(self, encrypted_data: bytes) -> str:
        """Decrypts data using the hardware-bound master key."""
        return self._get_master_fernet().decrypt(encrypted_data).decode()

    # ------------------------------------------------------------------ #
    #  Master key storage: system keyring (primary) + local file (fallback)
    # ------------------------------------------------------------------ #

    #: 覆盖回退密钥文件位置（自定义部署/测试用）
    FALLBACK_KEY_ENV = "SCHOLAR_NAVIS_FALLBACK_KEY_FILE"
    #: 置为 1/true 时禁止把密钥镜像到本地文件（仅依赖系统 keyring）
    DISABLE_MIRROR_ENV = "SCHOLAR_NAVIS_DISABLE_KEY_MIRROR"

    def _fallback_key_path(self) -> str:
        custom = os.environ.get(self.FALLBACK_KEY_ENV, "").strip()
        if custom:
            return os.path.expanduser(custom)
        return os.path.join(BASE_DIR, "config", ".secret_fallback.key")

    @staticmethod
    def _protect(raw: bytes) -> bytes:
        """Windows 上用 DPAPI 再包一层；其他平台原样返回。"""
        if SYSTEM == "Windows" and win32crypt:
            return win32crypt.CryptProtectData(raw, "ScholarNavis Key", None, None, None, 0)
        return raw

    @staticmethod
    def _unprotect(blob: bytes) -> bytes:
        if SYSTEM == "Windows" and win32crypt:
            return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]
        return blob

    def _keyring_read(self) -> Optional[bytes]:
        """读取 keyring 中的主密钥；不可用或已损坏时返回 None（不抛错）。"""
        try:
            stored = keyring.get_password(self.service_name, self.account_name)
        except Exception as e:
            self.logger.warning(f"Keyring unavailable (read): {e}")
            return None

        if not stored:
            return None

        try:
            return self._unprotect(base64.b64decode(stored))
        except Exception as e:
            self.logger.warning(f"Stored master key is corrupted, discarding: {e}")
            try:
                keyring.delete_password(self.service_name, self.account_name)
            except Exception:  # 删除失败不影响后续重建
                pass
            return None

    def _keyring_write(self, raw: bytes) -> bool:
        """把主密钥写入 keyring；失败返回 False（调用方转用本地密钥文件）。"""
        try:
            keyring.set_password(
                self.service_name, self.account_name,
                base64.b64encode(self._protect(raw)).decode())
            return True
        except Exception as e:
            self.logger.warning(f"Keyring unavailable (write): {e}")
            return False

    def _read_fallback_key(self) -> Optional[bytes]:
        path = self._fallback_key_path()
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except FileNotFoundError:
            return None
        except OSError as e:
            self.logger.warning(f"Cannot read fallback key file '{path}': {e}")
            return None

        try:
            return self._unprotect(base64.b64decode(blob.strip()))
        except Exception as e:
            self.logger.error(f"Fallback key file is corrupted ('{path}'): {e}")
            return None

    def _write_fallback_key(self, raw: bytes) -> bool:
        """原子写入本地密钥文件（权限 0o600）。"""
        path = self._fallback_key_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "wb") as f:
                f.write(base64.b64encode(self._protect(raw)))
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
            return True
        except OSError as e:
            self.logger.error(f"Failed to persist fallback key at '{path}': {e}")
            return False

    def _mirror_enabled(self) -> bool:
        return os.environ.get(self.DISABLE_MIRROR_ENV, "").strip().lower() not in {
            "1", "true", "yes", "on"}

    def _get_master_key(self) -> bytes:
        """获取或创建系统绑定的主密钥。

        存储策略（按优先级）：

        1. 系统 keyring（Windows 凭据管理器 / macOS Keychain / Linux Secret Service）；
        2. 本地密钥文件 ``config/.secret_fallback.key``（0o600，Windows 上再经 DPAPI 保护）。

        第 2 条是本应用在 Linux 上的关键保障：无桌面会话（SSH、容器、未配置
        gnome-keyring/kwallet 的 KDE）时 keyring 必然不可用，此前会直接抛
        RuntimeError 导致应用启动失败。现在降级到本地密钥文件，配置仍可正常
        解密；keyring 可用时同时镜像一份，避免"桌面会话写、终端会话读不到"
        而丢失配置。
        """
        key = self._keyring_read()
        if key:
            path = self._fallback_key_path()
            if self._mirror_enabled() and not os.path.exists(path):
                # 仅补齐缺失的镜像，绝不覆盖已有文件
                self._write_fallback_key(key)
            return key

        key = self._read_fallback_key()
        if key:
            self.logger.warning(
                f"System keyring unavailable; using local fallback key file "
                f"'{self._fallback_key_path()}'. Configuration stays readable.")
            self._keyring_write(key)  # 尝试回填 keyring，失败无妨
            return key

        self.logger.info("Initializing new system-bound master key.")
        new_key = os.urandom(32)
        stored = self._keyring_write(new_key)
        mirrored = self._write_fallback_key(new_key) if self._mirror_enabled() else False

        if not stored and not mirrored:
            raise RuntimeError(
                "Platform-bound security is unreachable: neither the system keyring "
                f"nor a local key file at '{self._fallback_key_path()}' could be written.")
        return new_key


    def derive_key_from_password(self, password: str, salt: bytes = None) -> Tuple[bytes, bytes]:
        """Derives a high-entropy key with 600,000 iterations"""
        if salt is None:
            salt = os.urandom(16)

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600000, # 提升至现代安全标准
            backend=default_backend()
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        return key, salt

    @staticmethod
    def identify_data_format(data_bundle: dict) -> str:
        """
        Detects if the bundle is 'encrypted' or 'plain_text'.
        Returns: 'encrypted' | 'plain'
        """
        # 如果 JSON 中包含 salt 和 payload 字段，则视为加密格式
        if "payload" in data_bundle and "salt" in data_bundle:
            return "encrypted"
        return "plain"

    def decrypt_bundle(self, bundle: dict, password: str) -> dict:
        """Helper to decrypt the payload and return the original dictionary."""
        import json
        encrypted_data = base64.b64decode(bundle["payload"])
        salt = base64.b64decode(bundle["salt"])

        decrypted_json = self.import_data(encrypted_data, password, salt)
        return json.loads(decrypted_json)



    def export_data(self, data: str, password: str) -> Tuple[bytes, bytes]:
        """Encrypts data for export using a portable password-based key."""
        key, salt = self.derive_key_from_password(password)
        f = Fernet(key)
        return f.encrypt(data.encode()), salt

    def import_data(self, encrypted_data: bytes, password: str, salt: bytes) -> str:
        """Decrypts imported data using a portable password-based key."""
        key, _ = self.derive_key_from_password(password, salt)
        f = Fernet(key)
        return f.decrypt(encrypted_data).decode()