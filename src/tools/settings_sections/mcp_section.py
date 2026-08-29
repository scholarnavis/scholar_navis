"""Settings UI mixin: Agent tools (MCP servers / Native skills) management.

拆分自 src/tools/settings_tool.py。负责 MCP 表格的构建、渲染、状态刷新、
添加 / 编辑 / 删除以及 Native Skill 的导入流程。所有方法运行于共享的
SettingsTool 实例上。
"""

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFileDialog,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from src.core.mcp_manager import MCPManager
from src.core.skill_manager import SkillManager
from src.core.theme_manager import ThemeManager
from src.ui.components.toast import ToastManager


class McpSectionMixin:
    """MCP 服务与 Native Skill 管理区块。"""

    # ---------- Section build ----------
    def init_agent_tool_section(self):
        tm = ThemeManager()
        group = QGroupBox("AI Agent & External Tools")
        layout = QVBoxLayout(group)

        header_layout = QHBoxLayout()
        header_layout.addWidget(QLabel("<b>Manage Local & Remote Tools:</b>"))
        header_layout.addStretch()

        self.btn_add_mcp = QPushButton(" Add MCP Server")
        self.btn_add_mcp.clicked.connect(self._on_add_mcp_clicked)

        self.btn_import_skill = QPushButton(" Import Native Skill")
        self.btn_import_skill.clicked.connect(self._on_import_skill_clicked)
        self.btn_import_skill.setStyleSheet(self._get_btn_style(btn_type="warning"))

        self.btn_refresh_mcp = QPushButton(" Refresh Status")
        self.btn_refresh_mcp.clicked.connect(self._on_refresh_mcp_clicked)

        header_layout.addWidget(self.btn_import_skill)
        header_layout.addWidget(self.btn_add_mcp)
        header_layout.addWidget(self.btn_refresh_mcp)
        layout.addLayout(header_layout)

        self.table_mcp = QTableWidget(0, 7)
        self.table_mcp.setHorizontalHeaderLabels(
            ["Enabled", "Name", "Description", "Type", "Target", "Status", "Action"])
        self.table_mcp.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table_mcp.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        self.table_mcp.horizontalHeader().setSectionResizeMode(2, QHeaderView.Interactive)
        self.table_mcp.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table_mcp.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_mcp.setFixedHeight(220)
        self.table_mcp.cellDoubleClicked.connect(self._on_mcp_double_clicked)
        layout.addWidget(self.table_mcp)

        self.lbl_mcp_hint = QLabel(
            "💡 <i>Changes to MCP servers require clicking the blue 'Save Settings & Verify' button below to take effect.</i>")
        layout.addWidget(self.lbl_mcp_hint)

        self.layout.addWidget(group)

    # ---------- Status ----------
    def _refresh_mcp_status(self):
        try:

            tm = ThemeManager()
            mcp_mgr = MCPManager.get_instance()
            skill_mgr = SkillManager.get_instance()

            for row in range(self.table_mcp.rowCount()):
                name_item = self.table_mcp.item(row, 1)
                if not name_item: continue
                name = name_item.text()

                # 获取该行的工具类型 (SKILL 还是 stdio/sse)
                cfg = name_item.data(Qt.UserRole)
                tool_type = cfg.get("type", "stdio")

                status_lbl = self.table_mcp.cellWidget(row, 5)
                if not status_lbl: continue

                chk_widget = self.table_mcp.cellWidget(row, 0)
                chk = chk_widget.layout().itemAt(0).widget() if chk_widget else None
                is_enabled = chk.isChecked() if chk else False

                if is_enabled:
                    # 分支 1: 处理 Native SKILL 的状态
                    if tool_type == "SKILL":
                        if skill_mgr.is_skill_available(name):
                            status_lbl.setText("Ready (Native)")
                            status_lbl.setStyleSheet(f"color: {tm.color('success')}; font-weight: bold;")
                        else:
                            status_lbl.setText("Not Loaded")
                            status_lbl.setStyleSheet(f"color: {tm.color('danger')};")
                            status_lbl.setToolTip("Script not found or failed to load. Check logs.")

                    # 分支 2: 处理 MCP 服务的状态
                    else:
                        status = mcp_mgr.get_server_status(name)
                        if status == "connected":
                            status_lbl.setText("Connected")
                            status_lbl.setStyleSheet(f"color: {tm.color('success')}; font-weight: bold;")
                        elif "error" in status:
                            status_lbl.setText("Error")
                            status_lbl.setStyleSheet(f"color: {tm.color('danger')};")
                            status_lbl.setToolTip(status)
                        else:
                            status_lbl.setText(status.capitalize())
                            status_lbl.setStyleSheet(f"color: {tm.color('warning')};")
                else:
                    status_lbl.setText("Disabled")
                    status_lbl.setStyleSheet(f"color: {tm.color('text_muted')};")

        except Exception as e:
            self.logger.error(f"Status refresh failed: {e}")

    # ---------- Table rendering ----------
    def _on_mcp_double_clicked(self, row, col):
        name_item = self.table_mcp.item(row, 1)
        if not name_item: return

        name = name_item.text()

        if name == "builtin":
            ToastManager().show(f"Core service '{name}' cannot be edited here.", "info")
            return

        self._on_edit_mcp_clicked(row)

    def _load_mcp_servers_to_ui(self):
        self.table_mcp.setRowCount(0)

        servers = self.config.mcp_servers.get("mcpServers", {})
        dirty = False
        for bad_name in ["built-in", "Academic Tool"]:
            if bad_name in servers:
                del servers[bad_name]
                dirty = True
        if dirty:
            self.config.save_mcp_servers()

        for name, cfg in servers.items():
            self._add_mcp_row(name, cfg)

        skills = self.config.mcp_servers.get("external_skills", {})
        if isinstance(skills, dict) and "scripts" not in skills:
            for name, info in skills.items():
                if isinstance(info, str):
                    cfg = {"type": "SKILL", "command": info, "enabled": True, "description": "Native Python Script"}
                else:
                    cfg = info
                self._add_mcp_row(name, cfg)

    def _on_refresh_mcp_clicked(self):
        try:
            mcp_mgr = MCPManager.get_instance()
            mcp_mgr.bootstrap_servers(force_all=False)

            from src.core.skill_manager import SkillManager
            skill_mgr = SkillManager.get_instance()
            if hasattr(skill_mgr, 'reload_external_skills'):
                skill_mgr.reload_external_skills()

            ToastManager().show("Refreshing external tool states...", "info")
            self._refresh_mcp_status()
        except Exception as e:
            self.logger.error(f"Refresh clicked failed: {e}")

    def _add_mcp_row(self, name, cfg, is_hardcoded=False):
        row = self.table_mcp.rowCount()
        self.table_mcp.insertRow(row)

        always_on = cfg.get("always_on", False)
        is_enabled = cfg.get("enabled", False) or always_on

        chk = QCheckBox()
        chk.setChecked(is_enabled)

        tm = ThemeManager()
        muted_color = QColor(tm.color("text_muted"))

        if always_on or is_hardcoded:
            chk.setEnabled(False)
            if always_on:
                chk.setToolTip("Core service must remain enabled.")
            if is_hardcoded:
                chk.setChecked(True)  # 强制勾选
                chk.setToolTip("Built-in Academic Tools cannot be disabled here.")

        if hasattr(self, '_mark_unsaved'):
            chk.stateChanged.connect(self._mark_unsaved)

        chk_widget = QWidget()
        l = QHBoxLayout(chk_widget)
        l.addWidget(chk)
        l.setAlignment(Qt.AlignCenter)
        l.setContentsMargins(0, 0, 0, 0)
        self.table_mcp.setCellWidget(row, 0, chk_widget)

        name_item = QTableWidgetItem(name)
        name_item.setData(Qt.UserRole, cfg)
        name_item.setFlags(name_item.flags() ^ Qt.ItemIsEditable)
        if is_hardcoded:
            name_item.setForeground(muted_color)
        self.table_mcp.setItem(row, 1, name_item)

        desc_str = cfg.get("description", "")
        desc_item = QTableWidgetItem(desc_str)
        desc_item.setFlags(desc_item.flags() ^ Qt.ItemIsEditable)
        if is_hardcoded:
            desc_item.setForeground(muted_color)
        self.table_mcp.setItem(row, 2, desc_item)

        stype = cfg.get("type", "stdio")
        type_item = QTableWidgetItem(stype)
        type_item.setFlags(type_item.flags() ^ Qt.ItemIsEditable)
        if is_hardcoded:
            type_item.setForeground(muted_color)
        self.table_mcp.setItem(row, 3, type_item)

        target = cfg.get("command", "") if stype == "stdio" else cfg.get("url", "")
        target_item = QTableWidgetItem(target)
        target_item.setFlags(target_item.flags() ^ Qt.ItemIsEditable)
        if is_hardcoded:
            target_item.setForeground(muted_color)
        self.table_mcp.setItem(row, 4, target_item)

        status_lbl = QLabel("Checking...")
        status_lbl.setAlignment(Qt.AlignCenter)
        self.table_mcp.setCellWidget(row, 5, status_lbl)

        action_widget = QWidget()
        al = QHBoxLayout(action_widget)
        al.setContentsMargins(0, 0, 0, 0)
        tm = ThemeManager()

        # 锁定 Academic Tool 和历史遗留的 builtin，不给编辑和删除按钮
        if is_hardcoded or name == "builtin" or name == "Academic Tool":
            lbl_lock = QLabel()
            lbl_lock.setPixmap(tm.icon("lock", "text_muted").pixmap(16, 16))
            lbl_lock.setAlignment(Qt.AlignCenter)
            lbl_lock.setToolTip("Core system service (Read-only)")
            al.addWidget(lbl_lock)
        else:
            btn_edit = QPushButton()
            btn_edit.setIcon(tm.icon("edit", "accent"))
            btn_edit.setCursor(Qt.PointingHandCursor)
            btn_edit.setStyleSheet("background: transparent; border: none;")
            btn_edit.clicked.connect(lambda _, r=row: self._on_edit_mcp_clicked(r))
            al.addWidget(btn_edit)

            if not always_on:
                btn_del = QPushButton()
                btn_del.setIcon(tm.icon("delete", "danger"))
                btn_del.setCursor(Qt.PointingHandCursor)
                btn_del.setStyleSheet("background: transparent; border: none;")

                def delete_mcp_row(srv_name):
                    from src.ui.components.dialog import StandardDialog
                    dlg = StandardDialog(
                        self.widget,
                        "Confirm Delete",
                        f"Are you sure you want to delete tool '{srv_name}'?\nThis will disconnect it immediately.",
                        show_cancel=True
                    )

                    if dlg.exec():
                        MCPManager.get_instance().disconnect_server(srv_name)

                        for i in range(self.table_mcp.rowCount()):
                            item = self.table_mcp.item(i, 1)
                            if item and item.text() == srv_name:
                                self.table_mcp.removeRow(i)
                                break

                        if hasattr(self, '_mark_unsaved'):
                            self._mark_unsaved()

                btn_del.clicked.connect(lambda _, n=name: delete_mcp_row(n))
                al.addWidget(btn_del)

        self.table_mcp.setCellWidget(row, 6, action_widget)

    # ---------- Add / Edit / Import ----------
    def _on_add_mcp_clicked(self):
        tm = ThemeManager()
        warning_msg = (
            "<b>⚠️ Security Disclaimer for External MCP Servers</b><br><br>"
            "You are about to connect a third-party MCP server to Scholar Navis.<br>"
            "External servers are highly privileged and can execute code, read local files, or access the network on your behalf. "
            f"<span style='color:{tm.color('danger')}; font-weight:bold;'>Only connect to servers from trusted developers.</span><br><br>"
            "<i>The Scholar Navis developers are not responsible for any data loss, security breaches, or system damage caused by third-party MCP servers.</i><br><br>"
            "Do you understand the risks and wish to proceed?"
        )

        from src.ui.components.dialog import StandardDialog, McpConfigDialog

        dlg = StandardDialog(self.widget, "Security Warning", warning_msg, show_cancel=True)
        if not dlg.exec():
            return

        config_dlg = McpConfigDialog(self.widget)
        if config_dlg.exec():
            name, cfg = config_dlg.get_config()
            if not name: return

            if name in ["builtin", "Academic Tool","built-in"]:
                ToastManager().show(f"The name '{name}' is reserved for core system usage.", "error")
                return

            cfg["enabled"] = True
            self._add_mcp_row(name, cfg)

            if hasattr(self, '_mark_unsaved'):
                self._mark_unsaved()

    def _on_import_skill_clicked(self):
        tm = ThemeManager()
        warning_msg = (
            "<b>🚨 CRITICAL SECURITY WARNING: NATIVE SKILL IMPORT</b><br><br>"
            "You are attempting to import a Native Python Skill (`.py` script) directly into the main process of Scholar Navis.<br><br>"
            f"<span style='color:{tm.color('danger')}; font-weight:bold;'>1. ARBITRARY CODE EXECUTION:</span> These scripts run with the EXACT SAME privileges as the main application. Malicious scripts can steal your data, delete files, or compromise your system.<br>"
            f"<span style='color:{tm.color('danger')}; font-weight:bold;'>2. STRICT SANDBOXING:</span> The script MUST ONLY import Python Standard Library modules (e.g., `os`, `json`, `urllib`). Importing third-party pip packages (like `requests`, `pandas`) that are not packaged with Navis will instantly crash the agent with a `ModuleNotFoundError`.<br><br>"
            "<i>Only import scripts from absolutely trusted sources. Do you accept all risks and wish to proceed?</i>"
        )

        import ast

        dlg = StandardDialog(self.widget, "⚠️ HIGH RISK OPERATION", warning_msg, show_cancel=True)
        if not dlg.exec():
            return

        path, _ = QFileDialog.getOpenFileName(self.widget, "Import Native Skill", "", "Python Files (*.py)")
        if not path: return

        skill_name = os.path.basename(path).replace(".py", "")
        with open(path, 'r', encoding='utf-8') as f:
            raw_code = f.read()

        parsed_name = skill_name
        parsed_desc = "User Imported Native Script"
        try:
            tree = ast.parse(raw_code)
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "SCHEMA":
                            # 安全地将代码中的字典结构转为 Python 字典
                            schema_dict = ast.literal_eval(node.value)
                            func_info = schema_dict.get("function", {})
                            if func_info.get("name"):
                                parsed_name = func_info.get("name")
                            if func_info.get("description"):
                                parsed_desc = func_info.get("description")
        except Exception as e:
            self.logger.warning(f"Failed to parse SCHEMA from skill: {e}")

        from src.ui.components.dialog import SkillPreviewDialog, SkillConfigDialog

        preview_dlg = SkillPreviewDialog(self.widget, parsed_name, raw_code, is_importing=True)
        if not preview_dlg.exec():
            return

        final_code = preview_dlg.get_edited_code()

        config_dlg = SkillConfigDialog(self.widget, skill_name=parsed_name, script_path=path, description=parsed_desc)
        if not config_dlg.exec():
            return

        final_name, final_desc, final_path = config_dlg.get_data()

        enc_service = SystemEncryptionService()
        encrypted_bytes = enc_service.encrypt(final_code)

        cfg = {
            "description": final_desc,
            "type": "SKILL",
            "command": f"<Pending Edit: {final_name}>",
            "enabled": True,
            "_pending_bytes": encrypted_bytes,
            "_is_new": True
        }

        self._add_mcp_row(final_name, cfg)

        if hasattr(self, '_mark_unsaved'):
            self._mark_unsaved()

        ToastManager().show(f"Skill '{final_name}' staged. Click 'Save Settings' to commit.", "info")

    def _on_import_skill_finished(self, result, skill_name):
        if result.get("success"):
            target_path = result.get("target_path")

            # 写入表格并标记配置未保存
            cfg = {
                "description": "User Imported Native Script",
                "type": "SKILL",
                "command": target_path,
                "enabled": True
            }

            self._add_mcp_row(skill_name, cfg)

            if hasattr(self, '_mark_unsaved'):
                self._mark_unsaved()

            self.import_skill_pd.show_finish_state(True, "Import Successful",
                                                   f"Skill '{skill_name}' has been encrypted and secured.")
            ToastManager().show(f"Skill '{skill_name}' imported successfully.", "success")
        else:
            self.import_skill_pd.show_finish_state(False, "Import Failed",
                                                   result.get("msg", "Unknown error during encryption."))

    def _on_edit_mcp_clicked(self, row):
        name_item = self.table_mcp.item(row, 1)
        if not name_item: return

        old_name = name_item.text()
        old_cfg = name_item.data(Qt.UserRole)

        if old_cfg.get("type") == "SKILL":
            from src.ui.components.dialog import SkillConfigDialog

            dlg = SkillConfigDialog(
                self.widget,
                skill_name=old_name,
                script_path=old_cfg.get("command", ""),
                description=old_cfg.get("description", "")
            )

            if dlg.exec():
                new_name, new_desc, new_path = dlg.get_data()

                if new_name != old_name and new_name in ["built-in", "Academic Tool", "builtin"]:
                    ToastManager().show(f"The name '{new_name}' is reserved for core system usage.", "error")
                    return

                new_cfg = old_cfg.copy()
                new_cfg["description"] = new_desc  # 更新 Description

                from src.ui.components.dialog import SkillPreviewDialog
                from src.core.encryption_service import SystemEncryptionService


                if new_path != old_cfg.get("command", "") and new_path.endswith(".py"):
                    try:
                        with open(new_path, 'r', encoding='utf-8') as f:
                            raw_code = f.read()

                        preview_dlg = SkillPreviewDialog(self.widget, new_name, raw_code, is_importing=True)
                        if preview_dlg.exec():
                            final_code = preview_dlg.get_edited_code()
                            enc_service = SystemEncryptionService()
                            encrypted_bytes = enc_service.encrypt(final_code)

                            new_cfg["_pending_bytes"] = encrypted_bytes
                            new_cfg["_is_edited"] = True
                            new_path = f"<Pending Edit: {new_name}>"
                        else:
                            return
                    except Exception as e:
                        ToastManager().show(f"Failed to load new script: {e}", "error")
                        return

                elif new_path == old_cfg.get("command", "") and new_path.endswith(".enc") and os.path.exists(new_path):
                    try:
                        enc_service = SystemEncryptionService()
                        with open(new_path, 'rb') as f:
                            decrypted_code = enc_service.decrypt(f.read())

                        preview_dlg = SkillPreviewDialog(self.widget, new_name, decrypted_code, is_importing=True)
                        if preview_dlg.exec():
                            final_code = preview_dlg.get_edited_code()
                            encrypted_bytes = enc_service.encrypt(final_code)

                            new_cfg["_pending_bytes"] = encrypted_bytes
                            new_cfg["_is_edited"] = True
                            new_path = f"<Pending Edit: {new_name}>"
                        else:
                            return
                    except Exception as e:
                        ToastManager().show(f"Failed to decrypt existing skill: {e}", "error")
                        return

                # 更新 UI 和内存中的数据配置
                new_cfg["command"] = new_path
                name_item.setText(new_name)
                name_item.setData(Qt.UserRole, new_cfg)
                self.table_mcp.item(row, 2).setText(new_desc)
                self.table_mcp.item(row, 4).setText(new_path)

                if hasattr(self, '_mark_unsaved'):
                    self._mark_unsaved()

        else:
            from src.ui.components.dialog import McpConfigDialog
            dlg = McpConfigDialog(self.widget, server_name=old_name, server_config=old_cfg)
            if dlg.exec():
                new_name, new_server_cfg = dlg.get_config()

                if new_name != old_name and new_name in ["built-in", "Academic Tool", "builtin"]:
                    ToastManager().show(f"The name '{new_name}' is reserved for core system usage.", "error")
                    return

                new_server_cfg["always_on"] = old_cfg.get("always_on", False)
                new_server_cfg["enabled"] = old_cfg.get("enabled", True)

                name_item.setText(new_name)
                name_item.setData(Qt.UserRole, new_server_cfg)
                self.table_mcp.item(row, 2).setText(new_server_cfg.get("description", ""))
                self.table_mcp.item(row, 3).setText(new_server_cfg.get("type", "stdio"))

                target = new_server_cfg.get("command", "") if new_server_cfg.get("type",
                                                                                 "stdio") == "stdio" else new_server_cfg.get(
                    "url", "")
                self.table_mcp.item(row, 4).setText(target)

                if hasattr(self, '_mark_unsaved'):
                    self._mark_unsaved()
