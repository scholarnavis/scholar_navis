# dialog_tasks.py
"""
对话框任务管理模块：基于core_task框架管理对话框中的后台任务
"""
import logging
import traceback
from typing import Dict, Any, Optional

from src.core.core_task import BackgroundTask, TaskState


class McpTestTask(BackgroundTask):
    """MCP连接测试任务：在后台测试MCP服务器连接"""

    def __init__(self, task_id: str, task_queue, kwargs: Optional[Dict] = None):
        super().__init__(task_id, task_queue, kwargs)

    def _execute(self) -> Any:
        """执行MCP连接测试"""
        server_name = self.kwargs.get("server_name", "")
        config = self.kwargs.get("config", {})
        test_name = f"test_{server_name}"

        self.send_log("INFO", f"Starting MCP connection test for: {server_name}")

        try:
            from src.core.mcp_manager import MCPManager
            mgr = MCPManager.get_instance()

            self.update_progress(30, "Connecting to server...")

            if self.is_cancelled():
                return {"success": False, "msg": "Cancelled by user"}

            # 尝试同步连接
            success = mgr._sync_start(test_name, config)
            status = mgr.get_server_status(test_name)

            self.update_progress(70, "Retrieving tool count...")

            if self.is_cancelled():
                mgr.disconnect_server(test_name)
                return {"success": False, "msg": "Cancelled by user"}

            # 获取加载的工具数量
            if success:
                tool_count = sum(1 for v in mgr.tool_map.values() if v == test_name)
                msg = f"Connection successful! A total of {tool_count} available tools have been loaded."
            else:
                msg = status

            self.update_progress(90, "Cleaning up test connection...")

            # 测试完毕后立即断开清理
            try:
                mgr.disconnect_server(test_name)
            except Exception as e:
                self.send_log("WARNING", f"Failed to disconnect test server: {e}")

            self.send_log("INFO", f"MCP test completed: success={success}")
            return {"success": success, "msg": msg}

        except Exception as e:
            error_msg = str(e)
            self.send_log("ERROR", f"MCP test failed: {error_msg}\n{traceback.format_exc()}")
            return {"success": False, "msg": error_msg}

    def cancel(self):
        """取消测试任务"""
        super().cancel()
        # 尝试清理可能存在的测试连接
        try:
            server_name = self.kwargs.get("server_name", "")
            test_name = f"test_{server_name}"
            from src.core.mcp_manager import MCPManager
            mgr = MCPManager.get_instance()
            mgr.disconnect_server(test_name)
        except Exception:
            pass
        self.send_log("INFO", "MCP test task cancelled")


class DialogTaskManager:
    """
    对话框任务管理器：封装TaskManager，专门管理对话框中的后台任务
    """

    def __init__(self):
        from src.core.core_task import TaskManager
        self._task_manager = TaskManager()
        self._task_manager.register_hooks(
            pre=self._on_task_pre,
            post=self._on_task_post,
            on_terminate=self._on_task_terminate
        )
        self.logger = logging.getLogger("DialogTaskManager")

    def start_mcp_test(self, server_name: str, config: Dict):
        """
        启动MCP测试任务

        Args:
            server_name: 服务器名称
            config: 服务器配置
        """
        from src.core.core_task import TaskMode
        task_id = f"mcp_test_{server_name}"

        self._task_manager.start_task(
            task_class=McpTestTask,
            task_id=task_id,
            mode=TaskMode.THREAD,
            server_name=server_name,
            config=config
        )

    def cancel_task(self):
        """取消当前任务"""
        self._task_manager.cancel_task()

    def is_running(self) -> bool:
        """检查是否有任务在运行"""
        return self._task_manager.worker is not None

    def connect_signals(self, finished_callback=None, error_callback=None, progress_callback=None):
        """
        连接信号到回调函数

        Args:
            finished_callback: 完成回调，签名: (result: dict) -> None
            error_callback: 错误回调，签名: (error_msg: str) -> None
            progress_callback: 进度回调，签名: (progress: int, msg: str) -> None
        """
        if finished_callback:
            self._task_manager.sig_result.connect(finished_callback)

        if error_callback:
            def on_state_changed(state, msg):
                if state == TaskState.FAILED.value:
                    error_callback(msg)

            self._task_manager.sig_state_changed.connect(on_state_changed)

        if progress_callback:
            self._task_manager.sig_progress.connect(progress_callback)

    def _on_task_pre(self):
        """任务开始前的准备工作"""
        self.logger.info("Dialog task starting...")

    def _on_task_post(self):
        """任务成功完成后的处理"""
        self.logger.info("Dialog task completed successfully")

    def _on_task_terminate(self):
        """任务终止后的清理工作"""
        self.logger.info("Dialog task terminated")

    def wait_for_completion(self, timeout_sec: float = None):
        """等待任务完成"""
        self._task_manager.wait(timeout_sec)