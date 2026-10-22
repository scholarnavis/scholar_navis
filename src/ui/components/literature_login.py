"""付费墙文献获取面板：内嵌浏览器登录 + 自动识别下载的 PDF。

设计要点
--------
* **非模态**：面板是一个独立窗口，但不阻塞对话；用户可以一边登录/下载，一边
  继续看当前对话。下载完成后只把 PDF 加入当前会话证据库，不重置 history。
* **持久化 Cookie**：QWebEngineProfile 指向 ``scholar_workspace/browser_profile``，
  登录状态跨启动保留；不保存密码。设置里提供清除入口。
* **下载即导入**：监听 ``downloadRequested``，只接受用户主动点击的下载；完成后
  抓取页面 ``citation_*`` 元数据，连同已知 DOI/标题一起交给上层入库。
* **地址可见可复制**：顶部地址栏显示当前地址（只读、可选中复制），并提供
  "复制 / 重新载入 / 用系统浏览器打开"。
* **安全状态可见**：``http://`` 标为"未加密"，证书校验失败时默认拦截、用户
  明确选择继续后才放行，并持续标注"不安全"，直到离开该站点。
* **降级**：QtWebEngine 不可用时退回系统浏览器 + 手动选择已下载 PDF。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time

from urllib.parse import urlparse

from PySide6.QtCore import QSize, QStandardPaths, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtNetwork import QNetworkProxy, QNetworkProxyFactory
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

from src.core import BASE_DIR
from src.core.config_manager import ConfigManager
from src.core.i18n import tr
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
#: 最近一次写入 Qt 网络层的代理设置签名；相同则跳过写入（写代理会重置
#: WebEngine 的网络上下文，重复设置会让首包变慢）。
_APPLIED_PROXY_SIG = None
#: 当前可见的面板数量。代理是**应用级**的：第一个面板显示时接管，最后一个面板
#: 隐藏时恢复；预热流程在没有面板可见时也必须立刻恢复，不能把聊天网络一起改了。
_OPEN_PANELS = 0


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

    重复调用是**幂等**的：同一份设置不会被反复写入 Qt 网络层。写代理会重置
    WebEngine 的网络上下文，每次显示面板都重设一遍，会让页面首包明显变慢。
    """
    global _PROXY_SAVED, _PREV_APP_PROXY, _PREV_SYSTEM_PROXY, _APPLIED_PROXY_SIG

    cfg = ConfigManager().user_settings
    use_proxy = _journal_proxy_preference()
    mode = str(cfg.get("proxy_mode", "off")) if use_proxy else ""
    raw_url = str(cfg.get("proxy_url", "")).strip() if use_proxy else ""
    sig = (use_proxy, mode, raw_url)
    if _PROXY_SAVED and _APPLIED_PROXY_SIG == sig:
        return _proxy_description(use_proxy, mode, raw_url)

    if not _PROXY_SAVED:
        _PREV_APP_PROXY = QNetworkProxy.applicationProxy()
        try:
            _PREV_SYSTEM_PROXY = QNetworkProxyFactory.usesSystemConfiguration()
        except Exception:
            _PREV_SYSTEM_PROXY = True
        _PROXY_SAVED = True

    if not use_proxy:
        QNetworkProxy.setApplicationProxy(
            QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
    elif mode == "custom" and raw_url:
        parsed = urlparse(raw_url if "://" in raw_url else f"http://{raw_url}")
        host = parsed.hostname or ""
        port = parsed.port or 8080
        if host:
            QNetworkProxy.setApplicationProxy(QNetworkProxy(
                QNetworkProxy.ProxyType.HttpProxy, host, port,
                parsed.username or "", parsed.password or ""))
        else:
            QNetworkProxy.setApplicationProxy(
                QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
    elif mode == "system":
        QNetworkProxyFactory.setUseSystemConfiguration(True)
    else:
        QNetworkProxy.setApplicationProxy(
            QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
    _APPLIED_PROXY_SIG = sig
    return _proxy_description(use_proxy, mode, raw_url)


def _proxy_description(use_proxy: bool, mode: str, raw_url: str) -> str:
    """把代理设置翻译成面板状态行里的一句说明。"""
    if not use_proxy:
        return "direct (institutional IP)"
    if mode == "custom" and raw_url:
        parsed = urlparse(raw_url if "://" in raw_url else f"http://{raw_url}")
        host = parsed.hostname or ""
        if host:
            return f"proxy {host}:{parsed.port or 8080}"
    if mode == "system":
        return "system proxy"
    return "direct (no proxy configured)"


def _restore_application_proxy() -> None:
    global _PROXY_SAVED, _APPLIED_PROXY_SIG
    if not _PROXY_SAVED:
        return
    try:
        QNetworkProxy.setApplicationProxy(_PREV_APP_PROXY)
        QNetworkProxyFactory.setUseSystemConfiguration(_PREV_SYSTEM_PROXY)
    except Exception as e:
        logger.debug("Restore application proxy failed: %s", e)
    _PROXY_SAVED = False
    _APPLIED_PROXY_SIG = None


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
        # 去掉 UA 里的 "QtWebEngine/x.y" 标记：不少出版社的机器人防护（Cloudflare
        # 等）会据此判定"非标准浏览器"，直接把页面换成验证页。版本号本身保留
        # （Qt 的默认 UA 已对齐内置 Chromium），只是不再自报宿主框架。
        try:
            default_ua = profile.httpUserAgent()
            cleaned = " ".join(tok for tok in default_ua.split()
                               if not tok.startswith("QtWebEngine/"))
            if cleaned and cleaned != default_ua:
                profile.setHttpUserAgent(cleaned)
        except Exception as e:
            logger.debug("Cannot adjust the browser user agent: %s", e)
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


#: 出版社页面里的链接很多是 ``target=_blank``：Qt 默认**不创建**新窗口，点击就
#: 没有任何反应（用户看到的是"点链接没反应"）。让新窗口请求回到同一个视图，
#: 并显式允许导航，页面内的链接才真正可点。
def _is_pdf_url(url) -> bool:
    """判断导航目标是不是 PDF 文件本身。

    出版社的"下载 PDF"按钮常常直接 `window.open('/xxx.pdf')`：Qt 会把这次导航
    交给页面（内置 PDF 查看器默认关闭时表现为一片空白），**根本不会触发下载**，
    用户看到的就是"点了没反应"。
    """
    try:
        path = str(url.path() or "").lower()
        query = str(url.query() or "").lower()
    except Exception:
        return False
    if path.endswith(".pdf"):
        return True
    # 形如 /download?format=pdf 的直链同样按 PDF 处理
    return bool(re.search(r"(?:^|[?&])(?:format|type|mime|download)=pdf(?:$|[&;])", query))


def _build_literature_page_class():
    from PySide6.QtWebEngineCore import QWebEnginePage

    class _LiteraturePage(QWebEnginePage):
        def createWindow(self, _window_type):      # noqa: N802 (Qt 命名)
            return self                            # 在当前视图里打开

        def acceptNavigationRequest(self, url, nav_type, is_main_frame):  # noqa: N802
            # PDF 导航改为下载：download() 会触发 downloadRequested，
            # 由面板接收并入库（Qt 6.4+）。
            if is_main_frame and _is_pdf_url(url):
                try:
                    if hasattr(self, "download"):
                        self.download(url)
                        return False
                except Exception as e:
                    logger.debug("PDF navigation could not be turned into a download: %s", e)
            return True

        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
            return                                 # 页面自带脚本的噪音不要刷屏

    return _LiteraturePage


try:                        # WebEngine 缺失时不导入，交给 _build_browser 走降级
    _LiteraturePage = _build_literature_page_class()
except Exception:           # pragma: no cover - 纯环境问题
    _LiteraturePage = None


_WARM_VIEW = None


def warm_up_literature_browser() -> bool:
    """预热 QtWebEngine：提前拉起渲染进程，缩短首次打开面板的等待。

    QtWebEngine 首次创建页面时要启动 Chromium 的 GPU/渲染子进程并初始化磁盘缓存，
    部分环境要 3–6 秒，表现为"点了半天没反应"（与网速无关）。调用方应在用户**已经
    在等待**的时机触发（例如发出第一条消息之后），这样既不占启动内存，也不影响
    首屏；预热本身幂等，失败只是回到原来的首次加载成本。

    返回值表示是否已就绪（WebEngine 不可用时为 False）。
    """
    global _WARM_VIEW
    if _WARM_VIEW is not None:
        return True
    try:
        from PySide6.QtWebEngineCore import QWebEnginePage
        from PySide6.QtWebEngineWidgets import QWebEngineView

        profile = shared_literature_profile(None)
        view = QWebEngineView()            # 不上屏，只为拉起渲染进程
        view.setAttribute(Qt.WA_DontShowOnScreen, True)
        view.setPage(QWebEnginePage(profile, view))
        view.load(QUrl("about:blank"))
        # 必须保活：视图析构会连带销毁刚热起来的渲染进程，预热门就白做了。
        _WARM_VIEW = view
        # 创建 profile 时接管了应用级代理；此刻没有面板可见，必须立刻还回去，
        # 否则聊天本身的网络请求会在用户不知情时走直连/代理。
        if _OPEN_PANELS == 0:
            _restore_application_proxy()
        logger.info("QtWebEngine warmed up for the literature panel.")
        return True
    except Exception as e:
        logger.info("QtWebEngine warm-up skipped: %s", e)
        return False


class LiteratureLoginPanel(QDialog):
    """文献获取面板：加载 DOI/出版社页，用户登录后下载，PDF 就绪时发信号。"""

    sig_pdf_ready = Signal(str, object)   # (本地 PDF 路径, 元数据 dict)
    sig_status = Signal(str)
    sig_submit_finished = Signal(bool, str)
    #: 面板已关闭（bool = 是否至少提交成功过一份 PDF）。聊天页据此判断"用户放弃
    #: 登录"，把暂停的对话解锁，避免输入框一直锁着。
    sig_closed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Get Full Text")
        self.setModal(False)
        self.resize(1040, 820)
        self.setWindowFlag(Qt.Window, True)
        self._known: dict = {}
        self._view = None
        self._profile = None
        #: 共享 profile 上的下载信号只允许**当前可见**的面板订阅。历史面板若一直挂着，
        #: 会抢在可见面板之前 accept 下载并把文件登记进早已失效的对话。
        self._downloads_bound = False
        self._download_dir = os.path.join(tempfile.gettempdir(), "navis_literature_downloads")
        os.makedirs(self._download_dir, exist_ok=True)
        self._building = False
        self._pending_paths = set()
        self._submit_results = {}
        self._waiting_close = False
        self._allow_close = False
        #: 用户在本面板里明确"继续访问过证书无效站点"的 host：只要还停在这些
        #: 主机上，地址栏就一直显示不安全，避免"点过一次就忘"。
        self._insecure_hosts: set[str] = set()
        #: 系统浏览器下载监听（机器人验证页过不去时的兜底）。
        self._watch_timer = None
        self._fs_watcher = None
        self._watch_known: set[str] = set()
        self._watch_seen: dict[str, int] = {}
        self._watch_started = 0.0
        self._watch_deadline = 0.0
        #: 下载进度：定时器 + 速度平滑（瞬时速度抖动很大，显示用滑动平均）。
        self._dl_timer = None
        self._download = None
        self._dl_started = 0.0
        self._dl_last_bytes = 0
        self._dl_last_ts = 0.0
        self._dl_speed = 0.0
        #: 是否有一次下载正在进行/刚结束（用它判断"要不要更新进度文案"，
        #: 不能用 isVisible：面板未显示时子控件的 isVisible 恒为 False）。
        self._dl_active = False
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

        # 标题必须限高：模型有可能把一整段正文（带 [24] 这类引用号）当成 title 传进来，
        # 不限高就会把窗口上半屏变成一堵字墙，浏览器只剩一条缝。
        self.lbl_title = QLabel("")
        self.lbl_title.setWordWrap(True)
        self.lbl_title.setTextFormat(Qt.PlainText)      # 标题来自模型：不做 HTML 解析
        self.lbl_title.setMaximumHeight(48)
        self.lbl_hint = QLabel(
            "Log in to the publisher or your institution, then click the PDF download "
            "button. The downloaded PDF will be added to this conversation's evidence.")
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setTextFormat(Qt.PlainText)
        outer.addWidget(self.lbl_title)
        outer.addWidget(self.lbl_hint)

        # 机器人验证 / 站点拦爬虫时的兜底条：系统浏览器登录 + 自动接管新下载的 PDF。
        self.challenge_row = QWidget()
        ch_row = QHBoxLayout(self.challenge_row)
        ch_row.setContentsMargins(0, 0, 0, 0)
        ch_row.setSpacing(8)
        self.lbl_challenge = QLabel(
            "This site asks for a bot check that the embedded browser cannot pass. "
            "Sign in with your system browser instead — a newly downloaded PDF is picked up "
            "automatically.")
        self.lbl_challenge.setWordWrap(True)
        self.lbl_challenge.setTextFormat(Qt.PlainText)
        self.btn_challenge_open = QPushButton("Open in system browser")
        self.btn_challenge_open.setCursor(Qt.PointingHandCursor)
        self.btn_challenge_open.clicked.connect(self._open_external_and_watch)
        self.challenge_row.setVisible(False)
        ch_row.addWidget(self.lbl_challenge, 1)
        ch_row.addWidget(self.btn_challenge_open)
        outer.addWidget(self.challenge_row)

        # 地址栏：显示当前地址（只读、可选中复制）+ 安全状态 + 常用操作。
        nav = QHBoxLayout()
        nav.setSpacing(6)
        # 徽标由"图标 + 文本"两个控件组成：QLabel 的 setText 会清掉 setPixmap 设的
        # 图片（两者互斥），塞进同一个标签里必然丢一个。
        self.security_host = QWidget()
        sec_row = QHBoxLayout(self.security_host)
        sec_row.setContentsMargins(0, 0, 0, 0)
        sec_row.setSpacing(3)
        self.lbl_sec_icon = QLabel()
        self.lbl_sec_icon.setFixedSize(14, 14)
        self.lbl_sec_text = QLabel("—")
        sec_row.addWidget(self.lbl_sec_icon, 0, Qt.AlignVCenter)
        sec_row.addWidget(self.lbl_sec_text, 0, Qt.AlignVCenter)
        self.security_host.setToolTip(
            "http:// 为明文传输；证书校验失败时此处会标注「不安全」")
        self.url_edit = QLineEdit()
        self.url_edit.setReadOnly(True)
        self.url_edit.setPlaceholderText("about:blank")
        self.url_edit.setCursorPosition(0)
        self.url_edit.setToolTip("当前地址（只读，可选中后复制）")
        # 三个操作按钮只放图标（文字进 tooltip / 无障碍名）：地址栏已经很长，
        # 再摆三块文字按钮会把网址挤到看不见。
        self.btn_copy_url = QPushButton()
        self.btn_copy_url.setCursor(Qt.PointingHandCursor)
        self.btn_copy_url.setToolTip("Copy URL")
        self.btn_copy_url.setAccessibleName("Copy URL")
        self.btn_copy_url.clicked.connect(self._copy_url)
        self.btn_reload = QPushButton()
        self.btn_reload.setCursor(Qt.PointingHandCursor)
        self.btn_reload.setToolTip("Reload")
        self.btn_reload.setAccessibleName("Reload")
        self.btn_reload.clicked.connect(self._reload)
        self.btn_open_ext = QPushButton()
        self.btn_open_ext.setCursor(Qt.PointingHandCursor)
        self.btn_open_ext.setToolTip("Open in the system browser")
        self.btn_open_ext.setAccessibleName("Open in the system browser")
        self.btn_open_ext.clicked.connect(self._open_external)
        for btn in (self.btn_copy_url, self.btn_reload, self.btn_open_ext):
            btn.setFixedSize(32, 28)
        nav.addWidget(self.security_host)
        nav.addWidget(self.url_edit, 1)
        nav.addWidget(self.btn_copy_url)
        nav.addWidget(self.btn_reload)
        nav.addWidget(self.btn_open_ext)
        outer.addLayout(nav)
        self._set_security("idle", "—")

        self.browser_host = QWidget()
        self.browser_layout = QVBoxLayout(self.browser_host)
        self.browser_layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.browser_host, 1)

        # 下载进度条：只在真正开始下载时出现（不占常态空间）。
        self.download_box = QWidget()
        dl_layout = QVBoxLayout(self.download_box)
        dl_layout.setContentsMargins(0, 0, 0, 0)
        dl_layout.setSpacing(3)
        self.lbl_download = QLabel("")
        self.lbl_download.setWordWrap(True)
        self.lbl_download.setTextFormat(Qt.PlainText)
        self.progress_download = QProgressBar()
        self.progress_download.setRange(0, 100)
        self.progress_download.setValue(0)
        self.progress_download.setTextVisible(False)
        self.progress_download.setFixedHeight(6)
        dl_layout.addWidget(self.lbl_download)
        dl_layout.addWidget(self.progress_download)
        self.download_box.setVisible(False)
        outer.addWidget(self.download_box)

        row = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        # 状态行里会出现用户下载的文件名：按纯文本渲染，避免文件名里的尖括号被当标签。
        self.lbl_status.setTextFormat(Qt.PlainText)
        row.addWidget(self.lbl_status, 1)
        self.btn_clear = QPushButton("Clear login")
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.clicked.connect(self.clear_login)
        # 手动兜底：用户已经在任何地方下好了 PDF，直接挑进来即可（不依赖浏览器）。
        self.btn_pick_pdf = QPushButton("Choose PDF...")
        self.btn_pick_pdf.setCursor(Qt.PointingHandCursor)
        self.btn_pick_pdf.clicked.connect(self._pick_pdf)
        self.btn_close = QPushButton("Submit && Close")
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.clicked.connect(self.request_submit_and_close)
        row.addWidget(self.btn_pick_pdf)
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

        self._view = QWebEngineView(self)
        page_cls = _LiteraturePage or QWebEnginePage
        page = page_cls(self._profile, self._view)
        self._view.setPage(page)
        self._view.urlChanged.connect(self._on_url_changed)
        # 机器人验证页（Cloudflare 等）在嵌入式浏览器里常常过不去：加载完先看一眼
        # 页面文字，命中就给一句可执行的提示，而不是让用户对着转圈页面发呆。
        self._view.loadFinished.connect(self._on_load_finished)
        # 证书校验失败：Qt 默认直接拦截；这里接管信号，让用户知道"为什么打不开"，
        # 并在明确选择继续后持续标注不安全（这个信号在 Qt 6.4+ 才有，故用 hasattr 兜底）。
        if hasattr(page, "certificateError"):
            page.certificateError.connect(self._on_certificate_error)
        else:
            logger.info("QWebEnginePage.certificateError unavailable; "
                        "invalid certificates will be blocked without a prompt.")
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
    #  地址栏 / 安全状态
    # ------------------------------------------------------------------ #
    def _set_security(self, state: str, text: str):
        """更新安全状态徽标。``state`` ∈ {idle, secure, insecure, unknown}。

        图标用仓库自带的 SVG（``lock`` / ``warning``），由
        :meth:`ThemeManager.icon` 按主题色重着色，因此深色 / 浅色切换不需要另备
        两套图；不使用 emoji——emoji 的颜色由系统字体决定，在深色模式下常常糊成
        一团，也无法与主题色统一。
        """
        tm = ThemeManager()
        spec = {
            "secure": ("lock", "accent"),
            "insecure": ("warning", "danger"),
            "unknown": ("warning", "warning"),
            "idle": ("", "text_muted"),
        }
        icon_name, color_key = spec.get(state, ("", "text_muted"))
        if icon_name:
            self.lbl_sec_icon.setPixmap(tm.icon(icon_name, color_key).pixmap(14, 14))
        else:
            self.lbl_sec_icon.clear()
        self.lbl_sec_text.setText(text)
        self.lbl_sec_text.setStyleSheet(
            f"color: {tm.color(color_key)}; font-weight: {strong_weight_css()}; "
            f"font-size: 12px;")

    def _sync_url_bar(self, url_str: str):
        """地址栏跟随当前地址；同时按 scheme / 证书历史刷新安全状态。"""
        url_str = str(url_str or "")
        if url_str and not url_str.startswith("data:"):
            self.url_edit.setText(url_str)
            self.url_edit.setCursorPosition(0)   # 长 URL 先看开头（域名优先）
        self._sync_security(url_str)

    def _sync_security(self, url_str: str):
        url = QUrl(url_str)
        scheme = url.scheme().lower()
        host = url.host().lower()
        if not url_str or scheme in ("", "about", "blob"):
            self._set_security("idle", "—")
        elif scheme == "http":
            # 明文：能上，但要让用户知道这段内容可能被中间人看过或改过。
            self._set_security("insecure", "不安全 · 未加密 (HTTP)")
        elif scheme == "https":
            if host and host in self._insecure_hosts:
                self._set_security("insecure", "不安全 · 证书无效（已手动继续）")
            else:
                self._set_security("secure", "HTTPS")
        elif scheme == "file":
            self._set_security("idle", "本地文件")
        else:
            self._set_security("unknown", scheme.upper())

    def _on_url_changed(self, url):
        try:
            self._sync_url_bar(url.toString())
        except RuntimeError:
            pass

    #: 机器人验证页的常见文案（中英），命中即提示改用系统浏览器。
    _CHALLENGE_HINTS = (
        "performing security verification", "just a moment", "checking your browser",
        "verify you are human", "enable javascript and cookies", "captcha",
        "请稍候", "人机验证", "安全验证",
    )

    #: 页面元数据抓取：出版社落地页普遍带 citation_* / og:*，比检索源给的字段准。
    _META_JS = r"""
    (function () {
      function m(name) {
        var el = document.querySelector('meta[name="' + name + '"]') ||
                 document.querySelector('meta[property="' + name + '"]');
        return el ? (el.getAttribute('content') || '') : '';
      }
      function all(name) {
        return Array.prototype.map.call(
          document.querySelectorAll('meta[name="' + name + '"]'),
          function (e) { return e.getAttribute('content') || ''; }
        ).filter(Boolean);
      }
      return JSON.stringify({
        title: m('citation_title') || m('og:title') || document.title || '',
        authors: all('citation_author').join('; '),
        journal: m('citation_journal_title') || m('citation_conference_title') ||
                 m('citation_book_title') || '',
        year: (m('citation_publication_date') || m('citation_date') ||
               m('citation_year') || '').slice(0, 4),
        doi: m('citation_doi'),
        type: m('citation_type') || ''
      });
    })()
    """

    def _on_load_finished(self, ok: bool):
        if not ok or self._view is None:
            return
        try:
            self._view.page().runJavaScript(
                "document.body ? document.body.innerText.slice(0, 4000) : ''",
                self._on_page_text)
            self._view.page().runJavaScript(self._META_JS, self._on_page_meta)
        except Exception as e:
            logger.debug("Challenge probe skipped: %s", e)

    def _on_page_meta(self, payload):
        """把页面里的 citation_* 元数据合并进本面板的著录。"""
        try:
            import json as _json
            data = _json.loads(payload or "{}") if isinstance(payload, str) else {}
        except Exception:
            return
        if not isinstance(data, dict) or not data:
            return
        changed = False
        for key in ("title", "authors", "journal", "year", "type"):
            value = str(data.get(key) or "").strip()
            if value and value != str(self._known.get(key) or ""):
                self._known[key] = value
                changed = True
        page_doi = str(data.get("doi") or "").strip().replace("https://doi.org/", "")
        if page_doi and not self._known.get("doi"):
            self._known["doi"] = page_doi
            changed = True
        title = str(self._known.get("title") or "")
        if title:
            shown = title if len(title) <= self._MAX_TITLE_CHARS else \
                title[: self._MAX_TITLE_CHARS].rstrip() + "…"
            self.lbl_title.setText(shown)
            self.lbl_title.setToolTip(title)
        if changed:
            logger.info("Literature metadata updated from page: %s",
                        str(self._known.get("title") or "")[:80])

    def _on_page_text(self, text):
        low = str(text or "").lower()
        blocked = any(hint in low for hint in self._CHALLENGE_HINTS)
        # 提示条只在该页面上显示；一旦离开验证页（用户在浏览器里完成了验证），
        # 或者正常加载了别的页面，就收起来，不长期占用垂直空间。
        self.challenge_row.setVisible(blocked)
        if blocked:
            self.lbl_status.setText(
                "该站点要求人机验证（Cloudflare / 机器人检查）。可在此完成验证；"
                "若一直停在此页，点上面按钮用系统浏览器登录，新下载的 PDF 会被自动接管。")
        elif self.lbl_status.text().startswith("该站点要求人机验证"):
            self.lbl_status.setText("Waiting for login / PDF download...")

    # ------------------------------------------------------------------ #
    #  系统浏览器兜底：监听下载目录，自动接管新下载的 PDF
    # ------------------------------------------------------------------ #
    @staticmethod
    def _download_dirs():
        """可能落盘的目录：系统"下载"目录 + 本面板的临时下载目录。"""
        dirs = []
        try:
            path = QStandardPaths.writableLocation(QStandardPaths.DownloadLocation)
            if path and os.path.isdir(path):
                dirs.append(path)
        except Exception:
            pass
        home = os.path.expanduser("~/Downloads")
        if os.path.isdir(home):
            dirs.append(home)
        return list(dict.fromkeys(dirs))

    def _open_external_and_watch(self):
        """用系统浏览器打开当前页，并开始盯下载目录里的新 PDF。"""
        self._open_external()
        self._start_download_watch()
        self.lbl_status.setText(
            "已用系统浏览器打开。登录并下载 PDF 后无需手动操作——面板会自动接收它。")

    def _start_download_watch(self):
        """监听下载目录，只处理**开始监听之后**新增的 PDF。

        不再"先把目录里所有 PDF 枚举一遍再记下来"：那是 O(目录大小) 的冷启动，
        下载目录越大越慢，且与"找某一篇论文"毫无关系。改用时间水位线
        （``mtime >= 开始监听时刻``）+ 目录变更事件，正常只处理 0–1 个新文件。
        """
        if self._watch_timer is not None:
            return
        self._watch_started = time.time() - 2.0      # 容忍文件系统时间精度
        self._watch_seen = {}
        self._watch_deadline = time.time() + 900
        dirs = self._download_dirs()
        try:
            from PySide6.QtCore import QFileSystemWatcher
            self._fs_watcher = QFileSystemWatcher(self)
            if dirs:
                self._fs_watcher.addPaths(dirs)
            self._fs_watcher.directoryChanged.connect(
                lambda _path: self._poll_downloads())
        except Exception as e:
            logger.debug("Directory watcher unavailable: %s", e)
        # 事件可能合并/丢失（部分网络盘）：再用低频定时器兜一层，只做加法。
        self._watch_timer = QTimer(self)
        self._watch_timer.timeout.connect(self._poll_downloads)
        self._watch_timer.start(5000)

    def _stop_download_watch(self):
        if self._watch_timer is not None:
            self._watch_timer.stop()
            self._watch_timer.deleteLater()
            self._watch_timer = None
        watcher = getattr(self, "_fs_watcher", None)
        if watcher is not None:
            try:
                watcher.deleteLater()
            except Exception:
                pass
            self._fs_watcher = None

    def _poll_downloads(self):
        """检查新出现的 PDF；体积连续两次不变才认为下载完成。

        只处理 **开始监听之后** 的文件（``mtime`` 水位线）：不枚举历史文件，也不把
        整目录的快照存进内存。下载常常先写 ``.part`` 再改名，所以仍然用"体积稳定"
        作为完成判据；事件由 ``QFileSystemWatcher`` 触发，定时器只作兜底。
        """
        if time.time() > self._watch_deadline:
            self._stop_download_watch()
            self.lbl_status.setText(
                "没有检测到新下载的 PDF。可点“Choose PDF...”手动选择已下载的文件。")
            return
        started = float(getattr(self, "_watch_started", 0.0) or 0.0)
        for directory in self._download_dirs():
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                if not name.lower().endswith(".pdf"):
                    continue
                path = os.path.join(directory, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                if stat.st_mtime < started:
                    continue                            # 监听开始前就存在的旧文件
                if path in self._watch_known:
                    continue
                if self._watch_seen.get(path) != stat.st_size:
                    self._watch_seen[path] = stat.st_size   # 还在写：下一轮再看
                    continue
                self._watch_known.add(path)
                self._stop_download_watch()
                self.lbl_status.setText(f"检测到新下载：{name}，正在入库…")
                self._pending_paths.add(str(path))
                self._submit_results[str(path)] = None
                self.sig_pdf_ready.emit(path, dict(self._known))
                return

    def _copy_url(self):
        text = self.url_edit.text().strip()
        if not text:
            self.lbl_status.setText("No URL to copy yet.")
            return
        QApplication.clipboard().setText(text)
        self.lbl_status.setText("URL copied to clipboard.")

    def _reload(self):
        if self._view is None:
            return
        try:
            if self.url_edit.text().strip():
                self._view.reload()
        except RuntimeError:
            pass

    def _on_certificate_error(self, error):
        """证书校验失败：默认拒绝；只有用户明确选择才继续，并记下该 host。"""
        url_str = ""
        try:
            url_str = error.url().toString()
        except Exception:
            pass
        host = QUrl(url_str).host().lower()
        reason = ""
        for attr in ("description", "errorDescription"):
            try:
                value = getattr(error, attr)()
            except Exception:
                continue
            if value:
                reason = str(value)
                break
        overridable = True
        try:
            overridable = bool(error.isOverridable())
        except Exception:
            pass

        if not overridable:
            try:
                error.rejectCertificate()
            except Exception as e:
                logger.warning("Reject certificate failed: %s", e)
            self._set_security("insecure", "不安全 · 证书无效（已阻止）")
            self.lbl_status.setText(
                "这个站点的证书无法通过校验，且不允许忽略；已停止加载。")
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("证书校验失败 / Certificate error")
        box.setText("该站点的 HTTPS 证书未通过校验。")
        box.setInformativeText(
            f"{reason or '证书与域名不匹配、已过期或由未知机构签发。'}\n\n"
            f"{url_str}\n\n"
            "继续访问时，你提交的账号与页面内容可能被第三方读取或篡改。"
            "仅在你确认这是学校/机构的内网或代理证书时才继续。")
        btn_continue = box.addButton("仍然继续（不安全）", QMessageBox.AcceptRole)
        btn_back = box.addButton("返回安全页面", QMessageBox.RejectRole)
        box.setDefaultButton(btn_back)
        box.exec()

        try:
            if box.clickedButton() is btn_continue:
                error.acceptCertificate()
                if host:
                    self._insecure_hosts.add(host)
                self._set_security("insecure", "不安全 · 证书无效（已手动继续）")
                self.lbl_status.setText(
                    f"已忽略证书警告并继续：{host}. 请勿在此页面输入与本次检索无关的凭据。")
                logger.warning("User accepted an invalid certificate for %s (%s)",
                               host or url_str, reason)
            else:
                error.rejectCertificate()
                self._set_security("insecure", "不安全 · 证书无效（已阻止）")
                self.lbl_status.setText("已停止加载：站点证书无效。")
        except Exception as e:
            logger.warning("Resolve certificate error failed: %s", e)

    # ------------------------------------------------------------------ #
    #  打开 / 下载
    # ------------------------------------------------------------------ #
    #: 标题显示上限（约两行）。超出部分进 tooltip——模型有时会把整段正文当作
    #: title 传进来，原样显示会把浏览器挤成一条缝。
    _MAX_TITLE_CHARS = 180

    @staticmethod
    def _clean_title(text: str) -> str:
        """清洗传进来的标题：去标签、去引用编号、折叠空白。"""
        raw = re.sub(r"<[^>]+>", " ", str(text or ""))
        raw = re.sub(r"\[\d{1,3}\]", " ", raw)      # 引用编号不是标题的一部分
        return " ".join(raw.split())

    def open_paper(self, doi: str = "", title: str = "", url: str = ""):
        self._known = {
            "doi": str(doi or "").strip().replace("https://doi.org/", ""),
            "title": self._clean_title(title),
            "url": str(url or "").strip(),
        }
        full_title = self._known["title"]
        shown = full_title
        if len(shown) > self._MAX_TITLE_CHARS:
            shown = shown[: self._MAX_TITLE_CHARS].rstrip() + "…"
        self.lbl_title.setText(shown or self._known["doi"] or "Paper")
        self.lbl_title.setToolTip(full_title or self._known["doi"] or "Paper")
        self.challenge_row.setVisible(False)
        proxy_mode = "external browser" if self._view is None else _apply_journal_proxy()
        # **DOI 优先**：doi.org 会 302 到出版社的正式落地页，页面里有
        # citation_title / citation_author / citation_journal_title 等元数据，
        # 比检索源给的 url（常常是聚合页或摘要页）更容易拿到准确的标题与作者。
        # 没有 DOI 时才退回 url。
        target = (f"https://doi.org/{self._known['doi']}" if self._known["doi"]
                  else self._known["url"])
        # 地址栏先显示目标地址：降级（无 WebEngine）时用户也能看到/复制它。
        self._sync_url_bar(target)
        if self._view is None:
            self.lbl_status.setText(
                f"Waiting for login / PDF download... (network: {proxy_mode})")
        elif target:
            self.lbl_status.setText("Loading page...")
        else:
            self.lbl_status.setText("No DOI/URL was provided for this paper.")

        # 先把窗口画出来，再让 WebEngine 加载：首帧渲染较重，若在 show 之前就
        # setUrl，用户看到的是"点了没反应"——窗口和状态文案都要先出现。
        self.show()
        self.raise_()
        self.activateWindow()
        if self._view is not None and target:
            QTimer.singleShot(0, self, lambda t=target: self._load_target(t))

    def _load_target(self, target: str):
        """真正发起导航（由 :meth:`open_paper` 在窗口上屏后调用）。"""
        if self._view is None or not target:
            return
        try:
            self._view.setUrl(QUrl(target))
        except RuntimeError:
            pass    # 面板已销毁（标签页关闭）：忽略即可

    def _bind_profile_downloads(self):
        """订阅共享 profile 的下载请求（仅在面板可见时）。"""
        if self._profile is None or self._downloads_bound:
            return
        try:
            self._profile.downloadRequested.connect(self._on_download)
            self._downloads_bound = True
        except Exception as e:
            logger.debug("Cannot bind downloadRequested: %s", e)

    def _unbind_profile_downloads(self):
        if self._profile is None or not self._downloads_bound:
            return
        try:
            self._profile.downloadRequested.disconnect(self._on_download)
        except Exception as e:
            logger.debug("Cannot unbind downloadRequested: %s", e)
        self._downloads_bound = False

    def _bind_download_signals(self, download):
        """把"下载结束"的两条通路都接上。

        真实故障：只连 stateChanged，而 WebEngine 在某些版本/场景下不会为
        结束态补发这个信号，于是进度条永远停在"下载中"、导入也不再触发。
        isFinishedChanged（Qt ≥ 6.7）可用时一并接上，另外 _update_download_ui
        的轮询里还有一道兜底判断。
        """
        try:
            download.stateChanged.connect(
                lambda _state, d=download: self._on_download_state(d))
        except Exception as e:
            logger.debug("stateChanged unavailable: %s", e)
        try:
            download.isFinishedChanged.connect(
                lambda d=download: self._on_download_state(d))
        except Exception as e:
            logger.debug("isFinishedChanged unavailable: %s", e)

    def _on_download(self, download):
        # 只有可见面板会订阅该信号（见 _bind_profile_downloads），所以这里基本
        # 就是"用户正在浏览的那个面板"。仍然保留 visible 判断以防误订阅。
        visible = self.isVisible()
        try:
            name = download.downloadFileName() or "paper.pdf"
            logger.info("Download requested: %s (panel visible=%s)", name, visible)
            # 先握住句柄再 accept()：downloadRequested 的参数所有权在 PySide6 里
            # 交给接收方，没有 Python 引用时对象会被回收、下载随即中断——表现正是
            # 进度条卡在"下载中"。句柄同时用于轮询完成态。
            self._download = download
            if not visible:
                download.accept()
                return
            if not name.lower().endswith(".pdf"):
                # 用户可能点了补充材料；仍然接收，但明确提示不是论文正文
                self.sig_status.emit(f"Downloading {name} (not a PDF paper)...")
            os.makedirs(self._download_dir, exist_ok=True)
            download.setDownloadDirectory(self._download_dir)
            download.setDownloadFileName(name)
            download.accept()
            self._bind_download_signals(download)
            self.lbl_status.setText(f"Downloading {name}…")
            self._begin_download_ui(download)
            # 命中缓存等情况下下载可能已经结束，结束信号不会再来一次。
            if download.isFinished():
                QTimer.singleShot(250, self, lambda d=download: self._on_download_state(d))
        except Exception as e:
            logger.warning("Download request handling failed: %s", e)
            self.lbl_status.setText(f"Download failed: {e}")

    # ------------------------------------------------------------------ #
    #  下载进度（大小 / 速度 / 已用 / 剩余）
    # ------------------------------------------------------------------ #
    #: isFinished 已置位但文件还没落盘时，最多再等几拍轮询（500ms/拍）。
    _DL_MISSING_RETRIES = 10

    @staticmethod
    def _human_size(num) -> str:
        try:
            value = float(num or 0)
        except (TypeError, ValueError):
            return "0 B"
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} GB"

    @staticmethod
    def _human_time(seconds) -> str:
        try:
            total = max(0, int(round(float(seconds or 0))))
        except (TypeError, ValueError):
            return "0s"
        if total >= 3600:
            return f"{total // 3600}h{(total % 3600) // 60:02d}m"
        if total >= 60:
            return f"{total // 60}m{total % 60:02d}s"
        return f"{total}s"

    def _begin_download_ui(self, download):
        """开始一次下载：显示进度条并启动刷新定时器。"""
        self._download = download
        self._dl_active = True
        self._dl_started = time.monotonic()
        self._dl_last_bytes = 0
        self._dl_last_ts = self._dl_started
        self._dl_speed = 0.0
        self._dl_missing_tries = 0
        self.download_box.setVisible(True)
        self.progress_download.setRange(0, 0)      # 总大小未知：先显示忙碌态
        try:
            download.receivedBytesChanged.connect(
                lambda d=download: self._update_download_ui(d))
            download.totalBytesChanged.connect(
                lambda d=download: self._update_download_ui(d))
        except Exception as e:
            logger.debug("Download byte signals unavailable: %s", e)
        if self._dl_timer is None:
            self._dl_timer = QTimer(self)
            self._dl_timer.setInterval(500)
            self._dl_timer.timeout.connect(lambda: self._update_download_ui(self._download))
        self._dl_timer.start()
        self._update_download_ui(download)

    def _stop_download_ui(self):
        if self._dl_timer is not None:
            self._dl_timer.stop()

    def _update_download_ui(self, download):
        if download is None:
            return
        # 轮询兜底：结束信号可能缺失，这里每 500ms 主动查一次完成态，保证
        # 进度条/文案一定会切到"已完成 → 导入"。
        try:
            finished = bool(download.isFinished())
        except Exception:
            finished = False
        if finished:
            self._on_download_state(download)
            return
        try:
            got = int(download.receivedBytes() or 0)
            total = int(download.totalBytes() or 0)
        except Exception:
            return
        now = time.monotonic()
        dt = max(1e-3, now - self._dl_last_ts)
        if got > self._dl_last_bytes:
            instant = (got - self._dl_last_bytes) / dt
            # 指数滑动平均：瞬时速度跳变太大，直接显示会闪
            self._dl_speed = instant if self._dl_speed <= 0 else \
                self._dl_speed * 0.6 + instant * 0.4
        self._dl_last_bytes, self._dl_last_ts = got, now
        elapsed = now - self._dl_started

        if total > 0:
            pct = max(0, min(100, int(got * 100 / total)))
            self.progress_download.setRange(0, 100)
            self.progress_download.setValue(pct)
            remaining = (total - got) / self._dl_speed if self._dl_speed > 1024 else 0
            text = (f"{tr('Downloading…')} {self._human_size(got)} / "
                    f"{self._human_size(total)} ({pct}%) · "
                    f"{self._human_size(self._dl_speed)}/s · "
                    f"{tr('elapsed')} {self._human_time(elapsed)}")
            if remaining > 0:
                text += f" · {tr('remaining')} {self._human_time(remaining)}"
        else:
            self.progress_download.setRange(0, 0)
            text = (f"{tr('Downloading…')} {self._human_size(got)} · "
                    f"{self._human_size(self._dl_speed)}/s · "
                    f"{tr('elapsed')} {self._human_time(elapsed)}")
        self.lbl_download.setText(text)

    def _on_download_state(self, download, state=None):
        # 枚举名在不同 PySide6 版本里略有差异，统一用 isFinished() 判断完成；
        # 取消/中断同样 finished，但文件不存在，下面走失败提示。信号与轮询都会
        # 调到这里，所以整个函数必须可重入、幂等。
        try:
            if not download.isFinished():
                return
        except Exception:
            return
        if self._download is not None and download is not self._download:
            return                       # 非本面板接管的下载：不重复导入
        try:
            path = os.path.join(download.downloadDirectory(),
                                download.downloadFileName())
        except Exception as e:
            logger.debug("Cannot resolve the downloaded path: %s", e)
            return
        if path in getattr(self, "_finalized", set()):
            return
        if not os.path.isfile(path):
            # isFinished 会早于文件句柄落盘一点点；先让轮询再等几拍，避免把
            # "文件还没写完"误判成"下载失败"。
            tries = getattr(self, "_dl_missing_tries", 0) + 1
            self._dl_missing_tries = tries
            if tries <= self._DL_MISSING_RETRIES:
                return
            self._stop_download_ui()
            self.progress_download.setRange(0, 100)
            self.progress_download.setValue(0)
            self.lbl_download.setText(tr("Download failed."))
            self.lbl_status.setText("Download finished, but the file was not found.")
            return
        self._dl_missing_tries = 0
        self._stop_download_ui()
        # 下载完成：进度条填满 + 明确告知“已完成 / 用时 / 大小”，随后进入导入。
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        used = time.monotonic() - self._dl_started if self._dl_started else 0
        self.progress_download.setRange(0, 100)
        self.progress_download.setValue(100)
        self.lbl_download.setText(
            f"{tr('Download complete')} · {self._human_size(size)} · "
            f"{tr('elapsed')} {self._human_time(used)} · {tr('Importing…')}")
        self.lbl_status.setText(tr("Download complete. Importing into evidence…"))
        self._finalized = getattr(self, "_finalized", set())
        self._finalized.add(path)
        # 等文件句柄完全关闭一帧再解析/搬运
        QTimer.singleShot(250, self, lambda p=path: self._finalize_download(p))

    def _finalize_download(self, path: str):
        self.lbl_status.setText("Download detected. Reading citation metadata...")
        if self._dl_active:
            self.lbl_download.setText(
                f"{tr('Download complete')} · {tr('Reading metadata…')}")

        done = {"flag": False}

        def _emit(meta):
            # runJavaScript 的回调在页面跳转/销毁时可能永远不回来；用一次性标志
            # 保证只登记一次，并由下面的兜底定时器补发，绝不让导入卡在
            # "Reading metadata…"（用户看到的就是"下载完了但什么都没发生"）。
            if done["flag"]:
                return
            done["flag"] = True
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

        ran = False
        if self._view is not None:
            try:
                self._view.page().runJavaScript(_META_JS, _emit)
                ran = True
            except Exception as e:
                logger.debug("Metadata JS failed: %s", e)
        if ran:
            QTimer.singleShot(1500, self, lambda: _emit({}))
        else:
            _emit({})

    def _open_external(self):
        # 优先用地址栏当前地址：用户在浏览器里点进出版社页面后，想用系统浏览器
        # 接着看时，应该打开这一页，而不是最初那篇论文的 DOI 页。
        target = (self.url_edit.text().strip() or self._known.get("url")
                  or (f"https://doi.org/{self._known.get('doi')}" if self._known.get("doi") else ""))
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
        was_pending = path in self._pending_paths
        self._pending_paths.discard(path)
        # 路径归一化差异（软链接 / realpath / 大小写）会让 discard 落空，于是
        # "提交"永远等在那里。只剩一个待提交项时按唯一项清理，保证一定能收敛。
        if not was_pending and len(self._pending_paths) == 1:
            self._pending_paths.clear()
        if not ok:
            self._waiting_close = False
            self.lbl_status.setText(
                f"Full-text submission failed: {message or 'unknown error'}")
            if self._dl_active:
                self.lbl_download.setText(
                    f"{tr('Import failed')}: {message or 'unknown error'}")
            self.sig_submit_finished.emit(False, message or "Full-text submission failed")
            return
        if self._pending_paths:
            self.lbl_status.setText(
                f"Registered. Waiting for {len(self._pending_paths)} more PDF(s)...")
            return
        self.lbl_status.setText("Full text registered successfully.")
        if self._dl_active:
            self.lbl_download.setText(tr("Imported into the conversation evidence."))
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
        # 下载进度：加粗 + 主题色，让它从一堆状态文案里跳出来。
        self.lbl_download.setStyleSheet(
            f"color: {tm.color('accent')}; font-weight: {strong_weight_css()}; "
            f"font-size: 12px;")
        self.progress_download.setStyleSheet(
            f"QProgressBar {{ background-color: {tm.color('bg_input')}; "
            f"border: 1px solid {tm.color('border')}; border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background-color: {tm.color('accent')}; "
            f"border-radius: 2px; }}")
        # 地址栏：只读但要能一眼看出"这是一段可以复制的网址"，故保留边框与底色。
        self.url_edit.setStyleSheet(
            f"QLineEdit {{ background-color: {tm.color('btn_bg')}; "
            f"color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; "
            f"border-radius: 4px; padding: 4px 8px; font-size: 12px; "
            f"selection-background-color: {tm.color('accent')}; }}")
        for btn in (self.btn_clear, self.btn_close, self.btn_pick_pdf,
                    self.btn_challenge_open, self.btn_copy_url,
                    self.btn_reload, self.btn_open_ext):
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {tm.color('btn_bg')}; color: {tm.color('text_main')}; "
                f"border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 5px 12px; }}"
                f"QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}")
        # 图标按钮：每次换主题都按新主题色重新着色（SVG 走 CompositionMode_SourceIn）
        for btn, icon_name in ((self.btn_copy_url, "copy"),
                               (self.btn_reload, "refresh"),
                               (self.btn_open_ext, "open")):
            btn.setIcon(tm.icon(icon_name, "text_main"))
            btn.setIconSize(QSize(16, 16))
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {tm.color('btn_bg')}; "
                f"border: 1px solid {tm.color('border')}; border-radius: 4px; }}"
                f"QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}")
        # 徽标颜色随主题/当前状态刷新（_set_security 读 ThemeManager 取色）
        self._sync_security(self.url_edit.text())

    def showEvent(self, event):
        global _OPEN_PANELS
        super().showEvent(event)
        _OPEN_PANELS += 1
        # 只有可见面板订阅下载：避免历史面板抢接下载并登记进失效的对话。
        self._bind_profile_downloads()
        if self._view is not None:
            _apply_journal_proxy()

    def hideEvent(self, event):
        # 代理是应用级的：只有**最后一个**面板隐藏时才恢复，避免一个面板还开着、
        # 另一个关掉就把它的直连设置撤了。
        global _OPEN_PANELS
        _OPEN_PANELS = max(0, _OPEN_PANELS - 1)
        if _OPEN_PANELS == 0:
            _restore_application_proxy()
        # 已在途的下载已单独绑定结束信号与轮询，不受退订影响。
        self._unbind_profile_downloads()
        super().hideEvent(event)

    def closeEvent(self, event):
        # 用户点窗口 X 时也走“提交后关闭”：等待已下载 PDF 的入库结果。
        # 代理恢复交给 hideEvent（关闭必然触发隐藏），不在这里重复处理。
        if self._pending_paths and not self._allow_close:
            event.ignore()
            self.request_submit_and_close()
            return
        self._stop_download_watch()
        self._stop_download_ui()
        self._unbind_profile_downloads()
        super().closeEvent(event)
        submitted = any(bool(v and v[0]) for v in (self._submit_results or {}).values())
        try:
            self.sig_closed.emit(submitted)
        except Exception:
            pass


__all__ = ["LiteratureLoginPanel", "clear_literature_login",
           "shared_literature_profile", "warm_up_literature_browser"]
