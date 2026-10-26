"""容器 / 云平台入口：缺索引则先建，再启动 uvicorn。

为什么用 Python 而不是 shell 脚本：
- 开发机（Windows，无 WSL）也能直接跑这个入口，等于把部署入口纳入本地验证；
- 避免 CRLF / 权限等 shell 脚本在镜像里的常见坑。

行为：
1. 若 CHROMA_DIR 不存在或为空 → 运行 `python src/ingest3.py` 建索引（需要嵌入 API Key + 网络）
2. 建索引失败不阻塞启动，但会打印警告（/health 会因此返回 500，便于平台探活发现问题）
3. 用 os.execvp 启动 uvicorn，PORT 由平台注入（默认 8000）
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def has_index(chroma_dir: Path) -> bool:
    """目录存在且非空即认为已有索引。"""
    try:
        return chroma_dir.is_dir() and any(chroma_dir.iterdir())
    except OSError:
        return False


def main() -> int:
    raw = os.environ.get("CHROMA_DIR", "/app/data/chroma")
    chroma_dir = Path(raw)
    print(f"[entrypoint] CHROMA_DIR={chroma_dir}", flush=True)

    if has_index(chroma_dir):
        print("[entrypoint] 已存在索引，跳过入库", flush=True)
    else:
        print("[entrypoint] 索引为空，开始入库（约 20~40s）...", flush=True)
        rc = subprocess.call([sys.executable, "src/ingest3.py"])
        if rc != 0:
            print(
                "[entrypoint] 入库失败：服务仍会启动，但 /health 将报错；"
                "请检查 SILICONFLOW_API_KEY 与网络",
                file=sys.stderr,
                flush=True,
            )

    port = os.environ.get("PORT", "8000")
    argv = [
        sys.executable,
        "-m",
        "uvicorn",
        "api7:app",
        "--app-dir",
        "src",
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
    ]
    print(f"[entrypoint] exec: {' '.join(argv)}", flush=True)
    os.execvp(sys.executable, argv)
    return 0  # 正常不会到这里


if __name__ == "__main__":
    sys.exit(main())
