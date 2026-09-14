import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"c:\data\Python\scholar_navis")

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

from src.core.theme_manager import ThemeManager  # noqa: E402
from src.ui.components.chat_bubble import ChatBubbleWidget  # noqa: E402
from src.ui.components.text_formatter import TextFormatter  # noqa: E402

ANSWER = """<think>
先分析一下：这是一个很长的推理过程。""" + "推理内容很长。" * 200 + """
</think>
[FINAL_ANSWER]

## 分析结果

这是正文段落，包含 `inline code` 与 [1] 引用。

| 基因 | 表达量 | 描述 |
| --- | --- | --- |
| Ghir_D03G12349 | 12.5 | """ + "很长的描述文本" * 30 + """ |
| Ghir_A05G00001 | 3.2 | short |

```python
def hello():
    print("a very long line: " + "x" * 300)
"""

# 再加一段超长代码块触发高度上限
ANSWER += "\n".join(f"line_{i} = {i}" for i in range(60)) + """
```

> 引用块内容：这是 markdown 引用，不属于文末 reference。
> 第二行引用。
> 第三行引用，再加一段很长的文本让它超过高度上限。""" + "引用内容。" * 60 + """

正文结束。
"""


def layout(bubble):
    bubble.show()
    bubble.resize(900, 800)
    bubble.layout().activate()
    app.processEvents()
    bubble._sync_heights()
    app.processEvents()


def report(tag, bubble):
    print(f"--- {tag} ---")
    print("kinds:", [b._block_kind for b in bubble._extra_blocks])
    print("lbl_text h:", bubble.lbl_text.height(), "visible:", bubble.lbl_text.isVisible())
    for b in bubble._extra_blocks:
        print(f"  {b._block_kind:6s} outer_h={b.height():4d} content_h={b.browser.height():4d} "
              f"content_w={b.browser.width():4d} vp_w={b.viewport().width():4d} "
              f"hbar={int(b.horizontalScrollBar().isVisible())} vbar={int(b.verticalScrollBar().isVisible())}")


bubble = ChatBubbleWidget("", is_user=False, index=0)

# 折叠态
bubble.set_content(TextFormatter.format_response(ANSWER, 0, set(), set(), {}))
layout(bubble)
report("collapsed", bubble)

# 展开态：思考链应被 340px 上限截断，并出现内部纵向滚动条
bubble.set_content(TextFormatter.format_response(ANSWER, 0, {0}, {0}, {}))
layout(bubble)
report("expanded", bubble)
think = bubble._extra_blocks[0]
assert think._block_kind == "think"
assert abs(think.height() - 340) <= 12, think.height()
assert think.verticalScrollBar().isVisible(), "think panel should scroll internally"

# 代码块与引用块应被各自上限截断并出现纵向滚动条
code = next(b for b in bubble._extra_blocks if b._block_kind == "code")
quote = next(b for b in bubble._extra_blocks if b._block_kind == "quote")
assert abs(code.height() - 420) <= 12, code.height()
assert code.verticalScrollBar().isVisible()
assert code.horizontalScrollBar().isVisible(), "long code line should scroll horizontally"
assert abs(quote.height() - 300) <= 12, quote.height()
assert quote.verticalScrollBar().isVisible()

for theme in ("light", "dark"):
    ThemeManager().set_theme(theme)
    bubble.set_content(TextFormatter.format_response(ANSWER, 0, {0}, {0}, {}))
    layout(bubble)
    print("theme", theme, "->", [b._block_kind for b in bubble._extra_blocks])

print("OK")
