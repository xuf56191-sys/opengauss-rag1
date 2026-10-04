"""阶段 3–4 单测：LangGraph 流转 + 证据/拒答门闩（monkeypatch 大模型出口，不联网）。

分两部分：
- 图流转（阶段 3）：工具轮、直连收口、max_steps 收口、路由
- 门闩（阶段 4）：拒答覆盖、零证据判定、领域回退、工具重试
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

import answer6
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


def _resp(content: str = "", tool_calls: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    message: Dict[str, Any] = {"content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


def _scripted(responses: List[Dict[str, Any]], captured: List[Dict[str, Any]]):
    def fake_post(payload: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
        captured.append(payload)
        if not responses:
            raise AssertionError("脚本响应已用尽：模型被调用了太多次")
        return responses.pop(0)

    return fake_post


DOMAIN_Q = "enable_dolphin_proto 是干什么的？"


# =========================================================== 阶段 3：图流转


def test_graph_tool_round_then_final(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: List[Dict[str, Any]] = []
    responses = [
        _resp(tool_calls=[_tool_call("get_config_detail", '{"name": "enable_dolphin_proto"}')]),
        _resp(content="它是 MySQL 协议开关 [1]。"),
    ]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, captured))

    out = g.run_agent(DOMAIN_Q, hr=FakeRetriever(_hits(2)))  # type: ignore[arg-type]

    assert out["error"] is None
    assert out["steps"] == 2
    assert out["tools_used"] == ["get_config_detail"]
    assert out["tool_calls"][0]["ok"] is True
    assert out["tool_calls"][0]["origin"] == "agent"
    assert out["answer"] == "它是 MySQL 协议开关 [1]。"
    assert out["citations"] == [1]
    assert out["citation_valid"] is True

    roles = [m["role"] for m in captured[1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]


def test_graph_stops_at_max_steps_and_forces_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型每轮都要工具时，图必须在 max_steps 内收口。"""
    captured: List[Dict[str, Any]] = []
    responses = [
        _resp(tool_calls=[_tool_call("search_docs", '{"query": "a"}')]),
        _resp(tool_calls=[_tool_call("search_docs", '{"query": "b"}')]),
        _resp(tool_calls=[_tool_call("search_docs", '{"query": "c"}')]),
        _resp(content="被强制收口后的回答。"),
    ]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, captured))

    out = g.run_agent(DOMAIN_Q, hr=FakeRetriever(_hits(1)), max_steps=3)  # type: ignore[arg-type]

    assert out["steps"] == 3
    assert "tools" in captured[0]
    assert "tools" in captured[1]
    assert "tools" not in captured[2]
    assert "tools" not in captured[3]
    assert out["answer"] == "被强制收口后的回答。"


def test_graph_llm_error_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(payload: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
        raise RuntimeError("LLM HTTP 401: invalid api key")

    monkeypatch.setattr(tc, "_post_chat", boom)
    monkeypatch.setattr(tc, "LLM_MAX_ATTEMPTS", 1)  # 4xx 不可重试，且避免测试等待

    out = g.run_agent(DOMAIN_Q, hr=FakeRetriever(_hits(1)))  # type: ignore[arg-type]

    assert out["error"] is not None
    assert "LLM HTTP 401" in out["error"]
    assert out["answer"] == tc.LLM_ERROR_MESSAGE
    # 系统故障不等于「资料不足」，不应记成拒答
    assert out["refused"] is False


def test_graph_retries_transient_llm_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """503/连接类错误应重试；重试成功后流程正常继续（用非领域 query 隔离，避免触发真实回退）。"""
    captured: List[Dict[str, Any]] = []
    responses = [_resp(content="重试成功后的回答。")]
    calls = {"n": 0}

    def flaky(payload: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("LLM HTTP 503: service unavailable")
        return _scripted(responses, captured)(payload)

    monkeypatch.setattr(tc, "_post_chat", flaky)
    monkeypatch.setattr(tc, "LLM_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(tc.time, "sleep", lambda _s: None)  # 测试不等待

    out = g.run_agent("你好", hr=FakeRetriever())  # type: ignore[arg-type]

    assert calls["n"] == 2          # 第一次失败 + 重试一次
    assert out["error"] is None     # 重试后不再报错
    # 非领域且零证据 → 由门闩拒答（与 LLM 文本无关，这正是门闩的作用）
    assert out["refused"] is True
    assert out["refusal_reason"] == "no_evidence"


def test_graph_flags_out_of_range_citation(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: List[Dict[str, Any]] = []
    responses = [
        _resp(tool_calls=[_tool_call("search_docs", '{"query": "enable_dolphin_proto"}')]),
        _resp(content="参考了资料 [5]。"),
    ]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, captured))

    out = g.run_agent(DOMAIN_Q, hr=FakeRetriever(_hits(2)))  # type: ignore[arg-type]

    assert out["citations"] == []
    assert out["citation_valid"] is False


@pytest.mark.parametrize(
    "state,expected",
    [
        ({"error": "boom"}, "guard"),
        ({"pending_tool_calls": []}, "guard"),
        ({"pending_tool_calls": [{"id": "c1"}]}, "tools"),
    ],
)
def test_route_after_agent(state: Dict[str, Any], expected: str) -> None:
    assert g.route_after_agent(state) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "state,expected",
    [
        ({"step_count": 3, "max_steps": 3}, "guard"),
        ({"step_count": 1, "max_steps": 3}, "agent"),
    ],
)
def test_route_after_tools(state: Dict[str, Any], expected: str) -> None:
    assert g.route_after_tools(state) == expected  # type: ignore[arg-type]


# ======================================================= 阶段 4：拒答与证据


def test_refusal_from_tool_overrides_model_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """规则 1：检索层判拒答，模型话术必须被覆盖。"""
    responses = [
        _resp(tool_calls=[_tool_call("rag_answer", '{"query": "今天西安天气怎么样？"}')]),
        _resp(content="模型自己编的总结。"),
    ]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, []))

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
    assert out["answer"] == answer6.REFUSE_MESSAGE
    assert out["citations"] == []


def test_no_evidence_non_domain_refuses_without_extra_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """规则 4a：零证据且不属领域 → 直接拒答，不再多花一次工具调用。"""
    responses = [_resp(content="你好！有什么可以帮你？")]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, []))

    calls: List[str] = []
    monkeypatch.setattr(
        g,
        "call_tool",
        lambda name, args, hr=None: (calls.append(name), tc.ToolResult.failure(name, "x", "x", 0.0))[1],
    )

    out = g.run_agent("你好")

    assert out["refused"] is True
    assert out["refusal_reason"] == "no_evidence"
    assert out["answer"] == answer6.REFUSE_MESSAGE
    assert calls == []  # 没有触发回退调用


def test_no_evidence_domain_falls_back_to_rag_answer_and_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """规则 4b：领域问题却没走工具 → 回退 rag_answer；它说不可答就拒答。"""
    responses = [_resp(content="模型凭记忆编的默认值是 on。")]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, []))

    def fake_call_tool(name: str, args: Dict[str, Any], hr: Any = None) -> tc.ToolResult:
        assert name == "rag_answer"
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
                "contexts": [],
            },
            1.0,
        )

    monkeypatch.setattr(g, "call_tool", fake_call_tool)

    out = g.run_agent("enable_dolphin_proto 的默认值是多少？", hr=FakeRetriever(_hits(1)))  # type: ignore[arg-type]

    assert out["refused"] is True
    assert out["refusal_reason"] == "high_vector_distance"
    assert out["answer"] == answer6.REFUSE_MESSAGE
    assert "编的默认值" not in out["answer"]
    assert out["tool_calls"][-1]["origin"] == "guard_fallback"
    assert out["tool_calls"][-1]["name"] == "rag_answer"


def test_no_evidence_domain_fallback_replaces_ungrounded_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回退拿到有据回答时，用有据回答替换模型的无据回答。"""
    responses = [_resp(content="模型凭记忆编的答案。")]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, []))

    def fake_call_tool(name: str, args: Dict[str, Any], hr: Any = None) -> tc.ToolResult:
        return tc.ToolResult.success(
            name,
            {
                "query": args.get("query"),
                "answer": "根据资料：它是 MySQL 协议开关 [1]。",
                "refused": False,
                "refuse_reason": None,
                "cited": [1],
                "invalid_citations": [],
                "citation_valid": True,
                "contexts": [{"id": "x", "heading_path": "h", "score": 0.03, "preview": "..."}],
            },
            1.0,
        )

    monkeypatch.setattr(g, "call_tool", fake_call_tool)

    out = g.run_agent("enable_dolphin_proto 是干什么的？", hr=FakeRetriever(_hits(1)))  # type: ignore[arg-type]

    assert out["refused"] is False
    assert out["answer"] == "根据资料：它是 MySQL 协议开关 [1]。"
    assert out["citations"] == [1]


def test_fallback_failure_refuses_instead_of_leaking_error(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [_resp(content="模型编的答案。")]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, []))

    monkeypatch.setattr(
        g,
        "call_tool",
        lambda name, args, hr=None: tc.ToolResult.failure(name, "tool_error", "boom", 0.0),
    )

    out = g.run_agent("enable_dolphin_proto 是干什么的？", hr=FakeRetriever(_hits(1)))  # type: ignore[arg-type]

    assert out["refused"] is True
    assert out["refusal_reason"] == "fallback_failed:tool_error"
    assert out["answer"] == answer6.REFUSE_MESSAGE


def test_grounded_answer_passes_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """已有证据时，门闩不干预模型答案。"""
    responses = [
        _resp(tool_calls=[_tool_call("search_docs", '{"query": "enable_dolphin_proto"}')]),
        _resp(content="基于检索到的资料：它是协议开关 [1]。"),
    ]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, []))

    out = g.run_agent(DOMAIN_Q, hr=FakeRetriever(_hits(2)))  # type: ignore[arg-type]

    assert out["refused"] is False
    assert out["answer"] == "基于检索到的资料：它是协议开关 [1]。"
    assert out["citation_valid"] is True


# ======================================================= 阶段 4：工具重试


def test_transient_tool_error_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        _resp(tool_calls=[_tool_call("search_docs", '{"query": "enable_dolphin_proto"}')]),
        _resp(content="重试后的回答 [1]。"),
    ]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, []))

    calls: List[str] = []
    real_call_tool = g.call_tool

    def flaky(name: str, args: Dict[str, Any], hr: Any = None) -> tc.ToolResult:
        calls.append(name)
        if len(calls) == 1:
            return tc.ToolResult.failure(name, "tool_error", "temporary network fail", 0.0)
        return real_call_tool(name, args, hr=hr)

    monkeypatch.setattr(g, "call_tool", flaky)

    out = g.run_agent(DOMAIN_Q, hr=FakeRetriever(_hits(2)))  # type: ignore[arg-type]

    assert len(calls) == 2  # 第一次失败 + 重试一次
    assert out["tool_calls"][0]["ok"] is True
    assert out["refused"] is False


def test_invalid_args_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        _resp(tool_calls=[_tool_call("search_docs", '{"bad": 1}')]),
        _resp(content="换个方式回答 [1]。"),
    ]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, []))

    calls: List[str] = []
    real_call_tool = g.call_tool

    def counting(name: str, args: Dict[str, Any], hr: Any = None) -> tc.ToolResult:
        calls.append(name)
        if name == "rag_answer":
            # 门闩的领域回退：这里必须打桩，否则会真实调用 LLM（测试必须离线）
            return tc.ToolResult.failure(name, "tool_error", "stubbed in test", 0.0)
        return real_call_tool(name, args, hr=hr)

    monkeypatch.setattr(g, "call_tool", counting)

    out = g.run_agent(DOMAIN_Q, hr=FakeRetriever(_hits(2)))  # type: ignore[arg-type]

    # 参数错误只调一次（不重试）；随后的 rag_answer 不是重试，而是 guard 的领域回退
    assert calls == ["search_docs", "rag_answer"]
    assert out["tool_calls"][0]["error_code"] == "invalid_args"
    assert out["tool_calls"][0]["origin"] == "agent"
    assert out["tool_calls"][1]["origin"] == "guard_fallback"


# ======================================================= 领域启发式


@pytest.mark.parametrize(
    "query,expected",
    [
        ("enable_dolphin_proto 是干什么的？", True),
        ("openGauss 的 B 兼容模式是什么", True),
        ("Peewee 连不上 3308 端口", True),
        ("今天西安天气怎么样？", False),
        ("帮我写一首诗", False),
        ("你好", False),
    ],
)
def test_domain_heuristic(query: str, expected: bool) -> None:
    assert g._looks_domain_relevant(query) is expected
