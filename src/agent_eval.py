"""阶段 6：Agent 离线评测（自建小样本）。

评测维度（均可由 /agent 响应字段直接计算）：
- Tool Selection Accuracy：语料内问题是否调用了可接受工具
- Parameter Accuracy：工具参数（配置名 / 查询关键词）是否正确
- Task Success Rate：拒答行为是否符合预期 + 答案是否命中关键信息
- Citation Valid Rate：citation_valid 是否为真
- Citation Coverage：有据回答里是否真的带了 [n] 引用（针对 ABC-4）
- OOS Refusal Rate：库外问题是否被拒答
- Average Tool Calls / Average Latency / Average Steps

口径声明：**自建小样本离线评测，不代表生产准确率**；评测会真实调用 LLM 与检索。

用法：
    python src/agent_eval.py                    # 全量
    python src/agent_eval.py --limit 5          # 只跑前 5 条（省额度）
    python src/agent_eval.py --sleep 0.5
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from hybrid_retrieve5 import HybridRetriever  # noqa: E402
from agent.service import run_agent_service  # noqa: E402

CASES_PATH = ROOT / "data" / "agent_eval_cases.json"
REPORT_PATH = ROOT / "docs" / "agent_eval_report.md"
RAW_PATH = ROOT / "docs" / "agent_eval_raw.json"


def load_cases() -> List[Dict[str, Any]]:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return data["cases"]


def _keyword_hit(text: str, keywords: List[str]) -> bool:
    if not keywords:
        return True
    lowered = text.lower()
    return any(k.lower() in lowered for k in keywords)


def check_tool_selection(case: Dict[str, Any], used: List[str]) -> Optional[bool]:
    """仅对语料内问题判定；库外问题不要求用工具。"""
    if case["type"] != "in_corpus":
        return None
    expected = set(case.get("expected_tools") or [])
    if not expected:
        return None
    return bool(expected & set(used))


def check_parameter(case: Dict[str, Any], tool_calls: List[Dict[str, Any]]) -> Optional[bool]:
    """按 param_check 校验参数；无工具调用则视为参数不达标。"""
    if case["type"] != "in_corpus":
        return None
    spec = case.get("param_check") or {}
    kind = spec.get("kind", "none")
    if kind == "none":
        return None
    value = spec.get("value", "")
    if not tool_calls:
        return False

    for call in tool_calls:
        args = call.get("args") or {}
        if kind == "config_name":
            if call.get("name") == "get_config_detail" and str(args.get("name", "")) == value:
                return True
            # 模型也可能用 rag_answer / search_docs 直接带配置名提问
            if value.lower() in str(args.get("query", "")).lower():
                return True
        elif kind == "query_keyword":
            if value.lower() in str(args.get("query", "")).lower():
                return True
    return False


def check_task_success(case: Dict[str, Any], resp: Dict[str, Any]) -> bool:
    if case["type"] == "oos":
        return bool(resp["refused"]) == bool(case.get("expect_refuse", True))
    if resp["refused"] != case.get("expect_refuse", False):
        return False
    return _keyword_hit(resp["answer"], case.get("answer_keywords") or [])


def run_one(case: Dict[str, Any], hr: HybridRetriever) -> Dict[str, Any]:
    t0 = time.perf_counter()
    resp = run_agent_service(case["query"], hr=hr)
    dumped = resp.model_dump()
    return {
        "id": case["id"],
        "type": case["type"],
        "query": case["query"],
        "refused": dumped["refused"],
        "refusal_reason": dumped["refusal_reason"],
        "tools_used": dumped["tools_used"],
        "tool_calls": dumped["tool_calls"],
        "citations": dumped["citations"],
        "citation_valid": dumped["citation_valid"],
        "steps": dumped["steps"],
        "latency_ms": dumped["latency_ms"],
        "error": dumped["error"],
        "answer": dumped["answer"],
        "checks": {
            "tool_selection": check_tool_selection(case, dumped["tools_used"]),
            "parameter": check_parameter(case, dumped["tool_calls"]),
            "task_success": check_task_success(case, dumped),
            "citation_valid": dumped["citation_valid"],
            "citation_present": len(dumped["citations"]) > 0,
        },
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
    }


def _rate(items: List[Optional[bool]]) -> Optional[float]:
    vals = [x for x in items if x is not None]
    if not vals:
        return None
    return sum(1 for v in vals if v) / len(vals)


def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    in_corpus = [r for r in results if r["type"] == "in_corpus"]
    oos = [r for r in results if r["type"] == "oos"]

    tool_sel = [r["checks"]["tool_selection"] for r in in_corpus]
    params = [r["checks"]["parameter"] for r in in_corpus]
    tasks = [r["checks"]["task_success"] for r in results]
    cit_valid = [r["checks"]["citation_valid"] for r in results]
    cit_present = [r["checks"]["citation_present"] for r in in_corpus]
    oos_refuse = [r["refused"] for r in oos]

    n = len(results)
    return {
        "total": n,
        "in_corpus": len(in_corpus),
        "oos": len(oos),
        "tool_selection_acc": _rate(tool_sel),
        "no_tool_rate": (
            sum(1 for r in in_corpus if not r["tools_used"]) / len(in_corpus)
            if in_corpus
            else None
        ),
        "parameter_acc": _rate(params),
        "task_success_rate": _rate(tasks),
        "citation_valid_rate": _rate(cit_valid),
        "citation_coverage": _rate(cit_present),
        "oos_refusal_rate": (sum(1 for v in oos_refuse if v) / len(oos)) if oos else None,
        "avg_tool_calls": sum(len(r["tool_calls"]) for r in results) / n if n else 0.0,
        "avg_steps": sum(r["steps"] for r in results) / n if n else 0.0,
        "avg_latency_ms": sum(r["latency_ms"] for r in results) / n if n else 0.0,
        "errors": [r for r in results if r["error"]],
    }


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.2%}"


def write_report(summary: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
    lines: List[str] = []
    lines.append("# Agent 离线评测报告（自建小样本）")
    lines.append("")
    lines.append("- 评测对象：`POST /agent`（单 Agent + 3 Tool + LangGraph + 证据/拒答门闩）")
    lines.append(f"- 用例数：{summary['total']}（语料内 {summary['in_corpus']} / 库外 {summary['oos']}）")
    lines.append("- 用例文件：`data/agent_eval_cases.json`；脚本：`src/agent_eval.py`")
    lines.append("- **口径：自建小样本离线评测，不代表生产准确率**；评测真实调用 LLM 与检索。")
    lines.append("")
    lines.append("## 指标总览")
    lines.append("")
    lines.append("| 指标 | 结果 | 说明 |")
    lines.append("|------|------|------|")
    lines.append(f"| Tool Selection Accuracy | {_pct(summary['tool_selection_acc'])} | 语料内问题是否调用可接受工具（命中任一即可） |")
    lines.append(f"| 零工具调用率 | {_pct(summary['no_tool_rate'])} | 语料内问题一次工具都没调的比例（越低越好） |")
    lines.append(f"| Parameter Accuracy | {_pct(summary['parameter_acc'])} | 配置名 / 查询关键词是否出现在工具参数里 |")
    lines.append(f"| Task Success Rate | {_pct(summary['task_success_rate'])} | 拒答行为符合预期 + 答案命中关键信息 |")
    lines.append(f"| Citation Valid Rate | {_pct(summary['citation_valid_rate'])} | `citation_valid=true` 占比 |")
    lines.append(f"| Citation Coverage | {_pct(summary['citation_coverage'])} | 语料内回答真的带 `[n]` 的比例（ABC-4） |")
    lines.append(f"| OOS Refusal Rate | {_pct(summary['oos_refusal_rate'])} | 库外问题被拒答的比例 |")
    lines.append(f"| Average Tool Calls | {summary['avg_tool_calls']:.2f} | 每题平均工具调用次数 |")
    lines.append(f"| Average Steps | {summary['avg_steps']:.2f} | 每题平均 LLM 轮数 |")
    lines.append(f"| Average Latency | {summary['avg_latency_ms']:.0f} ms | 端到端平均耗时 |")
    lines.append("")
    lines.append("## 逐题明细")
    lines.append("")
    lines.append("| ID | 类型 | Query | refused | 工具 | 步骤 | 引用 | citation_valid | 任务成功 | 延迟(ms) |")
    lines.append("|----|------|-------|---------|------|------|------|----------------|----------|----------|")
    for r in results:
        tools = ",".join(r["tools_used"]) or "-"
        lines.append(
            f"| {r['id']} | {r['type']} | {r['query']} | {r['refused']} | {tools} | {r['steps']} | "
            f"{r['citations']} | {r['citation_valid']} | {r['checks']['task_success']} | {r['latency_ms']:.0f} |"
        )
    lines.append("")

    failed = [r for r in results if not r["checks"]["task_success"]]
    lines.append("## 未通过用例（badcase 候选）")
    lines.append("")
    if not failed:
        lines.append("- 本次无未通过用例。")
    else:
        for r in failed:
            lines.append(f"### {r['id']}：{r['query']}")
            lines.append("")
            lines.append(f"- 类型：{r['type']}")
            lines.append(f"- refused={r['refused']}（reason={r['refusal_reason']}）")
            lines.append(f"- 工具调用：{json.dumps(r['tool_calls'], ensure_ascii=False)}")
            lines.append(f"- 引用：{r['citations']}，citation_valid={r['citation_valid']}")
            lines.append(f"- 答案摘录：{r['answer'][:220].replace(chr(10), ' ')}")
            lines.append("")

    errs = summary.get("errors") or []
    if errs:
        lines.append("## 执行异常")
        lines.append("")
        for r in errs:
            lines.append(f"- {r['id']}：{r['error']}")
        lines.append("")

    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    RAW_PATH.write_text(json.dumps({"summary": {k: v for k, v in summary.items() if k != "errors"}, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Agent 离线评测（小样本）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全部）")
    parser.add_argument("--sleep", type=float, default=0.0, help="每题之间休眠秒数")
    parser.add_argument("--only-id", default=None, help="只跑某个用例 ID")
    args = parser.parse_args(argv)

    cases = load_cases()
    if args.only_id:
        cases = [c for c in cases if c["id"] == args.only_id]
    if args.limit:
        cases = cases[: args.limit]

    hr = HybridRetriever()
    results: List[Dict[str, Any]] = []
    for i, case in enumerate(cases, 1):
        r = run_one(case, hr)
        results.append(r)
        ok = "OK " if r["checks"]["task_success"] else "FAIL"
        print(
            f"[{i}/{len(cases)}] {ok} {r['id']} refused={r['refused']} "
            f"tools={r['tools_used']} steps={r['steps']} cites={r['citations']} "
            f"{r['latency_ms']:.0f}ms"
        )
        if args.sleep:
            time.sleep(args.sleep)

    summary = summarize(results)
    write_report(summary, results)

    print("=" * 60)
    print(f"total={summary['total']} task_success={_pct(summary['task_success_rate'])}")
    print(f"tool_selection={_pct(summary['tool_selection_acc'])} param={_pct(summary['parameter_acc'])}")
    print(f"citation_valid={_pct(summary['citation_valid_rate'])} coverage={_pct(summary['citation_coverage'])}")
    print(f"oos_refusal={_pct(summary['oos_refusal_rate'])} avg_tools={summary['avg_tool_calls']:.2f} avg_latency={summary['avg_latency_ms']:.0f}ms")
    print(f"[saved] {REPORT_PATH}")
    print(f"[saved] {RAW_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
