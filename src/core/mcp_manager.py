import asyncio
import logging
import threading
import sys
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger("MCP.Manager")


class MCPManager:
    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = MCPManager()
        return cls._instance

    def __init__(self):
        self.session = None
        self.available_tools = []

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._start_loop, daemon=True)
        self._thread.start()

        # 🆕 新增：用于生命周期管理的事件与任务句柄
        self._stop_event = None
        self._connect_ready = None
        self._session_task = None

        # 缓存的启动参数
        self._saved_script_path = None
        self._saved_python_path = None
        self._saved_args = None

    def _start_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # 核心生命周期管理
    async def _run_session(self, script_path: str, python_path: str, explicit_args: list):
        """真正的核心连接协程：它会一直挂起保持运行，维持 AsyncExitStack 的生命周期"""
        run_args = explicit_args if explicit_args is not None else [script_path]
        import os
        current_env = os.environ.copy()

        server_params = StdioServerParameters(
            command=python_path,
            args=run_args,
            env=current_env  # 显式注入环境
        )

        logger.info(f"Initializing MCP server connection. Command: {python_path} {' '.join(run_args)}")

        try:
            # 使用标准的 async with，确保进入和退出都在同一个 Task 中
            async with AsyncExitStack() as stack:
                stdio_transport = await stack.enter_async_context(stdio_client(server_params))
                self.read, self.write = stdio_transport
                self.session = await stack.enter_async_context(ClientSession(self.read, self.write))

                await self.session.initialize()

                tools_response = await self.session.list_tools()
                self.available_tools.extend(tools_response.tools)

                logger.info(f"Connection successful. Loaded {len(self.available_tools)} research tools.")

                # 通知外层 connect_sync：连接已建立
                self._connect_ready.set()

                # 挂起当前任务，保持上下文存活，直到接收到重启/关闭信号
                await self._stop_event.wait()
                logger.info("MCP Server shutdown signal received. Safely closing context...")

        except Exception as e:
            logger.error(f"MCP Session encountered an error: {e}")
            if not self._connect_ready.is_set():
                self._connect_ready.set()  # 防止由于报错导致 UI 线程永久死锁

    async def _start_session_task(self, script_path: str, python_path: str, explicit_args: list):
        """包装方法：创建并启动长驻任务"""
        self._stop_event = asyncio.Event()
        self._connect_ready = asyncio.Event()

        # 将 _run_session 作为一个后台任务启动
        self._session_task = asyncio.create_task(
            self._run_session(script_path, python_path, explicit_args)
        )

        # 阻塞等待 session 初始化完成
        await self._connect_ready.wait()

    # 对外暴露的同步接口 (UI 线程调用)
    def connect_sync(self, script_path: str = None, python_path: str = None, args: list = None):
        """同步连接入口，供主程序调用"""
        self._saved_script_path = script_path
        self._saved_python_path = python_path
        self._saved_args = args

        if python_path is None:
            python_path = sys.executable

        future = asyncio.run_coroutine_threadsafe(
            self._start_session_task(script_path, python_path, args), self._loop
        )
        return future.result()

    def restart_sync(self):
        """同步重启入口：释放旧上下文，应用新环境变量"""
        if not self._session_task: return

        future = asyncio.run_coroutine_threadsafe(self._async_restart(), self._loop)
        return future.result()

    async def _async_restart(self):
        """异步执行热重启的底层逻辑"""
        # 1. 干净地触发旧连接退出，并等待底层清理完毕
        if self._stop_event:
            self._stop_event.set()
            if self._session_task:
                await self._session_task

                # 2. 拉起新连接 (此时子进程会无缝读取到主进程刚刚修改的 os.environ 环境变量)
        await self._start_session_task(
            self._saved_script_path,
            self._saved_python_path,
            self._saved_args
        )

    # 工具调用接口 (保持原样)
    def get_openai_tools_schema(self) -> list:
        openai_tools = []
        for tool in self.available_tools:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.inputSchema
                }
            })
        return openai_tools

    def call_tool_sync(self, tool_name: str, arguments: dict) -> str:
        logger.info(f"Executing MCP Tool: [{tool_name}] with arguments: {arguments}")
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.session.call_tool(tool_name, arguments=arguments), self._loop
            )
            result = future.result()

            preview = result.content[0].text
            if len(preview) > 150:
                preview = preview[:150] + " ... (truncated)"

            logger.info(f"MCP Tool [{tool_name}] executed successfully. Result preview: {preview}")
            return result.content[0].text
        except Exception as e:
            logger.error(f"MCP Tool [{tool_name}] execution failed. Error: {str(e)}")
            raise e

    def call_tool_async_wrapper(self, tool_name: str, arguments: dict, callback):
        logger.info(f"Dispatching tool execution to background: {tool_name}")

        def _run_and_callback():
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self.session.call_tool(tool_name, arguments=arguments), self._loop
                )
                result = future.result()
                callback(result.content[0].text, None)
                logger.info(f"Tool execution completed successfully: {tool_name}")
            except Exception as e:
                logger.error(f"Tool execution failed: {tool_name}. Error: {str(e)}")
                callback(None, str(e))

        threading.Thread(target=_run_and_callback, daemon=True).start()