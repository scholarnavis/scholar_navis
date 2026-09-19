"""Publication chart style registry — the single source of truth for figure looks.

Every figure the agent renders through the R plotting pipeline takes its
**default** appearance from this module. The defaults were derived by reading a
reference library of SCI-style R figures (``bioR02`` … ``bioR51``: bar / box /
violin / bubble / volcano / heatmap / corrplot / PCA / ROC / survival /
circos …). What that library actually does, and what is therefore encoded here:

* **Canvas** — every script saves with an explicit journal size in inches
  (``pdf(width=..., height=...)`` / ``ggsave(width, height)``); no script relies
  on a device default. -> :data:`CHART_CANVAS`
* **Base theme** — ``theme_bw()`` dominates, ``ggpubr::theme_pubr()`` for group
  comparisons, ``theme_minimal()`` occasionally, and a blank-grid/black-axis-line
  variant for stacked multi-series panels; axis text is black 10 pt, axis titles
  bold, titles bold and centred. -> :data:`THEME_BASE`
* **Colour** — ``npg`` / ``jco`` / ``aaas`` / ``lancet`` journal palettes, base R
  ``rainbow()`` once more than ~4-6 categories exist, ``c("blue", "red")`` for
  two-group comparisons, and the blue-white-red ramp (``colorRampPalette(c(
  "blue", "white", "red"))(50)``) for heatmaps / correlation matrices, with a
  red-to-blue ramp for FDR (small FDR = red = significant).
  -> :data:`DISCRETE_PALETTES`, :data:`CONTINUOUS_RAMPS`
* **Per-figure-type contract** — axis expansion for bars, term ordering (the most
  significant / most abundant term on top), significance stars, size legends,
  percentage labels. -> :data:`CHART_STYLES`

Precedence (highest first)::

    1. the user's explicit requirement — the ``style`` / ``palette`` / ``theme``
       arguments of ``plot_chart``, or a natural-language edit through
       ``modify_chart``;
    2. the per-chart-type default in :data:`CHART_STYLES`;
    3. the journal preset in :data:`STYLE_PRESETS`.

The module is deliberately dependency-free (pure data + tiny string helpers) so
that the R code generator, the style-inference step and the LLM-facing tool
descriptions can all import it without creating an import cycle.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("Core.PlotStyles")

#: Human-readable provenance of the defaults below (also quoted to the LLM).
STYLE_REFERENCE = "R reference library of 50 SCI figures (bioR02-bioR51)"

# ---------------------------------------------------------------------- #
#  Canvas sizes (inches) — taken from the reference scripts' device calls
# ---------------------------------------------------------------------- #
#: Per-chart-type figure size in inches, as published by the reference set.
CHART_CANVAS: Dict[str, Tuple[float, float]] = {
    "bar": (7.0, 5.0),          # bioR03/bioR05 horizontal bars
    "boxplot": (6.0, 5.0),      # bioR08/bioR11 group distributions
    "violin": (6.0, 5.0),       # bioR11/bioR12
    "bubble": (7.0, 5.0),       # bioR29 enrichment dotplot
    "dotplot": (7.0, 5.0),      # bioR30 lollipop / dot
    "scatter": (5.5, 4.8),      # bioR22 correlation scatter / bioR45 PCA
    "volcano": (5.5, 4.5),      # bioR19
    "heatmap": (6.0, 5.5),      # bioR17 expression heatmap
    "corrplot": (7.0, 7.0),     # bioR23 correlation matrix
    "pie": (7.0, 6.0),          # bioR28
    "donut": (7.0, 6.0),        # bioR28
    "ridge": (7.0, 5.5),
    "density": (6.0, 5.0),
    "histogram": (6.0, 5.0),
    "line": (8.0, 5.5),         # bioR33 multi-GSEA line panel
    "area": (8.0, 5.5),
    "alluvial": (7.0, 6.0),     # bioR27
    "network": (7.0, 6.0),      # bioR25
}

#: Fallback when a chart type has no reference-derived size.
DEFAULT_CANVAS: Tuple[float, float] = (7.0, 5.5)

#: 画布上下限（英寸）：下限=期刊单栏可读尺寸，上限避免极端数据量下产出无法
#: 查看的巨图。
CANVAS_LIMITS: Dict[str, Tuple[float, float]] = {"w": (3.5, 18.0), "h": (3.0, 18.0)}


def canvas(chart_type: str) -> Tuple[float, float]:
    """Return the reference-derived ``(width, height)`` in inches."""
    return CHART_CANVAS.get((chart_type or "").strip().lower(), DEFAULT_CANVAS)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def dynamic_canvas(chart_type: str, n_categories: int = 0, max_label_len: int = 0,
                   n_series: int = 0) -> Tuple[float, float]:
    """图幅随数据量动态调整（英寸），夹在 :data:`CANVAS_LIMITS` 之内。

    高分期刊的图都按数据密度定幅：横排条目越多图越高、分类标签越长图越宽、
    多序列需要额外空间。此处以参考图库给出的基准尺寸为起点做增量：

    * 横向条形 / 富集气泡 / 点图：条目数决定高度（每条约 0.30 in），最长术语
      决定宽度（富集 Term 常常 40+ 字符）。
    * 箱线 / 小提琴 / 密度 / 山脊：分组数决定宽度（每组约 0.35 in），并为旋转
      标签留出高度。
    * 折线 / 面积：序列数与标签长度小幅增大。
    * 热图：行数决定高度、列数决定宽度，避免格子被压扁。
    * 饼 / 环 / 网络 等定幅图型不随类别数放大（类别靠图例与颜色区分）。

    设计意图：条目多→加高（而非缩小字号），标签长→加宽（而非截断），
    从源头避免文本标签互相重叠。
    """
    ct = (chart_type or "").strip().lower()
    w, h = canvas(ct)

    if ct in ("bar", "bubble", "dotplot"):
        if n_categories:
            h = max(h, 1.4 + 0.30 * n_categories)
        if max_label_len > 18:
            w += min(3.0, (max_label_len - 18) * 0.06)
    elif ct in ("boxplot", "violin", "density", "histogram", "ridge"):
        extra = max(0, n_categories - 3)
        if extra:
            w += min(4.0, 0.35 * extra)
            h += min(1.6, 0.06 * extra)
    elif ct in ("line", "area"):
        if n_series > 2:
            h += min(2.0, 0.18 * (n_series - 2))
        if max_label_len > 10:
            w += min(2.0, (max_label_len - 10) * 0.05)
    elif ct == "heatmap":
        if n_categories:
            h = max(h, 1.2 + 0.16 * n_categories)
        if n_series > 2:
            w = max(w, 1.5 + 0.30 * n_series)
    elif ct == "alluvial":
        if n_categories > 6:
            h += min(2.5, 0.20 * (n_categories - 6))
    elif ct == "scatter":
        if max_label_len > 12:
            w += min(1.5, (max_label_len - 12) * 0.04)

    return (round(_clamp(w, *CANVAS_LIMITS["w"]), 2),
            round(_clamp(h, *CANVAS_LIMITS["h"]), 2))


def axis_text_size(n_categories: int = 0, max_label_len: int = 0) -> int:
    """密集分类轴自动缩小的刻度字号（0 = 用主题默认 10 pt）。

    条目多或标签长时逐级缩小，配合标签旋转，避免刻度文字互相挤压。
    """
    if n_categories >= 34 or max_label_len >= 42:
        return 7
    if n_categories >= 22 or max_label_len >= 30:
        return 8
    return 0


# ---------------------------------------------------------------------- #
#  Base themes
# ---------------------------------------------------------------------- #
#: Journal base themes. Names are stable identifiers used by ``plot_chart``.
THEME_BASE: Dict[str, str] = {
    "bw": "theme_bw(base_size = 12)",
    "classic": "theme_classic(base_size = 12)",
    "minimal": "theme_minimal(base_size = 12)",
    # ggpubr is installed for group-comparison charts (boxplot / violin).
    "pubr": "ggpubr::theme_pubr(base_size = 12)",
    "void": "theme_void(base_size = 12)",
}
DEFAULT_THEME = "bw"

#: Typography / layout shared by every themed figure (reference convention:
#: black axis text ~10 pt, bold axis titles, bold centred title, no minor grid,
#: thin black panel border).
THEME_TYPOGRAPHY = (
    "plot.title = element_text(face = 'bold', size = 14, hjust = 0.5),\n"
    "axis.title = element_text(face = 'bold', size = 12),\n"
    "axis.text = element_text(color = 'black', size = 10),\n"
    "legend.title = element_text(face = 'bold', size = 10),\n"
    "legend.text = element_text(size = 9),\n"
    "panel.grid.minor = element_blank()"
)

#: ``theme_classic`` panels have no border in the reference figures (bioR33
#: multi-GSEA panel keeps only the black axis line), so the border is optional.
PANEL_BORDER = "panel.border = element_rect(color = 'black', linewidth = 0.8)"


def theme_block(theme: str = "", axis_text_size: int = 0) -> str:
    """Return the R ``theme(...)`` expression for a base-theme identifier.

    ``axis_text_size`` > 0 overrides the default 10 pt tick label size (used for
    dense category axes so labels stay readable without overlapping). ``void``
    is returned as-is (radial plots build their own layout).
    """
    name = (theme or "").strip().lower()
    if name == "void":
        return THEME_BASE["void"]
    base = THEME_BASE.get(name, THEME_BASE[DEFAULT_THEME])
    # 每行去掉行尾逗号后统一用 ",\n" 拼接，避免出现空参数（R 能解析 "a=1,," 却在
    # 求值时报 "argument is missing, with no default"）。
    lines = [line.strip().rstrip(",") for line in THEME_TYPOGRAPHY.split("\n")
             if line.strip()]
    if axis_text_size and int(axis_text_size) > 0:
        size = int(axis_text_size)
        lines = [
            f"axis.text = element_text(color = 'black', size = {size})"
            if line.startswith("axis.text =") else line
            for line in lines
        ]
    if name != "classic":
        lines.append(PANEL_BORDER)
    return f"{base} +\n  theme(\n    " + ",\n    ".join(lines) + "\n  )"


# ---------------------------------------------------------------------- #
#  Unified academic palette (low saturation)
# ---------------------------------------------------------------------- #
#: 统一分类配色：低饱和度、相邻对比度适中的学术风格色板。
#:
#: 高分期刊常见做法是"同一篇文章/同一张图里所有分类共用一套克制的色系"，
#: 因此除少数有**固定色彩语义**的图以外（火山图上下调、热图与相关矩阵
#: 蓝白红、FDR 连续色标、两分组对照），其余图型一律使用这一套配色：
#: 类别数 <= 10 时按序取色；类别更多时用低彩度 HCL 色轮生成同风格颜色，
#: 保证图内配色逻辑一致（而不是换一套红绿彩虹色）。
ACADEMIC_CORE: List[str] = [
    "#4C72B0",  # 蓝
    "#DD8452",  # 橙
    "#55A868",  # 绿
    "#C44E52",  # 砖红
    "#8172B3",  # 紫
    "#937860",  # 棕
    "#DA8BC3",  # 玫粉
    "#8C8C8C",  # 灰
    "#CCB974",  # 芥黄
    "#64B5CD",  # 青
]
ACADEMIC_PALETTE = "academic"
#: >10 类时的兜底：固定低彩度（c）与亮度（l），仅色相均匀铺开，
#: 与上面的核心色板同属"低饱和、不刺眼"的风格。
ACADEMIC_HCL_TEMPLATE = (
    "grDevices::hcl(h = seq(15, 375, length.out = {n} + 1)[1:{n}], c = 42, l = 62)"
)

#: 分类轴条目上限：超过只画最主要的前 N 条（参考图库 bioR02 的 showNum=30
#: 做法），否则坐标轴标签必然相互挤压。
CATEGORY_LIMIT = 30

#: 有固定色彩语义的"常规图"配色（保留传统语义，统一做低饱和处理）。
CONVENTIONAL_COLORS: Dict[str, object] = {
    # 两分组对照：对照/处理、低/高表达
    "two_group": ["#4C72B0", "#C44E52"],
    # 火山图 / 上下调：红=上调，蓝=下调，灰=不显著
    "up_down": {"up": "#C44E52", "down": "#4C72B0", "ns": "#BFBFBF"},
}


def academic_values_expr(n: int = 0) -> str:
    """R 表达式：为 ``n`` 个分类生成同一套低饱和学术配色。"""
    if 0 < n <= len(ACADEMIC_CORE):
        return "c(" + ", ".join(f"'{c}'" for c in ACADEMIC_CORE[:n]) + ")"
    if n > len(ACADEMIC_CORE):
        logger.debug(f"Academic palette: {n} categories -> low-chroma HCL wheel")
        return ACADEMIC_HCL_TEMPLATE.format(n=n)
    return "c(" + ", ".join(f"'{c}'" for c in ACADEMIC_CORE) + ")"


def two_group_colors() -> List[str]:
    """Two-group comparison pair (control/treatment, low/high)."""
    return list(CONVENTIONAL_COLORS["two_group"])  # type: ignore[arg-type]


def up_down_colors() -> Dict[str, str]:
    """Volcano / up-down-regulation colours."""
    return dict(CONVENTIONAL_COLORS["up_down"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------- #
#  Discrete palettes
# ---------------------------------------------------------------------- #
#: Named palettes. ``academic`` is the project default; the others exist for
#: explicit user requests (journal presets, ggsci-style looks) and for figures
#: whose colour semantics are fixed by convention.
DISCRETE_PALETTES: Dict[str, List[str]] = {
    # 统一低饱和学术配色（默认）
    "academic": ACADEMIC_CORE,
    # ggsci npg — bioR05 grouped bar
    "npg": ["#E64B35", "#4DBBD5", "#00A087", "#3C5488", "#F39B7F",
            "#8491B4", "#91D1C2", "#DC0000", "#7E6148", "#B09C85"],
    # ggsci jco — bioR14 paired diff / bioR16 deviation
    "jco": ["#0073C2", "#EFC000", "#868686", "#CD534C", "#7AA6DC",
            "#003C67", "#8F7700", "#3B3B3B", "#A73030", "#4A6990"],
    # ggsci aaas — bioR30 lollipop
    "aaas": ["#3B4992", "#EE0000", "#008B45", "#631879", "#008280",
             "#BB0021", "#5F559B", "#A20056", "#808180", "#1B1919"],
    # ggsci lancet
    "lancet": ["#00468B", "#ED0000", "#42B540", "#0099B4", "#925E9F",
               "#FDAF91", "#AD002A", "#ADB6B6", "#1B1919"],
    # ColorBrewer Set1 / Set2 (high contrast vs soft categorical)
    "set1": ["#E41A1C", "#377EB8", "#4DAF4A", "#984EA3", "#FF7F00",
             "#FFFF33", "#A65628", "#F781BF", "#999999"],
    "set2": ["#66C2A5", "#FC8D62", "#8DA0CB", "#E78AC3", "#A6D854",
             "#FFD92F", "#E5C494", "#B3B3B3"],
    # Journal red/blue sets
    "nature": ["#E64B35", "#4DBAE5", "#3C5488", "#F39B7F", "#00A087", "#8491B4"],
    "cell": ["#E64B35", "#4DBAE5", "#3C5488", "#F39B7F", "#00A087", "#8491B4"],
    # 两分组对照（常规固定语义，低饱和版）
    "redblue": ["#4C72B0", "#C44E52"],
    # 火山图 Down / NS / Up（常规固定语义，低饱和版）
    "volcano": ["#4C72B0", "#BFBFBF", "#C44E52"],
    # Multi-series trend panels (bioR27 alluvial / bioR33 multi-GSEA)
    "gsea": ["#58CDD9", "#7A142C", "#5D90BA", "#431A3D", "#91612D", "#6E568C",
             "#E0367A", "#D8D155", "#64495D", "#7CC767", "#223D6C", "#D20A13",
             "#FFD121", "#088247", "#11AA4D"],
    "alluvial": ["#029149", "#6E568C", "#E0367A", "#D8D155", "#223D6C",
                 "#D20A13", "#431A3D", "#91612D", "#FFD121", "#088247",
                 "#11AA4D", "#58CDD9", "#7A142C", "#5D90BA", "#64495D",
                 "#7CC767"],
    # Single-hue sequential scale for pie / donut (bioR28: #074284 -> #f3faed)
    "pie_teal": ["#074284", "#006097", "#007CA1", "#3A97A8",
                 "#6DB1B0", "#9DC9BC", "#CAE1D0", "#F3FAED"],
    # base R rainbow(12) — 目录参考用，仅在用户明确要求时使用
    "rainbow": ["#FF0000", "#FF8000", "#FFFF00", "#80FF00", "#00FF00", "#00FF80",
                "#00FFFF", "#0080FF", "#0000FF", "#8000FF", "#FF00FF", "#FF0080"],
    # Perceptually uniform sequential ramps
    "viridis": ["#440154", "#3B528B", "#21918C", "#5EC962", "#FDE725"],
    "magma": ["#000004", "#51127C", "#B63679", "#FB8861", "#FCFDBF"],
    # ColorBrewer RdBu (clusterProfiler-style enrichment gradient)
    "rdbu": ["#67001F", "#B2182B", "#D6604D", "#F4A582", "#FDDBC7", "#F7F7F7",
             "#D1E5F0", "#92C5DE", "#4393C3", "#2166AC", "#053061"],
}

DEFAULT_PALETTE = ACADEMIC_PALETTE


def palette_vector(name: str = "") -> str:
    """Return an R ``c('#...', ...)`` literal for a discrete palette name."""
    key = (name or "").strip().lower()
    colours = DISCRETE_PALETTES.get(key)
    if not colours:
        if key:
            logger.warning(f"Unknown palette '{name}', falling back to '{DEFAULT_PALETTE}'.")
        colours = DISCRETE_PALETTES[DEFAULT_PALETTE]
    return "c(" + ", ".join(f"'{c}'" for c in colours) + ")"


def scale_values_expr(palette: str = "", n_levels: int = 0) -> str:
    """R expression for a discrete scale's ``values``.

    The unified academic palette is generated for exactly ``n_levels``
    categories (low-saturation HCL wheel beyond 10), so a figure with many
    groups stays in the same visual family instead of cycling a short palette.
    """
    key = (palette or "").strip().lower()
    if (not key or key == ACADEMIC_PALETTE) and n_levels > 0:
        return academic_values_expr(n_levels)
    return palette_vector(key)


def manual_scale_expr(aesthetic: str, palette: str = "", column: str = "",
                      n_levels: int = 0) -> str:
    """Discrete colour scale (``fill`` / ``color``) bound to a data column.

    ``rep(<values>, length.out = length(unique(<column>)))`` keeps the scale
    working even when the real level count differs from the predicted one
    (ggplot2 would otherwise abort with "Insufficient values in manual scale").
    """
    values = scale_values_expr(palette, n_levels)
    if column:
        values = (f"rep({values}, "
                  f"length.out = length(unique(.data[[\"{column}\"]])))")
    return f"scale_{aesthetic}_manual(values = {values})"


def first_color(name: str = "") -> str:
    """First colour of a discrete palette (used by single-colour geoms)."""
    key = (name or "").strip().lower()
    colours = DISCRETE_PALETTES.get(key) or DISCRETE_PALETTES[DEFAULT_PALETTE]
    return colours[0]


def is_known_palette(name: str) -> bool:
    return (name or "").strip().lower() in DISCRETE_PALETTES


# ---------------------------------------------------------------------- #
#  Continuous ramps
# ---------------------------------------------------------------------- #
#: Continuous colour ramps. ``low`` -> ``high`` (``mid`` set = diverging).
#: 低饱和版本：与分类配色同源（蓝 #4C72B0 / 红 #C44E52 / 绿 #55A868），
#: 使同一张图内"分类色"与"连续色标"看起来出自同一套设计。
CONTINUOUS_RAMPS: Dict[str, Dict[str, object]] = {
    # Heatmap / correlation matrix（常规固定语义）：蓝-白-红，50 级
    "bwr": {"low": "#4C72B0", "mid": "#F7F7F7", "high": "#C44E52",
            "kind": "diverging", "steps": 50},
    # 绿-白-红变体
    "gwr": {"low": "#55A868", "mid": "#F7F7F7", "high": "#C44E52",
            "kind": "diverging", "steps": 50},
    # FDR / 显著性渐变（常规固定语义）：FDR 小（显著）= 红 -> 蓝
    "fdr_rb": {"low": "#C44E52", "high": "#4C72B0", "kind": "sequential"},
    "viridis": {"low": "#440154", "high": "#FDE725", "kind": "sequential"},
    "magma": {"low": "#000004", "high": "#FCFDBF", "kind": "sequential"},
}


def ramp_stops(ramp: str) -> List[str]:
    """Return the colour stops of a ramp, low -> high (2 or 3 entries)."""
    spec = CONTINUOUS_RAMPS.get((ramp or "").strip().lower(),
                                CONTINUOUS_RAMPS["bwr"])
    stops = [str(spec["low"])]
    if spec.get("mid"):
        stops.append(str(spec["mid"]))
    stops.append(str(spec["high"]))
    return stops


def ramp_call(ramp: str, steps: Optional[int] = None) -> str:
    """Return an R ramp expression, e.g. ``colorRampPalette(c('blue', 'white',
    'red'))(50)`` (used by base-graphics devices such as pheatmap)."""
    spec = CONTINUOUS_RAMPS.get((ramp or "").strip().lower(),
                                CONTINUOUS_RAMPS["bwr"])
    stops = ramp_stops(ramp)
    n = int(steps or spec.get("steps") or 50)
    return "colorRampPalette(" + "c(" + ", ".join(f"'{s}'" for s in stops) + ")" + f")({n})"


def gradient_scale(ramp: str, aesthetic: str = "fill", name: str = "",
                   midpoint: Optional[float] = None) -> str:
    """Return a ggplot2 continuous scale expression for a ramp.

    ``aesthetic`` is ``fill`` or ``color``. Diverging ramps become
    ``scale_*_gradient2`` (with ``midpoint`` when supplied), sequential ramps
    ``scale_*_gradient``.
    """
    spec = CONTINUOUS_RAMPS.get((ramp or "").strip().lower(),
                                CONTINUOUS_RAMPS["bwr"])
    aes_name = "fill" if (aesthetic or "").lower().startswith("f") else "color"
    label = f", name = '{name}'" if name else ""
    if spec.get("mid"):
        mid = midpoint if midpoint is not None else (spec.get("midpoint") or 0)
        return (f"scale_{aes_name}_gradient2(low = '{spec['low']}', "
                f"mid = '{spec['mid']}', high = '{spec['high']}', "
                f"midpoint = {mid}{label})")
    return (f"scale_{aes_name}_gradient(low = '{spec['low']}', "
            f"high = '{spec['high']}'{label})")


# ---------------------------------------------------------------------- #
#  Journal presets (coarse user-facing switch)
# ---------------------------------------------------------------------- #
#: ``style`` argument of ``plot_chart``. An empty value means "use the
#: per-chart-type reference default" (see :data:`CHART_STYLES`).
STYLE_PRESETS: Dict[str, Dict[str, str]] = {
    "publication": {"theme": "", "palette": ""},
    "nature": {"theme": "classic", "palette": "npg"},
    "cell": {"theme": "bw", "palette": "aaas"},
    "minimal": {"theme": "minimal", "palette": ""},
    "clusterprofiler": {"theme": "bw", "palette": "rdbu"},
    "custom": {"theme": "", "palette": ""},
}

# ---------------------------------------------------------------------- #
#  Per-chart-type style contract
# ---------------------------------------------------------------------- #
#: Default look of each supported chart type, distilled from the reference
#: library. Keys are the ``chart_type`` values of ``plot_chart``.
CHART_STYLES: Dict[str, Dict[str, object]] = {
    "bar": {
        "theme": "pubr",
        "palette": ACADEMIC_PALETTE,
        "ramp": "fdr_rb",
        "expand_zero": True,     # bioR03/bioR05: expand = c(0, 0) on both axes
        "sort_desc": True,       # most significant / largest bar ends up on top
        "signif_stars": True,
        "reference": "bioR03/bioR05 horizontal bars: sorted, fill = FDR (red -> blue)",
    },
    "boxplot": {
        "theme": "pubr",
        "palette": ACADEMIC_PALETTE,
        "jitter": True,
        "rotate_x": 45,          # 标签旋转，配合动态加宽避免挤压
        "signif_stars": True,
        "reference": "bioR07/bioR09/bioR11: ggpubr boxplot, jitter points, significance",
    },
    "violin": {
        "theme": "pubr",
        "palette": ACADEMIC_PALETTE,
        "inner_box": True,
        "rotate_x": 45,
        "signif_stars": True,
        "reference": "bioR11/bioR12 ggviolin with inner boxplot",
    },
    "bubble": {
        "theme": "bw",
        "palette": ACADEMIC_PALETTE,
        "ramp": "fdr_rb",        # 常规固定语义：FDR 小 = 红 = 显著
        "sort_desc": True,       # largest ratio on top (clusterProfiler layout)
        "size_range": (2, 10),
        "size_label": "Count",
        "reference": "bioR29 enrichment dotplot: x = ratio, y = term, size = count, colour = FDR",
    },
    "dotplot": {
        "theme": "bw",
        "palette": ACADEMIC_PALETTE,
        "ramp": "fdr_rb",
        "size_range": (3, 8),
        "sort_desc": True,
        "reference": "bioR30 lollipop / dot chart",
    },
    "scatter": {
        "theme": "bw",
        "palette": ACADEMIC_PALETTE,
        "ramp": "viridis",       # 连续第三变量 -> 顺序色阶
        "reference": "bioR22/bioR45: theme_bw point cloud, repel labels, no major grid",
    },
    "density": {
        "theme": "bw",
        "palette": ACADEMIC_PALETTE,
        "ramp": "viridis",
        "reference": "bioR22 marginal density",
    },
    "histogram": {
        "theme": "bw",
        "palette": ACADEMIC_PALETTE,
        "reference": "theme_bw histogram, white bar border",
    },
    "ridge": {
        "theme": "bw",
        "palette": ACADEMIC_PALETTE,
        "ramp": "viridis",
        "reference": "ggridges density ridges per group",
    },
    "line": {
        "theme": "classic",      # bioR33: black axis line, no grid/border
        "palette": ACADEMIC_PALETTE,
        "line_width": 1.5,
        "reference": "bioR33 multi-GSEA trend: line width 1.5, clear axis line",
    },
    "area": {
        "theme": "classic",
        "palette": ACADEMIC_PALETTE,
        "reference": "bioR33 trend panel",
    },
    "volcano": {
        "theme": "bw",
        "palette": "volcano",    # 常规固定语义：Up 红 / Down 蓝 / NS 灰（低饱和）
        "fold_cutoff": 1.0,      # |log2FC| threshold (bioR19 logFCfilter)
        "p_cutoff": 0.05,        # FDR threshold (bioR19 fdrFilter)
        "reference": "bioR19 volcano: |log2FC| > 1 & p < 0.05, Up/NS/Down colours",
    },
    "heatmap": {
        "theme": "bw",
        "ramp": "bwr",           # 常规固定语义：蓝-白-红（低饱和），50 级
        "scale": "row",          # row-wise z-score (bioR17)
        "cluster": True,
        "font_size": 8,
        "reference": "bioR17/bioR18 pheatmap: blue-white-red, scale = 'row'",
    },
    "corrplot": {
        "theme": "bw",
        "ramp": "bwr",           # 常规固定语义：蓝-白-红（低饱和）
        "method": "circle",
        "reference": "bioR23 corrplot: circle glyphs, hclust order, upper triangle",
    },
    "pie": {
        "theme": "void",
        "palette": ACADEMIC_PALETTE,
        "percent_labels": True,  # 仅标注占比 >= 5% 的扇区
        "reference": "bioR28 pie: percentage labels; unified academic palette",
    },
    "donut": {
        "theme": "void",
        "palette": ACADEMIC_PALETTE,
        "percent_labels": True,
        "reference": "bioR28 pie (donut variant)",
    },
    "alluvial": {
        "theme": "bw",
        "palette": ACADEMIC_PALETTE,
        "stratum_width": 0.2,
        "reference": "bioR27 ggalluvial: stratum width 0.2, forward flow colouring",
    },
    "network": {
        "theme": "bw",
        "palette": ACADEMIC_PALETTE,
        "node_size": 8,
        "reference": "bioR25 correlation network: white nodes, signed edges",
    },
}


def chart_style(chart_type: str) -> Dict[str, object]:
    """Default style contract of a chart type (empty dict when unknown)."""
    return CHART_STYLES.get((chart_type or "").strip().lower(), {})


def resolve_style(chart_type: str, style: str = "", palette: str = "",
                  theme: str = "") -> Dict[str, str]:
    """Resolve the effective ``theme`` / ``palette`` / ``ramp`` for a figure.

    Explicit arguments (the user's requirement) win over the per-chart-type
    reference default, which wins over the journal preset.
    """
    key = (chart_type or "").strip().lower()
    chart = chart_style(key)
    preset_name = (style or "publication").strip().lower()
    preset = STYLE_PRESETS.get(preset_name, STYLE_PRESETS["publication"])

    resolved_theme = (theme or "").strip().lower() \
        or str(preset.get("theme") or "") or str(chart.get("theme") or "") or DEFAULT_THEME

    resolved_palette = (palette or "").strip().lower() or str(preset.get("palette") or "")
    if not resolved_palette or not is_known_palette(resolved_palette):
        if resolved_palette:
            logger.warning(f"Palette '{resolved_palette}' unknown; using chart default.")
        resolved_palette = str(chart.get("palette") or DEFAULT_PALETTE)

    resolved_ramp = str(chart.get("ramp") or "bwr")
    logger.debug(
        "Resolved plot style | chart=%s style=%s palette=%s theme=%s ramp=%s",
        key, preset_name, resolved_palette, resolved_theme, resolved_ramp,
    )
    return {
        "style": preset_name,
        "theme": resolved_theme,
        "palette": resolved_palette,
        "ramp": resolved_ramp,
        "canvas": canvas(key),
    }


# ---------------------------------------------------------------------- #
#  LLM-facing brief
# ---------------------------------------------------------------------- #
#: Compact, token-cheap description of the defaults, injected into every place
#: where the model has to decide how a figure should look.
_LLM_BRIEF = """DEFAULT FIGURE STYLE ({reference}) — apply unless the user asks otherwise:
* Palette — ONE unified low-saturation academic palette for all categorical mappings (blue / orange / green / brick red / purple / brown / rose / grey / mustard / cyan); beyond 10 categories the same family is generated on a low-chroma HCL hue wheel, so every figure shares one coherent, restrained colour logic. FIXED-SEMANTICS EXCEPTIONS only: volcano Up = red, Down = blue, NS = grey; heatmap and correlation matrix blue-white-red; FDR colour ramp red (significant) -> blue; two-group comparisons blue vs red; a continuous third variable uses viridis.
* Canvas — explicit inches derived from the DATA VOLUME (never a device default): horizontal bars / enrichment bubbles / dotplots grow about 0.30 in per term and widen with the longest term; box / violin / density widen about 0.35 in per group; heatmaps grow with rows and columns; line panels grow with the number of series; everything stays within 3.5-18 in wide and 3-18 in tall.
* Typography — theme_bw base, black 10 pt axis text (auto-reduced to 8 or 7 pt on a dense axis), bold axis titles, bold centred 14 pt title, no minor grid, thin black panel border; ggpubr theme_pubr for grouped box / violin; black axis line without grid or border for multi-series line panels.
* Anti-overlap — dense categorical axes get rotated labels (45 degrees), reduced tick size and a taller/wider canvas; a figure charts at most the top 30 terms (report the truncation in your text); point labels always use ggrepel; pie / donut label only slices >= 5% (the legend covers the rest); heatmap row names are hidden past 60 rows and fonts shrink automatically.
* Ordering and statistics — horizontal bars and dotplots put the most significant (or largest) term on TOP; bars keep the value axis tight to the baseline (a little padding only when significance stars are drawn); no coord_flip for bubble plots; significance stars *** p<0.001, ** p<0.01, * p<0.05; report the statistic on the figure (p-value, AUC, HR with 95% CI, correlation coefficient).
* The user's explicit requirement ALWAYS wins: pass style / palette / theme to plot_chart, or express the change in a modify_chart request."""


def describe_defaults() -> str:
    """Return the LLM-facing brief of the default figure style."""
    return _LLM_BRIEF.format(reference=STYLE_REFERENCE)


def describe_overrides() -> str:
    """Return a one-line summary of the user-facing override knobs."""
    styles = ", ".join(f"'{k}'" for k in STYLE_PRESETS)
    palettes = ", ".join(f"'{k}'" for k in DISCRETE_PALETTES)
    themes = ", ".join(f"'{k}'" for k in THEME_BASE)
    return (f"Override knobs — style ({styles}); palette ({palettes}); "
            f"theme ({themes}).")
