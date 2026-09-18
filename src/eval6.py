"""Offline eval: pure vector vs hybrid Recall@k + OOS refusal (no LLM calls)."""

import json
import sys
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env", override=True)

from embed_client1 import embed_texts  # noqa: E402
from answer6 import should_refuse  # noqa: E402
from hybrid_retrieve5 import HybridRetriever, CHROMA_DIR, COLLECTION  # noqa: E402


def load_cases() -> Dict:
    path = ROOT / "data" / "eval_cases.json"
    return json.loads(path.read_text(encoding="utf-8"))


def keyword_hit(text: str, keywords: List[str]) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in keywords)


def vector_search(hr: HybridRetriever, query: str, k: int) -> List[Dict]:
    """纯向量 Top-K（Chroma cosine distance），不经过 BM25/RRF。"""
    qvec = embed_texts([query])[0]
    res = hr.col.query(
        query_embeddings=[qvec],
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )
    ids = res["ids"][0]
    docs = res["documents"][0]
    metas = res["metadatas"][0]
    dists = res["distances"][0]
    out = []
    for i, doc_id in enumerate(ids):
        out.append(
            {
                "id": doc_id,
                "text": docs[i],
                "meta": metas[i],
                "vector_distance": dists[i],
            }
        )
    return out


def hit_any(hits: List[Dict], keywords: List[str]) -> bool:
    for h in hits:
        blob = f"{h['meta'].get('heading_path') or ''}\n{h['text']}"
        if keyword_hit(blob, keywords):
            return True
    return False


def eval_source(hr: HybridRetriever, cases: List[Dict], k: int = 3) -> Dict:
    rows = []
    v1 = v3 = h1 = h3 = 0
    for c in cases:
        q = c["query"]
        kws = c["keywords"]
        vec_hits = vector_search(hr, q, k=k)
        hyb_hits = hr.search(q, k=k)

        v_top1 = hit_any(vec_hits[:1], kws)
        v_topk = hit_any(vec_hits[:k], kws)
        h_top1 = hit_any(hyb_hits[:1], kws)
        h_topk = hit_any(hyb_hits[:k], kws)

        v1 += int(v_top1)
        v3 += int(v_topk)
        h1 += int(h_top1)
        h3 += int(h_topk)

        rows.append(
            {
                "id": c["id"],
                "query": q,
                "vec_r1": v_top1,
                "vec_rk": v_topk,
                "hyb_r1": h_top1,
                "hyb_rk": h_topk,
                "vec_top": [
                    (h["meta"].get("heading_path"), round(h["vector_distance"], 4))
                    for h in vec_hits[:k]
                ],
                "hyb_top": [
                    (
                        h["meta"].get("heading_path"),
                        round(h["score"], 5),
                        round(h.get("bm25_raw_score") or 0, 4),
                        round(h.get("vector_distance") or -1, 4),
                    )
                    for h in hyb_hits[:k]
                ],
            }
        )

    n = len(cases)
    return {
        "n": n,
        "k": k,
        "vector_recall1": v1 / n if n else 0.0,
        "vector_recall_k": v3 / n if n else 0.0,
        "hybrid_recall1": h1 / n if n else 0.0,
        "hybrid_recall_k": h3 / n if n else 0.0,
        "rows": rows,
    }


def eval_oos(hr: HybridRetriever, cases: List[Dict], k: int = 3) -> Dict:
    rows = []
    correct = 0
    for c in cases:
        hits = hr.search(c["query"], k=k)
        refuse, reason = should_refuse(hits)
        max_bm25 = max((h.get("bm25_raw_score") or 0.0) for h in hits) if hits else 0.0
        dists = [h["vector_distance"] for h in hits if h.get("vector_distance") is not None]
        min_dist = min(dists) if dists else None
        ok = refuse == c.get("expect_refuse", True)
        correct += int(ok)
        rows.append(
            {
                "id": c["id"],
                "query": c["query"],
                "refused": refuse,
                "reason": reason,
                "max_bm25_raw": round(max_bm25, 4),
                "min_vec_dist": None if min_dist is None else round(min_dist, 4),
                "pass": ok,
            }
        )
    n = len(cases)
    return {"n": n, "refusal_acc": correct / n if n else 0.0, "rows": rows}


def write_report(src: Dict, oos: Dict, out_path: Path) -> str:
    lines = []
    lines.append("# 离线评测报告（小样本回归）")
    lines.append("")
    lines.append(f"- 语料集合：`{COLLECTION}`")
    lines.append(f"- Chroma：`{CHROMA_DIR}`")
    lines.append(f"- 来源题：{src['n']} 条，指标 Recall@1 / Recall@{src['k']}")
    lines.append(f"- 越界题：{oos['n']} 条，指标：代码层拒答是否触发（`should_refuse`）")
    lines.append("- 说明：评测**不调用 LLM**，只测检索与拒答；样本来自个人笔记切片，**非生产准确率**。")
    lines.append("")
    lines.append("## 检索：纯向量 vs 混合")
    lines.append("")
    lines.append("| 方法 | Recall@1 | Recall@3 |")
    lines.append("|------|----------|----------|")
    lines.append(
        f"| 纯向量 | {src['vector_recall1']:.2%} | {src['vector_recall_k']:.2%} |"
    )
    lines.append(
        f"| 混合 BM25+向量 | {src['hybrid_recall1']:.2%} | {src['hybrid_recall_k']:.2%} |"
    )
    lines.append("")
    lines.append("### 来源题明细")
    lines.append("")
    lines.append("| ID | Query | Vec@1 | Vec@3 | Hyb@1 | Hyb@3 |")
    lines.append("|----|-------|-------|-------|-------|-------|")
    for r in src["rows"]:
        lines.append(
            f"| {r['id']} | {r['query']} | {r['vec_r1']} | {r['vec_rk']} | {r['hyb_r1']} | {r['hyb_rk']} |"
        )
    lines.append("")
    lines.append("## 越界题拒答")
    lines.append("")
    lines.append(f"- 拒答正确率：**{oos['refusal_acc']:.2%}**（{oos['n']} 题）")
    lines.append("")
    lines.append("| ID | Query | refused | reason | max_bm25 | min_dist | pass |")
    lines.append("|----|-------|---------|--------|----------|----------|------|")
    for r in oos["rows"]:
        lines.append(
            f"| {r['id']} | {r['query']} | {r['refused']} | {r['reason']} | "
            f"{r['max_bm25_raw']} | {r['min_vec_dist']} | {r['pass']} |"
        )
    lines.append("")
    lines.append("## 口径")
    lines.append("")
    lines.append("- Recall@k：Top-k 文本或 `heading_path` 命中任一关键词即算命中。")
    lines.append("- 拒答规则：`no_hits` 或 `max_bm25_raw < 2.0` 或 `min_vec_dist > 0.55`（OR，评测后收紧）。")
    lines.append("- 阈值来自 peek_signals + 本评测集标定；语料扩充后需重标。")
    lines.append("")
    text = "\n".join(lines) + "\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return text


def main() -> None:
    cases = load_cases()
    hr = HybridRetriever()
    src = eval_source(hr, cases["source"], k=3)
    oos = eval_oos(hr, cases["oos"], k=3)

    print("=== Retrieval ===")
    print(
        f"vector R@1={src['vector_recall1']:.2%} R@3={src['vector_recall_k']:.2%} | "
        f"hybrid R@1={src['hybrid_recall1']:.2%} R@3={src['hybrid_recall_k']:.2%} | n={src['n']}"
    )
    for r in src["rows"]:
        print(
            f"  {r['id']} vec={int(r['vec_r1'])}/{int(r['vec_rk'])} "
            f"hyb={int(r['hyb_r1'])}/{int(r['hyb_rk'])} :: {r['query']}"
        )

    print("=== OOS refusal ===")
    print(f"refusal_acc={oos['refusal_acc']:.2%} n={oos['n']}")
    for r in oos["rows"]:
        print(
            f"  {r['id']} refused={r['refused']} reason={r['reason']} "
            f"bm25={r['max_bm25_raw']} dist={r['min_vec_dist']} pass={r['pass']} :: {r['query']}"
        )

    out = ROOT / "docs" / "eval_report.md"
    write_report(src, oos, out)
    print(f"\n[saved] {out}")


if __name__ == "__main__":
    main()
