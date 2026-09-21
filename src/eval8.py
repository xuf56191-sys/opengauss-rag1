"""Offline eval: pure vector vs hybrid Recall@k + OOS refusal (no LLM calls)."""
# 模块说明：离线评测脚本
# 目标：对比“纯向量检索”和“混合检索”的 Recall@k，并测试“越界问题（OOS）拒答”机制
# 特点：整个评测不调用 LLM，只测检索层和拒答规则，运行快、成本低、可重复

import json
import sys
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv

# 定位项目根目录，并把 src 加入模块搜索路径
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 加载 .env，override=True 确保 .env 的值覆盖任何系统残留变量
load_dotenv(ROOT / ".env", override=True)

# 导入评测需要的模块（noqa: E402 是为了忽略“导入语句不在文件顶部”的 flake8 警告）
from embed_client1 import embed_texts  # noqa: E402    # 复用嵌入函数
from answer6 import should_refuse      # noqa: E402    # 复用拒答判定逻辑
from hybrid_retrieve5 import HybridRetriever, CHROMA_DIR, COLLECTION  # noqa: E402


def load_cases() -> Dict:
    """从 JSON 文件加载测试用例（source 题 + oos 越界题）"""
    path = ROOT / "data" / "eval_cases.json"
    return json.loads(path.read_text(encoding="utf-8"))


def keyword_hit(text: str, keywords: List[str]) -> bool:
    """判断文本里是否命中了关键词列表中的任意一个（大小写不敏感）"""
    t = text.lower()
    return any(k.lower() in t for k in keywords)


def vector_search(hr: HybridRetriever, query: str, k: int) -> List[Dict]:
    """纯向量 Top-K（Chroma cosine distance），不经过 BM25/RRF"""
    # 把查询词转成向量
    qvec = embed_texts([query])[0]
    # 直接调用底层 Chroma 的 query 方法，只做向量检索
    res = hr.col.query(
        query_embeddings=[qvec],
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )
    # Chroma 返回的是嵌套列表（外层是每个查询，内层是 k 个结果），取第 0 个查询的结果
    ids = res["ids"][0]
    docs = res["documents"][0]
    metas = res["metadatas"][0]
    dists = res["distances"][0]

    # 把结果组装成统一格式的列表
    out = []
    for i, doc_id in enumerate(ids):
        out.append(
            {
                "id": doc_id,
                "text": docs[i],
                "meta": metas[i],
                "vector_distance": dists[i],  # 余弦距离，越小越相似
            }
        )
    return out


def hit_any(hits: List[Dict], keywords: List[str]) -> bool:
    """检查一批检索结果里，是否有任意一条命中关键词（用于计算 Recall）"""
    for h in hits:
        # 把标题路径和正文拼起来作为匹配文本
        blob = f"{h['meta'].get('heading_path') or ''}\n{h['text']}"
        if keyword_hit(blob, keywords):
            return True
    return False


def eval_source(hr: HybridRetriever, cases: List[Dict], k: int = 3) -> Dict:
    """评测“来源题”：对比纯向量和混合检索的 Recall@1 与 Recall@k"""
    rows = []
    # 四个计数器：向量 Top1/Topk 命中数，混合 Top1/Topk 命中数
    v1 = v3 = h1 = h3 = 0

    for c in cases:
        q = c["query"]        # 测试问题
        kws = c["keywords"]   # 用于判断“是否命中”的关键词列表

        # 两路检索
        vec_hits = vector_search(hr, q, k=k)   # 纯向量
        hyb_hits = hr.search(q, k=k)           # 混合检索

        # 判断是否命中（Top-1 和 Top-k 两种口径）
        v_top1 = hit_any(vec_hits[:1], kws)
        v_topk = hit_any(vec_hits[:k], kws)
        h_top1 = hit_any(hyb_hits[:1], kws)
        h_topk = hit_any(hyb_hits[:k], kws)

        # 累加计数器
        v1 += int(v_top1)
        v3 += int(v_topk)
        h1 += int(h_top1)
        h3 += int(h_topk)

        # 保存每题的明细，方便报告里逐条对照
        rows.append(
            {
                "id": c["id"],
                "query": q,
                "vec_r1": v_top1,
                "vec_rk": v_topk,
                "hyb_r1": h_top1,
                "hyb_rk": h_topk,
                # 记录向量检索 Top-k 的（标题路径，距离），方便 debug
                "vec_top": [
                    (h["meta"].get("heading_path"), round(h["vector_distance"], 4))
                    for h in vec_hits[:k]
                ],
                # 记录混合检索 Top-k 的（标题路径，RRF 分，BM25 原始分，向量距离）
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
    # 计算 Recall：命中数 / 总题数
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
    """评测“越界题（OOS, Out-Of-Scope）”：应该被拒答的问题是否被正确拒答"""
    rows = []
    correct = 0
    for c in cases:
        # 用混合检索取 Top-k 结果
        hits = hr.search(c["query"], k=k)
        # 调用业务逻辑中的拒答判定函数，返回 (是否拒答, 原因)
        refuse, reason = should_refuse(hits)

        # 提取信号值用于展示：最高 BM25 原始分、最小向量距离
        max_bm25 = max((h.get("bm25_raw_score") or 0.0) for h in hits) if hits else 0.0
        dists = [h["vector_distance"] for h in hits if h.get("vector_distance") is not None]
        min_dist = min(dists) if dists else None

        # 判定是否通过：实际拒答结果 == 期望拒答结果
        # expect_refuse 默认为 True（越界题一般期望被拒答）
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
    """把评测结果写成 Markdown 报告（便于提交到 docs/ 目录存档）"""
    lines = []
    lines.append("# 离线评测报告（小样本回归）")
    lines.append("")
    lines.append(f"- 语料集合：`{COLLECTION}`")
    lines.append(f"- Chroma：`{CHROMA_DIR}`")
    lines.append(f"- 来源题：{src['n']} 条，指标 Recall@1 / Recall@{src['k']}")
    lines.append(f"- 越界题：{oos['n']} 条，指标：代码层拒答是否触发（`should_refuse`）")
    lines.append("- 说明：评测**不调用 LLM**，只测检索与拒答；样本来自个人笔记切片，**非生产准确率**。")
    lines.append("")

    # 检索对比表格
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

    # 每题明细表格
    lines.append("### 来源题明细")
    lines.append("")
    lines.append("| ID | Query | Vec@1 | Vec@3 | Hyb@1 | Hyb@3 |")
    lines.append("|----|-------|-------|-------|-------|-------|")
    for r in src["rows"]:
        lines.append(
            f"| {r['id']} | {r['query']} | {r['vec_r1']} | {r['vec_rk']} | {r['hyb_r1']} | {r['hyb_rk']} |"
        )
    lines.append("")

    # 拒答评测部分
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

    # 口径说明（评测标准，避免误读）
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
    """主流程：加载用例 -> 跑两组评测 -> 控制台打印摘要 -> 写 Markdown 报告"""
    cases = load_cases()
    hr = HybridRetriever()

    # 1. 评测来源题（检索质量）
    src = eval_source(hr, cases["source"], k=3)
    # 2. 评测越界题（拒答质量）
    oos = eval_oos(hr, cases["oos"], k=3)

    # 控制台打印摘要，方便快速查看
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

    # 把结果写成 Markdown 报告，存档到 docs/eval_report.md
    out = ROOT / "docs" / "eval_report.md"
    write_report(src, oos, out)
    print(f"\n[saved] {out}")


if __name__ == "__main__":
    main()