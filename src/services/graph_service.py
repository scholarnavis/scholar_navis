import networkx as nx
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np
from src.core.database import DatabaseManager


class GraphService:
    def __init__(self):
        self.db = DatabaseManager()

    def build_networkx_graph(self, threshold=0.7):
        """
        构建 NetworkX 图对象
        """
        data = self.db.collection.get(include=['metadatas', 'documents', 'embeddings'])

        if not data or not data['ids']:
            return None, "Database is empty."

        ids = data['ids']
        metas = data['metadatas']
        embeddings = data['embeddings']


        if embeddings is None or len(embeddings) == 0:
            return None, "No embeddings found. Check model settings."

        # 转为矩阵
        matrix = np.array(embeddings)
        count = len(matrix)

        # 2. 计算相似度矩阵 (O(N^2))
        sim_matrix = cosine_similarity(matrix)

        # 3. 创建图
        G = nx.Graph()

        # 添加节点
        for i in range(count):
            source = metas[i].get('source', 'Unknown')
            page = metas[i].get('page', '?')
            G.add_node(i, label=f"{source}", desc=f"Page {page}", cluster=source)

        # 添加连线
        for i in range(count):
            for j in range(i + 1, count):
                sim = sim_matrix[i][j]
                if sim > threshold:
                    G.add_edge(i, j, weight=sim)

        return G, f"Graph built: {count} nodes."