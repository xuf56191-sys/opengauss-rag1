"""Agent 交互式命令行：本地就能和单 Agent 对话。

用法：
    python src/agent_cli.py                       # 交互模式
    python src/agent_cli.py --once "问题"          # 只问一次
    python src/agent_cli.py --max-steps 4         # 调大/调小步数上限（1~6）

交互模式命令：
    :help   显示帮助
    :tools  列出工具
    :quit   退出

说明：
- 复用一个 HybridRetriever 单例，避免每题重建 BM25
- 输出与 POST /agent 的字段一致（refused / tools_used / steps / citations / latency）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from hybrid_retrieve5 import HybridRetriever  # noqa: E402
from agent.service import AgentResponse, run_agent_service  # noqa: E402
from agent.tools import TOOL_SPECS  # noqa: E402

BANNER = """openGauss 配置问答 Agent（单 Agent + 3 工具）
输入问题直接回车；:help 帮助，:tools 看工具，:quit 退出。
"""


def print_result(resp: AgentResponse) -> None:
    """按统一格式打印一次 Agent 结果。"""
    if resp.error:
        print(f"[错误] {resp.error}")
    if resp.refused:
        print(f"[拒答] reason={resp.refusal_reason}")
    print(resp.answer)
    tools = ", ".join(resp.tools_used) or "-"
    calls = "; ".join(
        f"{c.name}({c.args}){'✓' if c.ok else '✗' + str(c.error_code)}[{c.origin}]"
        for c in resp.tool_calls
    ) or "-"
    print("-" * 70)
    print(f"工具: {tools}")
    print(f"调用: {calls}")
    print(
        f"步数: {resp.steps} | 引用: {resp.citations} | 引用合法: {resp.citation_valid} "
        f"| 耗时: {resp.latency_ms:.0f}ms"
    )
    print("=" * 70)


def ask(query: str, hr: HybridRetriever, max_steps: int) -> None:
    resp = run_agent_service(query, max_steps=max_steps, hr=hr)
    print_result(resp)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="openGauss 配置问答 Agent CLI")
    parser.add_argument("--once", default=None, help="只问一个问题后退出")
    parser.add_argument("--max-steps", type=int, default=3, help="最大 LLM 轮数（1~6）")
    args = parser.parse_args(argv)

    max_steps = max(1, min(6, args.max_steps))

    print("加载检索器（首次会构建 BM25 索引）...")
    hr = HybridRetriever()
    print(f"就绪：{len(hr.ids)} chunks\n")

    if args.once:
        ask(args.once, hr, max_steps)
        return 0

    print(BANNER)
    while True:
        try:
            line = input("问> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not line:
            continue
        if line in {":quit", ":q", "quit", "exit"}:
            return 0
        if line == ":help":
            print(BANNER)
            continue
        if line == ":tools":
            for spec in TOOL_SPECS:
                print(f"- {spec['name']}: {spec['description']}")
            print()
            continue

        ask(line, hr, max_steps)


if __name__ == "__main__":
    sys.exit(main())
