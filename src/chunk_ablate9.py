"""Phase7: chunk length ablation — ingest 800/1200/1600 and compare hybrid Recall@k."""
# 模块说明：Phase7 切片长度消融实验
# 目标：用同一套评测题，对比不同 max_chars 参数下，混合检索的 Recall@k 表现
# 产出：docs/chunk_ablate.md 报告，用于决定生产环境的最优分块大小

import json
import sys
from pathlib import Path

import chromadb
from dotenv import load_dotenv

# 定位项目根目录，把 src 加入模块搜索路径
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env", override=True)

from chunker2 import chunk_markdown          # noqa: E402   # 分块器（Phase7 版本）
from embed_client1 import embed_texts        # noqa: E402   # 嵌入函数
from hybrid_retrieve5 import HybridRetriever, CHROMA_DIR  # noqa: E402
from eval8 import load_cases, hit_any        # noqa: E402   # 复用评测用例和命中判定

# 与生产默认一致的 overlap 比例约 10%
# 四个待对比的切片长度：800 / 1200 / 1600 / 400
# 每个配置的 overlap_chars 都取 max_chars 的 10%，保证变量单一（只对比长度）
SIZES = [
    {"label": "c800",  "max_chars": 800,  "overlap_chars": 80},
    {"label": "c1200", "max_chars": 1200, "overlap_chars": 120},
    {"label": "c1600", "max_chars": 1600, "overlap_chars": 160},
    {"label": "c400",  "max_chars": 400,  "overlap_chars": 40},
]

# 待消融的语料文件和嵌入批大小
MD = ROOT / "personal" / "peewee-notes.md"
BATCH = 8


def ingest_size(label: str, max_chars: int, overlap_chars: int) -> int:
    """针对某个切片长度配置，把语料 ingest 到一个独立的 collection 里"""
    # 每个配置使用独立的 collection 名称，物理隔离，互不污染
    # 这正是"collection 参数化"带来的好处：不同实验可以并行存在
    col_name = f"opengauss_{label}"
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    col = client.get_or_create_collection(
        name=col_name,
        metadata={"hnsw:space": "cosine"},
    )

    # 重建集合，保证消融可重复
    # 先取出现有所有 ID，再逐一删除，确保旧数据不会残留导致实验污染
    ids = col.get(include=[])["ids"]
    if ids:
        col.delete(ids=ids)

    # 读取 Markdown 源文件
    raw = MD.read_text(encoding="utf-8")
    rel = MD.relative_to(ROOT).as_posix()

    # 用指定的 max_chars 和 overlap_chars 进行分块
    # 这是本实验唯一变化的变量，其他都保持一致
    chunks = chunk_markdown(
        raw,
        source=rel,
        max_chars=max_chars,
        overlap_chars=overlap_chars,
    )

    # 准备写入 Chroma 的三个列表
    documents = [c.text for c in chunks]
    metadatas = [
        {
            "source": rel,
            "heading_path": c.heading_path,
            "chunk_index": c.chunk_index,
        }
        for c in chunks
    ]
    chunk_ids = [f"{rel}::{c.chunk_index}" for c in chunks]

    # 分批嵌入并 upsert 到该配置专属的 collection
    n = 0
    for i in range(0, len(documents), BATCH):
        batch_docs = documents[i : i + BATCH]
        vecs = embed_texts(batch_docs)
        col.upsert(
            ids=chunk_ids[i : i + BATCH],
            documents=batch_docs,
            embeddings=vecs,
            metadatas=metadatas[i : i + BATCH],
        )
        n += len(batch_docs)
        print(f"  [{label}] upserted {n}/{len(documents)}")

    # 打印该配置下的 chunk 总数，方便对比不同长度对 chunk 数量的影响
    print(f"  [{label}] collection={col_name} count={col.count()}")
    return col.count()


def eval_collection(label: str, cases: list, k: int = 3) -> dict:
    """针对某个切片配置的 collection，评测混合检索的 Recall@1 和 Recall@k"""
    # 关键：HybridRetriever 现在支持传入 collection 参数
    # 如果写死 collection，这里就没法做消融对比了
    hr = HybridRetriever(collection=f"opengauss_{label}")

    r1 = rk = 0
    rows = []
    for c in cases:
        # 用同一套评测用例去查询该 collection
        hits = hr.search(c["query"], k=k)

        # 判定 Top-1 和 Top-k 是否命中关键词
        h1 = hit_any(hits[:1], c["keywords"])
        hk = hit_any(hits[:k], c["keywords"])
        r1 += int(h1)
        rk += int(hk)

        rows.append({"id": c["id"], "r1": h1, "rk": hk})

    n = len(cases)
    return {
        "label": label,
        "n_chunks": len(hr.ids),   # 该配置下的 chunk 总数
        "recall1": r1 / n if n else 0.0,
        "recall_k": rk / n if n else 0.0,
        "rows": rows,
    }


def main() -> None:
    """主流程：对每个切片长度配置，依次 ingest -> 评测 -> 汇总报告"""
    # 只取 source 组（来源题），因为是评测检索质量，不含越界题
    cases = load_cases()["source"]
    results = []

    # 遍历四个切片长度配置，逐一跑完整流程
    for s in SIZES:
        print(f"=== ingest {s['label']} max_chars={s['max_chars']} ===")

        # 步骤 1：把语料 ingest 到该配置专属的 collection
        ingest_size(s["label"], s["max_chars"], s["overlap_chars"])

        # 步骤 2：用同一套评测题，在该 collection 上跑混合检索，计算 Recall
        metrics = eval_collection(s["label"], cases, k=3)
        results.append(metrics)

        print(
            f"  eval {s['label']}: chunks={metrics['n_chunks']} "
            f"R@1={metrics['recall1']:.2%} R@3={metrics['recall_k']:.2%}"
        )

    # 组装 Markdown 报告
    lines = [
        "# Phase7 切片长度消融（混合检索 Recall）",
        "",
        f"- 语料：`personal/peewee-notes.md`",
        f"- 来源题：{len(cases)} 条（与 eval8 同一套 keywords）",
        "- 指标：混合 BM25+向量，k=3；小样本回归，非生产指标",
        "",
        "| max_chars | overlap | chunks | Recall@1 | Recall@3 |",
        "|-----------|---------|--------|----------|----------|",
    ]
    for s, m in zip(SIZES, results):
        lines.append(
            f"| {s['max_chars']} | {s['overlap_chars']} | {m['n_chunks']} | "
            f"{m['recall1']:.2%} | {m['recall_k']:.2%} |"
        )
    lines.append("")
    lines.append("## 结论写法（按实测填写，勿编造）")
    lines.append("")
    for s, m in zip(SIZES, results):
        lines.append(
            f"- {s['label']}: chunks={m['n_chunks']}, R@1={m['recall1']:.2%}, R@3={m['recall_k']:.2%}"
        )

    # 写入报告文件
    out = ROOT / "docs" / "chunk_ablate.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[saved] {out}")

    # 同时把原始结果以 JSON 格式打印，方便后续分析或存档
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()