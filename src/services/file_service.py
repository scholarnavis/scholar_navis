import os

import chardet
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

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
        """
        通用文件读取逻辑，支持 .docx 和普通文本文件。
        """
        if not os.path.exists(path):
            return ""

        ext = path.lower()

        # 处理 DOCX
        if ext.endswith('.docx'):
            try:
                import docx
                doc = docx.Document(path)
                return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
            except Exception as e:
                print(f"Error reading DOCX: {e}")
                return ""

        elif ext.endswith('.doc'):
            return ""

        else:
            try:
                with open(path, 'rb') as f:
                    raw_data = f.read()
                    detected = chardet.detect(raw_data)
                    encoding = detected['encoding'] if detected['encoding'] else 'utf-8'
                    return raw_data.decode(encoding, errors='replace').strip()
            except Exception as e:
                print(f"Error reading text file: {e}")
                return ""