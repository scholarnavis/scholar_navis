import httpx

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
