import asyncio
import logging
import threading
import sys
import os
import time
from contextlib import AsyncExitStack

import anyio
import httpx
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.types import JSONRPCMessage

from typing import Dict, List, Optional

from src.core.config_manager import ConfigManager
from src.core.signals import GlobalSignals

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
        self.tool_schemas: Dict[str, dict] = {}
        self.server_status: Dict[str, str] = {}
        self.server_tasks: Dict[str, object] = {}
        self.server_stops: Dict[str, asyncio.Event] = {}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._start_loop, daemon=True)
        self._thread.start()

    def _start_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def bootstrap_servers(self, force_all=True):  # <-- 增加 force_all 参数，以防强制重启一切
        config_mgr = ConfigManager()
        servers = config_mgr.mcp_servers.get("mcpServers", {})
        user_cfg = config_mgr.user_settings

        safe_base_env = os.environ.copy()
        safe_base_env["PYTHONIOENCODING"] = "utf-8"

        if user_cfg.get("proxy_mode") == "custom" and user_cfg.get("proxy_url"):
            proxy = user_cfg.get("proxy_url")
            safe_base_env["HTTP_PROXY"] = proxy
            safe_base_env["HTTPS_PROXY"] = proxy
            safe_base_env["http_proxy"] = proxy
            safe_base_env["https_proxy"] = proxy

        builtin_env = safe_base_env.copy()
        if user_cfg.get("ncbi_email"): builtin_env["NCBI_API_EMAIL"] = user_cfg.get("ncbi_email")
        if user_cfg.get("ncbi_api_key"): builtin_env["NCBI_API_KEY"] = user_cfg.get("ncbi_api_key")
        if user_cfg.get("s2_api_key"): builtin_env["S2_API_KEY"] = user_cfg.get("s2_api_key")

        delay_ms = 500

        for server_name, srv_cfg in servers.items():
            is_enabled = srv_cfg.get("enabled", False)
            always_on = srv_cfg.get("always_on", False)

            if is_enabled or always_on:
                if not force_all:

                    status = self.server_status.get(server_name, "")
                    if status == "connected":
                        continue

                self.server_status[server_name] = "starting"
                run_cfg = dict(srv_cfg)

                if server_name == "builtin":
                    logger.info(f"Bootstrapping Core MCP Server: [{server_name}] immediately.")
                    is_frozen = getattr(sys, 'frozen', False) or not sys.executable.endswith('python.exe')
                    run_cfg['command'] = sys.executable
                    if is_frozen:
                        run_cfg['args'] =["--run-builtin-mcp"]
                    else:
                        run_cfg['args'] =["-c", "from plugins.academic_mcp_server import mcp; mcp.run(transport='stdio')"]

                    run_cfg['env'] = builtin_env
                    self._async_start(server_name, run_cfg)

                else:
                    logger.info(f"Scheduled Lazy Load for External MCP Server:[{server_name}] in {delay_ms}ms")

                    if run_cfg.get('type') == 'stdio' and run_cfg.get('command') == 'python':
                        ext_py = config_mgr.user_settings.get("external_python_path", "")
                        run_cfg['command'] = ext_py if ext_py and ext_py != "python" else sys.executable

                    if run_cfg.get('type') == 'stdio':
                        custom_env = safe_base_env.copy()
                        user_defined_env = run_cfg.get('env', {})
                        if isinstance(user_defined_env, dict):
                            custom_env.update({k: str(v) for k, v in user_defined_env.items()})
                        run_cfg['env'] = custom_env

                    def start_lazy_server(name=server_name, cfg=run_cfg):
                        self._async_start(name, cfg)

                    QTimer.singleShot(delay_ms, start_lazy_server)
                    delay_ms += 2000

    async def _run_session(self, server_name: str, connection_config: dict):
        self.server_status[server_name] = "connecting"
        if server_name not in self.server_stops:
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
                    read, write = transport
                elif connection_config['type'] == 'sse':
                    headers = connection_config.get('headers')
                    transport = await stack.enter_async_context(sse_client(connection_config['url'], headers=headers))
                    read, write = transport


                elif connection_config['type'] == 'streamable_http':

                    url = connection_config.get('url')
                    headers = connection_config.get('headers', {})
                    read_tx, read_rx = anyio.create_memory_object_stream(100)
                    write_tx, write_rx = anyio.create_memory_object_stream(100)

                    async def http_poster():
                        import json
                        # 使用持久化客户端避免频繁握手
                        async with httpx.AsyncClient() as client:
                            async with write_rx:
                                async for message in write_rx:
                                    try:
                                        if hasattr(message, "model_dump"):
                                            payload = message.model_dump(mode='json')
                                        elif hasattr(message, "dict"):
                                            payload = message.dict()
                                        else:
                                            payload = json.loads(json.dumps(message, default=lambda o: o.__dict__))

                                        post_headers = {k: v for k, v in headers.items() if k.lower() != 'accept'}
                                        post_headers['Content-Type'] = 'application/json'

                                        await client.post(url, json=payload, headers=post_headers)
                                    except Exception as e:
                                        logger.error(f"HTTP POST failed: {e}")

                    async def http_receiver():
                        async with read_tx:
                            try:

                                limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
                                async with httpx.AsyncClient(timeout=None, limits=limits) as client:
                                    async with client.stream("GET", url, headers=headers) as resp:
                                        if resp.status_code != 200:
                                            logger.error(f"Abnormal HTTP status code: {resp.status_code}")
                                            return

                                        async for line in resp.aiter_lines():
                                            line = line.strip()
                                            if not line: continue

                                            if line.startswith("data: "):
                                                line = line[6:].strip()

                                            if '"jsonrpc"' not in line:
                                                continue

                                            try:
                                                msg = JSONRPCMessage.model_validate_json(line)
                                                await read_tx.send(msg)
                                            except Exception as val_e:
                                                logger.warning(
                                                    f"Failed to parse message: {line[:100]} | Error: {val_e}")

                            except Exception as e:
                                logger.error(f"HTTP POST failed: {e}")

                    tg = await stack.enter_async_context(anyio.create_task_group())


                    tg.start_soon(http_poster, *[])
                    tg.start_soon(http_receiver, *[])

                    read, write = read_rx, write_tx
                else:
                    raise ValueError(f"Unsupported type: {connection_config['type']}")

                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()

                self.sessions[server_name] = session
                self.server_status[server_name] = "connected"

                # 注册工具列表
                tools_response = await session.list_tools()
                for tool in tools_response.tools:
                    self.tool_map[tool.name] = server_name
                    self.tool_schemas[tool.name] = {
                        "type": "function",
                        "server": server_name,
                        "function": {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.inputSchema
                        }
                    }

                logger.info(f"[{server_name}] Connected via HTTP Stream. Loaded {len(tools_response.tools)} tools.")
                GlobalSignals().mcp_status_changed.emit()
                await self.server_stops[server_name].wait()

        except Exception as e:

            def _unwrap_exception(exc):
                if hasattr(exc, 'exceptions'):
                    return " | ".join(_unwrap_exception(sub_e) for sub_e in exc.exceptions)
                return str(exc)

            err_msg = _unwrap_exception(e)
            if not err_msg.strip():
                err_msg = repr(e)

            logger.error(f"[{server_name}] Connection error: {err_msg}")
            self.server_status[server_name] = f"error: {err_msg}"
            self.sessions.pop(server_name, None)

            tools_to_remove = [k for k, v in self.tool_map.items() if v == server_name]
            for tool in tools_to_remove:
                del self.tool_map[tool]
                self.tool_schemas.pop(tool, None)

            GlobalSignals().mcp_status_changed.emit()




    def get_available_tags(self) -> list:
        """供 UI 获取所有可选标签"""
        tags = set()
        for schema in self.tool_schemas.values():
            tags.update(self._get_tool_effective_tags(schema))
        return sorted(list(tags))

    def _get_tool_effective_tags(self, schema: dict) -> list:
        server_name = schema.get("server", "")
        desc = schema.get("function", {}).get("description", "")

        if server_name == "builtin":
            import re
            match = re.search(r"\[Tags:\s*(.*?)\]", str(desc), re.IGNORECASE)
            if match:
                return [t.strip() for t in match.group(1).split(",")]
            return ["General Tools"]
        else:
            return [server_name] if server_name else ["Unknown Server"]

    def get_tools_schema_by_tags(self, selected_tags: list) -> list:
        if selected_tags is None:
            return self.get_all_tools_schema()

        if not selected_tags:
            return []

        filtered_tools = []
        for schema in self.tool_schemas.values():
            tool_tags = self._get_tool_effective_tags(schema)
            if any(tag in selected_tags for tag in tool_tags):
                filtered_tools.append({
                    "type": schema.get("type", "function"),
                    "function": schema.get("function", {})
                })

        return filtered_tools


    def _async_start(self, server_name: str, config: dict):
        """非阻塞启动：把任务扔给 asyncio 循环后立即返回，状态靠 UI 的 QTimer 自动刷新"""
        if server_name in self.sessions or server_name in self.server_status:
            self.disconnect_server(server_name)

        self.server_status[server_name] = "connecting"
        future = asyncio.run_coroutine_threadsafe(self._run_session(server_name, config), self._loop)
        self.server_tasks[server_name] = future


    def _sync_start(self, server_name: str, config: dict) -> bool:
        # Disconnect existing instance if restarting
        if server_name in self.sessions or server_name in self.server_status:
            self.disconnect_server(server_name)

        future = asyncio.run_coroutine_threadsafe(self._run_session(server_name, config), self._loop)
        self.server_tasks[server_name] = future

        for _ in range(20):  # Wait up to 10 seconds
            status = self.server_status.get(server_name, "")
            if status == "connected": return True
            if "error" in status: return False
            time.sleep(0.5)
        return False

    def call_tool_sync(self, tool_name: str, arguments: dict) -> str:
        server_name = self.tool_map.get(tool_name)
        if not server_name:
            raise ValueError(f"Tool '{tool_name}' not found in any connected MCP server.")

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
            logger.error(f"Tool {tool_name} failed on {server_name}: {e}")
            return f"{{\"status\": \"error\", \"message\": \"{str(e)}\"}}"

    def is_tool_available(self, tool_name: str) -> bool:
        return tool_name in self.tool_map

    def get_server_status(self, server_name: str) -> str:
        return self.server_status.get(server_name, "disconnected")

    def get_all_tools_schema(self) -> list:
        return [
            {
                "type": schema.get("type", "function"),
                "function": schema.get("function", {})
            }
            for schema in self.tool_schemas.values()
        ]


    def disconnect_server(self, server_name: str):
        if server_name in self.server_stops:
            self._loop.call_soon_threadsafe(self.server_stops[server_name].set)
            time.sleep(0.1)

        if server_name in self.sessions:
            del self.sessions[server_name]
        if server_name in self.server_tasks:
            del self.server_tasks[server_name]
        if server_name in self.server_stops:
            del self.server_stops[server_name]

        # Clean up the tool map
        tools_to_remove = [k for k, v in self.tool_map.items() if v == server_name]
        for tool in tools_to_remove:
            del self.tool_map[tool]
            self.tool_schemas.pop(tool, None)

        self.server_status[server_name] = "disconnected"
        logger.info(f"[{server_name}] Disconnected and tools unmapped.")
        GlobalSignals().mcp_status_changed.emit()