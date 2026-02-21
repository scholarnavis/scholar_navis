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
        self._exit_stack = None
        self.available_tools = []

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._start_loop, daemon=True)
        self._thread.start()

    def _start_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def call_tool_async_wrapper(self, tool_name: str, arguments: dict, callback):
        """
        Non-blocking tool execution for UI applications.
        """
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

        import threading
        threading.Thread(target=_run_and_callback, daemon=True).start()

    def connect_sync(self, script_path: str = None, python_path: str = None, args: list = None):
        """
        Synchronous connection entry point for the main program (UI thread).
        Supports both traditional script paths and explicit arguments (e.g., for Nuitka internal execution).
        """
        if python_path is None:
            python_path = sys.executable

        future = asyncio.run_coroutine_threadsafe(
            self._async_connect(script_path, python_path, args), self._loop
        )
        return future.result()

    async def _async_connect(self, script_path: str, python_path: str, explicit_args: list):
        self._exit_stack = AsyncExitStack()

        # Determine execution arguments.
        # Use explicit_args if provided (for Nuitka "-c" execution), otherwise fallback to script_path.
        run_args = explicit_args if explicit_args is not None else [script_path]

        server_params = StdioServerParameters(
            command=python_path,
            args=run_args,
        )

        logger.info(f"Initializing MCP server connection. Command: {python_path} {' '.join(run_args)}")

        # Establish connection and maintain context
        stdio_transport = await self._exit_stack.enter_async_context(stdio_client(server_params))
        self.read, self.write = stdio_transport
        self.session = await self._exit_stack.enter_async_context(ClientSession(self.read, self.write))

        await self.session.initialize()

        # Retrieve and cache the list of available tools
        tools_response = await self.session.list_tools()
        self.available_tools = tools_response.tools

        logger.info(f"Connection successful. Loaded {len(self.available_tools)} research tools.")

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
        """同步调用工具，并记录日志到 UI"""
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