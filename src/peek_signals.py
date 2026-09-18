import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hybrid_retrieve5 import HybridRetriever

def main():
    hr = HybridRetriever()
    qs = [
        "enable_dolphin_proto 是干什么的？",
        "今天西安天气怎么样？",
        "Peewee 怎么连 openGauss B 兼容模式？",
        "为什么 PyMySQL 的 database 参数要和 schema 同名？",
    ]
    for q in qs:
        print("=" * 60)
        print("Q:", q)
        for i, h in enumerate(hr.search(q, k=3), 1):
            path = h["meta"].get("heading_path")
            print(
                f"  #{i} rrf={h['score']:.5f} "
                f"bm25_raw={h['bm25_raw_score']:.4f} "
                f"vec_dist={h['vector_distance']:.4f} "
                f"path={path!r}"
            )

if __name__ == "__main__":
    main()