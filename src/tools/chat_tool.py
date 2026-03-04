import base64
import csv
import datetime
import functools
import gc
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import traceback
from urllib.parse import urlparse, parse_qs, quote

import markdown
import pymupdf4llm
import torch
from PySide6.QtCore import Qt, Signal, QObject, QThread, QUrl, QTimer, QPropertyAnimation, QMarginsF
from PySide6.QtGui import QDesktopServices, QCursor, QAction, QPdfWriter, QTextDocument, QPageSize
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout,
                               QPlainTextEdit, QPushButton, QLabel,
                               QScrollArea, QFrame, QFileDialog, QMenu, QDialog,
                               QAbstractItemView, QListWidget, QListWidgetItem, QDialogButtonBox, QCheckBox,
                               QToolButton, QWidgetAction, QSizePolicy, QGraphicsOpacityEffect, QApplication, QComboBox)
from langdetect import detect

from src.core.config_manager import ConfigManager
from src.core.core_task import TaskManager, TaskMode, TaskState
from src.core.device_manager import DeviceManager
from src.core.kb_manager import KBManager, DatabaseManager
from src.core.llm_impl import OpenAICompatibleLLM
from src.core.mcp_manager import MCPManager
from src.core.models_registry import get_model_conf, resolve_auto_model, ModelManager
from src.core.rerank_engine import RerankEngine
from src.core.signals import GlobalSignals
from src.core.theme_manager import ThemeManager
from src.task.chat_tasks import ProcessAttachmentTask
from src.task.kb_tasks import _worker_load_model
from src.tools.base_tool import BaseTool
from src.tools.settings_tool import FloatingOverlayFilter
from src.ui.components.chat_bubble import ChatBubbleWidget
from src.ui.components.combo import BaseComboBox
from src.ui.components.dialog import StandardDialog, SelectKBFileDialog
from src.ui.components.mermaid_viewer import MermaidViewer
from src.ui.components.model_selector import ModelSelectorWidget
from src.ui.components.pdf_viewer import InternalPDFViewer, InternalTextViewer
from src.ui.components.pill_button import FollowUpGroupWidget
from src.ui.components.text_formatter import TextFormatter
from src.ui.components.toast import ToastManager


@functools.lru_cache(maxsize=128)
def get_cached_translation(text, direction="to_en", llm_instance=None, **kwargs):
    if not llm_instance: return text

    if direction == "to_en":
        prompt = (
            "You are an expert bioinformatician and translator. "
            "Translate the following user query into precise academic English. "
            "CRITICAL: DO NOT translate or alter any Latin taxonomic names (e.g., Gossypium, Arabidopsis) "
            "or scientific abbreviations (e.g., scRNA-seq, qPCR). "
            "Output ONLY the translated English text, nothing else."
        )
    else:
        prompt = (
            "You are an expert academic translator. Translate the following English text "
            "into the language of the user's original query. \n"
            "CRITICAL RULES:\n"
            "1. KEEP ALL CITATION TAGS INTACT (e.g., [1], [2]).\n"
            "2. DO NOT translate Latin taxonomic names (e.g., Gossypium) or scientific abbreviations.\n"
            "3. PRESERVE all Markdown formatting, bolding, and structure.\n"
        )

    return llm_instance.chat([
        {"role": "system", "content": prompt},
        {"role": "user", "content": text}
    ], **kwargs).strip()


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
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.sig_files_dropped.emit(paths)
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

    # 修改后
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Return and not event.modifiers() & Qt.ShiftModifier:
            parent = self.parent()
            while parent is not None:
                if hasattr(parent, 'btn_send'):
                    if not parent.btn_send.isEnabled():
                        event.accept()
                        return
                    break
                parent = parent.parent()
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
        self.chk_mcp_enable = QCheckBox("Enable advanced search (MCP Tools - Requires model support)")
        self.chk_mcp_enable.setStyleSheet("color: #05B8CC; font-weight: bold;")
        self.chk_mcp_enable.setChecked(True)

        self.btn_mcp_tags = QToolButton()
        self.btn_mcp_tags = QPushButton("Filter Tools: Loading...")
        self.btn_mcp_tags.setIcon(ThemeManager().icon("filter", "text_muted"))
        self.btn_mcp_tags.setCursor(Qt.PointingHandCursor)
        self.btn_mcp_tags.setStyleSheet(
            "QPushButton { color: #aaaaaa; background: transparent; border: 1px solid #555; border-radius: 4px; padding: 2px 8px; }"
            "QPushButton:hover { background: #333; }"
        )
        self.btn_mcp_tags.setVisible(True)

        self.menu_mcp_tags = QMenu(self)
        self.menu_mcp_tags.setStyleSheet(
            "QMenu { background-color: #2b2b2b; border: 1px solid #555; border-radius: 6px; padding: 4px; }"
        )
        self.btn_mcp_tags.clicked.connect(self._show_filter_menu)

        self.tag_actions = {}
        self.user_deselected_tags = set()
        self.known_tags = set()

        self.btn_mcp_guide = QPushButton("Prompt guide")

        # 存储复选框动作的字典
        self.tag_actions = {}

        self.btn_mcp_guide = QPushButton("Prompt guide")
        self.btn_mcp_guide.setCursor(Qt.PointingHandCursor)
        self.btn_mcp_guide.setStyleSheet(
            "color: #aaaaaa; background: transparent; border: 1px solid #555; border-radius: 4px; padding: 2px 8px;")
        self.btn_mcp_guide.setVisible(True)

        self.mcp_toolbar.addWidget(self.chk_mcp_enable)
        self.mcp_toolbar.addWidget(self.btn_mcp_tags)
        self.mcp_toolbar.addWidget(self.btn_mcp_guide)
        self.mcp_toolbar.addStretch()
        main_layout.insertLayout(1, self.mcp_toolbar)

        self.chk_mcp_enable.toggled.connect(self._on_mcp_enable_toggled)
        self.menu_mcp_guide = QMenu(self)
        # 设置菜单
        prompts = [
            "Search for the latest literature and abstracts regarding a specific research topic or gene.",
            "Find the exact metadata and full-text Open Access PDF link for this article title or DOI.",
            "Trace the citation graph (references and citations) to find related high-impact papers for this DOI.",
            "Query the functional summary, length, and taxonomic info of a specific gene or protein.",
            "Find 3D protein structures and experimental resolution details in the RCSB PDB.",
            "Search for public omics datasets (GEO/SRA) related to specific experimental treatments or phenotypes.",
            "Fetch the exact sequence in FASTA format for a given nucleotide or protein accession.",
            "Retrieve plant-specific genomic data, orthologs, or gene families from Phytozome.",
            "Get the exact scientific name, TaxID, and evolutionary lineage for a specific organism.",
            "Explain a complex biological mechanism and draw a Mermaid relationship map."
        ]

        for p in prompts:
            action = QAction(p, self)
            action.triggered.connect(lambda checked, text=p: self.set_text(text))
            self.menu_mcp_guide.addAction(action)

        self.btn_mcp_guide.setMenu(self.menu_mcp_guide)

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
        self.btn_send.setFixedSize(70, 32)
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
        self.btn_stop.setFixedSize(70, 32)
        self.btn_stop.setVisible(False)
        self.bottom_bar.addWidget(self.btn_stop)

        main_layout.addLayout(self.bottom_bar)

        self.btn_send.clicked.connect(self._emit_send)
        self.text_edit.sig_send.connect(self._emit_send)

        GlobalSignals().mcp_status_changed.connect(self._on_mcp_status_changed)

        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()

    def _apply_theme(self):
        tm = ThemeManager()
        self.setStyleSheet(
            f"QFrame#ChatInputContainer {{ background-color: {tm.color('bg_card')}; border: 1px solid {tm.color('border')}; border-radius: 8px; }}")


        self.text_edit.setStyleSheet(f"""
            QPlainTextEdit {{ background-color: transparent; color: {tm.color('text_main')}; border: none; font-size: 14px; font-family: {tm.font_family()}; }}
            QScrollBar:vertical {{ background: transparent; width: 6px; }}
            QScrollBar::handle:vertical {{ background: rgba(150, 150, 150, 0.35); border-radius: 3px; }}
            QScrollBar::handle:vertical:hover {{ background: rgba(150, 150, 150, 0.65); }}
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

        if hasattr(self, 'lbl_context_icon'):
            self.lbl_context_icon.setPixmap(tm.icon("link", "accent").pixmap(14, 14))
            self.lbl_context_info.setStyleSheet(f"color: {tm.color('accent')}; font-size: 12px; border: none;")

        self.btn_clear_context.setIcon(tm.icon("close", "danger"))
        self.btn_clear_context.setStyleSheet(
            "QPushButton { border: none; background: transparent; padding: 2px; } QPushButton:hover { background: rgba(255, 107, 107, 0.2); border-radius: 4px; }")

        btn_mcp_style = f"""
            QPushButton {{ color: {tm.color('text_muted')}; background: transparent; border: 1px solid {tm.color('border')}; border-radius: 4px; padding: 4px 8px; font-size: 12px; }}
            QPushButton:hover {{ background: {tm.color('btn_hover')}; color: {tm.color('text_main')}; }}
        """
        self.btn_mcp_tags.setIcon(tm.icon("filter", "text_muted"))
        self.btn_mcp_tags.setStyleSheet(btn_mcp_style)
        self.btn_mcp_guide.setIcon(tm.icon("help", "text_muted"))
        self.btn_mcp_guide.setStyleSheet(btn_mcp_style)

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
        if hasattr(self, 'menu_mcp_guide'):
            self.menu_mcp_guide.setStyleSheet(menu_style)


    def set_uploading(self, is_uploading: bool):
        self.btn_send.setEnabled(not is_uploading)
        self.btn_attach.setEnabled(not is_uploading)
        if is_uploading:
            self.btn_send.setToolTip("Please wait for file upload to complete...")
            self.btn_send.setStyleSheet(self.btn_send.styleSheet() + "QPushButton:disabled { background-color: #555; color: #888; }")
        else:
            self.btn_send.setToolTip("")

    def _on_mcp_status_changed(self):
        if self.chk_mcp_enable.isChecked():
            self.refresh_mcp_tags()

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

    def _on_mcp_enable_toggled(self, checked):
        """当用户勾选/取消勾选 MCP 开关时触发"""
        self.btn_mcp_guide.setVisible(checked)
        self.btn_mcp_tags.setVisible(checked)
        if checked:
            # 只要开启，就立刻主动刷新一次标签，而不是傻等用户点击菜单
            self.refresh_mcp_tags()

    def _show_filter_menu(self):
        self.btn_mcp_tags.setText("Filter Tools: Fetching...")
        QApplication.processEvents()

        self.refresh_mcp_tags()

        pos = self.btn_mcp_tags.mapToGlobal(self.btn_mcp_tags.rect().topLeft())
        menu_height = self.menu_mcp_tags.sizeHint().height()
        pos.setY(pos.y() - menu_height - 4)

        self.menu_mcp_tags.popup(pos)

    def refresh_mcp_tags(self):
        try:
            mcp_mgr = MCPManager.get_instance()

            available_tags = mcp_mgr.get_available_tags()
            deselected_tags = self.config.mcp_servers.get("deselected_mcp_tags", [])

            self.menu_mcp_tags.clear()
            self.tag_actions.clear()
            self.known_tags.clear()

            if not available_tags:
                self.btn_mcp_tags.setText("🏷️ Filter Tools: None")
                from PySide6.QtGui import QAction
                dummy = QAction("⏳ No active MCP servers...", self)
                dummy.setEnabled(False)
                self.menu_mcp_tags.addAction(dummy)
                return

            tm = ThemeManager()
            for tag in available_tags:
                chk = QCheckBox(f"  {tag}")

                chk.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                chk.setChecked(tag not in deselected_tags)
                chk.setCursor(Qt.PointingHandCursor)
                chk.toggled.connect(lambda checked, t=tag: self._on_tag_toggled(t, checked))

                wa = QWidgetAction(self)
                wa.setDefaultWidget(chk)
                self.menu_mcp_tags.addAction(wa)

                self.tag_actions[tag] = chk
                self.known_tags.add(tag)

            self._update_tag_button_text()

        except Exception as e:
            self.logger.error(f"Error fetching MCP tags: {e}", exc_info=True)
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
            available = MCPManager.get_instance().get_available_tags()
            deselected = self.config.mcp_servers.get("deselected_mcp_tags", [])
            return [t for t in available if t not in deselected]
        except:
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


class ChatWorker(QObject):
    sig_token = Signal(str)
    sig_finished = Signal()
    sig_error = Signal(str)

    def __init__(self, main_config, trans_config, messages, kb_id, requires_translation=False, external_context=None,
                 use_mcp=False):
        super().__init__()
        self.logger = logging.getLogger("ChatWorker")
        self.main_config = main_config
        self.trans_config = trans_config
        self.messages = messages
        self.kb_id = kb_id if kb_id != "none" else None
        self.requires_translation = requires_translation
        self.external_context = external_context
        self.use_mcp = use_mcp
        self.db = DatabaseManager()
        self.kb_manager = KBManager()
        self.reranker = RerankEngine()
        self.full_response_cache = ""

        # 实例长连接缓存
        self.main_llm = None
        self.trans_llm = None

    def cancel(self):
        self._is_cancelled = True
        if self.main_llm: self.main_llm.cancel()
        if self.trans_llm: self.trans_llm.cancel()

    def _init_llms(self):
        """初始化主模型与翻译模型池"""
        if self.main_config and not self.main_llm:
            cfg = self.main_config.copy()
            if "tools" not in cfg:
                cfg["tools"] = []
            self.main_llm = OpenAICompatibleLLM(cfg)

        if self.requires_translation and self.trans_config and not self.trans_llm:
            self.trans_llm = OpenAICompatibleLLM(self.trans_config)


    def _process_rerank(self, query, docs, domain, top_k):
        if not docs: return []

        import multiprocessing as mp
        from src.core.core_task import TaskState, RunnerProcess
        from src.task.kb_tasks import RerankTask

        queue = mp.Queue()
        worker = RunnerProcess(
            RerankTask, "rerank_sync", queue,
            {"query": query, "docs": docs, "domain": domain, "top_k": top_k}
        )
        worker.start()

        ranked = docs[:top_k]
        while True:
            if getattr(self, '_is_cancelled', False):
                worker.terminate()
                break
            try:
                # Wait for process result synchronously (perfectly safe inside QThread)
                data = queue.get(timeout=0.2)
                state = data.get("state")

                if state == TaskState.SUCCESS.value:
                    if data.get("payload"): ranked = data["payload"]
                    break
                elif state == TaskState.FAILED.value:
                    self.logger.error(f"Rerank process failed: {data.get('msg')}")
                    break
            except Exception:
                if not worker.is_alive(): break

        return ranked


    def run(self):
        try:
            from src.core.config_manager import ConfigManager
            self.config = ConfigManager()

            self._init_llms()
            original_user_query = self.messages[-1]['content']
            search_query = original_user_query
            domain = "General Academic"
            context_str = ""
            sources_map = {}

            # Phase 1: Query Extraction & Translation (Cache Accelerated)
            if self.requires_translation:
                self.sig_token.emit("<i>Translating your query to academic English for precise retrieval...</i>\n\n")
                try:
                    trans_kwargs = {
                        "is_translation": True,
                        "stream": False  # 翻译不需要流式
                    }
                    search_query = get_cached_translation(original_user_query, "to_en", self.trans_llm, **trans_kwargs)
                except Exception as e:
                    self.sig_error.emit(
                        f"Translation model request failed. Please check your translation API configuration.\nDetails: {e}")
                    return

            self.sig_token.emit("[CLEAR_SEARCH]")

            # ==========================================
            # Phase 2: Vector Retrieval & Reranking (Local KB)
            # ==========================================
            if self.kb_id and self.kb_id != "none":

                kb_info = self.kb_manager.get_kb_by_id(self.kb_id)

                if kb_info and kb_info.get('doc_count', 0) == 0:
                    self.logger.warning(f"Knowledge Base '{kb_info.get('name')}' is empty. Skipping vector retrieval.")
                    pass
                elif kb_info:
                    self.sig_token.emit("<i>Loading local vector model and retrieving literature...</i>\n\n")
                    domain = kb_info.get('domain', 'General Academic')
                    model_id = kb_info.get('model_id', 'embed_auto')


                    user_pref = self.config.user_settings.get("inference_device", "Auto")
                    target_device = DeviceManager().parse_device_string(user_pref)

                    conf = get_model_conf(model_id, "embedding")
                    if not conf or conf.get('is_auto'):
                        real_id = resolve_auto_model("embedding", target_device)
                        conf = get_model_conf(real_id, "embedding")

                    try:
                        embed_fn = _worker_load_model(self.kb_id, self.config)
                        if not self.db.switch_kb(self.kb_id, embedding_function=embed_fn):
                            self.sig_error.emit(f"Failed to switch to Knowledge Base: {self.kb_id}")
                            return
                    except Exception as e:
                        self.sig_error.emit(f"Critical Model Error: {str(e)}")
                        return

                    history_context = ""
                    if len(self.messages) >= 3:
                        prev_assistant = self.messages[-2]['content'][:100]
                        history_context = f" (Context: {prev_assistant})"

                    expanded_queries = [
                        search_query,
                        f"{search_query}{history_context}",
                        f"{domain} context: {search_query} research details"
                    ]

                    candidate_docs = []
                    seen_contents = set()

                    for eq in expanded_queries:
                        raw_results = self.db.query(eq, n_results=20)
                        if raw_results and raw_results.get('documents') and raw_results['documents'][0]:
                            docs = raw_results['documents'][0]
                            metas = raw_results['metadatas'][0]
                            distances = raw_results.get('distances', [[0] * len(docs)])[0]

                            for i, doc_text in enumerate(docs):
                                clean_text = doc_text.strip()
                                if clean_text not in seen_contents and len(clean_text) > 20:
                                    seen_contents.add(clean_text)
                                    candidate_docs.append({
                                        "content": clean_text,
                                        "metadata": metas[i],
                                        "v_dist": distances[i]
                                    })

                    if candidate_docs:
                        candidate_docs = sorted(candidate_docs, key=lambda x: x.get('v_dist', 0))[:40]
                        final_docs = self._process_rerank(search_query, candidate_docs, domain, 10)

                        current_ref_id = 1
                        for doc in final_docs:
                            sources_map[current_ref_id] = {
                                "path": doc['metadata'].get('file_path', ''),
                                "page": doc['metadata'].get('page', 1),
                                "name": doc['metadata'].get('source', 'Local DB'),
                                "search_text": doc['content'][:100]
                            }
                            context_str += (
                                f"--- [Document {current_ref_id}] ---\n"
                                f"Source: {doc['metadata'].get('source', 'Local')} (Page {doc['metadata'].get('page', 1)})\n"
                                f"Content: {doc['content']}\n\n"
                            )
                            current_ref_id += 1

            if not context_str.strip():
                context_str = "No local database documents provided."

            # Phase 3: Dynamic External Context (Multimodal & On-the-fly Reranking)
            external_chunks = getattr(self, 'external_context', [])
            images = [c for c in external_chunks if c.get("type") == "image" or str(c.get("path", "")).lower().endswith(
                ('.png', '.jpg', '.jpeg', '.webp'))]
            docs = [c for c in external_chunks if c not in images]

            llm_content = []

            # 3.1 处理上传的长文本 / PDF (启用 Reranker 降低幻觉)
            if docs:
                self.sig_token.emit("<i>Filtering and reranking attached documents...</i>\n\n")
                cand_docs = [{"content": d.get("content", ""),
                              "metadata": {"name": d.get("name", "Unknown"), "page": d.get("page", 1)}} for d in docs]

                if len(cand_docs) > 5:
                    cand_docs = self._process_rerank(search_query, cand_docs, "General", 8)

                files_dict = {}
                for doc in cand_docs:
                    f_name = doc["metadata"]["name"]
                    page = doc["metadata"]["page"]
                    if f_name not in files_dict:
                        files_dict[f_name] = ""
                    files_dict[f_name] += f"\n[Page {page}]\n{doc['content']}"

                # 按照你的要求，以 JSON 格式封装文本内容
                docs_json = json.dumps(files_dict, ensure_ascii=False)
                llm_content.append({"type": "text", "text": f"User Uploaded Files (JSON Format):\n{docs_json}\n\n"})

            # 3.2 正常用户文字输入
            llm_content.append({"type": "text", "text": f"User Query:\n{search_query}"})

            # 3.3 处理多图顺序挂载
            for img in images:
                # 兼容不同来源的 base64 key
                img_data = img.get("base64_url") or img.get("content")
                if img_data:
                    # 确保前缀符合 OpenAI 视觉标准
                    if not img_data.startswith("data:image"):
                        ext = str(img.get("path", ".jpeg")).split('.')[-1]
                        img_data = f"data:image/{ext};base64,{img_data}"

                    llm_content.append({
                        "type": "image_url",
                        "image_url": {"url": img_data}
                    })

            self.sig_token.emit("[CLEAR_SEARCH]")

            # Phase 4: Low VRAM Release
            is_low_vram = self.config.user_settings.get("low_vram_mode", False)
            if is_low_vram:
                self.sig_token.emit("<i>[Low VRAM Mode] Unloading RAG models to free up memory for LLM...</i>\n\n")
                if hasattr(self, 'reranker') and getattr(self.reranker, 'model', None) is not None:
                    self.reranker.model = None
                if hasattr(self, 'db') and self.db:
                    self.db.reload()
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

            # ==========================================
            # Phase 5: Agentic Generation
            # ==========================================
            self.sig_token.emit("[START_LLM_NETWORK]")

            mcp_mgr = MCPManager.get_instance()
            mcp_tools = None
            if self.use_mcp:
                selected_tags = getattr(self, 'selected_mcp_tags', None)
                if selected_tags is not None:
                    mcp_tools = mcp_mgr.get_tools_schema_by_tags(selected_tags)
                else:
                    mcp_tools = mcp_mgr.get_all_tools_schema()

            system_prompt = (
                f"You are a Senior Research Scientist specializing in {domain}. "
                "Your goal is to provide high-density, evidence-based academic responses.\n\n"

                "### TOOL USE PROTOCOL (STRICT):\n"
                "1. If the provided Context is insufficient, invoke tools IMMEDIATELY.\n"
                "2. SILENT EXECUTION: Never output your reasoning process for choosing a tool.\n\n"
                "3. If graphics need to be created, use mermaid uniformly.\n\n"

                "### RESPONSE GUIDELINES:\n"
                "1.GROUNDING (CRITICAL RULE): You MUST append inline citations (e.g., [1], [2]) at the end of every sentence that uses information from the Context. NEVER claim facts without appending the corresponding document number. Failure to cite will result in penalties.\n\n"

                "### FOLLOW-UP STRUCTURE (MANDATORY):\n"
                "At the very end of your response, you MUST output the exact string [FOLLOW_UPS] followed by exactly 6 follow-up questions using this EXACT format:\n"
                "[FOLLOW_UPS]\n"
                "💡 Suggested Follow-ups:\n"
                "   - [Deep Dive] <Question about specific details or mechanisms>\n"
                "   - [Critical] <Question about limitations, alternatives, or weaknesses>\n"
                "   - [Broader] <Question about implications or future trends>\n"
                "   - [Brainstorm] <A creative brainstorming question or hypothetical \"What if\" scenario>\n"
                "   - [Similar] <Question connecting to a similar or parallel topic/concept>\n"
                "   - [Application] <Question about real-world applications or cross-disciplinary use>\n\n"

                f"### CONTEXT:\n{context_str}"
            )

            # 使用列表结构替换掉尾部用户输入，支持多模态
            rag_messages = [{"role": "system", "content": system_prompt}] + self.messages[:-1]
            rag_messages.append({"role": "user", "content": llm_content})

            tool_executed = False

            # MCP 循环
            if mcp_tools:
                try:
                    MAX_ITERATIONS = 5
                    for iteration in range(MAX_ITERATIONS):
                        response_msg = self.main_llm.chat(
                            messages=rag_messages,
                            tools=mcp_tools,
                            tool_choice="auto"
                        )


                        if isinstance(response_msg, dict):
                            tool_calls = response_msg.get('tool_calls')
                        else:

                            tool_calls = getattr(response_msg, 'tool_calls', None)
                            response_msg = {
                                "role": getattr(response_msg, "role", "assistant"),
                                "content": getattr(response_msg, "content", ""),
                                "tool_calls": [
                                    {
                                        "id": tc.id,
                                        "type": tc.type,
                                        "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                                    } for tc in tool_calls
                                ] if tool_calls else None
                            }

                        if not tool_calls:
                            break

                        tool_executed = True


                        if isinstance(response_msg, dict) and response_msg.get("tool_calls"):
                            response_msg["tools"] = response_msg["tool_calls"]

                        rag_messages.append(response_msg)

                        for tool_call in tool_calls:
                            tool_name = tool_call['function']['name']
                            try:
                                tool_args = json.loads(tool_call['function']['arguments'])
                                self.sig_token.emit(f"<i>📡 Requesting MCP server: {tool_name}...</i>\n\n")
                                tool_result = mcp_mgr.call_tool_sync(tool_name, tool_args)
                            except Exception as e:
                                self.logger.error(f"MCP tool {tool_name} failed: {e}")
                                tool_result = f"Tool execution failed: {str(e)}"

                            rag_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call['id'],
                                "name": tool_name,
                                "content": tool_result
                            })

                    if tool_executed:
                        self.sig_token.emit(
                            "<i>All data retrieved successfully. Conducting comprehensive analysis...</i>\n\n")
                except Exception as e:
                    self.logger.warning(f"Tool calling loop failed: {e}")

            if tool_executed:
                silence_prompt = "CRITICAL: Tools executed. Provide final answer now. No JSON argument blocks. Remember the [FOLLOW_UPS] format."
                rag_messages.append({"role": "system", "content": silence_prompt})

            # --- LLM Output Streaming ---
            for token in self.main_llm.stream_chat(rag_messages):
                self.full_response_cache += token
                self.sig_token.emit(token)

            # ==========================================
            # Phase 6: Dynamic Citation Mounting
            # ==========================================
            has_citation = bool(re.search(r'\[\d+\]', self.full_response_cache))
            if sources_map and has_citation:
                ref_html = "\n<br><hr style='border:0; height:1px; background:#444; margin:15px 0;'><b>📚 Cited Sources:</b><br>"
                used_indices = set(int(ref) for ref in re.findall(r'\[(\d+)\]', self.full_response_cache))
                displayed = 0
                for rid, info in sources_map.items():
                    if rid in used_indices:
                        safe_path = quote(info['path'])
                        safe_text = quote(info['search_text'])
                        safe_name = quote(info['name'])

                        link = f"cite://view?path={safe_path}&page={info['page']}&text={safe_text}&name={safe_name}"
                        ref_html += f"<div style='margin-bottom: 5px;'>▪ <a style='color:#05B8CC; text-decoration:none;' href='{link}'><b>[{rid}]</b> {info['name']} (Page {info['page']})</a></div>"
                        displayed += 1
                if displayed > 0:
                    self.sig_token.emit(ref_html)

        except Exception as e:
            self.sig_error.emit(f"Error: {str(e)}\n{traceback.format_exc()}")
        finally:
            self.sig_finished.emit()


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

        if hasattr(GlobalSignals(), 'sig_send_to_chat'):
            GlobalSignals().sig_send_to_chat.connect(self.handle_external_send)

        if hasattr(GlobalSignals(), 'sig_route_to_chat_with_mcp'):
            GlobalSignals().sig_route_to_chat_with_mcp.connect(self.handle_external_send_with_mcp)

    def get_ui_widget(self) -> QWidget:
        if self.widget: return self.widget

        # 1. 主容器与全局布局
        self.widget = ChatDropTargetWidget()
        self.widget.sig_files_dropped.connect(self.process_attached_files)

        main_layout = QVBoxLayout(self.widget)
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(10, 10, 10, 10)

        top_bar = QVBoxLayout()
        top_bar.setSpacing(8)
        top_bar.setContentsMargins(0, 0, 0, 10)

        row1_layout = QHBoxLayout()
        self.model_selector = ModelSelectorWidget(label_text=" Main Model:", config_key="chat_llm_id",
                                                  model_key="chat_model_name")
        row1_layout.addWidget(self.model_selector)
        row1_layout.addStretch()

        row2_layout = QHBoxLayout()
        self.trans_selector = ModelSelectorWidget(label_text=" Translator:", config_key="chat_trans_llm_id",
                                                  model_key="chat_trans_model_name")

        lbl_kb = QLabel(" Knowledge Base:")
        self.combo_kb = BaseComboBox(max_width=400)
        self.refresh_kb_list()

        row2_layout.addWidget(self.trans_selector)
        row2_layout.addSpacing(15)
        row2_layout.addWidget(lbl_kb)
        row2_layout.addWidget(self.combo_kb)
        row2_layout.addStretch()

        top_bar.addLayout(row1_layout)
        top_bar.addLayout(row2_layout)
        main_layout.addLayout(top_bar)

        # 加载配置
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
        self.btn_scroll_bottom.clicked.connect(self.scroll_to_bottom)
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

    def attach_from_local(self):
        """按钮点击触发的文件选择器"""
        paths, _ = QFileDialog.getOpenFileNames(
            self.widget, "Select Document(s) or Image(s)", "",
            "All Supported (*.pdf *.md *.txt *.csv *.py *.png *.jpg *.jpeg *.webp *.gif *.bmp);;"
            "Images (*.png *.jpg *.jpeg *.webp *.gif *.bmp);;"
            "Documents (*.pdf *.md *.txt *.csv *.py)"
        )
        if not paths: return
        self.process_attached_files(paths)

    def process_attached_files(self, items):
        if not hasattr(self, 'external_chunks'):
            self.external_chunks = []
        if not hasattr(self, 'external_context_html'):
            self.external_context_html = ""


        file_infos = []
        for item in items:
            if isinstance(item, str):
                file_infos.append({"path": item, "name": os.path.basename(item)})
            elif isinstance(item, dict):
                file_infos.append(item)

        # 防止用户重复狂点
        self.input_container.set_uploading(True)
        self.input_container.show_context_preview("Loading files into memory...")

        if hasattr(self, 'attach_task_mgr'):
            self.attach_task_mgr.cancel_task()

        self.attach_task_mgr = TaskManager()

        self.attach_task_mgr.sig_progress.connect(
            lambda p, m: self.input_container.show_context_preview(f"⏳ {m}")
        )
        self.attach_task_mgr.sig_result.connect(self._on_attachment_result)
        self.attach_task_mgr.sig_state_changed.connect(self._on_attachment_state_changed)

        self.attach_task_mgr.start_task(
            ProcessAttachmentTask,
            task_id="process_attachment",
            mode=TaskMode.THREAD,
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
            if hasattr(self.input_container, 'chk_mcp_enable'):
                self.input_container.chk_mcp_enable.setEnabled(enabled)
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
        if state == TaskState.FAILED.value:
            self.input_container.set_uploading(False)
            self.input_container.hide_context_preview()
            ToastManager().show(f"Attachment failed: {msg}", "error")

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

        path, ext = QFileDialog.getSaveFileName(
            self.widget, "导出学术记录 (Export Academic Log)", "Scholar_Navis_Log",
            "PDF Document (*.pdf);;Text File (*.txt);;CSV Data (*.csv)"
        )
        if not path: return

        try:
            tm = ThemeManager()
            font_family = tm.font_family()

            if path.endswith(".pdf"):
                doc = QTextDocument()
                date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                doc.setDefaultStyleSheet(f"""
                    body {{ font-family: {font_family}; font-size: 10.5pt; line-height: 1.6; color: #24292e; background-color: #ffffff; }}
                    h1, h2, h3 {{ color: {tm.color('title_blue')}; border-bottom: 1px solid #eaecef; padding-bottom: 4px; }}
                    .msg-box {{ margin-bottom: 25px; padding-bottom: 15px; border-bottom: 1px dashed #dddddd; page-break-inside: avoid; }}
                    .header-user {{ color: {tm.color('academic_blue')}; font-weight: bold; font-size: 12pt; margin-bottom: 8px; }}
                    .header-ai {{ color: {tm.color('success')}; font-weight: bold; font-size: 12pt; margin-bottom: 8px; }}
                    .content {{ margin-top: 5px; }}
                    pre {{ background-color: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 4px; padding: 12px; white-space: pre-wrap; font-family: Consolas, "Courier New", monospace; font-size: 9.5pt; }}
                    code {{ font-family: Consolas, "Courier New", monospace; background-color: #f3f4f6; padding: 2px 4px; border-radius: 3px; color: #d73a49; font-size: 9.5pt; }}
                    pre code {{ background-color: transparent; padding: 0; color: #24292e; }}
                    blockquote {{ border-left: 4px solid #dfe2e5; color: #6a737d; padding-left: 15px; margin-left: 0; }}
                    table {{ border-collapse: collapse; width: 100%; margin-top: 10px; margin-bottom: 10px; }}
                    th, td {{ border: 1px solid #dfe2e5; padding: 8px 12px; text-align: left; }}
                    th {{ background-color: #f6f8fa; font-weight: bold; }}
                    .doc-header {{ text-align: center; border-bottom: 2px solid {tm.color('title_blue')}; padding-bottom: 15px; margin-bottom: 30px; }}
                    .doc-title {{ font-size: 22pt; font-weight: bold; color: {tm.color('title_blue')}; font-family: 'Segoe UI', sans-serif; }}
                    .doc-meta {{ font-size: 10pt; color: #586069; margin-top: 5px; }}
                """)


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

                user_icon_b64 = _get_colored_svg_base64("user", tm.color('academic_blue'))
                ai_icon_b64 = _get_colored_svg_base64("ai_model", tm.color('success'))

                html = f"""
                <html><body>
                <div class='doc-header'>
                    <div class='doc-title'>Scholar Navis - Analysis Report</div>
                    <div class='doc-meta'>Generated on: {date_str} | Document Type: Academic Chat Log</div>
                </div>
                """

                for msg in self.history:
                    is_user = (msg['role'] == "user")
                    clean_content = TextFormatter.clean_text_for_export(msg['content'])
                    rendered_html = markdown.markdown(
                        clean_content,
                        extensions=['extra', 'nl2br', 'tables', 'fenced_code']
                    )

                    if is_user:
                        header = f"<div class='header-user'><img src='{user_icon_b64}' width='16' height='16' style='vertical-align:middle;'> User Inquiry</div>"
                    else:
                        header = f"<div class='header-ai'><img src='{ai_icon_b64}' width='16' height='16' style='vertical-align:middle;'> AI Analysis</div>"

                    html += f"<div class='msg-box'>{header}<div class='content'>{rendered_html}</div></div>"

                html += "</body></html>"
                doc.setHtml(html)

                writer = QPdfWriter(path)
                writer.setPageSize(QPageSize(QPageSize.A4))
                writer.setPageMargins(QMarginsF(15, 20, 15, 20))
                writer.setResolution(300)
                doc.print_(writer)

            elif path.endswith(".txt"):
                txt = f"================ SCHOLAR NAVIS REPORT ================\nGenerated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                for msg in self.history:
                    role = "User Inquiry" if msg['role'] == "user" else "AI Analysis"
                    clean_content = TextFormatter.clean_text_for_export(msg['content'])
                    txt += f"[{role}]:\n{clean_content}\n\n{'-' * 60}\n\n"
                with open(path, "w", encoding="utf-8") as f:
                    f.write(txt)

            elif path.endswith(".csv"):
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["Role", "Content"])
                    for msg in self.history:
                        clean_content = TextFormatter.clean_text_for_export(msg['content'])
                        writer.writerow(["User" if msg['role'] == 'user' else "AI", clean_content])

            ToastManager().show(f"Document successfully exported to: {os.path.basename(path)}", "success")
            self.logger.info(f"Chat history successfully exported to: {path}")

        except Exception as e:
            ToastManager().show(f"Failed to export document: {str(e)}", "error")
            self.logger.error(f"Failed to export document: {str(e)}")


    def clear_follow_up_shelf(self):
        while self.follow_up_shelf_layout.count() > 0:
            item = self.follow_up_shelf_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.follow_up_shelf.setVisible(False)

    def clear_chat_history(self):
        self.cancel_generation()  # 这里会触发文本还原
        self.current_ai_bubble = None
        self.history.clear()
        self.clear_layout(self.chat_layout)

        self.clear_follow_up_shelf()

        self.input_container.unlock_input()

        self.input_container.clear_text()
        self.clear_attached_context()

        ToastManager().show("Chat history cleared.", "success")


    def scroll_to_user_message(self, bubble_widget):
        QApplication.processEvents()

        target_y = max(0, bubble_widget.y() - 10)
        self.scroll_area.verticalScrollBar().setValue(target_y)

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

        # 2. 模型拦截校验
        if kb_id != "none":
            ready, missing_label, missing_id, m_type = ModelManager().verify_chat_models(kb_id)
            if not ready:
                msg = (
                    f"<b>Model Missing - Action Blocked</b><br><br>"
                    f"Required offline model is not installed: <br>"
                    f"<font color='#ff6b6b'>• {missing_label}</font><br><br>"
                    f"Please go to <b>[Global Settings]</b> and click 'Save' to download required models."
                )
                StandardDialog(self.widget, "Offline Security Intercept", msg, show_cancel=False).exec()
                GlobalSignals().request_model_download.emit(missing_id, m_type)
                return

        trans_config = self.trans_selector.get_current_config()
        if trans_config:
            trans_config = trans_config.copy()
            trans_config["model_name"] = trans_config.get("trans_model_name", trans_config.get("model_name"))

        is_english = True
        try:
            detected_lang = detect(text)
            is_english = (detected_lang == 'en')
        except Exception:
            is_english = True

        requires_translation = (not is_english) and (trans_config is not None)

        if not is_english and trans_config is None:
            if not getattr(self.__class__, '_has_shown_lang_warning', False):
                ToastManager().show(
                    "Non-English input detected, but translation model is not enabled. \nThe core model may not perfectly handle this language; please verify the accuracy of the results.",
                    "warning"
                )
                self.__class__._has_shown_lang_warning = True

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
                [f"--- {c['name']} (Page {c['page']}) ---\n{c['content']}" for c in current_chunks]
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
        self.start_ai_response(kb_id, requires_translation)

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
            bubble.lbl_text.linkActivated.connect(self.handle_link_click)

        self.chat_layout.addWidget(bubble)

        if not getattr(self, '_is_editing', False):
            if is_user:
                QTimer.singleShot(50, lambda: self.scroll_to_user_message(bubble))
            else:
                QTimer.singleShot(50, self.scroll_to_bottom)

        return bubble


    def scroll_to_bottom(self):
        sb = self.scroll_area.verticalScrollBar()
        sb.setValue(sb.maximum())


    def start_ai_response(self, kb_id, requires_translation=False):
        if getattr(self, 'worker_thread', None) is not None:
            try:
                if getattr(self, 'worker', None):
                    self.worker.cancel()
                    try:
                        self.worker.sig_token.disconnect()
                    except Exception:
                        pass
                if self.worker_thread.isRunning():
                    if not hasattr(self, '_orphaned_threads'): self._orphaned_threads = []
                    old_t, old_w = self.worker_thread, self.worker
                    old_t.quit()
                    self._orphaned_threads.append((old_t, old_w))
                    old_t.finished.connect(lambda t=old_t, w=old_w: self._orphaned_threads.remove((t, w)) if (t, w) in getattr(self, '_orphaned_threads', []) else None)
            except RuntimeError:
                pass

            self.worker_thread = None
            self.worker = None

        # 获取最新的主模型配置与翻译配置
        main_config = self.model_selector.get_current_config()
        trans_config = self.trans_selector.get_current_config()
        use_mcp_tools = self.input_container.chk_mcp_enable.isChecked() if hasattr(self.input_container, 'chk_mcp_enable') else False
        selected_mcp_tags = self.input_container.get_selected_tags() if use_mcp_tools else []

        def _clean_model_name(name):
            if not name: return ""
            for suffix in [" [Custom]", " [Closed]"]:
                if name.endswith(suffix): return name[:-len(suffix)]
            return name


        if main_config:
            main_config = main_config.copy()
            combos_main = self.model_selector.findChildren(QComboBox)
            if len(combos_main) >= 2:
                raw_ui_model = combos_main[1].currentText()
            else:
                raw_ui_model = self.config.user_settings.get("chat_model_name", "")

            ui_model = _clean_model_name(raw_ui_model)
            if ui_model:
                main_config["model_name"] = ui_model
                self.config.user_settings["chat_model_name"] = raw_ui_model
                self.config.save_settings()

        if trans_config:
            trans_config = trans_config.copy()
            combos_trans = self.trans_selector.findChildren(QComboBox)
            if len(combos_trans) >= 2:
                raw_ui_trans = combos_trans[1].currentText()
            else:
                raw_ui_trans = self.config.user_settings.get("chat_trans_model_name", "")

            ui_trans = _clean_model_name(raw_ui_trans)
            if ui_trans:
                trans_config["model_name"] = ui_trans
                self.config.user_settings["chat_trans_model_name"] = raw_ui_trans
                self.config.save_settings()


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
        self.set_controls_enabled(False)

        self._is_rendering_dirty = False
        self._render_timer.start()

        # 实例化后台 Worker
        self.worker_thread = QThread()

        self.worker = ChatWorker(
            main_config=main_config,
            trans_config=trans_config,
            messages=list(self.history),
            kb_id=kb_id,
            requires_translation=requires_translation,
            external_context=getattr(self, 'external_chunks', []),
            use_mcp=use_mcp_tools
        )

        self.worker.selected_mcp_tags = selected_mcp_tags
        self.external_chunks = []
        self.external_context_html = ""
        self.input_container.hide_context_preview()
        self.worker.moveToThread(self.worker_thread)

        try:
            self.input_container.btn_stop.clicked.disconnect()
        except:
            pass
        self.input_container.btn_stop.clicked.connect(self.cancel_generation)

        self.worker_thread.started.connect(self.worker.run)
        self.worker.sig_token.connect(self.update_ai_bubble)
        self.worker.sig_finished.connect(self.on_chat_finished)
        self.worker.sig_error.connect(self.on_chat_error)
        self.worker.sig_finished.connect(self.worker_thread.quit)
        self.worker.sig_finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)

        GlobalSignals().sig_toast.connect(lambda msg, lvl: ToastManager().show(msg, lvl))
        self.worker.sig_finished.connect(self.worker_thread.quit)
        self.worker_thread.start()

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
                [f"--- {c['name']} (Page {c['page']}) ---\n{c['content']}" for c in old_chunks]
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
        if hasattr(self, 'worker') and self.worker:
            self.worker.cancel()
            try:
                self.worker.sig_token.disconnect()
            except Exception:
                pass

        try:
            if getattr(self, 'worker_thread', None) is not None and self.worker_thread.isRunning():
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

        if hasattr(self, '_render_timer'): self._render_timer.stop()
        self.set_controls_enabled(True)

        if self.current_ai_bubble and self.current_ai_bubble.is_loading:
            self.current_ai_bubble.set_loading(False)

        tm = ThemeManager()
        self.current_ai_text += f"\n\n<div style='color:{tm.color('warning')}; font-weight:bold;'>[Generation Cancelled by User]</div>"

        if self.current_ai_bubble:
            idx = getattr(self.current_ai_bubble, 'index', -1)
            self.current_ai_bubble.set_content(self._format_response(self.current_ai_text, idx))

        self.input_container.btn_stop.setVisible(False)
        self.input_container.btn_send.setVisible(True)

        if hasattr(self, '_restore_last_input'):
            self._restore_last_input()


        self.logger.info("AI generation cancelled by user.")
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

        if hasattr(self, 'input_container') and hasattr(self.input_container, 'chk_mcp_enable'):
            if not self.input_container.chk_mcp_enable.isChecked():
                self.input_container.chk_mcp_enable.setChecked(True)

        config_mgr = ConfigManager()
        available_tags = MCPManager.get_instance().get_available_tags()

        deselected = set(self.config.mcp_servers.get("deselected_mcp_tags", []))
        for tag in available_tags:
            if tag.lower() == target_tag.lower():
                deselected.discard(tag)
            else:
                deselected.add(tag)

        self.config.mcp_servers["deselected_mcp_tags"] = list(deselected)
        self.config.save_mcp_servers()

        if hasattr(self, 'input_container') and hasattr(self.input_container, 'refresh_mcp_tags'):
            self.input_container.refresh_mcp_tags()

        self.handle_external_send(context_text, prompt_text)

    def handle_external_send(self, context_text, prompt_text=""):

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

        # 提取前 80 个字符做预览，去掉换行符保持气泡紧凑
        preview_text = context_text[:80].replace('\n', ' ') + "..."
        self.external_context_html = f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#05B8CC; text-decoration:none;'>📄 {preview_text} (Click to read more)</a></div>"

        if hasattr(self, 'input_container') and hasattr(self.input_container, 'chk_mcp_enable'):
            if not self.input_container.chk_mcp_enable.isChecked():
                self.input_container.chk_mcp_enable.setChecked(True)

        p = self.widget
        while p:
            if hasattr(p, 'setCurrentWidget'):
                try:
                    p.setCurrentWidget(self.widget)
                    main_window = p.window()
                    if hasattr(main_window, 'sidebar'):
                        idx = p.indexOf(self.widget)
                        if idx >= 0:
                            main_window.sidebar.setCurrentRow(idx)
                except:
                    pass
            p = p.parentWidget()

        if self.widget.window():
            self.widget.window().showNormal()
            self.widget.window().raise_()
            self.widget.window().activateWindow()

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

        # 1. Handle clearing of status prompts via Regex
        if token == "[CLEAR_SEARCH]":
            self.current_ai_text = re.sub(r'<i>.*?</i>(?:\n\n)?', '', self.current_ai_text, flags=re.DOTALL)
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

        # --- 翻译或生成失败处理 ---
        # 显示重试按钮
        self.input_container.btn_stop.setVisible(False)
        self.input_container.btn_send.setVisible(True)
        self._restore_last_input()

        display_error = msg
        if "translation" in msg.lower() or "translator" in msg.lower():
            ToastManager().show(f"Translation model error; conversation terminated: {msg}", "error")
            display_error = f"Translation model error: {msg}"
        elif "time" in msg.lower() or "connect" in msg.lower():
            ToastManager().show("Network connection failed. Please check your API configuration or proxy settings.",
                                "error")
            display_error = "Network connection failed."

        if self.current_ai_bubble and self.current_ai_bubble.is_loading:
            self.current_ai_bubble.set_loading(False)

        self.current_ai_text += f"\n\n<div style='color:#ff6b6b; font-weight:bold;'>[AI Error]</div>\n<div style='color:#888; font-size:12px;'>{display_error}</div>"

        if self.current_ai_bubble:
            idx = getattr(self.current_ai_bubble, 'index', -1)
            self.current_ai_bubble.set_content(self._format_response(self.current_ai_text, idx))

        self.scroll_to_bottom()

    def on_chat_finished(self):

        if hasattr(self, '_render_timer'): self._render_timer.stop()
        self.set_controls_enabled(True)

        # 强制进行最后一次全量渲染，确保不丢掉最后的 token
        if getattr(self, '_is_rendering_dirty', False):
            self._throttled_render()


        if not self.current_ai_bubble:
            return

        self.input_container.btn_stop.setVisible(False)
        self.input_container.btn_send.setVisible(True)
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

        match = re.search(r'\[FOLLOW_UPS\]\s*(.*)', full_text, flags=re.IGNORECASE | re.DOTALL)
        questions = []

        if match:
            follow_up_block = match.group(1)
            clean_text = full_text[:match.start()].strip()
            self.current_ai_text = clean_text + cites_html

            for line in follow_up_block.split('\n'):
                line = line.strip()
                if line.startswith('-') or line.startswith('*'):
                    q = line.lstrip('-* ').strip()
                    if q and q.startswith('['):
                        tag_match = re.match(r'^\[(.*?)\]\s*(.*)', q)
                        if tag_match:
                            tag, text = tag_match.groups()
                            questions.append({"tag": tag.strip(), "text": text.strip()})
                        else:
                            questions.append({"tag": "General", "text": q})

            idx = getattr(self.current_ai_bubble, 'index', -1)
            final_html = self._format_response(self.current_ai_text, idx)
            self.current_ai_bubble.set_content(final_html)

            if questions:
                self.render_follow_up_buttons(questions)
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
        if not self.history:
            return

        curr_data = self.combo_kb.currentData()
        curr_id = curr_data.get("id") if isinstance(curr_data, dict) else curr_data

        # 如果当前正在聊天的库，刚好就是后台被增删改的库
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




    def handle_link_click(self, url_str):

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
            page_num = int(params.get('page', ['1'])[0]) - 1
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
                    ToastManager().show(f"已打开文档，位于第 {page_num + 1} 页", "success")
                elif ext in ['md', 'txt', 'csv', 'json', 'py']:  # 常见纯文本格式拦截
                    if not hasattr(self, 'text_viewer') or self.text_viewer is None:
                        self.text_viewer = InternalTextViewer(None)
                    self.text_viewer.load_document(target_path, text_snippet, display_name=source_name)
                    ToastManager().show(f"已打开文档片段", "success")
                else:
                    # 对于图片或我们无法渲染的格式，降级交给操作系统处理
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

    def _check_scroll_position(self):
        sb = self.scroll_area.verticalScrollBar()
        should_show = (sb.maximum() - sb.value() > 200)

        # 防止动画重复触发
        if should_show and not self.btn_scroll_bottom.isVisible():
            self.btn_scroll_bottom.setVisible(True)
            self.fade_anim.stop()
            self.fade_anim.setStartValue(self.opacity_effect.opacity())
            self.fade_anim.setEndValue(1.0)

            try:
                self.fade_anim.finished.disconnect()
            except:
                pass

            self.fade_anim.start()

        elif not should_show and self.btn_scroll_bottom.isVisible():
            if self.fade_anim.endValue() != 0.0:
                self.fade_anim.stop()
                self.fade_anim.setStartValue(self.opacity_effect.opacity())
                self.fade_anim.setEndValue(0.0)

                try:
                    self.fade_anim.finished.disconnect()
                except:
                    pass
                self.fade_anim.finished.connect(self.btn_scroll_bottom.hide)

                self.fade_anim.start()

    def _save_setting(self, key, value):
        self.config.user_settings[key] = value
        self.config.save_settings()

    def execute_task(self):
        pass
