"""阶段 3–4：LangGraph Agent 图 + 证据/拒答门闩。

图形：

    START → agent ──(有待执行工具且未超步)──→ tools → agent → …
                 └─(无工具 / 出错 / 步数用尽)─→ guard ─→ final → END

职责划分：
- agent  ：一次 LLM 调用；决定「调用哪些工具」或「直接给最终文本」
- tools  ：执行 agent 要的工具（复用 agent/tools.py 的 call_tool），tool_error 重试一次
- guard  ：阶段 4 的门闩——保证「没有证据就不许下结论」
- final  ：收口与 citation 解析

阶段 4 三条门闩规则（可用测试证明）：
1) 检索/工具层已判拒答 → 以检索层为准，用固定拒答文案覆盖模型输出；
2) 已有证据（观测到资料片段）→ 放行模型答案；
3) 零证据：
   - 问题像 openGauss 配置域 → 确定性回退调 rag_answer（再判定，或换成有据回答）；
   - 否则 → 直接拒答（reason=no_evidence），不接受模型的自由发挥。

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
from hybrid_retrieve5 import HybridRetriever, extract_identifiers  # noqa: E402
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

# 工具级重试：只对「像是临时故障」的错误重试，参数/名字错误重试没有意义
RETRYABLE_TOOL_ERRORS = {"tool_error"}

# 领域判定关键词（启发式）：命中才认为「这个问题本该查我们的语料」
DOMAIN_KEYWORDS = (
    "opengauss",
    "peewee",
    "pymysql",
    "dolphin",
    "兼容",
    "协议",
    "端口",
    "schema",
    "database",
    "索引",
    "主键",
    "自增",
    "事务",
    "字符集",
    "连接",
    "配置",
    "参数",
    "驱动",
    "表",
    "sql",
    "orm",
    "数据库",
)


def _looks_domain_relevant(query: str) -> bool:
    """启发式判断问题是否属于本项目语料覆盖的领域。

    命中任一条即可：包含 snake_case 配置项，或包含领域关键词。
    这是规则而非模型判断，便于测试与解释；语料扩大后应同步维护关键词。
    """
    if extract_identifiers(query):
        return True
    lowered = query.lower()
    return any(kw in lowered for kw in DOMAIN_KEYWORDS)


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
        data = tc.post_chat_with_retry(tc._build_payload(messages, with_tools=with_tools, temperature=0.2))
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
            # 临时性故障（网络/向量库抖动）重试一次；参数或工具名错误不重试
            if (not result.ok) and result.error_code in RETRYABLE_TOOL_ERRORS:
                result = call_tool(name, args, hr=hr)

        if result.ok:
            if name == "rag_answer":
                contexts = result.data.get("contexts") or []
                ctx_count = max(ctx_count, len(contexts))
                if result.data.get("refused"):
                    # 记录检索层判定；guard 节点会据此强制拒答
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
                "origin": "agent",
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


# --------------------------------------------------------------------- guard


def guard_node(state: AgentState, config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
    """阶段 4 门闩：没有证据就不允许给出结论。

    规则顺序：
    1) 系统级错误 → 保持错误提示，不额外判拒答；
    2) 工具层已判拒答 → 以检索层为准，覆盖模型话术；
    3) 已有证据 → 放行；
    4) 零证据 → 领域内问题回退 rag_answer；否则直接拒答。
    """
    hr = _hr_from_config(config)
    query = state.get("query") or ""
    updates: Dict[str, Any] = {}

    if state.get("error"):
        return updates

    # 规则 2：检索层说不可答，就不接受模型自己的说法
    if state.get("refused"):
        updates["answer"] = answer6.REFUSE_MESSAGE
        updates["citations"] = []
        updates["citation_valid"] = True
        return updates

    # 规则 3：有证据，放行
    if state.get("observed_context_count", 0) > 0:
        return updates

    # 规则 4：零证据
    if not _looks_domain_relevant(query):
        updates["refused"] = True
        updates["refusal_reason"] = "no_evidence"
        updates["answer"] = answer6.REFUSE_MESSAGE
        return updates

    # 领域内问题却没走工具 → 确定性回退，直接调 rag_answer 判定与作答
    result = call_tool("rag_answer", {"query": query}, hr=hr)
    log = list(state.get("tool_calls") or [])
    log.append(
        {
            "name": "rag_answer",
            "args": {"query": query},
            "ok": result.ok,
            "error_code": result.error_code,
            "latency_ms": result.latency_ms,
            "origin": "guard_fallback",
        }
    )
    updates["tool_calls"] = log

    if result.ok and result.data.get("refused"):
        updates["refused"] = True
        updates["refusal_reason"] = result.data.get("refuse_reason")
        updates["answer"] = answer6.REFUSE_MESSAGE
        updates["citations"] = []
        updates["citation_valid"] = True
        return updates

    if result.ok:
        contexts = result.data.get("contexts") or []
        updates["answer"] = result.data.get("answer") or state.get("answer") or ""
        updates["observed_context_count"] = max(state.get("observed_context_count", 0), len(contexts))
        return updates

    # 回退本身失败（例如 LLM 故障）：宁可拒答，也不放行无据答案
    updates["refused"] = True
    updates["refusal_reason"] = f"fallback_failed:{result.error_code}"
    updates["answer"] = answer6.REFUSE_MESSAGE
    return updates


# --------------------------------------------------------------------- final


def final_node(state: AgentState, config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
    """收口：保证有文本，并解析/校验引用编号。"""
    answer = state.get("answer") or ""
    error = state.get("error")

    if not answer:
        # 兜底：循环结束时还没有文本，再要一次（不带工具）
        messages = list(state.get("messages") or [])
        try:
            data = tc.post_chat_with_retry(tc._build_payload(messages, with_tools=False, temperature=0.2))
            answer = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        except Exception as exc:
            answer = tc.LLM_ERROR_MESSAGE
            error = f"{type(exc).__name__}: {exc}"

    n_ctx = state.get("observed_context_count", 0)
    if state.get("refused"):
        # 拒答句不该带引用
        cite = {"cited": [], "invalid_citations": [], "citation_valid": True}
    elif n_ctx:
        cite = answer6.parse_citations(answer, n_ctx)
    else:
        cite = {"cited": [], "invalid_citations": [], "citation_valid": True}

    return {
        "answer": answer,
        "error": error,
        "citations": cite["cited"],
        "citation_valid": cite["citation_valid"],
    }


# ------------------------------------------------------------------- routing


def route_after_agent(state: AgentState) -> str:
    if state.get("error"):
        return "guard"
    if not state.get("pending_tool_calls"):
        return "guard"
    return "tools"


def route_after_tools(state: AgentState) -> str:
    if state.get("step_count", 0) >= state.get("max_steps", DEFAULT_MAX_STEPS):
        return "guard"
    return "agent"


# --------------------------------------------------------------------- build


def build_graph():
    """编译状态图：START → agent ⇄ tools → guard → final → END。"""
    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("guard", guard_node)
    graph.add_node("final", final_node)

    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {"tools": "tools", "guard": "guard"})
    graph.add_conditional_edges("tools", route_after_tools, {"agent": "agent", "guard": "guard"})
    graph.add_edge("guard", "final")
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
