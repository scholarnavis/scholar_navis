import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
                               QPushButton, QSplitter, QTextBrowser)
from PySide6.QtCore import QObject, Signal, Qt

from src.tools.base_tool import BaseTool
from src.core.database import DatabaseManager


class RadarWorker(QObject):
    sig_result = Signal(dict, str)
    sig_finished = Signal()

    def __init__(self, topic, llm_config):
        super().__init__()
        self.topic = topic
        self.llm_config = llm_config
        self.db = DatabaseManager()

    def run(self):
        # 1. 检索
        results = self.db.query(self.topic, n_results=10)
        docs = results['documents'][0]

        context = "\n".join([f"Excerpt {i + 1}: {d}" for i, d in enumerate(docs)])

        prompt = (
            f"Analyze the following excerpts regarding: '{self.topic}'.\n"
            "For each excerpt, determine if it Supports, Refutes, or is Neutral to the topic.\n"
            "Output JSON format only: {'support': count, 'refute': count, 'neutral': count, 'summary': 'analysis text'}\n"
            f"\nExcerpts:\n{context}"
        )

        # 模拟数据返回
        import time
        time.sleep(1)

        # 伪造数据用于演示 (实际要解析 LLM 输出)
        stats = {'Support': 5, 'Refute': 2, 'Neutral': 3}
        report = f"Analysis of '{self.topic}':\n\nMost literature supports this view, but Excerpt 3 and 7 present conflicting evidence regarding condition X..."

        self.sig_result.emit(stats, report)
        self.sig_finished.emit()


class RadarTool(BaseTool):
    def __init__(self):
        super().__init__("Contradiction Radar")
        self.widget = None

    def get_ui_widget(self) -> QWidget:
        if self.widget: return self.widget

        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)

        # 输入
        input_layout = QHBoxLayout()
        self.inp_topic = QLineEdit()
        self.inp_topic.setPlaceholderText("Enter a proposition (e.g. 'Coffee causes cancer')")
        btn = QPushButton("Scan for Contradictions")
        btn.clicked.connect(self.start_scan)
        input_layout.addWidget(self.inp_topic)
        input_layout.addWidget(btn)
        layout.addLayout(input_layout)

        # 分割
        splitter = QSplitter(Qt.Orientation.Vertical)

        # 图表区
        self.figure = plt.figure(facecolor='#1e1e1e')
        self.canvas = FigureCanvas(self.figure)
        splitter.addWidget(self.canvas)

        # 文本区
        self.browser = QTextBrowser()
        splitter.addWidget(self.browser)

        layout.addWidget(splitter)
        return self.widget

    def start_scan(self):
        # 模拟数据绘制
        self.figure.clear()
        ax = self.figure.add_subplot(111)

        labels = ['Support', 'Refute', 'Neutral']
        sizes = [60, 15, 25]
        colors = ['#4caf50', '#f44336', '#9e9e9e']

        ax.pie(sizes, labels=labels, colors=colors, autopct='%1.1f%%', startangle=90)
        ax.axis('equal')
        ax.set_title(f"Consensus Analysis: {self.inp_topic.text()}", color='white')

        self.canvas.draw()
        self.browser.setText("### AI Analysis Report\n\nGenerating conflict analysis based on retrieved segments...")