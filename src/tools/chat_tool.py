import base64
import functools
import gc
import hashlib
import json
import logging
import mimetypes
import os
import re
import traceback
from urllib.parse import urlparse, parse_qs, quote

import fitz
import pymupdf4llm
import torch
from PySide6.QtCore import Qt, Signal, QObject, QThread, QUrl, QTimer, QPropertyAnimation
from PySide6.QtGui import QDesktopServices, QCursor, QAction
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout,
                               QPlainTextEdit, QPushButton, QLabel,
                               QScrollArea, QFrame, QFileDialog, QMenu, QDialog,
                               QAbstractItemView, QListWidget, QListWidgetItem, QDialogButtonBox, QCheckBox,
                               QToolButton, QWidgetAction, QSizePolicy, QGraphicsOpacityEffect)
from chromadb.utils import embedding_functions
from huggingface_hub import snapshot_download
from langdetect import detect

from src.core.config_manager import ConfigManager
from src.core.core_task import TaskManager, TaskMode, TaskState
from src.core.database import DatabaseManager
from src.core.device_manager import DeviceManager
from src.core.kb_manager import KBManager
from src.core.llm_impl import OpenAICompatibleLLM
from src.core.mcp_manager import MCPManager
from src.core.models_registry import get_model_conf, resolve_auto_model, ModelManager
from src.core.network_worker import NetworkEmbeddingFunction, NetworkRerankerFunction
from src.core.rerank_engine import RerankEngine
from src.core.signals import GlobalSignals
from src.services.file_service import FileService
from src.task.chat_tasks import ProcessAttachmentTask
from src.task.kb_tasks import _worker_load_model
from src.tools.base_tool import BaseTool
from src.tools.settings_tool import FloatingOverlayFilter
from src.ui.components.chat_bubble import ChatBubbleWidget
from src.ui.components.combo import BaseComboBox
from src.ui.components.dialog import StandardDialog
from src.ui.components.model_selector import ModelSelectorWidget
from src.ui.components.pdf_viewer import InternalPDFViewer, InternalTextViewer
from src.ui.components.pill_button import FollowUpPillButton
from src.ui.components.text_formatter import TextFormatter
from src.ui.components.toast import ToastManager


@functools.lru_cache(maxsize=128)
def get_cached_translation(text, direction="to_en", llm_instance=None):
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
    ]).strip()


class ChatDropTargetWidget(QWidget):
    """支持全局拖拽上传文件的容器，并带有视觉叠加层"""
    sig_files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

        # 拖拽时的叠加提示层
        self.overlay = QLabel("📥 Drop files here to attach", self)
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
            QPlainTextEdit { background-color: transparent; color: #eeeeee; border: none; font-size: 14px; }
            QScrollBar:vertical { background: #2b2b2b; width: 6px; }
        """)
        self.setFixedHeight(40)
        self.max_height = 200
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.textChanged.connect(self.adjust_height)

    def adjust_height(self):
        doc_height = int(self.document().size().height())
        margins = self.contentsMargins()
        new_height = min(max(doc_height + margins.top() + margins.bottom() + 10, 40), self.max_height)
        self.setFixedHeight(new_height)
        # 达到最高时开启滚动条
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if new_height >= self.max_height else Qt.ScrollBarAlwaysOff)

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

        self.lbl_context_info = QLabel("📎 Context Attached")
        self.lbl_context_info.setStyleSheet("color: #05B8CC; font-size: 12px; border: none;")
        self.btn_clear_context = QPushButton("✖")
        self.btn_clear_context.setCursor(Qt.PointingHandCursor)
        self.btn_clear_context.setStyleSheet(
            "QPushButton { color: #ff6b6b; border: none; font-weight: bold; background: transparent; } QPushButton:hover { color: #ff4c4c; }")
        self.btn_clear_context.clicked.connect(self.sig_clear_context_clicked.emit)

        banner_layout.addWidget(self.lbl_context_info)
        banner_layout.addStretch()
        banner_layout.addWidget(self.btn_clear_context)
        main_layout.addWidget(self.context_banner)

        self.mcp_toolbar = QHBoxLayout()
        self.chk_mcp_enable = QCheckBox("🚀 Enable advanced search (MCP Tools - Requires model support)")
        self.chk_mcp_enable.setStyleSheet("color: #05B8CC; font-weight: bold;")
        self.chk_mcp_enable.setChecked(False)

        self.btn_mcp_tags = QToolButton()
        self.btn_mcp_tags = QPushButton("🏷️ Filter Tools: Loading...")
        self.btn_mcp_tags.setCursor(Qt.PointingHandCursor)
        self.btn_mcp_tags.setStyleSheet(
            "QPushButton { color: #aaaaaa; background: transparent; border: 1px solid #555; border-radius: 4px; padding: 2px 8px; }"
            "QPushButton:hover { background: #333; }"
        )
        self.btn_mcp_tags.setVisible(False)

        self.menu_mcp_tags = QMenu(self)
        self.menu_mcp_tags.setStyleSheet(
            "QMenu { background-color: #2b2b2b; border: 1px solid #555; border-radius: 6px; padding: 4px; }"
        )
        self.btn_mcp_tags.clicked.connect(self._show_filter_menu)

        self.tag_actions = {}
        self.user_deselected_tags = set()
        self.known_tags = set()

        # 创建一个菜单用于多选
        self.menu_mcp_tags = QMenu(self)
        self.menu_mcp_tags.setStyleSheet("""
                    QMenu { 
                        background-color: #2b2b2b; 
                        border: 1px solid #555; 
                        border-radius: 6px; 
                        padding: 4px; 
                    }
                    QMenu::item { 
                        padding: 0px; 
                        margin: 0px; 
                    }
                """)
        self.btn_mcp_tags.setMenu(self.menu_mcp_tags)

        # 存储复选框动作的字典
        self.tag_actions = {}

        self.btn_mcp_guide = QPushButton("💡 Prompt guide")
        self.btn_mcp_guide.setCursor(Qt.PointingHandCursor)
        self.btn_mcp_guide.setStyleSheet(
            "color: #aaaaaa; background: transparent; border: 1px solid #555; border-radius: 4px; padding: 2px 8px;")
        self.btn_mcp_guide.setVisible(False)  # 默认隐藏，开启 MCP 后显示

        self.mcp_toolbar.addWidget(self.chk_mcp_enable)
        self.mcp_toolbar.addWidget(self.btn_mcp_tags)
        self.mcp_toolbar.addWidget(self.btn_mcp_guide)
        self.mcp_toolbar.addStretch()
        main_layout.insertLayout(1, self.mcp_toolbar)

        self.chk_mcp_enable.toggled.connect(self._on_mcp_enable_toggled)

        # 设置菜单
        guide_menu = QMenu(self)
        guide_menu.setStyleSheet(
            "QMenu { background-color: #2b2b2b; color: #ddd; border: 1px solid #555; } QMenu::item:selected { background-color: #05B8CC; color: white; }")

        prompts = [
            "🔍 Search for the latest high-impact literature and abstracts in a specific research field",
            "📄 Extract detailed metadata (authors, journal, publication year) for this exact article title",
            "📥 Find Open Access (OA) free download links or PDFs for specific papers or DOIs",
            "🧬 Query the expression and functional summary of a specific gene or protein in a target organism",
            "📊 Find public omics datasets (GEO/SRA) related to specific experimental treatments or phenotypes",
            "🌐 Explain a complex biological mechanism and draw a Mermaid relationship map"
        ]

        for p in prompts:
            action = QAction(p, self)
            action.triggered.connect(lambda checked, text=p: self.set_text(text.split("：")[0] if "：" in text else text))
            guide_menu.addAction(action)

        self.btn_mcp_guide.setMenu(guide_menu)


        self.bottom_bar = QHBoxLayout()
        self.bottom_bar.setContentsMargins(0, 0, 0, 0)

        tool_btn_style = """
            QPushButton { background-color: transparent; color: #888888; border: 1px solid transparent; border-radius: 4px; padding: 4px 10px; font-family: 'Segoe UI'; font-size: 13px;}
            QPushButton:hover { background-color: #333333; border: 1px solid #555555; color: #ffffff;}
            QPushButton:pressed { background-color: #222222; }
        """
        self.btn_export = QPushButton("📤 Export")
        self.btn_export.setCursor(Qt.PointingHandCursor)
        self.btn_export.setStyleSheet(tool_btn_style)
        self.btn_export.clicked.connect(self.sig_export_clicked.emit)

        self.btn_clear = QPushButton("🧹 Clear")
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.setStyleSheet(tool_btn_style)
        self.btn_clear.clicked.connect(self.sig_clear_clicked.emit)

        self.btn_attach = QPushButton("📎 Attach")
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
        self.btn_send.setStyleSheet("""
            QPushButton { 
                background-color: #007acc; color: white; border-radius: 6px; 
                font-weight: bold; font-family: 'Microsoft YaHei';
            }
            QPushButton:hover { background-color: #0062a3; }
        """)
        self.bottom_bar.addWidget(self.btn_send)


        self.btn_stop = QPushButton("⏹ Stop")
        self.btn_stop.setCursor(Qt.PointingHandCursor)
        self.btn_stop.setFixedSize(70, 32)
        self.btn_stop.setStyleSheet("""
            QPushButton { 
                background-color: #c42b1c; color: white; border-radius: 6px; 
                font-weight: bold; font-family: 'Microsoft YaHei';
            }
            QPushButton:hover { background-color: #d13438; }
        """)
        self.btn_stop.setVisible(False)
        self.bottom_bar.addWidget(self.btn_stop)

        # 重试按钮
        self.btn_retry = QPushButton("🔄 重试")
        self.btn_retry.setCursor(Qt.PointingHandCursor)
        self.btn_retry.setFixedSize(70, 32)
        self.btn_retry.setStyleSheet("""
                    QPushButton { 
                        background-color: #ff9800; color: white; border-radius: 6px; 
                        font-weight: bold; font-family: 'Microsoft YaHei';
                    }
                    QPushButton:hover { background-color: #f57c00; }
                """)
        self.btn_retry.setVisible(False)
        self.bottom_bar.addWidget(self.btn_retry)

        main_layout.addLayout(self.bottom_bar)

        self.btn_send.clicked.connect(self._emit_send)
        self.text_edit.sig_send.connect(self._emit_send)

        GlobalSignals().mcp_status_changed.connect(self._on_mcp_status_changed)

    def _on_mcp_status_changed(self):
        if self.chk_mcp_enable.isChecked():
            self.refresh_mcp_tags()

    def _on_tag_toggled(self, tag, checked):
        ConfigManager().toggle_mcp_tag(tag, checked)
        self._update_tag_button_text()

    def _on_mcp_enable_toggled(self, checked):
        """当用户勾选/取消勾选 MCP 开关时触发"""
        self.btn_mcp_guide.setVisible(checked)
        self.btn_mcp_tags.setVisible(checked)
        if checked:
            # 只要开启，就立刻主动刷新一次标签，而不是傻等用户点击菜单
            self.refresh_mcp_tags()

    def _show_filter_menu(self):
        """完全接管菜单弹出逻辑：确保每次点击绝对会请求最新数据"""
        self.refresh_mcp_tags()
        # 计算在按钮正下方弹出
        pos = self.btn_mcp_tags.mapToGlobal(self.btn_mcp_tags.rect().bottomLeft())
        pos.setY(pos.y() + 2)  # 向下偏移 2px 更好看
        self.menu_mcp_tags.popup(pos)

    def refresh_mcp_tags(self):
        try:
            mcp_mgr = MCPManager.get_instance()
            config_mgr = ConfigManager()

            available_tags = mcp_mgr.get_available_tags()
            deselected_tags = config_mgr.mcp_servers.get("deselected_mcp_tags", [])

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

            from PySide6.QtWidgets import QCheckBox, QWidgetAction
            for tag in available_tags:
                chk = QCheckBox(f"  {tag}")

                # 干净透明的 UI，配合你的 rss_tool 风格
                chk.setStyleSheet("""
                                    QCheckBox { 
                                        color: #ddd; 
                                        padding: 8px 16px; 
                                        margin: 0px;
                                        background: transparent; 
                                        font-size: 13px; 
                                        border-radius: 4px; 
                                    }
                                    QCheckBox:hover { background: #37373d; }
                                """)

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
            pass

    def _update_tag_button_text(self):
        selected = self.get_selected_tags()
        total = len(self.tag_actions)
        if total == 0:
            self.btn_mcp_tags.setText("🏷️ Filter Tools: None")
        elif len(selected) == total:
            self.btn_mcp_tags.setText("🏷️ Filter Tools: All")
        else:
            self.btn_mcp_tags.setText(f"🏷️ Filter Tools: {len(selected)} selected")


    def get_selected_tags(self) -> list:
        try:
            available = MCPManager.get_instance().get_available_tags()
            deselected = ConfigManager().mcp_servers.get("deselected_mcp_tags", [])
            return [t for t in available if t not in deselected]
        except:
            return []


    def _emit_send(self):
        text = self.text_edit.toPlainText().strip()
        if text: self.sig_send_clicked.emit(text)

    def clear_text(self):
        self.text_edit.clear()
        self.text_edit.setFocus()

    def set_text(self, text):
        self.text_edit.setPlainText(text)
        self.text_edit.setFocus()

    def lock_input(self):
        self.text_edit.setPlaceholderText("知识库已变更，请清空历史记录以解锁对话。")
        tip = "当前关联的知识库内容或模型已发生改变，继续对话会导致上下文错乱。请点击右侧的 '🧹 Clear' 清空历史记录。"
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
        if self.main_llm: self.main_llm.cancel()
        if self.trans_llm: self.trans_llm.cancel()

    def _init_llms(self):
        """初始化主模型与翻译模型池"""
        if self.main_config and not self.main_llm:
            cfg = self.main_config.copy()
            self.main_llm = OpenAICompatibleLLM(cfg)

        if self.requires_translation and self.trans_config and not self.trans_llm:
            self.trans_llm = OpenAICompatibleLLM(self.trans_config)

    def run(self):
        try:
            self._init_llms()
            original_user_query = self.messages[-1]['content']
            search_query = original_user_query
            domain = "General Academic"
            context_str = ""
            sources_map = {}

            # ==========================================
            # Phase 1: Query Extraction & Translation (Cache Accelerated)
            # ==========================================
            if self.requires_translation:
                self.sig_token.emit("<i>🌐 Translating your query to academic English for precise retrieval...</i>\n\n")
                try:
                    search_query = get_cached_translation(original_user_query, "to_en", self.trans_llm)
                except Exception as e:
                    self.sig_error.emit(
                        f"Translation model request failed. Please check your translation API configuration.\nDetails: {e}")
                    return

            self.sig_token.emit("[CLEAR_SEARCH]")


            # Phase 2 & 3: Vector Retrieval & Reranking (Only if KB is selected)
            if self.kb_id and self.kb_id != "none":
                self.sig_token.emit("<i>🔍 Loading local vector model and retrieving literature...</i>\n\n")

                kb_info = self.kb_manager.get_kb_by_id(self.kb_id)
                if kb_info:
                    domain = kb_info.get('domain', 'General Academic')
                    model_id = kb_info.get('model_id', 'embed_auto')
                    user_pref = ConfigManager().user_settings.get("inference_device", "Auto")
                    target_device = DeviceManager().parse_device_string(user_pref)

                    conf = get_model_conf(model_id, "embedding")
                    if not conf or conf.get('is_auto'):
                        real_id = resolve_auto_model("embedding", target_device)
                        conf = get_model_conf(real_id, "embedding")

                    repo_id = conf.get('hf_repo_id')

                    try:
                        embed_fn = _worker_load_model(self.kb_id)

                        if not self.db.switch_kb(self.kb_id, embedding_function=embed_fn):
                            self.sig_error.emit(f"Failed to switch to Knowledge Base: {self.kb_id}")
                            return
                    except Exception as e:
                        self.sig_error.emit(f"Critical Model Error: {str(e)}")
                        return

                    # Multi-query Expansion
                    history_context = ""
                    if len(self.messages) >= 3:
                        prev_assistant = self.messages[-2]['content'][:100]
                        history_context = f" (Context: {prev_assistant})"

                    expanded_queries = [
                        search_query,
                        f"{search_query}{history_context}",
                        f"{domain} context: {search_query} research details",
                        f"{search_query} references bibliography citations",
                    ]

                    candidate_docs = []
                    seen_contents = set()

                    for eq in expanded_queries:
                        raw_results = self.db.query(eq, n_results=25)
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

                    # Reranker execution
                    if candidate_docs:
                        candidate_docs = sorted(candidate_docs, key=lambda x: x.get('v_dist', 0))[:45]

                        final_docs = self.reranker.rerank(search_query, candidate_docs, domain=domain, top_k=15)
                    else:
                        final_docs = []

            # ==========================================
            # Phase 3.5: Inject External Context (e.g., RSS, Selected Docs)
            # ==========================================
            if getattr(self, 'external_context', None):
                if isinstance(self.external_context, list):
                    current_ref_id = len(sources_map) + 1 if sources_map else 1
                    for chunk in self.external_context:
                        sources_map[current_ref_id] = {
                            "path": chunk.get('path', ''),
                            "page": chunk.get('page', 1),
                            "name": chunk.get('name', 'External Document'),
                            "search_text": chunk.get('content', '')[:100]
                        }
                        context_str += (
                            f"--- [Document {current_ref_id}] ---\n"
                            f"Source: {chunk.get('name', 'External')} (Page {chunk.get('page', 1)})\n"
                            f"Content: {chunk.get('content', '')}\n\n"
                        )
                        current_ref_id += 1
                else:
                    context_str += f"\n\n--- [External Context Provided by User] ---\n{self.external_context}\n\n"

            if not context_str.strip():
                context_str = "No local documents provided. Use tools to fetch real-time data if necessary."

            # Phase 3.9: Low VRAM 内存/显存释放
            is_low_vram = ConfigManager().user_settings.get("low_vram_mode", False)
            if is_low_vram:
                self.sig_token.emit("<i>🧹 [Low VRAM Mode] Unloading RAG models to free up memory for LLM...</i>\n\n")
                if hasattr(self, 'reranker') and getattr(self.reranker, 'model', None) is not None:
                    self.reranker.model = None
                if hasattr(self, 'db') and self.db:
                    self.db.reload()
                if 'embed_fn' in locals():
                    del embed_fn
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

            # Phase 4: Hybrid Agentic RAG (Local DB + MCP Tools)
            self.sig_token.emit("[CLEAR_SEARCH]")
            self.sig_token.emit("[START_LLM_NETWORK]")

            mcp_mgr = MCPManager.get_instance()

            # 按需过滤加载工具
            mcp_tools = None
            if self.use_mcp:
                selected_tags = getattr(self, 'selected_mcp_tags', [])
                if selected_tags:
                    mcp_tools = mcp_mgr.get_tools_schema_by_tags(selected_tags)
                else:
                    mcp_tools = mcp_mgr.get_all_tools_schema()

            system_prompt = (
                f"You are a Senior Research Scientist specializing in {domain}. "
                "Your goal is to provide high-density, evidence-based academic responses.\n\n"

                "### TOOL USE PROTOCOL (STRICT):\n"
                "1. If the provided Context is insufficient, invoke tools IMMEDIATELY.\n"
                "2. SILENT EXECUTION: Never output your reasoning process for choosing a tool.\n\n"

                "### REASONING PROTOCOL (CRITICAL):\n"
                "If you utilize an internal thinking process, you MUST strictly encapsulate ALL reasoning inside <think> and </think> tags.\n"
                "Immediately after closing the </think> tag, you MUST output the exact string [FINAL_ANSWER] before starting your actual response.\n\n"
                
                "### RESPONSE GUIDELINES:\n"
                "1. GROUNDING: If data comes from Context, append citations (e.g., [1], [2]).\n\n"

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
            rag_messages = [{"role": "system", "content": system_prompt}] + self.messages[:-1]
            images = [chunk for chunk in getattr(self, 'external_context', []) if chunk.get("type") == "image"]

            if images:
                vision_content = [{"type": "text", "text": search_query}]
                for img in images:
                    vision_content.append({
                        "type": "image_url",
                        "image_url": {"url": img["base64_url"]}
                    })
                rag_messages.append({"role": "user", "content": vision_content})
            else:
                # 纯文本请求
                rag_messages.append({"role": "user", "content": search_query})

            tool_executed = False

            if mcp_tools:
                try:
                    MAX_ITERATIONS = 5
                    for iteration in range(MAX_ITERATIONS):
                        response_msg = self.main_llm.chat(
                            messages=rag_messages,
                            tools=mcp_tools,
                            tool_choice="auto",
                        )

                        tool_calls = response_msg.get('tool_calls') if isinstance(response_msg, dict) else None
                        content_text = response_msg.get('content', '') if isinstance(response_msg, dict) else str(
                            response_msg)

                        # 拦截格式幻觉
                        if not tool_calls and content_text and re.search(r'(?:Tool_args|tool_calls|arguments):\s*\{',
                                                                         content_text, re.IGNORECASE):
                            self.logger.warning(f"Intercepted model tool hallucination: {content_text}")
                            break

                            # 🚀 核心逻辑：如果本次请求没有返回 tool_calls，说明大模型觉得所需数据（如车票）已经全部查完，主动退出循环
                        if not tool_calls:
                            break

                        tool_executed = True
                        rag_messages.append(response_msg)  # 将模型的调用意图加入上下文

                        # 遍历并执行本次所有的工具请求
                        for tool_call in tool_calls:
                            tool_name = tool_call['function']['name']
                            try:
                                tool_args = json.loads(tool_call['function']['arguments'])
                                self.sig_token.emit(f"<i>📡 Requesting MCP server: {tool_name}...</i>\n\n")

                                tool_result = mcp_mgr.call_tool_sync(tool_name, tool_args)

                                try:
                                    res_dict = json.loads(tool_result)
                                    if isinstance(res_dict, dict) and res_dict.get("status") == "error":
                                        error_msg = res_dict.get("message", "Unknown error")
                                        GlobalSignals().sig_toast.emit(f"Tool Request Failed: {error_msg}", "warning")
                                except Exception:
                                    pass

                            except Exception as e:
                                self.logger.error(f"MCP tool {tool_name} failed: {e}")
                                tool_result = f"Tool execution failed: {str(e)}"

                            # 将工具的返回结果追加给大模型，供下一轮判断
                            rag_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call['id'],
                                "name": tool_name,
                                "content": tool_result
                            })

                    # 循环结束后，所有必要的数据一定集齐了
                    if tool_executed:
                        self.sig_token.emit(
                            "<i>✅ All data retrieved successfully. Conducting comprehensive analysis...</i>\n\n")
                except Exception as e:
                    self.logger.warning(f"Tool calling loop failed: {e}")

            # 🚀 优化闭嘴指令：强化回答，同时强制召回追问格式
            if tool_executed:
                silence_prompt = (
                    "CRITICAL SYSTEM INSTRUCTION: "
                    "The tools have successfully executed and returned the necessary data. "
                    "You MUST NOW provide the final answer directly to the user based on the tool results. "
                    "STRICTLY FORBIDDEN: Do not explain your tool execution process. "
                    "Do not output any JSON argument blocks.\n\n"
                    "REMEMBER: At the very end of your response, you MUST output the [FOLLOW_UPS] tag followed by exactly 6 follow-up questions using the EXACT format specified in the initial system prompt."
                )
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

    def get_ui_widget(self) -> QWidget:
        if self.widget: return self.widget
        self.widget = ChatDropTargetWidget()
        self.widget.sig_files_dropped.connect(self.process_attached_files)
        layout = QVBoxLayout(self.widget)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)

        top_bar = QVBoxLayout()
        top_bar.setSpacing(8)

        #  模型选择组件
        row1_layout = QHBoxLayout()
        # Chat 用的模型
        self.model_selector = ModelSelectorWidget(label_text="🧠 Model:", config_key="active_llm_id",
                                                  model_key="model_name")
        row1_layout.addWidget(self.model_selector)

        row1_layout.addSpacing(15)
        # 翻译用的模型 (指向 trans_model_name)
        self.trans_selector = ModelSelectorWidget(label_text="🌐 Translator:", config_key="trans_llm_id",
                                                  model_key="trans_model_name")
        row1_layout.addWidget(self.trans_selector)
        row1_layout.addStretch()

        # 第二行：知识库选择
        row2_layout = QHBoxLayout()
        row2_layout.addWidget(QLabel("🗂️ Knowledge Base:"))
        self.combo_kb = BaseComboBox(min_width=250)
        self.refresh_kb_list()
        row2_layout.addWidget(self.combo_kb)
        row2_layout.addStretch()

        top_bar.addLayout(row1_layout)
        top_bar.addLayout(row2_layout)
        layout.addLayout(top_bar)


        # 加载本地配置文件并填充两个下拉框
        self.load_llm_configs()

        # --- 对话展示区 ---
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("""
            QScrollArea { border: none; background-color: transparent; }
            QScrollBar:vertical { background: #2d2d30; width: 8px; }
            QScrollBar::handle:vertical { background: #555; border-radius: 4px; }
        """)
        self.chat_container = QWidget()
        self.chat_container.setStyleSheet("background-color: transparent;")
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setSpacing(15)
        self.chat_layout.addStretch()
        self.scroll_area.setWidget(self.chat_container)
        layout.addWidget(self.scroll_area, stretch=1)

        self.btn_scroll_bottom = QPushButton("⬇️", self.scroll_area)
        self.btn_scroll_bottom.setCursor(Qt.PointingHandCursor)
        self.btn_scroll_bottom.setFixedSize(40, 40)  # 圆形尺寸
        self.btn_scroll_bottom.setStyleSheet("""
                    QPushButton { 
                        background-color: rgba(5, 184, 204, 0.85); 
                        color: white; 
                        border-radius: 20px; /* 宽度的二分之一，形成正圆 */
                        font-size: 18px;
                        border: 1px solid rgba(255, 255, 255, 0.2);
                    }
                    QPushButton:hover { 
                        background-color: rgba(0, 122, 204, 1); 
                        border: 1px solid rgba(255, 255, 255, 0.5);
                    }
                """)

        # 透明度动画效果
        self.opacity_effect = QGraphicsOpacityEffect(self.btn_scroll_bottom)
        self.btn_scroll_bottom.setGraphicsEffect(self.opacity_effect)
        self.opacity_effect.setOpacity(0.0)
        self.btn_scroll_bottom.setVisible(False)

        self.fade_anim = QPropertyAnimation(self.opacity_effect, b"opacity")
        self.fade_anim.setDuration(250)  # 250ms 淡入淡出

        # 绑定点击事件
        self.btn_scroll_bottom.clicked.connect(self.scroll_to_bottom)

        # 安装事件过滤器，处理位置自适应
        self.overlay_filter = FloatingOverlayFilter(self.scroll_area, self.btn_scroll_bottom)
        self.scroll_area.installEventFilter(self.overlay_filter)

        # 监听滚动条数值变化
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._check_scroll_position)


        # --- 输入区 ---
        self.input_container = ChatInputContainer()
        self.input_container.sig_send_clicked.connect(self.process_send)
        self.input_container.sig_export_clicked.connect(self.export_chat_history)
        self.input_container.sig_clear_clicked.connect(self.clear_chat_history)
        self.input_container.btn_retry.clicked.connect(self.trigger_retry)
        self.input_container.sig_attach_clicked.connect(self.show_attachment_menu)
        self.input_container.sig_clear_context_clicked.connect(self.clear_attached_context)

        layout.addWidget(self.input_container)
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

    def process_attached_files(self, paths):
        if not hasattr(self, 'external_chunks'):
            self.external_chunks = []
        if not hasattr(self, 'external_context_html'):
            self.external_context_html = ""

        # 防止用户重复狂点
        self.input_container.btn_attach.setEnabled(False)
        self.input_container.btn_send.setEnabled(False)
        self.input_container.show_context_preview("⏳ Loading files into memory...")


        if hasattr(self, 'attach_task_mgr'):
            self.attach_task_mgr.cancel_task()

        self.attach_task_mgr = TaskManager()

        # 连接信号
        self.attach_task_mgr.sig_progress.connect(
            lambda p, m: self.input_container.show_context_preview(f"⏳ {m}")
        )
        self.attach_task_mgr.sig_result.connect(self._on_attachment_result)
        self.attach_task_mgr.sig_state_changed.connect(self._on_attachment_state_changed)

        # 以 THREAD 模式启动，避免多进程的大文本序列化开销
        self.attach_task_mgr.start_task(
            ProcessAttachmentTask,
            task_id="process_attachment",
            mode=TaskMode.THREAD,
            paths=paths
        )

    def _on_attachment_state_changed(self, state, msg):
        if state == TaskState.FAILED.value:
            self.input_container.btn_attach.setEnabled(True)
            self.input_container.btn_send.setEnabled(True)
            self.input_container.hide_context_preview()
            ToastManager().show(f"Attachment failed: {msg}", "error")

    def _on_attachment_result(self, result):
        # 接收并应用后台处理结果
        self.input_container.btn_attach.setEnabled(True)
        self.input_container.btn_send.setEnabled(True)

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
            self.input_container.show_context_preview(display_text)
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
            self.widget, "导出聊天记录 (Export Chat)", "chat_history",
            "HTML File (*.html);;Text File (*.txt);;CSV/Excel (*.csv)"
        )
        if not path: return
        try:
            if path.endswith(".html"):
                html = "<html><head><meta charset='utf-8'><style>body{font-family:sans-serif; background:#f4f4f4; padding:20px;}.msg{background:#fff; padding:15px; margin-bottom:10px; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,0.1);}.role{font-weight:bold; color:#007acc; margin-bottom:5px;}</style></head><body>"
                for msg in self.history:
                    role = "🧑 User" if msg['role'] == "user" else "🤖 AI Assistant"
                    content = re.sub(r"\[CLEAR_SEARCH\]", "", msg['content'])
                    html += f"<div class='msg'><div class='role'>{role}</div><div>{content.replace(chr(10), '<br>')}</div></div>"
                html += "</body></html>"
                with open(path, "w", encoding="utf-8") as f:
                    f.write(html)
            elif path.endswith(".txt"):
                txt = "================ CHAT HISTORY ================\n\n"
                for msg in self.history:
                    role = "User" if msg['role'] == "user" else "AI"
                    content = re.sub(r"<[^>]+>", "", msg['content'])
                    txt += f"[{role}]:\n{content}\n\n{'-' * 40}\n\n"
                with open(path, "w", encoding="utf-8") as f:
                    f.write(txt)
            elif path.endswith(".csv"):
                import csv
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["Role", "Content"])
                    for msg in self.history:
                        content = re.sub(r"<[^>]+>", "", msg['content'])
                        writer.writerow(["User" if msg['role'] == 'user' else "AI", content])
            ToastManager().show(f"Chat history successfully exported to: {os.path.basename(path)}", "success")
            self.logger.info(f"Chat history successfully exported to: {path}")
        except Exception as e:
            ToastManager().show(f"Failed to export chat history: {str(e)}", "error")
            self.logger.error(f"Failed to export chat history: {str(e)}")

    def clear_chat_history(self):
        self.cancel_generation()
        self.current_ai_bubble = None

        self.history.clear()
        self.clear_layout(self.chat_layout)
        self.is_locked = False
        self.input_container.unlock_input()

        self.input_container.btn_retry.setVisible(False)
        self.input_container.btn_stop.setVisible(False)
        self.input_container.btn_send.setVisible(True)

        ToastManager().show("Chat history has been cleared.", "success")
        self.logger.info("Chat history cleared by user.")


    def scroll_to_user_message(self, bubble_widget):
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()

        target_y = max(0, bubble_widget.y() - 10)
        self.scroll_area.verticalScrollBar().setValue(target_y)

    def load_llm_configs(self):
        if hasattr(self, 'model_selector'):
            self.model_selector.load_llm_configs()
        if hasattr(self, 'trans_selector'):
            self.trans_selector.load_llm_configs()

    def process_send(self, text, is_retry=False):
        # 1. 获取并格式化 KB ID
        kb_data = self.combo_kb.currentData()
        kb_id = kb_data.get("id") if isinstance(kb_data, dict) else kb_data
        if not kb_id:
            kb_id = "none"

        # 2. 模型拦截校验（仅在使用了本地知识库时触发）
        if kb_id != "none":
            ready, missing_label, missing_id, m_type = ModelManager().verify_chat_models(kb_id)
            if not ready:
                msg = (
                    f"<b>⚠️ Model Missing - Action Blocked</b><br><br>"
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
                    "检测到非英语输入，但翻译模型未启用。核心模型可能无法完美处理该语言，请注意结果准确性。",
                    "warning"
                )
                self.__class__._has_shown_lang_warning = True

        # 4. 获取当前附件数据
        current_html = getattr(self, 'external_context_html', "")
        current_chunks = getattr(self, 'external_chunks', [])
        self.external_context_html = ""
        self.external_chunks = []

        # 5. UI 切换与历史记录管理
        self.input_container.btn_retry.setVisible(False)
        self.input_container.btn_send.setVisible(False)
        self.input_container.btn_stop.setVisible(True)

        if not is_retry:
            self.logger.info(f"🗣️ User asked: {text[:50]}... (KB: {kb_id})")
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

    def trigger_retry(self):
        """用户点击重试按钮触发"""
        if not self.history: return

        # 寻找最后一次 user 的提问
        last_user_msg = None
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i]['role'] == 'user':
                last_user_msg = self.history[i]
                break

        if last_user_msg:
            # 清理历史记录中失败的 Assistant 回复及其气泡，避免大模型读取到报错信息
            if self.history[-1]['role'] == 'assistant':
                self.history.pop()
                if self.chat_layout.count() > 1:
                    item = self.chat_layout.itemAt(self.chat_layout.count() - 2)
                    if item.widget():
                        item.widget().deleteLater()

            # 恢复外部附件切片
            self.external_chunks = last_user_msg.get('external_chunks', [])
            self.process_send(last_user_msg.get('display_text', last_user_msg['content']), is_retry=True)


    def add_bubble(self, text, is_user, context_html=None):
        if self.chat_layout.count() > 0:
            item = self.chat_layout.itemAt(self.chat_layout.count() - 1)
            if item.spacerItem(): self.chat_layout.removeItem(item)
        if is_user:
            for i in range(self.chat_layout.count()):
                item = self.chat_layout.itemAt(i)
                if item and item.widget():
                    w = item.widget()
                    if isinstance(w, ChatBubbleWidget) and w.is_user: w.disable_edit()

        index = len(self.history)

        bubble = ChatBubbleWidget(text, is_user, index, context_html=context_html)

        bubble.index = index

        if is_user:
            bubble.sig_edit_confirmed.connect(self.handle_edit_resend)
            bubble.sig_link_clicked.connect(self.handle_link_click)
        else:
            bubble.sig_retry_clicked.connect(lambda idx: self.trigger_retry())

            bubble.lbl_text.linkActivated.connect(self.handle_link_click)

        self.chat_layout.addWidget(bubble)
        self.chat_layout.addStretch()

        if is_user:
            QTimer.singleShot(50, lambda: self.scroll_to_user_message(bubble))
        else:
            QTimer.singleShot(50, self.scroll_to_bottom)
            self.scroll_to_bottom()

        return bubble





    def scroll_to_bottom(self):
        sb = self.scroll_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    def start_ai_response(self, kb_id, requires_translation=False):
        # 获取最新的主模型配置与翻译配置
        main_config = self.model_selector.get_current_config()
        trans_config = self.trans_selector.get_current_config()
        use_mcp_tools = self.input_container.chk_mcp_enable.isChecked() if hasattr(self.input_container,
                                                                                   'chk_mcp_enable') else False
        selected_mcp_tags = self.input_container.get_selected_tags() if use_mcp_tools else []


        if trans_config:
            trans_config = trans_config.copy()
            trans_config["model_name"] = trans_config.get("trans_model_name", trans_config.get("model_name"))

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
        self.sig_finished.connect(self.thread.quit)

        self.thread.start()

    def cancel_generation(self):
        """Handle the stop button click to abort AI generation."""
        # 1. 调用 worker 层面的取消逻辑（中断 LLM 请求）
        if hasattr(self, 'worker') and self.worker:
            self.worker.cancel()

        # 2. 停止 UI 加载状态
        if self.current_ai_bubble and self.current_ai_bubble.is_loading:
            self.current_ai_bubble.set_loading(False)

        # 3. 在对话气泡中追加中止提示
        self.current_ai_text += "\n\n<div style='color:#ff9800; font-weight:bold;'>[⏹️ Generation Cancelled by User]</div>"
        if self.current_ai_bubble:
            idx = getattr(self.current_ai_bubble, 'index', -1)
            self.current_ai_bubble.set_content(self._format_response(self.current_ai_text, idx))

        # 4. 恢复底部输入框的按钮状态
        self.input_container.btn_stop.setVisible(False)
        self.input_container.btn_send.setVisible(True)
        self.input_container.btn_retry.setVisible(True)

        # 5. 强制退出后台线程
        if getattr(self, 'worker_thread', None) is not None and self.worker_thread.isRunning():
            self.worker_thread.quit()

        self.logger.info("AI generation cancelled by user.")
        self.scroll_to_bottom()


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

    def handle_external_send(self, context_text, prompt_text=""):
        import tempfile
        import os
        from urllib.parse import quote

        # 1. 将纯文本伪装成本地物理文件，供 TextViewer 读取
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, "scholar_navis_external_context.txt")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(context_text)
        except Exception as e:
            self.logger.error(f"Failed to write temp external context: {e}")

        # 2. 兼容投递给大模型的 Chunks
        self.external_chunks = [{
            "path": temp_path,
            "name": "External Context.txt",
            "page": 1,
            "content": context_text
        }]

        # 3. 生成带 <a> 标签的可点击 URI 链接
        safe_path = quote(temp_path)
        safe_name = quote("External Context.txt")
        link = f"cite://view?path={safe_path}&page=1&name={safe_name}"

        # 提取前 80 个字符做预览，去掉换行符保持气泡紧凑
        preview_text = context_text[:80].replace('\n', ' ') + "..."
        self.external_context_html = f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#05B8CC; text-decoration:none;'>📄 {preview_text} (Click to read more)</a></div>"

        if hasattr(self, 'input_container') and hasattr(self.input_container, 'chk_mcp_enable'):
            if not self.input_container.chk_mcp_enable.isChecked():
                self.input_container.chk_mcp_enable.setChecked(True)

        # 4. 界面焦点跳转与预览 UI 逻辑
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
        menu = QMenu(self.widget)
        menu.setStyleSheet("""
            QMenu { background-color: #2d2d30; color: white; border: 1px solid #444; } 
            QMenu::item { padding: 6px 20px; }
            QMenu::item:selected { background-color: #007acc; }
        """)

        act_kb = menu.addAction("🗂️ Select from Knowledge Base")
        act_local = menu.addAction("💻 Upload Local File")

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

        # 简单的文件选择弹窗
        dlg = QDialog(self.widget)
        dlg.setWindowTitle("Select Files from KB")
        dlg.setFixedSize(400, 300)
        dlg.setStyleSheet("background-color: #1e1e1e; color: white;")
        layout = QVBoxLayout(dlg)

        list_widget = QListWidget()
        list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        list_widget.setStyleSheet("background-color: #252526; border: 1px solid #444;")
        for f in files:
            item = QListWidgetItem(f['name'])
            item.setData(Qt.UserRole, f['path'])
            list_widget.addItem(item)

        layout.addWidget(QLabel("Hold Ctrl/Shift to select multiple files:"))
        layout.addWidget(list_widget)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        layout.addWidget(btn_box)

        if dlg.exec():
            selected_items = list_widget.selectedItems()
            if not selected_items: return

            if not hasattr(self, 'external_chunks'):
                self.external_chunks = []
            if not hasattr(self, 'external_context_html'):
                self.external_context_html = ""

            names = []
            for item in selected_items:
                f_path = item.data(Qt.UserRole)
                f_name = item.text()
                try:
                    # 按照 PDF 页数进行精确切片
                    if f_name.lower().endswith('.pdf') or f_path.lower().endswith('.pdf'):
                        try:
                            chunks = pymupdf4llm.to_markdown(f_path, page_chunks=True)
                            for chunk in chunks:
                                text = chunk.get("text", "").strip()
                                if len(text) > 10:
                                    self.external_chunks.append({
                                        "path": f_path, "name": f_name,
                                        "page": chunk.get("metadata", {}).get("page", 1),
                                        "content": text
                                    })
                        except Exception as e:
                            self.logger.error(f"PyMuPDF4LLM failed for {f_name}: {e}")
                    else:
                        # 🚨 修正：直接原生读取，不用那个根本不存在的 read_file_content
                        with open(f_path, 'r', encoding='utf-8') as f:
                            content = f.read().strip()
                            if content:
                                self.external_chunks.append({
                                    "path": f_path, "name": f_name,
                                    "page": 1, "content": content
                                })

                    names.append(f_name)
                    # 构造气泡上方的可点击链接
                    from urllib.parse import quote
                    link = f"cite://view?path={quote(f_path)}&page=1&name={quote(f_name)}"
                    self.external_context_html += f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#05B8CC; text-decoration:none;'>📄 {f_name}</a></div>"
                except Exception as e:
                    print(f"Failed to read {f_name}: {e}")

            if names:
                self.input_container.show_context_preview(", ".join(names))
                ToastManager().show(f"Attached {len(names)} document(s).", "success")


    def update_ai_bubble(self, token):
        """Updates the AI chat bubble with streaming tokens and handles status clearing."""
        if not self.current_ai_bubble: return
        sb = self.scroll_area.verticalScrollBar()
        is_at_bottom = (sb.maximum() - sb.value()) <= 15

        idx = getattr(self.current_ai_bubble, 'index', -1)

        # 1. Handle clearing of status prompts via Regex
        if token == "[CLEAR_SEARCH]":
            import re
            # Removes any italicized status messages and trailing newlines to prevent Markdown block errors
            self.current_ai_text = re.sub(r'<i>.*?</i>(?:\n\n)?', '', self.current_ai_text, flags=re.DOTALL)
            self.current_ai_text = self.current_ai_text.lstrip()
            self.current_ai_bubble.set_content(self._format_response(self.current_ai_text, idx))
            if is_at_bottom: self.scroll_to_bottom()
            return

        # 2. Handle LLM connection start
        if token == "[START_LLM_NETWORK]":
            self._is_waiting_llm = True
            # Ensure text is stripped to prevent <div> from being treated as a code block
            base_html = self._format_response(self.current_ai_text.lstrip(), idx)
            self.current_ai_bubble.set_content(
                base_html +
                "<br><div style='color:#05B8CC;'><i>Connecting to LLM provider, please wait...</i></div>"
            )
            from PySide6.QtCore import QTimer
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
        self.current_ai_bubble.set_content(self._format_response(self.current_ai_text.lstrip(), idx))
        if is_at_bottom: self.scroll_to_bottom()

    def _format_response(self, text, index):
        pattern = r'```mermaid\s*\n(.*?)\n```'

        def repl_mermaid(match):
            code = match.group(1).strip()
            # 使用 MD5 哈希作为字典的 Key
            code_hash = hashlib.md5(code.encode('utf-8')).hexdigest()

            if not hasattr(self, 'mermaid_codes'):
                self.mermaid_codes = {}
            self.mermaid_codes[code_hash] = code

            # 生成一个清爽的 UI 卡片替代长串代码
            return (
                f"<br><div style='padding:12px; margin: 8px 0; border:1px solid #05B8CC; border-radius:6px; background-color: rgba(5, 184, 204, 0.08);'>"
                f"<div style='margin-bottom: 5px;'>📊 <b>Mermaid Diagram Generated</b></div>"
                f"<a href='mermaid://view?hash={code_hash}' style='color:#05B8CC; text-decoration:none; font-weight:bold;'>"
                f"👉 Click here to view / edit interactive diagram</a></div><br>")

        # 执行替换
        processed_text = re.sub(pattern, repl_mermaid, text, flags=re.DOTALL | re.IGNORECASE)

        # 交给原本的格式化器处理其他 Markdown 元素
        return TextFormatter.format_chat_text(
            processed_text, index, getattr(self, 'expanded_thinks', set()), getattr(self, 'user_toggled_thinks', set())
        )

    def on_chat_error(self, msg):
        self.logger.error(f"Chat generation encountered an error: {msg}")
        if hasattr(self, 'slow_conn_timer'): self.slow_conn_timer.stop()
        self._is_waiting_llm = False

        # --- 翻译或生成失败处理 ---
        # 显示重试按钮
        self.input_container.btn_stop.setVisible(False)
        self.input_container.btn_retry.setVisible(True)
        self.input_container.btn_send.setVisible(True)

        display_error = msg
        if "translation" in msg.lower() or "translator" in msg.lower():
            ToastManager().show(f"翻译模型出现异常，对话已终止: {msg}", "error")
            display_error = f"翻译中断: {msg}"
        elif "time" in msg.lower() or "connect" in msg.lower():
            ToastManager().show("网络连接失败，请检查 API 配置或网络代理。", "error")
            display_error = "网络或 API 连接超时。"

        if self.current_ai_bubble and self.current_ai_bubble.is_loading:
            self.current_ai_bubble.set_loading(False)

        self.current_ai_text += f"\n\n<div style='color:#ff6b6b; font-weight:bold;'>[⚠️ AI Error]</div>\n<div style='color:#888; font-size:12px;'>{display_error}</div>"

        if self.current_ai_bubble:
            idx = getattr(self.current_ai_bubble, 'index', -1)
            self.current_ai_bubble.set_content(self._format_response(self.current_ai_text, idx))

        self.scroll_to_bottom()

    def on_chat_finished(self):
        if not self.current_ai_bubble:
            return

        self.input_container.btn_stop.setVisible(False)
        self.input_container.btn_send.setVisible(True)
        try:
            self.input_container.btn_stop.clicked.disconnect()
        except:
            pass

        if self.current_ai_bubble and self.current_ai_bubble.is_loading:
            self.current_ai_bubble.set_loading(False)

        full_text = self.current_ai_text

        match = re.search(r'\[FOLLOW_UPS\]\s*(.*)', full_text, flags=re.IGNORECASE | re.DOTALL)
        questions = []

        if match:
            follow_up_block = match.group(1)
            clean_text = full_text[:match.start()].strip()
            self.current_ai_text = clean_text

            for line in follow_up_block.split('\n'):
                line = line.strip()
                # 兼容模型偶尔输出的 💡 前缀或星号
                if line.startswith('-') or line.startswith('*') or line.startswith('💡'):
                    q = line.lstrip('-*💡 ').strip()
                    if q and q.startswith('['):
                        # 匹配 [Tag] 内容
                        tag_match = re.match(r'^\[(.*?)\]\s*(.*)', q)
                        if tag_match:
                            tag, text = tag_match.groups()
                            questions.append({"tag": tag.strip(), "text": text.strip()})
                        else:
                            questions.append({"tag": "General", "text": q})

            idx = getattr(self.current_ai_bubble, 'index', -1)
            final_html = self._format_response(self.current_ai_text, idx)
            self.current_ai_bubble.set_content(final_html)

            # 渲染胶囊按钮
            if questions:
                self.render_follow_up_buttons(questions)
        else:
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
            from src.ui.components.toast import ToastManager
            ToastManager().show("The knowledge base was modified. Chat is currently locked.", "warning")


    def render_follow_up_buttons(self, questions):
        if self.chat_layout.count() > 0:
            item = self.chat_layout.itemAt(self.chat_layout.count() - 1)
            if item.spacerItem(): self.chat_layout.removeItem(item)
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(60, 10, 60, 20)
        layout.setSpacing(8)
        lbl = QLabel("💡 <b>Explore deeper (Left-click to send, Right-click to edit):</b>")
        lbl.setStyleSheet("color: #888888; font-size: 12px; border: none;")
        layout.addWidget(lbl)
        color_map = {"Deep Dive": ("#ffb86c", "🔍"), "Critical": ("#ff5555", "⚠️"), "Broader": ("#50fa7b", "🌐"),
                     "Brainstorm": ("#bd93f9", "⚡"), "Similar": ("#8be9fd", "🔗"), "Application": ("#f1fa8c", "🚀"),
                     "General": ("#05B8CC", "💡")}
        for q_obj in questions:
            tag = q_obj.get("tag", "General") if isinstance(q_obj, dict) else "General"
            raw_text = q_obj.get("text", q_obj) if isinstance(q_obj, dict) else q_obj
            clean_text = re.sub(r'\[\s*\d+\s*(?:,\s*\d+\s*)*\]', '', raw_text)
            clean_text = clean_text.replace('**', '').strip()
            color, icon = color_map.get(tag, ("#05B8CC", "💡"))
            btn = FollowUpPillButton(tag, clean_text, color, icon)
            if getattr(self, 'is_locked', False):
                btn.setToolTip("知识库已变更，无法追问，请清空历史记录。")
            btn.sig_clicked.connect(self._trigger_follow_up)
            btn.sig_right_clicked.connect(self._edit_follow_up)
            layout.addWidget(btn)
        self.chat_layout.addWidget(container)
        self.chat_layout.addStretch()
        QTimer.singleShot(50, self.scroll_to_bottom)
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

    def handle_edit_resend(self, index, new_text):
        if getattr(self, 'is_locked', False):
            ToastManager().show("Cannot edit: The current library has been modified. Please clear chat.", "warning")
            old_msg = self.history[index]
            for i in range(self.chat_layout.count()):
                item = self.chat_layout.itemAt(i)
                if item and item.widget() and isinstance(item.widget(), ChatBubbleWidget):
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
        self.clear_layout(self.chat_layout)
        temp_history = list(self.history)
        self.history = []

        # 重新绘制历史气泡时，带上附加 UI 元素并避免渲染整段大长串 Prompt
        for msg in temp_history:
            display_text = msg.get('display_text', msg['content'])
            ctx_html = msg.get('context_html')
            self.add_bubble(display_text, is_user=(msg['role'] == 'user'), context_html=ctx_html)
            self.history.append(msg)

        kb_data = self.combo_kb.currentData()
        kb_id = kb_data.get("id") if isinstance(kb_data, dict) else kb_data

        # 重新绘制被编辑的新气泡，并带上附件
        self.add_bubble(new_text, is_user=True, context_html=old_context_html)

        # 重新拼接供底层喂给大模型的完整 Prompt
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


    def clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                if hasattr(widget, 'clean_up_images'):
                    widget.clean_up_images()
                widget.deleteLater()
        layout.addStretch()


    def handle_link_click(self, url_str):

        if url_str.startswith("mermaid://"):
            parsed = urlparse(url_str)
            params = parse_qs(parsed.query)
            code_hash = params.get('hash', [''])[0]

            # 从缓存字典中取出真实的 Mermaid 代码
            code = getattr(self, 'mermaid_codes', {}).get(code_hash, "")
            if code:
                # 延迟导入避免循环依赖
                from src.ui.components.mermaid_viewer import MermaidViewer
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
                    import tempfile
                    import shutil
                    temp_dir = tempfile.gettempdir()
                    safe_name = source_name if source_name else "document.bin"
                    temp_file_path = os.path.join(temp_dir, f"scholar_navis_view_{safe_name}")

                    try:
                        shutil.copy2(target_path, temp_file_path)
                        QDesktopServices.openUrl(QUrl.fromLocalFile(temp_file_path))
                        ToastManager().show(f"已调用系统程序打开: {safe_name}", "success")
                    except Exception as e:
                        ToastManager().show(f"外部程序调用失败: {str(e)}", "error")
            else:
                ToastManager().show(f"未找到文件: {source_name or file_path}", "error")
        else:
            QDesktopServices.openUrl(QUrl(url_str))

    def refresh_kb_list(self):
        self.load_llm_configs()
        if not hasattr(self, 'combo_kb'): return
        curr_data = self.combo_kb.currentData()
        curr_id = curr_data['id'] if isinstance(curr_data, dict) else curr_data

        self.combo_kb.blockSignals(True)
        self.combo_kb.clear()

        self.combo_kb.addItem("❌ No Knowledge Base (Direct Chat)", "none")

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

    def execute_task(self):
        pass