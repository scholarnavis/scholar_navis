"""Settings UI mixin: LLM model entry operations and remote tasks.

拆分自 src/tools/settings_tool.py。负责模型条目的增删 / 参数编辑、
模型列表刷新，以及 Fetch Models / Test API 两个网络任务。
"""

from src.core.core_task import TaskManager, TaskMode
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
    def _on_copy_params_clicked(self):

        provider_params = self.editor_provider_params.extract_data()
        if not provider_params:
            ToastManager().show("Provider has no parameters to copy.", "info")
            return

        model_params = self.editor_model_params.extract_data()
        model_params_dict = {p['name']: p for p in model_params if p.get('name')}

        merged_params = list(model_params)

        for p_param in provider_params:
            name = p_param.get("name", "").strip()
            if not name:
                continue

            if name in model_params_dict:
                m_param = model_params_dict[name]
                msg = (
                    f"Parameter '{name}' already exists in this model.\n\n"
                    f"【Current Model Parameter】\n"
                    f"  • Type: {m_param.get('type')}\n"
                    f"  • Value: {m_param.get('value')}\n\n"
                    f"【Provider Parameter to Copy】\n"
                    f"  • Type: {p_param.get('type')}\n"
                    f"  • Value: {p_param.get('value')}\n\n"
                    f"Do you want to overwrite the model's parameter with the provider's?"
                )

                dlg = StandardDialog(self.widget, "Duplicate Parameter", msg, show_cancel=True)
                reply = dlg.exec()

                if reply:
                    for i, mp in enumerate(merged_params):
                        if mp.get('name') == name:
                            merged_params[i] = p_param.copy()
                            break
            else:
                merged_params.append(p_param.copy())

        try:
            self.editor_model_params.load_data(merged_params, append=False)
        except TypeError:
            self.editor_model_params.load_data(merged_params)

        self._sync_llm_data_execute()
        ToastManager().show("Parameters copied and merged successfully.", "success")

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
        dlg = StandardDialog(self.widget, "Delete Model",
                             f"Are you sure you want to remove '{real_name}' from the list?", show_cancel=True)
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

        models_config = conf.get("models_config", {})
        m_conf = models_config.get(model_name, {})
        mode = m_conf.get("mode", "inherit")
        params = m_conf.get("params", [])

        reverse_map = {"inherit": 0, "custom": 1, "closed": 2}

        self.combo_model_param_strategy.blockSignals(True)
        self.combo_model_param_strategy.setCurrentIndex(reverse_map.get(mode, 0))
        self.combo_model_param_strategy.blockSignals(False)

        self.model_param_container.setVisible(mode == "custom")

        self.editor_model_params.blockSignals(True)
        self.editor_model_params.load_data(params)
        self.editor_model_params.blockSignals(False)

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
            StandardDialog(self.widget, "Warning", "Please enter API Base URL first.").exec()
            return

        self.net_pd = ProgressDialog(self.widget, "Network Request", "Contacting API...")
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

        ToastManager().show("MiniMax model list refreshed (defaults restored).", "success")

    def _on_models_fetched(self, result):
        if result.get("success"):
            models = result["models"]
            self.logger.info(f"Successfully fetched {len(models)} models from API.")
            idx = self.combo_llm_preset.currentIndex()
            if 0 <= idx < len(self.llm_configs):
                self.llm_configs[idx]["fetched_models"] = models
                self._refresh_model_combo(self.llm_configs[idx])
            self.net_pd.show_finish_state(True, "Success", result["msg"])
        else:
            self.logger.warning(f"Failed to fetch models: {result['msg']}")
            self.net_pd.show_finish_state(False, "Fetch Failed", result['msg'])

    def _start_test_task(self):
        self._sync_llm_data_execute()
        idx = self.combo_llm_preset.currentIndex()
        conf = self.llm_configs[idx] if 0 <= idx < len(self.llm_configs) else {}

        base_url = conf.get("base_url", "").strip()
        api_key = conf.get("api_key", "").strip()
        provider_id = conf.get("id", "")
        model_name = self._extract_real_model_name(self.combo_llm_model.currentText().strip())

        if not base_url or not model_name:
            StandardDialog(self.widget, "Warning", "Please ensure Base URL and Model Name are provided.").exec()
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

        self.net_pd = ProgressDialog(self.widget, "API Connection Test", f"Sending test prompt to '{model_name}'...")
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
            self.net_pd.show_finish_state(True, "Test Passed", result["msg"])
        else:
            self.net_pd.show_finish_state(False, "Test Failed", result["msg"])
