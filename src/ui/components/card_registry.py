"""卡片组件的**标记协议**与**冻结 / 快照 / 恢复约定**的唯一事实来源。

三类知识集中在这里，其余地方只调用，不重复定义：

1. **标记协议**：``<tag data="base64(JSON)"></tag>``。产出方（agent runtime、任务层、
   外部技能或任何第三方）只写标记，UI 层按本表把标记渲染成组件。每个类型在此登记
   一条 :class:`CardSpec`（类型名、标签、去重键、依赖文件、清理规则）；
2. **组件约定**（每张卡自行实现，本模块按鸭子类型调用）：

   ``CARD_KIND``        稳定类型名，写进历史条目的 ``ui_components[].kind``
   ``card_key()``       同一气泡内的稳定去重键，也是快照匹配键
   ``card_state()``     用户交互结果（可 JSON 序列化），未交互返回 ``{}``
   ``restore_state(s)`` 应用交互结果
   ``freeze()``         冻结为只读：禁用输入与按钮，文字仍可框选复制
   ``sig_state_changed``交互落定后发出，供上层把状态回写历史条目并落盘

3. **归档可迁移性**：``artifacts_of(data)`` 声明组件依赖的本机文件（如 R 图的
   PNG/SVG/PDF），归档导出时随附件内嵌、导入时改写标记里的路径，使组件在别的
   机器上仍然可用。

历史条目字段
------------
用户交互结果存在消息的 ``ui_components`` 字段里（:data:`FIELD_NAME`），形如::

    [{"kind": "ask_user", "key": "……问题……", "state": {"selected": [...], "custom": "…"}}]

标记（在 ``content`` 里）记录的是**生成时的事实**，``ui_components`` 记录的是
**用户交互的结果**，两者互补：内容不动，交互结果增量覆盖。

外部（第三方）组件的接入方式
----------------------------
在自己的模块里构造一条带 ``factory`` 的 :class:`CardSpec` 并调用 :func:`register_card`，
再在应用启动时导入该模块即可。此后它自动获得与内置卡片完全相同的链路：
标记渲染 → 用户交互 → 冻结 → 历史快照 → 归档内嵌 → 导入/启动恢复（只读）。
``factory(bubble, data)`` 只负责"建卡 + 接线信号 + 返回组件"；去重、插入、快照、
恢复、冻结都由本模块统一处理。

冻结与框选
----------
Qt 里的"不可编辑但可框选"必须逐个控件处理：只读文本框用 ``setReadOnly(True)``
（``setEnabled(False)`` 会让文本连选都不能选），按钮与选择控件可以 ``setEnabled(False)``。
各卡片的 :meth:`freeze` 按这个原则实现。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: 历史条目里承载"组件用户交互结果"的字段名。
FIELD_NAME = "ui_components"


def _as_pattern(pattern: Any) -> re.Pattern:
    return pattern if isinstance(pattern, re.Pattern) else re.compile(pattern)


@dataclass(frozen=True)
class CardSpec:
    """一条卡片类型登记项。"""

    kind: str
    #: 标记标签名：``<tag data="…"></tag>``
    tag: str
    #: 完整标记正则，group(1) 为 base64 payload
    marker: re.Pattern
    #: 同一气泡内的稳定去重键 / 快照匹配键
    key_of: Callable[[Dict[str, Any]], str]
    #: 抹掉流式中残留的不完整标记（可多条）
    cleanup: Tuple[re.Pattern, ...] = ()
    #: 该组件依赖的本机文件（归档内嵌与路径改写用）；不依赖文件时为 None
    artifacts_of: Optional[Callable[[Dict[str, Any]], Sequence[str]]] = None
    #: 外部组件自带工厂；内置组件的建卡逻辑留在 chat_bubble（沿用既有抽取器），
    #: 故为 None，但去重 / 插入 / 快照 / 恢复仍统一走本模块。
    factory: Optional[Callable[[Any, Dict[str, Any]], Any]] = None


_SPECS: Dict[str, CardSpec] = {}


def register_card(
    kind: str,
    *,
    tag: str,
    key_of: Callable[[Dict[str, Any]], str],
    cleanup: Sequence[Any] = (),
    artifacts_of: Optional[Callable[[Dict[str, Any]], Sequence[str]]] = None,
    factory: Optional[Callable[[Any, Dict[str, Any]], Any]] = None,
    marker: Any = None,
) -> CardSpec:
    """登记（或覆盖）一种卡片类型；``marker`` 省略时按标签自动构造。"""
    spec = CardSpec(
        kind=kind,
        tag=tag,
        marker=_as_pattern(marker if marker is not None
                           else rf'<{tag} data="([^"]*)"\s*>\s*</{tag}>'),
        key_of=key_of,
        cleanup=tuple(_as_pattern(p) for p in cleanup),
        artifacts_of=artifacts_of,
        factory=factory,
    )
    if kind in _SPECS:
        logger.warning("Card kind '%s' re-registered; overwriting previous spec", kind)
    _SPECS[kind] = spec
    return spec


def spec(kind: str) -> Optional[CardSpec]:
    """取某类型的登记项；未登记返回 ``None``。"""
    return _SPECS.get(kind)


def specs() -> List[CardSpec]:
    """全部登记项（含内置与外部）。"""
    return list(_SPECS.values())


def key_of(kind: str, data: Dict[str, Any]) -> str:
    """按类型计算去重键（各卡片 ``card_key()`` 也复用这里，保证口径一致）。"""
    s = _SPECS.get(kind)
    if s is None or not isinstance(data, dict):
        return ""
    try:
        return str(s.key_of(data) or "")
    except Exception as e:
        logger.warning("key_of('%s') failed: %s", kind, e)
        return ""


# --------------------------------------------------------------------------- #
#  标记编码 / 解码（产出方与 UI 层共用）
# --------------------------------------------------------------------------- #
def encode_marker(kind: str, data: Dict[str, Any]) -> str:
    """把载荷编码成标记字符串（产出方的唯一入口，避免各写各的 base64）。"""
    s = _SPECS.get(kind)
    tag = s.tag if s is not None else kind
    payload = base64.b64encode(
        json.dumps(data, ensure_ascii=False).encode("utf-8")).decode("ascii")
    return f'<{tag} data="{payload}"></{tag}>'


def decode_payload(b64_text: str) -> Optional[Dict[str, Any]]:
    """解码标记载荷；失败返回 ``None``（调用方决定丢弃还是保留原文）。"""
    try:
        data = json.loads(base64.b64decode(b64_text).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        logger.warning("Malformed card marker payload dropped: %s", e)
        return None
    return data if isinstance(data, dict) else None


def iter_artifact_payloads(text: str) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """遍历正文里**声明了依赖文件**的卡片标记，产出 ``(kind, data)``。

    只服务于归档的两件事：收集要内嵌的成品文件、改写其中的路径。没有
    ``artifacts_of`` 的类型（纯 UI 卡片）与文件无关，不必进这里。
    """
    if not isinstance(text, str) or "<" not in text:
        return
    for s in _SPECS.values():
        if s.artifacts_of is None:
            continue
        for match in s.marker.finditer(text):
            data = decode_payload(match.group(1))
            if data is not None:
                yield s.kind, data


def artifact_paths_of(messages: Any) -> List[str]:
    """收集所有消息里组件依赖的本机文件路径（去重，保持出现顺序）。"""
    seen: Dict[str, None] = {}
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        for kind, data in iter_artifact_payloads(content or ""):
            s = _SPECS.get(kind)
            if s is None or s.artifacts_of is None:
                continue
            for path in s.artifacts_of(data) or ():
                if path:
                    seen.setdefault(str(path), None)
    return list(seen.keys())


def rewrite_card_paths(text: str, mapping: Dict[str, str]) -> Tuple[str, int]:
    """按 ``mapping`` 改写正文里组件标记中的文件路径，返回 ``(新文本, 改写数)``。

    归档导入把附件解包到新目录后，R 图卡这类组件的预览/下载路径必须一起搬到新位置，
    否则组件虽在、文件已失。只处理声明了 ``artifacts_of`` 的类型。
    """
    if not isinstance(text, str) or "<" not in text or not mapping:
        return text, 0
    changed = 0

    for s in _SPECS.values():
        if s.artifacts_of is None or not s.marker.search(text):
            continue

        def _replace(match, s=s):
            nonlocal changed
            data = decode_payload(match.group(1))
            if data is None:
                return match.group(0)
            hit = False
            for key in _artifact_keys(s, data):
                value = data.get(key)
                new_value = mapping.get(str(value)) if value else None
                if new_value:
                    data[key] = new_value
                    hit = True
            if not hit:
                return match.group(0)
            changed += 1
            return encode_marker(s.kind, data)

        text = s.marker.sub(_replace, text)
    return text, changed


def _artifact_keys(s: CardSpec, data: Dict[str, Any]) -> Tuple[str, ...]:
    """组件声明的文件路径字段名（``artifacts_of`` 返回值即路径，这里回推字段名）。"""
    paths = set(str(p) for p in (s.artifacts_of(data) or ()) if p)
    return tuple(k for k, v in data.items() if isinstance(v, str) and v in paths)


# --------------------------------------------------------------------------- #
#  渲染（标记 → 组件）
# --------------------------------------------------------------------------- #
def _rendered_keys(host: Any) -> set:
    """气泡上的统一去重集：``{(kind, key)}``，取代各卡自己维护的一份。"""
    keys = getattr(host, "_card_rendered_keys", None)
    if keys is None:
        keys = set()
        try:
            host._card_rendered_keys = keys
        except Exception:      # 宿主不允许挂属性时退化为每次新集（仅影响去重）
            pass
    return keys


def begin_card(host: Any, kind: str, data: Dict[str, Any]) -> Optional[str]:
    """建卡前置检查：算出键并登记去重，返回键；重复或键为空时返回 ``None``。

    内置与外部的建卡路径都先过这里，保证同一类型只有一套去重口径。
    """
    key = key_of(kind, data)
    if not key:
        logger.warning("Card '%s' skipped: empty dedupe key", kind)
        return None
    keys = _rendered_keys(host)
    if (kind, key) in keys:
        return None
    keys.add((kind, key))
    return key


def insert_card(host: Any, widget: Any):
    """把组件插到气泡尾部按钮行之前（宿主提供 ``_insert_card`` 时复用其逻辑）。"""
    insert = getattr(host, "_insert_card", None)
    if callable(insert):
        insert(widget)
        return widget
    layout = getattr(host, "content_layout", None)
    if layout is not None:
        layout.addWidget(widget)
    return widget


def build_card(host: Any, kind: str, data: Dict[str, Any]):
    """外部组件的统一建卡入口：去重 → 工厂 → 插入。"""
    s = _SPECS.get(kind)
    if s is None or s.factory is None:
        logger.warning("No factory registered for card kind '%s'", kind)
        return None
    if begin_card(host, kind, data) is None:
        return None
    try:
        widget = s.factory(host, data)
    except Exception as e:
        logger.error("Card factory for '%s' failed: %s", kind, e)
        return None
    if widget is None:
        return None
    insert_card(host, widget)
    return widget


def extract_registered_cards(host: Any, text: str) -> str:
    """把正文里**已注册工厂**的卡片标记渲染成组件（外部组件走这条路）。

    内置组件的建卡逻辑在 ``chat_bubble``（沿用既有抽取器），故其 ``factory`` 为
    ``None``，不会在这里被处理；其余机制（去重 / 插入 / 快照 / 恢复）对两者一致。
    """
    if not isinstance(text, str) or "<" not in text:
        return text
    for s in list(_SPECS.values()):
        if s.factory is None or f"<{s.tag}" not in text:
            continue

        def _replace(match, s=s):
            data = decode_payload(match.group(1))
            if data is not None:
                build_card(host, s.kind, data)
            return ""

        text = s.marker.sub(_replace, text)
        for pattern in s.cleanup:
            text = pattern.sub("", text)
    return text


# --------------------------------------------------------------------------- #
#  快照 / 恢复 / 冻结
# --------------------------------------------------------------------------- #
def iter_cards(host: Any) -> Iterator[Any]:
    """按布局顺序遍历宿主（气泡）里的全部组件。"""
    layout = getattr(host, "content_layout", None)
    if layout is None:
        return
    for i in range(layout.count()):
        item = layout.itemAt(i)
        widget = item.widget() if item is not None else None
        if widget is not None and getattr(widget, "CARD_KIND", ""):
            yield widget


def card_key(card: Any) -> str:
    """取组件自身的键；组件未实现 ``card_key`` 时按登记表从 ``data`` 回推。"""
    fn = getattr(card, "card_key", None)
    if callable(fn):
        try:
            return str(fn() or "")
        except Exception as e:
            logger.warning("card_key() failed: %s", e)
    data = getattr(card, "data", None)
    kind = getattr(card, "CARD_KIND", "")
    if kind and isinstance(data, dict):
        return key_of(kind, data)
    return ""


def freeze_card(card: Any) -> None:
    """冻结单个组件；组件未实现 ``freeze`` 时视为本来就只读。"""
    fn = getattr(card, "freeze", None)
    if callable(fn):
        try:
            fn()
        except Exception as e:
            logger.warning("freeze() failed for card '%s': %s",
                           getattr(card, "CARD_KIND", "?"), e)


def snapshot_cards(host: Any) -> List[Dict[str, Any]]:
    """收集宿主内所有组件的用户交互结果（写进历史条目的 :data:`FIELD_NAME`）。"""
    snapshots: List[Dict[str, Any]] = []
    for card in iter_cards(host):
        kind = getattr(card, "CARD_KIND", "")
        get_state = getattr(card, "card_state", None)
        try:
            state = get_state() if callable(get_state) else {}
        except Exception as e:
            logger.warning("card_state() failed for '%s': %s", kind, e)
            state = {}
        if not isinstance(state, dict):
            state = {}
        snapshots.append({"kind": kind, "key": card_key(card), "state": state})
    return snapshots


def restore_cards(host: Any, snapshots: Any) -> int:
    """把历史快照回灌到宿主内的组件，并**全部冻结**。

    历史重放（导入 / 启动恢复 / 编辑重发）出来的组件一律属于"已经交互过"的过去，
    因此无论有没有对应快照都冻结：快照只决定它显示什么，冻结决定它不能改。
    """
    cards = list(iter_cards(host))
    if not cards:
        return 0

    index: Dict[Tuple[str, str], Any] = {}
    for card in cards:
        index.setdefault((getattr(card, "CARD_KIND", ""), card_key(card)), card)

    applied = 0
    for snap in snapshots or []:
        if not isinstance(snap, dict):
            continue
        card = index.get((str(snap.get("kind", "")), str(snap.get("key", ""))))
        if card is None:
            logger.warning("No card matches snapshot kind=%s key=%s",
                           snap.get("kind"), str(snap.get("key"))[:60])
            continue
        apply_state = getattr(card, "restore_state", None)
        if not callable(apply_state):
            continue
        try:
            apply_state(snap.get("state") or {})
            applied += 1
        except Exception as e:
            logger.warning("restore_state() failed for '%s': %s", snap.get("kind"), e)

    for card in cards:
        freeze_card(card)
    return applied


# --------------------------------------------------------------------------- #
#  内置类型登记
#  标记协议（标签 / 正则 / 去重键 / 清理规则 / 依赖文件）以本表为准；建卡逻辑仍在
#  chat_bubble 的抽取器里（那里能拿到气泡的按钮位、翻译配置与各卡信号），两侧通过
#  本表的键与正则保持一致。
# --------------------------------------------------------------------------- #
register_card(
    "r_plot",
    tag="rplot_card",
    marker=r'<rplot_card data="([^"]*)"></rplot_card>',
    cleanup=(r"<rplot_card[^>]*>",),
    key_of=lambda d: str(d.get("png_path") or d.get("svg_path")
                         or d.get("chart_title") or ""),
    # R 图的成品文件都在本机临时目录：归档必须一起打包，否则组件过去了、图没了
    artifacts_of=lambda d: (d.get("png_path", ""), d.get("svg_path", ""),
                            d.get("pdf_path", ""), d.get("data_path", ""),
                            d.get("script_path", "")),
)

register_card(
    "plot_plan",
    tag="plot_plan",
    marker=r'<plot_plan data="([^"]*)"></plot_plan>',
    cleanup=(r"<plot_plan[^>]*>",),
    key_of=lambda d: str(d.get("plan_text") or d.get("request") or "plan"),
)

register_card(
    "ask_user",
    tag="ask_user",
    marker=r'<ask_user data="([^"]*)"\s*></ask_user>',
    cleanup=(r"<ask_user[^>]*/?>", r'&lt;/?ask_user[^&]*&gt;'),
    key_of=lambda d: str(d.get("question") or "question"),
)

register_card(
    "deep_plan",
    tag="deep_plan",
    marker=r'<deep_plan data="([^"]*)"\s*></deep_plan>',
    cleanup=(r"<deep_plan[^>]*/?>",),
    key_of=lambda d: str(d.get("query") or "plan"),
)

register_card(
    "error_panel",
    tag="error_panel",
    marker=r'<error_panel data="([^"]*)"\s*>\s*</error_panel>',
    cleanup=(r"<error_panel[^>]*/?>",),
    key_of=lambda d: f"{d.get('title', '')}|{str(d.get('details', ''))[:128]}",
)
