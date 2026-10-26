"""容器 / 云平台入口：缺索引则先建，再启动 uvicorn。

为什么用 Python 而不是 shell：
- Windows 开发机也能直接跑这个入口，部署路径因此可本地验证；
- 避免 CRLF / 可执行权限在镜像里的常见坑。

设计要点：
1. **可写目录回退**：HF Spaces 以 uid=1000 运行，`/app` 可能不可写；
   这里按 CHROMA_DIR → /app/data/chroma → /tmp/chroma 依次探测可写性；
2. **端口**：PORT（Render 注入）→ APP_PORT → 7860（HF Spaces 默认）；
3. 缺索引时运行 ingest3 建库；失败不阻塞启动，但 /health 会报错便于发现；
4. `os.execvp` 启动 uvicorn（不额外留一层父进程）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

DEFAULT_PORT = "7860"
FALLBACK_CHROMA = Path("/tmp/chroma")


def _is_writable(path: Path) -> bool:
    """目录可创建且可写（会写入并删除一个探针文件）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_chroma_dir(env: Optional[dict] = None) -> Path:
    """按优先级选一个可写的向量库目录。"""
    e = os.environ if env is None else env
    raw = (e.get("CHROMA_DIR") or "").strip()

    candidates = []
    if raw:
        candidates.append(Path(raw))
    candidates.append(Path("/app/data/chroma"))
    candidates.append(FALLBACK_CHROMA)

    for candidate in candidates:
        if _is_writable(candidate):
            return candidate
    return FALLBACK_CHROMA


def resolve_port(env: Optional[dict] = None) -> str:
    e = os.environ if env is None else env
    return (e.get("PORT") or e.get("APP_PORT") or DEFAULT_PORT).strip()


def has_index(chroma_dir: Path) -> bool:
    try:
        return chroma_dir.is_dir() and any(chroma_dir.iterdir())
    except OSError:
        return False


def main() -> int:
    chroma_dir = resolve_chroma_dir()
    os.environ["CHROMA_DIR"] = str(chroma_dir)
    port = resolve_port()
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
