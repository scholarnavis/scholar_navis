import os
import platform
import subprocess
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl


class FileService:
    @staticmethod
    def open_file(path: str, page: int = None, line: int = None):
        """
        通用文件打开逻辑，支持 PDF 跳转页码。
        """
        if not os.path.exists(path):
            return False, "File not found"

        try:
            if page is not None and path.lower().endswith('.pdf'):
                FileService._open_pdf_at_page(path, page)
                return True, "Opened in Browser/Viewer"


            url = QUrl.fromLocalFile(path)
            QDesktopServices.openUrl(url)
            return True, "Opened with default app"

        except Exception as e:
            return False, str(e)

    @staticmethod
    def _open_pdf_at_page(path: str, page: int):
        file_url = QUrl.fromLocalFile(path).toString() + f"#page={page}"

        system_name = platform.system()

        if system_name == 'Windows':
            subprocess.Popen(['start', file_url], shell=True)

        elif system_name == 'Darwin':  # macOS
            subprocess.Popen(['open', file_url])

        else:  # Linux
            subprocess.Popen(['xdg-open', file_url])

    @staticmethod
    def reveal_in_explorer(path: str):
        """在资源管理器中显示文件"""
        folder = os.path.dirname(path)
        if platform.system() == 'Windows':
            subprocess.Popen(['explorer', '/select,', path])
        elif platform.system() == 'Darwin':
            subprocess.Popen(['open', '-R', path])
        else:
            subprocess.Popen(['xdg-open', folder])