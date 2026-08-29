"""MCP server / Native skill configuration dialogs.

拆分自 src/ui/components/dialog.py。包含 MCP 服务器配置、Skill 配置、
Skill 源码预览（含静态安全分析与 Python 语法高亮）。
"""

import ast

from PySide6.QtCore import Qt, QRegularExpression
from PySide6.QtGui import (QColor, QFont, QRegularExpressionValidator,
                           QTextCharFormat, QSyntaxHighlighter)
from PySide6.QtWidgets import (QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from src.core.core_task import TaskManager, TaskMode
from src.core.theme_manager import ThemeManager
from src.task.settings_tasks import TestMcpConnectionTask
from src.ui.components.combo import BaseComboBox
from src.ui.components.dialogs.base import BaseDialog
from src.ui.components.dialogs.common import ProgressDialog, StandardDialog
from src.ui.components.param_editor import ParamEditorWidget
from src.ui.components.source_code_viewer import SourceCodeViewer


class McpConfigDialog(BaseDialog):
    def __init__(self, parent=None, server_name="", server_config=None):
        title = "Edit MCP Server" if server_config else "Add MCP Server"
        super().__init__(parent, title=title, width=660)

        self.task_mgr = None

        self.form_widget = QWidget()
        self.form_layout = QFormLayout(self.form_widget)
        self.form_layout.setSpacing(15)
        self.form_layout.setLabelAlignment(Qt.AlignRight)

        self.inp_name = QLineEdit(server_name)
        self.inp_name.setPlaceholderText("e.g. remote-database-mcp")
        if server_name in ["builtin", "external"]:
            self.inp_name.setEnabled(False)
            self.inp_name.setToolTip("Core component identifier cannot be changed")


        self.desc_container = QWidget()
        desc_v_layout = QVBoxLayout(self.desc_container)
        desc_v_layout.setContentsMargins(0, 0, 0, 0)
        desc_v_layout.setSpacing(4)

        self.inp_desc = QLineEdit()
        self.inp_desc.setPlaceholderText("Describe the tool's purpose (English only)...")  # 修改占位符，提示仅限英文

        desc_regex = QRegularExpression(r"^[\x20-\x7E]*$")
        desc_validator = QRegularExpressionValidator(desc_regex, self.inp_desc)
        self.inp_desc.setValidator(desc_validator)

        self.desc_hint_widget = QWidget()
        hint_layout = QHBoxLayout(self.desc_hint_widget)
        hint_layout.setContentsMargins(0, 0, 0, 0)
        hint_layout.setSpacing(6)

        self.lbl_desc_icon = QLabel()
        self.lbl_desc_icon.setFixedSize(14, 14)

        self.lbl_desc_text = QLabel(
            "<b>Crucial for AI:</b> Clearly describe the tool's purpose in English so the AI knows exactly when to use it.")
        self.lbl_desc_text.setWordWrap(True)
        self.lbl_desc_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        self.lbl_desc_text = QLabel(
            "<b>Crucial for AI:</b> Clearly describe the tool's purpose so the AI knows exactly when to use it.")
        self.lbl_desc_text.setWordWrap(True)
        self.lbl_desc_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        hint_layout.addWidget(self.lbl_desc_icon, 0, Qt.AlignTop)
        hint_layout.addWidget(self.lbl_desc_text, 1)

        desc_v_layout.addWidget(self.inp_desc)
        desc_v_layout.addWidget(self.desc_hint_widget)

        self.combo_type = BaseComboBox()
        self.combo_type.addItems(["stdio", "sse"])

        self.inp_cmd_url = QLineEdit()
        self.inp_args = QLineEdit()
        self.inp_args.setPlaceholderText("arg1, arg2 (comma-separated)")

        self.env_editor = ParamEditorWidget()

        env_btn_layout = QHBoxLayout()
        self.btn_add_env = QPushButton(" Add Entry")
        self.btn_add_env.setIcon(self.tm.icon("add", "text_main"))
        self.btn_add_env.clicked.connect(lambda: self.env_editor.add_param_row())

        self.btn_add_auth = QPushButton(" Insert Authorization Header")
        self.btn_add_auth.setIcon(self.tm.icon("lock", "warning"))
        self.btn_add_auth.clicked.connect(self._add_auth_header)

        env_btn_layout.addWidget(self.btn_add_env)
        env_btn_layout.addWidget(self.btn_add_auth)
        env_btn_layout.addStretch()

        self.env_container = QWidget()
        env_layout = QVBoxLayout(self.env_container)
        env_layout.setContentsMargins(0, 0, 0, 0)
        env_layout.addWidget(self.env_editor)
        env_layout.addLayout(env_btn_layout)

        self.lbl_args = QLabel("Arguments:")
        self.lbl_env = QLabel("Environment:")

        self.form_layout.addRow("Server ID:", self.inp_name)
        self.form_layout.addRow("Description:", self.desc_container)
        self.form_layout.addRow("Transport:", self.combo_type)
        self.form_layout.addRow("Command / URL:", self.inp_cmd_url)
        self.form_layout.addRow(self.lbl_args, self.inp_args)
        self.form_layout.addRow(self.lbl_env, self.env_container)

        if server_config:
            self.inp_desc.setText(server_config.get("description", ""))
            type_idx = {"stdio": 0, "sse": 1, "streamable_http": 2}
            self.combo_type.setCurrentIndex(type_idx.get(server_config.get("type", "stdio"), 0))

            if self.combo_type.currentIndex() == 0:  # stdio
                self.inp_cmd_url.setText(server_config.get("command", ""))
                self.inp_args.setText(", ".join(server_config.get("args", [])))
                dict_data = server_config.get("env", {})
            else:
                self.inp_cmd_url.setText(server_config.get("url", ""))
                dict_data = server_config.get("headers", {})

            if dict_data:
                param_list = [{"name": k, "type": "str", "value": str(v)} for k, v in dict_data.items()]
                self.env_editor.load_data(param_list)

        self.content_layout.addWidget(self.form_widget)

        self.btn_test = self.add_button("Test Connection", self._on_test_clicked)
        self.btn_test.setFixedSize(140, 32)
        self.footer_layout.removeWidget(self.btn_test)
        self.footer_layout.insertWidget(0, self.btn_test)

        self.add_button("Cancel", self.reject)
        self.add_button("Save", self.accept, is_primary=True)

        self.combo_type.currentIndexChanged.connect(self._on_type_changed)
        self._on_type_changed()

        self._apply_theme()
        self.adjustSize()

    def _on_test_clicked(self):
        """测试连接（使用标准 TaskManager 管理）"""
        name, cfg = self.get_config()
        if not name or (not cfg.get("command") and not cfg.get("url")):
            StandardDialog(self, "Missing Info", "Please enter at least a Server ID and Command/URL.").exec()
            return

        # 1. 取消现有任务
        if self.task_mgr is not None:
            self.task_mgr.cancel_task()

        self.btn_test.setEnabled(False)
        self.pd = ProgressDialog(self, "Testing Connection", f"Connecting to [{name}]...\nPlease wait...")
        self.pd.show()

        self.task_mgr = TaskManager()
        self.task_mgr.sig_progress.connect(self.pd.update_progress)
        self.task_mgr.sig_result.connect(self._on_test_finished)
        self.pd.sig_canceled.connect(self.task_mgr.cancel_task)

        # 3. 启动任务
        self.task_mgr.start_task(
            TestMcpConnectionTask,
            task_id="test_mcp_conn",
            mode=TaskMode.THREAD,
            server_name=name,
            config=cfg
        )

    def _on_test_finished(self, result):
        """测试完成统一回调"""
        self.btn_test.setEnabled(True)

        if not hasattr(self, 'pd') or not self.pd:
            return

        success = result.get("success", False)
        msg = result.get("msg", "Unknown result")

        if success:
            self.pd.show_success_state("Connection Successful", msg)
        else:
            self.pd.show_finish_state(False, "Connection Failed", f"Unable to connect to server:\n{msg}")


    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm
        self.form_widget.setStyleSheet(f"""
            QLabel {{ color: {tm.color('text_muted')}; font-size: 13px; border: none; }} 
        """)

        self.lbl_desc_icon.setPixmap(tm.icon("help", "warning").pixmap(14, 14))
        self.lbl_desc_text.setStyleSheet(
            f"color: {tm.color('text_muted')}; font-size: 11.5px; font-style: italic; border: none; background: transparent;")
        self.desc_hint_widget.setStyleSheet("background: transparent;")

        self.btn_add_auth.setStyleSheet(
            f"color: {tm.color('warning')}; font-weight: bold; background: transparent; border: none;")
        self.btn_add_env.setStyleSheet(f"color: {tm.color('text_main')}; background: transparent; border: none;")
        self.btn_test.setStyleSheet(
            f"QPushButton {{ background-color: {tm.color('btn_bg')}; color: {tm.color('warning')}; border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 5px 10px; }} QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}")

    def _on_type_changed(self):
        stype = self.combo_type.currentText()
        is_stdio = (stype == "stdio")

        self.inp_args.setVisible(is_stdio)
        self.lbl_args.setVisible(is_stdio)
        self.btn_add_auth.setVisible(not is_stdio)

        if is_stdio:
            self.inp_cmd_url.setPlaceholderText("e.g. python, npx, node")
            self.lbl_env.setText("Environment:")
        elif stype == "sse":
            self.inp_cmd_url.setPlaceholderText("e.g. http://domain.com/sse")
            self.lbl_env.setText("HTTP Headers:")

    def _add_auth_header(self):
        current_data = self.env_editor.extract_data()
        for p in current_data:
            if p.get("name") == "Authorization": return
        current_data.append({"name": "Authorization", "type": "str", "value": "Bearer "})
        self.env_editor.blockSignals(True)
        self.env_editor.load_data(current_data)
        self.env_editor.blockSignals(False)
        self.adjustSize()

    def get_config(self):
        name = self.inp_name.text().strip()
        stype = self.combo_type.currentText()
        cfg = {"type": stype, "description": self.inp_desc.text().strip()}

        raw_params = self.env_editor.extract_data()
        env_dict = {p["name"].strip(): str(p.get("value", "")) for p in raw_params if p.get("name", "").strip()}

        if stype == "stdio":
            cfg["command"] = self.inp_cmd_url.text().strip()
            args_raw = self.inp_args.text().strip()
            cfg["args"] = [a.strip() for a in args_raw.split(",") if a.strip()]
            if env_dict: cfg["env"] = env_dict
        else:
            cfg["url"] = self.inp_cmd_url.text().strip()
            if env_dict: cfg["headers"] = env_dict

        return name, cfg


class SkillConfigDialog(BaseDialog):
    def __init__(self, parent=None, skill_name="", script_path="", description=""):
        super().__init__(parent, title="Native Skill Configuration", width=550)

        tm = ThemeManager()
        self.tm = tm

        self.input_name = QLineEdit(skill_name)
        self.input_name.setPlaceholderText("e.g., fetch_arxiv_summary")
        self.input_name.setMinimumHeight(32)

        self.input_path = QLineEdit(script_path)
        self.input_path.setPlaceholderText("Select a Python (.py) script...")
        self.input_path.setMinimumHeight(32)

        self.desc_container = QWidget()
        desc_v_layout = QVBoxLayout(self.desc_container)
        desc_v_layout.setContentsMargins(0, 0, 0, 0)
        desc_v_layout.setSpacing(4)

        self.input_desc = QLineEdit(description)
        self.input_desc.setPlaceholderText("Describe the tool's purpose (English only)...")  # 修改占位符，与 MCP 保持一致
        self.input_desc.setMinimumHeight(32)

        desc_regex = QRegularExpression(r"^[\x20-\x7E]*$")
        desc_validator = QRegularExpressionValidator(desc_regex, self.input_desc)
        self.input_desc.setValidator(desc_validator)

        self.desc_hint_widget = QWidget()
        hint_layout = QHBoxLayout(self.desc_hint_widget)
        hint_layout.setContentsMargins(0, 0, 0, 0)
        hint_layout.setSpacing(6)

        self.lbl_desc_icon = QLabel()
        self.lbl_desc_icon.setFixedSize(14, 14)

        self.lbl_desc_text = QLabel(
            "<b>Crucial for AI:</b> Clearly describe the tool's purpose in English so the AI knows exactly when to use it.")
        self.lbl_desc_text.setWordWrap(True)
        self.lbl_desc_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        hint_layout.addWidget(self.lbl_desc_icon, 0, Qt.AlignTop)
        hint_layout.addWidget(self.lbl_desc_text, 1)

        desc_v_layout.addWidget(self.input_desc)
        desc_v_layout.addWidget(self.desc_hint_widget)

        self.btn_browse = QPushButton()
        self.btn_browse.setIcon(tm.icon("folder", "accent"))
        self.btn_browse.setToolTip("Browse and select a .py script...")
        self.btn_browse.setCursor(Qt.PointingHandCursor)
        self.btn_browse.setStyleSheet("background: transparent; border: none; padding: 4px;")

        self.btn_clear = QPushButton()
        self.btn_clear.setIcon(tm.icon("delete", "danger"))
        self.btn_clear.setToolTip("Clear selected path.")
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.setStyleSheet("background: transparent; border: none; padding: 4px;")

        path_layout = QHBoxLayout()
        path_layout.addWidget(self.input_path, stretch=1)
        path_layout.addWidget(self.btn_browse)
        path_layout.addWidget(self.btn_clear)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(4)

        form = QFormLayout()
        form.setSpacing(15)
        form.setLabelAlignment(Qt.AlignRight)

        form.addRow("Tool Name:", self.input_name)
        form.addRow("Description:", self.desc_container)
        form.addRow("Script Path:", path_layout)

        if hasattr(self, 'content_layout'):
            self.content_layout.addLayout(form)
        else:
            self.v_layout.insertLayout(0, form)

        self.add_button("Cancel", self.reject)
        self.btn_ok = self.add_button("Confirm", self._validate_and_accept, is_primary=True)

        self.btn_browse.clicked.connect(self._browse_file)
        self.btn_clear.clicked.connect(self.input_path.clear)

        self._apply_theme()
        self._apply_inputs_theme(tm)

    def _apply_theme(self):
        super()._apply_theme()
        tm = self.tm
        self.lbl_desc_icon.setPixmap(tm.icon("help", "warning").pixmap(14, 14))
        self.lbl_desc_text.setStyleSheet(
            f"color: {tm.color('text_muted')}; font-size: 11.5px; font-style: italic; border: none; background: transparent;")
        self.desc_hint_widget.setStyleSheet("background: transparent;")

    def _apply_inputs_theme(self, tm):
        input_style = f"""
            QLineEdit {{
                border: 1px solid {tm.color('border')};
                border-radius: 4px;
                padding: 6px 10px;
                background: {tm.color('bg_input')};
                color: {tm.color('text_main')};
            }}
            QLineEdit:focus {{
                border: 1px solid {tm.color('accent')};
            }}
        """
        self.input_name.setStyleSheet(input_style)
        self.input_path.setStyleSheet(input_style)
        self.input_desc.setStyleSheet(input_style)

    def _browse_file(self):
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(self, "Select Native Skill Script", "", "Python Scripts (*.py)")
        if path:
            self.input_path.setText(path)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    raw_code = f.read()
                tree = ast.parse(raw_code)
                for node in tree.body:
                    if isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name) and target.id == "SCHEMA":
                                schema_dict = ast.literal_eval(node.value)
                                func_info = schema_dict.get("function", {})

                                if func_info.get("name"):
                                    self.input_name.setText(func_info.get("name"))
                                if func_info.get("description"):
                                    self.input_desc.setText(func_info.get("description"))
            except Exception as e:
                pass

    def _validate_and_accept(self):
        from src.ui.components.toast import ToastManager
        if not self.input_name.text().strip():
            ToastManager().show("Please enter a Tool Name.", "warning")
            return
        if not self.input_desc.text().strip():
            ToastManager().show("Please enter a Tool Description.", "warning")
            return
        if not self.input_path.text().strip():
            ToastManager().show("Please select a Script Path.", "warning")
            return
        self.accept()

    def get_data(self):
        return self.input_name.text().strip(), self.input_desc.text().strip(), self.input_path.text().strip()


class SkillSecurityAnalyzer(ast.NodeVisitor):
    DANGEROUS_IMPORTS = {'os', 'sys', 'subprocess', 'shutil', 'socket', 'requests', 'urllib', 'http'}
    DANGEROUS_CALLS = {'eval', 'exec', 'open', '__import__'}

    def __init__(self):
        self.score = 100
        self.warnings = []

    def analyze(self, code_str: str) -> dict:
        self.score = 100
        self.warnings.clear()

        try:
            tree = ast.parse(code_str)
            self.visit(tree)
        except SyntaxError as e:
            return {"score": 0, "warnings": [f"Syntax Error: {e}"], "level": "Fatal"}

        level = "Safe"
        if self.score < 60:
            level = "High Risk"
        elif self.score < 80:
            level = "Medium Risk"

        return {"score": max(0, self.score), "warnings": self.warnings, "level": level}

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name.split('.')[0] in self.DANGEROUS_IMPORTS:
                self.score -= 20
                self.warnings.append(f"Dangerous import detected: '{alias.name}'")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module and node.module.split('.')[0] in self.DANGEROUS_IMPORTS:
            self.score -= 20
            self.warnings.append(f"Dangerous from...import detected: '{node.module}'")
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name):
            if node.func.id in self.DANGEROUS_CALLS:
                self.score -= 30
                self.warnings.append(f"Dangerous function call detected: '{node.func.id}'")
        self.generic_visit(node)


class PythonHighlighter(QSyntaxHighlighter):
    def __init__(self, document, theme_mgr):
        super().__init__(document)
        self.highlighting_rules = []

        keyword_format = QTextCharFormat()
        keyword_format.setForeground(QColor(theme_mgr.color("accent")))
        keyword_format.setFontWeight(QFont.Bold)
        keywords = [
            "def", "class", "import", "from", "return", "pass", "if", "elif", "else",
            "try", "except", "finally", "with", "as", "for", "while", "in", "and", "or", "not"
        ]
        for word in keywords:
            pattern = QRegularExpression(rf"\b{word}\b")
            self.highlighting_rules.append((pattern, keyword_format))

        string_format = QTextCharFormat()
        string_format.setForeground(QColor(theme_mgr.color("success")))
        self.highlighting_rules.append((QRegularExpression("\".*\""), string_format))
        self.highlighting_rules.append((QRegularExpression("'.*'"), string_format))

    def highlightBlock(self, text):
        for pattern, format in self.highlighting_rules:
            iterator = pattern.globalMatch(text)
            while iterator.hasNext():
                match = iterator.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), format)


class SkillPreviewDialog(BaseDialog):
    def __init__(self, parent=None, skill_name="", code_content="", is_importing=True):
        title = f"Skill Review: {skill_name}" if is_importing else f"Edit Skill: {skill_name}"
        super().__init__(parent, title=title, width=750)
        self.setMinimumHeight(550)

        self.code_content = code_content
        self.is_importing = is_importing

        # 1. 静态安全分析
        analyzer = SkillSecurityAnalyzer()
        report = analyzer.analyze(code_content)

        # 2. 渲染顶部安全评分提示
        lbl_info = QLabel(f"<b>Security Score: {report['score']}/100 ({report['level']})</b>")
        if report['score'] < 60:
            lbl_info.setStyleSheet(f"color: {self.tm.color('danger')}; font-size: 14px;")
        elif report['score'] < 80:
            lbl_info.setStyleSheet(f"color: {self.tm.color('warning')}; font-size: 14px;")
        else:
            lbl_info.setStyleSheet(f"color: {self.tm.color('success')}; font-size: 14px;")
        self.content_layout.addWidget(lbl_info)

        if report['warnings']:
            lbl_warnings = QLabel("Warnings:\n- " + "\n- ".join(report['warnings']))
            lbl_warnings.setStyleSheet(f"color: {self.tm.color('warning')}; font-size: 12px;")
            self.content_layout.addWidget(lbl_warnings)

        # 3. 渲染代码高亮编辑器（专属源码控件：边框底纹、独立滚动条、复制/折叠、深色模式）
        self.editor = SourceCodeViewer(
            title="Skill Source Code (.py)",
            editable=True,
            collapsed=False,
            max_height=600,
        )
        self.editor.set_code(code_content)
        self.highlighter = PythonHighlighter(self.editor.document(), self.tm)

        self.content_layout.addWidget(self.editor, stretch=1)

        self.add_button("Cancel", self.reject)
        btn_text = "Confirm Import" if is_importing else "Save Changes"
        self.add_button(btn_text, self.accept, is_primary=True)

        self._apply_theme()

    def get_edited_code(self):
        return self.editor.code()
