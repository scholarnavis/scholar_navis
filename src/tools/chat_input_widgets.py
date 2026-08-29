"""Chat input area widgets.

拆分自 src/tools/chat_tool.py：拖拽上传容器、自动伸缩输入框、
底部输入容器（含 Agent 开关与工具过滤菜单）。
"""

import logging

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout,
                               QPlainTextEdit, QPushButton, QLabel, QFrame,
                               QMenu, QCheckBox, QToolButton, QWidgetAction,
                               QSizePolicy, QApplication)

from src.core.config_manager import ConfigManager
from src.core.mcp_manager import MCPManager
from src.core.signals import GlobalSignals
from src.core.skill_manager import SkillManager
from src.core.theme_manager import ThemeManager
from src.ui.components.chat_bubble import hex_to_rgba
from src.ui.components.toast import ToastManager


class ChatDropTargetWidget(QWidget):
    """支持全局拖拽上传文件的容器，并带有视觉叠加层"""
    sig_files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.config = ConfigManager()

        self.overlay = QLabel("Drop files here to attach", self)
        self.overlay.setAlignment(Qt.AlignCenter)
        self.overlay.setStyleSheet("""
            background-color: rgba(5, 184, 204, 0.85); 
            color: white; 
            font-size: 28px; 
            font-weight: bold; 
            border-radius: 12px;
            border: 4px dashed rgba(255, 255, 255, 0.5);
        """)
        self.overlay.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 确保叠加层始终覆盖整个组件
        self.overlay.resize(self.size())

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.overlay.show()
            self.overlay.raise_()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.overlay.hide()
        super().dragLeaveEvent(event)


    def dropEvent(self, event):
        self.overlay.hide()

        #supported_exts = ('.pdf', '.md', '.txt', '.csv', '.docx', '.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif')
        supported_exts = ('.pdf', '.md', '.txt', '.docx')
        paths = [
            url.toLocalFile() for url in event.mimeData().urls()
            if url.isLocalFile() and url.toLocalFile().lower().endswith(supported_exts)
        ]

        if paths:
            self.sig_files_dropped.emit(paths)
        else:
            ToastManager().show("Unsupported file format.", "warning")

        event.acceptProposedAction()


class AutoResizingTextEdit(QPlainTextEdit):
    sig_send = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("Ask a question... (Recommend English or enabling translator for best results. Enter to send, Shift+Enter for new line)")
        self.setStyleSheet("""
            QPlainTextEdit { background-color: transparent; border: none; font-size: 14px; }
        """)

        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        fm = self.fontMetrics()
        line_h = fm.lineSpacing()
        doc_margins = self.document().documentMargin()
        base_padding = self.contentsMargins().top() + self.contentsMargins().bottom() + int(doc_margins * 2)

        fixed_height = int((line_h * 5) + base_padding + 2)
        self.setFixedHeight(fixed_height)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Return and not event.modifiers() & Qt.ShiftModifier:
            self.sig_send.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class ChatInputContainer(QFrame):
    sig_send_clicked = Signal(str)
    sig_export_clicked = Signal()
    sig_clear_clicked = Signal()
    sig_attach_clicked = Signal()
    sig_clear_context_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.config = ConfigManager()
        self.logger = logging.getLogger("ChatInputContainer")
        self.setObjectName("ChatInputContainer")
        self.setStyleSheet("""
            QFrame#ChatInputContainer {
                background-color: #2b2b2b;
                border: 1px solid #444;
                border-radius: 8px;
            }
        """)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(5)

        self.text_edit = AutoResizingTextEdit()
        main_layout.addWidget(self.text_edit)

        self.context_banner = QWidget()
        self.context_banner.setVisible(False)
        self.context_banner.setStyleSheet(
            "background-color: rgba(5, 184, 204, 0.1); border: 1px solid #05B8CC; border-radius: 4px;")
        banner_layout = QHBoxLayout(self.context_banner)
        banner_layout.setContentsMargins(8, 4, 8, 4)

        self.lbl_context_icon = QLabel()
        self.lbl_context_info = QLabel("Context Attached")

        self.lbl_context_info.setStyleSheet("color: #05B8CC; font-size: 12px; border: none;")

        self.btn_clear_context = QPushButton("")
        self.btn_clear_context.setCursor(Qt.PointingHandCursor)
        self.btn_clear_context.clicked.connect(self.sig_clear_context_clicked.emit)

        banner_layout.addWidget(self.lbl_context_icon)
        banner_layout.addWidget(self.lbl_context_info)
        banner_layout.addStretch()
        banner_layout.addWidget(self.btn_clear_context)
        main_layout.addWidget(self.context_banner)

        self.mcp_toolbar = QHBoxLayout()
        use_academic = self.config.user_settings.get("chat_use_academic_agent", True)
        use_external = self.config.user_settings.get("chat_use_external_tools", False)

        # 1. 学术 Agent 开关
        self.chk_academic_agent = QCheckBox("Academic Agent")
        self.chk_academic_agent.setStyleSheet("color: #05B8CC; font-weight: bold;")
        self.chk_academic_agent.setChecked(use_academic)
        self.chk_academic_agent.setToolTip("Enable built-in native academic skills (Zero Latency)")
        self.chk_academic_agent.toggled.connect(lambda c: self._save_agent_state("chat_use_academic_agent", c))

        # 2. 外部 Tools 开关
        self.chk_external_tools = QCheckBox("External Tools")
        self.chk_external_tools.setStyleSheet("color: #05B8CC; font-weight: bold;")
        self.chk_external_tools.setChecked(use_external)
        self.chk_external_tools.setToolTip("Enable external MCP servers and custom Python scripts")
        self.chk_external_tools.toggled.connect(lambda c: self._save_agent_state("chat_use_external_tools", c))

        # 3. 深度研究开关：分解为并行子任务，分节汇总（默认关闭）
        use_deep = self.config.user_settings.get("agent_deep_mode", False)
        self.chk_deep_mode = QCheckBox("Deep Mode")
        self.chk_deep_mode.setStyleSheet("color: #05B8CC; font-weight: bold;")
        self.chk_deep_mode.setChecked(use_deep)
        self.chk_deep_mode.setToolTip(
            "Decompose the query into parallel sub-investigations, then synthesize "
            "a section-by-section answer (higher cost, deeper coverage)")
        self.chk_deep_mode.toggled.connect(lambda c: self._save_agent_state("agent_deep_mode", c))

        self.btn_mcp_tags = QToolButton()
        self.btn_mcp_tags = QPushButton("Tools Filter", self)
        self.btn_mcp_tags.setIcon(ThemeManager().icon("filter", "text_muted"))
        self.btn_mcp_tags.setCursor(Qt.PointingHandCursor)
        self.btn_mcp_tags.setStyleSheet(
            "QPushButton { color: #aaaaaa; background: transparent; border: 1px solid #555; border-radius: 4px; padding: 2px 8px; }"
            "QPushButton:hover { background: #333; }"
        )

        self.menu_mcp_tags = QMenu(self)
        self.menu_mcp_tags.setStyleSheet(
            "QMenu { background-color: #2b2b2b; border: 1px solid #555; border-radius: 6px; padding: 4px; }"
        )
        self.btn_mcp_tags.clicked.connect(self._show_filter_menu)

        self.lbl_tool_hint = QLabel(" (Tip: Selecting fewer tools improves accuracy)")

        self.tag_actions = {}
        self.user_deselected_tags = set()
        self.known_tags = set()

        self.mcp_toolbar.addWidget(self.chk_academic_agent)
        self.mcp_toolbar.addWidget(self.chk_external_tools)
        self.mcp_toolbar.addWidget(self.chk_deep_mode)
        self.mcp_toolbar.addWidget(self.btn_mcp_tags)
        self.mcp_toolbar.addWidget(self.lbl_tool_hint)  # 新增：将标签加入水平布局
        self.mcp_toolbar.addStretch()
        main_layout.insertLayout(1, self.mcp_toolbar)

        self.chk_external_tools.toggled.connect(self._on_external_tools_toggled)

        self.bottom_bar = QHBoxLayout()
        self.bottom_bar.setContentsMargins(0, 0, 0, 0)

        tool_btn_style = f"""
                    QPushButton {{ background-color: transparent; color: #888888; border: 1px solid transparent; border-radius: 4px; padding: 4px 10px; font-family: {ThemeManager().font_family()}; font-size: 13px;}}
                    QPushButton:hover {{ background-color: #333333; border: 1px solid #555555; color: #ffffff;}}
                    QPushButton:pressed {{ background-color: #222222; }}
                """

        self.btn_export = QPushButton("Export")
        self.btn_export.setCursor(Qt.PointingHandCursor)
        self.btn_export.setStyleSheet(tool_btn_style)
        self.btn_export.clicked.connect(self.sig_export_clicked.emit)

        self.btn_clear = QPushButton("Clear")
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.setStyleSheet(tool_btn_style)
        self.btn_clear.clicked.connect(self.sig_clear_clicked.emit)

        self.btn_attach = QPushButton("Attach")
        self.btn_attach.setCursor(Qt.PointingHandCursor)
        self.btn_attach.setStyleSheet(tool_btn_style)
        self.btn_attach.clicked.connect(self.sig_attach_clicked.emit)
        self.bottom_bar.insertWidget(0, self.btn_attach)

        self.bottom_bar.addWidget(self.btn_export)
        self.bottom_bar.addWidget(self.btn_clear)
        self.bottom_bar.addStretch()

        self.btn_send = QPushButton("Send")
        self.btn_send.setCursor(Qt.PointingHandCursor)
        self.btn_send.setFixedSize(90, 32)  # 加宽以防止文字截断
        self.btn_send.setStyleSheet(f"""
                           QPushButton {{ 
                               background-color: #007acc; color: white; border-radius: 6px; 
                               font-weight: bold; font-family: {ThemeManager().font_family()};
                           }}
                           QPushButton:hover {{ background-color: #0062a3; }}
                       """)
        self.bottom_bar.addWidget(self.btn_send)

        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setCursor(Qt.PointingHandCursor)
        self.btn_stop.setFixedSize(90, 32)
        self.btn_stop.setVisible(False)
        self.bottom_bar.addWidget(self.btn_stop)

        main_layout.addLayout(self.bottom_bar)

        self.btn_send.clicked.connect(self._emit_send)
        self.text_edit.sig_send.connect(self._emit_send)

        GlobalSignals().mcp_status_changed.connect(self._on_mcp_status_changed)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()
        QTimer.singleShot(100, self.refresh_mcp)
        if self.chk_external_tools.isChecked():
            self.refresh_mcp()

    def _save_agent_state(self, key, checked):
        self.config.user_settings[key] = checked
        self.config.save_settings()


    def _apply_theme(self):
        tm = ThemeManager()
        self.setStyleSheet(
            f"QFrame#ChatInputContainer {{ background-color: {tm.color('bg_card')}; border: 1px solid {tm.color('border')}; border-radius: 8px; }}")

        self.text_edit.setStyleSheet(f"""
                    QPlainTextEdit {{ 
                        background-color: transparent; 
                        color: {tm.color('text_main')}; 
                        border: none; 
                        font-size: 14px; 
                        font-family: {tm.font_family()}; 
                    }}
                    QScrollBar:vertical {{ 
                        background: transparent; 
                        width: 6px; 
                        margin: 0px;
                    }}
                    QScrollBar::handle:vertical {{ 
                        background: {hex_to_rgba(tm.color('text_muted'), 0.4) if 'hex_to_rgba' in globals() else 'rgba(150, 150, 150, 0.35)'}; 
                        border-radius: 3px; 
                        min-height: 20px;
                    }}
                    QScrollBar::handle:vertical:hover {{ 
                        background: {tm.color('accent')}; 
                    }}
                    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                        height: 0px; 
                    }}
                """)


        tool_btn_style = f"""
                     QPushButton {{ background-color: transparent; color: {tm.color('text_muted')}; border: 1px solid transparent; border-radius: 4px; padding: 4px 10px; font-family: {tm.font_family()}; font-size: 13px; text-align: left; }}
                     QPushButton:hover {{ background-color: {tm.color('btn_hover')}; border: 1px solid {tm.color('border')}; color: {tm.color('text_main')};}}
                 """

        self.btn_export.setText("Export")
        self.btn_export.setIcon(tm.icon("download", "text_muted"))
        self.btn_export.setStyleSheet(tool_btn_style)

        self.btn_clear.setText("Clear")
        self.btn_clear.setIcon(tm.icon("delete", "text_muted"))
        self.btn_clear.setStyleSheet(tool_btn_style)

        self.btn_attach.setText("Attach")
        self.btn_attach.setIcon(tm.icon("link", "text_muted"))
        self.btn_attach.setStyleSheet(tool_btn_style)

        if hasattr(self, 'lbl_hardware_status'):
            self.lbl_hardware_status.setStyleSheet(
                f"color: {tm.color('text_muted')}; font-size: 11px; font-weight: bold; padding-left: 4px;"
            )

        if hasattr(self, 'btn_ribbon_state'):
            self.btn_ribbon_state.setStyleSheet(f"""
                        QPushButton {{ background: transparent; border: 1px solid {tm.color('border')}; border-radius: 4px; color: {tm.color('text_muted')}; font-size: 11px; padding: 2px 6px; text-align: left;}}
                        QPushButton:hover {{ background: {tm.color('btn_hover')}; color: {tm.color('text_main')}; }}
                    """)

            state_icons = {
                "Pinned": "keep",
                "Hover": "menu",
                "Collapsed": "down"
            }
            if hasattr(self, 'ribbon_state') and self.ribbon_state in state_icons:
                self.btn_ribbon_state.setIcon(tm.icon(state_icons[self.ribbon_state], "text_muted"))

        if hasattr(self, 'lbl_context_icon'):
            self.lbl_context_icon.setPixmap(tm.icon("link", "accent").pixmap(14, 14))
            self.lbl_context_info.setStyleSheet(f"color: {tm.color('accent')}; font-size: 12px; border: none;")

        self.btn_clear_context.setIcon(tm.icon("close", "danger"))
        self.btn_clear_context.setToolTip("Clear all attached contexts")
        self.btn_clear_context.setStyleSheet(f"""
                    QPushButton {{ border: none; background: transparent; padding: 4px; border-radius: 4px; }} 
                    QPushButton:hover {{ background: {hex_to_rgba(tm.color('danger'), 0.2)}; }}
                """)

        btn_mcp_style = f"""
                    QPushButton {{ color: {tm.color('text_muted')}; background: transparent; border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 4px 8px; font-size: 12px; }}
                    QPushButton:hover {{ background: {tm.color('btn_hover')}; color: {tm.color('text_main')}; }}
                """
        self.btn_mcp_tags.setIcon(tm.icon("filter", "text_muted"))
        self.btn_mcp_tags.setStyleSheet(btn_mcp_style)

        if hasattr(self, 'lbl_tool_hint'):
            self.lbl_tool_hint.setStyleSheet(
                f"color: {tm.color('text_muted')}; font-size: 11px; font-style: italic; border: none;")

        self.btn_send.setIcon(tm.icon("send", "bg_main"))
        self.btn_send.setStyleSheet(f"""
                            QPushButton {{ background-color: {tm.color('academic_blue')}; color: #ffffff; border-radius: 6px; font-weight: bold; font-family: {tm.font_family()}; }}
                            QPushButton:hover {{ background-color: {tm.color('academic_blue_hover')}; }}
                        """)

        self.btn_stop.setIcon(tm.icon("close", "bg_main"))
        self.btn_stop.setStyleSheet(f"""
                    QPushButton {{ background-color: {tm.color('danger')}; color: {tm.color('bg_main')}; border-radius: 6px; font-weight: bold; font-family: {tm.font_family()}; }}
                    QPushButton:hover {{ background-color: rgba(255, 107, 107, 0.8); }}
                """)

        menu_style = f"""
                    QMenu {{ background-color: {tm.color('bg_card')}; border: 1px solid {tm.color('border')}; border-radius: 6px; padding: 4px; }}
                    QMenu::item {{ padding: 6px 12px; margin: 2px 0px; color: {tm.color('text_main')}; border-radius: 4px; }}
                    QMenu::item:selected {{ background-color: {tm.color('accent')}; color: #ffffff; }}
                    QMenu QCheckBox {{ color: {tm.color('text_main')}; background-color: transparent; padding: 6px 12px; font-size: 13px; border-radius: 4px; }}
                    QMenu QCheckBox:hover {{ background-color: {tm.color('accent')}; color: #ffffff; }}
                """
        self.menu_mcp_tags.setStyleSheet(menu_style)

    def set_uploading(self, is_uploading: bool):
        self.btn_send.setEnabled(not is_uploading)
        self.btn_attach.setEnabled(not is_uploading)
        if is_uploading:
            self.btn_send.setToolTip("Please wait for file upload to complete...")
            self.btn_send.setStyleSheet(
                self.btn_send.styleSheet() + "QPushButton:disabled { background-color: #555; color: #888; }")
        else:
            self.btn_send.setToolTip("")

    def _on_mcp_status_changed(self):
        if hasattr(self, 'chk_external_tools') and self.chk_external_tools.isChecked():
            self.refresh_mcp()
        elif hasattr(self, 'chk_mcp_enable') and self.chk_mcp_enable.isChecked():
            self.refresh_mcp()

    def _on_tag_toggled(self, tag, checked):
        if hasattr(self.config, 'toggle_mcp_tag'):
            self.config.toggle_mcp_tag(tag, checked)
        else:
            deselected = self.config.mcp_servers.get("deselected_mcp_tags", [])
            if checked and tag in deselected:
                deselected.remove(tag)
            elif not checked and tag not in deselected:
                deselected.append(tag)
            self.config.mcp_servers["deselected_mcp_tags"] = deselected

            if hasattr(self.config, 'save_mcp_servers'):
                self.config.save_mcp_servers()
            else:
                self.config.save_settings()

        self._update_tag_button_text()

    def _on_external_tools_toggled(self, checked):
            self.refresh_mcp()

    def _show_filter_menu(self):
        self.btn_mcp_tags.setText("Tools Filter: Fetching...")
        QApplication.processEvents()

        self.refresh_mcp()

        pos = self.btn_mcp_tags.mapToGlobal(self.btn_mcp_tags.rect().topLeft())
        menu_height = self.menu_mcp_tags.sizeHint().height()
        pos.setY(pos.y() - menu_height - 4)

        self.menu_mcp_tags.popup(pos)

    def get_all_available_tags(self) -> list:
        """Fetch and aggregate tags from both MCP servers and internal/external SkillManagers."""
        tags = set()
        try:
            skill_mgr = SkillManager.get_instance()
            import re

            # 1. Fetch Academic Skills with [ACADEMIC] prefix
            for schema in skill_mgr.academic_schemas.values():
                desc = schema.get("function", {}).get("description", "")
                match = re.search(r"\[Tags:\s*(.*?)\]", desc)
                if match:
                    for t in match.group(1).split(","):
                        tags.add(f"[ACADEMIC] {t.strip().title()}")

            # 2. Fetch External Skills with [External] prefix
            for schema in skill_mgr.external_schemas.values():
                name = schema.get("function", {}).get("name", "Unknown")
                tags.add(f"[External] {name}")

            # 3. Fetch external MCP Server tags with [External] prefix
            mcp_mgr = MCPManager.get_instance()
            for server in mcp_mgr.get_available_mcp():
                tags.add(f"[External] {server}")

        except Exception as e:
            self.logger.error(f"Failed to fetch combined tags from SkillManager and MCPManager: {e}", exc_info=True)

        return sorted(list(tags))


    def refresh_mcp(self):
        try:
            available_tags = self.get_all_available_tags()
            deselected_tags = self.config.mcp_servers.get("deselected_mcp_tags", [])

            self.menu_mcp_tags.clear()
            self.tag_actions.clear()
            self.known_tags.clear()

            if not available_tags:
                self.btn_mcp_tags.setText("🏷️ Tools Filter: None")
                from PySide6.QtGui import QAction
                dummy = QAction("⏳ No active skills or MCP servers...", self)
                dummy.setEnabled(False)
                self.menu_mcp_tags.addAction(dummy)
                return

            class MenuContainerWidget(QWidget):
                def mousePressEvent(self, event):
                    event.accept()

                def mouseReleaseEvent(self, event):
                    event.accept()

            self.menu_container = MenuContainerWidget()
            self.menu_layout = QVBoxLayout(self.menu_container)
            self.menu_layout.setContentsMargins(6, 6, 6, 6)
            self.menu_layout.setSpacing(4)

            for tag in available_tags:
                chk = QCheckBox(f"  {tag}")
                chk.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                chk.setChecked(tag not in deselected_tags)
                chk.setCursor(Qt.PointingHandCursor)
                chk.toggled.connect(lambda checked, t=tag: self._on_tag_toggled(t, checked))

                self.menu_layout.addWidget(chk)
                self.tag_actions[tag] = chk
                self.known_tags.add(tag)

            wa = QWidgetAction(self)
            wa.setDefaultWidget(self.menu_container)
            self.menu_mcp_tags.addAction(wa)

            self._update_tag_button_text()

        except Exception as e:
            self.logger.error(f"Error refreshing skill and tool tags: {e}", exc_info=True)
            self.btn_mcp_tags.setText("Tools Filter: Error")

    def _update_tag_button_text(self):
        selected = self.get_selected_tags()
        total = len(self.tag_actions)
        if total == 0:
            self.btn_mcp_tags.setText("Tools Filter: None")
        elif len(selected) == total:
            self.btn_mcp_tags.setText("Tools Filter: All")
        else:
            self.btn_mcp_tags.setText(f"Tools Filter: {len(selected)} selected")

    def get_selected_tags(self) -> list:
        try:
            available = self.get_all_available_tags()
            deselected = self.config.mcp_servers.get("deselected_mcp_tags", [])
            return [t for t in available if t not in deselected]
        except Exception as e:
            self.logger.error(f"Failed to retrieve selected user tags: {e}")
            return []

    def _emit_send(self):
        if not self.btn_send.isEnabled():
            return
        text = self.text_edit.toPlainText().strip()
        if text: self.sig_send_clicked.emit(text)

    def clear_text(self):
        self.text_edit.clear()
        self.text_edit.setFocus()

    def set_text(self, text):
        self.text_edit.setPlainText(text)
        self.text_edit.setFocus()

    def lock_input(self):
        self.text_edit.setPlaceholderText("Knowledge base updated. Clear history to resume chat.")
        tip = "The linked knowledge base or model has changed. Continuing may cause context inconsistency. Please click 'Clear' to reset history."
        self.text_edit.setToolTip(tip)
        self.btn_send.setToolTip(tip)

    def unlock_input(self):
        self.text_edit.setEnabled(True)
        self.text_edit.setPlaceholderText(
            "Ask a question... (Recommend English or enabling translator for best results. Enter to send, Shift+Enter for new line)")
        self.btn_send.setEnabled(True)

    def show_context_preview(self, text_info):
        """显示输入框上方的附件预览条"""
        self.lbl_context_info.setText(f"📎 Attached: {text_info}")
        self.context_banner.setVisible(True)

    def hide_context_preview(self):
        """隐藏输入框上方的附件预览条"""
        self.context_banner.setVisible(False)
        self.lbl_context_info.setText("📎 Context Attached")
