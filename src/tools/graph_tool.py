from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                               QLabel, QSlider)
from PySide6.QtCore import Qt

# Matplotlib 集成库
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
import networkx as nx

from src.tools.base_tool import BaseTool
from src.services.graph_service import GraphService


class GraphTool(BaseTool):
    def __init__(self):
        super().__init__("Knowledge Graph")
        self.graph_service = GraphService()
        self.widget = None
        self.canvas = None

    def get_ui_widget(self) -> QWidget:
        if self.widget: return self.widget

        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)

        # --- 1. 顶部工具栏 ---
        top_bar = QHBoxLayout()

        self.btn_draw = QPushButton("🎨 Draw Graph")
        self.btn_draw.setStyleSheet("background-color: #007acc; color: white; padding: 6px; font-weight: bold;")
        self.btn_draw.clicked.connect(self.draw_graph)

        # 阈值滑块：控制连线的稀疏程度
        lbl_thresh = QLabel("Similarity Threshold:")
        lbl_thresh.setStyleSheet("color: #ccc;")
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 95)  # 0.0 - 0.95
        self.slider.setValue(60)  # Default 0.6
        self.slider.setFixedWidth(150)

        top_bar.addWidget(self.btn_draw)
        top_bar.addSpacing(20)
        top_bar.addWidget(lbl_thresh)
        top_bar.addWidget(self.slider)
        top_bar.addStretch()

        layout.addLayout(top_bar)

        # --- 2. Matplotlib 画布区域 ---
        # 创建 Figure 对象
        self.figure = Figure(figsize=(8, 6), dpi=100, facecolor='#1e1e1e')  # 深色背景
        self.canvas = FigureCanvas(self.figure)

        # 添加 Matplotlib 原生的工具栏 (放大镜、保存图片等)
        self.toolbar = NavigationToolbar(self.canvas, self.widget)
        self.toolbar.setStyleSheet("background-color: #ccc;")  # 工具栏浅色，否则看不清图标

        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas)

        return self.widget

    def draw_graph(self):
        # 获取阈值
        threshold = self.slider.value() / 100.0
        self.log(f"Building graph with similarity > {threshold}...")

        # 异步或者同步执行 (这里图不大，同步也可，为了安全还是建议异步，这里简化演示同步)
        # 1. 计算图结构
        G, msg = self.graph_service.build_networkx_graph(threshold=threshold)

        if not G or G.number_of_nodes() == 0:
            self.log("No nodes to draw. Import PDFs first!")
            return

        # 2. 清空画布
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.set_facecolor('#1e1e1e')  # 坐标轴背景

        # 3. 计算布局 (Force-Directed Layout)
        # 这就是让相似节点聚在一起的核心算法
        pos = nx.spring_layout(G, k=0.5, seed=42)

        # 4. 绘图 (学术风格)
        # 绘制点
        nx.draw_networkx_nodes(G, pos, ax=ax, node_size=50, node_color='#05B8CC', alpha=0.8)

        # 绘制线 (透明度设低一点，营造朦胧感)
        nx.draw_networkx_edges(G, pos, ax=ax, width=0.5, edge_color='#555', alpha=0.3)


        ax.set_title(f"Literature Semantic Cluster (N={G.number_of_nodes()})", color='white')
        ax.axis('off')  # 隐藏坐标轴

        # 5. 刷新画布
        self.canvas.draw()
        self.log(msg)