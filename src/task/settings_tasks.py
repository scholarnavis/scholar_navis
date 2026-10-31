import httpx

from src.core.core_task import BackgroundTask


class FetchModelsTask(BackgroundTask):
    """拉取远端模型列表任务 (纯 OpenAI 标准接口)"""

    def _execute(self):
        base_url = self.kwargs.get("base_url", "")
        api_key = self.kwargs.get("api_key", "")

        self.update_progress(20, "Connecting to API endpoint...")
        from src.core.network_worker import httpx_proxy_kwargs

        url = f"{base_url.rstrip('/')}/models"
        self.send_log("INFO", f"Fetching models from {url}")

        # 代理模式（system / off / custom）只在 network_worker 判定一次。
        httpx_kwargs = {"timeout": 10.0, **httpx_proxy_kwargs()}

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

        if self.is_cancelled():
            raise InterruptedError("API connection safely terminated by user.")


        try:
            with httpx.Client(**httpx_kwargs) as client:
                res = client.get(url, headers=headers)
                res.raise_for_status()
                data = res.json()

            models = sorted([m['id'] for m in data.get('data', [])])
            if not models:
                raise ValueError("API returned an empty model list.")

            self.update_progress(100, "Done")
            self.send_log("INFO", f"API response successful. Found {len(models)} models.")
            return {"success": True, "models": models, "msg": f"Successfully fetched {len(models)} models."}
        except Exception as e:
            return {"success": False, "models": [], "msg": str(e)}


class TestApiTask(BackgroundTask):
    """测试大模型 API 连通性 """

    def _execute(self):
        base_url = self.kwargs.get("base_url", "")
        api_key = self.kwargs.get("api_key", "")
        model_name = self.kwargs.get("model_name", "")
        custom_params = self.kwargs.get("custom_params", {})

        self.send_log("INFO", f"Testing connectivity for model: {model_name}")
        self.update_progress(30, f"Sending test prompt to {model_name}...")
        from src.core.network_worker import httpx_proxy_kwargs

        url = f"{base_url.rstrip('/')}/chat/completions"
        httpx_kwargs = {"timeout": 15.0, **httpx_proxy_kwargs()}

        headers = {
            "Authorization": f"Bearer {api_key or 'sk-test'}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": "Hello. Please reply with exactly one word: 'OK'."}],
            "max_tokens": 5
        }

        for k, v in custom_params.items():
            if k not in ["model", "messages", "stream"]:
                payload[k] = v

        if self.is_cancelled():
            raise InterruptedError("API test safely terminated by user.")

        try:
            with httpx.Client(**httpx_kwargs) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()

            choices = data.get("choices", [])
            if not choices:
                raise ValueError("No choices returned from API.")

            msg_obj = choices[0].get("message", {})
            raw_content = msg_obj.get("content") or "[Empty Response]"

            self.update_progress(100, "Done")
            self.send_log("INFO", f"API test passed for {model_name}")
            return {"success": True, "msg": f"Connection excellent!\nModel replied: '{raw_content.strip()}'"}
        except Exception as e:
            return {"success": False, "msg": str(e)}


class EmailVerifyTask(BackgroundTask):
    """Background task for robustly verifying user email addresses without blocking the UI."""

    def _execute(self):
        email = self.kwargs.get("email")
        if not email:
            return {"success": True, "msg": ""}

        try:
            from src.core.email_check import verify_email_robust
            res = verify_email_robust(email)

            if not res.get("is_valid"):
                detailed_error = res.get("error_msg", "Unknown email validation error.")
                prompt_msg = (
                    f"Email Validation Failed:\n"
                    f"{detailed_error}\n\n"
                    f"Please provide a valid email address or leave it completely empty (which will disable NCBI tools)."
                )
                return {"success": False, "msg": prompt_msg}
            else:
                return {"success": True, "msg": ""}
        except Exception as e:
            return {"success": False, "msg": f"Email validation encountered a system error: {e}"}


class TestMcpConnectionTask(BackgroundTask):
    """
    后台测试 MCP 服务器连接状态的统一任务
    """

    def _execute(self):
        server_name = self.kwargs.get("server_name")
        config = self.kwargs.get("config")

        self.update_progress(20, f"Initializing connection to '{server_name}'...")

        from src.core.mcp_manager import MCPManager
        mcp_mgr = MCPManager.get_instance()

        self.update_progress(50, "Handshaking and verifying MCP protocol...")

        success = mcp_mgr._sync_start(server_name, config)

        if success:
            self.update_progress(90, "Connection successful. Reading tools schema...")
            mcp_mgr.disconnect_server(server_name)
            self.update_progress(100, "Test passed.")
            return {"success": True, "msg": f"Successfully connected to '{server_name}' and verified protocol."}
        else:
            status = mcp_mgr.get_server_status(server_name)
            mcp_mgr.disconnect_server(server_name)
            return {"success": False, "msg": f"Handshake failed: {status}"}
class ToolEnvDetectTask(BackgroundTask):
    """检测外置工具：R 运行时、WSL 状态与清单里每个生信工具的落点。

    为什么必须离开界面线程
    --------------------
    Windows 上没有本机构建的工具都要经 ``wsl.exe`` 探测一次（``wsl -l`` + 发行版内
    ``command -v`` + 版本探测），单次几百毫秒，发行版冷启动时可达十余秒；工具一多，
    在界面线程里跑就是"设置页打开即卡死十几秒"。

    返回的是一份可直接渲染的扁平结构（不含任何 Qt 对象），由
    :mod:`src.tools.settings_sections.toolenv_section` 负责显示。
    """

    def _execute(self):
        from src.core.bioenv import catalog, help as help_mod, resolver, wsl
        from src.core.r_engine import get_r_engine
        from src.core.toolchain import micromamba as mamba

        refresh = bool(self.kwargs.get("refresh"))
        self.update_progress(10, "Loading the bioinformatics catalog...")
        if refresh:
            resolver.clear_cache()
            wsl.invalidate()

        cat = catalog.default(refresh=refresh)
        platform = mamba.runtime_platform()

        self.update_progress(25, "Checking WSL...")
        status = wsl.detect(refresh=refresh)
        if self.is_cancelled():
            raise InterruptedError("Tool detection cancelled by user.")

        self.update_progress(45, f"Resolving tools on {platform}...")
        locations = resolver.resolve_all(cat, platform, refresh=refresh)
        if self.is_cancelled():
            raise InterruptedError("Tool detection cancelled by user.")

        self.update_progress(85, "Checking the R runtime...")
        r_info = get_r_engine().detect()

        reachable = cat.for_reachable(platform)
        rows = []
        for entry in reachable:
            location = locations.get(entry.name)
            rows.append({
                "name": entry.name,
                "available": location is not None,
                "source": location.source if location else "",
                "distro": location.distro if location else "",
                "version": location.version if location else "",
                "path": location.path if location else "",
                "reason": ("" if location
                           else resolver.unavailable_reason(entry, platform)),
                "help_lines": help_mod.total_lines(entry.name),
            })

        self.update_progress(100, "Detection finished.")
        self.send_log("INFO", f"External tools: {len(locations)}/{len(reachable)} "
                              f"resolved on {platform}; {wsl.describe(status)}")
        return {
            "platform": platform,
            "wsl": wsl.describe(status),
            "wsl_usable": status.usable,
            "wsl_applicable": status.state != wsl.STATE_NOT_APPLICABLE,
            "r": {
                "available": bool(r_info.get("available")),
                "version": str(r_info.get("version") or ""),
                "executable": str(r_info.get("executable") or ""),
            },
            "tools": rows,
            "total": len(reachable),
            "resolved": len(locations),
            "problems": list(cat.problems[:10]),
        }


class HWDetectTask(BackgroundTask):
    """后台采集硬件信息（OS / CPU / 内存 / GPU）。

    纯 stdlib + psutil + 系统命令（``nvidia-smi`` / ``lspci`` / WMI），不触碰
    ``onnxruntime``：本地推理下线后，"ONNX Engine / Providers" 那部分探测没有任何
    消费者，留在启动与设置页链路上只是让 import 与探针白白发生。
    """

    def _execute(self):
        from src.core import hardware_info

        try:
            info = hardware_info.collect()
            self.send_log("INFO", f"Hardware: {info.get('cpu', 'unknown CPU')} / "
                                  f"{info.get('ram_total', 'unknown RAM')} / "
                                  f"{len(info.get('gpu_info') or [])} GPU(s)")
            return {"success": True, "info": info}
        except Exception as e:
            self.send_log("ERROR", f"Hardware detection failed: {e}")
            return {"success": False, "info": {}, "msg": str(e)}
