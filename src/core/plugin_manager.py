import os
import sys
import importlib.util
import inspect
import logging
from src.tools.base_tool import BaseTool


class ToolExtensionManager:
    """动态加载外部扩展工具的管理器"""

    def __init__(self, plugins_dir="tools_ext"):
        self.plugins_dir = os.path.join(os.getcwd(), plugins_dir)
        self.logger = logging.getLogger("ToolExtensionManager")
        self.loaded_tools = []

        # 确保目录存在
        os.makedirs(self.plugins_dir, exist_ok=True)
        # 将插件目录加入系统路径，防止插件内的相对导入报错
        if self.plugins_dir not in sys.path:
            sys.path.insert(0, self.plugins_dir)

    def load_extensions(self):
        """扫描并实例化所有继承自 BaseTool 的外挂工具"""
        self.loaded_tools.clear()

        for filename in os.listdir(self.plugins_dir):
            if filename.endswith(".py") and not filename.startswith("__"):
                module_name = filename[:-3]
                file_path = os.path.join(self.plugins_dir, filename)

                try:
                    # 动态导入模块
                    spec = importlib.util.spec_from_file_location(module_name, file_path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    # 遍历模块内的所有成员
                    for name, obj in inspect.getmembers(module):
                        # 找到继承自 BaseTool 且不是 BaseTool 本身的类
                        if inspect.isclass(obj) and issubclass(obj, BaseTool) and obj is not BaseTool:
                            try:
                                tool_instance = obj()  # 实例化
                                self.loaded_tools.append(tool_instance)
                                self.logger.info(f"✅ Successfully loaded external tool: {tool_instance.tool_name}")
                            except Exception as e:
                                self.logger.error(f"❌ Failed to instantiate {name} in {filename}: {e}")
                except Exception as e:
                    self.logger.error(f"❌ Failed to load module {filename}: {e}")

        return self.loaded_tools