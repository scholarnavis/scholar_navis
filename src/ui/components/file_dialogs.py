"""统一文件对话框入口（打开 / 多选打开 / 保存 / 选择目录）。

设计动机
--------
``QFileDialog`` 的静态便捷方法（``getOpenFileName`` / ``getSaveFileName`` 等）
无法定制侧边栏，一旦 Qt 回退到自绘对话框，用户看到的窗口既没有
Home / Desktop / Documents / Downloads 等快捷位置，也不会记住上次使用的目录，
与系统原生选择器的体验差距明显。

本模块把四类入口收敛到一处，遵循"原生优先、自绘兜底"的策略：

* **原生优先**：不设置 ``DontUseNativeDialog``，因此由
  :func:`src.core.platform_env.enable_native_file_dialogs` 启用的
  xdg-desktop-portal 选择器，以及 Windows / macOS 的系统选择器仍会被使用，
  它们自带快捷位置与最近访问记录。
* **自绘兜底**：当系统选择器不可用（门户后端缺失、打包后插件路径变化等）
  Qt 回退到自绘对话框时，用 :func:`standard_places` 注入标准目录作为侧边栏，
  尽量贴近原生体验。
* **记忆目录**：按"打开 / 保存"分别记住上次使用的目录，跨会话生效。
* **默认后缀**：保存对话框从过滤器推断默认扩展名，避免用户漏写后缀。

所有出入参数与 Qt 静态方法保持同构，便于调用点平滑迁移。
"""
from __future__ import annotations

import logging
import os
import re
from typing import List, Sequence, Tuple

from PySide6.QtCore import QSettings, QStandardPaths, QUrl
from PySide6.QtWidgets import QFileDialog, QWidget

logger = logging.getLogger(__name__)

__all__ = [
    "standard_places",
    "open_file_name",
    "open_file_names",
    "save_file_name",
    "existing_directory",
]

_SETTINGS_ORG = "ScholarNavis"
_SETTINGS_APP = "ScholarNavis"

#: side bar 展示的标准目录，元组顺序即展示顺序
_PLACE_LOCATIONS: Tuple = (
    QStandardPaths.StandardLocation.HomeLocation,
    QStandardPaths.StandardLocation.DesktopLocation,
    QStandardPaths.StandardLocation.DocumentsLocation,
    QStandardPaths.StandardLocation.DownloadLocation,
    QStandardPaths.StandardLocation.PicturesLocation,
    QStandardPaths.StandardLocation.MusicLocation,
    QStandardPaths.StandardLocation.MoviesLocation,
)

#: "打开 / 保存"分别记忆的目录键
_DIR_KEYS = {
    "open": "filedialog/last_open_dir",
    "save": "filedialog/last_save_dir",
}


def standard_places() -> List[QUrl]:
    """返回去重后的标准目录 ``QUrl`` 列表（Home / Desktop / Documents / ...）。

    未配置的目录会被系统回落（例如 Desktop 常常等于 Home），因此这里按
    ``realpath`` 去重，避免侧边栏出现重复条目。
    """
    seen = set()
    urls: List[QUrl] = []
    for location in _PLACE_LOCATIONS:
        try:
            path = QStandardPaths.writableLocation(location)
        except Exception:  # noqa: BLE001 - 平台差异不应影响对话框可用性
            logger.debug("standard location %s unavailable", location, exc_info=True)
            continue
        if not path:
            continue
        real = os.path.realpath(path)
        if real in seen or not os.path.isdir(real):
            continue
        seen.add(real)
        urls.append(QUrl.fromLocalFile(real))
    return urls


def _settings() -> QSettings:
    return QSettings(_SETTINGS_ORG, _SETTINGS_APP)


def _last_dir(kind: str) -> str:
    value = _settings().value(_DIR_KEYS[kind], "")
    return str(value) if value else ""


def _remember_dir(kind: str, file_path: str) -> None:
    """记录本次选择所在目录，供下次打开使用。"""
    if not file_path:
        return
    folder = os.path.dirname(os.path.abspath(file_path))
    if folder and os.path.isdir(folder):
        _settings().setValue(_DIR_KEYS[kind], folder)


def _split_filters(filter_str: str) -> List[str]:
    """把 ``"A (*.a);;B (*.b)"`` 拆成 ``setNameFilters`` 需要的列表。"""
    return [item.strip() for item in (filter_str or "").split(";;") if item.strip()]


def _infer_suffix(filters: Sequence[str]) -> str:
    """从过滤器里推断默认扩展名，用于保存时自动补全后缀。"""
    for pattern in filters:
        match = re.search(r"\*\.([0-9A-Za-z]+)", pattern)
        if match:
            return match.group(1).lower()
    return ""


def _prime_location(dialog: QFileDialog, directory: str, kind: str) -> None:
    """设置起始目录与预填文件名，优先级：调用方 > 上次使用 > Home。

    ``directory`` 兼容 Qt 语义：既可以是目录，也可以是"目录+默认文件名"。
    """
    raw = (directory or "").strip()
    start_dir = ""
    preset_name = ""
    if raw:
        expanded = os.path.expanduser(raw)
        if os.path.isdir(expanded):
            start_dir = expanded
        else:
            parent_dir = os.path.dirname(expanded)
            if parent_dir and os.path.isdir(parent_dir):
                start_dir = parent_dir
            preset_name = os.path.basename(expanded)

    if not start_dir:
        remembered = _last_dir(kind)
        if remembered and os.path.isdir(remembered):
            start_dir = remembered
    if not start_dir:
        home = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.HomeLocation)
        if home and os.path.isdir(home):
            start_dir = home

    if start_dir:
        dialog.setDirectory(start_dir)
    if preset_name:
        dialog.selectFile(preset_name)


def _build(parent: QWidget, caption: str, directory: str, filter_str: str,
           accept_mode, kind: str) -> QFileDialog:
    """构造一个带侧边栏、记忆目录与默认后缀的 ``QFileDialog``。

    ``accept_mode`` 为 ``AcceptOpen`` / ``AcceptSave``，沿用 Qt 枚举；
    这里显式不启用 ``DontUseNativeDialog``，保证系统原生选择器优先。
    """
    dialog = QFileDialog(parent, caption)
    dialog.setAcceptMode(accept_mode)
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, False)

    filters = _split_filters(filter_str)
    if filters:
        dialog.setNameFilters(filters)

    places = standard_places()
    if places:
        dialog.setSidebarUrls(places)

    _prime_location(dialog, directory, kind)

    if accept_mode == QFileDialog.AcceptMode.AcceptSave:
        suffix = _infer_suffix(filters)
        if suffix:
            dialog.setDefaultSuffix(suffix)

    return dialog


def _run(dialog: QFileDialog, kind: str) -> Tuple[List[str], str]:
    """执行对话框，返回 ``(选中文件列表, 实际使用的过滤器)``。"""
    accepted = dialog.exec() == QFileDialog.Accepted
    if not accepted:
        logger.debug("file dialog cancelled (kind=%s)", kind)
        return [], ""
    files = dialog.selectedFiles()
    selected_filter = dialog.selectedNameFilter()
    if files:
        _remember_dir(kind, files[0])
    logger.debug("file dialog accepted (kind=%s, count=%d, filter=%r)",
                 kind, len(files), selected_filter)
    return files, selected_filter


def open_file_name(parent: QWidget, caption: str, directory: str = "",
                   filter_str: str = "") -> Tuple[str, str]:
    """打开单个文件，返回 ``(路径, 过滤器)``；取消时路径为空字符串。"""
    dialog = _build(parent, caption, directory, filter_str,
                    QFileDialog.AcceptMode.AcceptOpen, "open")
    dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
    files, selected_filter = _run(dialog, "open")
    return (files[0] if files else ""), selected_filter


def open_file_names(parent: QWidget, caption: str, directory: str = "",
                    filter_str: str = "") -> Tuple[List[str], str]:
    """打开多个文件，返回 ``(路径列表, 过滤器)``；取消时列表为空。"""
    dialog = _build(parent, caption, directory, filter_str,
                    QFileDialog.AcceptMode.AcceptOpen, "open")
    dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
    return _run(dialog, "open")


def save_file_name(parent: QWidget, caption: str, directory: str = "",
                   filter_str: str = "") -> Tuple[str, str]:
    """选择保存路径，返回 ``(路径, 过滤器)``；取消时路径为空字符串。"""
    dialog = _build(parent, caption, directory, filter_str,
                    QFileDialog.AcceptMode.AcceptSave, "save")
    dialog.setFileMode(QFileDialog.FileMode.AnyFile)
    files, selected_filter = _run(dialog, "save")
    return (files[0] if files else ""), selected_filter


def existing_directory(parent: QWidget, caption: str, directory: str = "") -> str:
    """选择一个已存在的目录，返回路径；取消时为空字符串。"""
    dialog = _build(parent, caption, directory, "",
                    QFileDialog.AcceptMode.AcceptOpen, "open")
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
    files, _ = _run(dialog, "open")
    return files[0] if files else ""