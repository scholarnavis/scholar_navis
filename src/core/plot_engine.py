"""
R Plotting Engine
=================

Renders charts via a sandboxed R process for the Scholar Navis agent.

The plotting pipeline is deliberately separated from the LLM:

    * The LLM only ever sees a *preview* (first N rows + schema) of the data,
      not the full payload. It chooses a chart type and writes R code.
    * The R code reads the full data from a local CSV file (referenced by path),
      never embedding the data inline.
    * Output is rendered to SVG (vector, journal-friendly) + PNG (preview) +
      PDF (print) simultaneously, since R runs locally on the user's machine.

Security model (sandbox):
    * A prelude script neutralizes dangerous R builtins (system/shell/file/
      source/download.file/writeLines/...).
    * The process runs in an isolated temp working directory with a hard
      timeout, output-size cap, and no window on Windows.

Design principles:
    * High cohesion: data marshalling + R execution + output collection live
      here; the LLM-facing skill is a thin wrapper in ``academic_agent``.
    * Low coupling: only depends on ``r_engine``, ``plot_styles`` and
      stdlib + subprocess.
    * Single source of truth for looks: themes, journal palettes, per-chart-type
      defaults and canvas sizes come from :mod:`src.core.plot_styles` (distilled
      from a reference SCI-figure library), so the LLM-facing descriptions and
      the generated R code can never drift apart.
    * Performance: single Rscript invocation per plot; no per-row Python I/O.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 默认样式（主题 / 配色 / 画布尺寸 / 各图型风格契约）集中在本模块之外的单一样式
# 注册表里，本引擎只负责把它翻译成安全的 ggplot2 代码，避免样式知识散落多处。
from src.core import plot_styles

logger = logging.getLogger("Core.PlotEngine")

# Registry persisted next to plot outputs so charts can be modified in later
# turns / sessions (registry survives runtime object recreation).
PLOT_REGISTRY_FILENAME = "plot_registry.json"
_registry_lock = threading.Lock()

# ---------------------------------------------------------------------- #
#  Output location
# ---------------------------------------------------------------------- #
#: 绘图产物（PNG/SVG/PDF/CSV/脚本 + plot_registry.json）默认落在项目目录下，
#: **不能**用系统临时目录：
#:   * NixOS 上本应用会通过 ``steam-run`` 重启自身（见 platform_env），
#:     steam-run 的 /tmp 是私有的、随进程退出即消失；
#:   * 其余平台上 /tmp 也会被 systemd-tmpfiles 定期清理、或随重启清空。
#: 一旦产物消失，历史对话里的图卡片就只剩一个失效路径——预览加载不出来、
#: 双击也打不开（表现为"双击浏览图片突然不工作了"）。目录解析见
#: :mod:`src.core.output_paths`。

# ---------------------------------------------------------------------- #
#  R package allow-list
# ---------------------------------------------------------------------- #
# Core plotting (always expected).
CORE_R_PACKAGES = ["ggplot2", "dplyr", "tidyr", "scales", "RColorBrewer"]

# Extended packages for complex figures.
EXTENDED_R_PACKAGES = [
    "pheatmap",        # clustered heatmaps
    "ggrepel",         # non-overlapping labels
    "ggpubr",          # publication-ready multi-panel + stats annotations
    "ggdendro",        # dendrograms
    "ggridges",        # ridge / density plots
    "ggalluvial",      # alluvial / flow diagrams
    "ggcorrplot",      # correlation matrices
    "viridis",         # perceptually uniform palettes
    "patchwork",       # multi-panel composition
    "cowplot",         # themeing + multi-panel
    "ggforce",         # advanced shapes / facets
    "igraph",          # network layout (optional)
]

ALLOWED_R_PACKAGES = CORE_R_PACKAGES + EXTENDED_R_PACKAGES

# Dangerous R functions neutralized before running user/LLM code.
# Note: base ``read.csv``/``read.table`` are blocked; the prelude exposes a
# safe ``.data`` (already loaded from the designated file) and a read-only
# ``utils::read.csv`` via namespace is left for the final data load. The LLM's
# code must NOT call these raw readers on arbitrary paths.
_BLOCKED_R_FUNCTIONS = [
    "system", "system2", "shell", "shell.exec",
    "file", "file.choose", "file.create", "file.remove", "file.rename",
    "unlink", "dir.create", "dir.exists", "list.files", "list.dirs",
    "source", "sys.source", "load", "save", "saveRDS", "readRDS",
    "download.file", "url", "curl", "readLines", "writeLines", "write",
    "write.table", "write.csv", "write.csv2",
    "setwd", "getwd",
    "Sys.getenv", "Sys.setenv", "Sys.getpid", "Sys.sleep",
]

# ---------------------------------------------------------------------- #
#  Sandbox prelude
# ---------------------------------------------------------------------- #
# The prelude blocks dangerous base builtins and pre-loads the designated data
# file into ``.data`` (using the namespace-qualified reader so the ban does not
# affect it). The LLM's code simply uses ``.data`` — no file I/O is needed.
_SANDBOX_PRELUDE = r'''
# --- Scholar Navis sandbox prelude (auto-generated) ---
safe_data_path <- commandArgs(trailingOnly = TRUE)[1]
if (is.na(safe_data_path) || safe_data_path == "") {
  stop("No data file supplied.")
}
if (!file.exists(safe_data_path)) {
  stop("Data file not found.")
}
# NOTE: we shadow dangerous functions in the *global* environment rather than
# overwriting them in baseenv(). Since R >= 4.0 many base bindings (e.g.
# `system`) are locked, so `assign(..., envir = baseenv())` aborts with
# "cannot change value of locked binding". The Rscript body executes at the top
# level (globalenv), so its lookup chain hits these shadowed functions first —
# same protection, but compatible with every R version.
.blocked <- function(name) {
  assign(name, function(...) stop(paste0("Function '", name, "' is disabled in sandbox.")),
         envir = globalenv())
}
.blocked_names <- c(%BLOCKED_LIST%)
for (.f in .blocked_names) {
  if (exists(.f, envir = baseenv(), inherits = FALSE)) {
    .blocked(.f)
  }
}
# Load the core plotting packages; fail loudly with install guidance when one
# is missing (ggplot2 must be loaded before the generated spec code runs).
%PACKAGE_LOAD%
.data <- utils::read.csv(safe_data_path, check.names = FALSE, stringsAsFactors = FALSE)
rm(.blocked, .blocked_names, .f)
# --- end prelude ---
'''

# Data preview defaults (only this many rows are ever shown to the LLM).
DEFAULT_PREVIEW_ROWS = 5
DEFAULT_PREVIEW_COLS = 12

# Subprocess safety limits.
R_TIMEOUT_SECONDS = 120
MAX_OUTPUT_BYTES = 256 * 1024


# ---------------------------------------------------------------------- #
#  Data structures
# ---------------------------------------------------------------------- #
@dataclass
class PlotData:
    """A dataset marshalled to disk for plotting, plus its preview."""

    data_path: str
    """Absolute path to the CSV file the R script should read."""

    preview: List[Dict[str, Any]]
    """First N rows as JSON-serializable dicts (shown to the LLM)."""

    columns: List[str]
    """Ordered column names."""

    column_types: Dict[str, str]
    """Column name -> inferred type ('numeric' | 'integer' | 'logical' | 'character')."""

    total_rows: int


@dataclass
class PlotResult:
    """Outcome of a plotting run (success or failure)."""

    success: bool
    svg_path: str = ""
    png_path: str = ""
    pdf_path: str = ""
    script_path: str = ""
    # Path to the pure plotting R code (no sandbox prelude / output directives).
    # Used to re-render modified versions of the chart in later turns.
    code_path: str = ""
    stdout: str = ""
    stderr: str = ""
    error_message: str = ""
    duration_ms: int = 0
    #: Canvas actually used (inches); persisted so a later ``modify_chart``
    #: re-render keeps the figure size of the original chart.
    figure_size: Optional[Tuple[float, float]] = None


# ---------------------------------------------------------------------- #
#  Data marshalling (JSON/table -> CSV + preview)
# ---------------------------------------------------------------------- #
class PlotEngine:
    """Stateless entry point: marshals data, runs R, collects outputs."""

    def __init__(self, output_dir: Optional[str] = None):
        self._output_dir = output_dir
        # 解析结果缓存：目录探测（含写权限自检）只在首个图渲染时做一次。
        self._resolved_dir: Optional[str] = None

    # -- output directory ----------------------------------------------- #
    def _ensure_output_dir(self) -> str:
        if self._output_dir:
            os.makedirs(self._output_dir, exist_ok=True)
            return self._output_dir
        if self._resolved_dir:
            return self._resolved_dir
        self._resolved_dir = self._default_output_dir()
        return self._resolved_dir

    @staticmethod
    def _default_output_dir() -> str:
        """持久化的绘图输出目录（不可写时退回系统临时目录）。

        默认 ``<BASE_DIR>/output/r_plots``：绘图产物必须持久保存，否则重启或
        系统清理临时目录后，历史对话里的图卡片就只剩一个失效路径（预览加载
        失败、双击打不开）。目录解析与兜底逻辑统一在
        :mod:`src.core.output_paths` 中实现。
        """
        from src.core.output_paths import plot_output_dir
        return plot_output_dir()

    # -- plot registry persistence -------------------------------------- #
    # The registry maps ``plot_id -> {script_path, code_path, data_path, ...}``
    # so charts drawn in earlier turns / sessions can be edited with natural
    # language later. Persisted next to the plot outputs so it survives runtime
    # object recreation (the agent runtime is rebuilt per turn).
    def _registry_path(self) -> str:
        return os.path.join(self._ensure_output_dir(), PLOT_REGISTRY_FILENAME)

    def save_plot_registry(self, registry: Dict[str, Any]) -> None:
        """Persist the registry to disk, dropping entries whose files vanished."""
        with _registry_lock:
            path = self._registry_path()
            pruned = {}
            for pid, info in registry.items():
                if not isinstance(info, dict):
                    continue
                # Drop entries whose key artifacts no longer exist on disk.
                data_ok = not info.get("data_path") or os.path.exists(info["data_path"])
                script_ok = not info.get("script_path") or os.path.exists(info["script_path"])
                if data_ok and script_ok:
                    pruned[pid] = info
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(pruned, f, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
                logger.info(f"[plot] registry persisted: {len(pruned)} chart(s) -> {path}")
            except OSError as e:
                logger.warning(f"[plot] could not persist registry: {e}")

    def load_plot_registry(self) -> Dict[str, Any]:
        """Load the registry from disk (empty dict when absent/corrupt)."""
        with _registry_lock:
            path = self._registry_path()
            if not os.path.exists(path):
                return {}
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError) as e:
                logger.warning(f"[plot] could not load registry: {e}")
                return {}

    # -- data marshalling ----------------------------------------------- #
    def marshal_data(self, records: List[Dict[str, Any]], job_id: str) -> PlotData:
        """Write ``records`` (list of dicts) to a CSV and build its preview.

        ``job_id`` uniquifies the file so concurrent plots never collide.
        """
        if not records:
            raise ValueError("No data rows provided for plotting.")

        # 1) Unify columns across records (handle ragged dicts).
        columns: List[str] = []
        for rec in records:
            if isinstance(rec, dict):
                for k in rec.keys():
                    if k not in columns:
                        columns.append(k)
        if not columns:
            raise ValueError("Data records contain no columns.")

        # 2) Infer column types from the first non-empty value.
        column_types = self._infer_types(records, columns)

        # 3) Write CSV.
        out_dir = self._ensure_output_dir()
        safe_job = "".join(c for c in job_id if c.isalnum() or c in "-_")
        data_path = os.path.join(out_dir, f"{safe_job}_data.csv")

        import csv
        with open(data_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            for rec in records:
                if isinstance(rec, dict):
                    writer.writerow([rec.get(c, "") for c in columns])

        # 4) Build preview (first N rows).
        preview = []
        for rec in records[:DEFAULT_PREVIEW_ROWS]:
            if isinstance(rec, dict):
                preview.append({c: rec.get(c, "") for c in columns})

        return PlotData(
            data_path=data_path,
            preview=preview,
            columns=columns,
            column_types=column_types,
            total_rows=len(records),
        )

    def load_plot_data(self, data_path: str) -> PlotData:
        """Reconstruct a :class:`PlotData` from an existing CSV on disk.

        Used when re-rendering a previously drawn chart (e.g. after the user
        edits the R source or asks the AI to modify it): the data file is
        already marshalled, so we read it back without re-writing a new CSV.
        """
        if not data_path or not os.path.exists(data_path):
            raise ValueError(f"Data file not found: {data_path}")
        import csv
        with open(data_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            records = [dict(row) for row in reader]
        if not records:
            raise ValueError("Data file contains no rows.")
        columns = list(records[0].keys())
        column_types = self._infer_types(records, columns)
        preview = records[:DEFAULT_PREVIEW_ROWS]
        return PlotData(
            data_path=data_path,
            preview=preview,
            columns=columns,
            column_types=column_types,
            total_rows=len(records),
        )

    @staticmethod
    def _infer_types(records: List[Dict[str, Any]], columns: List[str]) -> Dict[str, str]:
        types: Dict[str, str] = {}
        for col in columns:
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                v = rec.get(col)
                if v is None or v == "":
                    continue
                if isinstance(v, bool):
                    types[col] = "logical"
                elif isinstance(v, int):
                    types[col] = "integer"
                elif isinstance(v, float):
                    types[col] = "numeric"
                elif isinstance(v, str):
                    s = v.strip()
                    try:
                        float(s)
                        types[col] = "numeric"
                    except ValueError:
                        types[col] = "character"
                else:
                    types[col] = "character"
                break
            else:
                types[col] = "character"
        return types

    # -- R execution ---------------------------------------------------- #
    def run_plot(self, r_code: str, plot_data: PlotData, job_id: str,
                 extra_packages: Optional[List[str]] = None,
                 figure_size: Optional[Tuple[float, float]] = None) -> PlotResult:
        """Execute ``r_code`` (plotting only) against ``plot_data`` in a sandbox.

        ``extra_packages`` are loaded on demand for chart types that need them
        (e.g. pheatmap, ggpubr, ggridges).

        ``figure_size`` is the ``(width, height)`` in inches for the three
        output devices; ``None`` falls back to the chart-style default from
        :mod:`src.core.plot_styles` (journal sizes taken from the reference
        R figure library).

        Returns a :class:`PlotResult` carrying the three output paths on
        success, or a human-readable error on failure.
        """
        from src.core.r_engine import get_r_engine

        engine = get_r_engine()
        info = engine.detect()
        if not info.get("available"):
            logger.warning(f"[plot {job_id}] R not available. {engine.install_guidance()}")
            return PlotResult(
                success=False,
                error_message=engine.install_guidance(),
            )

        # Validate that the R code references only the designated data file,
        # and inject the safe reader + output paths.
        out_dir = self._ensure_output_dir()
        safe_job = "".join(c for c in job_id if c.isalnum() or c in "-_")
        base = os.path.join(out_dir, safe_job)
        svg_path = base + ".svg"
        png_path = base + ".png"
        pdf_path = base + ".pdf"

        # 画布尺寸：显式传入优先（例如修改图时沿用原图尺寸），否则用图型默认值。
        canvas = tuple(figure_size) if figure_size else plot_styles.DEFAULT_CANVAS

        script = self._compose_script(
            r_code, plot_data, svg_path, png_path, pdf_path,
            extra_packages=extra_packages, figure_size=canvas)

        # Write script to the isolated dir.
        script_path = base + ".R"
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)

        # Persist the *pure plotting* code (no sandbox prelude / output
        # directives) so a later turn can ask the LLM to edit only this part
        # and re-render against the same data. Keeps the edit loop safe:
        # the prelude is never duplicated, output paths always fresh.
        code_path = base + ".code.R"
        try:
            with open(code_path, "w", encoding="utf-8") as f:
                f.write(r_code)
        except OSError:
            logger.warning(f"[plot {job_id}] Could not persist pure R code to {code_path}")
            code_path = ""

        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        start = time.time()
        logger.info(
            f"[plot {job_id}] Executing R script: {script_path} | "
            f"executable={info.get('executable')} | data={plot_data.data_path} | "
            f"canvas={PlotEngine._inch(canvas[0])}x{PlotEngine._inch(canvas[1])} in"
        )
        try:
            proc = subprocess.run(
                [info["executable"], script_path, plot_data.data_path],
                capture_output=True,
                text=True,
                timeout=R_TIMEOUT_SECONDS,
                cwd=out_dir,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired:
            logger.error(
                f"[plot {job_id}] R plotting timed out (>{R_TIMEOUT_SECONDS}s). "
                f"script={script_path}"
            )
            return PlotResult(
                success=False,
                error_message=f"R plotting timed out (>{R_TIMEOUT_SECONDS}s). Simplify the plotting code.",
            )
        except OSError as e:
            logger.error(f"[plot {job_id}] Failed to launch R: {e}")
            return PlotResult(success=False, error_message=f"Failed to launch R: {e}")

        duration_ms = int((time.time() - start) * 1000)
        stdout = (proc.stdout or "")[:MAX_OUTPUT_BYTES]
        stderr = (proc.stderr or "")[:MAX_OUTPUT_BYTES]

        # Success = R exited 0 AND the SVG was actually produced.
        if proc.returncode == 0 and os.path.exists(svg_path):
            # Mirror the R process output into the application log so that both
            # the dev-mode log panel and the log file show what R actually did.
            # Warnings on success (e.g. "Removed N rows containing missing
            # values", dev.copy issues) are often the only clue to an empty
            # (white) plot, so they are logged at WARNING level.
            logger.info(
                f"[plot {job_id}] OK in {duration_ms} ms | rc=0 | "
                f"svg={os.path.basename(svg_path)} ({os.path.getsize(svg_path)} bytes)"
            )
            if stdout.strip():
                logger.info(f"[plot {job_id}] R stdout:\n{stdout.rstrip()}")
            if stderr.strip():
                logger.warning(f"[plot {job_id}] R warnings/stderr:\n{stderr.rstrip()}")
            return PlotResult(
                success=True,
                svg_path=svg_path,
                png_path=png_path if os.path.exists(png_path) else "",
                pdf_path=pdf_path if os.path.exists(pdf_path) else "",
                script_path=script_path if os.path.exists(script_path) else "",
                code_path=code_path if code_path and os.path.exists(code_path) else "",
                stdout=stdout,
                stderr=stderr,
                duration_ms=duration_ms,
                figure_size=canvas,
            )

        # Failure: surface the R error to the UI and log the full output.
        err = stderr.strip() or stdout.strip()
        if not err:
            err = f"R exited with code {proc.returncode} but produced no image."
        logger.error(
            f"[plot {job_id}] FAILED in {duration_ms} ms | rc={proc.returncode} | "
            f"script={script_path}\n"
            f"--- R stderr ---\n{stderr.rstrip() or '(empty)'}\n"
            f"--- R stdout ---\n{stdout.rstrip() or '(empty)'}"
        )
        return PlotResult(
            success=False,
            error_message=err,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
        )

    # -- spec -> R code translation -------------------------------------- #
    # Supported declarative chart types. The LLM only declares the chart type
    # and column roles; the system generates the actual (safe) ggplot2 code.
    # These cover the vast majority of common scientific figures.
    SUPPORTED_CHART_TYPES = {
        # scatter / point-based
        "scatter", "bubble", "volcano", "line", "area",
        # categorical / distribution
        "bar", "boxplot", "violin", "histogram", "density", "dotplot",
        # matrix / heatmap
        "heatmap", "corrplot",
        # composition
        "pie", "donut",
        # advanced / multi-panel
        "ridge", "alluvial", "network",
    }

    # Chart types that need an extended R package (loaded on demand).
    _EXTENDED_TYPE_PACKAGES = {
        "heatmap": ["pheatmap"],
        "corrplot": ["ggcorrplot"],
        "ridge": ["ggridges"],
        "alluvial": ["ggalluvial"],
        "network": ["igraph"],
        "pie": ["scales"],
        "boxplot": ["ggpubr"],
        "violin": ["ggpubr"],
        # Point labels use repel-able text (ggrepel) so labels never overlap:
        # it must be loaded in the sandbox before geom_text_repel() is evaluated.
        "bubble": ["ggrepel"],
        "scatter": ["ggrepel"],
        "volcano": ["ggrepel"],
    }

    # Journal style presets, per-chart-type defaults, palettes and base themes
    # all live in ``src.core.plot_styles`` — the single source of truth distilled
    # from the reference library of 50 SCI figures. An explicit ``theme`` /
    # ``palette`` argument (the user's requirement) always wins over a default.
    @staticmethod
    def _academic_theme_block(theme: str = "") -> str:
        """R theme expression for a base-theme id (see plot_styles.THEME_BASE)."""
        return plot_styles.theme_block(theme)

    @staticmethod
    def _academic_palette(palette: str = "") -> str:
        """R colour vector for a discrete journal palette."""
        return plot_styles.palette_vector(palette)

    @staticmethod
    def _inch(value: float) -> str:
        """Format inches for an R device call (7.0 -> '7', 5.5 -> '5.5')."""
        f = float(value)
        return str(int(f)) if f.is_integer() else f"{f:g}"

    @staticmethod
    def _chain(head: str, layers: List[str]) -> str:
        """Append ggplot2 layers with ``+`` while dropping empty ones.

        An empty layer would leave a dangling ``+`` and R would parse the next
        line as a *unary* plus, which fails with "invalid argument to unary
        operator" as soon as the operand is not numeric (e.g. ``labs(...)``).
        """
        parts = [p.strip() for p in layers if p and p.strip()]
        return head + "".join(f" +\n  {p}" for p in parts)

    @staticmethod
    def _manual_scale(aesthetic: str, palette: str, column: str,
                      n_levels: int = 0) -> str:
        """Discrete colour scale bound to a data column (plot_styles helper).

        ``n_levels`` lets the unified academic palette be generated with the
        exact number of colours the figure needs (low-chroma HCL wheel beyond
        10 categories); the R side still guards the length with
        ``rep(..., length.out = ...)`` so a miscount can never abort the render
        with "Insufficient values in manual scale".
        """
        return plot_styles.manual_scale_expr(aesthetic, palette, column, n_levels)

    @staticmethod
    def top_n_r_block(order_by: str, n: int) -> str:
        """R block keeping only the ``n`` highest rows of a numeric column.

        Used on term-style figures (enrichment bars / bubbles) where charting
        every term would make the axis labels unreadable — the same idea as the
        reference scripts' ``showNum = 30``.
        """
        col = (order_by or "").replace('"', "").replace("\\", "")
        if not col or n <= 0:
            return ""
        return (
            "# --- keep only the top N terms so axis labels stay readable ---\n"
            f".data <- .data[order(-.data[[\"{col}\"]]), , drop = FALSE]\n"
            f".data <- utils::head(.data, {int(n)})\n"
        )

    @staticmethod
    def _color_label(column: str) -> str:
        """Legend title of a colour gradient (FDR family -> 'FDR')."""
        c = (column or "").strip()
        if c.lower() in ("fdr", "padj", "adj_p", "adjp", "qvalue", "q_value"):
            return "FDR"
        return c

    @staticmethod
    def spec_to_r_code(chart_type: str, x: str, y: str, size: str = "",
                       label: str = "", title: str = "Chart",
                       log_transform_y: bool = False,
                       style: str = "publication", palette: str = "",
                       theme: str = "", sort_col: str = "",
                       sort_desc: bool = False, color_col: str = "",
                       signif_col: str = "",
                       compute_fdr: bool = False,
                       fdr_source_col: str = "",
                       n_levels: int = 0,
                       axis_text_size: int = 0) -> str:
        """Translate a declarative chart spec into safe, academic ggplot2 code.

        ``x``/``y``/``size``/``label`` are column names; the data frame is
        available as ``.data`` (pre-loaded by the sandbox prelude). Column
        names are quoted via backticks to survive non-syntactic names.

        ``log_transform_y`` applies a ``-log10()`` transform to the Y column
        (typically for p-values / volcano plots).

        ``style`` selects a journal preset (publication / nature / cell /
        minimal / clusterprofiler / custom); ``theme`` and ``palette`` override
        the individual pieces so the user's special requirements are honored.
        Left empty, they fall back to the per-chart-type default in
        :mod:`src.core.plot_styles` (the unified low-saturation academic style).

        Enrichment-aware options (auto-inferred by the agent from the data):
        ``sort_col``   — column used to order the categorical axis (e.g. pvalue
                         so the most significant term is on top).
        ``sort_desc``  — sort ``sort_col`` in descending order.
        ``color_col``  — column mapped to a colour: a continuous column becomes
                         a gradient, a categorical column a discrete palette.
        ``signif_col`` — p-value column used to draw significance stars
                         (* / ** / ***) on bar charts.

        Layout guards against label overlap:
        ``n_levels``       — number of categories in the discrete column, so the
                             unified palette can be generated for exactly that
                             many levels (>10 -> same-family HCL hues).
        ``axis_text_size`` — tick label size override for dense axes (0 = theme
                             default 10 pt).
        """
        ct = (chart_type or "").strip().lower()
        if ct not in PlotEngine.SUPPORTED_CHART_TYPES:
            raise ValueError(
                f"Unsupported chart_type '{chart_type}'. "
                f"Choose from {sorted(PlotEngine.SUPPORTED_CHART_TYPES)}.")

        def q(name: str) -> str:
            # Backtick-quote a column name for safe use inside ggplot2 aes.
            return f"`{name.replace('`', '')}`"

        # 样式解析：显式参数（用户需求）> 图型默认风格 > 期刊预设。
        resolved = plot_styles.resolve_style(
            ct, style=style, palette=palette, theme=theme)
        chart = plot_styles.chart_style(ct)
        ramp = resolved["ramp"]
        pal_name = resolved["palette"]
        pal_first = plot_styles.first_color(pal_name)
        # 刻度字号按分类密度自动下调，避免标签互相挤压。
        theme_block = plot_styles.theme_block(resolved["theme"], axis_text_size)

        # Y expression: optionally apply -log10 for p-value style charts.
        y_expr = f"-log10({q(y)})" if log_transform_y else q(y)

        # Escape the title for an R string literal.
        title_r = title.replace("\\", "\\\\").replace('"', '\\"')

        # FDR (Benjamini-Hochberg) is computed in R via stats::p.adjust() so
        # that all statistics live on the R side, never in Python. When
        # ``compute_fdr`` is set, we derive a new ``fdr`` column from the raw
        # p-value column and point the color mapping at it.
        fdr_block = ""
        if compute_fdr and fdr_source_col:
            fdr_block = (
                f"# --- FDR (Benjamini-Hochberg) computed in R ---\n"
                f".fdr_src <- .data[[\"{fdr_source_col}\"]]\n"
                f".fdr_ok <- !is.na(.fdr_src) & .fdr_src > 0\n"
                f".data$fdr <- NA_real_\n"
                f".data$fdr[.fdr_ok] <- stats::p.adjust(.fdr_src[.fdr_ok], method = \"BH\")\n"
                f"rm(.fdr_src, .fdr_ok)\n"
            )

        # Categorical-axis ordering. A data-driven ``sort_col`` (e.g. pvalue)
        # wins; otherwise the chart-type default applies — horizontal bars and
        # enrichment dotplots list the most significant / largest term on TOP,
        # which is what the reference figures do.
        if sort_col:
            sort_expr = (
                f"reorder({q(y)}, {q(sort_col)})"
                if not sort_desc
                else f"reorder({q(y)}, -{q(sort_col)})"
            )
        elif ct == "dotplot":
            sort_expr = f"reorder({q(y)}, -{q(x)})"
        else:
            sort_expr = q(y)

        # Bar charts are drawn horizontally but WITHOUT coord_flip: the category
        # goes straight onto the (discrete) Y axis and the value onto X. That
        # makes the reading order unambiguous — on a discrete ggplot axis the
        # first level sits at the BOTTOM, so the largest / most significant bar
        # must be the LAST level to end up on TOP (reference bioR03/bioR02 keep
        # the biggest term on top).
        if ct == "bar":
            bar_cat_expr = (
                f"reorder({q(x)}, -{q(sort_col)})" if sort_col
                else f"reorder({q(x)}, {y_expr})"
            )
        else:
            bar_cat_expr = q(x)

        # Continuous colour mapping (enrichment significance gradient).
        color_scale = (
            plot_styles.gradient_scale(
                ramp, "color", PlotEngine._color_label(color_col))
            if color_col else ""
        )

        # Significance stars (bar charts): * p<0.05, ** p<0.01, *** p<0.001.
        signif_geom = ""
        if signif_col:
            signif_geom = (
                f"geom_text(aes(label = ifelse({q(signif_col)} < 0.001, '***', "
                f"ifelse({q(signif_col)} < 0.01, '**', "
                f"ifelse({q(signif_col)} < 0.05, '*', '')))), "
                f"hjust = -0.2, size = 3.5)"
            )

        # ---- scatter / point-based ------------------------------------ #
        if ct == "bubble":
            # Enrichment Dotplot (reference bioR29): X = gene ratio, Y = term,
            # size = gene count, colour = FDR on a red -> blue gradient (a small
            # FDR is significant, hence red). Size / colour mappings must live
            # inside aes() for the scales to take effect. The Y axis is ordered
            # by gene ratio DESCENDING so the largest ratio sits on TOP, like
            # clusterProfiler::dotplot.
            size_a = f"size = {q(size)}" if size else ""
            bubble_color_aes = f"color = {q(color_col)}" if color_col else ""
            label_layer = (
                f"geom_text_repel(aes(label = {q(label)}), size = 3, max.overlaps = 20)"
                if label else ""
            )
            size_lo, size_hi = chart.get("size_range", (2, 10))
            size_scale = (
                f"scale_size_continuous(range = c({size_lo}, {size_hi}), "
                f"name = '{chart.get('size_label', 'Count')}')"
            )
            extras = "".join(f", {a}" for a in (size_a, bubble_color_aes) if a)
            sort_for_bubble = (
                f"reorder({q(y)}, -{q(x)})" if not sort_col else sort_expr
            )
            # A computed ``fdr`` column must exist before ggplot builds the plot.
            head = f"{fdr_block}\n" if fdr_block else ""
            head += f"p <- ggplot(.data, aes(x = {q(x)}, y = {sort_for_bubble}{extras}))"
            return PlotEngine._chain(head, [
                "geom_point(alpha = 0.95)",
                label_layer,
                color_scale,
                size_scale,
                f"labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\")",
                theme_block,
            ])

        if ct == "scatter":
            # 散点标签用 ggrepel：高密度点云里普通 geom_text 必然互相压字。
            label_layer = (
                f"geom_text_repel(aes(label = {q(label)}), size = 3, "
                f"max.overlaps = 15)" if label else ""
            )
            # Colour by the mapped column when present, otherwise use the first
            # palette colour so the figure is never black-and-white.
            point_layer = (
                f"geom_point(aes(color = {q(color_col)}), alpha = 0.75)"
                if color_col else f"geom_point(color = '{pal_first}', alpha = 0.75)"
            )
            return PlotEngine._chain(
                f"p <- ggplot(.data, aes(x = {q(x)}, y = {y_expr}))",
                [
                    point_layer,
                    label_layer,
                    color_scale,
                    f"labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\")",
                    theme_block,
                ],
            )

        if ct == "line":
            group_aes = f", group = {q(label)}" if label else ""
            series_aes = f", color = {q(color_col)}" if color_col else ""
            line_color = f"color = '{pal_first}', " if not color_col else ""
            return PlotEngine._chain(
                f"p <- ggplot(.data, aes(x = {q(x)}, y = {y_expr}{group_aes}{series_aes}))",
                [
                    # ``size`` (not ``linewidth``) keeps the script working on
                    # ggplot2 < 3.4 as well.
                    f"geom_line({line_color}size = {chart.get('line_width', 1.5)}, alpha = 0.9)",
                    f"geom_point({line_color}size = 1.5)",
                    # A series column is categorical -> journal palette
                    # (reference bioR33 multi-GSEA trend panel).
                    (PlotEngine._manual_scale("color", pal_name, color_col, n_levels)
                     if color_col else ""),
                    f"labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\")",
                    theme_block,
                ],
            )

        if ct == "area":
            if color_col:
                head = f"p <- ggplot(.data, aes(x = {q(x)}, y = {y_expr}, fill = {q(color_col)}))"
                layers = [
                    "geom_area(alpha = 0.6)",
                    PlotEngine._manual_scale("fill", pal_name, color_col, n_levels),
                ]
            else:
                head = f"p <- ggplot(.data, aes(x = {q(x)}, y = {y_expr}))"
                layers = [
                    f"geom_area(fill = '{pal_first}', alpha = 0.6)",
                    f"geom_line(color = '{pal_first}')",
                ]
            layers += [
                f"labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\")",
                theme_block,
            ]
            return PlotEngine._chain(head, layers)

        if ct == "volcano":
            # 常规固定语义（参考 bioR19 的阈值做法，配色统一为低饱和版）：
            # Up = 红，Down = 蓝，NS = 灰；|log2FC| 与 p 阈值画虚线。
            fold = float(chart.get("fold_cutoff", 1.0))
            p_cut = float(chart.get("p_cutoff", 0.05))
            thr = round(-math.log10(p_cut), 3)
            up_cond = f"{q(x)} > {fold} & {y_expr} > {thr}"
            down_cond = f"{q(x)} < -{fold} & {y_expr} > {thr}"
            # 只标注显著点，并用 ggrepel 防止基因名互相覆盖。
            label_layer = (
                f"geom_text_repel(data = subset(.data, {y_expr} > {thr}), "
                f"aes(label = {q(label)}), size = 3, max.overlaps = 15)"
                if label else ""
            )
            conv = plot_styles.up_down_colors()
            up_c, down_c, ns_c = conv["up"], conv["down"], conv["ns"]
            return PlotEngine._chain(
                f"p <- ggplot(.data, aes(x = {q(x)}, y = {y_expr}))",
                [
                    (f"geom_point(aes(color = ifelse({up_cond}, 'Up', "
                     f"ifelse({down_cond}, 'Down', 'NS'))), alpha = 0.75)"),
                    label_layer,
                    f"scale_color_manual(values = c('Up' = '{up_c}', "
                    f"'Down' = '{down_c}', 'NS' = '{ns_c}'), name = 'Regulation')",
                    f"geom_hline(yintercept = {thr}, linetype = 'dashed', color = 'grey50')",
                    f"geom_vline(xintercept = c(-{fold}, {fold}), "
                    f"linetype = 'dashed', color = 'grey50')",
                    f"labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\")",
                    theme_block,
                ],
            )

        # ---- categorical / distribution ------------------------------- #
        if ct == "bar":
            layers = [
                # Reference bioR03/bioR05: horizontal bars coloured by
                # significance, sorted so the most significant term is on TOP.
                (f"geom_col(aes(fill = {q(color_col)}), alpha = 0.85)" if color_col
                 else f"geom_col(fill = '{pal_first}', alpha = 0.85)"),
                signif_geom,
                (plot_styles.gradient_scale(ramp, "fill", PlotEngine._color_label(color_col))
                 if color_col else ""),
            ]
            if chart.get("expand_zero"):
                # 数值轴紧贴 0（期刊常规），但存在星级标注时留一点余量，
                # 否则标记会被面板边界切掉。
                pad = "0.06" if signif_col else "0"
                layers += [
                    f"scale_x_continuous(expand = c(0, {pad}))",
                    "scale_y_discrete(expand = c(0, 0))",
                ]
            layers += [
                f"labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\")",
                theme_block,
            ]
            return PlotEngine._chain(
                f"p <- ggplot(.data, aes(x = {y_expr}, y = {bar_cat_expr}))", layers)

        if ct == "boxplot":
            # Group comparison (reference bioR07/09/11): boxes filled by the
            # grouping column (falling back to the X categories), jittered raw
            # points in the same colour, ggpubr base theme.
            group_col = color_col or x
            layers = [
                f"geom_boxplot(aes(fill = {q(group_col)}), outlier.shape = 21, "
                f"outlier.size = 1.5, alpha = 0.8)",
            ]
            if chart.get("jitter"):
                layers.append(
                    f"geom_jitter(aes(color = {q(group_col)}), width = 0.15, "
                    f"size = 1, alpha = 0.45)")
            layers.append(
                PlotEngine._manual_scale("fill", pal_name, group_col, n_levels))
            if chart.get("jitter"):
                # Same palette + same column -> ggplot merges both legends.
                layers.append(
                    PlotEngine._manual_scale("color", pal_name, group_col, n_levels))
            layers += [
                f"labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\")",
                theme_block,
            ]
            rotate = chart.get("rotate_x")
            if rotate:
                # 倾斜的 X 轴标签放在基础主题之后，确保不被主题里的 axis.text 覆盖。
                layers.append(
                    f"theme(axis.text.x = element_text(angle = {rotate}, hjust = 1))")
            return PlotEngine._chain(
                f"p <- ggplot(.data, aes(x = {q(x)}, y = {y_expr}))", layers)

        if ct == "violin":
            # Reference bioR11/bioR12: filled violins with a white inner boxplot.
            group_col = color_col or x
            layers = [
                f"geom_violin(aes(fill = {q(group_col)}), alpha = 0.7)",
                "geom_boxplot(width = 0.1, outlier.shape = NA, fill = 'white', alpha = 0.6)",
                PlotEngine._manual_scale("fill", pal_name, group_col, n_levels),
            ]
            layers += [
                f"labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\")",
                theme_block,
            ]
            rotate = chart.get("rotate_x")
            if rotate:
                layers.append(
                    f"theme(axis.text.x = element_text(angle = {rotate}, hjust = 1))")
            return PlotEngine._chain(
                f"p <- ggplot(.data, aes(x = {q(x)}, y = {y_expr}))", layers)

        if ct == "histogram":
            return PlotEngine._chain(
                f"p <- ggplot(.data, aes(x = {q(x)}))",
                [
                    f"geom_histogram(fill = '{pal_first}', color = 'white', "
                    f"bins = 30, alpha = 0.8)",
                    f"labs(title = \"{title_r}\", x = \"{x}\", y = \"Count\")",
                    theme_block,
                ],
            )

        if ct == "density":
            if color_col:
                head = f"p <- ggplot(.data, aes(x = {q(x)}, fill = {q(color_col)}))"
                layers = [
                    "geom_density(alpha = 0.5)",
                    PlotEngine._manual_scale("fill", pal_name, color_col, n_levels),
                ]
            else:
                head = f"p <- ggplot(.data, aes(x = {q(x)}))"
                layers = [
                    f"geom_density(fill = '{pal_first}', color = '{pal_first}', alpha = 0.5)",
                ]
            layers += [
                f"labs(title = \"{title_r}\", x = \"{x}\", y = \"Density\")",
                theme_block,
            ]
            return PlotEngine._chain(head, layers)

        if ct == "dotplot":
            size_lo, size_hi = chart.get("size_range", (3, 8))
            mappings = []
            if size:
                mappings.append(f"size = {q(size)}")
            if color_col:
                mappings.append(f"color = {q(color_col)}")
            point_layer = (
                f"geom_point(aes({', '.join(mappings)}), alpha = 0.9)" if mappings
                else f"geom_point(size = 3, color = '{pal_first}', alpha = 0.9)"
            )
            return PlotEngine._chain(
                f"p <- ggplot(.data, aes(x = {q(x)}, y = {sort_expr}))",
                [
                    point_layer,
                    color_scale,
                    (f"scale_size_continuous(range = c({size_lo}, {size_hi}))"
                     if size else ""),
                    f"labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\")",
                    theme_block,
                ],
            )

        # ---- matrix / heatmap ------------------------------------------ #
        if ct == "heatmap":
            # 常规固定语义（参考 bioR17/bioR18）：蓝-白-红（低饱和）、按行 z-score、
            # 行列聚类；字号与行名随矩阵规模自适应，行列过多时隐藏行名，
            # 从源头避免标签互相重叠。
            cluster = "TRUE" if chart.get("cluster", True) else "FALSE"
            base_fs = chart.get("font_size", 8)
            return (
                f"m <- as.matrix(.data[, c(\"{x}\", \"{y}\")])\n"
                f"rownames(m) <- .data[[\"{label if label else x}\"]]\n"
                f".fs <- if (nrow(m) > 60) 4 else if (nrow(m) > 30) 6 else {base_fs}\n"
                f".do_cluster <- nrow(m) > 1 && ncol(m) > 1\n"
                f"p <- pheatmap::pheatmap(m, main = \"{title_r}\",\n"
                f"  color = {plot_styles.ramp_call(ramp)},\n"
                f"  scale = '{chart.get('scale', 'row')}',\n"
                f"  cluster_rows = {cluster} && .do_cluster,\n"
                f"  cluster_cols = {cluster} && .do_cluster,\n"
                f"  show_rownames = nrow(m) <= 60, show_colnames = ncol(m) <= 40,\n"
                f"  border_color = NA, fontsize = .fs, fontsize_row = .fs,\n"
                f"  fontsize_col = .fs)"
            )

        if ct == "corrplot":
            # Reference bioR23: circle glyphs, hclust ordering, upper triangle,
            # coefficients printed, blue-white-red ramp.
            stops = plot_styles.ramp_stops(ramp)
            low, mid, high = stops[0], stops[len(stops) // 2], stops[-1]
            return (
                f"m <- as.matrix(.data[, c(\"{x}\", \"{y}\")])\n"
                f"p <- ggcorrplot::ggcorrplot(cor(m),\n"
                f"  method = '{chart.get('method', 'circle')}', hc.order = TRUE,\n"
                f"  type = 'upper', lab = TRUE, lab_size = 3,\n"
                f"  colors = c('{low}', '{mid}', '{high}'), title = \"{title_r}\")"
            )

        # ---- composition ----------------------------------------------- #
        if ct in ("pie", "donut"):
            # Reference bioR28: slices ordered large -> small, single-hue
            # sequential scale, percentage label on every slice.
            fill_aes = f"reorder({q(x)}, -{q(y)})"
            if ct == "donut":
                head = f"p <- ggplot(.data, aes(x = 2, y = {q(y)}, fill = {fill_aes}))"
                layers = ["geom_col(width = 1, color = 'white')", "xlim(0.5, 2.5)"]
            else:
                head = f"p <- ggplot(.data, aes(x = '', y = {q(y)}, fill = {fill_aes}))"
                layers = ["geom_col(width = 1, color = 'white')"]
            layers += [
                "coord_polar(theta = 'y')",
                PlotEngine._manual_scale("fill", pal_name, x, n_levels),
                # 只标注占比 >= 5% 的扇区：小扇区的百分比标签必然互相重叠，
                # 其类别信息由图例承担（文本用空串占位，避免位置被移动）。
                (f"geom_text(aes(label = ifelse({q(y)} / sum({q(y)}) >= 0.05, "
                 f"paste0(round(100 * {q(y)} / sum({q(y)}), 1), '%'), '')), "
                 f"position = position_stack(vjust = 0.5), size = 3)"),
                f"labs(title = \"{title_r}\", fill = \"{x}\")",
                "theme_void()",
                "theme(plot.title = element_text(face = 'bold', size = 14, hjust = 0.5))",
            ]
            return PlotEngine._chain(head, layers)

        # ---- advanced / multi-panel ------------------------------------- #
        if ct == "ridge":
            group_col = color_col or y
            return PlotEngine._chain(
                f"p <- ggplot(.data, aes(x = {q(x)}, y = {q(y)}, fill = {q(group_col)}))",
                [
                    "ggridges::geom_density_ridges(alpha = 0.7)",
                    PlotEngine._manual_scale("fill", pal_name, group_col, n_levels),
                    f"labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\")",
                    theme_block,
                ],
            )

        if ct == "alluvial":
            # Reference bioR27: strata coloured by the source axis, flows
            # forwarded so consecutive axes keep the upstream colour.
            y_col = size or y
            return PlotEngine._chain(
                f"p <- ggplot(.data, aes(axis1 = {q(x)}, axis2 = {q(y)}, y = {q(y_col)}))",
                [
                    f"ggalluvial::geom_alluvium(aes(fill = {q(x)}), alpha = 0.7)",
                    "ggalluvial::geom_stratum()",
                    PlotEngine._manual_scale("fill", pal_name, x, n_levels),
                    f"labs(title = \"{title_r}\")",
                    theme_block,
                ],
            )

        if ct == "network":
            # Base graphics: the engine re-draws it through ``draw_plot()`` on
            # every output device (plain base plots cannot be replayed by
            # ``print(p)``). Reference bioR25: white nodes, bold black labels.
            node_size = chart.get("node_size", 8)
            return (
                f"draw_plot <- function() {{\n"
                f"  g <- igraph::graph_from_data_frame(\n"
                f"    .data[, c(\"{x}\", \"{y}\")], directed = FALSE)\n"
                f"  plot(g, main = \"{title_r}\", vertex.color = 'white',\n"
                f"    vertex.frame.color = NA, vertex.size = {node_size},\n"
                f"    vertex.label.color = 'black', vertex.label.font = 2,\n"
                f"    vertex.label.cex = 1.0, edge.color = 'grey60', edge.curved = 0.2)\n"
                f"}}\n"
                f"p <- NULL"
            )

        # Fallback: unknown chart type -> raise so the agent can ask the user
        # for more detail (handled in academic_agent).
        raise ValueError(
            f"Unsupported chart_type '{chart_type}'. "
            f"Choose from {sorted(PlotEngine.SUPPORTED_CHART_TYPES)}.")

    # -- script composition --------------------------------------------- #
    @staticmethod
    def _package_load_block(extra_packages: Optional[List[str]] = None) -> str:
        """Emit an R block that checks for and loads the core plot packages.

        ``extra_packages`` are loaded on demand (e.g. pheatmap for heatmaps,
        ggpubr for boxplots) so we don't force-install everything upfront.
        """
        pkgs = list(CORE_R_PACKAGES)
        for p in (extra_packages or []):
            if p not in pkgs:
                pkgs.append(p)
        lines = []
        for pkg in pkgs:
            # 措辞必须保留 "Required R package 'X' is not installed" 前缀：
            # r_engine.plot_failure_missing_packages 靠它反解出缺哪个包（含扩展包）。
            # 但不再附带 install.packages 命令——NixOS 上该命令必然失败，装包指引
            # 统一由 r_engine.package_install_guidance 按平台生成。
            lines.append(
                f'if (!requireNamespace("{pkg}", quietly = TRUE)) '
                f'stop("Required R package \'{pkg}\' is not installed.")'
            )
            lines.append(f'suppressPackageStartupMessages(library("{pkg}"))')
        return "\n".join(lines)

    @staticmethod
    def _compose_script(r_code: str, plot_data: PlotData, svg_path: str,
                        png_path: str, pdf_path: str,
                        extra_packages: Optional[List[str]] = None,
                        figure_size: Optional[Tuple[float, float]] = None) -> str:
        """Wrap the LLM's plotting code with the sandbox prelude and output
        directives for the three formats."""
        blocked = ", ".join(f"'{f}'" for f in _BLOCKED_R_FUNCTIONS)
        prelude = _SANDBOX_PRELUDE.replace("%BLOCKED_LIST%", blocked)
        prelude = prelude.replace(
            "%PACKAGE_LOAD%", PlotEngine._package_load_block(extra_packages))

        # Replace backslashes for R string literals.
        svg_r = svg_path.replace("\\", "/")
        png_r = png_path.replace("\\", "/")
        pdf_r = pdf_path.replace("\\", "/")

        # 画布尺寸来自样式注册表（参考图库中的期刊尺寸），每个图型各不相同。
        width, height = (figure_size or plot_styles.DEFAULT_CANVAS)
        w = PlotEngine._inch(width)
        h = PlotEngine._inch(height)

        # The plot must be re-drawn onto each output device. ``dev.copy`` is NOT
        # used: it replays the display list and is unreliable for grid/ggplot2
        # graphics (it can silently produce a blank file), which is exactly the
        # failure mode seen when plots came back empty. Opening each device and
        # drawing onto it directly is robust for both base and grid.
        # ``.sn_draw()`` covers both plot families: ggplot/pheatmap objects are
        # assigned to ``p`` and printed, while base-graphics charts (e.g. the
        # igraph network) expose a ``draw_plot()`` function that re-draws itself.
        # IMPORTANT: the output blocks MUST come *after* ``r_code`` — the spec
        # code assigns ``p``, and printing before that fails with
        # "Error: object 'p' not found".
        output_directives = f'''
# --- output device directives ---
.sn_draw <- function() {{
  if (exists("draw_plot", mode = "function")) {{
    draw_plot()
  }} else {{
    print(p)
  }}
}}
svg("{svg_r}", width = {w}, height = {h})
.sn_draw()
dev.off()
'''

        epilogue = f'''
# --- finalize outputs (SVG + PNG + PDF) ---
png("{png_r}", width = {w}, height = {h}, units = "in", res = 150)
.sn_draw()
dev.off()
pdf("{pdf_r}", width = {w}, height = {h})
.sn_draw()
dev.off()
# --- end ---
'''

        # Order: sandbox prelude -> spec code (defines ``p``) -> devices.
        return prelude + "\n" + r_code + "\n" + output_directives + "\n" + epilogue


# ---------------------------------------------------------------------- #
#  Convenience singleton
# ---------------------------------------------------------------------- #
_engine: Optional[PlotEngine] = None


def get_plot_engine() -> PlotEngine:
    global _engine
    if _engine is None:
        _engine = PlotEngine()
    return _engine
