"""Phase7: chunk length ablation — ingest 800/1200/1600 and compare hybrid Recall@k."""

import json
import sys
from pathlib import Path

import chromadb
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env", override=True)

from chunker2 import chunk_markdown  # noqa: E402
from embed_client1 import embed_texts  # noqa: E402
from hybrid_retrieve5 import HybridRetriever, CHROMA_DIR  # noqa: E402
from eval8 import load_cases, hit_any  # noqa: E402

# 与生产默认一致的 overlap 比例约 10%
SIZES = [
    {"label": "c800", "max_chars": 800, "overlap_chars": 80},
    {"label": "c1200", "max_chars": 1200, "overlap_chars": 120},
    {"label": "c1600", "max_chars": 1600, "overlap_chars": 160},
]
MD = ROOT / "personal" / "peewee-notes.md"
BATCH = 8


def ingest_size(label: str, max_chars: int, overlap_chars: int) -> int:
    col_name = f"opengauss_{label}"
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    col = client.get_or_create_collection(
        name=col_name,
        metadata={"hnsw:space": "cosine"},
    )
    # 重建集合，保证消融可重复
    ids = col.get(include=[])["ids"]
    if ids:
        col.delete(ids=ids)

    raw = MD.read_text(encoding="utf-8")
    rel = MD.relative_to(ROOT).as_posix()
    chunks = chunk_markdown(
        raw,
        source=rel,
        max_chars=max_chars,
        overlap_chars=overlap_chars,
    )
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
    print(f"  [{label}] collection={col_name} count={col.count()}")
    return col.count()


def eval_collection(label: str, cases: list, k: int = 3) -> dict:
    hr = HybridRetriever(collection=f"opengauss_{label}")
    r1 = rk = 0
    rows = []
    for c in cases:
        hits = hr.search(c["query"], k=k)
        h1 = hit_any(hits[:1], c["keywords"])
        hk = hit_any(hits[:k], c["keywords"])
        r1 += int(h1)
        rk += int(hk)
        rows.append({"id": c["id"], "r1": h1, "rk": hk})
    n = len(cases)
    return {
        "label": label,
        "n_chunks": len(hr.ids),
        "recall1": r1 / n if n else 0.0,
        "recall_k": rk / n if n else 0.0,
        "rows": rows,
    }


def main() -> None:
    cases = load_cases()["source"]
    results = []
    for s in SIZES:
        print(f"=== ingest {s['label']} max_chars={s['max_chars']} ===")
        ingest_size(s["label"], s["max_chars"], s["overlap_chars"])
        metrics = eval_collection(s["label"], cases, k=3)
        results.append(metrics)
        print(
            f"  eval {s['label']}: chunks={metrics['n_chunks']} "
            f"R@1={metrics['recall1']:.2%} R@3={metrics['recall_k']:.2%}"
        )

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
    out = ROOT / "docs" / "chunk_ablate.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[saved] {out}")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
