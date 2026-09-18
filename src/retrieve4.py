"""Pure vector retrieval smoke test."""
# 模块说明：纯向量检索冒烟测试
# 这是一个轻量级的测试脚本，用来验证之前 ingest.py 写入 Chroma 的数据
# 是否能通过向量相似度正常检索出来（冒烟测试 = 快速验证核心功能是否跑通）

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# 计算项目根目录（当前文件的上一级目录的上一级）
ROOT = Path(__file__).resolve().parents[1]

# 将 src 目录加入 Python 模块搜索路径，确保能正确导入 embed_client
sys.path.insert(0, str(ROOT / "src"))

from embed_client1 import embed_texts

# 加载根目录下的 .env 文件
load_dotenv(ROOT / ".env")

# 获取 Chroma 持久化目录（默认在 data/chroma 下；相对路径相对仓库根）
_raw_chroma = os.getenv("CHROMA_DIR") or str(ROOT / "data" / "chroma")
CHROMA_DIR = str(Path(_raw_chroma) if Path(_raw_chroma).is_absolute() else (ROOT / _raw_chroma))

# 集合名称（与 ingest.py 中写入时保持完全一致）
COLLECTION = "opengauss_docs"


def search(query: str, k: int = 3):
    """核心检索函数：把问题转向量，去 Chroma 里找最相似的 k 个片段"""
    import chromadb

    # 连接本地持久化的 Chroma 数据库
    client = chromadb.PersistentClient(path=CHROMA_DIR)

    # 获取之前建好的集合（注意：这里不用 get_or_create，因为测试前提是数据已入库）
    col = client.get_collection(COLLECTION)

    # 把用户的查询词（query）转成向量，embed_texts 返回的是列表，取第一个元素
    qvec = embed_texts([query])[0]

    # 在 Chroma 中执行向量查询
    res = col.query(
        query_embeddings=[qvec],  # 传入查询向量
        n_results=k,  # 返回最相似的前 k 个结果
        include=["documents", "metadatas", "distances"],  # 要求返回文档内容、元数据、距离
    )
    return res


def main():
    """主测试逻辑：定义几个问题，逐个去检索，并打印结果"""
    # 预设三个测试问题，覆盖不同的知识面
    queries = [
        "Peewee 怎么连 openGauss B 兼容模式？",
        "enable_dolphin_proto 是干什么的？",
        "为什么 PyMySQL 的 database 参数要和 schema 同名？",
    ]

    for q in queries:
        # 打印分隔线和当前问题
        print("=" * 60)
        print("Q:", q)

        # 调用检索函数，取前 3 个最相似的结果
        res = search(q, k=3)

        # 从返回结果中提取文档、元数据和距离
        # 注意：Chroma 返回的结构是嵌套列表，外层对应每个查询（这里只有1个），内层对应 k 个结果
        docs = res["documents"][0]
        metas = res["metadatas"][0]
        dists = res["distances"][0]

        # 遍历这 3 个结果，依次打印
        for i, (d, m, dist) in enumerate(zip(docs, metas, dists), 1):
            # 取文档前 100 个字符做预览，把换行替换成 " / " 方便单行显示
            preview = d[:100].replace("\n", " / ")

            # 打印排名、距离（越小越相似，余弦距离下 0 表示完全一致）、标题路径
            print(f"  #{i} dist={dist:.4f} path={m.get('heading_path')!r}")

            # 打印文档预览
            print(f"     {preview}...")


if __name__ == "__main__":
    main()