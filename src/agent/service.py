"""Agent 服务层：把 LangGraph 图包装成稳定的请求/响应契约。

分层原因：
- graph.py 关心「怎么流转」；
- service.py 关心「对外给什么字段」，便于 API、CLI、评测共用同一形状；
- 检索器由调用方注入（api7 的单例），避免在 Agent 路径上重建 BM25。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pydantic import BaseModel, Field  # noqa: E402

try:
    from .graph import DEFAULT_MAX_STEPS, run_agent as _run_agent
except ImportError:  # 允许直接以脚本方式运行
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from graph import DEFAULT_MAX_STEPS, run_agent as _run_agent  # type: ignore


class AgentRequest(BaseModel):
    """POST /agent 的请求体。"""

    query: str = Field(..., min_length=1, description="用户问题，不能为空")
    max_steps: int = Field(
        DEFAULT_MAX_STEPS,
        ge=1,
        le=6,
        description="最多允许的 LLM 轮数（含收口轮），用于限制工具调用次数",
    )


class AgentToolCall(BaseModel):
    """单次工具调用的可观测记录。"""

    name: str
    args: Dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    error_code: Optional[str] = None
    latency_ms: float = 0.0
    origin: str = Field("agent", description="agent=模型主动调用；guard_fallback=门闩回退补救")


class AgentResponse(BaseModel):
    """POST /agent 的响应体（字段直接服务于 Agent Eval）。"""

    query: str
    answer: str
    refused: bool = False
    refusal_reason: Optional[str] = None
    tools_used: List[str] = Field(default_factory=list)
    tool_calls: List[AgentToolCall] = Field(default_factory=list)
    citations: List[int] = Field(default_factory=list)
    citation_valid: bool = True
    steps: int = 0
    latency_ms: float = 0.0
    error: Optional[str] = None


def run_agent_service(
    query: str,
    max_steps: int = DEFAULT_MAX_STEPS,
    hr: Any = None,
) -> AgentResponse:
    """执行 Agent 并映射为响应模型；异常统一落到 error 字段。"""
    try:
        result = _run_agent(query, hr=hr, max_steps=max_steps)
    except Exception as exc:  # 图内部已尽量兜底，这里再保一层，保证接口不 500
        return AgentResponse(
            query=query,
            answer="Agent 执行失败，请稍后重试。",
            refused=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    return AgentResponse(
        query=result.get("query", query),
        answer=result.get("answer") or "",
        refused=bool(result.get("refused")),
        refusal_reason=result.get("refusal_reason"),
        tools_used=list(result.get("tools_used") or []),
        tool_calls=[AgentToolCall(**call) for call in (result.get("tool_calls") or [])],
        citations=list(result.get("citations") or []),
        citation_valid=bool(result.get("citation_valid", True)),
        steps=int(result.get("steps") or 0),
        latency_ms=float(result.get("latency_ms") or 0.0),
        error=result.get("error"),
    )


if __name__ == "__main__":
    # 真实冒烟（会调用模型与检索）
    import json

    for q in ["enable_dolphin_proto 是干什么的？", "今天西安天气怎么样？"]:
        resp = run_agent_service(q)
        print("=" * 70)
        print("Q:", q)
        print(json.dumps(resp.model_dump(), ensure_ascii=False, indent=2)[:900])
