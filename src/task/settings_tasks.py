import httpx
import requests
from anthropic import Anthropic

from src.core.core_task import BackgroundTask
from src.core.device_manager import DeviceManager
from src.core.llm_impl import OpenAICompatibleLLM
from src.core.models_registry import resolve_auto_model, get_model_conf, check_model_exists
from src.core.network_worker import _get_explicit_proxy_kwargs


class VerifySettingsTask(BackgroundTask):
    """标准的系统校验 Task，通过 TaskManager 统一调度"""

    def _execute(self):
        self.update_progress(10, "Verifying hardware and AI model files...")

        embed_id = self.kwargs.get('embed_id')
        rerank_id = self.kwargs.get('rerank_id')

        dev = DeviceManager().get_optimal_device()

        real_embed = embed_id
        if real_embed == "embed_auto": real_embed = resolve_auto_model("embedding", dev)

        real_rerank = rerank_id
        if real_rerank == "rerank_auto": real_rerank = resolve_auto_model("reranker", dev)

        to_download = []
        e_conf = get_model_conf(real_embed, "embedding")
        if e_conf and not e_conf.get('is_network', False) and not check_model_exists(e_conf.get('hf_repo_id')):
            to_download.append(e_conf['hf_repo_id'])

        r_conf = get_model_conf(real_rerank, "reranker")
        if r_conf and not r_conf.get('is_network', False) and not check_model_exists(r_conf.get('hf_repo_id')):
            to_download.append(r_conf['hf_repo_id'])

        self.update_progress(90, "Verification complete.")

        return {
            "to_download": to_download
        }


class FetchModelsTask(BackgroundTask):
    """拉取远端模型列表任务"""

    def _execute(self):
        base_url = self.kwargs.get("base_url", "")
        api_key = self.kwargs.get("api_key", "")
        provider_id = self.kwargs.get("provider_id", "")

        self.update_progress(20, "Connecting to API endpoint...")
        proxy_url = self.config.user_settings.get("proxy_url", "").strip()

        if provider_id == "anthropic" or "api.anthropic.com" in base_url:
            try:
                import httpx as ax
                client = Anthropic(
                    api_key=api_key or "sk-dummy",
                    base_url=base_url,
                    http_client=ax.Client(proxy=proxy_url) if proxy_url else None
                )
                page = client.beta.models.list()
                models = sorted([m.id for m in page.data])
                self.update_progress(100, "Done")
                return {"success": True, "models": models, "msg": f"Successfully fetched {len(models)} models."}
            except Exception as e:
                return {"success": False, "models": [], "msg": str(e)}

        url = f"{base_url.rstrip('/')}/models"
        session = requests.Session()
        if proxy_url:
            session.proxies = {"http": proxy_url, "https": proxy_url}
        else:
            session.trust_env = False

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

        try:
            res = session.get(url, headers=headers, timeout=10)
            res.raise_for_status()
            data = res.json()
            models = sorted([m['id'] for m in data.get('data', [])])
            if not models:
                raise ValueError("API returned an empty model list.")

            self.update_progress(100, "Done")
            return {"success": True, "models": models, "msg": f"Successfully fetched {len(models)} models."}
        except Exception as e:
            return {"success": False, "models": [], "msg": str(e)}
        finally:
            session.close()



class TestApiTask(BackgroundTask):
    """测试大模型 API 连通性"""

    def _execute(self):
        base_url = self.kwargs.get("base_url", "")
        api_key = self.kwargs.get("api_key", "")
        model_name = self.kwargs.get("model_name", "")
        custom_params = self.kwargs.get("custom_params", {})
        provider_id = self.kwargs.get("provider_id", "")

        self.update_progress(30, f"Sending test prompt to {model_name}...")
        proxy_url = self.config.user_settings.get("proxy_url", "").strip()

        # 🚀 绝对特判：唯独 Anthropic 走官方 SDK
        if provider_id == "anthropic" or "api.anthropic.com" in base_url:
            try:
                import httpx as ax
                client = Anthropic(
                    api_key=api_key or "sk-dummy",
                    base_url=base_url,
                    http_client=ax.Client(proxy=proxy_url) if proxy_url else None
                )
                response = client.messages.create(
                    model=model_name,
                    max_tokens=10,
                    messages=[{"role": "user", "content": "Hello. Please reply with exactly one word: 'OK'."}]
                )
                raw_content = response.content[0].text if response.content else "[Empty]"
                self.update_progress(100, "Done")
                return {"success": True, "msg": f"✅ Connection excellent!\nModel replied: '{raw_content.strip()}'"}
            except Exception as e:
                return {"success": False, "msg": str(e)}

        # 🚀 其余所有模型，统统强制走 OpenAI 标准的 POST /chat/completions 接口！
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
            raw_content = msg_obj.get("content") or msg_obj.get("reasoning_content") or "[Empty Response]"

            self.update_progress(100, "Done")
            return {"success": True, "msg": f"✅ Connection excellent!\nModel replied: '{raw_content.strip()}'"}
        except Exception as e:
            return {"success": False, "msg": str(e)}