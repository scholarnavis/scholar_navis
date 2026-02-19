import json
import os
import markdown
import numpy as np
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QTextBrowser,
                               QLineEdit, QPushButton, QComboBox, QLabel,
                               QProgressBar, QGroupBox, QFormLayout)
from PySide6.QtCore import Qt, Signal, QObject, QThread
from sklearn.cluster import KMeans

from src.tools.base_tool import BaseTool
from src.core.llm_impl import OpenAICompatibleLLM
from src.core.database import DatabaseManager


class GapAnalysisWorker(QObject):
    sig_log = Signal(str)  # 进度日志
    sig_token = Signal(str)  # LLM 流式输出
    sig_finished = Signal()  # 完成
    sig_error = Signal(str)  # 错误

    def __init__(self, topic_a, topic_b, llm_config):
        super().__init__()
        self.topic_a = topic_a
        self.topic_b = topic_b
        self.llm_config = llm_config
        self.db = DatabaseManager()

    def run(self):
        try:
            self.sig_log.emit("🔍 Phase 1: Retrieving Literature Context...")

            # 1. 分别检索 Topic A 和 Topic B 的上下文
            # 我们限制数量，避免爆 Token
            res_a = self.db.query(self.topic_a, n_results=5)
            res_b = self.db.query(self.topic_b, n_results=5)

            context_a = self._format_docs(res_a, "Topic A")
            context_b = self._format_docs(res_b, "Topic B")

            if not context_a and not context_b:
                raise ValueError("No relevant documents found in the library.")

            self.sig_log.emit("🧠 Phase 2: Constructing Logic Matrix...")

            # 2. 构建超级 Prompt (Research Gap Framework)
            system_prompt = (
                "You are a Senior Principal Investigator (PI) in this field. "
                "Your task is to identify 'Research Gaps' by synthesizing two research topics based strictly on the provided literature.\n"
                "You must cite sources using [Source: Page] format.\n"
                "----------------\n"
                "Output Structure:\n"
                "1. **State of the Art**: Briefly summarize key findings for Topic A and Topic B.\n"
                "2. **The Intersection**: What is the current known relationship between A and B?\n"
                "3. **Identified Gaps**: Find contradictions, underexplored mechanisms, or methodological limitations.\n"
                "4. **Proposed Novel Projects**: Propose 3 specific, innovative research titles and rationales based on these gaps.\n"
            )

            user_prompt = (
                f"Topic A: {self.topic_a}\n"
                f"Topic B: {self.topic_b}\n\n"
                f"--- Literature Context for A ---\n{context_a}\n\n"
                f"--- Literature Context for B ---\n{context_b}\n\n"
                "Please generate the Research Gap Report:"
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

            self.sig_log.emit("🚀 Phase 3: Generating Report via LLM...")

            # 3. 调用 LLM
            llm = OpenAICompatibleLLM(self.llm_config)
            for token in llm.stream_chat(messages):
                self.sig_token.emit(token)

        except Exception as e:
            import traceback
            self.sig_error.emit(str(e) + "\n" + traceback.format_exc())
        finally:
            self.sig_finished.emit()

    def _format_docs(self, results, label):
        """将数据库返回的 dict 格式化为字符串"""
        if not results or not results['documents']:
            return f"No documents found for {label}."

        text = ""
        docs = results['documents'][0]
        metas = results['metadatas'][0]

        for i, doc in enumerate(docs):
            source = metas[i].get('source', 'Unknown')
            page = metas[i].get('page', '?')
            text += f"[{source} (P{page})]: {doc[:300]}...\n"  # 截取前300字符

        return text


# --- UI 界面 ---
class GapMinerTool(BaseTool):
    def __init__(self):
        super().__init__("Gap Miner")
        self.widget = None
        self.worker_thread = None

    def get_ui_widget(self) -> QWidget:
        if self.widget: return self.widget

        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)

        # --- 1. 设置区 (Input Panel) ---
        input_group = QGroupBox("⛏️ Research Gap Excavation Settings")
        input_group.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #444; margin-top: 10px; padding-top: 10px; }")
        form_layout = QFormLayout(input_group)
        form_layout.setSpacing(10)

        # 话题输入
        self.inp_topic_a = QLineEdit()
        self.inp_topic_a.setPlaceholderText("e.g. Drought Stress (干旱胁迫)")
        self.inp_topic_b = QLineEdit()
        self.inp_topic_b.setPlaceholderText("e.g. Root Architecture (根系构型)")

        # 模型选择 (复用 Chat 的配置逻辑)
        self.combo_llm = QComboBox()
        self.load_llm_configs()

        form_layout.addRow("Research Topic A:", self.inp_topic_a)
        form_layout.addRow("Research Topic B:", self.inp_topic_b)
        form_layout.addRow("Reasoning Model:", self.combo_llm)

        # 按钮
        self.btn_analyze = QPushButton("🚀 Generate Gap Report")
        self.btn_analyze.setMinimumHeight(45)
        self.btn_analyze.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_analyze.setStyleSheet(
            "background-color: #007acc; color: white; font-weight: bold; font-size: 14px; border-radius: 5px;")
        self.btn_analyze.clicked.connect(self.start_analysis)

        form_layout.addRow("", self.btn_analyze)  # 占位
        layout.addWidget(input_group)

        # --- 2. 进度条 ---
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 0)  # 忙碌模式
        layout.addWidget(self.progress_bar)

        self.lbl_status = QLabel("Ready to identify novel research opportunities.")
        self.lbl_status.setStyleSheet("color: #888; font-style: italic;")
        layout.addWidget(self.lbl_status)

        # --- 3. 报告输出区 (Report Viewer) ---
        self.browser = QTextBrowser()
        self.browser.setStyleSheet(
            "background-color: #1e1e1e; border: 1px solid #333; padding: 15px; font-family: 'Segoe UI'; font-size: 14px;")
        self.browser.setPlaceholderText("The analysis report will appear here...")
        self.browser.setOpenExternalLinks(True)

        layout.addWidget(self.browser)

        return self.widget

    def load_llm_configs(self):
        """加载 LLM 配置 (复用逻辑)"""
        path = os.path.join(os.getcwd(), "config", "llm_config.json")
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    configs = json.load(f)
                    for cfg in configs:
                        self.combo_llm.addItem(cfg['name'], cfg)
            except:
                pass

    def start_analysis(self):
        topic_a = self.inp_topic_a.text().strip()
        topic_b = self.inp_topic_b.text().strip()

        if not topic_a or not topic_b:
            self.lbl_status.setText("⚠️ Please enter both topics to find the intersection.")
            return

        # UI 状态更新
        self.btn_analyze.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.browser.clear()
        self.browser.append("<h1>🧪 Research Gap Analysis Report</h1><hr>")
        self.current_report = ""  # 缓存 Markdown

        # 启动线程
        llm_config = self.combo_llm.currentData()

        self.thread = QThread()
        self.worker = GapAnalysisWorker(topic_a, topic_b, llm_config)
        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.run)
        self.worker.sig_log.connect(self.update_log)
        self.worker.sig_token.connect(self.update_report)
        self.worker.sig_finished.connect(self.on_finished)
        self.worker.sig_finished.connect(self.thread.quit)
        self.worker.sig_finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)

        self.thread.start()

    def update_log(self, msg):
        self.lbl_status.setText(msg)

    def update_report(self, token):
        self.current_report += token
        # 简单渲染 Markdown
        html = markdown.markdown(self.current_report)
        self.browser.setHtml(html)
        # 滚动到底部
        sb = self.browser.verticalScrollBar()
        sb.setValue(sb.maximum())

    def on_finished(self):
        self.btn_analyze.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.lbl_status.setText("Analysis Complete.")
        self.browser.append("<br><hr><i>Report generated by Scholar Navis Pro Gap Miner Engine.</i>")


# 新增 Worker
class GlobalGapAnalysisWorker(QObject):
    sig_log = Signal(str)
    sig_token = Signal(str)
    sig_finished = Signal()
    sig_error = Signal(str)

    def __init__(self, llm_config, n_clusters=5):
        super().__init__()
        self.llm_config = llm_config
        self.n_clusters = n_clusters
        self.db = DatabaseManager()

    def run(self):
        try:
            self.sig_log.emit("🔍 Phase 1: Analyzing Knowledge Landscape (K-Means)...")

            # 1. 获取所有向量
            data = self.db.collection.get(include=['embeddings', 'documents', 'metadatas'])
            if not data['embeddings']:
                raise ValueError("Database is empty!")

            matrix = np.array(data['embeddings'])
            docs = data['documents']
            metas = data['metadatas']

            # 2. 聚类
            n_clusters = min(self.n_clusters, len(matrix))
            kmeans = KMeans(n_clusters=n_clusters, random_state=42)
            labels = kmeans.fit_predict(matrix)

            # 3. 抽取每个聚类的核心观点
            self.sig_log.emit("🧠 Phase 2: Summarizing Research Clusters...")
            cluster_summaries = []

            for i in range(n_clusters):
                # 找到距离聚类中心最近的文档
                center = kmeans.cluster_centers_[i]
                cluster_indices = np.where(labels == i)[0]

                # 计算距离
                distances = np.linalg.norm(matrix[cluster_indices] - center, axis=1)
                nearest_idx = cluster_indices[np.argmin(distances)]

                # 获取代表性文本
                rep_text = docs[nearest_idx][:500]  # 截取前500字
                source = metas[nearest_idx].get('source', 'Unknown')

                cluster_summaries.append(f"Cluster {i + 1} (Rep: {source}): {rep_text}")

            # 4. 构建 Global Gap Prompt
            prompt_context = "\n\n".join(cluster_summaries)

            system_prompt = (
                "You are a visionary scientist. "
                "I have clustered the entire literature database into the following topics. "
                "Your task is to find the 'Missing Links' between these clusters.\n"
                "----------------\n"
                f"{prompt_context}\n"
                "----------------\n"
                "Task:\n"
                "1. Briefly name each cluster.\n"
                "2. Identify relationships between them.\n"
                "3. **Find the Global Gap**: What major connection is missing between these clusters?\n"
                "4. Propose a high-impact research direction that unifies at least 3 clusters."
            )

            messages = [{"role": "user", "content": system_prompt}]

            self.sig_log.emit("🚀 Phase 3: Generating Global Report...")

            llm = OpenAICompatibleLLM(self.llm_config)
            for token in llm.stream_chat(messages):
                self.sig_token.emit(token)

        except Exception as e:
            import traceback
            self.sig_error.emit(str(e) + "\n" + traceback.format_exc())
        finally:
            self.sig_finished.emit()


