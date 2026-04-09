import base64
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from urllib.parse import urlparse, parse_qs, quote

from PySide6.QtCore import Qt, Signal, QUrl, QTimer, QPropertyAnimation, QEasingCurve, \
    QEvent
from PySide6.QtGui import QDesktopServices, QCursor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout,
                               QPlainTextEdit, QPushButton, QLabel,
                               QScrollArea, QFrame, QFileDialog, QMenu, QCheckBox,
                               QToolButton, QWidgetAction, QSizePolicy, QGraphicsOpacityEffect, QApplication)

from src.core.config_manager import ConfigManager
from src.core.core_task import TaskManager, TaskMode, TaskState
from src.core.kb_manager import KBManager, DatabaseManager
from src.core.mcp_manager import MCPManager
from src.core.models_registry import get_model_conf
from src.core.signals import GlobalSignals
from src.core.skill_manager import SkillManager
from src.core.theme_manager import ThemeManager
from src.task.chat_tasks import ProcessAttachmentTask, ChatGenerationTask
from src.tools.base_tool import BaseTool
from src.tools.settings_tool import FloatingOverlayFilter
from src.ui.components.chat_bubble import ChatBubbleWidget, hex_to_rgba
from src.ui.components.combo import BaseComboBox
from src.ui.components.dialog import StandardDialog, SelectKBFileDialog
from src.ui.components.mermaid_viewer import MermaidViewer
from src.ui.components.model_selector import ModelSelectorWidget
from src.ui.components.pdf_viewer import InternalPDFViewer
from src.ui.components.pill_button import FollowUpGroupWidget
from src.ui.components.text_formatter import TextFormatter
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
        self.setPlaceholderText("Ask a question... (Enter to send, Shift+Enter for new line)")
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

        self.btn_mcp_tags = QToolButton()
        self.btn_mcp_tags = QPushButton("Filter Tools", self)
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

        self.tag_actions = {}
        self.user_deselected_tags = set()
        self.known_tags = set()

        self.mcp_toolbar.addWidget(self.chk_academic_agent)
        self.mcp_toolbar.addWidget(self.chk_external_tools)
        self.mcp_toolbar.addWidget(self.btn_mcp_tags)
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
        self.btn_mcp_tags.setVisible(checked)
        if checked:
            self.refresh_mcp()

    def _show_filter_menu(self):
        self.btn_mcp_tags.setText("Filter Tools: Fetching...")
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
                self.btn_mcp_tags.setText("🏷️ Filter Tools: None")
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
            self.btn_mcp_tags.setText("Filter Tools: Error")

    def _update_tag_button_text(self):
        selected = self.get_selected_tags()
        total = len(self.tag_actions)
        if total == 0:
            self.btn_mcp_tags.setText("Filter Tools: None")
        elif len(selected) == total:
            self.btn_mcp_tags.setText("Filter Tools: All")
        else:
            self.btn_mcp_tags.setText(f"Filter Tools: {len(selected)} selected")

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
        self.text_edit.setPlaceholderText("Ask a question... (Enter to send, Shift+Enter for new line)")
        self.btn_send.setEnabled(True)

    def show_context_preview(self, text_info):
        """显示输入框上方的附件预览条"""
        self.lbl_context_info.setText(f"📎 Attached: {text_info}")
        self.context_banner.setVisible(True)

    def hide_context_preview(self):
        """隐藏输入框上方的附件预览条"""
        self.context_banner.setVisible(False)
        self.lbl_context_info.setText("📎 Context Attached")



class ChatTool(BaseTool):
    def __init__(self):
        super().__init__("Chat Assistant")
        self.history = []
        self.widget = None
        self.worker_thread = None
        self.kb_manager = KBManager()
        self.current_ai_text = ""
        self.current_ai_bubble = None
        self.pdf_viewer = None
        self.expanded_thinks = set()
        self.user_toggled_thinks = set()
        self.external_context_buffer = ""
        self.external_context_html = ""

        GlobalSignals().kb_list_changed.connect(self.refresh_kb_list)
        GlobalSignals().kb_switched.connect(self.on_global_kb_switched)
        GlobalSignals().kb_modified.connect(self.on_kb_modified)

        if hasattr(GlobalSignals(), 'llm_config_changed'):
            GlobalSignals().llm_config_changed.connect(self.load_llm_configs)


    def get_ui_widget(self) -> QWidget:
        if self.widget: return self.widget

        # 1. Main Container & Global Layout
        self.widget = ChatDropTargetWidget()
        self.widget.sig_files_dropped.connect(self.process_attached_files)

        main_layout = QVBoxLayout(self.widget)
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(10, 10, 10, 10)

        # --- Ribbon UI Implementation ---
        self.top_bar_wrapper = QWidget()
        self.top_bar_wrapper.setObjectName("TopBarWrapper")
        top_bar = QVBoxLayout(self.top_bar_wrapper)
        top_bar.setSpacing(8)
        top_bar.setContentsMargins(0, 0, 0, 10)

        row1_layout = QHBoxLayout()
        self.model_selector = ModelSelectorWidget(label_text=" Main Model:", config_key="chat_llm_id",
                                                  model_key="chat_model_name", enable_vision=False)

        self.collapsed_placeholder = QLabel(" ")
        self.collapsed_placeholder.setVisible(False)

        row1_layout.addWidget(self.model_selector, 1)
        row1_layout.addWidget(self.collapsed_placeholder, 1)

        # 硬件状态显示标签
        self.lbl_hardware_status = QLabel()
        self._update_hardware_status()
        row1_layout.addWidget(self.lbl_hardware_status)

        # Pin/Toggle Button for Ribbon State
        tm = ThemeManager()
        self.btn_ribbon_state = QPushButton(" Pinned")
        self.btn_ribbon_state.setIcon(tm.icon("keep", "text_muted"))
        self.btn_ribbon_state.setCursor(Qt.PointingHandCursor)
        self.btn_ribbon_state.setFixedWidth(90)
        self.btn_ribbon_state.setStyleSheet("""
                            QPushButton { background: transparent; border: 1px solid #555; border-radius: 4px; color: #aaa; font-size: 11px; padding: 2px 6px; text-align: left;}
                            QPushButton:hover { background: #333; color: #fff; }
                        """)
        row1_layout.addWidget(self.btn_ribbon_state)

        row2_layout = QHBoxLayout()
        self.trans_selector = ModelSelectorWidget(label_text=" Translator:", config_key="chat_trans_llm_id",
                                                  model_key="chat_trans_model_name", enable_vision=False)

        self.lbl_kb = QLabel(" Knowledge Base:")
        self.combo_kb = BaseComboBox(max_width=400)
        self.refresh_kb_list()

        row2_layout.addWidget(self.trans_selector)
        row2_layout.addSpacing(15)
        row2_layout.addWidget(self.lbl_kb)
        row2_layout.addWidget(self.combo_kb, 1)

        top_bar.addLayout(row1_layout)
        top_bar.addLayout(row2_layout)

        self.lbl_hardware_status = QLabel("Compute Device: Detecting...")
        top_bar.addWidget(self.lbl_hardware_status)
        self._update_hardware_status()

        main_layout.addWidget(self.top_bar_wrapper)

        self.ribbon_state = self.config.user_settings.get("chat_ribbon_state", "Pinned")

        def set_ribbon_visible(visible):
            self.model_selector.setVisible(visible)
            self.collapsed_placeholder.setVisible(not visible)
            self.trans_selector.setVisible(visible)
            self.lbl_kb.setVisible(visible)
            self.combo_kb.setVisible(visible)

            if hasattr(self, 'lbl_hardware_status'):
                self.lbl_hardware_status.setVisible(visible)

        def apply_ribbon_state(state):
            tm = ThemeManager()
            self.ribbon_state = state
            self.config.user_settings["chat_ribbon_state"] = state
            self.config.save_settings()

            if state == "Pinned":
                self.btn_ribbon_state.setText(" Pinned")
                self.btn_ribbon_state.setIcon(tm.icon("keep", "text_muted"))
                set_ribbon_visible(True)
            elif state == "Hover":
                self.btn_ribbon_state.setText(" Hover")
                self.btn_ribbon_state.setIcon(tm.icon("menu", "text_muted"))
                set_ribbon_visible(False)
            elif state == "Collapsed":
                self.btn_ribbon_state.setText(" Collapsed")
                self.btn_ribbon_state.setIcon(tm.icon("down", "text_muted"))
                set_ribbon_visible(False)

        def toggle_ribbon_state():
            if self.ribbon_state == "Pinned":
                apply_ribbon_state("Hover")
            elif self.ribbon_state == "Hover":
                apply_ribbon_state("Collapsed")
            else:
                apply_ribbon_state("Pinned")

        self.btn_ribbon_state.clicked.connect(toggle_ribbon_state)

        apply_ribbon_state(self.ribbon_state)

        # Install event filter for Hover mechanics
        self.top_bar_wrapper.installEventFilter(self)

        # Load configurations
        self.load_llm_configs()

        # 3. 对话展示滚动区 (仅存放消息气泡)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("QScrollArea { border: none; background-color: transparent; }")

        self.chat_container = QWidget()
        self.chat_container.setStyleSheet("background-color: transparent;")
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setSpacing(12)
        self.chat_layout.setContentsMargins(10, 10, 10, 0)
        self.chat_layout.setAlignment(Qt.AlignTop)

        self.scroll_area.setWidget(self.chat_container)
        main_layout.addWidget(self.scroll_area, stretch=1)

        main_layout.addSpacing(5)

        # --- 悬浮滚动到底部按钮 ---
        self.btn_scroll_bottom = QPushButton("", self.scroll_area)
        self.btn_scroll_bottom.setIcon(ThemeManager().icon("down", "bg_main"))
        self.btn_scroll_bottom.setFixedSize(40, 40)
        self.btn_scroll_bottom.setCursor(Qt.PointingHandCursor)
        self.btn_scroll_bottom.setStyleSheet(f"""
            QPushButton {{ 
                background-color: {ThemeManager().color('accent')}; 
                border-radius: 20px; border: 1px solid {ThemeManager().color('border')};
            }}
            QPushButton:hover {{ background-color: {ThemeManager().color('accent_hover')}; }}
        """)
        self.opacity_effect = QGraphicsOpacityEffect(self.btn_scroll_bottom)
        self.btn_scroll_bottom.setGraphicsEffect(self.opacity_effect)
        self.opacity_effect.setOpacity(0.0)
        self.btn_scroll_bottom.setVisible(False)
        self.fade_anim = QPropertyAnimation(self.opacity_effect, b"opacity")
        self.fade_anim.setDuration(250)
        self.fade_anim.finished.connect(self._on_fade_anim_finished)
        self.scroll_anim = QPropertyAnimation(self.scroll_area.verticalScrollBar(), b"value")
        self.scroll_anim.setEasingCurve(QEasingCurve.OutCubic)
        self.btn_scroll_bottom.clicked.connect(lambda: self.scroll_to_bottom(smooth=True))

        self.overlay_filter = FloatingOverlayFilter(self.scroll_area, self.btn_scroll_bottom)
        self.scroll_area.installEventFilter(self.overlay_filter)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._check_scroll_position)

        self.follow_up_shelf = QWidget()
        self.follow_up_shelf.setObjectName("FollowUpShelf")
        self.follow_up_shelf.setVisible(False)  # 初始隐藏
        self.follow_up_shelf_layout = QVBoxLayout(self.follow_up_shelf)
        self.follow_up_shelf_layout.setContentsMargins(10, 5, 10, 5)
        self.follow_up_shelf_layout.setSpacing(5)

        # 将 shelf 添加到主布局
        main_layout.addWidget(self.follow_up_shelf)

        # 5. 底部输入区
        self.input_container = ChatInputContainer()
        self.input_container.sig_send_clicked.connect(self.process_send)
        self.input_container.sig_export_clicked.connect(self.export_chat_history)
        self.input_container.sig_clear_clicked.connect(self.clear_chat_history)
        self.input_container.sig_attach_clicked.connect(self.show_attachment_menu)
        self.input_container.sig_clear_context_clicked.connect(self.clear_attached_context)

        main_layout.addWidget(self.input_container)

        self._render_timer = QTimer(self.widget)
        self._render_timer.setInterval(60)
        self._render_timer.timeout.connect(self._throttled_render)
        self._is_rendering_dirty = False

        return self.widget

    def _update_hardware_status(self):
        """异步获取当前推理设备，防止阻塞主界面"""
        from src.task.chat_tasks import FetchHardwareStatusTask
        self.hw_task_mgr = TaskManager()
        self.hw_task_mgr.sig_result.connect(self._on_hw_status_result)
        self.hw_task_mgr.start_task(FetchHardwareStatusTask, task_id="fetch_hw_chat", mode=TaskMode.THREAD)

    def _on_hw_status_result(self, result):
        if result and "dev_name" in result:
            self.lbl_hardware_status.setText(f"Compute Device: {result['dev_name']}")


    def attach_from_local(self):
        """按钮点击触发的文件选择器"""
        """
        paths, _ = QFileDialog.getOpenFileNames(
            self.widget, "Select Document(s) or Image(s)", "",
            "Supported Files (*.pdf *.md *.txt *.csv *.docx *.png *.jpg *.jpeg *.webp *.gif *.bmp);;"
            "Images (*.png *.jpg *.jpeg *.webp *.gif *.bmp);;"
            "Documents (*.pdf *.md *.txt *.csv *.docx)"
        )
        """
        paths, _ = QFileDialog.getOpenFileNames(
            self.widget, "Select Document(s)", "",
            "Supported Files (*.pdf *.md *.txt *.docx);;"
            "Documents (*.pdf *.md *.txt *.docx)"
        )
        if not paths: return
        self.process_attached_files(paths)


    def eventFilter(self, obj, event):
        if obj == self.top_bar_wrapper:
            if self.ribbon_state == "Hover":
                if event.type() == QEvent.Enter:
                    self.model_selector.setVisible(True)
                    self.collapsed_placeholder.setVisible(False)
                    self.trans_selector.setVisible(True)
                    self.lbl_kb.setVisible(True)
                    self.combo_kb.setVisible(True)
                    if hasattr(self, 'lbl_hardware_status'):
                        self.lbl_hardware_status.setVisible(True)

                elif event.type() == QEvent.Leave:
                    if not self.top_bar_wrapper.geometry().contains(self.widget.mapFromGlobal(QCursor.pos())):
                        self.model_selector.setVisible(False)
                        self.collapsed_placeholder.setVisible(True)
                        self.trans_selector.setVisible(False)
                        self.lbl_kb.setVisible(False)
                        self.combo_kb.setVisible(False)
                        if hasattr(self, 'lbl_hardware_status'):
                            self.lbl_hardware_status.setVisible(False)

        return super().eventFilter(obj, event)


    def process_attached_files(self, items):
        if not hasattr(self, 'external_chunks'):
            self.external_chunks = []
        if not hasattr(self, 'external_context_html'):
            self.external_context_html = ""

        file_infos = []
        has_legacy_doc = False

        for item in items:
            if isinstance(item, str):
                file_infos.append({"path": item, "name": os.path.basename(item)})
                if item.lower().endswith('.doc'): has_legacy_doc = True
            elif isinstance(item, dict):
                file_infos.append(item)
                if item.get("name", "").lower().endswith('.doc'): has_legacy_doc = True

        if has_legacy_doc:
            ToastManager().show("Legacy .doc format detected. It may not be fully parsed. Please convert to .docx",
                                "warning")

        self.input_container.set_uploading(True)

        if hasattr(self, 'attach_task_mgr'):
            self.attach_task_mgr.cancel_task()

        # 引入标准的 ProgressDialog，满足在执行期间可取消的交互需求
        from src.ui.components.dialog import ProgressDialog
        self.attach_pd = ProgressDialog(self.widget, "Processing Attachments", "Parsing files into memory...")
        self.attach_pd.show()

        self.attach_task_mgr = TaskManager()

        # 进度信号与取消操作桥接
        self.attach_task_mgr.sig_progress.connect(self.attach_pd.update_progress)
        self.attach_pd.sig_canceled.connect(self.attach_task_mgr.cancel_task)

        self.attach_task_mgr.sig_result.connect(self._on_attachment_result)
        self.attach_task_mgr.sig_state_changed.connect(self._on_attachment_state_changed)

        self.attach_task_mgr.start_task(
            ProcessAttachmentTask,
            task_id="process_attachment",
            mode=TaskMode.PROCESS,
            file_infos=file_infos
        )

    def set_controls_enabled(self, enabled: bool):
        """锁定或解锁对话控制区的关键配置"""
        if hasattr(self, 'model_selector'):
            self.model_selector.setEnabled(enabled)
        if hasattr(self, 'trans_selector'):
            self.trans_selector.setEnabled(enabled)
        if hasattr(self, 'combo_kb'):
            self.combo_kb.setEnabled(enabled)

        if hasattr(self, 'input_container'):
            if hasattr(self.input_container, 'chk_external_tools'):
                self.input_container.chk_external_tools.setEnabled(enabled)
            elif hasattr(self.input_container, 'chk_mcp_enable'):
                self.input_container.chk_mcp_enable.setEnabled(enabled)

            if hasattr(self.input_container, 'chk_academic_agent'):
                self.input_container.chk_academic_agent.setEnabled(enabled)

            if hasattr(self.input_container, 'btn_mcp_tags'):
                self.input_container.btn_mcp_tags.setEnabled(enabled)

            if hasattr(self.input_container, 'btn_clear'):
                self.input_container.btn_clear.setEnabled(enabled)
            if hasattr(self.input_container, 'btn_attach'):
                self.input_container.btn_attach.setEnabled(enabled)

    def _throttled_render(self):
        if getattr(self, '_is_rendering_dirty', False) and self.current_ai_bubble:
            self._is_rendering_dirty = False
            idx = getattr(self.current_ai_bubble, 'index', -1)
            self.current_ai_bubble.set_content(self._format_response(self.current_ai_text.lstrip(), idx))

            sb = self.scroll_area.verticalScrollBar()
            if (sb.maximum() - sb.value()) <= 50:
                self.scroll_to_bottom()

    def _on_attachment_state_changed(self, state, msg):
        if state == TaskState.SUCCESS.value:
            self.attach_pd.show_finish_state(True, "Attachment Complete", "Files successfully loaded into memory.")
        elif state == TaskState.FAILED.value or state == TaskState.TERMINATED.value:
            self.input_container.set_uploading(False)
            self.input_container.hide_context_preview()
            self.attach_pd.show_finish_state(False, "Attachment Halted", f"Task ended: {msg}")

    def _on_attachment_result(self, result):
        self.input_container.set_uploading(False)

        if not result: return

        chunks = result.get("chunks", [])
        html = result.get("html", "")

        self.external_chunks.extend(chunks)
        self.external_context_html += html

        if self.external_chunks:
            names = []
            for c in self.external_chunks:
                if c['name'] not in names:
                    names.append(c['name'])

            display_text = f"{names[0]}, {names[1]} and {len(names) - 2} more" if len(names) > 2 else ", ".join(names)

            QTimer.singleShot(100, lambda: self.input_container.show_context_preview(display_text))

            ToastManager().show(f"Attached {len(names)} file(s).", "success")
        else:
            self.input_container.hide_context_preview()

    def _on_attachment_finished(self, chunks, html):
        self.input_container.btn_attach.setEnabled(True)
        self.input_container.btn_send.setEnabled(True)
        self.external_chunks.extend(chunks)
        self.external_context_html += html

        if self.external_chunks:
            names = []
            for c in self.external_chunks:
                if c['name'] not in names:
                    names.append(c['name'])

            display_text = f"{names[0]}, {names[1]} 等 {len(names)} 个文件" if len(names) > 2 else ", ".join(names)
            self.input_container.show_context_preview(display_text)
            ToastManager().show(f"Attached {len(names)} file(s).", "success")
        else:
            self.input_container.hide_context_preview()

    def export_chat_history(self):
        if not self.history:
            ToastManager().show("There are currently no chat records to export.", "warning")
            self.logger.warning("Attempted to export empty chat history.")
            return

        tm = ThemeManager()
        menu = QMenu(self.widget)
        menu.setStyleSheet(f"""
            QMenu {{ background-color: {tm.color('bg_card')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; border-radius: 6px; padding: 4px;}} 
            QMenu::item {{ padding: 6px 20px; border-radius: 4px;}}
            QMenu::item:selected {{ background-color: {tm.color('accent')}; color: #fff; }}
        """)

        act_pdf = menu.addAction(tm.icon("article", "text_main"), "Export as PDF")
        act_md = menu.addAction(tm.icon("markdown", "text_main"), "Export as MD")
        act_txt = menu.addAction(tm.icon("file-text", "text_main"), "Export as TXT")

        # 在鼠标位置弹出菜单
        action = menu.exec(QCursor.pos())
        if not action:
            return

        if action == act_pdf:
            filter_str, default_ext = "PDF Document (*.pdf)", ".pdf"
        elif action == act_md:
            filter_str, default_ext = "Markdown File (*.md)", ".md"
        else:
            filter_str, default_ext = "Text File (*.txt)", ".txt"

        # 弹出系统保存对话框
        path, _ = QFileDialog.getSaveFileName(
            self.widget, "Export Log", f"Scholar_Navis_Log{default_ext}", filter_str
        )

        if not path:
            return

        if not path.endswith(default_ext):
            path += default_ext

        def _get_colored_svg_base64(icon_name, color_hex):
            svg_path = tm.get_resource_path("assets", "icons", f"{icon_name}.svg")
            try:
                with open(svg_path, "r", encoding="utf-8") as f:
                    svg_content = f.read()
                if "<svg" in svg_content:
                    svg_content = re.sub(r'<svg', f'<svg fill="{color_hex}"', svg_content, count=1)
                encoded = base64.b64encode(svg_content.encode('utf-8')).decode('utf-8')
                return f"data:image/svg+xml;base64,{encoded}"
            except Exception:
                return ""

        colors = {
            'title_blue': tm.color('title_blue'),
            'academic_blue': tm.color('academic_blue'),
            'success': tm.color('success')
        }

        user_icon_b64 = _get_colored_svg_base64("user", tm.color('academic_blue'))
        ai_icon_b64 = _get_colored_svg_base64("ai_model", tm.color('success'))

        # 初始化后台导出任务并连接弹窗
        from src.ui.components.dialog import ProgressDialog
        self.export_pd = ProgressDialog(self.widget, "Exporting Chat", "Processing file in background...")
        self.export_pd.show()

        self.export_task_mgr = TaskManager()
        self.export_task_mgr.sig_progress.connect(self.export_pd.update_progress)
        self.export_task_mgr.sig_state_changed.connect(self._on_export_state_changed)
        self.export_task_mgr.sig_result.connect(self._on_export_result)

        from src.task.chat_tasks import ExportChatTask
        self.export_task_mgr.start_task(
            ExportChatTask,
            task_id="export_chat",
            mode=TaskMode.THREAD,
            history=self.history,
            path=path,
            export_fmt=default_ext,
            colors=colors,
            font_family=tm.font_family(),
            user_icon=user_icon_b64,
            ai_icon=ai_icon_b64
        )

    def _on_export_state_changed(self, state, msg):
        from src.core.core_task import TaskState
        if state == TaskState.FAILED.value:
            self.export_pd.show_finish_state(False, "Export Failed", str(msg))

    def _on_export_result(self, result):
        if result and result.get("success"):
            self.export_pd.show_finish_state(True, "Export Complete",
                                             f"Saved to {os.path.basename(result.get('path', ''))}")
            ToastManager().show(f"Document successfully exported.", "success")
            self.logger.info(f"Chat history successfully exported to: {result.get('path')}")
        else:
            self.export_pd.show_finish_state(False, "Export Failed",
                                             result.get("msg", "Unknown error") if result else "Unknown error")
            self.logger.error(f"Failed to export document: {result.get('msg') if result else 'None'}")


    def clear_follow_up_shelf(self):
        while self.follow_up_shelf_layout.count() > 0:
            item = self.follow_up_shelf_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.follow_up_shelf.setVisible(False)

    def clear_chat_history(self):
        self.cancel_generation()
        self.current_ai_bubble = None
        self.history.clear()
        self.clear_layout(self.chat_layout)

        self.clear_follow_up_shelf()

        self.input_container.unlock_input()

        self.input_container.clear_text()
        self.clear_attached_context()
        self.is_locked = False
        ToastManager().show("Chat history cleared.", "success")

    def scroll_to_user_message(self, bubble_widget):
        QApplication.processEvents()

        target_y = max(0, bubble_widget.y() - 10)
        sb = self.scroll_area.verticalScrollBar()

        if hasattr(self, 'scroll_anim'):
            self.scroll_anim.stop()
            self.scroll_anim.setDuration(300)
            self.scroll_anim.setStartValue(sb.value())
            self.scroll_anim.setEndValue(target_y)
            self.scroll_anim.start()
        else:
            sb.setValue(target_y)

    def load_llm_configs(self):
        if hasattr(self, 'model_selector'):
            self.model_selector.load_llm_configs()
        if hasattr(self, 'trans_selector'):
            self.trans_selector.load_llm_configs()

    def process_send(self, text):
        # 1. 获取并格式化 KB ID
        kb_data = self.combo_kb.currentData()
        kb_id = kb_data.get("id") if isinstance(kb_data, dict) else kb_data
        if not kb_id:
            kb_id = "none"



        # 4. 获取当前附件数据
        current_html = getattr(self, 'external_context_html', "")
        current_chunks = getattr(self, 'external_chunks', [])
        self.external_context_html = ""
        self.external_chunks = []

        # 5. UI 切换与历史记录管理
        self.input_container.btn_send.setVisible(False)
        self.input_container.btn_stop.setVisible(True)

        self.logger.info(f"User asked: {text[:50]}... (KB: {kb_id})")
        self.input_container.clear_text()

        # 将上下文的 HTML 链接渲染在气泡上方
        self.add_bubble(text, is_user=True, context_html=current_html if current_html else None)

        llm_text = text
        if current_chunks:
            context_block = "\n".join(
                [f"--- {c['name']} ---\n{c['content']}" for c in current_chunks]
            )
            llm_text = f"Context Info:\n{context_block}\n\nQuestion:\n{text}"

        self.history.append({
            "role": "user",
            "content": llm_text,
            "display_text": text,
            "context_html": current_html if current_html else None,
            "external_chunks": current_chunks
        })

        self.input_container.hide_context_preview()
        self.external_chunks = current_chunks
        self.start_ai_response(kb_id)

    def _restore_last_input(self):
        last_user_msg = None
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i]['role'] == 'user':
                last_user_msg = self.history[i]
                break

        if last_user_msg:
            self.input_container.set_text(last_user_msg.get('display_text', ''))

            chunks = last_user_msg.get('external_chunks', [])
            html = last_user_msg.get('context_html', '')

            self.external_chunks = list(chunks) if chunks else []
            self.external_context_html = html if html else ""

            if self.external_chunks:
                names = []
                for c in self.external_chunks:
                    if c['name'] not in names:
                        names.append(c['name'])
                display_text = f"{names[0]}, {names[1]} and {len(names) - 2} more" if len(names) > 2 else ", ".join(
                    names)
                self.input_container.show_context_preview(display_text)
            else:
                self.input_container.hide_context_preview()

    def add_bubble(self, text, is_user, context_html=None):
        if is_user:
            self.remove_old_follow_ups()
            for i in range(self.chat_layout.count()):
                item = self.chat_layout.itemAt(i)
                if item and item.widget():
                    w = item.widget()
                    if hasattr(w, 'is_user') and w.is_user:
                        w.disable_edit()

        index = len(self.history)
        bubble = ChatBubbleWidget(text, is_user, index, context_html=context_html)
        bubble.index = index

        if is_user:
            bubble.sig_edit_confirmed.connect(self.handle_edit_resend)
            bubble.sig_link_clicked.connect(self.handle_link_click)
        else:
            bubble.lbl_text.anchorClicked.connect(self.handle_link_click)

        self.chat_layout.addWidget(bubble)

        if not getattr(self, '_is_editing', False):
            if is_user:
                QTimer.singleShot(50, lambda: self.scroll_to_user_message(bubble))
            else:
                QTimer.singleShot(50, lambda: self.scroll_to_bottom(smooth=True))
        return bubble

    def scroll_to_bottom(self, smooth=False):
        sb = self.scroll_area.verticalScrollBar()
        target = sb.maximum()

        if smooth and hasattr(self, 'scroll_anim') and sb.value() != target:
            self.scroll_anim.stop()
            self.scroll_anim.setDuration(250)  # 250毫秒的平滑过渡
            self.scroll_anim.setStartValue(sb.value())
            self.scroll_anim.setEndValue(target)
            self.scroll_anim.start()
        else:
            sb.setValue(target)

    def _on_query_translated(self, translated_text):
        for i in range(self.chat_layout.count() - 1, -1, -1):
            item = self.chat_layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                if getattr(w, 'is_user', False):
                    if hasattr(w, 'add_translation_widget'):
                        w.add_translation_widget(translated_text)
                    else:
                        self.logger.warning("ChatBubbleWidget is missing 'add_translation_widget' method.")
                    break

    def start_ai_response(self, kb_id, requires_translation=False):
        if getattr(self, 'worker_thread', None) is not None:
            try:
                if getattr(self, 'worker', None):
                    self.worker.cancel()
                    try:
                        self.worker.sig_token.disconnect()
                        self.worker.sig_finished.disconnect()
                        self.worker.sig_error.disconnect()
                        self.worker.sig_translated.disconnect()
                    except Exception:
                        pass
                if self.worker_thread.isRunning():
                    if not hasattr(self, '_orphaned_threads'): self._orphaned_threads = []
                    old_t, old_w = self.worker_thread, self.worker
                    old_t.quit()
                    self._orphaned_threads.append((old_t, old_w))
                    old_t.finished.connect(
                        lambda t=old_t, w=old_w: self._orphaned_threads.remove((t, w)) if (t, w) in getattr(self,
                                                                                                            '_orphaned_threads',
                                                                                                            []) else None)
            except RuntimeError:
                pass

            self.worker_thread = None
            self.worker = None

        main_config = self.model_selector.get_current_config()
        trans_config = self.trans_selector.get_current_config()

        use_academic_agent = self.input_container.chk_academic_agent.isChecked() if hasattr(self.input_container,
                                                                                            'chk_academic_agent') else True
        use_external_tools = self.input_container.chk_external_tools.isChecked() if hasattr(self.input_container,
                                                                                            'chk_external_tools') else False

        selected_tags = self.input_container.get_selected_tags()
        academic_tags = [t.replace("[ACADEMIC]", "").strip() for t in selected_tags if t.startswith("[ACADEMIC]")]
        external_names = [t.replace("[External]", "").strip() for t in selected_tags if t.startswith("[External]")]

        if use_academic_agent and not academic_tags:
            use_academic_agent = False

        if use_external_tools and not external_names:
            use_external_tools = False

        if main_config:
            actual_model = main_config.get("model_name", "").strip()
            self.logger.info(
                f" Starting AI response | Model: [{actual_model}] | Provider: [{main_config.get('name', 'Unknown')}]")

        # 初始化聊天气泡与 UI 状态
        self.current_ai_text = ""
        self.current_ai_bubble = self.add_bubble("", is_user=False)
        self.current_ai_bubble.set_loading(True)

        self.input_container.btn_send.setVisible(False)
        self.input_container.btn_stop.setVisible(True)
        self.input_container.btn_stop.setEnabled(True)
        self.input_container.btn_stop.setText("Stop")
        self.input_container.btn_stop.setToolTip("")
        self.set_controls_enabled(False)

        self._is_rendering_dirty = False
        self._render_timer.start()

        # Cleanly abort previous tasks if any exist
        if getattr(self, 'chat_task_mgr', None):
            try:
                self.chat_task_mgr.sig_progress.disconnect()
                self.chat_task_mgr.sig_state_changed.disconnect()
                self.chat_task_mgr.sig_result.disconnect()
            except Exception:
                pass
            self.chat_task_mgr.cancel_task()

        self.chat_task_mgr = TaskManager()
        self.chat_task_mgr.sig_progress.connect(self._on_chat_progress)
        self.chat_task_mgr.sig_state_changed.connect(self._on_chat_state_changed)
        self.chat_task_mgr.sig_result.connect(self._on_chat_result)

        try:
            self.input_container.btn_stop.clicked.disconnect()
        except Exception:
            pass
        self.input_container.btn_stop.clicked.connect(self.cancel_generation)

        GlobalSignals().sig_toast.connect(lambda msg, lvl: ToastManager().show(msg, lvl))

        current_external_chunks = getattr(self, 'external_chunks', [])

        QApplication.processEvents()

        def _launch_task():
            self.chat_task_mgr.start_task(
                ChatGenerationTask,
                task_id="chat_generation",
                mode=TaskMode.PROCESS,
                main_config=main_config,
                trans_config=trans_config,
                messages=list(self.history),
                kb_id=kb_id,
                requires_translation=requires_translation,
                external_context=current_external_chunks,
                use_academic_agent=use_academic_agent,
                academic_tags=academic_tags if use_academic_agent else [],
                use_external_tools=use_external_tools,
                external_tool_names=external_names if use_external_tools else []
            )

        QTimer.singleShot(100, _launch_task)

        self.external_chunks = []
        self.external_context_html = ""
        self.input_container.hide_context_preview()

    def handle_edit_resend(self, index, new_text):
        if getattr(self, 'is_locked', False):
            ToastManager().show("Cannot edit: The current library has been modified. Please clear chat.", "warning")
            old_msg = self.history[index]
            for i in range(self.chat_layout.count()):
                item = self.chat_layout.itemAt(i)
                if item and item.widget() and hasattr(item.widget(), 'index'):
                    if item.widget().index == index:
                        item.widget().set_content(
                            self._format_response(old_msg.get('display_text', old_msg['content']), index))
            return

        last_user_idx = -1
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i]['role'] == 'user':
                last_user_idx = i
                break

        if index != last_user_idx:
            ToastManager().show("You can only edit your most recent message.", "warning")
            return

        old_msg = self.history[index]
        old_context_html = old_msg.get('context_html')
        old_chunks = old_msg.get('external_chunks', [])

        self.history = self.history[:index]

        v_bar = self.scroll_area.verticalScrollBar()
        current_scroll = v_bar.value()
        self._is_editing = True

        self.clear_layout(self.chat_layout)
        temp_history = list(self.history)
        self.history = []

        for msg in temp_history:
            display_text = msg.get('display_text', msg['content'])
            ctx_html = msg.get('context_html')
            self.add_bubble(display_text, is_user=(msg['role'] == 'user'), context_html=ctx_html)
            self.history.append(msg)

        kb_data = self.combo_kb.currentData()
        kb_id = kb_data.get("id") if isinstance(kb_data, dict) else kb_data

        self.add_bubble(new_text, is_user=True, context_html=old_context_html)

        QApplication.processEvents()
        v_bar.setValue(current_scroll)
        self._is_editing = False

        llm_text = new_text
        if old_chunks:
            context_block = "\n".join(
                [f"--- {c['name']} ---\n{c['content']}" for c in old_chunks]
            )
            llm_text = f"Context Info:\n{context_block}\n\nQuestion:\n{new_text}"
        elif "Context Info:\n" in old_msg['content'] and "\n\nQuestion:\n" in old_msg['content']:
            context_part = old_msg['content'].split("\n\nQuestion:\n")[0]
            llm_text = f"{context_part}\n\nQuestion:\n{new_text}"

        self.history.append({
            "role": "user",
            "content": llm_text,
            "display_text": new_text,
            "context_html": old_context_html,
            "external_chunks": old_chunks
        })

        self.external_chunks = old_chunks
        self.start_ai_response(kb_id)

    def cancel_generation(self):
        if not self.input_container.btn_stop.isEnabled():
            return

        self.input_container.btn_stop.setEnabled(False)
        self.input_container.btn_stop.setText("Stopping...")

        if hasattr(self, '_render_timer'):
            self._render_timer.stop()
            self._is_rendering_dirty = False

        if getattr(self, 'chat_task_mgr', None):
            self.chat_task_mgr.cancel_task()

        if self.current_ai_bubble:
            self.current_ai_bubble.set_loading(False)

        self.logger.info("AI generation cancellation requested by user. Task manager is gracefully terminating.")
        self.scroll_to_bottom()

    def _trigger_follow_up(self, text):
        if getattr(self, 'is_locked', False):
            ToastManager().show("Cannot send: The current library has been modified. Please clear chat.", "warning")
            return
        self.process_send(text)

    def _edit_follow_up(self, text):
        if getattr(self, 'is_locked', False):
            ToastManager().show("Cannot edit: The current library has been modified. Please clear chat.", "warning")
            return
        self.input_container.set_text(text)

    def _show_slow_connection_warning(self):
        if self.current_ai_bubble and getattr(self, '_is_waiting_llm', False):
            idx = getattr(self.current_ai_bubble, 'index', -1)
            base_html = self._format_response(self.current_ai_text.lstrip(), idx)
            self.current_ai_bubble.set_content(
                base_html +
                "<br><div style='color:#05B8CC;'><i>Still connecting...</i></div>"
                "<div style='color:#e6a23c; font-size:12px; margin-top:5px; padding:8px; border:1px solid #e6a23c; border-radius:4px;'>"
                "Warning: The connection is taking longer than expected. Please check your <b>Network Proxy</b> or <b>API Endpoint (URL)</b>."
                "</div>"
            )
            self.scroll_to_bottom()

    def handle_external_send_with_mcp(self, context_text, prompt_text, target_tag):
        self.get_ui_widget()

        if hasattr(self, 'input_container'):
            if hasattr(self.input_container, 'chk_external_tools'):
                if not self.input_container.chk_external_tools.isChecked():
                    self.input_container.chk_external_tools.setChecked(True)
            elif hasattr(self.input_container, 'chk_mcp_enable'):
                if not self.input_container.chk_mcp_enable.isChecked():
                    self.input_container.chk_mcp_enable.setChecked(True)

        config_mgr = ConfigManager()
        available_tags = MCPManager.get_instance().get_available_mcp()

        deselected = set(self.config.mcp_servers.get("deselected_mcp_tags", []))
        for tag in available_tags:
            if tag.lower() == target_tag.lower():
                deselected.discard(tag)
            else:
                deselected.add(tag)

        self.config.mcp_servers["deselected_mcp_tags"] = list(deselected)
        self.config.save_mcp_servers()

        if hasattr(self, 'input_container') and hasattr(self.input_container, 'refresh_mcp_tags'):
            self.input_container.refresh_mcp()

        self.handle_external_send(context_text, prompt_text)

    def handle_external_send(self, context_text, prompt_text=""):
        self.get_ui_widget()

        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, "scholar_navis_external_context.txt")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(context_text)
        except Exception as e:
            self.logger.error(f"Failed to write temp external context: {e}")

        self.external_chunks = [{
            "path": temp_path,
            "name": "External Context.txt",
            "page": 1,
            "content": context_text
        }]

        safe_path = quote(temp_path)
        safe_name = quote("External Context.txt")
        link = f"cite://view?path={safe_path}&page=1&name={safe_name}"

        preview_text = context_text[:80].replace('\n', ' ') + "..."
        self.external_context_html = f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#05B8CC; text-decoration:none;'>📄 {preview_text} (Click to read more)</a></div>"

        if hasattr(self, 'input_container'):
            if hasattr(self.input_container, 'chk_external_tools'):
                if not self.input_container.chk_external_tools.isChecked():
                    self.input_container.chk_external_tools.setChecked(True)
            elif hasattr(self.input_container, 'chk_mcp_enable'):
                if not self.input_container.chk_mcp_enable.isChecked():
                    self.input_container.chk_mcp_enable.setChecked(True)

        if prompt_text:
            self.process_send(prompt_text)
        else:
            self.input_container.set_text("Please summarize this content and extract key insights.")
            self.input_container.show_context_preview("External Information (RSS/Web)")

    def show_attachment_menu(self):
        tm = ThemeManager()
        menu = QMenu(self.widget)
        menu.setStyleSheet(f"""
            QMenu {{ background-color: {tm.color('bg_card')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; }} 
            QMenu::item {{ padding: 6px 20px; }}
            QMenu::item:selected {{ background-color: {tm.color('accent')}; color: #fff; }}
        """)

        act_kb = menu.addAction(tm.icon("folder", "text_main"), "Select from Knowledge Base")
        act_local = menu.addAction(tm.icon("upload", "text_main"), "Upload Local File")

        act_kb.triggered.connect(self.attach_from_kb)
        act_local.triggered.connect(self.attach_from_local)

        menu.exec(QCursor.pos())

    def clear_attached_context(self):
        self.external_chunks = []
        self.external_context_html = ""
        self.input_container.hide_context_preview()

    def attach_from_kb(self):
        kb_data = self.combo_kb.currentData()
        kb_id = kb_data.get("id") if isinstance(kb_data, dict) else kb_data

        if not kb_id or kb_id == "none":
            ToastManager().show("Please select a Knowledge Base from the top dropdown first.", "warning")
            return

        files = self.kb_manager.get_kb_files(kb_id)
        if not files:
            ToastManager().show("The selected Knowledge Base is empty.", "warning")
            return

        dlg = SelectKBFileDialog(self.widget, files=files)

        if dlg.exec():
            paths = dlg.get_selected_paths()
            if paths:
                file_infos = []
                for p in paths:
                    real_name = next((f["name"] for f in files if f["path"] == p), os.path.basename(p))
                    file_infos.append({"path": p, "name": real_name})
                self.process_attached_files(file_infos)

    def update_ai_bubble(self, token):
        if not self.current_ai_bubble: return
        sb = self.scroll_area.verticalScrollBar()
        is_at_bottom = (sb.maximum() - sb.value()) <= 15
        idx = getattr(self.current_ai_bubble, 'index', -1)

        if token == "[CLEAR_SEARCH]":
            self.current_ai_text = re.sub(
                r"<div class=['\"]status-msg['\"].*?>.*?</div>\s*(?:<br>\s*)*(?:\n)*",
                '',
                self.current_ai_text,
                flags=re.DOTALL | re.IGNORECASE
            )
            self.current_ai_text = re.sub(
                r'(?:<br>\s*)*<i>(?:🌐\s*|📚\s*)?(?:Translating|Loading|Filtering|Extracting|\[Low VRAM).*?</i>\s*(?:<br>\s*)*(?:\n)*',
                '',
                self.current_ai_text,
                flags=re.DOTALL | re.IGNORECASE
            )
            self.current_ai_text = self.current_ai_text.lstrip()
            self._is_rendering_dirty = True
            return


        # 2. Handle LLM connection start
        if token == "[START_LLM_NETWORK]":
            self._is_waiting_llm = True
            base_html = self._format_response(self.current_ai_text.lstrip(), idx)
            self.current_ai_bubble.set_content(
                base_html +
                "<br><div style='color:#05B8CC;'><i>Connecting to LLM provider, please wait...</i></div>"
            )
            self.slow_conn_timer = QTimer(self)
            self.slow_conn_timer.setSingleShot(True)
            self.slow_conn_timer.timeout.connect(self._show_slow_connection_warning)
            self.slow_conn_timer.start(8000)
            if is_at_bottom: self.scroll_to_bottom()
            return

        # 3. Stop waiting and clear timer once real content arrives
        if getattr(self, '_is_waiting_llm', False):
            self._is_waiting_llm = False
            if hasattr(self, 'slow_conn_timer'):
                self.slow_conn_timer.stop()
            if self.current_ai_bubble.is_loading:
                self.current_ai_bubble.set_loading(False)

        self.current_ai_text += token
        self._is_rendering_dirty = True

    def _format_response(self, text, index):
        if not text:
            return ""

        try:
            pattern = r'```mermaid\s*\n(.*?)\n```'
            tm = ThemeManager()

            def repl_mermaid(match):
                code = match.group(1).strip()
                code_hash = hashlib.md5(code.encode('utf-8')).hexdigest()

                if not hasattr(self, 'mermaid_codes'):
                    self.mermaid_codes = {}
                self.mermaid_codes[code_hash] = code

                return (
                    f"<br><div style='padding:12px; margin: 8px 0; border:1px solid {tm.color('accent')}; border-radius:6px; background-color: transparent;'>"
                    f"<div style='margin-bottom: 5px;'><b>Mermaid Diagram Generated</b></div>"
                    f"<a href='mermaid://view?hash={code_hash}' style='color:{tm.color('accent')}; text-decoration:none; font-weight:bold;'>"
                    f"Click here to view / edit interactive diagram</a></div><br>")

            processed_text = re.sub(pattern, repl_mermaid, text, flags=re.DOTALL | re.IGNORECASE)

            return TextFormatter.format_chat_text(
                processed_text, index, getattr(self, 'expanded_thinks', set()),
                getattr(self, 'user_toggled_thinks', set())
            )
        except Exception as e:
            self.logger.error(f"Error formatting response: {e}")
            return str(text).replace('\n', '<br>')

    def on_chat_error(self, msg):
        self.logger.error(f"Chat generation encountered an error: {msg}")
        if hasattr(self, 'slow_conn_timer'): self.slow_conn_timer.stop()
        self._is_waiting_llm = False

        if hasattr(self, '_render_timer'): self._render_timer.stop()
        self.set_controls_enabled(True)

        self.input_container.btn_stop.setVisible(False)
        self.input_container.btn_send.setVisible(True)
        self._restore_last_input()


        error_title = "Generation Terminated"
        display_msg = str(msg).strip()

        try:
            parsed = json.loads(display_msg)
            error_title = parsed.get("title", error_title)
            display_msg = parsed.get("body", display_msg)
        except json.JSONDecodeError:
            prefix_match = re.match(r'^\s*\[(.*?)\]\s*\n*(.*)', display_msg, re.DOTALL)
            if prefix_match:
                raw_title = prefix_match.group(1).strip()
                display_msg = prefix_match.group(2).strip()

                if "API Request Error" in raw_title:
                    error_title = "Provider API Error"
                    if "404" in raw_title and "404" not in display_msg:
                        display_msg += "\n\nSuggestion: The selected model may not exist or your API endpoint path (e.g., /v1) is incorrect."
                    elif ("401" in raw_title or "key" in display_msg.lower()) and "verify" not in display_msg.lower():
                        error_title = "Authentication Required"
                        display_msg += "\n\nSuggestion: Please verify your API Key in the Global Settings."
                elif "Context Exceeded" in raw_title:
                    error_title = "Context Window Exceeded"
                elif "Rate Limit" in raw_title:
                    error_title = "Rate Limit Reached"
                elif "Timeout" in raw_title:
                    error_title = "Connection Timeout"

        # 处理特定的顶层网络拦截
        if "translation" in msg.lower() or "translator" in msg.lower():
            error_title = "Translation Module Failure"
            ToastManager().show("Translation model error. Please check your translator settings.", "error")
        elif "time" in msg.lower() or "connect" in msg.lower():
            ToastManager().show("Network connection failed. Please check your API configuration or proxy.", "error")

        error_json_str = json.dumps({"title": error_title, "body": display_msg})

        if self.current_ai_bubble:
            idx = getattr(self.current_ai_bubble, 'index', -1)
            self.current_ai_bubble.is_interrupted = True

            if not self.current_ai_text.strip():
                self.chat_layout.removeWidget(self.current_ai_bubble)
                self.current_ai_bubble.deleteLater()

                error_bubble = ChatBubbleWidget(
                    text=error_json_str,
                    is_user=False,
                    index=idx,
                    msg_type=ChatBubbleWidget.MSG_ERROR
                )
                self.chat_layout.addWidget(error_bubble)
            else:
                self.current_ai_bubble.set_content(self._format_response(self.current_ai_text, idx))

                idx += 1
                error_bubble = ChatBubbleWidget(
                    text=error_json_str,
                    is_user=False,
                    index=idx,
                    msg_type=ChatBubbleWidget.MSG_ERROR
                )
                self.chat_layout.addWidget(error_bubble)

        self.current_ai_bubble = None
        self.scroll_to_bottom()

    def on_chat_finished(self, is_cancelled=False):
        if hasattr(self, '_render_timer'): self._render_timer.stop()
        self.set_controls_enabled(True)

        if getattr(self, '_is_rendering_dirty', False):
            self._throttled_render()

        if not self.current_ai_bubble:
            return

        self.input_container.btn_stop.setText("Stop")
        self.input_container.btn_stop.setEnabled(True)
        self.input_container.btn_stop.setVisible(False)
        self.input_container.btn_send.setVisible(True)

        if is_cancelled:
            if self.current_ai_bubble:
                self.current_ai_bubble.is_interrupted = True
            StandardDialog(self.widget, "Task Cancelled", "The AI generation has been stopped by the user.",
                           show_cancel=False).exec()
            if hasattr(self, '_restore_last_input'):
                self._restore_last_input()

            self.history.append({"role": "assistant", "content": self.current_ai_text, "status": "interrupted"})
            self.current_ai_bubble = None
            self.scroll_to_bottom()
            return

        try:
            self.input_container.btn_stop.clicked.disconnect()
        except Exception:
            pass

        if self.current_ai_bubble and self.current_ai_bubble.is_loading:
            self.current_ai_bubble.set_loading(False)

        full_text = self.current_ai_text
        cites_html = ""

        cite_match = re.search(
            r'<br><hr style=\'border:0; height:1px; background:#444; margin:15px 0;\'><b>.*?Cited Sources:</b><br>',
            full_text)
        if cite_match:
            cites_html = full_text[cite_match.start():]
            full_text = full_text[:cite_match.start()]

        pattern = r'(?:\[\s*FOLLOW[_-]?\s*UPS?\s*\]|(?:^|\n|<br>|<br/>)\s*\*?\*?(?:💡\s*)?Suggested\s*Follow[- ]?ups?(?:\s*questions?)?:?\*?\*?)\s*'
        matches = list(re.finditer(pattern, full_text, flags=re.IGNORECASE))
        questions = []

        if matches:
            last_match = matches[-1]
            follow_up_block = full_text[last_match.end():].replace('<br>', '\n').replace('<br/>', '\n')

            if len(follow_up_block) < 1500:
                clean_text = full_text[:last_match.start()].strip()
                self.current_ai_text = clean_text + cites_html

                for line in follow_up_block.split('\n'):
                    line = line.strip()
                    line = re.sub(r'^>\s*', '', line)
                    if re.match(r'^([-*]|\d+\.)', line):
                        q = re.sub(r'^([-*\s]+|\d+\.\s*)', '', line).strip()
                        q = q.replace('**', '').strip()
                        if q and len(q) > 4:  # 防止空行或者过短的字符
                            tag_match = re.match(r'^\[(.*?)\]\s*(.*)', q)
                            if tag_match:
                                tag, text = tag_match.groups()
                                questions.append({"tag": tag.strip(), "text": text.strip()})
                            else:
                                questions.append({"tag": "General", "text": q})

                # 更新气泡内容
                idx = getattr(self.current_ai_bubble, 'index', -1)
                final_html = self._format_response(self.current_ai_text, idx)
                self.current_ai_bubble.set_content(final_html)

                # 渲染追问按钮
                if questions:
                    self.render_follow_up_buttons(questions)
            else:
                self.current_ai_text = full_text + cites_html
                idx = getattr(self.current_ai_bubble, 'index', -1)
                final_html = self._format_response(self.current_ai_text,
                                                   idx) if self.current_ai_text else "No response."
                self.current_ai_bubble.set_content(final_html)
        else:
            self.current_ai_text = full_text + cites_html
            idx = getattr(self.current_ai_bubble, 'index', -1)
            final_html = self._format_response(self.current_ai_text, idx) if self.current_ai_text else "No response."
            self.current_ai_bubble.set_content(final_html)

        self.history.append({"role": "assistant", "content": self.current_ai_text})
        self.current_ai_bubble = None
        self.logger.info("AI response generation finished and UI updated.")

    def on_kb_modified(self, kb_id):
        """当当前关联的知识库在后台发生变更时触发，锁定对话防止上下文错乱"""

        DatabaseManager().reload()

        self.refresh_kb_list()

        if not self.history:
            return

        curr_data = self.combo_kb.currentData()
        curr_id = curr_data.get("id") if isinstance(curr_data, dict) else curr_data

        if curr_id == kb_id:
            self.is_locked = True
            if hasattr(self, 'input_container'):
                self.input_container.lock_input()
            ToastManager().show("The knowledge base was modified. Chat is currently locked.", "warning")



    def render_follow_up_buttons(self, questions):
        if not questions: return

        self.follow_up_group = FollowUpGroupWidget(
            questions,
            self._trigger_follow_up,
            self._edit_follow_up
        )

        # 将其作为对话流的一个整体插入
        self.chat_layout.addWidget(self.follow_up_group)

        if not getattr(self, '_is_editing', False):
            QTimer.singleShot(50, self.scroll_to_bottom)

    def remove_old_follow_ups(self):
        """清理历史中的追问组件，避免重复堆叠"""
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item and isinstance(item.widget(), FollowUpGroupWidget):
                item.widget().deleteLater()

    def clear_layout(self, layout):
        while layout.count() > 0:
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                if hasattr(widget, 'clean_up_images'):
                    widget.clean_up_images()
                widget.deleteLater()
            elif item.spacerItem():
                pass

    def handle_link_click(self, url):
        if hasattr(url, 'toString'):
            url_str = url.toString()
        else:
            url_str = str(url)

        if url_str.startswith("mermaid://"):
            parsed = urlparse(url_str)
            params = parse_qs(parsed.query)
            code_hash = params.get('hash', [''])[0]

            # 从缓存字典中取出真实的 Mermaid 代码
            code = getattr(self, 'mermaid_codes', {}).get(code_hash, "")
            if code:
                # 延迟导入避免循环依赖
                if not hasattr(self, 'mermaid_viewer') or self.mermaid_viewer is None:
                    self.mermaid_viewer = MermaidViewer(None)
                self.mermaid_viewer.load_diagram(code)
            else:
                ToastManager().show("Diagram data lost. Please ask the AI to generate it again.", "error")
            return

        if url_str.startswith("think://"):
            parsed = urlparse(url_str)
            action = parsed.netloc
            params = parse_qs(parsed.query)
            idx = int(params.get('index', [-1])[0])

            if idx != -1:
                if not hasattr(self, 'user_toggled_thinks'):
                    self.user_toggled_thinks = set()
                self.user_toggled_thinks.add(idx)

                if action == 'expand':
                    self.expanded_thinks.add(idx)
                else:
                    self.expanded_thinks.discard(idx)

                # 寻找对应的气泡重绘
                for i in range(self.chat_layout.count()):
                    item = self.chat_layout.itemAt(i)
                    if item and item.widget():
                        w = item.widget()
                        if isinstance(w, ChatBubbleWidget) and getattr(w, 'index', -1) == idx:
                            raw_text = self.current_ai_text if w == getattr(self, 'current_ai_bubble', None) else (
                                self.history[idx]['content'] if idx < len(self.history) else "")
                            if raw_text:
                                w.set_content(self._format_response(raw_text, idx))
                            break
            return

        if url_str.startswith("cite://"):
            parsed = urlparse(url_str)
            params = parse_qs(parsed.query)
            file_path = params.get('path', [''])[0]

            if file_path.startswith(("http://", "https://")):
                QDesktopServices.openUrl(QUrl(file_path))
                ToastManager().show(f"Opening online source...", "success")
                return

            # page_num = int(params.get('page', ['1'])[0]) - 1
            page_num = 0
            text_snippet = params.get('text', [''])[0]
            source_name = params.get('name', [''])[0]

            kb_data = self.combo_kb.currentData()
            kb_id = kb_data.get("id") if isinstance(kb_data, dict) else kb_data

            real_path = ""
            if kb_id and source_name:
                kb_meta = self.kb_manager.get_kb_by_id(kb_id)
                if kb_meta:
                    file_map = kb_meta.get("file_map", {})
                    reverse_map = {v: k for k, v in file_map.items()}
                    obf_name = reverse_map.get(source_name)
                    if obf_name:
                        real_path = os.path.join(self.kb_manager.WORKSPACE_DIR, kb_id, "documents", obf_name)

            target_path = real_path if real_path and os.path.exists(real_path) else file_path

            if os.path.exists(target_path):
                ext = source_name.lower().split('.')[-1] if '.' in source_name else ""

                # === 路由分发 ===
                if ext == 'pdf':
                    if self.pdf_viewer is None: self.pdf_viewer = InternalPDFViewer(None)
                    self.pdf_viewer.load_document(target_path, page_num, text_snippet, display_name=source_name)
                    ToastManager().show(f"Document opened.", "success")

                elif ext in ['md', 'txt', 'csv', 'json']:
                    if not hasattr(self, 'text_viewer') or self.text_viewer is None:
                        from src.ui.components.pdf_viewer import InternalTextViewer
                        self.text_viewer = InternalTextViewer(None)
                    self.text_viewer.load_document(target_path, text_snippet, display_name=source_name)
                    ToastManager().show("Document snippet opened", "success")

                else:
                    # 对于图片或无法渲染的格式，降级交给操作系统处理
                    temp_dir = tempfile.gettempdir()
                    safe_name = source_name if source_name else "document.bin"
                    temp_file_path = os.path.join(temp_dir, f"scholar_navis_view_{safe_name}")

                    try:
                        shutil.copy2(target_path, temp_file_path)
                        QDesktopServices.openUrl(QUrl.fromLocalFile(temp_file_path))
                        ToastManager().show(f"Opening with system default application: {safe_name}", "success")
                    except Exception as e:
                        ToastManager().show(f"Failed to invoke external program: {str(e)}", "error")
            else:
                ToastManager().show(f"File not found: {source_name or file_path}", "error")
        else:
            QDesktopServices.openUrl(QUrl(url_str))

    def refresh_kb_list(self):
        self.load_llm_configs()
        if not hasattr(self, 'combo_kb'): return
        curr_data = self.combo_kb.currentData()
        curr_id = curr_data['id'] if isinstance(curr_data, dict) else curr_data

        self.combo_kb.blockSignals(True)
        self.combo_kb.clear()

        self.combo_kb.addItem("No Knowledge Base (Direct Chat)", "none")

        kbs = self.kb_manager.get_all_kbs()
        target_idx = 0  # 默认选中 "none"

        for i, kb in enumerate(kbs):
            if kb.get('status') == 'ready':
                m = get_model_conf(kb.get('model_id'), "embedding")
                m_ui = m['ui_name'] if m else kb.get('model_id', '?')
                display_text = f"{kb['name']}   [Model: {m_ui} | Docs: {kb.get('doc_count', 0)}]"
                self.combo_kb.addItem(display_text, kb)
                if kb['id'] == curr_id:
                    target_idx = self.combo_kb.count() - 1

        if self.combo_kb.count() > 0:
            self.combo_kb.setCurrentIndex(target_idx)

        self.combo_kb.blockSignals(False)

    def on_global_kb_switched(self, kb_id):
        if not hasattr(self, 'combo_kb') or not kb_id: return
        for i in range(self.combo_kb.count()):
            data = self.combo_kb.itemData(i)
            if data and data.get('id') == kb_id:
                self.combo_kb.blockSignals(True)
                self.combo_kb.setCurrentIndex(i)
                self.combo_kb.blockSignals(False)
                self.kb_id = kb_id
                if hasattr(self, 'db'): self.db.switch_kb(kb_id)
                break

    def _on_fade_anim_finished(self):
        """动画结束时的统一处理逻辑，避免反复 connect/disconnect 产生警告"""
        if self.fade_anim.endValue() == 0.0:
            self.btn_scroll_bottom.hide()


    def _check_scroll_position(self):
        sb = self.scroll_area.verticalScrollBar()
        should_show = (sb.maximum() - sb.value() > 200)

        # 防止动画重复触发
        if should_show and not self.btn_scroll_bottom.isVisible():
            self.btn_scroll_bottom.setVisible(True)
            self.fade_anim.stop()
            self.fade_anim.setStartValue(self.opacity_effect.opacity())
            self.fade_anim.setEndValue(1.0)
            self.fade_anim.start()

        elif not should_show and self.btn_scroll_bottom.isVisible():
            if self.fade_anim.endValue() != 0.0:
                self.fade_anim.stop()
                self.fade_anim.setStartValue(self.opacity_effect.opacity())
                self.fade_anim.setEndValue(0.0)
                self.fade_anim.start()

    def _on_chat_progress(self, progress, msg):
        if progress == -1:
            self.update_ai_bubble(msg)

    def _on_chat_state_changed(self, state, msg):
        if state == TaskState.SUCCESS.value:
            self.on_chat_finished(is_cancelled=False)
        elif state == TaskState.FAILED.value:
            self.on_chat_error(msg)
        elif state == TaskState.TERMINATED.value:
            self.on_chat_finished(is_cancelled=True)

    def _on_chat_result(self, payload):
        if isinstance(payload, dict) and payload.get("event") == "translated":
            self._on_query_translated(payload.get("text"))


    def _save_setting(self, key, value):
        self.config.user_settings[key] = value
        self.config.save_settings()

    def execute_task(self):
        pass
