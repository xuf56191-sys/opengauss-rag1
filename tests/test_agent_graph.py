"""阶段 3 单测：LangGraph 图的流转（monkeypatch 大模型出口，不联网）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from agent import graph as g
from agent import tool_calling as tc


class FakeRetriever:
    def __init__(self, hits: Optional[List[Dict[str, Any]]] = None) -> None:
        self._hits = hits if hits is not None else []
        self.calls: List[Dict[str, Any]] = []

    def search(self, query: str, k: int = 3, pool: int = 10) -> List[Dict[str, Any]]:
        self.calls.append({"query": query, "k": k, "pool": pool})
        return self._hits[:k]


def _hits(n: int) -> List[Dict[str, Any]]:
    return [
        {
            "id": f"doc::{i}",
            "score": 0.03 - i * 0.001,
            "text": f"enable_dolphin_proto 相关说明 {i}",
            "meta": {"heading_path": f"2.{i} 连接前提", "source": "personal/peewee-notes.md"},
            "in_vector": True,
            "in_bm25": True,
            "vector_rank": i + 1,
            "bm25_rank": i + 1,
            "bm25_raw_score": 10.0 - i,
            "vector_distance": 0.3 + i * 0.05,
        }
        for i in range(n)
    ]


def _tool_call(name: str, arguments: str, call_id: str = "call_1") -> Dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _scripted(responses: List[Dict[str, Any]], captured: List[Dict[str, Any]]):
    def fake_post(payload: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
        captured.append(payload)
        if not responses:
            raise AssertionError("脚本响应已用尽：模型被调用了太多次")
        return responses.pop(0)

    return fake_post


# ------------------------------------------------------------------ 基本流转


def test_graph_tool_round_then_final(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: List[Dict[str, Any]] = []
    responses = [
        {"choices": [{"message": {"content": "", "tool_calls": [_tool_call("get_config_detail", '{"name": "enable_dolphin_proto"}')]}}]},
        {"choices": [{"message": {"content": "它是 MySQL 协议开关 [1]。"}}]},
    ]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, captured))

    out = g.run_agent(
        "enable_dolphin_proto 是干什么的？",
        hr=FakeRetriever(_hits(2)),  # type: ignore[arg-type]
    )

    assert out["error"] is None
    assert out["steps"] == 2
    assert out["tools_used"] == ["get_config_detail"]
    assert out["tool_calls"][0]["ok"] is True
    assert out["answer"] == "它是 MySQL 协议开关 [1]。"
    # 观测到 2 条命中（get_config_detail 过滤后仍含该配置名）→ [1] 合法
    assert out["citations"] == [1]
    assert out["citation_valid"] is True

    # 第二轮调用必须带上 tool 消息
    roles = [m["role"] for m in captured[1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]


def test_graph_no_tool_call_goes_straight_to_final(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: List[Dict[str, Any]] = []
    responses = [{"choices": [{"message": {"content": "不需要工具的直接回答。"}}]}]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, captured))

    out = g.run_agent("你好")

    assert out["steps"] == 1
    assert out["tool_calls"] == []
    assert out["answer"] == "不需要工具的直接回答。"
    assert out["citation_valid"] is True


def test_graph_stops_at_max_steps_and_forces_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型每轮都要求调工具时，图必须在 max_steps 内收口，不接受无限循环。"""
    captured: List[Dict[str, Any]] = []
    responses = [
        {"choices": [{"message": {"content": "", "tool_calls": [_tool_call("search_docs", '{"query": "a"}')]}}]},
        {"choices": [{"message": {"content": "", "tool_calls": [_tool_call("search_docs", '{"query": "b"}')]}}]},
        {"choices": [{"message": {"content": "", "tool_calls": [_tool_call("search_docs", '{"query": "c"}')]}}]},
        {"choices": [{"message": {"content": "被强制收口后的回答。"}}]},
    ]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, captured))

    out = g.run_agent("x", hr=FakeRetriever(_hits(1)), max_steps=3)  # type: ignore[arg-type]

    assert out["steps"] == 3
    # 最后一个 agent 轮与 final 兜底轮都不应再提供 tools
    assert "tools" in captured[0]
    assert "tools" in captured[1]
    assert "tools" not in captured[2]
    assert "tools" not in captured[3]
    assert out["answer"] == "被强制收口后的回答。"


# ------------------------------------------------------------------ 异常与拒答


def test_graph_llm_error_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(payload: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
        raise RuntimeError("LLM HTTP 500: oops")

    monkeypatch.setattr(tc, "_post_chat", boom)

    out = g.run_agent("x", hr=FakeRetriever(_hits(1)))  # type: ignore[arg-type]

    assert out["error"] is not None
    assert "LLM HTTP 500" in out["error"]
    assert out["answer"] == tc.LLM_ERROR_MESSAGE
    assert out["steps"] == 1


def test_graph_tool_error_is_logged_and_flow_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: List[Dict[str, Any]] = []
    responses = [
        {"choices": [{"message": {"content": "", "tool_calls": [_tool_call("no_such_tool", "{}")]}}]},
        {"choices": [{"message": {"content": "换个方式回答。"}}]},
    ]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, captured))

    out = g.run_agent("x", hr=FakeRetriever(_hits(1)))  # type: ignore[arg-type]

    assert out["tool_calls"][0]["error_code"] == "unknown_tool"
    assert out["error"] is None
    assert out["answer"] == "换个方式回答。"


def test_graph_records_refusal_from_rag_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """阶段 3 只记录 refused；阶段 4 才会强制用它覆盖模型文本。"""
    captured: List[Dict[str, Any]] = []
    responses = [
        {"choices": [{"message": {"content": "", "tool_calls": [_tool_call("rag_answer", '{"query": "天气"}')]}}]},
        {"choices": [{"message": {"content": "模型自己编的总结。"}}]},
    ]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, captured))

    def fake_call_tool(name: str, args: Dict[str, Any], hr: Any = None) -> tc.ToolResult:
        return tc.ToolResult.success(
            name,
            {
                "query": args.get("query"),
                "answer": "根据现有文档无法确认。",
                "refused": True,
                "refuse_reason": "high_vector_distance",
                "cited": [],
                "invalid_citations": [],
                "citation_valid": True,
                "contexts": [{"id": "x", "heading_path": "h", "score": 0.01, "preview": "..."}],
            },
            0.0,
        )

    monkeypatch.setattr(g, "call_tool", fake_call_tool)

    out = g.run_agent("今天西安天气怎么样？", hr=FakeRetriever(_hits(1)))  # type: ignore[arg-type]

    assert out["refused"] is True
    assert out["refusal_reason"] == "high_vector_distance"
    # 阶段 3 的已知缺陷：模型仍可能改写拒答（阶段 4 修）
    assert out["answer"] == "模型自己编的总结。"


# ------------------------------------------------------------------ citation


def test_graph_flags_out_of_range_citation(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: List[Dict[str, Any]] = []
    responses = [
        {"choices": [{"message": {"content": "", "tool_calls": [_tool_call("search_docs", '{"query": "x"}')]}}]},
        {"choices": [{"message": {"content": "参考了资料 [5]。"}}]},
    ]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, captured))

    out = g.run_agent("x", hr=FakeRetriever(_hits(2)))  # type: ignore[arg-type]

    assert out["citations"] == []
    assert out["citation_valid"] is False


# ------------------------------------------------------------------ routing


@pytest.mark.parametrize(
    "state,expected",
    [
        ({"error": "boom"}, "final"),
        ({"pending_tool_calls": []}, "final"),
        ({"pending_tool_calls": [{"id": "c1"}]}, "tools"),
    ],
)
def test_route_after_agent(state: Dict[str, Any], expected: str) -> None:
    assert g.route_after_agent(state) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "state,expected",
    [
        ({"step_count": 3, "max_steps": 3}, "final"),
        ({"step_count": 1, "max_steps": 3}, "agent"),
    ],
)
def test_route_after_tools(state: Dict[str, Any], expected: str) -> None:
    assert g.route_after_tools(state) == expected  # type: ignore[arg-type]
