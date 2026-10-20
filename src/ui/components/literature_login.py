"""付费墙文献获取面板：内嵌浏览器登录 + 自动识别下载的 PDF。

设计要点
--------
* **非模态**：面板是一个独立窗口，但不阻塞对话；用户可以一边登录/下载，一边
  继续看当前对话。下载完成后只把 PDF 加入当前会话证据库，不重置 history。
* **持久化 Cookie**：QWebEngineProfile 指向 ``scholar_workspace/browser_profile``，
  登录状态跨启动保留；不保存密码。设置里提供清除入口。
* **下载即导入**：监听 ``downloadRequested``，只接受用户主动点击的下载；完成后
  抓取页面 ``citation_*`` 元数据，连同已知 DOI/标题一起交给上层入库。
* **降级**：QtWebEngine 不可用时退回系统浏览器 + 手动选择已下载 PDF。
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile

from urllib.parse import urlparse

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtNetwork import QNetworkProxy, QNetworkProxyFactory
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from src.core import BASE_DIR
from src.core.config_manager import ConfigManager
from src.core.theme_manager import ThemeManager, strong_weight_css

logger = logging.getLogger("UI.LiteratureLogin")

_META_JS = r"""
(() => {
  const all = (name) => Array.from(document.querySelectorAll('meta[name="' + name + '"]'))
    .map(e => (e.content || '').trim()).filter(Boolean);
  const first = (name) => (all(name)[0] || '');
  return {
    title: first('citation_title') || document.title || '',
    doi: first('citation_doi') || '',
    authors: all('citation_author').join('; '),
    journal: first('citation_journal_title') || '',
    year: (first('citation_publication_date') || '').slice(0, 4),
    url: location.href || ''
  };
})()
"""


_SHARED_PROFILE = None


def _profile_dir() -> str:
    path = os.path.join(BASE_DIR, "scholar_workspace", "browser_profile")
    os.makedirs(path, exist_ok=True)
    return path


_PROXY_SAVED = False
_PREV_APP_PROXY = None
_PREV_SYSTEM_PROXY = True


def _journal_proxy_preference() -> bool:
    """期刊浏览器是否走全局代理；默认 False（机构 IP 自动登录）。"""
    try:
        return bool(ConfigManager().user_settings.get("journal_use_proxy", False))
    except Exception:
        return False


def _apply_journal_proxy() -> str:
    """按设置给 Qt/WebEngine 应用直连或自定义代理，返回状态描述。

    注意：QtWebEngine 没有 per-profile 代理 API，代理是应用级的。面板关闭时
    会恢复；这段时间内其它 Qt 网络请求也会受影响，这是换取"机构 IP 直连"
    的代价。
    """
    global _PROXY_SAVED, _PREV_APP_PROXY, _PREV_SYSTEM_PROXY
    if not _PROXY_SAVED:
        _PREV_APP_PROXY = QNetworkProxy.applicationProxy()
        try:
            _PREV_SYSTEM_PROXY = QNetworkProxyFactory.usesSystemConfiguration()
        except Exception:
            _PREV_SYSTEM_PROXY = True
        _PROXY_SAVED = True

    use_proxy = _journal_proxy_preference()
    if not use_proxy:
        QNetworkProxy.setApplicationProxy(
            QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        return "direct (institutional IP)"

    cfg = ConfigManager().user_settings
    mode = str(cfg.get("proxy_mode", "off"))
    raw_url = str(cfg.get("proxy_url", "")).strip()
    if mode == "custom" and raw_url:
        parsed = urlparse(raw_url if "://" in raw_url else f"http://{raw_url}")
        host = parsed.hostname or ""
        port = parsed.port or 8080
        if host:
            QNetworkProxy.setApplicationProxy(QNetworkProxy(
                QNetworkProxy.ProxyType.HttpProxy, host, port,
                parsed.username or "", parsed.password or ""))
            return f"proxy {host}:{port}"
    if mode == "system":
        QNetworkProxyFactory.setUseSystemConfiguration(True)
        return "system proxy"
    QNetworkProxy.setApplicationProxy(
        QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
    return "direct (no proxy configured)"


def _restore_application_proxy() -> None:
    global _PROXY_SAVED
    if not _PROXY_SAVED:
        return
    try:
        QNetworkProxy.setApplicationProxy(_PREV_APP_PROXY)
        QNetworkProxyFactory.setUseSystemConfiguration(_PREV_SYSTEM_PROXY)
    except Exception as e:
        logger.debug("Restore application proxy failed: %s", e)
    _PROXY_SAVED = False


def shared_literature_profile(parent=None):
    """所有登录面板共用同一个持久化 profile：Cookie 只存一份，设置里清除一次生效。"""
    global _SHARED_PROFILE
    if _SHARED_PROFILE is None:
        # 代理必须在 profile 创建前应用：WebEngine 的网络上下文在此时建立。
        _apply_journal_proxy()
        from PySide6.QtWebEngineCore import QWebEngineProfile
        path = _profile_dir()
        # 不挂到某个对话窗口上：面板关闭后 profile 仍需存活，下次打开沿用 Cookie。
        profile = QWebEngineProfile("literature_login")
        profile.setPersistentStoragePath(path)
        profile.setCachePath(os.path.join(path, "cache"))
        profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        _SHARED_PROFILE = profile
    return _SHARED_PROFILE


def clear_literature_login() -> bool:
    """清除出版社/机构登录会话；WebEngine 不可用时退回删除 profile 目录。"""
    global _SHARED_PROFILE
    try:
        profile = shared_literature_profile(None)
        profile.cookieStore().deleteAllCookies()
        profile.clearHttpCache()
        return True
    except Exception as e:
        logger.warning("Clear literature login via profile failed: %s", e)
        try:
            shutil.rmtree(_profile_dir(), ignore_errors=True)
            _SHARED_PROFILE = None
            return True
        except Exception as e2:
            logger.warning("Clear literature login directory failed: %s", e2)
            return False


class LiteratureLoginPanel(QDialog):
    """文献获取面板：加载 DOI/出版社页，用户登录后下载，PDF 就绪时发信号。"""

    sig_pdf_ready = Signal(str, object)   # (本地 PDF 路径, 元数据 dict)
    sig_status = Signal(str)
    sig_submit_finished = Signal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Get Full Text")
        self.setModal(False)
        self.resize(1040, 820)
        self.setWindowFlag(Qt.Window, True)
        self._known: dict = {}
        self._view = None
        self._profile = None
        self._download_dir = os.path.join(tempfile.gettempdir(), "navis_literature_downloads")
        os.makedirs(self._download_dir, exist_ok=True)
        self._building = False
        self._pending_paths = set()
        self._submit_results = {}
        self._waiting_close = False
        self._allow_close = False
        self._build_ui()
        self._apply_theme()
        ThemeManager().theme_changed.connect(self._apply_theme)

    # ------------------------------------------------------------------ #
    #  UI
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(6)

        self.lbl_title = QLabel("")
        self.lbl_title.setWordWrap(True)
        self.lbl_hint = QLabel(
            "Log in to the publisher or your institution, then click the PDF download "
            "button. The downloaded PDF will be added to this conversation's evidence.")
        self.lbl_hint.setWordWrap(True)
        outer.addWidget(self.lbl_title)
        outer.addWidget(self.lbl_hint)

        self.browser_host = QWidget()
        self.browser_layout = QVBoxLayout(self.browser_host)
        self.browser_layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.browser_host, 1)

        row = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        row.addWidget(self.lbl_status, 1)
        self.btn_clear = QPushButton("Clear login")
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.clicked.connect(self.clear_login)
        self.btn_close = QPushButton("Submit && Close")
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.clicked.connect(self.request_submit_and_close)
        row.addWidget(self.btn_clear)
        row.addWidget(self.btn_close)
        outer.addLayout(row)

        self._build_browser()

    def _build_browser(self):
        try:
            from PySide6.QtWebEngineCore import QWebEnginePage
            from PySide6.QtWebEngineWidgets import QWebEngineView
            profile = shared_literature_profile(self)
        except Exception as e:  # 运行库缺失时不让整个功能崩掉
            logger.warning("QtWebEngine unavailable for literature login: %s", e)
            self._build_fallback(str(e))
            return

        self._profile = profile
        self._profile.downloadRequested.connect(self._on_download)

        self._view = QWebEngineView(self)
        page = QWebEnginePage(self._profile, self._view)
        self._view.setPage(page)
        self.browser_layout.addWidget(self._view)

    def _build_fallback(self, reason: str):
        self._view = None
        box = QLabel(
            "The embedded browser could not be loaded on this system.\n"
            "You can open the paper in your system browser, log in there, download the "
            "PDF, then choose the downloaded file here.\n\n"
            f"Reason: {reason}")
        box.setWordWrap(True)
        self.browser_layout.addWidget(box)
        row = QHBoxLayout()
        btn_open = QPushButton("Open in system browser")
        btn_open.setCursor(Qt.PointingHandCursor)
        btn_open.clicked.connect(self._open_external)
        btn_pick = QPushButton("Choose downloaded PDF...")
        btn_pick.setCursor(Qt.PointingHandCursor)
        btn_pick.clicked.connect(self._pick_pdf)
        row.addWidget(btn_open)
        row.addWidget(btn_pick)
        row.addStretch(1)
        self.browser_layout.addLayout(row)

    # ------------------------------------------------------------------ #
    #  打开 / 下载
    # ------------------------------------------------------------------ #
    def open_paper(self, doi: str = "", title: str = "", url: str = ""):
        self._known = {
            "doi": str(doi or "").strip().replace("https://doi.org/", ""),
            "title": str(title or "").strip(),
            "url": str(url or "").strip(),
        }
        self.lbl_title.setText(self._known["title"] or self._known["doi"] or "Paper")
        proxy_mode = "external browser" if self._view is None else _apply_journal_proxy()
        self.lbl_status.setText(
            f"Waiting for login / PDF download... (network: {proxy_mode})")
        target = (self._known["url"]
                  or (f"https://doi.org/{self._known['doi']}" if self._known["doi"] else ""))
        if self._view is not None and target:
            self._view.setUrl(QUrl(target))
        elif self._view is not None:
            self.lbl_status.setText("No DOI/URL was provided for this paper.")
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_download(self, download):
        # 共享 profile 上可能挂着多个历史面板；只让当前可见的那个接收。
        if not self.isVisible():
            return
        try:
            name = download.downloadFileName() or "paper.pdf"
            if not name.lower().endswith(".pdf"):
                # 用户可能点了补充材料；仍然接收，但明确提示不是论文正文
                self.sig_status.emit(f"Downloading {name} (not a PDF paper)...")
            download.setDownloadDirectory(self._download_dir)
            download.setDownloadFileName(name)
            download.accept()
            download.stateChanged.connect(
                lambda state, d=download: self._on_download_state(d, state))
        except Exception as e:
            logger.warning("Download request handling failed: %s", e)
            self.lbl_status.setText(f"Download failed: {e}")

    def _on_download_state(self, download, state):
        # 枚举名在不同 PySide6 版本里略有差异，用 isFinished() 判断完成；
        # 取消/中断也会 finished，但文件不存在，下面会走失败提示。
        if not download.isFinished():
            return
        path = os.path.join(download.downloadDirectory(), download.downloadFileName())
        if not os.path.isfile(path):
            self.lbl_status.setText("Download finished, but the file was not found.")
            return
        if path in getattr(self, "_finalized", set()):
            return
        self._finalized = getattr(self, "_finalized", set())
        self._finalized.add(path)
        # 等文件句柄完全关闭一帧再解析/搬运
        QTimer.singleShot(250, self, lambda p=path: self._finalize_download(p))

    def _finalize_download(self, path: str):
        self.lbl_status.setText("Download detected. Reading citation metadata...")

        def _emit(meta):
            merged = dict(self._known)
            if isinstance(meta, dict):
                for key in ("doi", "title", "authors", "journal", "year", "url"):
                    if meta.get(key):
                        merged[key] = meta[key]
            if not merged.get("doi"):
                merged["doi"] = self._known.get("doi", "")
            self._pending_paths.add(str(path))
            self._submit_results[str(path)] = None
            self.lbl_status.setText(
                "PDF ready. Registering it as full-text evidence...")
            self.sig_pdf_ready.emit(path, merged)

        if self._view is not None:
            try:
                self._view.page().runJavaScript(_META_JS, _emit)
                return
            except Exception as e:
                logger.debug("Metadata JS failed: %s", e)
        _emit({})

    def _open_external(self):
        target = self._known.get("url") or (
            f"https://doi.org/{self._known.get('doi')}" if self._known.get("doi") else "")
        if target:
            QDesktopServices.openUrl(QUrl(target))

    def _pick_pdf(self):
        from src.ui.components.file_dialogs import open_file_name
        path, _ = open_file_name(self, "Choose downloaded PDF", "",
                                 "PDF (*.pdf)")
        if path:
            self._pending_paths.add(str(path))
            self._submit_results[str(path)] = None
            self.lbl_status.setText(
                "PDF selected. Registering it as full-text evidence...")
            self.sig_pdf_ready.emit(path, dict(self._known))

    def set_submission_result(self, path: str, ok: bool, message: str = ""):
        """由聊天页在 PDF 解析/入库完成后回调；只有成功才允许提交关闭。"""
        path = str(path or "")
        if not path:
            return
        self._submit_results[path] = (bool(ok), str(message or ""))
        self._pending_paths.discard(path)
        if not ok:
            self._waiting_close = False
            self.lbl_status.setText(
                f"Full-text submission failed: {message or 'unknown error'}")
            self.sig_submit_finished.emit(False, message or "Full-text submission failed")
            return
        if self._pending_paths:
            self.lbl_status.setText(
                f"Registered. Waiting for {len(self._pending_paths)} more PDF(s)...")
            return
        self.lbl_status.setText("Full text registered successfully.")
        self.sig_submit_finished.emit(True, "Full text registered successfully.")
        if self._waiting_close:
            self._allow_close = True
            self.close()

    def request_submit_and_close(self):
        """关闭前提交：等待所有已下载 PDF 完成解析/入库。"""
        if not self._pending_paths:
            # 没有待提交 PDF，或全部已完成；允许直接关闭。
            self._allow_close = True
            self.close()
            return
        self._waiting_close = True
        self.lbl_status.setText(
            "Waiting for downloaded PDF(s) to be registered as evidence...")
        # 入库在后台线程，最多等 120 秒；避免异常丢失回调后窗口永远关不掉。
        QTimer.singleShot(125000, self, self._submission_timeout)

    def _submission_timeout(self):
        if self._pending_paths and not self._allow_close:
            self._waiting_close = False
            self.lbl_status.setText(
                "Timed out waiting for PDF registration. Check the download and try again.")
            self.sig_submit_finished.emit(False, "PDF registration timed out")

    def clear_login(self):
        if clear_literature_login():
            self.lbl_status.setText("Login session cleared.")
        else:
            self.lbl_status.setText("Could not clear the login session.")

    # ------------------------------------------------------------------ #
    #  主题
    # ------------------------------------------------------------------ #
    def _apply_theme(self):
        tm = ThemeManager()
        self.setStyleSheet(
            f"QDialog {{ background-color: {tm.color('bg_main')}; color: {tm.color('text_main')}; }}"
            f"QLabel {{ color: {tm.color('text_main')}; }}")
        self.lbl_title.setStyleSheet(
            f"color: {tm.color('text_main')}; font-weight: {strong_weight_css()}; font-size: 15px;")
        self.lbl_hint.setStyleSheet(f"color: {tm.color('text_muted')}; font-size: 12px;")
        self.lbl_status.setStyleSheet(f"color: {tm.color('accent')}; font-size: 12px;")
        for btn in (self.btn_clear, self.btn_close):
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {tm.color('btn_bg')}; color: {tm.color('text_main')}; "
                f"border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 5px 12px; }}"
                f"QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}")

    def showEvent(self, event):
        super().showEvent(event)
        if self._view is not None:
            _apply_journal_proxy()

    def hideEvent(self, event):
        # 面板不可见时立即恢复应用级代理，避免直连/代理设置影响聊天网络。
        _restore_application_proxy()
        super().hideEvent(event)

    def closeEvent(self, event):
        # 用户点窗口 X 时也走“提交后关闭”：等待已下载 PDF 的入库结果。
        if self._pending_paths and not self._allow_close:
            event.ignore()
            self.request_submit_and_close()
            return
        _restore_application_proxy()
        super().closeEvent(event)


__all__ = ["LiteratureLoginPanel", "clear_literature_login", "shared_literature_profile"]
