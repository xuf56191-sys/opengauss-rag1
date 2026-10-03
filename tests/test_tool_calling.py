"""阶段 2 单测：Tool Calling 编排（monkeypatch 掉大模型出口，不联网）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from agent import tool_calling as tc


class FakeRetriever:
    """只实现 search，避免测试依赖 Chroma 与嵌入 API。"""

    def __init__(self, hits: Optional[List[Dict[str, Any]]] = None) -> None:
        self._hits = hits if hits is not None else []
        self.calls: List[Dict[str, Any]] = []

    def search(self, query: str, k: int = 3, pool: int = 10) -> List[Dict[str, Any]]:
        self.calls.append({"query": query, "k": k, "pool": pool})
        return self._hits[:k]


def _tool_call(name: str, arguments: str, call_id: str = "call_1") -> Dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _scripted(responses: List[Dict[str, Any]], captured: List[Dict[str, Any]]):
    """返回一个假的 _post_chat：按脚本依次吐出响应，并记录收到的 payload。"""

    def fake_post(payload: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
        captured.append(payload)
        if not responses:
            raise AssertionError("脚本响应已用尽，模型被调用了太多次")
        return responses.pop(0)

    return fake_post


# ------------------------------------------------------------------ 基本链路


def test_run_once_executes_tool_then_returns_final(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: List[Dict[str, Any]] = []
    fake_hr = FakeRetriever()
    responses = [
        {"choices": [{"message": {"content": "", "tool_calls": [_tool_call("search_docs", '{"query": "enable_dolphin_proto", "k": 2}')]}}]},
        {"choices": [{"message": {"content": "enable_dolphin_proto 控制 MySQL 协议能力。"}}]},
    ]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, captured))

    out = tc.run_once("enable_dolphin_proto 是干什么的？", hr=fake_hr)  # type: ignore[arg-type]

    assert out["error"] is None
    assert out["rounds"] == 2
    assert out["answer"] == "enable_dolphin_proto 控制 MySQL 协议能力。"
    assert out["tool_calls"] == [
        {"name": "search_docs", "args": {"query": "enable_dolphin_proto", "k": 2}, "ok": True, "error_code": None, "latency_ms": out["tool_calls"][0]["latency_ms"]}
    ]
    assert fake_hr.calls == [{"query": "enable_dolphin_proto", "k": 2, "pool": 10}]

    # 第二次调用必须带上 assistant.tool_calls 与 tool 结果，且 tool_call_id 对齐
    second_messages = captured[1]["messages"]
    roles = [m["role"] for m in second_messages]
    assert roles == ["system", "user", "assistant", "tool"]
    assert second_messages[2]["tool_calls"][0]["id"] == "call_1"
    assert second_messages[3]["tool_call_id"] == "call_1"
    assert '"ok":true' in second_messages[3]["content"]


def test_run_once_no_tool_calls_returns_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: List[Dict[str, Any]] = []
    responses = [{"choices": [{"message": {"content": "直接回答，不调用工具。"}}]}]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, captured))

    out = tc.run_once("你好")

    assert out["rounds"] == 1
    assert out["tool_calls"] == []
    assert out["answer"] == "直接回答，不调用工具。"


def test_last_round_is_called_without_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_tool_rounds=1 时：第 1 轮给工具，第 2 轮必须不给，强制收口。"""
    captured: List[Dict[str, Any]] = []
    responses = [
        {"choices": [{"message": {"content": "", "tool_calls": [_tool_call("search_docs", '{"query": "x"}')]}}]},
        {"choices": [{"message": {"content": "最终回答。"}}]},
    ]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, captured))

    out = tc.run_once("x", hr=FakeRetriever())  # type: ignore[arg-type]

    assert "tools" in captured[0]
    assert "tools" not in captured[1]
    assert out["rounds"] == 2
    assert out["answer"] == "最终回答。"


# ------------------------------------------------------------------ 异常分支


def test_invalid_json_arguments_logged_as_invalid_args(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: List[Dict[str, Any]] = []
    responses = [
        {"choices": [{"message": {"content": "", "tool_calls": [_tool_call("search_docs", "{not json")]}}]},
        {"choices": [{"message": {"content": "最终回答。"}}]},
    ]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, captured))

    out = tc.run_once("x", hr=FakeRetriever())  # type: ignore[arg-type]

    assert out["tool_calls"][0]["ok"] is False
    assert out["tool_calls"][0]["error_code"] == "invalid_args"
    assert out["answer"] == "最终回答。"


def test_unknown_tool_is_reported_not_crashed(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: List[Dict[str, Any]] = []
    responses = [
        {"choices": [{"message": {"content": "", "tool_calls": [_tool_call("no_such_tool", "{}")]}}]},
        {"choices": [{"message": {"content": "最终回答。"}}]},
    ]
    monkeypatch.setattr(tc, "_post_chat", _scripted(responses, captured))

    out = tc.run_once("x", hr=FakeRetriever())  # type: ignore[arg-type]

    assert out["tool_calls"][0]["error_code"] == "unknown_tool"
    assert out["error"] is None


def test_llm_failure_returns_error_without_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(payload: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
        raise RuntimeError("LLM HTTP 500: oops")

    monkeypatch.setattr(tc, "_post_chat", boom)

    out = tc.run_once("x", hr=FakeRetriever())  # type: ignore[arg-type]

    assert out["error"] is not None
    assert "LLM HTTP 500" in out["error"]
    assert out["answer"] == tc.LLM_ERROR_MESSAGE
    assert out["tool_calls"] == []


def test_refused_by_tool_flag_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """阶段 2 只做「标记」；阶段 4 会用它做强制拒答门闩。"""
    captured: List[Dict[str, Any]] = []

    def fake_post(payload: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
        captured.append(payload)
        if len(captured) == 1:
            return {"choices": [{"message": {"content": "", "tool_calls": [_tool_call("rag_answer", '{"query": "天气"}')]}}]}
        return {"choices": [{"message": {"content": "模型自己的总结。"}}]}

    monkeypatch.setattr(tc, "_post_chat", fake_post)

    def fake_rag_answer(query: str, k: int = 3, hr: Any = None) -> Dict[str, Any]:
        return {
            "query": query,
            "answer": "根据现有文档无法确认。",
            "refused": True,
            "refuse_reason": "high_vector_distance",
            "cited": [],
            "invalid_citations": [],
            "citation_valid": True,
            "contexts": [],
        }

    monkeypatch.setattr(tc, "call_tool", lambda name, args, hr=None: tc.ToolResult.success(name, fake_rag_answer(**args), 0.0))

    out = tc.run_once("今天西安天气怎么样？", hr=FakeRetriever())  # type: ignore[arg-type]

    assert out["refused_by_tool"] is True
    # 阶段 2 的行为：模型仍可能改写；阶段 4 才做强制门闩
    assert out["answer"] == "模型自己的总结。"


def test_agent_system_prompt_mentions_refusal_rule() -> None:
    assert "refused" in tc.AGENT_SYSTEM_PROMPT


def test_openai_tools_shape_matches_specs() -> None:
    names = [t["function"]["name"] for t in tc.OPENAI_TOOLS]
    assert names == [s["name"] for s in tc.TOOL_SPECS]
    for tool in tc.OPENAI_TOOLS:
        assert tool["type"] == "function"
        assert tool["function"]["parameters"]["type"] == "object"
