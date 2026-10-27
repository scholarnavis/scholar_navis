import os

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from src.core import attachment_parser
from src.core.platform_env import open_with_system, reveal_in_file_manager


# 找个时间把他去了
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
        """在系统默认阅读器中打开 PDF 并跳到指定页。

        ``#page=N`` 只有把**完整的 file:// URL（含片段）**交给系统打开器才生效，
        ``QDesktopServices`` 不接受片段，因此走 :mod:`src.core.platform_env` 的
        跨平台打开器（Windows ``os.startfile`` / macOS ``open`` / Linux
        ``xdg-open``），全程不经 shell（旧实现用 ``shell=True`` + ``start``，
        既会闪控制台窗口，也无法处理含空格/特殊字符的路径）。
        """
        file_url = QUrl.fromLocalFile(path).toString() + f"#page={page}"
        open_with_system(file_url)

    @staticmethod
    def reveal_in_explorer(path: str):
        """在系统文件管理器中定位文件（资源管理器 / Finder / Linux）。"""
        reveal_in_file_manager(path)

    @staticmethod
    def read_file_content(path: str) -> str:
        """读取文件的纯文本内容，支持 .docx 与普通文本。

        解析与编码探测统一委托 :mod:`src.core.attachment_parser`：同一份文件在
        "聊天附件"和"查看器"里必须解出同一段文本，否则检索语料与界面显示会打架。
        """
        if not os.path.exists(path):
            return ""

        lower = path.lower()
        try:
            if lower.endswith('.docx'):
                return attachment_parser.read_docx_text(path)
            if lower.endswith('.doc'):
                return ""
            with open(path, 'rb') as f:
                return attachment_parser.decode_text_bytes(f.read()).strip()
        except OSError as e:
            print(f"Error reading file {path}: {e}")
            return ""