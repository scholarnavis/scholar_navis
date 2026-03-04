import httpx
from onnx import helper, TensorProto

from src.core.core_task import BackgroundTask


class FetchModelsTask(BackgroundTask):
    """拉取远端模型列表任务 (纯 OpenAI 标准接口)"""

    def _execute(self):
        base_url = self.kwargs.get("base_url", "")
        api_key = self.kwargs.get("api_key", "")

        self.update_progress(20, "Connecting to API endpoint...")
        proxy_url = self.config.user_settings.get("proxy_url", "").strip()

        url = f"{base_url.rstrip('/')}/models"
        httpx_kwargs = {"timeout": 10.0}

        if proxy_url:
            httpx_kwargs["proxy"] = proxy_url
        else:
            httpx_kwargs["trust_env"] = False

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

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
    """Tests compute device availability (includes real hardware spin-up and anti-fallback detection)"""

    def _execute(self):
        device_id = self.kwargs.get("device_id", "cpu")
        self.update_progress(10, f"Testing {device_id}...")

        try:
            import onnxruntime as ort
            import numpy as np

            providers = ort.get_available_providers()
            provider_to_use = "CPUExecutionProvider"
            provider_options = {}

            if "cuda" in device_id.lower():
                provider_to_use = "CUDAExecutionProvider"
                if ":" in device_id:
                    provider_options["device_id"] = int(device_id.split(":")[1])
            elif "dml" in device_id.lower():
                provider_to_use = "DmlExecutionProvider"
                if ":" in device_id:
                    provider_options["device_id"] = int(device_id.split(":")[1])
            elif "coreml" in device_id.lower():
                provider_to_use = "CoreMLExecutionProvider"

            if provider_to_use not in providers:
                return {
                    "success": False,
                    "msg": f"Missing Environment: '{provider_to_use}' is not installed or detected. Please check your onnxruntime installation.\nCurrently available: {', '.join(providers)}"
                }

            if provider_to_use == "CPUExecutionProvider":
                self.update_progress(100, "CPU test passed.")
                return {"success": True, "msg": "CPU is correctly configured and ready."}

            self.update_progress(40, "Creating in-memory test model...")

            try:
                X = helper.make_tensor_value_info('X', TensorProto.FLOAT, [1])
                Y = helper.make_tensor_value_info('Y', TensorProto.FLOAT, [1])
                node = helper.make_node('Identity', ['X'], ['Y'])
                graph = helper.make_graph([node], 'test_graph', [X], [Y])
                model = helper.make_model(graph)
                model_bytes = model.SerializeToString()
            except ImportError:
                return {
                    "success": False,
                    "msg": "Deep hardware testing requires the 'onnx' package. Please run 'pip install onnx'."
                }

            self.update_progress(60, "Initializing hardware session...")

            session_options = ort.SessionOptions()
            session_options.log_severity_level = 3

            provider_config = [(provider_to_use, provider_options)] if provider_options else [provider_to_use]
            provider_config.append("CPUExecutionProvider")

            session = ort.InferenceSession(model_bytes, sess_options=session_options, providers=provider_config)

            actual_providers = session.get_providers()
            if actual_providers[0] != provider_to_use:
                return {
                    "success": False,
                    "msg": f"Silent fallback intercepted!\nRequested '{provider_to_use}' (Device ID: {provider_options.get('device_id', 0)}), but ONNX Runtime silently fell back to '{actual_providers[0]}'.\nThis usually means the GPU does not support DML/CUDA, the index is invalid, or the graphics drivers are missing/faulty."
                }

            self.update_progress(80, "Running inference on hardware...")

            input_data = np.array([1.0], dtype=np.float32)
            session.run(['Y'], {'X': input_data})

            self.update_progress(100, "Hardware test passed.")
            return {
                "success": True,
                "msg": f"Hardware acceleration activated successfully!\nDevice '{device_id}' ({provider_to_use}) passed the real VRAM inference test."
            }

        except Exception as e:
            return {"success": False, "msg": f"Hardware initialization crashed:\n{str(e)}"}


class TestApiTask(BackgroundTask):
    """测试大模型 API 连通性 """

    def _execute(self):
        base_url = self.kwargs.get("base_url", "")
        api_key = self.kwargs.get("api_key", "")
        model_name = self.kwargs.get("model_name", "")
        custom_params = self.kwargs.get("custom_params", {})

        self.update_progress(30, f"Sending test prompt to {model_name}...")
        proxy_url = self.config.user_settings.get("proxy_url", "").strip()

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
