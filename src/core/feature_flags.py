"""功能开关（**集中管理，唯一来源**）。

约定
----
* 每个开关只在本文件出现一次，并注明启用/停用的条件与影响范围；
* 关闭开关只隐藏**界面入口**，底层管线保持不动——恢复功能时把值改回
  即可，不要在业务代码里到处打补丁；
* 开关命名用大写下划线，表达"这件事的界面是否可用"，而不是"功能是否存在"。
"""

#: 设置页「AI Agent 与外部工具」区块的**界面入口**。
#:
#: 关闭时整个区块（MCP 服务表格、添加/导入/刷新按钮）不再创建，
#: MCPManager / SkillManager 管线不受影响，已有订阅仍会被加载。
EXTERNAL_TOOLS_SETTINGS_SECTION_ENABLED = False

#: 聊天输入区的"External Tools"开关与"Tools Filter"过滤器。
#:
#: 关闭时开关不进布局且强制置 False，避免"入口隐藏但功能仍在后台
#: 生效"的不可见状态。
EXTERNAL_TOOLS_CHAT_TOGGLE_ENABLED = True
