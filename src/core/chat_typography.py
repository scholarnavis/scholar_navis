"""聊天气泡排版参数：默认值、取值域与配置键的唯一来源。

UI（设置面板、气泡渲染）与落盘（``user_settings``）都从这里取规格，避免
"面板允许的范围"与"渲染时夹取的范围"各写一份而互相漂移。

参数分两个作用域（scope）：

* ``ai``   —— LLM（助手）回答气泡；
* ``user`` —— 用户提问气泡。

每个作用域各有 5 个参数：字号 / 字符间距 / 行距 / 段前距 / 段后距。配置键
形如 ``chat_bubble_<参数名>_<作用域>``，由 :func:`config_key` 统一生成——
不要在别处硬拼键名。
"""
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TypographyParam:
    """单个可调排版参数的完整规格（面板控件、夹取与落盘共用）。"""

    name: str        # 参数名（配置键 = chat_bubble_<name>_<scope>）
    label: str       # 面板标签
    unit: str        # 展示用单位后缀
    default: float
    minimum: float
    maximum: float
    step: float
    decimals: int    # 0 = 整数显示（仍用 QDoubleSpinBox，避免两套控件分支）


#: 作用域顺序即面板列顺序
SCOPES: tuple = ("ai", "user")

#: 作用域显示名
SCOPE_LABELS = {"ai": "LLM Bubble", "user": "Your Bubble"}

#: 全部参数。默认值取历史硬编码值（正文 14px、行距 150%、段前 4px、段后 8px），
#: 因此"用户从不打开面板"与"打开后直接恢复默认"效果完全一致。
PARAMS: tuple = (
    TypographyParam("font_size", "Font size", "px", 14.0, 9.0, 28.0, 1.0, 0),
    TypographyParam("letter_spacing", "Letter spacing", "px", 0.0, -1.0, 4.0, 0.25, 2),
    TypographyParam("line_height", "Line spacing", "%", 150.0, 100.0, 260.0, 5.0, 0),
    TypographyParam("space_before", "Space before", "px", 4.0, 0.0, 40.0, 1.0, 0),
    TypographyParam("space_after", "Space after", "px", 8.0, 0.0, 40.0, 1.0, 0),
)

PARAM_BY_NAME = {p.name: p for p in PARAMS}

_KEY_PREFIX = "chat_bubble_"

#: 标题层级的**自动偏移**表：``(级别, 字号倍数, 段前倍数, 段后倍数)``。
#:
#: 面板只暴露 5 个**正文字**参，标题（H1–H6）的大小与间距**全部由此表推导**：
#: H1 永远比 H2 大、标题间距永远比正文段落宽，不会因为用户调参而失效，也无需
#: 为每一级标题再加控件。
#:
#: 倍数以默认正文（14px / 段前 4px / 段后 8px）标定，因此默认档位下得到
#: 字号 21/18/16/15/14/13px、段前距 12/10/8/7/6/5px、段后距统一 4px：
#: 字号阶梯与 H1 的段距和改造前的硬编码外观一致，H2–H6 的段前距则按层级收窄
#: （旧实现六级共用 12px，看不出层次）。倍数刻意**严格递减**，避免取整后
#: 相邻层级撞成同一个值。
HEADING_OFFSETS: tuple = (
    (1, 1.50, 3.00, 0.50),
    (2, 1.29, 2.50, 0.50),
    (3, 1.14, 2.00, 0.50),
    (4, 1.07, 1.75, 0.50),
    (5, 1.00, 1.50, 0.50),
    (6, 0.93, 1.25, 0.50),
)

_HEADING_BY_LEVEL = {level: (size, top, bottom)
                     for level, size, top, bottom in HEADING_OFFSETS}


def config_key(name: str, scope: str) -> str:
    """参数名 + 作用域 → ``user_settings`` 键名（唯一拼装处）。"""
    return f"{_KEY_PREFIX}{name}_{scope}"


def clamp_font_size(value) -> float:
    """把正文字号夹到合法区间；``None`` / 非法输入回落默认字号。"""
    param = PARAM_BY_NAME["font_size"]
    return clamp(param, param.default if value is None else value)


def heading_metrics(level: int, base_font_px=None,
                    space_before=None, space_after=None) -> tuple:
    """返回某级标题的 ``(字号 px, 段前 px, 段后 px)``。

    三个入参都缺省时按正文默认值推导（即"面板未打开"的历史外观）。标题层级
    越浅倍数越大，因此任何字号 / 段距设置下层级关系都成立。

    :param level: 标题级别 1–6；越界按 H6 处理（防御性）。
    :param base_font_px: 正文字号，缺省取排版参数的默认值。
    :param space_before: 正文段前距，缺省取排版参数的默认值。
    :param space_after: 正文段后距，缺省取排版参数的默认值。
    """
    size_ratio, top_factor, bottom_factor = _HEADING_BY_LEVEL.get(
        int(level), _HEADING_BY_LEVEL[6])

    base = clamp_font_size(base_font_px)

    before_param = PARAM_BY_NAME["space_before"]
    before = clamp(before_param,
                   before_param.default if space_before is None else space_before)

    after_param = PARAM_BY_NAME["space_after"]
    after = clamp(after_param,
                  after_param.default if space_after is None else space_after)

    return (max(1, int(round(base * size_ratio))),
            max(0, int(round(before * top_factor))),
            max(0, int(round(after * bottom_factor))))


def defaults() -> dict:
    """全量默认值 ``{配置键: 默认值}``（两个作用域 × 全部参数）。"""
    return {config_key(p.name, scope): p.default for p in PARAMS for scope in SCOPES}


def clamp(param: TypographyParam, value) -> float:
    """把任意输入夹到参数取值域内；非数值输入回落默认值。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return param.default
    if number != number:  # NaN
        return param.default
    return min(max(number, param.minimum), param.maximum)


def read(settings: dict) -> dict:
    """从 ``user_settings`` 读出全量参数（缺项回落默认值、越界夹取）。

    返回值恒为完整的 10 项字典，便于"修改标记"直接做整体比较。
    """
    settings = settings or {}
    values = {}
    for param in PARAMS:
        for scope in SCOPES:
            key = config_key(param.name, scope)
            values[key] = clamp(param, settings.get(key, param.default))
    return values


def scope_values(values: dict, scope: str) -> dict:
    """把 :func:`read` 的结果（或面板草稿）拆成 ``{参数名: 值}``。

    容忍不完整的入参：缺失项按默认值补齐，避免调用方为"草稿只带了部分键"
    额外写兜底分支。
    """
    values = values or {}
    return {p.name: clamp(p, values.get(config_key(p.name, scope), p.default))
            for p in PARAMS}


def serialize(param: TypographyParam, value: float):
    """按参数精度整理待落盘的数值（整数参数存 int，避免配置里出现 14.0）。"""
    number = clamp(param, value)
    return int(round(number)) if param.decimals == 0 else round(number, param.decimals)
