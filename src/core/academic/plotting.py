"""Academic plotting tool: declarative chart spec -> safe R/ggplot2 code."""
import json
import re
import time
from typing import Optional

from src.core.academic.base import logger

__all__ = ["plot_chart"]

# Column-name aliases used to recognize p-value / significance columns.
_PVALUE_ALIASES = ("pvalue", "p_value", "p-value", "pval", "p", "padj",
                   "fdr", "qvalue", "q_value", "adj_p", "adjp")
# Column-name aliases for the categorical term / pathway label.
_TERM_ALIASES = ("term", "pathway", "description", "name", "category",
                 "go_term", "kegg", "id", "gene_set")


def _infer_plot_style(chart_type: str, x: str, y: str, records: list,
                      style: str = "", palette: str = "", theme: str = "") -> dict:
    """Analyze the data and infer an academic plotting plan.

    This is the "think before you draw" step: it inspects the actual records
    (column names + value ranges) to decide, for the requested chart type:

      * which column to sort the categorical axis by (e.g. pvalue so the most
        significant term is on top);
      * whether a continuous color gradient is warranted (e.g. -log10(pvalue));
      * whether significance stars (* / ** / ***) should be drawn;
      * the journal style / palette / theme.

    Explicit ``style`` / ``palette`` / ``theme`` from the LLM (honoring the
    user's special requirements) always win over the inferred defaults.

    Returns a dict with keys: style, palette, theme, sort_col, sort_desc,
    color_col, signif_col.
    """
    ct = (chart_type or "").strip().lower()
    style = (style or "").strip().lower()
    palette = (palette or "").strip().lower()
    theme = (theme or "").strip().lower()

    # Collect the union of column names present in the records.
    cols = set()
    for rec in records:
        if isinstance(rec, dict):
            cols.update(rec.keys())
    cols_lower = {c.lower() for c in cols}

    def find_col(aliases):
        for a in aliases:
            if a in cols_lower:
                for c in cols:
                    if c.lower() == a:
                        return c
        return ""

    pvalue_col = find_col(_PVALUE_ALIASES)
    term_col = find_col(_TERM_ALIASES)

    # Defaults.
    out = {
        "style": style or "publication",
        "palette": palette,
        "theme": theme,
        "sort_col": "",
        "sort_desc": False,
        "color_col": "",
        "signif_col": "",
        "size_col": "",
        "pvalue_col": pvalue_col,
        "has_fdr": bool(find_col(("fdr", "qvalue", "q_value", "padj", "adj_p", "adjp"))),
    }

    # Explicit user intent wins for style/palette/theme.
    if style:
        return out

    # Enrichment-like data (has a p-value column) -> sort by significance and
    # color by -log10(pvalue); bar charts additionally get significance stars.
    if pvalue_col:
        out["sort_col"] = pvalue_col
        # For enrichment bubble plots, the most significant term must sit at
        # the TOP of the Y axis (clusterProfiler / enrichplot convention).
        # ``reorder(term, -pvalue)`` puts the smallest pvalue at the top.
        # Bar charts use coord_flip() so they keep ascending order (most
        # significant at the bottom of the flipped horizontal bar).
        if ct == "bubble":
            out["sort_desc"] = True
        else:
            out["sort_desc"] = False
        out["color_col"] = pvalue_col
        if ct == "bar":
            out["signif_col"] = pvalue_col

    # Bubble / dotplot enrichment charts: auto-detect a gene-count column so
    # the point size encodes how many genes each term contains.
    if ct in ("bubble", "dotplot"):
        count_col = find_col(("gene_count", "count", "gene_number", "num_genes",
                              "intersection_size", "size", "n_genes", "gene_num"))
        if count_col:
            out["size_col"] = count_col

    # ---- Academic palette selection ---------------------------------- #
    # Choose a journal palette based on the chart type, the data structure
    # (categorical vs continuous, number of groups) and the analysis context.
    # Explicit ``palette`` from the LLM (honoring the user's requirements)
    # always wins over these inferred defaults.
    if not palette:
        if ct == "volcano":
            # Up/down regulation: red/blue contrast is the journal standard.
            out["palette"] = "set1"
        elif ct == "heatmap":
            # Perceptually uniform sequential scale for continuous matrices.
            out["palette"] = "viridis"
        elif ct in ("corrplot", "density", "ridge"):
            # Continuous / distributional data reads best on a sequential scale.
            out["palette"] = "viridis"
        elif ct in ("pie", "donut", "alluvial", "network"):
            # Many discrete categories -> high-contrast categorical palette.
            out["palette"] = "set1"
        elif ct in ("line", "area"):
            # Time-series / trends -> colorblind-safe categorical palette.
            out["palette"] = "nature"
        elif ct in ("boxplot", "violin", "dotplot"):
            # Grouped distributions -> soft categorical palette.
            out["palette"] = "set2"
        elif ct in ("scatter", "bubble"):
            # Point clouds: use a continuous gradient when a numeric column is
            # available, otherwise a categorical palette.
            out["palette"] = "set2"
        else:
            # bar / histogram / default -> categorical palette.
            out["palette"] = "set2"

    return out


def plot_chart(chart_type: str, data: str, x: str, y: str,
               size: str = "", label: str = "", title: str = "Chart",
               style: str = "", palette: str = "", theme: str = "") -> str:
    """
    Render a publication-quality chart via R using a DECLARATIVE spec.

    The system translates the spec into safe, journal-standard ggplot2 code —
    you do NOT write R code yourself. Before rendering, the system analyzes
    the actual data (column names + value ranges) and decides, for the given
    chart type: whether to sort the categorical axis by significance (so the
    most significant term is on top), whether to add a continuous color
    gradient (e.g. -log10(pvalue)), and whether to draw significance stars
    (* / ** / ***). It then applies international academic conventions (clean
    theme, bold typography, journal palettes).
    If the user has special requirements, pass them via ``style`` / ``palette`` /
    ``theme`` and they will be honored.

    Supported chart_type values (covers most common scientific figures):
        "scatter"  — plain scatter (x, y numeric).
        "bubble"   — scatter with point size (e.g. GO enrichment).
        "line"     — line chart (x, y; optional `label` as group).
        "area"     — area chart (x, y).
        "volcano"  — volcano plot (x = log2FC, y = -log10(p)).
        "bar"      — bar chart (x = category, y = numeric).
        "boxplot"  — box plot (x = group, y = numeric).
        "violin"   — violin plot (x = group, y = numeric).
        "histogram"— histogram (x = numeric).
        "density"  — density plot (x = numeric).
        "dotplot"  — dot plot (x = numeric, y = category).
        "heatmap"  — matrix heatmap (rows from `label`, values from `y`).
        "corrplot" — correlation matrix heatmap.
        "pie"      — pie chart (x = category, y = value).
        "donut"    — donut chart (x = category, y = value).
        "ridge"    — ridge / density plot (x = numeric, y = group).
        "alluvial" — alluvial / flow diagram (x, y categories).
        "network"  — network graph (x = from, y = to).

    Parameters:
        chart_type : one of the supported types above.
        data       : JSON string of records (list of dicts). Forward the
                     previous tool's results unchanged.
        x          : column name for the X axis.
        y          : column name for the Y axis.
        size       : optional column for point size (bubble/dotplot).
        label      : optional column for point labels / group.
        title      : optional chart title.
        style      : optional journal preset — "publication", "nature", "cell",
                     "minimal", or "custom". Defaults to "publication".
        palette    : optional color palette name ("set1", "set2", "nature",
                     "cell", "viridis", "magma"). Overrides the preset.
        theme      : optional theme ("bw", "classic", "minimal", "pubr").
                     Overrides the preset.

    Returns a structured JSON with chart paths (SVG/PNG/PDF), a data preview,
    and the full CSV path; the runtime assembles the frontend visualization.
    """
    logger.info(f"Task: R Plotting | type='{chart_type}' title='{title}' x='{x}' y='{y}'")
    try:
        # 1) Parse data records.
        try:
            records = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"status": "error", "message": "data is not a valid JSON string."}, ensure_ascii=False)

        if isinstance(records, dict):
            # Accept {"results": [...]}, {"enriched_terms": [...]}, etc. wrappers.
            for key in ("results", "enriched_terms", "datasets", "homologs", "items", "data"):
                if isinstance(records.get(key), list):
                    records = records[key]
                    break
            else:
                records = [records]
        if not isinstance(records, list) or not records:
            return json.dumps({"status": "error", "message": "data must be a non-empty list."}, ensure_ascii=False)

        # 2) Marshal to disk + derive translation to R + run in sandbox.
        from src.core.plot_engine import get_plot_engine

        job_id = "plot_" + str(int(time.time()))
        engine = get_plot_engine()
        try:
            plot_data = engine.marshal_data(records, job_id)
        except ValueError as ve:
            return json.dumps({"status": "error", "message": str(ve)}, ensure_ascii=False)

        # Resolve derived columns (e.g. y = "-log10(p_value)" when data only
        # has "p_value"). LLMs often express the Y axis as a transformation.
        log_col = ""
        if y and y not in plot_data.columns:
            m = re.match(r"^-?log10\((.+)\)$", y.strip())
            if m:
                log_col = m.group(1)
                if log_col not in plot_data.columns:
                    return json.dumps({
                        "status": "error",
                        "message": f"Column '{log_col}' (from y='{y}') not found. Available: {plot_data.columns}",
                    }, ensure_ascii=False)
                y = log_col  # spec_to_r_code will apply -log10 internally

        # Validate referenced columns exist in the data.
        missing = [c for c in (x, y, size, label) if c and c not in plot_data.columns]
        if missing:
            return json.dumps({
                "status": "error",
                "message": f"Column(s) not found in data: {missing}. Available: {plot_data.columns}",
            }, ensure_ascii=False)

        # 3) Analyze the data and infer an academic plotting plan (sorting,
        #    color gradient, significance, and style / palette / theme).
        plan = _infer_plot_style(
            chart_type, x, y, records, style=style, palette=palette, theme=theme)
        # Auto-detect the point-size column for bubble/dotplot enrichment
        # charts when the LLM did not specify one explicitly.
        if not size and plan.get("size_col"):
            size = plan["size_col"]

        # 3b) FDR (Benjamini-Hochberg) is computed in R, not Python. When the
        #     data only carries raw p-values and the user asked for an
        #     FDR-colored plot (e.g. enrichment bubble charts), we set a flag
        #     and let the generated R script call p.adjust(..., method = "BH")
        #     to produce the ``fdr`` column before plotting. Never silently
        #     assume p == FDR.
        pvalue_col = plan.get("pvalue_col", "")
        compute_fdr = False
        if (pvalue_col and not plan.get("has_fdr")
                and plan.get("color_col") == pvalue_col
                and pvalue_col in plot_data.columns):
            compute_fdr = True
            plan["color_col"] = "fdr"
            plan["sort_col"] = "fdr"
            logger.info(
                f"FDR will be computed in R via p.adjust('{pvalue_col}', method='BH') "
                f"-> color_col='fdr'")

        logger.info(
            f"Plot style inferred | style='{plan['style']}' palette='{plan['palette']}' "
            f"theme='{plan['theme']}' sort_col='{plan['sort_col']}' "
            f"color_col='{plan['color_col']}' signif_col='{plan['signif_col']}' "
            f"size_col='{size}'")

        # 4) Translate the semantic spec into R code (system-side, safe).
        #    If the chart type is not in the preset list, fall back to letting
        #    the LLM write the R code itself (see _build_custom_r_code).
        from src.core.plot_engine import PlotEngine

        ct = (chart_type or "").strip().lower()
        if ct in PlotEngine.SUPPORTED_CHART_TYPES:
            try:
                r_code = engine.spec_to_r_code(
                    chart_type=chart_type, x=x, y=y, size=size, label=label,
                    title=title, log_transform_y=bool(log_col),
                    style=plan["style"], palette=plan["palette"], theme=plan["theme"],
                    sort_col=plan["sort_col"], sort_desc=plan["sort_desc"],
                    color_col=plan["color_col"], signif_col=plan["signif_col"],
                    compute_fdr=compute_fdr, fdr_source_col=pvalue_col)
            except ValueError as ve:
                return json.dumps({"status": "error", "message": str(ve)}, ensure_ascii=False)
            extra_packages = PlotEngine._EXTENDED_TYPE_PACKAGES.get(ct, [])
        else:
            # Unknown / uncommon chart type: not in the preset list. Return a
            # clear, actionable message so the LLM can ask the user for more
            # detail (data structure, exact chart type, styling) and retry.
            return json.dumps({
                "status": "error",
                "message": (
                    f"Chart type '{chart_type}' is not in the preset list. "
                    f"Supported types: {sorted(PlotEngine.SUPPORTED_CHART_TYPES)}. "
                    "To draw this chart well, please provide more detail: "
                    "1) the data structure (column names and types), "
                    "2) the exact chart type you want, "
                    "3) any special styling / annotation requirements."
                ),
            }, ensure_ascii=False)

        result = engine.run_plot(r_code, plot_data, job_id, extra_packages=extra_packages)

        # 3) On failure, surface the R error to the frontend.
        if not result.success:
            err = result.error_message or "Unknown error"
            return json.dumps({
                "status": "error",
                "message": f"R plotting failed: {err}",
                "r_stderr": result.stderr[:2000],
            }, ensure_ascii=False)

        # 4) On success, return the structured payload for the runtime to display.
        return json.dumps(
            _build_plot_payload(result, plot_data, title, extra_packages=extra_packages),
            ensure_ascii=False,
        )

    except Exception as e:
        logger.error(f"plot_chart failed: {e}")
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)


def _build_plot_payload(result, plot_data, chart_title: str,
                        extra_packages: Optional[list] = None) -> dict:
    """Assemble a structured payload (image paths + data preview + download links)."""
    return {
        "status": "success",
        "chart_title": chart_title,
        "svg_path": result.svg_path,
        "png_path": result.png_path,
        "pdf_path": result.pdf_path,
        "script_path": result.script_path,
        # Pure plotting code path: enables natural-language edits of the chart
        # in later turns without re-running the data marshalling step.
        "code_path": getattr(result, "code_path", "") or "",
        "data_path": plot_data.data_path,
        "columns": plot_data.columns,
        "column_types": dict(plot_data.column_types or {}),
        "preview": plot_data.preview[:5],
        "total_rows": plot_data.total_rows,
        "extra_packages": list(extra_packages or []),
    }
