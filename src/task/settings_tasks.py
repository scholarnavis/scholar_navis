import asyncio

import httpx

from src.core.core_task import BackgroundTask


class FetchModelsTask(BackgroundTask):
    """拉取远端模型列表任务 (纯 OpenAI 标准接口)"""

    def _execute(self):
        base_url = self.kwargs.get("base_url", "")
        api_key = self.kwargs.get("api_key", "")

        self.update_progress(20, "Connecting to API endpoint...")
        from src.core.config_manager import ConfigManager
        proxy_url = ConfigManager().user_settings.get("proxy_url", "").strip()

        url = f"{base_url.rstrip('/')}/models"
        httpx_kwargs = {"timeout": 10.0}

        if proxy_url:
            httpx_kwargs["proxy"] = proxy_url
        else:
            httpx_kwargs["trust_env"] = False

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
            return {"success": True, "models": models, "msg": f"Successfully fetched {len(models)} models."}
        except Exception as e:
            return {"success": False, "models": [], "msg": str(e)}


class TestDeviceTask(BackgroundTask):
    def _execute(self):
        device_id = self.kwargs.get("device_id")
        self.send_log("INFO", f"Testing device: {device_id}")

        try:
            import onnxruntime as ort
            import numpy as np
            from onnx import helper, TensorProto


            provider = "CPUExecutionProvider"
            provider_options = None

            if device_id.startswith("cuda"):
                provider = "CUDAExecutionProvider"
                if ":" in device_id:
                    provider_options = {'device_id': int(device_id.split(":")[1])}
            elif device_id.startswith("dml"):
                provider = "DmlExecutionProvider"
                if ":" in device_id:
                    provider_options = {'device_id': int(device_id.split(":")[1])}
            elif device_id.startswith("rocm"):
                provider = "ROCmExecutionProvider"
                if ":" in device_id:
                    provider_options = {'device_id': int(device_id.split(":")[1])}
            elif device_id == "coreml":
                provider = "CoreMLExecutionProvider"


            self.send_log("INFO", "Generating native dummy ONNX model in memory...")
            X = helper.make_tensor_value_info('X', TensorProto.FLOAT, [1, 3])
            Y = helper.make_tensor_value_info('Y', TensorProto.FLOAT, [1, 3])
            node_def = helper.make_node('Identity', inputs=['X'], outputs=['Y'])
            graph_def = helper.make_graph([node_def], 'test-model', [X], [Y])
            model_def = helper.make_model(graph_def, producer_name='scholar-navis-test')


            model_bytes = model_def.SerializeToString()

            self.send_log("INFO", f"Initializing ORT Session with provider: {provider}...")

            if provider_options:
                providers_arg = [(provider, provider_options)]
            else:
                providers_arg = [provider]

            session = ort.InferenceSession(model_bytes, providers=providers_arg)

            active_providers = session.get_providers()
            if provider != "CPUExecutionProvider" and active_providers[0] == "CPUExecutionProvider":
                return {
                    "success": False,
                    "msg": f"Hardware acceleration failed!\n\nRequested '{provider}', but ONNX Runtime silently fell back to 'CPUExecutionProvider'.\n\nPlease check your GPU drivers or ONNX environment."
                }

            self.send_log("INFO", "Running dummy inference tensor...")
            test_input = np.random.randn(1, 3).astype(np.float32)
            session.run(None, {"X": test_input})

            return {
                "success": True,
                "msg": f"Success! \n\nThe device '{device_id}' ({provider}) is fully functional and hardware acceleration is active."
            }

        except Exception as e:
            import traceback
            error_msg = str(e)
            if "onnxruntime" not in error_msg.lower() and "onnx" not in error_msg.lower():
                error_msg = f"Initialization failed: {error_msg}\n{traceback.format_exc()}"
            return {"success": False, "msg": error_msg}


class TestApiTask(BackgroundTask):
    """测试大模型 API 连通性 """

    def _execute(self):
        base_url = self.kwargs.get("base_url", "")
        api_key = self.kwargs.get("api_key", "")
        model_name = self.kwargs.get("model_name", "")
        custom_params = self.kwargs.get("custom_params", {})

        self.update_progress(30, f"Sending test prompt to {model_name}...")
        from src.core.config_manager import ConfigManager
        proxy_url = ConfigManager().user_settings.get("proxy_url", "").strip()

        url = f"{base_url.rstrip('/')}/chat/completions"
        httpx_kwargs = {"timeout": 15.0}

        if proxy_url:
            httpx_kwargs["proxy"] = proxy_url
        else:
            httpx_kwargs["trust_env"] = False

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
            return {"success": True, "msg": f"Connection excellent!\nModel replied: '{raw_content.strip()}'"}
        except Exception as e:
            return {"success": False, "msg": str(e)}


class HardwareAuthTask(BackgroundTask):
    """Background task for securely authorizing hardware identity."""

    def _execute(self):
        try:
            from src.core.encryption_service import SystemEncryptionService
            service = SystemEncryptionService()

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            res = loop.run_until_complete(service.verify_identity("Authorize configuration export"))
            loop.close()

            return {"success": res}
        except Exception as e:
            self.send_log("WARNING", f"Hardware auth error: {e}")
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


class HWDetectTask(BackgroundTask):
    """Background task for safely probing system hardware and ORT providers."""

    def _execute(self):
        try:
            from src.core.device_manager import DeviceManager
            dev_mgr = DeviceManager()
            info = dev_mgr.get_sys_info()
            info['gpu_info'] = dev_mgr.get_gpu_info()
            devs = dev_mgr.get_available_devices()

            return {"success": True, "info": info, "devs": devs}
        except Exception as e:
            self.send_log("ERROR", f"Hardware detection failed: {e}")
            return {
                "success": False,
                "info": {"os": "Unknown OS", "error": str(e)},
                "devs": [{"name": "Auto Detect", "id": "auto"}],
                "msg": str(e)
            }