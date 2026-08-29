"""临时核对脚本：确认原 settings_tool.py 的全部方法在新结构中无遗漏。用后即删。"""
import ast
import subprocess
import sys

sys.path.insert(0, ".")

old_src = subprocess.run(
    ["git", "show", "HEAD:src/tools/settings_tool.py"],
    capture_output=True, text=True, encoding="utf-8"
).stdout
old_tree = ast.parse(old_src)

old_methods = set()
old_top_classes = set()
for node in old_tree.body:
    if isinstance(node, ast.ClassDef):
        old_top_classes.add(node.name)
        for m in node.body:
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                old_methods.add(m.name)

import src.tools.settings_tool as m

ST = m.SettingsTool
new_methods = {n for n in dir(ST) if not n.startswith("__") and callable(getattr(ST, n))}

missing = old_methods - new_methods
print("OLD method count:", len(old_methods))
print("MISSING methods:", sorted(missing) if missing else "NONE")

top_ok = {c: hasattr(m, c) for c in old_top_classes}
print("Top-level classes preserved:", top_ok)

# 额外核对各行数
import os
total = 0
base = "src/tools"
for f in ["settings_tool.py"] + [f"settings_sections/{x}" for x in os.listdir("src/tools/settings_sections") if x.endswith(".py")]:
    n = sum(1 for _ in open(os.path.join(base, f), encoding="utf-8"))
    total += n
    print(f"{n:6d}  {f}")
print("NEW total:", total, "(old: 2693, 差异为文件头注释/导入)")
