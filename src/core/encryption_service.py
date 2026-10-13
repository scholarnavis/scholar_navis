"""配置相关加解密：生产用途只剩"口令型配置导出"，其余都是**只读**的历史兼容层。

生产用途（保留）
    口令型导出 / 导入：:meth:`SystemEncryptionService.export_data` /
    :meth:`SystemEncryptionService.import_data` /
    :meth:`SystemEncryptionService.derive_key_from_password`。导出文件会离开本机，
    必须保持加密。

LEGACY-CIPHERTEXT 兼容层（最早 v2.2 删除）
    只读历史密文，**不再产生任何新密文**。旧版曾用"机器标识当 salt"（以及过渡期的
    固定 salt）加密 ``settings.json`` / ``llm_config.json`` / ``mcp_servers.json`` 与
    技能源码，并为此托管了一把主密钥。写入侧现已全部改为明文（配置见
    :mod:`src.core.secrets_store`，技能见 :mod:`src.core.skill_store`）。

    删除条件：现网已不存在这类密文文件（迁移后配置为明文 JSON + ``secrets.json``，
    技能为明文 ``.py``），或用户已明确放弃这些旧数据。届时连同以下一并清理：

    * 本模块：:data:`KEY_DERIVATION_SALT`、:data:`DERIVATION_ITERATIONS`、两个平台
      标识函数、:meth:`SystemEncryptionService._get_machine_id`、两套派生、
      :meth:`SystemEncryptionService.decrypt_with_mode`、
      :meth:`SystemEncryptionService.decrypt`，以及主密钥托管（keyring / 回退密钥文件
      / Windows DPAPI 包装）——即除"生产用途"三个方法外的全部内容；
    * ``src/core/config_manager.py``：``encryption_service`` 实例、
      ``load_json`` 的密文回退分支、``_read_legacy_encrypted_json``、``_backup_file``、
      ``_migrate_legacy_encrypted_config``、``_migrate_legacy_skill_files``；
    * ``src/core/skill_store.py``：``LEGACY_SUFFIX`` 与 ``read_skill_source`` 的
      ``.enc`` 分支。

    全仓搜索 ``LEGACY-CIPHERTEXT`` 即可定位所有待删点。
"""

import os
import platform
import logging
import base64
import re
import subprocess
import keyring
from typing import Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.backends import default_backend

from src.core import BASE_DIR
from src.core.platform_env import (
    PLATFORM_LINUX, PLATFORM_MACOS, PLATFORM_WINDOWS, no_window_flags, os_family,
)

#: 平台标识统一取自 ``platform_env``，本模块不再自行比较 ``platform.system()``
SYSTEM = os_family()
IS_WINDOWS = SYSTEM == PLATFORM_WINDOWS

if IS_WINDOWS:
    try:
        import win32crypt  # Used for hardware/user-bound DPAPI encryption
    except ImportError:
        win32crypt = None
else:
    win32crypt = None


#: LEGACY-CIPHERTEXT：过渡期加密用的**固定** salt（三平台一致，不含机器标识）。
#:
#: 旧版曾把机器标识（Windows WMIC UUID / macOS IOPlatformUUID / Linux
#: ``/etc/machine-id``，取不到则 hostname）当 PBKDF2 的 salt，于是"重装系统只留
#: home、容器镜像重建、克隆虚拟机"都会让密钥明明还在、密文却全部解不开。后续改为
#: 本常量以摆脱机器绑定。两者现均只用于**解开旧密文**，见模块顶部删除计划。
KEY_DERIVATION_SALT = b"ScholarNavis/master-key/v2"

#: LEGACY-CIPHERTEXT：PBKDF2 迭代次数，供上述历史派生使用（口令型导出的迭代次数见
#: :meth:`SystemEncryptionService.derive_key_from_password`，两处口径不同、不可混用）。
DERIVATION_ITERATIONS = 1000


# LEGACY-CIPHERTEXT：下面两个平台标识函数仅供"机器标识当 salt"的历史派生使用，
# 与密钥托管、业务逻辑无关，见模块顶部的删除计划。
def _windows_machine_uuid() -> str:
    """Windows 机器 UUID。

    ``wmic`` 自 Windows 11 24H2 起已从系统移除（降级为按需安装的功能），保留
    原有 wmic 取值是为了让既有密文仍能派生同一密钥——顺序不可调换，只能"取不到
    再回退"到 CIM（PowerShell）。
    """
    try:
        out = subprocess.check_output(
            "wmic csproduct get uuid", shell=True,
            creationflags=no_window_flags()).decode()
        uuid = out.splitlines()[1].strip()
        if uuid:
            return uuid
    except (OSError, IndexError, subprocess.SubprocessError) as e:
        logging.getLogger("EncryptionService").debug(f"wmic unavailable: {e}")

    out = subprocess.check_output(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance -ClassName Win32_ComputerSystemProduct).UUID"],
        creationflags=no_window_flags()).decode()
    return out.strip()


def _macos_machine_uuid() -> str:
    """macOS 机器 UUID（``IOPlatformUUID``）。

    直接解析 ``ioreg`` 输出，不再 ``shell=True`` 管道给 ``grep``：后者依赖外部
    命令，且 ``grep -E '(UUID)'`` 会匹配任意含 UUID 的行再取最后一个引号串，
    取值不确定。正则提取与旧行为在标准机器上结果一致。
    """
    out = subprocess.check_output(
        ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"]).decode(errors="replace")
    match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
    if not match:
        raise ValueError("IOPlatformUUID not found in ioreg output")
    return match.group(1)


class SystemEncryptionService:
    """配置相关加解密：口令型导出为现行用途，其余属于历史密文兼容层（见模块顶部）。"""

    def __init__(self, service_name: str = "ScholarNavis"):
        self.logger = logging.getLogger("EncryptionService")
        self.service_name = service_name
        self.account_name = "system_bound_master_key"
        self._master_fernet: Optional[Fernet] = None
        #: 历史派生（salt = 机器标识）的 Fernet，仅为解密旧密文而存在
        self._legacy_fernet: Optional[Fernet] = None
        self._legacy_unavailable = False

    # ------------------------------------------------------------------ #
    #  LEGACY-CIPHERTEXT 兼容层：机器标识派生 / 两套 Fernet / 解密入口
    #  （整段只读历史密文，最早 v2.2 删除；见模块顶部删除计划）
    # ------------------------------------------------------------------ #
    def _get_machine_id(self) -> str:
        """本机硬件标识——**仅用于解密历史密文**（见 :meth:`_get_legacy_fernet`）。

        保留本方法的唯一理由是：旧版本用"机器标识当 salt"加密过配置与技能源码，
        这些密文只有按当年那套 salt 才解得开，而各平台"先取既有来源、取不到才回退
        hostname"的取值顺序也必须是当年那套，顺序不可调整（见
        :func:`_windows_machine_uuid`）。改造后写入侧不再产生任何密文。
        """
        try:
            if SYSTEM == PLATFORM_WINDOWS:
                return _windows_machine_uuid()
            if SYSTEM == PLATFORM_LINUX:
                with open("/etc/machine-id", "r") as f:
                    return f.read().strip()
            if SYSTEM == PLATFORM_MACOS:
                return _macos_machine_uuid()
        except (OSError, ValueError, IndexError, subprocess.SubprocessError) as e:
            self.logger.warning(f"Machine id lookup failed, using hostname: {e}")
            return platform.node()
        return "fallback-id"

    def _derive_fernet(self, salt: bytes) -> Fernet:
        """用托管的主密钥 + 给定 salt 派生 Fernet（唯一的派生实现）。"""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=DERIVATION_ITERATIONS,
            backend=default_backend(),
        )
        return Fernet(base64.urlsafe_b64encode(kdf.derive(self._get_master_key())))

    def _get_master_fernet(self) -> Fernet:
        """固定 salt 派生的 Fernet：用于读"中间格式"密文（与机器标识无关）。

        该派生只出现在改造前的过渡版本里，现已无写入侧使用；保留是为了让那一批
        文件迁移时不必回退机器标识。
        """
        if self._master_fernet is None:
            self._master_fernet = self._derive_fernet(KEY_DERIVATION_SALT)
        return self._master_fernet

    def _get_legacy_fernet(self) -> Optional[Fernet]:
        """历史 Fernet（salt = 机器标识）：**只用于解密旧密文**，任何写入方都不得使用。

        机器标识取不到时返回 ``None``——此时历史密文本就解不开，交给调用方按
        "读不出"处理（例如配置回落到默认值），不要让它升级成启动失败。
        """
        if self._legacy_fernet is None and not self._legacy_unavailable:
            try:
                salt = self._get_machine_id().encode()
            except Exception as e:
                self._legacy_unavailable = True
                self.logger.warning(f"Legacy key derivation unavailable: {e}")
                return None
            try:
                self._legacy_fernet = self._derive_fernet(salt)
            except Exception as e:
                self._legacy_unavailable = True
                self.logger.warning(f"Legacy key derivation failed: {e}")
                return None
        return self._legacy_fernet

    def decrypt_with_mode(self, encrypted_data: bytes) -> Tuple[str, bool]:
        """解密并返回 ``(明文, 是否用了历史派生)``。

        调用方据此决定要不要把文件按新格式重写（一次性迁移）；解析本身两种派生共用
        同一份实现，避免"两处各写一遍解密"。
        """
        try:
            return self._get_master_fernet().decrypt(encrypted_data).decode(), False
        except InvalidToken:
            legacy = self._get_legacy_fernet()
            if legacy is None:
                raise
            plain = legacy.decrypt(encrypted_data).decode()
            self.logger.warning(
                "Decrypted a legacy machine-bound ciphertext. Rewrite the file to "
                "migrate it to the machine-independent key.")
            return plain, True

    def decrypt(self, encrypted_data: bytes) -> str:
        """Decrypts data using the master key (legacy ciphertexts still readable)."""
        return self.decrypt_with_mode(encrypted_data)[0]

    # ------------------------------------------------------------------ #
    #  LEGACY-CIPHERTEXT 兼容层：主密钥托管（只剩"解开历史密文"这一个用途，
    #  最早 v2.2 删除；见模块顶部删除计划）
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
        if IS_WINDOWS and win32crypt:
            return win32crypt.CryptProtectData(raw, "ScholarNavis Key", None, None, None, 0)
        return raw

    @staticmethod
    def _unprotect(blob: bytes) -> bytes:
        if IS_WINDOWS and win32crypt:
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


    # ------------------------------------------------------------------ #
    #  生产用途：口令型导出 / 导入（保留，导出文件会离开本机）
    # ------------------------------------------------------------------ #
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