"""书签的**数据模型与几何布局**（与界面无关的唯一实现）。

存什么
------
一条书签挂在**某条消息**上（``history[i]["bookmarks"]``），因此它天然随会话快照
与归档导出一起落盘、一起读回，不需要第二套持久化通道。字段：

* ``id``         —— 12 位十六进制，消息内唯一；
* ``title``      —— 必填、简短（上限 :data:`TITLE_MAX`）；
* ``description``—— 可选（上限 :data:`DESC_MAX`）；
* ``color``      —— ``#RRGGBB``，用于下划线、滚动条标注与卡片色块；
* ``text`` / ``prefix`` / ``suffix`` / ``occurrence`` / ``block`` / ``block_kind``
  —— **锚点**：正文是富文本，主题切换 / 字号调整 / 流式增长都会重排文档，
  字符偏移量随之失效。这里只存"选中的原文 + 前后各一小段上下文"，重新渲染后
  由 :func:`find_anchor` 按上下文匹配度重新定位。

为什么锚点在核心层
------------------
"如何把一段文字重新找回来"与"下方划线还是加背景"是两件事：前者是数据问题，
后者是表现问题。把它放在核心层后，气泡、滚动条、书签集都从同一份定义取锚点，
不会出现"气泡里能定位、书签集里跳不过去"这类两套说法。

滚动条标注的排布
----------------
:func:`layout_marks` 是纯函数（输入比例、轨道高度，输出每个标注的像素位置与
分组），UI 只负责画。相邻标注间距不足时先**在组内均匀摊开**；摊开后仍与相邻组
重叠就**并组合并**——合并后的组在滚动条上上下排列，悬停时一次性预览全部条目。
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Core.Bookmarks")

#: 标题长度上限（"简短"的量化口径；输入框与归一化共用同一个值）。
TITLE_MAX = 40
#: 介绍长度上限。介绍只是提示，不承担正文职责。
DESC_MAX = 240
#: 锚点原文长度上限：整段消息也能被选中，但没必要为定位保存几万字。
ANCHOR_TEXT_MAX = 4000
#: 锚点上下文长度（选中文字之前 / 之后各取这么多字符用于匹配）。
CONTEXT_LEN = 24

#: 可选调色板。取值在深浅两套主题下都具备足够的辨识度。
PALETTE: Tuple[str, ...] = (
    "#e5484d",   # red
    "#f76b15",   # orange
    "#f5b301",   # amber
    "#30a46c",   # green
    "#0ea5e9",   # sky
    "#6e56cf",   # violet
    "#d6409f",   # magenta
    "#8d8d8d",   # gray
)
DEFAULT_COLOR = PALETTE[0]

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


# --------------------------------------------------------------------------- #
#  颜色
# --------------------------------------------------------------------------- #
def normalize_color(value: Any, fallback: str = DEFAULT_COLOR) -> str:
    """把任意颜色值归一为 ``#RRGGBB``；非法值回退 ``fallback``。"""
    text = str(value or "").strip()
    if not _HEX_RE.match(text):
        return fallback
    text = text.lower()
    if len(text) == 4:                       # #abc -> #aabbcc
        text = "#" + "".join(ch * 2 for ch in text[1:])
    return text


def _relative_luminance(color: str) -> float:
    """WCAG 相对亮度（sRGB 线性化后按 0.2126/0.7152/0.0722 加权）。"""
    raw = normalize_color(color, "#000000").lstrip("#")
    channels = []
    for i in (0, 2, 4):
        c = int(raw[i:i + 2], 16) / 255.0
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast_text_color(background: str) -> str:
    """在给定底色上选一个看得清的文字色（深色或白色）。

    用户可能挑一个很浅的颜色（例如淡黄）；若文字仍跟随主题的浅色，就会
    出现"浅底浅字"。这里按相对亮度二选一，阈值取 0.45（白字在亮度 0.45 以上
    的底色上对比度不足）。
    """
    return "#141414" if _relative_luminance(background) > 0.45 else "#ffffff"


# --------------------------------------------------------------------------- #
#  归一化
# --------------------------------------------------------------------------- #
def normalize_plain(text: Any) -> str:
    """把 QTextDocument 的纯文本归一为稳定的比较基准。

    Qt 用 ``\\u2029`` 表示段落分隔、``\\u00a0`` 表示不换行空格。两者都是
    **一对一**替换，归一后字符下标仍与原文档位置对应；这样"存锚点"与
    "重新定位"用的是同一套坐标。
    """
    return str(text or "").replace("\u2029", "\n").replace("\u00a0", " ")


def _clean_title(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text[:TITLE_MAX]


def _clean_desc(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text[:DESC_MAX]


def normalize_bookmark(raw: Any) -> Optional[Dict[str, Any]]:
    """校验并归一一条书签记录；不可用时返回 ``None``。

    数据可能来自旧版本、手工编辑或第三方归档，因此每一个字段都要自己兜底：
    缺标题的书签不成立；缺 ``id`` 的补一个（同一定义只在这里补）。
    """
    if not isinstance(raw, dict):
        return None
    title = _clean_title(raw.get("title"))
    if not title:
        return None
    text = normalize_plain(raw.get("text"))[:ANCHOR_TEXT_MAX]
    if not text:
        return None

    bookmark_id = str(raw.get("id") or "").strip().lower()
    if not re.match(r"^[0-9a-f]{8,32}$", bookmark_id):
        bookmark_id = uuid.uuid4().hex[:12]

    block = raw.get("block", 0)
    try:
        block = max(0, int(block))
    except (TypeError, ValueError):
        block = 0
    occurrence = raw.get("occurrence", 0)
    try:
        occurrence = max(0, int(occurrence))
    except (TypeError, ValueError):
        occurrence = 0

    return {
        "id": bookmark_id,
        "title": title,
        "description": _clean_desc(raw.get("description")),
        "color": normalize_color(raw.get("color")),
        "text": text,
        "prefix": normalize_plain(raw.get("prefix"))[-CONTEXT_LEN:],
        "suffix": normalize_plain(raw.get("suffix"))[:CONTEXT_LEN],
        "occurrence": occurrence,
        "block": block,
        "block_kind": str(raw.get("block_kind") or "text"),
        "created": raw.get("created") or time.time(),
    }


def normalize_bookmarks(value: Any) -> List[Dict[str, Any]]:
    """归一一个消息里的全部书签；非列表 / 空值一律视为"没有书签"。"""
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for raw in value:
        bookmark = normalize_bookmark(raw)
        if bookmark is None:
            continue
        if bookmark["id"] in seen:       # 手工编辑可能产生重复 id
            bookmark["id"] = uuid.uuid4().hex[:12]
        seen.add(bookmark["id"])
        result.append(bookmark)
    return result


def make_bookmark(title: Any, description: Any, color: Any, text: Any,
                  prefix: Any = "", suffix: Any = "", occurrence: Any = 0,
                  block: Any = 0, block_kind: Any = "text") -> Dict[str, Any]:
    """构造一条新书签（标题为空时返回空字典，由调用方决定如何提示）。"""
    bookmark = normalize_bookmark({
        "id": uuid.uuid4().hex[:12],
        "title": title,
        "description": description,
        "color": normalize_color(color),
        "text": text,
        "prefix": prefix,
        "suffix": suffix,
        "occurrence": occurrence,
        "block": block,
        "block_kind": block_kind,
        "created": time.time(),
    })
    if bookmark is None:
        logger.warning("Rejected bookmark without a title or anchor text: title=%r", title)
        return {}
    return bookmark


# --------------------------------------------------------------------------- #
#  锚点匹配
# --------------------------------------------------------------------------- #
def _common_suffix(left: str, right: str) -> int:
    count = 0
    limit = min(len(left), len(right))
    while count < limit and left[-1 - count] == right[-1 - count]:
        count += 1
    return count


def _common_prefix(left: str, right: str) -> int:
    count = 0
    limit = min(len(left), len(right))
    while count < limit and left[count] == right[count]:
        count += 1
    return count


def find_anchor(text: str, bookmark: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """在归一化文本 ``text`` 中重新定位书签，返回 ``(start, end)``。

    多候选时按两侧上下文的最长公共前后缀打分（上下文完全一致 = 满分），
    再用创建时记录的"第几次出现"做平手判定。找不到时依次退化为
    "去掉首尾空白"与"前 80 字符"两种较短锚点——重渲染后正文本身也可能被
    上游改写（例如 think 块折叠）。
    """
    full = text or ""
    needle = str(bookmark.get("text") or "")
    if not full or not needle:
        return None

    attempts = [needle]
    trimmed = needle.strip()
    if trimmed and trimmed != needle:
        attempts.append(trimmed)
    if len(needle) > 80:
        attempts.append(needle[:80])

    for candidate in attempts:
        positions = []
        start = 0
        while True:
            index = full.find(candidate, start)
            if index < 0:
                break
            positions.append(index)
            start = index + 1
        if not positions:
            continue

        prefix = str(bookmark.get("prefix") or "")
        suffix = str(bookmark.get("suffix") or "")
        occurrence = bookmark.get("occurrence")
        best: Optional[Tuple[float, int, int]] = None
        for order, index in enumerate(positions):
            end = index + len(candidate)
            left = full[max(0, index - len(prefix)):index]
            right = full[end:end + len(suffix)]
            score = float(_common_suffix(left, prefix) + _common_prefix(right, suffix))
            if isinstance(occurrence, int) and order == occurrence:
                score += 0.5
            if best is None or score > best[0]:
                best = (score, index, end)
        if best is not None:
            return best[1], best[2]
    return None


# --------------------------------------------------------------------------- #
#  滚动条标注布局
# --------------------------------------------------------------------------- #
def layout_marks(positions: Sequence[float], track_height: float,
                 mark_height: float, gap: float = 3.0,
                 pad: float = 2.0) -> List[List[Tuple[int, float]]]:
    """把理想位置排成互不重叠的标注，返回按组划分的 ``[(下标, y), …]``。

    :param positions: 每个标注的理想 y 坐标（与 ``track_height`` 同一坐标系）。
    :param track_height: 可用轨道高度。
    :param mark_height: 单个标注的绘制高度。
    :param gap: 同组上下标注之间的最小空隙。
    :param pad: 轨道上下留白。

    步骤：

    1. 相邻理想位置间距小于 ``mark_height + gap`` 的标注归为一组；
    2. 组内在组中心附近**均匀摊开**（"尽可能分开"）；
    3. 摊开后若与相邻组仍然重叠，就把两组并成一组重新摊开，直到稳定。

    最后剩下的超长组无法再分开，调用方按"上下排列 + 悬停预览全部"呈现。
    """
    if not positions or track_height <= 0:
        return []

    mark_h = max(1.0, float(mark_height))
    slot = mark_h + max(0.0, float(gap))
    pad = max(0.0, float(pad))
    max_y = max(pad, float(track_height) - mark_h - pad)

    order = sorted(range(len(positions)), key=lambda i: float(positions[i]))
    ideals = [min(max(float(positions[i]), pad), max_y) for i in order]

    groups: List[List[int]] = []
    for local, ideal in enumerate(ideals):
        if groups and ideal - ideals[groups[-1][-1]] < slot:
            groups[-1].append(local)
        else:
            groups.append([local])

    #: 一组标注可用的纵向跨度（首个标注顶边到末个标注顶边）
    span = max(0.0, max_y - pad)

    def _distribute(group: List[int]) -> None:
        """把一组理想位置摊开到相邻槽位，整体居中于原位置重心。

        组内所需跨度超过轨道时（书签极多）按可用跨度压缩间距——此时标注会
        相互贴近/轻微重叠，这正是"分不开就合并显示"的极端情形；调用方仍可按
        组悬停一次性预览全部条目。
        """
        count = len(group)
        if count == 1:
            ideals[group[0]] = min(max(ideals[group[0]], pad), max_y)
            return
        spacing = slot
        needed = (count - 1) * slot
        if needed > span:
            spacing = span / (count - 1)
        center = sum(ideals[i] for i in group) / count
        first = center - (count - 1) * spacing / 2.0
        first = min(max(first, pad), pad + max(0.0, span - (count - 1) * spacing))
        for offset, index in enumerate(group):
            ideals[index] = first + offset * spacing

    for group in groups:
        _distribute(group)

    # 摊开可能把两组推到一起：并组后重新摊开，直到没有任何邻接重叠。
    merged = True
    while merged and len(groups) > 1:
        merged = False
        for gi in range(len(groups) - 1):
            left_end = ideals[groups[gi][-1]] + mark_h
            right_start = ideals[groups[gi + 1][0]]
            if right_start < left_end + gap - 0.25:
                groups[gi] = groups[gi] + groups.pop(gi + 1)
                _distribute(groups[gi])
                merged = True
                break

    return [[(order[index], ideals[index]) for index in group] for group in groups]


__all__ = [
    "PALETTE", "DEFAULT_COLOR", "TITLE_MAX", "DESC_MAX", "CONTEXT_LEN",
    "normalize_color", "contrast_text_color", "normalize_plain",
    "normalize_bookmark", "normalize_bookmarks", "make_bookmark",
    "find_anchor", "layout_marks",
]
