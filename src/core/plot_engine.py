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
    * Low coupling: only depends on ``r_engine`` and stdlib + subprocess.
    * Performance: single Rscript invocation per plot; no per-row Python I/O.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Core.PlotEngine")

# Registry persisted next to plot outputs so charts can be modified in later
# turns / sessions (registry survives runtime object recreation).
PLOT_REGISTRY_FILENAME = "plot_registry.json"
_registry_lock = threading.Lock()

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


# ---------------------------------------------------------------------- #
#  Data marshalling (JSON/table -> CSV + preview)
# ---------------------------------------------------------------------- #
class PlotEngine:
    """Stateless entry point: marshals data, runs R, collects outputs."""

    def __init__(self, output_dir: Optional[str] = None):
        self._output_dir = output_dir

    # -- output directory ----------------------------------------------- #
    def _ensure_output_dir(self) -> str:
        if self._output_dir:
            os.makedirs(self._output_dir, exist_ok=True)
            return self._output_dir
        base = tempfile.gettempdir()
        d = os.path.join(base, "scholar_navis_plots")
        os.makedirs(d, exist_ok=True)
        return d

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
                 extra_packages: Optional[List[str]] = None) -> PlotResult:
        """Execute ``r_code`` (plotting only) against ``plot_data`` in a sandbox.

        ``extra_packages`` are loaded on demand for chart types that need them
        (e.g. pheatmap, ggpubr, ggridges).

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

        script = self._compose_script(
            r_code, plot_data, svg_path, png_path, pdf_path,
            extra_packages=extra_packages)

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
            f"executable={info.get('executable')} | data={plot_data.data_path}"
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
    }

    # Journal style presets: each maps to a base theme + palette. The LLM may
    # also declare an explicit ``theme`` / ``palette`` to override the preset,
    # so the user's special requirements can be honored. All presets follow
    # international journal publication conventions.
    _STYLE_PRESETS = {
        "publication": {"theme": "bw", "palette": "set2"},
        "nature": {"theme": "classic", "palette": "nature"},
        "cell": {"theme": "bw", "palette": "cell"},
        "minimal": {"theme": "minimal", "palette": "set2"},
        "clusterprofiler": {"theme": "minimal", "palette": "rdbu"},
        "custom": {"theme": "", "palette": ""},
    }

    @staticmethod
    def _academic_theme_block(theme: str = "") -> str:
        """Return an R theme expression following journal publication standards.

        Uses a clean base theme (default ``theme_bw``) plus explicit typography
        and layout rules: bold centered title, bold axis titles, minimal minor
        grid, and a black panel border — the look expected by most journals.
        """
        t = (theme or "").strip().lower()
        if t == "minimal":
            base = "theme_minimal(base_size = 12)"
        elif t == "classic":
            base = "theme_classic(base_size = 12)"
        elif t == "pubr":
            base = "ggpubr::theme_pubr(base_size = 12)"
        else:
            base = "theme_bw(base_size = 12)"
        return (
            f"{base} +\n"
            f"  theme(\n"
            f"    plot.title = element_text(face = 'bold', size = 14, hjust = 0.5),\n"
            f"    axis.title = element_text(face = 'bold', size = 12),\n"
            f"    axis.text = element_text(size = 10),\n"
            f"    legend.title = element_text(face = 'bold', size = 10),\n"
            f"    legend.text = element_text(size = 9),\n"
            f"    panel.grid.minor = element_blank(),\n"
            f"    panel.border = element_rect(color = 'black', linewidth = 0.8)\n"
            f"  )"
        )

    @staticmethod
    def _academic_palette(palette: str = "") -> str:
        """Return an R color vector for a discrete journal palette."""
        p = (palette or "").strip().lower()
        palettes = {
            "set2": "c('#66c2a5', '#fc8d62', '#8da0cb', '#e78ac3', '#a6d854', '#ffd92f')",
            "set1": "c('#e41a1c', '#377eb8', '#4daf4a', '#984ea3', '#ff7f00', '#ffff33')",
            "nature": "c('#e64b35', '#4dbae5', '#3c5488', '#f39b7b', '#00a087', '#8491b4')",
            "cell": "c('#e64b35', '#4dbae5', '#3c5488', '#f39b7b', '#00a087', '#8491b4')",
            "viridis": "c('#440154', '#3b528b', '#21918c', '#5ec962', '#fde725')",
            "magma": "c('#000004', '#51127c', '#b63679', '#fb8861', '#fcfdbf')",
        }
        return palettes.get(
            p, "c('#66c2a5', '#fc8d62', '#8da0cb', '#e78ac3', '#a6d854', '#ffd92f')")

    @staticmethod
    def spec_to_r_code(chart_type: str, x: str, y: str, size: str = "",
                       label: str = "", title: str = "Chart",
                       log_transform_y: bool = False,
                       style: str = "publication", palette: str = "",
                       theme: str = "", sort_col: str = "",
                       sort_desc: bool = False, color_col: str = "",
                       signif_col: str = "",
                       compute_fdr: bool = False,
                       fdr_source_col: str = "") -> str:
        """Translate a declarative chart spec into safe, academic ggplot2 code.

        ``x``/``y``/``size``/``label`` are column names; the data frame is
        available as ``.data`` (pre-loaded by the sandbox prelude). Column
        names are quoted via backticks to survive non-syntactic names.

        ``log_transform_y`` applies a ``-log10()`` transform to the Y column
        (typically for p-values / volcano plots).

        ``style`` selects a journal preset (publication / nature / cell /
        minimal / custom); ``theme`` and ``palette`` override the individual
        pieces so the user's special requirements can be honored.

        Enrichment-aware options (auto-inferred by the agent from the data):
        ``sort_col``   — column used to order the categorical axis (e.g. pvalue
                         so the most significant term is on top).
        ``sort_desc``  — sort ``sort_col`` in descending order.
        ``color_col``  — continuous column mapped to a color gradient (e.g.
                         -log10(pvalue) for bubble/bar enrichment plots).
        ``signif_col`` — p-value column used to draw significance stars
                         (* / ** / ***) on bar charts.
        """
        ct = (chart_type or "").strip().lower()
        if ct not in PlotEngine.SUPPORTED_CHART_TYPES:
            raise ValueError(
                f"Unsupported chart_type '{chart_type}'. "
                f"Choose from {sorted(PlotEngine.SUPPORTED_CHART_TYPES)}.")

        def q(name: str) -> str:
            # Backtick-quote a column name for safe use inside ggplot2 aes.
            return f"`{name.replace('`', '')}`"

        # Resolve style preset -> theme + palette (explicit args win).
        style = (style or "publication").strip().lower()
        preset = PlotEngine._STYLE_PRESETS.get(
            style, PlotEngine._STYLE_PRESETS["publication"])
        theme = (theme or preset["theme"]).strip()
        palette = (palette or preset["palette"]).strip()

        # Y expression: optionally apply -log10 for p-value style charts.
        y_expr = f"-log10({q(y)})" if log_transform_y else q(y)

        # Escape the title for an R string literal.
        title_r = title.replace("\\", "\\\\").replace('"', '\\"')

        theme_block = PlotEngine._academic_theme_block(theme)
        pal_vec = PlotEngine._academic_palette(palette)
        # First color of the palette (used for single-color geoms).
        pal_first = pal_vec.split("'")[1]

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

        # Categorical-axis ordering expression (e.g. reorder(Term, pvalue)).
        if sort_col:
            sort_expr = (
                f"reorder({q(y)}, {q(sort_col)})"
                if not sort_desc
                else f"reorder({q(y)}, -{q(sort_col)})"
            )
        else:
            sort_expr = q(y)

        # Continuous color mapping (enrichment significance gradient).
        color_aes = f", color = {q(color_col)}" if color_col else ""
        color_scale = (
            f"scale_color_gradient(low = '#377eb8', high = '#e41a1c', "
            f"name = '{color_col}')" if color_col else ""
        )

        # Significance stars (bar charts): * p<0.05, ** p<0.01, *** p<0.001.
        signif_geom = ""
        if signif_col:
            signif_geom = (
                f" +\n  geom_text(aes(label = ifelse({q(signif_col)} < 0.001, '***', "
                f"ifelse({q(signif_col)} < 0.01, '**', "
                f"ifelse({q(signif_col)} < 0.05, '*', '')))), "
                f"hjust = -0.2, size = 3.5)"
            )

        # ---- scatter / point-based ------------------------------------ #
        if ct == "bubble":
            # NOTE on comma handling for the aes() mapping list:
            # The position aesthetics ``x`` and ``y`` always come first; any
            # subsequent mapping (size / color) MUST be preceded by a comma.
            # Both ``size_a`` and ``color_aes`` below are emitted WITHOUT a
            # leading comma, so the join code prepends one when needed.
            size_a = f"size = {q(size)}" if size else ""
            # Enrichment bubble plots typically label each term on the Y axis;
            # adding on-point text would only clutter the figure. The caller
            # can still pass ``label`` to opt back in.
            label_geom = (
                f" +\n  geom_text_repel(aes(label = {q(label)}), size = 3, "
                f"max.overlaps = 20)" if label else ""
            )
            # Enrichment-style bubble plot: X = gene ratio, Y = term,
            # color = -log10(FDR) gradient (smaller FDR -> more significant ->
            # hotter color), size = gene count. The color column MUST be mapped
            # inside aes() for the gradient to take effect. By convention in
            # enrichment Dotplots the Y axis lists pathway/term names ordered
            # from largest to smallest ratio, so the most significant term sits
            # on TOP of the figure (matches clusterProfiler::dotplot default).
            if color_col:
                color_aes = f"color = {q(color_col)}"
                # clusterProfiler / enrichplot Dotplot uses the blue->red
                # reverse-viridis ramp (``viridis::plasma`` reversed is the
                # closest built-in match). The continuous color bar in the
                # right-side legend is the canonical enrichment look; without
                # ``breaks`` we let ggplot pick ~4-5 ticks automatically.
                color_scale = (
                    f"scale_color_gradientn(\n"
                    f"    colors = c('#67001f', '#b2182b', '#d6604d', '#f4a582',\n"
                    f"              '#fddbc7', '#ffffff', '#d1e5f0', '#92c5de',\n"
                    f"              '#4393c3', '#2166ac', '#053061'),\n"
                    f"    name = '{color_col}'\n"
                    f"  )"
                )
            else:
                color_aes = ""
                color_scale = ""
            # Enrichment Dotplot reference style (clusterProfiler):
            #   * the size legend shows three discrete size reference dots
            #     (e.g. 10 / 20 / 30), like enrichplot::dotplot;
            #   * the size range is generous (2..10) since Gene Count often
            #     varies by an order of magnitude across pathways.
            size_scale = (
                f"scale_size_continuous(range = c(2, 10), name = 'Count')"
                if size else
                f"scale_size_continuous(range = c(2, 8))"
            )
            # FDR block (if any) must run before ggplot so the ``fdr`` column
            # exists in .data when the color mapping references it. We sort
            # the Y axis by the X column (gene_ratio) in DESCENDING order so
            # the largest ratio is at the TOP — the standard clusterProfiler
            # enrichment Dotplot convention.
            fdr_prefix = f"{fdr_block}\n" if fdr_block else ""
            sort_for_dotplot = (
                f"reorder({q(y)}, -{q(x)})"
                if not sort_col
                else sort_expr
            )
            # Compose the extra aes(...) mappings (size / color) with proper
            # comma separators. Neither ``size_a`` nor ``color_aes`` carries
            # a leading comma; we always prepend one comma when at least one
            # mapping follows the position aesthetics. ``sort_for_dotplot``
            # ends with a closing paren, so the leading comma is what glues
            # the join together.
            extras = ""
            if size_a and color_aes:
                extras = f", {size_a}, {color_aes}"
            elif size_a:
                extras = f", {size_a}"
            elif color_aes:
                extras = f", {color_aes}"
            # Only emit the + chains for the color / size scales when those
            # mappings actually exist; otherwise the generated script ends
            # with a dangling "+" which is a syntax error in R / ggplot2.
            color_chain = f" +\n  {color_scale}" if color_scale else ""
            size_chain = f" +\n  {size_scale}" if size_scale else ""
            return (
                f"{fdr_prefix}"
                f"p <- ggplot(.data, aes(x = {q(x)}, y = {sort_for_dotplot}{extras})) +\n"
                f"  geom_point(alpha = 0.95){label_geom}{color_chain}{size_chain} +\n"
                f"  labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\") +\n"
                f"  {theme_block}"
            )

        if ct == "scatter":
            label_geom = (
                f"\n  geom_text(aes(label = {q(label)}), vjust = -0.8, size = 3)" if label else ""
            )
            # Default to the first palette color when no continuous color column
            # is mapped, so the plot is never black-and-white.
            point_color = f"color = '{pal_first}', " if not color_col else ""
            return (
                f"p <- ggplot(.data, aes(x = {q(x)}, y = {y_expr}{color_aes})) +\n"
                f"  geom_point({point_color}alpha = 0.7){label_geom} +\n"
                f"  {color_scale} +\n"
                f"  labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\") +\n"
                f"  {theme_block}"
            )

        if ct == "line":
            group_aes = f", group = {q(label)}" if label else ""
            color_aes_line = f", color = {q(color_col)}" if color_col else ""
            line_color = f"color = '{pal_first}', " if not color_col else ""
            return (
                f"p <- ggplot(.data, aes(x = {q(x)}, y = {y_expr}{group_aes}{color_aes_line})) +\n"
                f"  geom_line({line_color}alpha = 0.8) +\n"
                f"  geom_point({line_color}size = 1.5) +\n"
                f"  {color_scale} +\n"
                f"  labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\") +\n"
                f"  {theme_block}"
            )

        if ct == "area":
            return (
                f"p <- ggplot(.data, aes(x = {q(x)}, y = {y_expr})) +\n"
                f"  geom_area(fill = '{pal_first}', alpha = 0.6) +\n"
                f"  geom_line(color = '{pal_first}') +\n"
                f"  labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\") +\n"
                f"  {theme_block}"
            )

        if ct == "volcano":
            label_geom = (
                f"\n  geom_text(data = subset(.data, {y_expr} > 1.3), "
                f"aes(label = {q(label)}), size = 3, vjust = -0.8)" if label else ""
            )
            return (
                f"p <- ggplot(.data, aes(x = {q(x)}, y = {y_expr})) +\n"
                f"  geom_point(aes(color = ifelse({y_expr} > 1.3 & abs({q(x)}) > 1, 'Up', "
                f"ifelse({y_expr} > 1.3, 'Down', 'NS'))), alpha = 0.7){label_geom} +\n"
                f"  scale_color_manual(values = c('Up' = '#e41a1c', 'Down' = '#377eb8', "
                f"'NS' = '#bdbdbd'), name = 'Regulation') +\n"
                f"  geom_hline(yintercept = 1.3, linetype = 'dashed', color = 'grey50') +\n"
                f"  geom_vline(xintercept = c(-1, 1), linetype = 'dashed', color = 'grey50') +\n"
                f"  labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\") +\n"
                f"  {theme_block}"
            )

        # ---- categorical / distribution ------------------------------- #
        if ct == "bar":
            fill_aes = f", fill = {q(color_col)}" if color_col else ""
            fill_scale = (
                f"scale_fill_gradient(low = '#377eb8', high = '#e41a1c', "
                f"name = '{color_col}')" if color_col else ""
            )
            # Default fill color when no continuous color column is mapped.
            bar_fill = f"fill = '{pal_first}', " if not color_col else ""
            return (
                f"p <- ggplot(.data, aes(x = {sort_expr}, y = {y_expr}{fill_aes})) +\n"
                f"  geom_col({bar_fill}alpha = 0.85){signif_geom} +\n"
                f"  coord_flip() +\n"
                f"  {fill_scale} +\n"
                f"  labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\") +\n"
                f"  {theme_block}"
            )

        if ct == "boxplot":
            fill_aes = f"fill = {q(color_col)}" if color_col else ""
            # Default fill + outline color when no grouping column is mapped.
            box_fill = f"fill = '{pal_first}', " if not color_col else ""
            box_color = f"color = '{pal_first}', " if not color_col else ""
            return (
                f"p <- ggplot(.data, aes(x = {q(x)}, y = {y_expr}{fill_aes})) +\n"
                f"  geom_boxplot({box_fill}{box_color}outlier.shape = 21, outlier.size = 1.5, alpha = 0.8) +\n"
                f"  {color_scale} +\n"
                f"  labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\") +\n"
                f"  {theme_block}"
            )

        if ct == "violin":
            fill_aes = f"fill = {q(color_col)}" if color_col else ""
            # Default fill + outline when no grouping column is mapped.
            violin_fill = f"fill = '{pal_first}', " if not color_col else ""
            violin_color = f"color = '{pal_first}', " if not color_col else ""
            return (
                f"p <- ggplot(.data, aes(x = {q(x)}, y = {y_expr}{fill_aes})) +\n"
                f"  geom_violin({violin_fill}{violin_color}alpha = 0.7) +\n"
                f"  geom_boxplot(width = 0.1, outlier.shape = NA) +\n"
                f"  {color_scale} +\n"
                f"  labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\") +\n"
                f"  {theme_block}"
            )

        if ct == "histogram":
            return (
                f"p <- ggplot(.data, aes(x = {q(x)})) +\n"
                f"  geom_histogram(fill = '{pal_first}', color = 'white', bins = 30, alpha = 0.8) +\n"
                f"  labs(title = \"{title_r}\", x = \"{x}\", y = \"Count\") +\n"
                f"  {theme_block}"
            )

        if ct == "density":
            fill_aes = f"fill = {q(color_col)}" if color_col else ""
            # Default fill + outline when no grouping column is mapped.
            dens_fill = f"fill = '{pal_first}', " if not color_col else ""
            dens_color = f"color = '{pal_first}', " if not color_col else ""
            return (
                f"p <- ggplot(.data, aes(x = {q(x)}{fill_aes})) +\n"
                f"  geom_density({dens_fill}{dens_color}alpha = 0.5) +\n"
                f"  {color_scale} +\n"
                f"  labs(title = \"{title_r}\", x = \"{x}\", y = \"Density\") +\n"
                f"  {theme_block}"
            )

        if ct == "dotplot":
            return (
                f"p <- ggplot(.data, aes(x = {q(x)}, y = {sort_expr})) +\n"
                f"  geom_point(aes(size = {q(size) if size else y}), color = '{pal_first}', alpha = 0.8) +\n"
                f"  scale_size_continuous(range = c(2, 8)) +\n"
                f"  labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\") +\n"
                f"  {theme_block}"
            )

        # ---- matrix / heatmap ------------------------------------------ #
        if ct == "heatmap":
            return (
                f"m <- as.matrix(.data[, c(\"{x}\", \"{y}\")])\n"
                f"rownames(m) <- .data[[\"{label if label else x}\"]]\n"
                f"p <- pheatmap::pheatmap(m, main = \"{title_r}\", cluster_cols = FALSE,\n"
                f"  color = colorRampPalette({pal_vec})(3))"
            )

        if ct == "corrplot":
            return (
                f"m <- as.matrix(.data[, c(\"{x}\", \"{y}\")])\n"
                f"p <- ggcorrplot::ggcorrplot(cor(m), method = 'circle',\n"
                f"  lab = TRUE, title = \"{title_r}\")"
            )

        # ---- composition ----------------------------------------------- #
        if ct in ("pie", "donut"):
            hole = "0.5" if ct == "donut" else "0"
            return (
                f"p <- ggplot(.data, aes(x = '', y = {q(y)}, fill = {q(x)})) +\n"
                f"  geom_col(width = 1) +\n"
                f"  coord_polar(theta = 'y') +\n"
                f"  scale_fill_manual(values = {pal_vec}) +\n"
                f"  labs(title = \"{title_r}\", fill = \"{x}\") +\n"
                f"  theme_void() +\n"
                f"  theme(plot.title = element_text(face = 'bold', size = 14, hjust = 0.5))"
            )

        # ---- advanced / multi-panel ------------------------------------- #
        if ct == "ridge":
            return (
                f"p <- ggplot(.data, aes(x = {q(x)}, y = {q(y)}, fill = {q(y)})) +\n"
                f"  ggridges::geom_density_ridges(alpha = 0.7) +\n"
                f"  scale_fill_manual(values = {pal_vec}) +\n"
                f"  labs(title = \"{title_r}\", x = \"{x}\", y = \"{y}\") +\n"
                f"  {theme_block}"
            )

        if ct == "alluvial":
            return (
                f"p <- ggplot(.data, aes(axis1 = {q(x)}, axis2 = {q(y)}, y = {q(size or y)})) +\n"
                f"  ggalluvial::geom_alluvium(aes(fill = {q(x)}), alpha = 0.7) +\n"
                f"  ggalluvial::geom_stratum() +\n"
                f"  scale_fill_manual(values = {pal_vec}) +\n"
                f"  labs(title = \"{title_r}\") +\n"
                f"  {theme_block}"
            )

        if ct == "network":
            return (
                f"g <- igraph::graph_from_data_frame(.data[, c(\"{x}\", \"{y}\")], directed = FALSE)\n"
                f"p <- igraph::plot(g, main = \"{title_r}\", vertex.color = '{pal_first}',\n"
                f"  vertex.size = 8, vertex.label.cex = 0.7, edge.color = 'grey60')"
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
            lines.append(
                f'if (!requireNamespace("{pkg}", quietly = TRUE)) '
                f'stop("Required R package \'{pkg}\' is not installed. '
                f"Install it with: install.packages('{pkg}')\")"
            )
            lines.append(f'suppressPackageStartupMessages(library("{pkg}"))')
        return "\n".join(lines)

    @staticmethod
    def _compose_script(r_code: str, plot_data: PlotData, svg_path: str,
                        png_path: str, pdf_path: str,
                        extra_packages: Optional[List[str]] = None) -> str:
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

        # The plot object must be assigned to ``p`` by the spec code so that it
        # can be re-printed onto each output device. ``dev.copy`` is NOT used:
        # it replays the display list and is unreliable for grid/ggplot2
        # graphics (it can silently produce a blank file), which is exactly the
        # failure mode seen when plots came back empty. Opening each device and
        # printing ``p`` onto it directly is robust for both base and grid.
        # IMPORTANT: the output blocks MUST come *after* ``r_code`` — the spec
        # code assigns ``p``, and ``print(p)`` executed before that assignment
        # fails with "Error: object 'p' not found".
        output_directives = f'''
# --- output device directives ---
svg("{svg_r}", width = 8, height = 6)
print(p)
dev.off()
'''

        epilogue = f'''
# --- finalize outputs (SVG + PNG + PDF) ---
png("{png_r}", width = 8, height = 6, units = "in", res = 150)
print(p)
dev.off()
pdf("{pdf_r}", width = 8, height = 6)
print(p)
dev.off()
# --- end ---
'''

        # Order: sandbox prelude -> user/spec code (defines ``p``) -> devices.
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
