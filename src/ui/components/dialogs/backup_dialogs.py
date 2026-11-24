"""WebDAV 备份源编辑对话框。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QFormLayout, QLabel, QLineEdit, QWidget

from src.core.i18n import tr
from src.core.theme_manager import ThemeManager
from src.core.webdav_backup import normalize_server_url, WebDAVError
from src.ui.components.dialogs.base import BaseDialog
from src.ui.components.HoverRevealLineEdit import HoverRevealLineEdit

__all__ = ["WebDAVSourceDialog"]


class WebDAVSourceDialog(BaseDialog):
    """新增/编辑一个备份源。``result_profile`` 为 None 表示用户取消。"""

    def __init__(self, parent=None, profile=None):
        editing = bool(profile)
        super().__init__(parent,
                         title=tr("Edit WebDAV Source") if editing else tr("Add WebDAV Source"),
                         width=520)

        self.result_profile = None
        source = dict(profile or {})
        self._source_id = str(source.get("id") or "")

        form_widget = QWidget()
        form = QFormLayout(form_widget)
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignRight)

        self.inp_name = QLineEdit(str(source.get("name") or ""))
        self.inp_name.setPlaceholderText(tr("Display name, e.g. Home NAS"))

        self.inp_url = QLineEdit(str(source.get("url") or ""))
        self.inp_url.setPlaceholderText("https://dav.example.com/remote.php/dav/files/<user>/")

        self.inp_dir = QLineEdit(str(source.get("remote_dir") or "ScholarNavis"))
        self.inp_dir.setPlaceholderText(tr("Folder on the server, created when missing"))

        self.inp_user = QLineEdit(str(source.get("username") or ""))

        self.inp_password = HoverRevealLineEdit(str(source.get("password") or ""))
        self.inp_password.setPlaceholderText(tr("App password is recommended"))

        self.chk_verify = QCheckBox(tr("Allow self-signed certificates"))
        self.chk_verify.setChecked(not bool(source.get("verify_ssl", True)))

        form.addRow(tr("Name:"), self.inp_name)
        form.addRow(tr("Server URL:"), self.inp_url)
        form.addRow(tr("Remote folder:"), self.inp_dir)
        form.addRow(tr("Username:"), self.inp_user)
        form.addRow(tr("Password:"), self.inp_password)
        form.addRow("", self.chk_verify)

        self.content_layout.addWidget(form_widget)

        self.lbl_hint = QLabel(tr(
            "The password is stored in config/secrets.json, never in the backup source "
            "file or in logs. An app password is safer than the account password."))
        self.lbl_hint.setWordWrap(True)
        self.content_layout.addWidget(self.lbl_hint)

        self.add_button(tr("Cancel"), self.reject)
        self.add_button(tr("Save"), self._on_save, is_primary=True)

        self._apply_theme()

    def _apply_theme(self):
        super()._apply_theme()
        tm = ThemeManager()
        self.lbl_hint.setStyleSheet(
            f"color: {tm.color('text_muted')}; font-size: 12px;"
            f"font-family: {tm.font_family()};")
        self.chk_verify.setStyleSheet(
            f"color: {tm.color('text_main')}; font-family: {tm.font_family()};")

    def _on_save(self):
        from src.ui.components.dialogs.common import StandardDialog

        name = self.inp_name.text().strip()
        if not name:
            StandardDialog(self, tr("Invalid Input"), tr("Please enter a name for this source.")).exec()
            return

        try:
            url = normalize_server_url(self.inp_url.text())
        except WebDAVError as e:
            StandardDialog(self, tr("Invalid Input"), str(e)).exec()
            return

        self.result_profile = {
            "id": self._source_id,
            "name": name,
            "url": url,
            "remote_dir": self.inp_dir.text().strip() or "ScholarNavis",
            "username": self.inp_user.text().strip(),
            "password": self.inp_password.text(),
            "verify_ssl": not self.chk_verify.isChecked(),
        }
        self.accept()
