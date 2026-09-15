"""Chunk markdown files, embed via SiliconFlow, persist into Chroma."""
# 模块说明：将 Markdown 文件分块，通过 SiliconFlow 生成嵌入向量，并持久化到 Chroma 向量数据库中
#给个人 Markdown 笔记建立“语义索引”，把它们变成 AI 可以快速检索的知识库（RAG 的数据准备阶段）

import os
import sys
from pathlib import Path
"""获取或创建 Chroma 集合"""
import chromadb
from dotenv import load_dotenv

# allow `python src/ingest3.py` from repo root
# 动态获取项目根目录（当前文件的上一级目录的上一级）
ROOT = Path(__file__).resolve().parents[1]

# 将项目的 src 目录添加到 Python 的模块搜索路径中
# 这样即使直接运行 `python src/ingest.py`，也能正确导入 src 下的 chunker 和 embed_client
sys.path.insert(0, str(ROOT / "src"))

from chunker2 import chunk_markdown
from embed_client1 import embed_texts

# 加载根目录下的 .env 文件（显式指定路径，避免受运行目录影响）
load_dotenv(ROOT / ".env")

# 从环境变量读取 Chroma 持久化目录，如果没有配置则默认存到 data/chroma
CHROMA_DIR = os.getenv("CHROMA_DIR", str(ROOT / "data" / "chroma"))

# Chroma 集合名称（相当于数据库中的表名）
COLLECTION = "opengauss_docs"

# 每次调用 API 嵌入的文档数量，避免单次请求过大
BATCH_SIZE = 8


def get_collection():


    # 创建持久化客户端，数据会落盘到 CHROMA_DIR 目录
    client = chromadb.PersistentClient(path=CHROMA_DIR)

    # bge-m3 is 1024-dim; Chroma default space is cosine L2-ish — we use cosine
    # bge-m3 模型输出 1024 维向量；这里显式指定使用余弦距离（cosine）来计算向量相似度
    # 这是最常用的距离度量，适合大多数文本嵌入模型
    return client.get_or_create_collection(
        name=COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )


def ingest_file(md_path: Path) -> int:
    """处理单个 Markdown 文件：分块 -> 嵌入 -> 存入 Chroma，返回处理的块数"""
    # 读取文件内容
    raw = md_path.read_text(encoding="utf-8")

    # 计算相对于项目根目录的路径（用作唯一来源标识）
    rel = md_path.relative_to(ROOT).as_posix()

    # 调用分块器，将 Markdown 切分成多个 Chunk 对象
    chunks = chunk_markdown(raw, source=rel)

    # 如果没有分块，直接返回 0
    if not chunks:
        return 0

    # 获取 Chroma 集合
    col = get_collection()

    # 准备要写入 Chroma 的三个列表
    ids = []         # 每个块的唯一 ID
    documents = []   # 每个块的文本内容
    metadatas = []   # 每个块的元数据（来源、标题路径、序号）

    for c in chunks:
        # 生成唯一 ID：来源路径::块索引
        ids.append(f"{rel}::{c.chunk_index}")

        # 文档内容
        documents.append(c.text)

        # 元数据：将来可以在检索时用来过滤或展示来源信息
        metadatas.append(
            {
                "source": rel,
                "heading_path": c.heading_path,
                "chunk_index": c.chunk_index,
            }
        )

    # upsert in batches
    # 分批调用嵌入 API 并写入 Chroma，避免单次请求过大
    n = 0
    for i in range(0, len(documents), BATCH_SIZE):
        # 切割出当前批次的文档、ID 和元数据
        batch_docs = documents[i : i + BATCH_SIZE]
        batch_ids = ids[i : i + BATCH_SIZE]
        batch_meta = metadatas[i : i + BATCH_SIZE]

        # 调用 SiliconFlow API 生成嵌入向量
        vecs = embed_texts(batch_docs)

        # 使用 upsert：如果 ID 已存在则更新，不存在则插入
        # 这样重复运行 ingest 脚本不会报错，也不会产生重复数据
        col.upsert(
            ids=batch_ids,
            documents=batch_docs,
            embeddings=vecs,
            metadatas=batch_meta,
        )

        n += len(batch_docs)
        print(f"  upserted {n}/{len(documents)}")

    return n


def main():
    """主入口：遍历指定的 Markdown 文件列表并执行入库"""
    # 待处理的文件列表（目前只有 peewee-notes.md，后续可以扩展）
    files = [
        ROOT / "personal" / "peewee-notes.md",
    ]

    total = 0

    for f in files:
        # 如果文件不存在，跳过并提示
        if not f.exists():
            print(f"skip missing: {f}")
            continue

        # 打印正在处理的文件（相对于根目录）
        print(f"ingest {f.relative_to(ROOT).as_posix()}")

        # 调用 ingest_file 处理该文件，并累加处理的块数
        total += ingest_file(f)

    # 全部处理完后，打印统计信息
    col = get_collection()
    print(f"done. chunks={total} collection_count={col.count()}")
    print(f"chroma_dir={CHROMA_DIR}")


if __name__ == "__main__":
    main()