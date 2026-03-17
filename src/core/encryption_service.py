import binascii
import os
import platform
import logging
import base64
import keyring
import hashlib
from typing import Optional, Tuple

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.backends import default_backend

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
        except Exception:
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

    def _get_master_key(self) -> bytes:
        """Retrieves or creates a hardware/OS-bound master key via Keyring and DPAPI."""
        try:
            stored_key = keyring.get_password(self.service_name, self.account_name)

            if stored_key:
                try:
                    encrypted_blob = base64.b64decode(stored_key)
                    if SYSTEM == "Windows" and win32crypt:
                        return win32crypt.CryptUnprotectData(encrypted_blob, None, None, None, 0)[1]
                    return encrypted_blob
                except (binascii.Error, Exception) as e:
                    self.logger.warning(f"Stored master key is corrupted or invalid format, resetting: {e}")
                    # 可选：尝试清除物理存储中的损坏数据
                    try:
                        keyring.delete_password(self.service_name, self.account_name)
                    except:
                        pass

            # Generate new key if missing or corrupted
            self.logger.info("Initializing new system-bound master key.")
            new_key = os.urandom(32)

            if SYSTEM == "Windows" and win32crypt:
                # Protect using DPAPI
                final_blob = win32crypt.CryptProtectData(new_key, "ScholarNavis Key", None, None, None, 0)
            else:
                final_blob = new_key

            keyring.set_password(self.service_name, self.account_name, base64.b64encode(final_blob).decode())
            return new_key
        except Exception as e:
            self.logger.error(f"Security context establishment failed: {e}")
            raise RuntimeError(f"Platform-bound security is unreachable: {e}")


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