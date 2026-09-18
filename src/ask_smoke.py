import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "api_smoke_results.txt"


def show(title: str, data) -> str:
    text = f"\n{'=' * 60}\n{title}\n{'=' * 60}\n"
    if isinstance(data, (dict, list)):
        text += json.dumps(data, ensure_ascii=False, indent=2)
    else:
        text += str(data)
    print(text)
    return text


def safe_json(resp: httpx.Response):
    try:
        return resp.json()
    except Exception:
        return {
            "status_code": resp.status_code,
            "content_type": resp.headers.get("content-type"),
            "raw_text": resp.text[:500],
        }


def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    parts = []
    # trust_env=False：忽略系统代理，避免本机 127.0.0.1 被代理劫持成 502
    with httpx.Client(timeout=120, trust_env=False) as client:
        h = client.get(f"{base}/health")
        parts.append(show(f"GET /health status={h.status_code}", safe_json(h)))

        r1 = client.post(
            f"{base}/ask",
            json={"query": "enable_dolphin_proto 是干什么的？", "k": 3},
        )
        parts.append(show(f"POST /ask in-corpus status={r1.status_code}", safe_json(r1)))

        r2 = client.post(
            f"{base}/ask",
            json={"query": "今天西安天气怎么样？", "k": 3},
        )
        parts.append(show(f"POST /ask out-of-domain status={r2.status_code}", safe_json(r2)))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"\n[saved] {OUT}")


if __name__ == "__main__":
    main()
