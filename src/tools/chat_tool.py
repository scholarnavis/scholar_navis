import json
import os
import re
from urllib.parse import urlparse, parse_qs, quote

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout,
                               QPlainTextEdit, QPushButton, QLabel,
                               QScrollArea, QFrame, QFileDialog, QCheckBox)
from PySide6.QtCore import Qt, Signal, QObject, QThread, QUrl
from PySide6.QtGui import QDesktopServices

from huggingface_hub import snapshot_download
from chromadb.utils import embedding_functions

from src.core.models_registry import get_model_conf, resolve_auto_model
from src.core.rerank_engine import RerankEngine
from src.core.signals import GlobalSignals
from src.tools.base_tool import BaseTool
from src.core.llm_impl import OpenAICompatibleLLM
from src.core.database import DatabaseManager
from src.core.kb_manager import KBManager
from src.core.config_manager import ConfigManager
from src.core.device_manager import DeviceManager
from src.ui.components.combo import BaseComboBox
from src.ui.components.toast import ToastManager

from src.ui.components.pdf_viewer import InternalPDFViewer
from src.ui.components.chat_bubble import ChatBubbleWidget
from src.ui.components.pill_button import FollowUpPillButton


# ... (AutoResizingTextEdit 和 ChatInputContainer 保持不变，直接看 ChatWorker) ...

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
        new_height = min(max(doc_height + 12, 40), self.max_height)
        self.setFixedHeight(new_height)
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if new_height == self.max_height else Qt.ScrollBarAlwaysOff)

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

        self.bottom_bar.addWidget(self.btn_export)
        self.bottom_bar.addWidget(self.btn_clear)
        self.bottom_bar.addStretch()

        self.btn_send = QPushButton("发送")
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

        self.btn_stop = QPushButton("⏹ 停止")
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

        main_layout.addLayout(self.bottom_bar)

        self.btn_send.clicked.connect(self._emit_send)
        self.text_edit.sig_send.connect(self._emit_send)

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


class ChatWorker(QObject):
    sig_token = Signal(str)
    sig_finished = Signal()
    sig_error = Signal(str)

    def __init__(self, llm_config, messages, kb_id, use_thinking_model=False):
        super().__init__()
        self.llm_config = llm_config
        self.messages = messages
        self.kb_id = kb_id
        self.db = DatabaseManager()
        self.kb_manager = KBManager()
        self.reranker = RerankEngine()
        self.full_response_cache = ""

    def cancel(self):
        if self.llm_instance: self.llm_instance.cancel()

    def run(self):
        try:
            user_query = self.messages[-1]['content']
            if not self.kb_id:
                self.sig_error.emit("No Knowledge Base ID provided.")
                self.sig_finished.emit()
                return

            kb_info = self.kb_manager.get_kb_by_id(self.kb_id)
            if not kb_info: return

            # === 核心修复：显式加载模型，不依赖 DatabaseManager 自动推断 ===
            model_id = kb_info.get('model_id', 'embed_auto')
            user_pref = ConfigManager().user_settings.get("inference_device", "Auto")
            target_device = DeviceManager().parse_device_string(user_pref)

            conf = get_model_conf(model_id, "embedding")
            if not conf or conf.get('is_auto'):
                real_id = resolve_auto_model("embedding", target_device)
                conf = get_model_conf(real_id, "embedding")

            repo_id = conf.get('hf_repo_id')

            try:
                # 尝试加载路径
                try:
                    model_path = snapshot_download(repo_id=repo_id, local_files_only=True)
                except:
                    model_path = snapshot_download(repo_id=repo_id)

                # 显式初始化
                embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name=model_path,
                    device=target_device,
                    trust_remote_code=conf.get('trust_remote_code', False),
                    normalize_embeddings=True
                )

                # 注入 switch_kb
                if not self.db.switch_kb(self.kb_id, embedding_function=embed_fn):
                    self.sig_error.emit(f"Failed to switch to Knowledge Base: {self.kb_id}")
                    return

            except Exception as e:
                self.sig_error.emit(f"Critical Model Error: {str(e)}")
                return
            # =========================================================

            domain = kb_info.get('domain', 'General Academic')
            history_context = ""
            if len(self.messages) >= 3:
                prev_assistant = self.messages[-2]['content'][:100]
                history_context = f" (Context: {prev_assistant})"

            expanded_queries = [
                user_query,
                f"{user_query}{history_context}",
                f"{domain} context: {user_query} research details",
                f"{user_query} references bibliography citations",
            ]

            self.sig_token.emit("<i>🔍 Integrating history & searching database...</i>\n\n")

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

            if candidate_docs:
                candidate_docs = sorted(candidate_docs, key=lambda x: x.get('v_dist', 0))[:45]
                final_docs = self.reranker.rerank(user_query, candidate_docs, domain=domain, top_k=15)
            else:
                final_docs = []

            diverse_docs = []
            source_counts = {}

            if final_docs:
                for doc in final_docs:
                    if doc.get('score', 0) < -5.0: continue

                    src_name = doc['metadata'].get('source', 'Unknown')
                    source_counts[src_name] = source_counts.get(src_name, 0) + 1
                    if source_counts[src_name] <= 3:
                        diverse_docs.append(doc)
                    if len(diverse_docs) >= 8: break

            context_str = ""
            sources_map = {}
            if diverse_docs:
                for i, doc_obj in enumerate(diverse_docs):
                    ref_id = i + 1
                    meta = doc_obj['metadata']
                    content = doc_obj['content'].replace('\n', ' ')
                    sources_map[ref_id] = {
                        "path": meta.get('file_path', ''),
                        "page": meta.get('page', 1),
                        "name": meta.get('source', 'Document'),
                        "search_text": content[:100]
                    }
                    context_str += (
                        f"--- [Document {ref_id}] ---\n"
                        f"Source: {meta.get('source', 'Unknown')} (Page {meta.get('page', '?')})\n"
                        f"Content: {content}\n\n"
                    )

            self.sig_token.emit("[CLEAR_SEARCH]")

            if not context_str:
                context_str = "No documents found. Reply strictly based on the lack of internal data."

            system_prompt = (
                f"You are a rigorous research assistant specializing in {domain}.\n"
                "Answer EXCLUSIVELY based on the provided Context.\n\n"
                "### CORE DIRECTIVES:\n"
                "1. **NO HALLUCINATION**: If the answer is not in the context, say 'I cannot answer this based on the provided documents.'\n"
                "2. **CITATIONS**: Append `[1]`, `[2]` directly after sentences using that document's facts.\n"
                "3. **FOLLOW-UPS (CRITICAL)**: At the end of your response, you MUST provide exactly 6 follow-up questions. The first 3 should dive into the current context, and the last 3 should expand the user's thinking (brainstorming/related topics). Format them EXACTLY like this:\n"
                "   💡 Suggested Follow-ups:\n"
                "   - [Deep Dive] <Question about specific details or mechanisms>\n"
                "   - [Critical] <Question about limitations, alternatives, or weaknesses>\n"
                "   - [Broader] <Question about implications or future trends>\n"
                "   - [Brainstorm] <A creative brainstorming question or hypothetical \"What if\" scenario>\n"
                "   - [Similar] <Question connecting to a similar or parallel topic/concept>\n"
                "   - [Application] <Question about real-world applications or cross-disciplinary use>\n\n"
                f"### CONTEXT:\n{context_str}"
            )

            rag_messages = [{"role": "system", "content": system_prompt}] + self.messages

            if not self.llm_config:
                self.llm_config = {"id": ConfigManager().user_settings.get("active_llm_id", "openai")}

            selected_id = self.llm_config.get("id")
            latest_config = self.llm_config
            try:
                config_path = os.path.join(os.getcwd(), "config", "llm_config.json")
                if os.path.exists(config_path):
                    with open(config_path, 'r', encoding='utf-8') as f:
                        for c in json.load(f):
                            if c.get("id") == selected_id:
                                latest_config = c
                                break
            except:
                pass

            if getattr(self, 'use_thinking_model', False):
                thinking_model = latest_config.get("thinking_model_name", "").strip()
                if thinking_model:
                    latest_config["model_name"] = thinking_model
                else:
                    self.sig_token.emit(
                        "<div style='color:#ff9800; font-size:12px; margin-bottom:10px;'><i>Thinking model not found, falling back to standard.</i></div>")

            self.llm_instance = OpenAICompatibleLLM(latest_config)

            # 向 UI 发送信号，表示本地检索结束，正式开始连接网络！
            self.sig_token.emit("[START_LLM_NETWORK]")

            for token in self.llm_instance.stream_chat(rag_messages):
                self.full_response_cache += token
                self.sig_token.emit(token)

            has_citation = bool(re.search(r'\[\d+\]', self.full_response_cache))
            if sources_map and has_citation:
                ref_html = "\n<br><hr style='border:0; height:1px; background:#444; margin:15px 0;'><b>📚 Cited Sources:</b><br>"
                used_indices = set(int(ref) for ref in re.findall(r'\[(\d+)\]', self.full_response_cache))
                displayed = 0
                for rid, info in sources_map.items():
                    if rid in used_indices:
                        safe_path = quote(info['path'])
                        safe_text = quote(info['search_text'])
                        link = f"cite://view?path={safe_path}&page={info['page']}&text={safe_text}"
                        ref_html += f"<div style='margin-bottom: 5px;'>▪ <a style='color:#05B8CC; text-decoration:none;' href='{link}'><b>[{rid}]</b> {info['name']} (Page {info['page']})</a></div>"
                        displayed += 1
                if displayed > 0:
                    self.sig_token.emit(ref_html)

        except Exception as e:
            import traceback
            self.sig_error.emit(f"Error: {str(e)}")
            print(traceback.format_exc())
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

        GlobalSignals().kb_list_changed.connect(self.refresh_kb_list)
        GlobalSignals().kb_switched.connect(self.on_global_kb_switched)
        GlobalSignals().kb_modified.connect(self.on_kb_modified)

        if hasattr(GlobalSignals(), 'llm_config_changed'):
            GlobalSignals().llm_config_changed.connect(self.load_llm_configs)


    def get_ui_widget(self) -> QWidget:
        if self.widget: return self.widget
        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)

        top_bar = QHBoxLayout()
        top_bar.addWidget(QLabel("Model:"))
        self.combo_llm = BaseComboBox(min_width=150)
        top_bar.addWidget(self.combo_llm)
        self.checkbox_think = QCheckBox("Think Mode")
        self.checkbox_think.setCursor(Qt.PointingHandCursor)
        self.checkbox_think.setStyleSheet("""
                    QCheckBox { color: #aaaaaa; font-weight: bold; font-family: 'Segoe UI'; font-size: 13px; }
                    QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px; border: 1px solid #555; background: #333; }
                    QCheckBox::indicator:checked { background: #007acc; border: 1px solid #007acc; }
                    QCheckBox:disabled { color: #555555; }
                """)
        top_bar.addWidget(self.checkbox_think)
        self.lbl_current_model = QLabel("")
        self.lbl_current_model.setStyleSheet(
            "color: #05B8CC; font-size: 11px; font-weight: bold; font-family: 'Consolas', monospace;")
        top_bar.addWidget(self.lbl_current_model)
        self.combo_llm.currentIndexChanged.connect(self._on_llm_changed)
        self.checkbox_think.stateChanged.connect(self._update_model_display)
        self.load_llm_configs()
        top_bar.addSpacing(15)
        top_bar.addWidget(QLabel("Knowledge Base:"))
        self.combo_kb = BaseComboBox(min_width=200)
        self.refresh_kb_list()
        top_bar.addWidget(self.combo_kb, stretch=1)
        layout.addLayout(top_bar)

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

        self.input_container = ChatInputContainer()
        self.input_container.sig_send_clicked.connect(self.process_send)
        self.input_container.sig_export_clicked.connect(self.export_chat_history)
        self.input_container.sig_clear_clicked.connect(self.clear_chat_history)
        layout.addWidget(self.input_container)
        return self.widget

    def _on_llm_changed(self):
        llm_config = self.combo_llm.currentData()
        if not llm_config: return
        has_think = bool(llm_config.get("thinking_model_name", "").strip())
        self.checkbox_think.blockSignals(True)
        if not has_think:
            self.checkbox_think.setChecked(False)
            self.checkbox_think.setEnabled(False)
            self.checkbox_think.setToolTip("Current provider has no thinking model configured.")
        else:
            self.checkbox_think.setEnabled(True)
            self.checkbox_think.setToolTip(f"Enable {llm_config.get('thinking_model_name')}")
        self.checkbox_think.blockSignals(False)
        self._update_model_display()

    def _update_model_display(self):
        if not hasattr(self, 'lbl_current_model'): return
        llm_config = self.combo_llm.currentData()
        if not llm_config: return
        use_think = self.checkbox_think.isChecked()
        thinking_model = llm_config.get("thinking_model_name", "").strip()
        standard_model = llm_config.get("model_name", "").strip()
        actual_model = thinking_model if (use_think and thinking_model) else standard_model
        if actual_model:
            self.lbl_current_model.setText(f"[{actual_model}]")
        else:
            self.lbl_current_model.setText("[No Model Configured]")

    def on_kb_modified(self, kb_id):
        if not self.history: return
        curr_data = self.combo_kb.currentData()
        curr_id = curr_data.get("id") if isinstance(curr_data, dict) else curr_data
        if curr_id == kb_id:
            self.is_locked = True
            self.input_container.lock_input()
            ToastManager().show("The knowledge base was modified. Chat is currently locked.", "warning")

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
        self.history.clear()
        self.clear_layout(self.chat_layout)
        self.is_locked = False
        self.input_container.unlock_input()
        ToastManager().show("Chat history has been cleared.", "success")
        self.logger.info("Chat history cleared by user.")

    def load_llm_configs(self):
        if not hasattr(self, 'combo_llm'): return

        path = os.path.join(os.getcwd(), "config", "llm_config.json")
        self.combo_llm.clear()
        active_id = ConfigManager().user_settings.get("active_llm_id", "openai")
        target_idx = 0
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    configs = json.load(f)
                    for i, cfg in enumerate(configs):
                        self.combo_llm.addItem(cfg['name'], cfg)
                        if cfg.get("id") == active_id: target_idx = i
            except:
                pass
        if self.combo_llm.count() > 0: self.combo_llm.setCurrentIndex(target_idx)
        self._on_llm_changed()

    def process_send(self, text):
        kb_data = self.combo_kb.currentData()
        if not kb_data:
            ToastManager().show("Attempted to send message without selecting a KB！", "error")
            self.logger.warning("Attempted to send message without selecting a KB.")
            return
        kb_id = kb_data.get("id") if isinstance(kb_data, dict) else kb_data
        self.logger.info(f"🗣️ User asked: {text[:50]}... (KB: {kb_id})")
        self.input_container.clear_text()
        self.add_bubble(text, is_user=True)
        self.history.append({"role": "user", "content": text})
        self.start_ai_response(kb_id)

    def add_bubble(self, text, is_user):
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
        bubble = ChatBubbleWidget(text, is_user, index)
        bubble.index = index

        if is_user:
            bubble.sig_edit_confirmed.connect(self.handle_edit_resend)
        else:
            bubble.lbl_text.linkActivated.connect(self.handle_link_click)
        self.chat_layout.addWidget(bubble)
        self.chat_layout.addStretch()
        QThread.msleep(10)
        self.scroll_to_bottom()
        return bubble

    def scroll_to_bottom(self):
        sb = self.scroll_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    def start_ai_response(self, kb_id):
        llm_config = self.combo_llm.currentData()
        use_think = getattr(self, 'checkbox_think', None) and self.checkbox_think.isChecked()
        thinking_model = llm_config.get("thinking_model_name", "").strip()
        standard_model = llm_config.get("model_name", "").strip()
        actual_model = thinking_model if (use_think and thinking_model) else standard_model
        self.logger.info(
            f" Starting AI response | Model: [{actual_model}] | Provider: [{llm_config.get('name', 'Unknown')}] | Deep Think: {use_think}")
        self.current_ai_text = ""
        self.current_ai_bubble = self.add_bubble("", is_user=False)
        self.current_ai_bubble.set_loading(True)
        self.input_container.btn_send.setVisible(False)
        self.input_container.btn_stop.setVisible(True)
        use_think = getattr(self, 'checkbox_think', None) and self.checkbox_think.isChecked()
        self.thread = QThread()
        self.worker = ChatWorker(llm_config, list(self.history), kb_id, use_thinking_model=use_think)
        self.worker.moveToThread(self.thread)
        try:
            self.input_container.btn_stop.clicked.disconnect()
        except:
            pass
        self.input_container.btn_stop.clicked.connect(self.cancel_generation)
        self.thread.started.connect(self.worker.run)
        self.worker.sig_token.connect(self.update_ai_bubble)
        self.worker.sig_finished.connect(self.on_chat_finished)
        self.worker.sig_error.connect(self.on_chat_error)
        self.worker.sig_finished.connect(self.thread.quit)
        self.worker.sig_finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def _show_slow_connection_warning(self):
        if self.current_ai_bubble and getattr(self, '_is_waiting_llm', False):
            idx = getattr(self.current_ai_bubble, 'index', -1)
            base_html = self._format_response(self.current_ai_text, idx)  # 👈 经过格式化
            self.current_ai_bubble.set_content(
                base_html +
                "<br><div style='color:#05B8CC;'><i>🔌 仍在连接...</i></div>"
                "<div style='color:#e6a23c; font-size:12px; margin-top:5px; padding:8px; border:1px solid #e6a23c; border-radius:4px;'>"
                "⚠️ 连接时间比预期的长。请检查您的<b>网络代理 (Proxy)</b> 或 <b>API Endpoint (URL)</b> 是否配置正确。"
                "</div>"
            )
            self.scroll_to_bottom()

    def update_ai_bubble(self, token):
        if not self.current_ai_bubble: return
        sb = self.scroll_area.verticalScrollBar()
        is_at_bottom = (sb.maximum() - sb.value()) <= 15

        idx = getattr(self.current_ai_bubble, 'index', -1)

        if token == "[CLEAR_SEARCH]":
            self.current_ai_text = self.current_ai_text.replace(
                "<i>🔍 Integrating history & searching database...</i>\n\n", "")
            self.current_ai_bubble.set_content(self._format_response(self.current_ai_text, idx))
            if is_at_bottom: self.scroll_to_bottom()
            return

        if token == "[START_LLM_NETWORK]":
            self._is_waiting_llm = True
            base_html = self._format_response(self.current_ai_text, idx)
            self.current_ai_bubble.set_content(
                base_html +
                "<br><div style='color:#05B8CC;'><i>正在连接 LLM 服务商，请稍候...</i></div>"
            )
            from PySide6.QtCore import QTimer
            self.slow_conn_timer = QTimer(self)
            self.slow_conn_timer.setSingleShot(True)
            self.slow_conn_timer.timeout.connect(self._show_slow_connection_warning)
            self.slow_conn_timer.start(8000)
            if is_at_bottom: self.scroll_to_bottom()
            return

        if getattr(self, '_is_waiting_llm', False):
            self._is_waiting_llm = False
            if hasattr(self, 'slow_conn_timer'):
                self.slow_conn_timer.stop()
            if self.current_ai_bubble.is_loading:
                self.current_ai_bubble.set_loading(False)

        self.current_ai_text += token
        self.current_ai_bubble.set_content(self._format_response(self.current_ai_text, idx))
        if is_at_bottom: self.scroll_to_bottom()

    def _format_response(self, text, index):
        """将原始的 <think> 标签动态渲染为可交互的折叠 UI"""
        import re

        def replacer(match):
            content = match.group(1)
            is_closed = match.group(2) == "</think>"

            if index in getattr(self, 'user_toggled_thinks', set()):
                is_expanded = index in self.expanded_thinks
            else:
                is_expanded = not is_closed

            action = "collapse" if is_expanded else "expand"
            icon = "🔽" if is_expanded else "▶️"

            link = f"<a href='think://{action}?index={index}' style='color:#05B8CC; text-decoration:none; white-space:nowrap;'><nobr>{icon} <b>AI Thinking Process</b></nobr></a>"

            if not is_expanded:
                return f"<div style='background:#222; border-left: 3px solid #555; padding: 8px 12px; margin: 10px 0; border-radius: 4px; font-size: 13px; white-space: nowrap;'>{link}</div>"
            else:
                safe_content = content.replace('\n', '<br>')
                suffix = "" if is_closed else " <span style='color:#05B8CC;'><i>...</i></span>"
                return f"<div style='background:#222; border-left: 3px solid #05B8CC; padding: 8px 12px; margin: 10px 0; border-radius: 4px; font-size: 13px; color: #aaa;'>{link}<br><br>{safe_content}{suffix}</div>"

        return re.sub(r'<think>(.*?)(</think>|$)', replacer, text, flags=re.DOTALL)


    def cancel_generation(self):
        if hasattr(self, 'worker') and self.worker: self.worker.cancel()
        ToastManager().show("User forcibly halted the LLM generation.", "warning")
        self.logger.info("User forcibly halted the LLM generation.")

    def on_chat_error(self, msg):
        self.logger.error(f"Chat generation encountered an error: {msg}")
        # 发生错误时停止计时器
        if hasattr(self, 'slow_conn_timer'): self.slow_conn_timer.stop()
        self._is_waiting_llm = False

        # 识别网络错误并美化提示
        if "time" in msg.lower() or "connect" in msg.lower() or "api" in msg.lower():
            display_error = "网络连接失败，请检查配置。\n" + msg
        else:
            display_error = msg

        if self.current_ai_bubble and self.current_ai_bubble.is_loading: self.current_ai_bubble.set_loading(False)
        self.current_ai_text += f"\n\n<div style='color:#ff6b6b; font-weight:bold;'>[⚠️ AI Communication Error]</div>\n<div style='color:#888; font-size:12px;'>{display_error}</div>"
        if self.current_ai_bubble: self.current_ai_bubble.set_content(self.current_ai_text)
        self.scroll_to_bottom()

    def on_chat_finished(self):
        if not self.current_ai_bubble: return

        self.input_container.btn_stop.setVisible(False)
        self.input_container.btn_send.setVisible(True)
        try:
            self.input_container.btn_stop.clicked.disconnect()
        except:
            pass
        if self.current_ai_bubble and self.current_ai_bubble.is_loading: self.current_ai_bubble.set_loading(False)
        full_text = self.current_ai_text
        match = re.search(r'(💡\s*Suggested Follow-ups:.*?(?=<br><hr|$))', full_text, flags=re.IGNORECASE | re.DOTALL)
        questions = []
        if match:
            follow_up_block = match.group(1)
            clean_text = full_text.replace(follow_up_block, "").strip()
            self.current_ai_bubble.set_content(clean_text)
            self.current_ai_text = clean_text
            for line in follow_up_block.split('\n'):
                line = line.strip()
                if line.startswith('-'):
                    q = line.lstrip('-').strip()
                    if q:
                        tag_match = re.match(r'\[(.*?)\]\s*(.*)', q)
                        if tag_match:
                            tag, text = tag_match.groups()
                            questions.append({"tag": tag.strip(), "text": text.strip()})
                        else:
                            questions.append({"tag": "General", "text": q})
            if questions: self.render_follow_up_buttons(questions)
        else:
            idx = getattr(self.current_ai_bubble, 'index', -1)
            final_html = self._format_response(self.current_ai_text, idx) if self.current_ai_text else "No response."
            self.current_ai_bubble.set_content(final_html)

        self.history.append({"role": "assistant", "content": self.current_ai_text})
        self.current_ai_bubble = None

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
        QThread.msleep(10)
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
            return
        last_user_idx = -1
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i]['role'] == 'user':
                last_user_idx = i
                break
        if index != last_user_idx:
            ToastManager().show("You can only edit your most recent message.", "warning")
            return
        self.history = self.history[:index]
        self.clear_layout(self.chat_layout)
        temp_history = list(self.history)
        self.history = []
        for msg in temp_history:
            self.add_bubble(msg['content'], is_user=(msg['role'] == 'user'))
            self.history.append(msg)
        kb_data = self.combo_kb.currentData()
        kb_id = kb_data.get("id") if isinstance(kb_data, dict) else kb_data
        self.add_bubble(new_text, is_user=True)
        self.history.append({"role": "user", "content": new_text})
        self.start_ai_response(kb_id)

    def clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget: widget.deleteLater()
        layout.addStretch()

    def handle_link_click(self, url_str):
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
            if os.path.exists(file_path):
                if self.pdf_viewer is None: self.pdf_viewer = InternalPDFViewer(None)
                self.pdf_viewer.load_document(file_path, page_num, text_snippet)
                ToastManager().show(f"已打开文档，位于第 {page_num + 1} 页", "success")
            else:
                ToastManager().show(f"未找到文件: {file_path}", "error")
        else:
            QDesktopServices.openUrl(QUrl(url_str))


    def refresh_kb_list(self):
        self.load_llm_configs()
        if not hasattr(self, 'combo_kb'): return
        curr_data = self.combo_kb.currentData()
        curr_id = curr_data['id'] if isinstance(curr_data, dict) else curr_data
        self.combo_kb.blockSignals(True)
        self.combo_kb.clear()
        kbs = KBManager().get_all_kbs()
        target_idx = -1
        for kb in kbs:
            if kb.get('status') == 'ready':
                m = get_model_conf(kb.get('model_id'), "embedding")
                m_ui = m['ui_name'] if m else kb.get('model_id', '?')
                display_text = f"{kb['name']}   [Model: {m_ui} | Docs: {kb.get('doc_count', 0)}]"
                self.combo_kb.addItem(display_text, kb)
                if kb['id'] == curr_id: target_idx = self.combo_kb.count() - 1
        if target_idx >= 0:
            self.combo_kb.setCurrentIndex(target_idx)
        elif self.combo_kb.count() > 0:
            self.combo_kb.setCurrentIndex(0)
        else:
            self.combo_kb.setCurrentIndex(-1)
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

    def execute_task(self):
        pass