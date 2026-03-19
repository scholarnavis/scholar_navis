import base64
import json
import logging

from src.core.config_manager import CONFIG_VERSION
from src.core.core_task import BackgroundTask
from src.core.encryption_service import SystemEncryptionService


logger = logging.getLogger("ConfigTask")

class ExportConfigTask(BackgroundTask):
    """
    Handles JSON bundling and encryption in the background.
    Returns a dictionary ready to be saved as a .json file.
    """
    def _execute(self) -> dict:
        bundle = self.kwargs.get("bundle")
        password = self.kwargs.get("password")  # Optional
        self.update_progress(30, "Serializing configuration...")
        try:
            content_str = json.dumps(bundle, indent=4, ensure_ascii=False)
        except Exception as e:
            return {"success": False, "msg": f"Serialization failed: {str(e)}"}

        if password:
            self.update_progress(60, "Performing PBKDF2 encryption...")

            if self.is_cancelled():
                raise InterruptedError("Export safely terminated by user.")

            try:
                service = SystemEncryptionService()
                encrypted_blob, salt = service.export_data(content_str, password)
                return {
                    "success": True,
                    "version": CONFIG_VERSION,
                    "payload": base64.b64encode(encrypted_blob).decode('utf-8'),
                    "salt": base64.b64encode(salt).decode('utf-8'),
                    "mode": "encrypted"
                }
            except Exception as e:
                logger.error(f"Encryption failed: {e}")
                return {"success": False, "msg": f"Encryption failed: {str(e)}"}
        # Plain text export
        return {
            "success": True,
            "version": CONFIG_VERSION,
            "data": bundle,
            "mode": "plain"
        }


class ImportConfigTask(BackgroundTask):
    """
    Handles decryption and format validation in the background.
    Returns the original configuration dictionary.
    """

    def _execute(self) -> dict:
        path = self.kwargs.get("path")
        password = self.kwargs.get("password")
        self.update_progress(20, "Reading configuration file...")
        try:
            with open(path, 'r', encoding='utf-8') as f:
                bundle = json.load(f)
        except Exception as e:
            return {"success": False, "msg": f"Failed to read file: {str(e)}"}
        service = SystemEncryptionService()

        # Check format
        is_encrypted = "payload" in bundle and "salt" in bundle

        if is_encrypted:
            if not password:
                return {"success": False, "msg": "This file is encrypted. Password required."}
            self.update_progress(50, "Decrypting secure payload...")

            if self.is_cancelled():
                raise InterruptedError("Import safely terminated by user.")

            try:
                encrypted_data = base64.b64decode(bundle["payload"])
                salt = base64.b64decode(bundle["salt"])

                # import_data returns the original string
                decrypted_str = service.import_data(encrypted_data, password, salt)
                data_dict = json.loads(decrypted_str)

                self.update_progress(100, "Verification complete")
                return {"success": True, "data": data_dict, "mode": "encrypted"}
            except Exception as e:
                logger.error(f"Decryption failed: {e}")
                return {"success": False, "msg": "Decryption failed. Wrong password or corrupted file."}
        else:
            # Plain text import
            self.update_progress(50, "Validating plain text structure...")
            if "data" in bundle:
                # New format with version wrapper
                return {"success": True, "data": bundle["data"], "mode": "plain"}
            elif "settings" in bundle:
                # Legacy format (direct dump)
                return {"success": True, "data": bundle, "mode": "plain"}
            else:
                return {"success": False, "msg": "Invalid configuration file structure."}