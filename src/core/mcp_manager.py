import asyncio
import logging
import threading
import sys
import os
import time
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from typing import Dict, List, Optional

logger = logging.getLogger("MCP.Manager")


class MCPManager:
    _instance = None
    TOOL_TIMEOUT = 30.0

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = MCPManager()
        return cls._instance

    def __init__(self):
        self.sessions: Dict[str, ClientSession] = {}
        self.tool_map: Dict[str, str] = {}
        self.server_status: Dict[str, str] = {}
        self.server_tasks: Dict[str, object] = {}
        self.server_stops: Dict[str, asyncio.Event] = {}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._start_loop, daemon=True)
        self._thread.start()

    def _start_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _run_session(self, server_name: str, connection_config: dict):
        self.server_status[server_name] = "connecting"

        self.server_stops[server_name] = asyncio.Event()

        try:
            async with AsyncExitStack() as stack:
                if connection_config['type'] == 'stdio':
                    server_params = StdioServerParameters(
                        command=connection_config['command'],
                        args=connection_config['args'],
                        env=connection_config.get('env')
                    )
                    transport = await stack.enter_async_context(stdio_client(server_params))
                elif connection_config['type'] == 'sse':
                    transport = await stack.enter_async_context(sse_client(connection_config['url']))
                else:
                    raise ValueError(f"Unsupported connection type: {connection_config['type']}")

                read, write = transport
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()

                self.sessions[server_name] = session
                self.server_status[server_name] = "connected"

                tools_response = await session.list_tools()
                for tool in tools_response.tools:
                    self.tool_map[tool.name] = server_name

                logger.info(f"[{server_name}] Connected. Loaded {len(tools_response.tools)} tools.")

                # 挂起协程，保持连接，直到收到断开信号
                await self.server_stops[server_name].wait()

        except Exception as e:
            logger.error(f"[{server_name}] Connection error: {e}")
            self.server_status[server_name] = f"error: {str(e)}"
            if server_name in self.sessions: del self.sessions[server_name]

    def connect_sync(self, server_name: str = "external", script_path: str = None, python_path: str = None,
                     args: list = None) -> bool:
        """通用同步连接接口，向下兼容 main.py 里的不同调用方式"""
        if python_path is None:
            python_path = sys.executable

        cmd_args = args if args is not None else []
        if script_path:
            cmd_args = [script_path] + cmd_args

        config = {'type': 'stdio', 'command': python_path, 'args': cmd_args, 'env': os.environ.copy()}
        return self._sync_start(server_name, config)

    def connect_external_mcp(self, script_path: str, python_path: str = None) -> bool:
        """连接本地外部脚本 MCP"""
        return self.connect_sync(server_name="external", script_path=script_path, python_path=python_path)

    def connect_network_mcp(self, server_name: str, url: str) -> bool:
        """连接网络 MCP 服务器"""
        config = {'type': 'sse', 'url': url}
        return self._sync_start(server_name, config)

    def _sync_start(self, server_name: str, config: dict) -> bool:
        future = asyncio.run_coroutine_threadsafe(self._run_session(server_name, config), self._loop)
        self.server_tasks[server_name] = future

        for _ in range(20):  # 等待最多 10 秒
            status = self.server_status.get(server_name, "")
            if status == "connected": return True
            if "error" in status: return False
            time.sleep(0.5)
        return False

    def call_tool_sync(self, tool_name: str, arguments: dict) -> str:
        """调用工具并附加安全沙箱机制"""
        server_name = self.tool_map.get(tool_name)
        if not server_name:
            raise ValueError(f"Tool '{tool_name}' not found.")
        session = self.sessions.get(server_name)

        is_trusted = server_name == "builtin"
        timeout = 120.0 if is_trusted else self.TOOL_TIMEOUT

        logger.info(f"Executing [{tool_name}] on [{server_name}] (Timeout: {timeout}s)")

        async def _call():
            return await asyncio.wait_for(
                session.call_tool(tool_name, arguments=arguments),
                timeout=timeout
            )

        try:
            future = asyncio.run_coroutine_threadsafe(_call(), self._loop)
            result = future.result()
            return result.content[0].text
        except asyncio.TimeoutError:
            err_msg = f"Security Constraint: Tool execution exceeded max allowed time ({timeout}s)."
            logger.error(err_msg)
            return f"{{\"status\": \"error\", \"message\": \"{err_msg}\"}}"
        except Exception as e:
            logger.error(f"Tool {tool_name} failed: {e}")
            return f"{{\"status\": \"error\", \"message\": \"{str(e)}\"}}"

    def is_tool_available(self, tool_name: str) -> bool:
        return tool_name in self.tool_map

    def get_server_status(self, server_name: str) -> str:
        return self.server_status.get(server_name, "disconnected")

    def get_all_tools_schema(self) -> list:
        all_tools = []
        for server_name, session in self.sessions.items():
            try:
                future = asyncio.run_coroutine_threadsafe(session.list_tools(), self._loop)
                tools = future.result()
                for tool in tools.tools:
                    all_tools.append({
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.inputSchema,
                            "server": server_name
                        }
                    })
            except Exception as e:
                logger.error(f"Failed to get tools from {server_name}: {e}")
        return all_tools

    def disconnect_server(self, server_name: str):
        """断开指定服务器"""
        if server_name in self.server_stops:
            self._loop.call_soon_threadsafe(self.server_stops[server_name].set)
            time.sleep(0.1)

        if server_name in self.sessions:
            del self.sessions[server_name]
        if server_name in self.server_tasks:
            del self.server_tasks[server_name]
        if server_name in self.server_stops:
            del self.server_stops[server_name]

        # 清理工具映射
        tools_to_remove = [k for k, v in self.tool_map.items() if v == server_name]
        for tool in tools_to_remove:
            del self.tool_map[tool]

        self.server_status[server_name] = "disconnected"
        logger.info(f"[{server_name}] Disconnected.")
