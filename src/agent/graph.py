"""阶段 3：LangGraph 最小 Agent 图。

图形（与阶段 0 设计一致）：

    START → agent ──(有待执行工具且未超步)──→ tools → agent → …
                 └─(无工具 / 出错 / 步数用尽)─→ final → END

职责划分：
- agent  ：一次 LLM 调用；决定「调用哪些工具」或「直接给最终文本」
- tools  ：执行 agent 要的工具（复用 agent/tools.py 的 call_tool）
- final  ：收口；若还没有文本则强制再要一次（不带工具），并做 citation 解析

复用：LLM 出口仍是 tool_calling._post_chat（便于单测 monkeypatch）；
hr（HybridRetriever）通过 LangGraph 的 config.configurable 传入，不放进状态。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import answer6  # noqa: E402
from hybrid_retrieve5 import HybridRetriever  # noqa: E402
from langchain_core.runnables import RunnableConfig  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

try:
    from . import tool_calling as tc
    from .schemas import ToolResult
    from .state import AgentState
    from .tools import call_tool
except ImportError:  # 允许直接以脚本方式运行
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import tool_calling as tc  # type: ignore
    from schemas import ToolResult  # type: ignore
    from state import AgentState  # type: ignore
    from tools import call_tool  # type: ignore

DEFAULT_MAX_STEPS = 3


def _hr_from_config(config: Optional[RunnableConfig]) -> Optional[HybridRetriever]:
    """从 LangGraph config 里取注入的检索器（便于测试替换）。"""
    if not config:
        return None
    configurable = config.get("configurable") or {}
    return configurable.get("hr")


# --------------------------------------------------------------------- agent


def agent_node(state: AgentState, config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
    """一次 LLM 调用：产出「要调用的工具」或「最终回答文本」。"""
    step_count = state.get("step_count", 0)
    max_steps = state.get("max_steps", DEFAULT_MAX_STEPS)
    messages = list(state.get("messages") or [])

    # 预留最后一轮不给工具，强制模型收口
    with_tools = step_count < max_steps - 1

    try:
        data = tc._post_chat(tc._build_payload(messages, with_tools=with_tools, temperature=0.2))
    except Exception as exc:
        return {
            "messages": messages,
            "step_count": step_count + 1,
            "pending_tool_calls": [],
            "answer": tc.LLM_ERROR_MESSAGE,
            "error": f"{type(exc).__name__}: {exc}",
        }

    message = (data.get("choices") or [{}])[0].get("message") or {}
    pending: List[Dict[str, Any]] = list(message.get("tool_calls") or [])

    update: Dict[str, Any] = {
        "messages": messages
        + [
            {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": pending,
            }
        ],
        "step_count": step_count + 1,
        "pending_tool_calls": pending,
    }
    if not pending:
        update["answer"] = message.get("content") or ""
    return update


# --------------------------------------------------------------------- tools


def tools_node(state: AgentState, config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
    """执行 agent 请求的工具，并把结果以 role=tool 回灌。"""
    hr = _hr_from_config(config)
    pending = list(state.get("pending_tool_calls") or [])
    messages = list(state.get("messages") or [])
    log = list(state.get("tool_calls") or [])
    ctx_count = state.get("observed_context_count", 0)

    updates: Dict[str, Any] = {}

    for call in pending:
        fn = call.get("function") or {}
        name = fn.get("name") or ""
        args, parse_error = tc._parse_tool_arguments(fn.get("arguments") or "")

        if parse_error:
            result = ToolResult.failure(
                name or "unknown", "invalid_args", parse_error, time.perf_counter()
            )
        else:
            result = call_tool(name, args, hr=hr)

        if result.ok:
            if name == "rag_answer":
                contexts = result.data.get("contexts") or []
                ctx_count = max(ctx_count, len(contexts))
                if result.data.get("refused"):
                    # 阶段 3 只记录；阶段 4 会据此强制拒答
                    updates["refused"] = True
                    updates["refusal_reason"] = result.data.get("refuse_reason")
            else:
                hits = result.data.get("hits") or []
                ctx_count = max(ctx_count, len(hits))

        log.append(
            {
                "name": name,
                "args": args,
                "ok": result.ok,
                "error_code": result.error_code,
                "latency_ms": result.latency_ms,
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": result.model_dump_json(),
            }
        )

    return {
        "messages": messages,
        "tool_calls": log,
        "pending_tool_calls": [],
        "observed_context_count": ctx_count,
        **updates,
    }


# --------------------------------------------------------------------- final


def final_node(state: AgentState, config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
    """收口：保证有文本，并解析/校验引用编号。"""
    answer = state.get("answer") or ""
    error = state.get("error")

    if not answer:
        # 兜底：循环结束时还没有文本，再要一次（不带工具）
        messages = list(state.get("messages") or [])
        try:
            data = tc._post_chat(tc._build_payload(messages, with_tools=False, temperature=0.2))
            answer = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        except Exception as exc:
            answer = tc.LLM_ERROR_MESSAGE
            error = f"{type(exc).__name__}: {exc}"

    n_ctx = state.get("observed_context_count", 0)
    cite = (
        answer6.parse_citations(answer, n_ctx)
        if n_ctx
        else {"cited": [], "invalid_citations": [], "citation_valid": True}
    )

    return {
        "answer": answer,
        "error": error,
        "citations": cite["cited"],
        "citation_valid": cite["citation_valid"],
    }


# ------------------------------------------------------------------- routing


def route_after_agent(state: AgentState) -> str:
    if state.get("error"):
        return "final"
    if not state.get("pending_tool_calls"):
        return "final"
    return "tools"


def route_after_tools(state: AgentState) -> str:
    if state.get("step_count", 0) >= state.get("max_steps", DEFAULT_MAX_STEPS):
        return "final"
    return "agent"


# --------------------------------------------------------------------- build


def build_graph():
    """编译状态图：START → agent ⇄ tools → final → END。"""
    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("final", final_node)

    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {"tools": "tools", "final": "final"})
    graph.add_conditional_edges("tools", route_after_tools, {"agent": "agent", "final": "final"})
    graph.add_edge("final", END)

    return graph.compile()


def run_agent(
    query: str,
    hr: Optional[HybridRetriever] = None,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> Dict[str, Any]:
    """对外编排入口：构建初始状态 → 执行图 → 返回扁平结果。"""
    started = time.perf_counter()
    graph = build_graph()

    initial: AgentState = {
        "query": query,
        "messages": [
            {"role": "system", "content": tc.AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ],
        "step_count": 0,
        "max_steps": max_steps,
        "pending_tool_calls": [],
        "tool_calls": [],
        "observed_context_count": 0,
        "answer": "",
        "refused": False,
        "refusal_reason": None,
        "citations": [],
        "citation_valid": True,
        "error": None,
    }

    final_state = graph.invoke(
        initial,
        config={
            "configurable": {"hr": hr},
            "recursion_limit": max_steps * 2 + 5,
        },
    )

    return {
        "query": query,
        "answer": final_state.get("answer") or "",
        "tool_calls": final_state.get("tool_calls") or [],
        "tools_used": [c["name"] for c in (final_state.get("tool_calls") or [])],
        "steps": final_state.get("step_count", 0),
        "refused": bool(final_state.get("refused")),
        "refusal_reason": final_state.get("refusal_reason"),
        "citations": final_state.get("citations") or [],
        "citation_valid": bool(final_state.get("citation_valid", True)),
        "error": final_state.get("error"),
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


if __name__ == "__main__":
    _base, _model, _key = tc.resolve_llm_config()
    print(f"[llm] base={_base} model={_model} key={'set' if _key else 'MISSING'}")
    for q in ["enable_dolphin_proto 是干什么的？", "今天西安天气怎么样？"]:
        print("=" * 70)
        print("Q:", q)
        out = run_agent(q)
        print("steps=", out["steps"], "tools_used=", out["tools_used"], "error=", out["error"])
        print(
            "refused=", out["refused"], "citation_valid=", out["citation_valid"],
            "latency_ms=", out["latency_ms"],
        )
        print("-" * 70)
        print(out["answer"])
