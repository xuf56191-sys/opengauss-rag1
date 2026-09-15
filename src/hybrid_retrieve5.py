"""Vector + BM25 hybrid retrieval with RRF. Optional identifier boost."""
# 模块说明：向量检索 + BM25 关键词检索的混合检索，使用 RRF（倒数排序融合）算法合并结果
# 可选：对包含 snake_case 配置项（如 enable_dolphin_proto）的文档进行微小的排名提升

import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

from dotenv import load_dotenv
from rank_bm25 import BM25Okapi

# 定位项目根目录，并把 src 加入模块搜索路径
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from embed_client1 import embed_texts

# 加载 .env 环境变量，读取 Chroma 存储路径和集合名
load_dotenv(ROOT / ".env")
CHROMA_DIR = os.getenv("CHROMA_DIR", str(ROOT / "data" / "chroma"))
COLLECTION = "opengauss_docs"

# simple tokenizer for mixed Chinese/English/config identifiers
# 轻量级分词器：兼顾中英文和代码标识符
# 匹配规则：
# 1. [A-Za-z_][A-Za-z0-9_]+  匹配英文单词或带下划线的标识符（如 enable_dolphin_proto）
# 2. [0-9]+                  匹配纯数字
# 3. [一-鿿]                 匹配单个中文字符（CJK统一表意文字）
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+|[0-9]+|[一-鿿]")


def tokenize(text: str) -> List[str]:
    """
    Lightweight mixed tokenizer:
    - keep identifiers like enable_dolphin_proto as one token
    - CJK: unigrams (BM25 baseline; good enough for config/notes QA)
    """
    # 将文本转小写后，按上面的正则规则提取所有 token
    tokens = _TOKEN_RE.findall(text.lower())

    # also split snake_case words into subtokens for partial match
    # 额外操作：如果 token 里包含下划线，再拆分成子词，增加部分匹配的概率
    # 例如 enable_dolphin_proto 会额外拆出 enable、dolphin、proto
    extra = []
    for t in tokens:
        if "_" in t:
            extra.extend(t.split("_"))
    return tokens + extra


# 专门用于提取查询中 snake_case 标识符的正则（要求至少包含一个下划线）
IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+")


def extract_identifiers(query: str) -> List[str]:
    """从查询语句中提取可能存在的 snake_case 配置项名称"""
    return [m.group(0).lower() for m in IDENTIFIER_RE.finditer(query)]


class HybridRetriever:
    """混合检索器：同时使用向量语义检索和 BM25 关键词检索"""

    def __init__(self):
        import chromadb

        # 连接 Chroma 持久化客户端并获取集合
        self.client = chromadb.PersistentClient(path=CHROMA_DIR)
        self.col = self.client.get_collection(COLLECTION)

        # 一次性把库里所有文档、元数据和 ID 全部取出来，用于构建 BM25 索引
        got = self.col.get(include=["documents", "metadatas"])
        self.ids = got["ids"]
        self.docs = got["documents"]
        self.metas = got["metadatas"]

        # 建立 ID 到数组位置的映射，方便后续快速通过 ID 找到对应的文本和元数据
        self.id_to_pos = {i: pos for pos, i in enumerate(self.ids)}

        # 对所有文档进行分词，并初始化 BM25 模型
        corpus_tokens = [tokenize(d) for d in self.docs]
        self.bm25 = BM25Okapi(corpus_tokens)

    def _vector_ranked(self, query: str, k: int) -> List[str]:
        """向量检索通道：把查询转向量，去 Chroma 里找最相似的 k 个文档 ID"""
        qvec = embed_texts([query])[0]
        res = self.col.query(
            query_embeddings=[qvec],
            n_results=k,
            include=["distances"],
        )
        # Chroma 返回的结果已经按相似度排好序，直接返回 ID 列表
        return res["ids"][0]

    def _bm25_ranked(self, query: str, k: int) -> List[str]:
        """BM25 检索通道：对查询分词，计算 BM25 得分，返回得分最高的 k 个文档 ID"""
        tokens = tokenize(query)
        scores = self.bm25.get_scores(tokens)
        # 按得分从高到低排序，取前 k 个
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return [self.ids[i] for i in ranked[:k]]

    def search(self, query: str, k: int = 3, pool: int = 10) -> List[Dict]:
        """混合检索主入口：合并两路召回结果，用 RRF 重新排序，返回最终 Top-K"""
        # 1. 分别从两个通道各取 pool（默认10）个候选文档
        vec_ids = self._vector_ranked(query, pool)
        bm_ids = self._bm25_ranked(query, pool)

        # 2. RRF（Reciprocal Rank Fusion，倒数排序融合）
        # RRF 的核心思想：不直接比较两路分数的绝对值，而是比较“排名”
        # 公式：score = 1 / (60 + rank)，60 是平滑常数，防止排名靠前的分数差距过大
        idents = extract_identifiers(query)
        # 动态权重策略：
        # 如果查询里包含标识符（如 snake_case 的配置项），说明用户在找精确匹配的字面信息。
        # 这种情况下，更信任 BM25（词法检索），所以 BM25 权重更大 (0.55 vs 0.45)。
        # 如果查询是纯自然语言（没有标识符），说明用户在表达语义，更信任向量检索（语义检索），
        # 所以向量权重更大 (0.75 vs 0.25)。
        # no identifiers → trust semantic more; identifiers → trust lexical more
        w_vec, w_bm25 = (0.45, 0.55) if idents else (0.75, 0.25)

        # RRF（Reciprocal Rank Fusion，倒数排序融合）—— 加权版
        # 核心思想：不直接比较两路分数的绝对值，而是比较“排名”。
        # 基础公式：score = 权重 / (60 + rank)
        # 60 是平滑常数，防止排名靠前的分数差距过大；权重由上面的动态策略决定。
        rrf: Dict[str, float] = {}
        for rank, doc_id in enumerate(vec_ids, start=1):
            rrf[doc_id] = rrf.get(doc_id, 0.0) + w_vec / (60 + rank)
        for rank, doc_id in enumerate(bm_ids, start=1):
            rrf[doc_id] = rrf.get(doc_id, 0.0) + w_bm25 / (60 + rank)

        # 3. identifier boost: if query has snake_case ids, nudge docs that contain them
        # 标识符提升：如果查询里包含 snake_case 配置项（比如 enable_dolphin_proto），
        # 而文档里也出现了这个标识符，就给这篇文档一点微小的分数加成（+0.02）
        # 注意：0.02 是经验值，只是为了微调排名，不能盖过 RRF 本身的排序逻辑

        if idents:
            for doc_id, score in list(rrf.items()):
                pos = self.id_to_pos[doc_id]
                text = self.docs[pos].lower()
                if any(ident in text for ident in idents):
                    rrf[doc_id] = score + 0.02  # small, ranking-level nudge

        # 4. 根据最终得分排序，截取前 k 个
        ranked_ids = sorted(rrf, key=lambda x: rrf[x], reverse=True)[:k]

        # 5. 组装返回结果，附带详细的排名信息，方便调试和评估
        out = []
        for doc_id in ranked_ids:
            pos = self.id_to_pos[doc_id]
            out.append(
                {
                    "id": doc_id,
                    "score": rrf[doc_id],  # RRF 最终得分
                    "text": self.docs[pos],  # 文档原文
                    "meta": self.metas[pos],  # 文档元数据
                    "in_vector": doc_id in vec_ids,  # 是否被向量通道召回
                    "in_bm25": doc_id in bm_ids,  # 是否被 BM25 通道召回
                    # 分别记录在两路通道里的排名，便于分析 RRF 的融合效果
                    "vector_rank": vec_ids.index(doc_id) + 1 if doc_id in vec_ids else None,
                    "bm25_rank": bm_ids.index(doc_id) + 1 if doc_id in bm_ids else None,
                }
            )
        return out


def main():
    """测试入口：用三个典型问题验证混合检索的效果"""
    hr = HybridRetriever()

    # 覆盖三种不同类型的查询：自然语言、特定配置项、带有因果逻辑的疑问
    queries = [
        "Peewee 怎么连 openGauss B 兼容模式？",
        "enable_dolphin_proto 是干什么的？",
        "为什么 PyMySQL 的 database 参数要和 schema 同名？",
    ]

    for q in queries:
        print("=" * 60)
        print("Q:", q)
        # 打印从查询中提取出的标识符，方便观察 identifier boost 是否生效
        print("identifiers:", extract_identifiers(q))

        # 执行混合检索，取前 3 个结果
        for i, hit in enumerate(hr.search(q, k=3), 1):
            path = hit["meta"].get("heading_path")
            preview = hit["text"][:100].replace("\n", " / ")

            # 打印 RRF 得分、两路排名情况、标题路径和文本预览
            print(
                f"  #{i} rrf={hit['score']:.5f} "
                f"vec_rank={hit['vector_rank']} bm25_rank={hit['bm25_rank']} "
                f"path={path!r}"
            )
            print(f"     {preview}...")


if __name__ == "__main__":
    main()