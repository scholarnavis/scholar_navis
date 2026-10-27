"""
R Environment Task
==================
把可视化所需的 R 环境备好，两种情况一条通路：

* **装包（默认）**：把缺失的绘图包装进私有库 ``<BASE_DIR>/r_env/library``，
  系统 R 与其库路径一概不动，删掉 ``r_env`` 即完全回退；
* **私有运行时**：机器上没有可用 R、或系统 R 只读且无法编译装包时，下载一个
  conda-forge 预编译的独立运行时（不需要编译器，也不需要管理员权限）。

为什么放在任务层：装包要下载并可能编译，持续几分钟，必须能显示逐行 R 日志、
推进进度、随时取消——这些正是 :class:`~src.core.core_task.BackgroundTask` 已有的
能力，因此不另造线程与日志通道。任务本身不碰界面，只回传日志 / 进度 / 结果，
由设置页的弹窗负责展示（见 ``src/ui/components/dialogs/r_env_dialogs.py``）。
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from src.core.core_task import BackgroundTask

logger = logging.getLogger("Task.REnv")


class REnvSetupTask(BackgroundTask):
    """备好 R 环境：装包装进私有库，必要时改为下载私有运行时。

    参数（经 ``TaskManager.start_task(..., action=..., packages=...)`` 传入）：

    :param action:   ``"packages"``（默认）或 ``"runtime"``。
    :param packages: 目标包清单，缺省取 ``plot_engine.ALLOWED_R_PACKAGES``。
    :param repo:     用户指定的 CRAN 兼容源；为空时按可达性自动选择。

    包清单在这里与 :data:`src.core.plot_engine.ALLOWED_R_PACKAGES` 取交集：本任务
    是"往私有库/R 运行时里装东西"的唯一入口，白名单只在引擎侧定义一次、在入口处
    生效，调用方传什么都不能装到白名单之外的包（详见 :meth:`_allowed`）。
    """

    def _allowed(self, requested: List[str]) -> List[str]:
        """把请求的包清单收敛到引擎白名单，并记录被丢掉的项。

        :data:`src.core.plot_engine.ALLOWED_R_PACKAGES` 是"引擎可能用到的全部包"
        的唯一出处；装包入口再校验一次，任何来源（界面、插件、外部调用）都无法
        越过它。
        """
        from src.core.plot_engine import ALLOWED_R_PACKAGES

        allowed = set(ALLOWED_R_PACKAGES)
        kept = [p for p in requested if p in allowed]
        dropped = [p for p in requested if p not in allowed]
        if dropped:
            self.send_log("WARNING",
                          f"Ignored {len(dropped)} package(s) outside the allow-list: "
                          f"{', '.join(dropped)}")
        if not kept:
            self.send_log("INFO", "No package requested that the visualization engine uses.")
        return kept

    def _execute(self) -> dict:
        from src.core.plot_engine import ALLOWED_R_PACKAGES

        action = str(self.kwargs.get("action") or "packages")
        repo = str(self.kwargs.get("repo") or "")
        requested = [str(p).strip() for p in (self.kwargs.get("packages")
                                              or ALLOWED_R_PACKAGES) if str(p).strip()]
        packages = self._allowed(requested)

        self.send_log("INFO", f"R environment setup started (action={action})")
        self.send_log("INFO", f"Target packages ({len(packages)}): {', '.join(packages)}")
        self.update_progress(1, "Preparing R environment...")

        if action == "runtime":
            return self._setup_runtime(packages)
        return self._install_packages(packages, repo)

    # ------------------------------------------------------------------ #
    #  装包到私有库
    # ------------------------------------------------------------------ #
    def _install_packages(self, packages: List[str], repo: str) -> dict:
        from src.core import r_env
        from src.core.r_engine import get_r_engine

        engine = get_r_engine()
        info = engine.detect()
        if not info.get("available"):
            self.send_log("WARNING",
                          "No usable R runtime on this machine; switching to the "
                          "private runtime download.")
            self.update_progress(5, "Downloading a private R runtime...")
            return self._setup_runtime(packages)

        self.send_log("INFO", f"R interpreter: {info.get('version')} "
                              f"({info.get('executable')})")
        self.send_log("INFO", f"Private library: {r_env.library_dir()}")

        self.update_progress(5, "Checking installed packages...")
        status = engine.check_packages(packages)
        missing = [p for p in packages if not status.get(p)]
        if not missing:
            summary = ("All required R packages are already available." if packages
                       else "No package requested that the visualization engine uses.")
            self.send_log("INFO", summary)
            self.update_progress(100, summary)
            return self._payload(True, "packages", summary, missing=[])

        self.send_log("INFO", f"Missing packages ({len(missing)}): {', '.join(missing)}")

        self.update_progress(8, "Selecting a package repository...")
        source = r_env.select_source(repo, log=lambda line: self.send_log("INFO", line))
        if source is None:
            summary = ("No reachable R package repository. Check the network connection "
                       "or the proxy settings, then run the install again.")
            self.send_log("ERROR", summary)
            return self._payload(False, "packages", summary, missing=missing)

        self.update_progress(10, f"Installing {len(missing)} package(s)...")
        outcome = r_env.install_packages(
            missing,
            interpreter=info["executable"],
            repo_url=source.url,
            binary=source.binary,
            log_cb=lambda line: self.send_log("INFO", line),
            progress_cb=self.update_progress,
            cancel_cb=self.is_cancelled,
        )

        # 装完必须作废包检查缓存，否则界面仍显示"缺失"。
        engine.invalidate_packages()
        still = missing
        if outcome.get("success"):
            # 复核一次：R 的 install.packages 可能只装上一部分（比如某个依赖编不过）。
            after = engine.check_packages(packages)
            still = [p for p in packages if not after.get(p)]

        if outcome.get("cancelled"):
            summary = "Install cancelled; the private library was left in a usable state."
        elif outcome.get("success") and not still:
            summary = f"{len(outcome.get('plan') or missing)} package(s) installed into the " \
                      f"private library."
        elif outcome.get("success"):
            summary = f"Installed, but these are still missing: {', '.join(still)}"
        else:
            summary = f"Install failed: {outcome.get('error') or 'unknown error'}"

        self.send_log("INFO" if outcome.get("success") else "ERROR", summary)
        if outcome.get("log_path"):
            self.send_log("INFO", f"Full R log: {outcome['log_path']}")
        if not outcome.get("cancelled"):
            self.update_progress(100 if outcome.get("success") else 0, summary)

        return self._payload(bool(outcome.get("success")) and not still, "packages",
                            summary, missing=still,
                            repo=source.url,
                            installed=list(outcome.get("plan") or []),
                            log_path=str(outcome.get("log_path") or ""),
                            cancelled=bool(outcome.get("cancelled")))

    # ------------------------------------------------------------------ #
    #  私有运行时
    # ------------------------------------------------------------------ #
    def _setup_runtime(self, packages: List[str]) -> dict:
        from src.core import r_env, r_runtime
        from src.core.r_engine import get_r_engine

        engine = get_r_engine()

        # 已经备好就不再下载：这是显式动作，但也常被当成"检查一下"来点，重跑一遍
        # micromamba 只会白等几分钟。先确认真实可用的私有解释器与包是否齐全，
        # 缺任何一个才继续走下载/创建。
        existing = r_runtime.private_rscript()
        if existing:
            info = r_runtime.verify_runtime(existing)
            if info.get("available"):
                status = engine.check_packages(packages, executable=existing)
                still = [p for p in packages if not status.get(p)]
                if not still:
                    summary = (f"Private R runtime already set up ({info.get('version')}) "
                               f"with all required packages; nothing to download.")
                    self.send_log("INFO", summary)
                    self.update_progress(100, summary)
                    return self._payload(True, "runtime", summary, missing=[],
                                         rscript=existing, lib=r_env.library_dir())
                self.send_log(
                    "INFO",
                    f"The existing private runtime is missing {len(still)} package(s); "
                    f"re-creating it to add them.")

        self.update_progress(2, "Downloading micromamba...")
        download = r_runtime.download_micromamba(
            log_cb=lambda line: self.send_log("INFO", line),
            progress_cb=self.update_progress,
            cancel_cb=self.is_cancelled,
        )
        if download.get("cancelled"):
            summary = "Runtime setup cancelled."
            self.send_log("WARNING", summary)
            return self._payload(False, "runtime", summary, cancelled=True)
        if not download.get("success"):
            summary = f"Cannot obtain micromamba: {download.get('error')}"
            self.send_log("ERROR", summary)
            return self._payload(False, "runtime", summary)

        self.update_progress(65, "Creating the private R runtime...")
        created = r_runtime.create_runtime(
            packages,
            log_cb=lambda line: self.send_log("INFO", line),
            progress_cb=self.update_progress,
            cancel_cb=self.is_cancelled,
        )
        if created.get("cancelled"):
            summary = "Runtime setup cancelled."
            self.send_log("WARNING", summary)
            return self._payload(False, "runtime", summary, cancelled=True)
        if not created.get("success"):
            summary = f"Cannot create the private R runtime: {created.get('error')}"
            self.send_log("ERROR", summary)
            return self._payload(False, "runtime", summary)

        rscript = str(created.get("rscript") or "")
        info = r_runtime.verify_runtime(rscript)
        if not info.get("available"):
            summary = ("The private runtime was created but cannot run on this system: "
                       f"{info.get('detail')}")
            self.send_log("ERROR", summary)
            self.update_progress(0, "Private runtime unusable")
            return self._payload(False, "runtime", summary, rscript=rscript)

        engine.refresh()
        status = engine.check_packages(packages)
        still = [p for p in packages if not status.get(p)]
        skipped = ", ".join(created.get("skipped") or [])

        if still:
            summary = (f"Private R runtime ready ({info.get('version')}), but these packages "
                       f"are not available yet: {', '.join(still)}")
        else:
            summary = (f"Private R runtime ready ({info.get('version')}) with all required "
                       f"packages.")
        if skipped:
            summary += f" Unavailable on the binary channel: {skipped}."

        self.send_log("INFO" if not still else "WARNING", summary)
        self.update_progress(100, summary)
        return self._payload(not still, "runtime", summary, missing=still,
                             rscript=rscript, lib=r_env.library_dir())

    # ------------------------------------------------------------------ #
    def _payload(self, ok: bool, action: str, summary: str, *,
                 missing: Optional[List[str]] = None, **extra) -> dict:
        """统一结果结构：界面据此决定"装好了/还差什么/是否需要换条路"。"""
        payload: Dict[str, object] = {
            "ok": bool(ok),
            "action": action,
            "summary": summary,
            "missing": list(missing or []),
        }
        payload.update(extra)
        return payload