"""生成随发布产物分发的第三方许可声明。

为什么需要
----------
发布产物内嵌了大量第三方代码，其中含 copyleft 组件：

* PyMuPDF / pymupdf4llm —— AGPL-3.0（或 Artifex 商业授权）
* PySide6 / shiboken6 / Qt —— LGPL-3.0（三选一许可里的 LGPL 分支）
* chardet —— LGPL-2.1
* certifi / orjson / tqdm —— MPL-2.0

AGPL-3 §4/§6 与 LGPL-3 §4 都要求随二进制向接收者提供相应许可文本与声明，而
PyInstaller 默认只打包代码、不带许可文件，因此必须在打包时显式补上。

做法
----
1. :func:`collect_license_files` 把每个已安装发行包自带的许可正文复制出来
   （PEP 639 之后多数 wheel 会带 ``*.dist-info/licenses/``）；
2. :func:`render_notices` 生成 Markdown 索引（组件 / 版本 / 许可证 / 全文位置）；
3. :func:`stage_notices` 串起来，返回交给 PyInstaller ``--add-data`` 的清单。

少数 wheel（PySide6、shiboken6、onnxruntime、tokenizers 等）**不自带**许可正文，
因此仓库里另放了 ``LICENSES/``（LGPL-3.0 与 GPL-3.0 的官方全文），由本模块一并
打进产物；``render_notices`` 会明确列出"未随 wheel 提供正文"的包，便于人工核对。
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

logger = logging.getLogger("BuildSupport.Notices")

__all__ = [
    "MAX_LICENSE_FILE_BYTES",
    "REPO_LICENSE_DIR",
    "collect_license_files",
    "render_notices",
    "stage_notices",
]

#: 单个许可文件大小上限：再大基本是误判（把 .so 之类当许可文件收进来）
MAX_LICENSE_FILE_BYTES = 512 * 1024

#: 仓库内随源码提供的官方许可全文目录（wheel 不自带的那几份）
REPO_LICENSE_DIR = "LICENSES"

_LICENSE_NAME_RE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE)", re.I)


def _is_license_entry(parts: List[str]) -> bool:
    """按路径片段判断是否许可正文。

    只认文件名前缀或 ``licenses/`` 目录，不按内容猜：一旦放宽，``AUTHORS``
    （实测单个 54KB）、``.so`` 之类都会被收进来，纯噪音。
    """
    return bool(_LICENSE_NAME_RE.match(parts[-1])) or \
        "licenses" in [p.lower() for p in parts[:-1]]


def _license_id(meta) -> str:
    """按 PEP 639 → 旧 License 字段 → Classifier 的顺序取许可证标识。"""
    expr = (meta.get("License-Expression") or "").strip()
    if expr:
        return expr
    raw = (meta.get("License") or "").strip()
    if raw and "\n" not in raw and len(raw) <= 80:
        return raw
    for cls in meta.get_all("Classifier") or []:
        if cls.startswith("License :: "):
            return cls.split("::")[-1].strip()
    return "(not declared)"


def collect_license_files(dest: Path) -> Tuple[int, List[str], int]:
    """把各发行包自带的许可正文复制到 ``dest/<发行包>-<版本>/``。

    按 RECORD 清单精确定位，而不是遍历发行包根目录：``locate_file("")`` 对可编辑
    安装或元数据不规范的分发会返回整个 site-packages / 仓库根，rglob 下去会把别的
    包的 ``licenses/`` 子树一并收进来（实测虚增到 5.5 万个文件 / 41MB，而且会让
    "无许可正文的包"统计错成 0）。

    Returns:
        ``(复制成功的文件数, 未随 wheel 提供任何许可正文的包名列表, 自带正文的包数)``
    """
    import importlib.metadata as md

    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    with_text = 0
    no_text: List[str] = []

    for dist in md.distributions():
        name = dist.metadata["Name"]
        if not name:
            continue

        files: List[Path] = []
        for entry in dist.files or []:
            parts = str(entry).replace("\\", "/").split("/")
            if not _is_license_entry(parts):
                continue
            try:
                path = Path(dist.locate_file(entry))
            except Exception:  # noqa: BLE001 - 个别脏元数据会让定位失败
                continue
            if not path.is_file():
                continue
            try:
                if path.stat().st_size > MAX_LICENSE_FILE_BYTES:
                    logger.debug("Skip oversized license-like file: %s", path)
                    continue
            except OSError:
                continue
            files.append(path)

        if not files:
            no_text.append(name)
            continue

        with_text += 1
        target = dest / f"{name}-{dist.version}"
        target.mkdir(parents=True, exist_ok=True)
        for path in files:
            try:
                shutil.copy2(path, target / path.name)
                copied += 1
            except OSError as exc:
                logger.warning("Failed to copy %s: %s", path, exc)

    logger.info("Collected %d license file(s) from %d distribution(s); %d ship no text.",
                copied, with_text, len(no_text))
    return copied, sorted(no_text), with_text


def render_notices(app_name: str, version: str, source_url: str,
                   total_distributions: int, license_file_count: int,
                   missing_text: Sequence[str] = (),
                   licenses_dir: str = "THIRD_PARTY_LICENSES") -> str:
    """生成 ``THIRD_PARTY_NOTICES.md`` 的内容。"""
    lines: List[str] = [
        f"# Third-Party Notices — {app_name} {version}",
        "",
        "This distribution bundles third-party software. The full license texts that "
        f"ship with those packages are collected under `{licenses_dir}/`, grouped by "
        "distribution. With PyInstaller's `--onedir` layout these files live in the "
        f"application's `_internal/` directory (i.e. `_internal/LICENSE`, "
        f"`_internal/{licenses_dir}/`, `_internal/LICENSES/`).",
        "",
        "## Project license",
        "",
        "Scholar Navis itself is licensed under the **GNU Affero General Public License "
        "v3.0**; the full text is in `LICENSE`, and the corresponding source is available at "
        f"<{source_url}>.",
        "",
        "## Components resolved by the build environment",
        "",
        f"The build environment resolved **{total_distributions}** distributions and "
        f"**{license_file_count}** license file(s) were collected from their package "
        "metadata. Which of them end up frozen into the executable is decided by "
        "PyInstaller's import analysis, so this list is a superset of what ships.",
        "The in-application *About → Licenses* dialog lists the components with functional "
        "impact together with their license identifiers.",
        "",
        "## Licenses that a wheel does not ship",
        "",
        "The following distributions declare a license but provide no license text file "
        "inside their wheel. Their canonical texts are included in this distribution under "
        "`LICENSES/` when they are copyleft (LGPL-3.0, GPL-3.0); for the rest refer to the "
        "identifier's canonical page at <https://spdx.org/licenses/>.",
        "",
    ]
    if missing_text:
        for name in missing_text:
            lines.append(f"* `{name}`")
    else:
        lines.append("*(none)*")

    lines += [
        "",
        "## Not redistributed",
        "",
        "Some components are required at run time but are **not** bundled and therefore "
        "carry no redistribution obligation here:",
        "",
        "* **R / Rscript** and its packages (ggplot2, dplyr, tidyr, scales, viridis, "
        "patchwork, ragg, RColorBrewer, pheatmap, ggpubr, ggrepel, cowplot) — detected on "
        "the user's machine and invoked as separate processes. Several of them are GPL-2 / "
        "GPL-3; if this project ever starts shipping R or those packages, the corresponding "
        "source must be offered as well.",
        "* **CUDA / TensorRT** runtimes — loaded from the host system by ONNX Runtime, "
        "not redistributed.",
        "* **Machine-learning model weights** — downloaded by the user at run time; each "
        "model carries its own license.",
        "",
        "## Notes",
        "",
        "* `PyInstaller`'s bootloader (GPL-2.0 with the bootloader exception) is embedded "
        "in the executable; the exception explicitly permits bundling applications under "
        "other licenses.",
        "* This distribution is frozen with PyInstaller `--onedir`, so the LGPL-covered "
        f"Qt libraries stay as separate, replaceable files (required by LGPL-3 §4).",
        "",
    ]
    return "\n".join(lines)


def stage_notices(stage_dir, *, app_name: str, version: str, source_url: str,
                  repo_root=".") -> Dict[str, object]:
    """生成声明与许可全文，返回 PyInstaller ``--add-data`` 所需的 (源, 目标) 清单。

    Args:
        stage_dir:        中间产物目录（建议放在 ``build/`` 下，已被 gitignore）。
        app_name/version: 写入声明头部。
        source_url:       Corresponding Source 地址（AGPL-3 §13）。
        repo_root:        仓库根目录，用于定位 ``LICENSE`` 与 ``LICENSES/``。

    Returns:
        ``{"add_data": [(src, dest), ...], "notices": 路径, "license_files": int,
        "missing_text": [包名...]}``
    """
    stage = Path(stage_dir)
    texts_dir = stage / "THIRD_PARTY_LICENSES"
    if texts_dir.exists():
        shutil.rmtree(texts_dir)

    count, missing, with_text = collect_license_files(texts_dir)

    root = Path(repo_root)
    notices_path = stage / "THIRD_PARTY_NOTICES.md"
    notices_path.write_text(
        render_notices(app_name, version, source_url, with_text + len(missing), count, missing),
        encoding="utf-8",
    )

    add_data: List[Tuple[str, str]] = [(str(notices_path), ".")]
    if texts_dir.exists() and any(texts_dir.iterdir()):
        add_data.append((str(texts_dir), texts_dir.name))

    license_file = root / "LICENSE"
    if license_file.is_file():
        add_data.append((str(license_file), "."))
    else:
        logger.warning("LICENSE file not found at %s; the bundled artifact would ship "
                       "without the project's own license text.", license_file)

    repo_licenses = root / REPO_LICENSE_DIR
    if repo_licenses.is_dir() and any(repo_licenses.iterdir()):
        add_data.append((str(repo_licenses), REPO_LICENSE_DIR))
    else:
        logger.warning("%s/ not found; LGPL-3.0 / GPL-3.0 full texts may be missing "
                       "(PySide6's wheel does not ship them).", repo_licenses)

    logger.info("Staged %d add-data entr(ies); %d license file(s) collected.",
                len(add_data), count)
    return {
        "add_data": add_data,
        "notices": str(notices_path),
        "license_files": count,
        "missing_text": missing,
    }
