"""Academic plotting tool: declarative chart spec -> safe R/ggplot2 code.

The default look of every figure comes from :mod:`src.core.plot_styles`
(the reference SCI-figure style registry); this module only *inspects the data*
and decides which of those defaults fit, plus the data-driven extras (term
ordering, significance colour mapping, stars). Explicit user requirements
passed through ``style`` / ``palette`` / ``theme`` always win.
"""
import json
import re
import time
from typing import Optional

from src.core import plot_styles
from src.core.academic.base import logger

__all__ = ["plot_chart"]

# Column-name aliases used to recognize p-value / significance columns.
_PVALUE_ALIASES = ("pvalue", "p_value", "p-value", "pval", "p", "padj",
                   "fdr", "qvalue", "q_value", "adj_p", "adjp")
# Column-name aliases for the categorical term / pathway label.
_TERM_ALIASES = ("term", "pathway", "description", "name", "category",
                 "go_term", "kegg", "id", "gene_set")

# Chart types where a continuous statistic (raw p-value, FDR, ...) is mapped to
# colour. Other types either colour by their own categories (boxplot / violin /
# density / ridge fill by the X groups, handled in the engine) or by their own
# ramp (heatmap / corrplot), so a p-value column must NOT be injected there.
_CONTINUOUS_COLOR_TYPES = {"bubble", "dotplot", "bar", "scatter"}

#: Chart types whose categorical axis is horizontal (terms stacked vertically).
_VERTICAL_TERM_TYPES = ("bar", "bubble", "dotplot")


def _discrete_stats(records: list, column: str):
    """Return ``(n_levels, max_label_len)`` of a categorical column.

    Used to size the canvas, pick palette length and decide whether labels need
    rotation / smaller ticks — every layout decision is data-driven.
    """
    if not column:
        return 0, 0
    values = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        v = rec.get(column, "")
        if v is None or v == "":
            continue
        values.append(str(v))
    if not values:
        return 0, 0
    uniq = set(values)
    return len(uniq), max(len(v) for v in uniq)


def _infer_plot_style(chart_type: str, x: str, y: str, records: list,
                      style: str = "", palette: str = "", theme: str = "") -> dict:
    """Analyze the data and infer an academic plotting plan.

    This is the "think before you draw" step: it inspects the actual records
    (column names + value ranges) and decides, for the requested chart type:

      * which column to sort the categorical axis by (e.g. pvalue so the most
        significant term ends up on top — the reference figure convention);
      * whether a continuous colour gradient is warranted (e.g. FDR);
      * whether significance stars (* / ** / ***) should be drawn;
      * which journal palette fits the number of groups present in the data.

    Style defaults are taken from :mod:`src.core.plot_styles`; explicit
    ``style`` / ``palette`` / ``theme`` from the caller (honoring the user's
    special requirements) always win over the inferred ones.

    Returns a dict with keys: style, palette, theme, sort_col, sort_desc,
    color_col, signif_col, size_col.
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
    term_col = find_col(_TERM_ALIASES)  # noqa: F841 (documented alias probe)

    # Per-chart-type defaults from the reference style registry.
    chart_defaults = plot_styles.chart_style(ct)

    # Defaults: explicit arguments first, then the chart-type reference style.
    out = {
        "style": style or "publication",
        "palette": palette or str(chart_defaults.get("palette") or ""),
        "theme": theme or str(chart_defaults.get("theme") or ""),
        "sort_col": "",
        "sort_desc": bool(chart_defaults.get("sort_desc", False)),
        "color_col": "",
        "signif_col": "",
        "size_col": "",
        "pvalue_col": pvalue_col,
        "has_fdr": bool(find_col(("fdr", "qvalue", "q_value", "padj", "adj_p", "adjp"))),
    }

    # Enrichment-like data (has a p-value column) -> sort by significance and
    # (for chart types that show a continuous statistic) colour by it. The most
    # significant term ends up on TOP: in the flipped horizontal bar that means
    # ordering by DESCENDING p-value, i.e. ``reorder(term, -pvalue)``.
    if pvalue_col:
        out["sort_col"] = pvalue_col
        out["sort_desc"] = True
        if ct in _CONTINUOUS_COLOR_TYPES:
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
    # When the caller names a journal preset (``style``), theme and palette are
    # resolved by the engine from that preset (an explicit palette/theme still
    # wins there) — otherwise "style='nature'" would still come out red/blue.
    if style:
        out["palette"] = palette or ""
        out["theme"] = theme or ""
    elif not palette:
        # The chart-type default (registry) already matches the reference
        # figures. Only the categorical "distribution" family needs a
        # data-driven choice: the reference set uses the red/blue pair for two
        # groups, a journal palette up to ~6 groups, and base R ``rainbow()``
        # beyond that.
        group_col = ""
        if ct in ("boxplot", "violin", "histogram"):
            group_col = x
        elif ct in ("scatter", "density", "ridge"):
            group_col = out["color_col"]
        if group_col:
            values = {str(rec.get(group_col, "")) for rec in records
                      if isinstance(rec, dict)}
            n_groups = len(values)
            if n_groups == 2:
                # 常规固定语义：两分组对照（对照/处理、低/高表达）保持蓝-红。
                out["palette"] = "redblue"
            else:
                # 其余一律使用统一的低饱和学术配色；类别 >10 时由
                # plot_styles 用同族低彩度 HCL 色相生成，保证图内风格一致。
                out["palette"] = plot_styles.ACADEMIC_PALETTE
            logger.debug(
                f"Palette by group count | chart={ct} column='{group_col}' "
                f"groups={n_groups} -> '{out['palette']}'")
        if not out["palette"]:
            out["palette"] = str(chart_defaults.get("palette")
                                 or plot_styles.DEFAULT_PALETTE)

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
    gradient (e.g. FDR), and whether to draw significance stars
    (* / ** / ***).
    The figure is then drawn in the project default style: ONE unified
    low-saturation academic palette for all categorical mappings (>10 categories
    are generated on a low-chroma HCL hue wheel so the look stays consistent),
    fixed-semantics colours only where convention demands them (volcano Up/Down,
    blue-white-red heatmaps and correlation matrices, red-to-blue FDR, blue/red
    two-group comparisons), a canvas size derived from the data volume, and
    automatic guards against overlapping labels (rotated/reduced ticks, ggrepel
    for point labels, top-30 term truncation, adaptive heatmap fonts).
    If the user has special requirements, pass them via ``style`` / ``palette`` /
    ``theme`` and they will be honored — an explicit requirement always wins.

    Supported chart_type values (covers most common scientific figures):
        "scatter"  — plain scatter (x, y numeric).
        "bubble"   — scatter with point size (e.g. GO/KEGG enrichment).
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
        style      : optional journal preset — "publication" (project default),
                     "nature", "cell", "minimal", "clusterprofiler", or "custom".
        palette    : optional color palette name — "academic" (the unified
                     low-saturation default), plus "redblue" (two-group),
                     "volcano" (up/down), "viridis", "magma", "npg", "jco",
                     "aaas", "lancet", "set1", "set2", "nature", "cell", "gsea",
                     "alluvial", "pie_teal", "rdbu", "rainbow". Overrides the preset.
        theme      : optional theme ("bw", "classic", "minimal", "pubr",
                     "void"). Overrides the preset.

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
        ct = (chart_type or "").strip().lower()
        plan = _infer_plot_style(
            chart_type, x, y, records, style=style, palette=palette, theme=theme)
        # Auto-detect the point-size column for bubble/dotplot enrichment
        # charts when the LLM did not specify one explicitly.
        if not size and plan.get("size_col"):
            size = plan["size_col"]

        # 3a) Colour gradients and significance stars only make sense on numeric
        #     columns. A non-numeric (e.g. categorical) column would make ggplot
        #     abort with "Discrete value supplied to continuous scale", so the
        #     mapping is dropped instead of producing a broken figure.
        for key in ("color_col", "signif_col"):
            col = plan.get(key) or ""
            if col and plot_data.column_types.get(col) not in ("numeric", "integer"):
                logger.warning(
                    f"Column '{col}' is not numeric; dropping the {key} mapping.")
                plan[key] = ""

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

        # ---- 数据量驱动的版式：画布尺寸、刻度字号、条目截断 ----------------
        # 高分期刊的图按数据密度定幅：条目多→加高、标签长→加宽，并自动缩小
        # 密集轴的刻度字号；条目超过上限时只画主要的前 N 条，避免标签相互重叠。
        notes: list = []
        cat_col = ""
        series_col = ""
        plan_color_col = plan.get("color_col") or ""
        if ct == "bar":
            cat_col = x               # 横向条形：分类列在 x，数值列在 y
        elif ct in ("bubble", "dotplot"):
            cat_col = y               # 富集气泡/点图：术语在 y，数值在 x
        elif ct in ("boxplot", "violin", "density", "histogram", "alluvial",
                    "pie", "donut"):
            cat_col = x
        elif ct == "ridge":
            cat_col = y
        elif ct in ("line", "area"):
            cat_col = series_col = plan_color_col or label
        elif ct == "scatter":
            cat_col = plan_color_col
        elif ct == "heatmap":
            cat_col = label or x      # 行名即类别：用于估行数与标签长度

        n_categories, max_label_len = _discrete_stats(records, cat_col)
        n_series = _discrete_stats(records, series_col)[0] if series_col else 0

        # 离散色标实际映射的列（决定需要生成几种颜色）。
        scale_col = ""
        if ct in ("boxplot", "violin"):
            scale_col = plan.get("color_col") or x
        elif ct == "ridge":
            scale_col = plan.get("color_col") or y
        elif ct in ("alluvial", "pie", "donut"):
            scale_col = x
        elif ct in ("line", "area", "density"):
            scale_col = plan.get("color_col") or ""
        n_levels = _discrete_stats(records, scale_col)[0] if scale_col else 0

        # 刻度字号只对使用 ggplot 主题的图型有效（热图/相关图/饼图/网络图由各自的
        # 引擎排版，字号在 R 侧自适应），因此这两类不参与字号下调，也不产生提示。
        themed = ct not in ("heatmap", "corrplot", "pie", "donut", "network")
        axis_size = 0
        if themed:
            axis_size = plot_styles.axis_text_size(n_categories, max_label_len)
        if axis_size:
            notes.append(
                f"Dense '{cat_col}' axis ({n_categories} categories, longest label "
                f"{max_label_len} characters): tick labels reduced to {axis_size} pt "
                f"and the canvas enlarged.")

        # 条目截断：仅横向条目图（条形 / 富集气泡 / 点图），参考图库 bioR02 的
        # showNum = 30 做法，只保留数值最大的前 N 条。
        top_n = 0
        top_by = ""
        if ct in _VERTICAL_TERM_TYPES and n_categories > plot_styles.CATEGORY_LIMIT:
            order_col = x if ct in ("bubble", "dotplot") else y
            if plot_data.column_types.get(order_col) in ("numeric", "integer"):
                top_n = plot_styles.CATEGORY_LIMIT
                top_by = order_col
                notes.append(
                    f"Only the top {top_n} of {n_categories} categories are charted "
                    f"(ordered by '{order_col}') so the axis labels stay readable.")
            else:
                logger.warning(
                    f"Cannot order '{order_col}' numerically; keeping all "
                    f"{n_categories} categories.")

        figure_size = plot_styles.dynamic_canvas(
            ct, n_categories=n_categories, max_label_len=max_label_len,
            n_series=n_series)
        logger.info(
            f"Plot style inferred | style='{plan['style']}' palette='{plan['palette']}' "
            f"theme='{plan['theme']}' sort_col='{plan['sort_col']}' "
            f"color_col='{plan['color_col']}' signif_col='{plan['signif_col']}' "
            f"size_col='{size}' | categories={n_categories} "
            f"max_label_len={max_label_len} series={n_series} n_levels={n_levels} "
            f"axis_text={axis_size or 10}pt top_n={top_n or 'all'} | "
            f"canvas={figure_size[0]}x{figure_size[1]}in")

        # 4) Translate the semantic spec into R code (system-side, safe).
        #    If the chart type is not in the preset list, fall back to letting
        #    the LLM write the R code itself (see _build_custom_r_code).
        from src.core.plot_engine import PlotEngine

        if ct in PlotEngine.SUPPORTED_CHART_TYPES:
            try:
                r_code = engine.spec_to_r_code(
                    chart_type=chart_type, x=x, y=y, size=size, label=label,
                    title=title, log_transform_y=bool(log_col),
                    style=plan["style"], palette=plan["palette"], theme=plan["theme"],
                    sort_col=plan["sort_col"], sort_desc=plan["sort_desc"],
                    color_col=plan["color_col"], signif_col=plan["signif_col"],
                    compute_fdr=compute_fdr, fdr_source_col=pvalue_col,
                    n_levels=n_levels, axis_text_size=axis_size)
            except ValueError as ve:
                return json.dumps({"status": "error", "message": str(ve)}, ensure_ascii=False)
            # 条目截断块必须放在最前面：后续所有图层都基于裁剪后的 .data。
            if top_n and top_by:
                r_code = PlotEngine.top_n_r_block(top_by, top_n) + "\n" + r_code
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

        result = engine.run_plot(r_code, plot_data, job_id,
                                 extra_packages=extra_packages,
                                 figure_size=figure_size)

        # 3) On failure, surface the R error to the frontend.
        if not result.success:
            err = result.error_message or "Unknown error"
            payload = {
                "status": "error",
                "message": f"R plotting failed: {err}",
                "r_stderr": result.stderr[:2000],
            }
            # 平台相关的修复指引随错误一起回传：运行时据此渲染统一错误面板，
            # 用户不必依赖模型转述 R 的原始 stderr（模型会照抄 install.packages，
            # 在 NixOS 上那是走不通的命令）。
            try:
                from src.core.r_engine import plot_failure_payload
                payload["guidance"] = plot_failure_payload(err)
            except Exception as e:
                logger.warning(f"Plot failure guidance unavailable: {e}")
            return json.dumps(payload, ensure_ascii=False)

        # 4) On success, return the structured payload for the runtime to display.
        return json.dumps(
            _build_plot_payload(result, plot_data, title, chart_type=ct,
                                extra_packages=extra_packages, notes=notes),
            ensure_ascii=False,
        )

    except Exception as e:
        logger.error(f"plot_chart failed: {e}")
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)


def _build_plot_payload(result, plot_data, chart_title: str,
                        chart_type: str = "",
                        extra_packages: Optional[list] = None,
                        notes: Optional[list] = None) -> dict:
    """Assemble a structured payload (image paths + data preview + downloads).

    ``chart_type`` and ``figure_size`` travel with the payload so the runtime
    can label the chart card and re-render modifications on the same canvas;
    ``notes`` records layout decisions the user should know about (e.g. term
    truncation) so the model can mention them instead of silently hiding data.
    """
    size = getattr(result, "figure_size", None) or plot_styles.canvas(chart_type)
    return {
        "status": "success",
        "chart_title": chart_title,
        "chart_type": chart_type or "",
        "figure_size": [float(size[0]), float(size[1])],
        "notes": list(notes or []),
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
