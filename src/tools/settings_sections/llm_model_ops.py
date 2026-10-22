"""Settings UI mixin: LLM model entry operations and remote tasks.

拆分自 src/tools/settings_tool.py。负责模型条目的增删 / 参数编辑、
模型列表刷新，以及 Fetch Models / Test API 两个网络任务。
"""

from src.core.core_task import TaskManager, TaskMode
from src.core.i18n import tr
from src.core.theme_manager import ThemeManager
from src.task.settings_tasks import FetchModelsTask, TestApiTask
from src.ui.components.dialog import (AddModelDialog, ProgressDialog,
                                      StandardDialog)
from src.ui.components.toast import ToastManager

MINIMAX_DEFAULT_MODELS = [
    "MiniMax-M2", "M2-her", "MiniMax-M2.1",
    "MiniMax-M2.1-lightning", "MiniMax-M2.5", "MiniMax-M2.5-lightning"
]


class LlmModelOpsMixin:
    """LLM 模型条目操作与远程任务（Fetch / Test）。"""

    # ---------- Params ----------
    # 自定义参数编辑器已从界面移除（见 llm_section）；下方方法只保留空实现，
    # 防止旧的模型切换路径在缺控件时抛 AttributeError。
    def _on_copy_params_clicked(self):
        """参数复制入口已随参数编辑器一起移除（无 UI 可复制）。"""
        return

    def _on_model_index_changed(self, index):
        if self._is_updating_model_ui or index < 0: return

        idx = self.combo_llm_preset.currentIndex()
        if idx < 0: return
        conf = self.llm_configs[idx]

        real_model_name = self._extract_real_model_name(self.combo_llm_model.itemText(index).strip())
        self._load_model_params_to_ui(conf, real_model_name)

    # ---------- Model CRUD ----------
    def _add_llm_model(self):
        dlg = AddModelDialog(self.widget)
        if dlg.exec():
            new_model = dlg.get_name().strip()
            if new_model:
                idx = self.combo_llm_preset.currentIndex()
                if idx >= 0:
                    conf = self.llm_configs[idx]
                    if "fetched_models" not in conf:
                        conf["fetched_models"] = []
                    if new_model not in conf["fetched_models"]:
                        conf["fetched_models"].insert(0, new_model)
                    self._refresh_model_combo(conf)

                    for i in range(self.combo_llm_model.count()):
                        if self._extract_real_model_name(self.combo_llm_model.itemText(i)) == new_model:
                            self.combo_llm_model.setCurrentIndex(i)
                            break

    def _del_llm_model(self):
        idx = self.combo_llm_model.currentIndex()
        if idx < 0: return

        curr_text = self.combo_llm_model.itemText(idx)
        real_name = self._extract_real_model_name(curr_text)

        from src.ui.components.dialog import StandardDialog
        dlg = StandardDialog(self.widget, tr("Delete Model"),
                             tr("Are you sure you want to remove '{model}' from the list?").format(model=real_name),
                             show_cancel=True)
        if dlg.exec():
            provider_idx = self.combo_llm_preset.currentIndex()
            if provider_idx >= 0:
                conf = self.llm_configs[provider_idx]

                if "fetched_models" in conf and real_name in conf["fetched_models"]:
                    conf["fetched_models"].remove(real_name)
                if "models_config" in conf and real_name in conf["models_config"]:
                    del conf["models_config"][real_name]

                if conf.get("model_name") == real_name:
                    conf["model_name"] = conf["fetched_models"][0] if conf.get("fetched_models") else ""

                self.combo_llm_model.blockSignals(True)
                self.combo_llm_model.setCurrentText("")
                self.combo_llm_model.blockSignals(False)

                self._refresh_model_combo(conf)
                self._sync_llm_data_execute()

    def _load_model_params_to_ui(self, conf, model_name):
        self._is_updating_model_ui = True

        # 参数编辑器已移除：这里不再回填任何控件。已有配置里的
        # models_config[*].params / provider_params 原样保留（保存时也不再覆盖），
        # 只是不再提供编辑入口。
        self._is_updating_model_ui = False

    def _refresh_model_combo(self, conf):
        self._is_updating_model_ui = True

        curr_real = conf.get("model_name", "").strip()

        self.combo_llm_model.blockSignals(True)
        self.combo_llm_model.clear()

        fetched = [m for m in conf.get("fetched_models", []) if m.strip()]
        models_config = conf.get("models_config", {})

        for m in models_config.keys():
            if m.strip() and m not in fetched:
                fetched.append(m)

        if curr_real and curr_real not in fetched:
            fetched.insert(0, curr_real)

        tm = ThemeManager()

        for m in fetched:
            mode = models_config.get(m, {}).get("mode", "inherit")

            if mode == "custom":
                self.combo_llm_model.addItem(tm.icon("settings", "warning"), f"{m} [Custom]")
            elif mode == "closed":
                self.combo_llm_model.addItem(tm.icon("cancel", "danger"), f"{m} [Closed]")
            else:
                self.combo_llm_model.addItem(tm.icon("api", "text_muted"), m)

        idx_to_select = -1
        for i in range(self.combo_llm_model.count()):
            if self._extract_real_model_name(self.combo_llm_model.itemText(i)) == curr_real:
                idx_to_select = i
                break

        if idx_to_select >= 0:
            self.combo_llm_model.setCurrentIndex(idx_to_select)

        self.combo_llm_model.blockSignals(False)
        self._is_updating_model_ui = False

        self._load_model_params_to_ui(conf, curr_real)

    # ---------- Fetch / Test tasks ----------
    def _start_fetch_task(self):
        self._sync_llm_data_execute()
        idx = self.combo_llm_preset.currentIndex()
        conf = self.llm_configs[idx] if 0 <= idx < len(self.llm_configs) else {}

        # 如果是 MiniMax，走本地刷新逻辑
        if conf.get("id") == "minimax":
            self._refresh_minimax_models(conf)
            return

        base_url = conf.get("base_url", "").strip()
        api_key = conf.get("api_key", "").strip()
        provider_id = conf.get("id", "").strip()

        if not base_url:
            StandardDialog(self.widget, tr("Warning"), tr("Please enter API Base URL first.")).exec()
            return

        self.net_pd = ProgressDialog(self.widget, tr("Network Request"), tr("Contacting API..."))
        self.net_pd.show()

        self.fetch_task_mgr = TaskManager()
        self.fetch_task_mgr.sig_progress.connect(self.net_pd.update_progress)
        self.fetch_task_mgr.sig_result.connect(self._on_models_fetched)
        self.net_pd.sig_canceled.connect(self.fetch_task_mgr.cancel_task)

        self.fetch_task_mgr.start_task(
            FetchModelsTask, task_id="fetch_models", mode=TaskMode.THREAD,
            base_url=base_url, api_key=api_key, provider_id=provider_id
        )

    def _refresh_minimax_models(self, conf):
        current_models = conf.get("fetched_models", [])

        for model in MINIMAX_DEFAULT_MODELS:
            if model not in current_models:
                current_models.append(model)

        conf["fetched_models"] = current_models
        self._refresh_model_combo(conf)

        ToastManager().show(tr("MiniMax model list refreshed (defaults restored)."), "success")

    def _on_models_fetched(self, result):
        if result.get("success"):
            models = result["models"]
            self.logger.info(f"Successfully fetched {len(models)} models from API.")
            idx = self.combo_llm_preset.currentIndex()
            if 0 <= idx < len(self.llm_configs):
                self.llm_configs[idx]["fetched_models"] = models
                self._refresh_model_combo(self.llm_configs[idx])
            self.net_pd.show_finish_state(True, tr("Success"), result["msg"])
        else:
            self.logger.warning(f"Failed to fetch models: {result['msg']}")
            self.net_pd.show_finish_state(False, tr("Fetch Failed"), result['msg'])

    def _start_test_task(self):
        self._sync_llm_data_execute()
        idx = self.combo_llm_preset.currentIndex()
        conf = self.llm_configs[idx] if 0 <= idx < len(self.llm_configs) else {}

        base_url = conf.get("base_url", "").strip()
        api_key = conf.get("api_key", "").strip()
        provider_id = conf.get("id", "")
        model_name = self._extract_real_model_name(self.combo_llm_model.currentText().strip())

        if not base_url or not model_name:
            StandardDialog(self.widget, tr("Warning"),
                           tr("Please ensure Base URL and Model Name are provided.")).exec()
            return

        models_config = conf.get("models_config", {})
        param_mode = models_config.get(model_name, {}).get("mode", conf.get("model_params_mode", "inherit"))
        custom_params_list = conf.get("provider_params", []) if param_mode == "inherit" else models_config.get(
            model_name, {}).get("params", [])

        parsed_params = {}
        for p in custom_params_list:
            if not p.get("name"): continue
            try:
                if p["type"] == "int":
                    parsed_params[p["name"]] = int(p["value"])
                elif p["type"] == "float":
                    parsed_params[p["name"]] = float(p["value"])
                elif p["type"] == "bool":
                    parsed_params[p["name"]] = str(p["value"]).lower() in ['true', '1']
                else:
                    parsed_params[p["name"]] = p["value"]
            except:
                pass

        self.net_pd = ProgressDialog(self.widget, tr("API Connection Test"),
                                     tr("Sending test prompt to '{model}'...").format(model=model_name))
        self.net_pd.show()

        self.test_task_mgr = TaskManager()
        self.test_task_mgr.sig_progress.connect(self.net_pd.update_progress)
        self.test_task_mgr.sig_result.connect(self._on_test_finished)
        self.net_pd.sig_canceled.connect(self.test_task_mgr.cancel_task)

        self.test_task_mgr.start_task(
            TestApiTask, task_id="test_api", mode=TaskMode.THREAD,
            base_url=base_url, api_key=api_key, model_name=model_name, custom_params=parsed_params,
            provider_id=provider_id
        )

    def _on_test_finished(self, result):
        if result.get("success"):
            self.net_pd.show_finish_state(True, tr("Test Passed"), result["msg"])
        else:
            self.net_pd.show_finish_state(False, tr("Test Failed"), result["msg"])
