import os
import platform
from urllib.parse import quote

from src.core.config_manager import ConfigManager
from src.core.core_task import BackgroundTask
from src.core.device_manager import DeviceManager
from src.core.models_registry import resolve_auto_model, get_model_conf
from src.core.network_worker import create_robust_session
from src.core.version import (
    __channel__,
    __changelog__,
    __dl__,
    __github__,
    __latest__,
    __version__,
    __versions__,
    is_newer_version,
)


class VersionCheckTask(BackgroundTask):
    """更新检查：一次取回两条通道，只与**本机所属通道**比较。

    通道与比较规则集中在 :mod:`src.core.version`（含 ``-dev`` 即开发通道），
    这里只负责取数与拼装结果，不做版本语义判断。

    返回 payload（由 :class:`src.tools.about_tool.AboutTool` 消费）::

        {
          "channel": "dev",                     # 本机版本所属通道
          "current_version": "2.0.6-dev-1",
          "os": "linux",                        # platform.system() 小写
          "stable": "2.0.6",                    # 各通道最新版本，缺失为 "0.0.0"
          "dev": "2.0.7-dev-1",
          "latest_version": "2.0.7-dev-1",      # 仅"本机通道确有更新"时非空
          "changelog": "### ...",               # 该版本的 Markdown 更新日志，可为空
          "release_url": "https://...",         # GitHub Release 页面
          "download_url": "https://.../dl?...", # 直链下载（已带通道）
        }
    """

    #: 云端用来表示"该通道暂无产物"的哨兵版本号。
    _NOT_AVAILABLE = "0.0.0"

    def _execute(self):
        os_name = platform.system().lower()
        payload = {
            "channel": __channel__,
            "current_version": __version__,
            "os": os_name,
            "stable": None,
            "dev": None,
            "latest_version": None,
            "changelog": None,
            "release_url": None,
            "download_url": f"{__dl__}?os={os_name}&channel={__channel__}",
        }

        session = create_robust_session()
        try:
            channels = self._fetch_channels(session, os_name)
            if not channels:
                self.logger.warning("No channel manifest available; update check skipped.")
                return payload

            payload["stable"] = channels.get("stable")
            payload["dev"] = channels.get("dev")

            target = (channels.get(__channel__) or "").strip()
            self.logger.info(
                f"Channel '{__channel__}' latest={target or 'unknown'} "
                f"(stable={channels.get('stable')}, dev={channels.get('dev')})")

            if not target or target == self._NOT_AVAILABLE:
                self.logger.info("No published release for this channel yet; nothing to do.")
                return payload

            if not is_newer_version(target, __version__):
                self.logger.info(f"Already up to date (local={__version__}, remote={target}).")
                return payload

            payload["latest_version"] = target
            # 兜底链接：即使更新日志接口不可用，也能让用户跳到对应 tag 的 Release 页。
            payload["release_url"] = f"{__github__}/releases/tag/v{target}"

            notes = self._fetch_changelog(session, target)
            if notes:
                payload["changelog"] = notes.get("body") or None
                payload["release_url"] = notes.get("release_url") or payload["release_url"]

            return payload
        except Exception as e:
            self.logger.error(f"Failed to check for updates: {e}")
            return payload
        finally:
            try:
                session.close()
            except Exception as e:
                self.logger.debug(f"Session close failed (ignored): {e}")

    def _fetch_channels(self, session, os_name):
        """取该平台两条通道的最新版本；失败时回退到旧版 ``/latest`` 接口。

        回退是必需的：应用可能先于 Worker 更新上线（或 Worker 回滚），
        此时 ``/versions`` 会 404/非 JSON，直接放弃会让所有用户失去更新提示。
        """
        url = f"{__versions__}?os={os_name}"
        self.logger.debug(f"Checking for updates at: {url}")

        try:
            response = session.get(url, timeout=15)
            if response.status_code == 200:
                channels = response.json().get("channels")
                if isinstance(channels, dict):
                    return channels
                self.logger.warning("/versions payload has no 'channels' object; "
                                    "falling back to /latest.")
            else:
                self.logger.warning(f"/versions responded with HTTP {response.status_code}; "
                                    f"falling back to /latest.")
        except Exception as e:
            self.logger.warning(f"Channel manifest request failed ({e}); "
                                f"falling back to /latest.")

        return self._fetch_legacy_latest(session, os_name)

    def _fetch_legacy_latest(self, session, os_name):
        """兼容旧接口：只查本机通道的最新版本，返回同样形状的字典。"""
        url = f"{__latest__}?os={os_name}&channel={__channel__}"
        try:
            response = session.get(url, timeout=15)
            if response.status_code == 200:
                version = response.text.strip()
                self.logger.info(f"Legacy /latest returned: {version}")
                return {__channel__: version}
            self.logger.warning(f"Update check failed with status code: {response.status_code}")
        except Exception as e:
            self.logger.error(f"Legacy update check failed: {e}")
        return None

    def _fetch_changelog(self, session, version):
        """取指定版本的更新日志（Worker 代理 GitHub Release，返回 Markdown）。

        失败**不**影响更新提示本身：UI 会退化为只提供 Release 页面链接。
        """
        url = f"{__changelog__}?version={quote(version)}"
        try:
            response = session.get(url, timeout=15)
            if response.status_code != 200:
                self.logger.warning(f"Changelog request for v{version} failed "
                                    f"with HTTP {response.status_code}.")
                return None
            data = response.json()
            self.logger.info(f"Release notes for v{version} fetched "
                             f"({len(data.get('body') or '')} chars).")
            return data
        except Exception as e:
            self.logger.warning(f"Changelog fetch failed for v{version}: {e}")
            return None


class VerifyModelsTask(BackgroundTask):
    def _check_local_onnx_exists(self, repo_id):
        """核心探测逻辑：严格检查 /models 下的对应目录是否有 .onnx 文件"""
        if not repo_id:
            self.logger.warning("VerifyModelsTask: Model check skipped - no repo_id provided.")
            return False

        repo_folder = f"models--{repo_id.replace('/', '--')}"
        model_dir = os.path.join(ConfigManager().BASE_DIR, "models", repo_folder)

        self.logger.info(f"VerifyModelsTask: Searching for ONNX model '{repo_id}' at expected path '{model_dir}'")

        if not os.path.exists(model_dir):
            self.logger.error(f"VerifyModelsTask: ONNX check failed. Directory missing: '{model_dir}'")
            return False

        for root, dirs, files in os.walk(model_dir):
            if self.is_cancelled():
                raise InterruptedError("Verification safely terminated by user.")

            if any(f.endswith('.onnx') for f in files):
                self.logger.info(f"VerifyModelsTask: ONNX files successfully verified for '{repo_id}' at '{root}'")
                return True

        self.logger.warning(
            f"VerifyModelsTask: ONNX check failed. Directory exists but no .onnx files found for '{repo_id}'")
        return False

    def _execute(self):
        self.update_progress(10, "Verifying hardware and AI model files (ONNX)...")

        embed_id = self.kwargs.get('embed_id')
        rerank_id = self.kwargs.get('rerank_id')

        dev = DeviceManager().get_optimal_device()

        real_embed = embed_id
        if real_embed == "embed_auto": real_embed = resolve_auto_model("embedding", dev)

        real_rerank = rerank_id
        if real_rerank == "rerank_auto": real_rerank = resolve_auto_model("reranker", dev)

        to_download = []

        # 修改点：在检查嵌入模型前更新进度条状态
        self.update_progress(30, f"Checking Embedding model: {real_embed}...")
        e_conf = get_model_conf(real_embed, "embedding")
        if e_conf and not e_conf.get('is_network', False):
            if not self._check_local_onnx_exists(e_conf.get('hf_repo_id')):
                to_download.append(e_conf['hf_repo_id'])

        # 修改点：在检查重排序模型前更新进度条状态
        self.update_progress(60, f"Checking Reranker model: {real_rerank}...")
        r_conf = get_model_conf(real_rerank, "reranker")
        if r_conf and not r_conf.get('is_network', False):
            if not self._check_local_onnx_exists(r_conf.get('hf_repo_id')):
                to_download.append(r_conf['hf_repo_id'])

        self.update_progress(100, "Verification complete.")

        return {
            "to_download": to_download,
            "embed": {"id": real_embed, "repo_id": e_conf.get('hf_repo_id') if e_conf else None,
                      "is_network": e_conf.get('is_network', False) if e_conf else False},
            "rerank": {"id": real_rerank, "repo_id": r_conf.get('hf_repo_id') if r_conf else None,
                       "is_network": r_conf.get('is_network', False) if r_conf else False}
        }