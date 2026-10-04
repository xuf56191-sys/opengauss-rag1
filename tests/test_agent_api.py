"""阶段 5 单测：POST /agent 与 POST /ask 的接口契约（不联网）。

要点：
- /agent 与 /ask 共存，且 /ask 行为不变；
- Agent 复用 api7 的检索器单例（不额外建索引）；
- 校验失败由 Pydantic 处理（422）；
- service 层异常落 error 字段，而不是 500。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

import api7
import answer6
from agent import service as agent_service


class FakeRetriever:
    def __init__(self) -> None:
        self.search_calls: List[Dict[str, Any]] = []

    def search(self, query: str, k: int = 3, pool: int = 10) -> List[Dict[str, Any]]:
        self.search_calls.append({"query": query, "k": k, "pool": pool})
        return []


@pytest.fixture()
def client() -> TestClient:
    return TestClient(api7.app)


@pytest.fixture()
def fake_hr() -> FakeRetriever:
    return FakeRetriever()


def test_agent_endpoint_maps_all_fields(
    client: TestClient, fake_hr: FakeRetriever, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: Dict[str, Any] = {}

    def fake_run(query: str, hr: Any = None, max_steps: int = 3) -> Dict[str, Any]:
        captured.update({"query": query, "hr": hr, "max_steps": max_steps})
        return {
            "query": query,
            "answer": "它是 MySQL 协议开关 [1]。",
            "tool_calls": [
                {
                    "name": "get_config_detail",
                    "args": {"name": "enable_dolphin_proto"},
                    "ok": True,
                    "error_code": None,
                    "latency_ms": 12.5,
                    "origin": "agent",
                }
            ],
            "tools_used": ["get_config_detail"],
            "steps": 2,
            "refused": False,
            "refusal_reason": None,
            "citations": [1],
            "citation_valid": True,
            "error": None,
            "latency_ms": 1234.5,
        }

    monkeypatch.setattr(agent_service, "_run_agent", fake_run)
    monkeypatch.setattr(api7, "get_retriever", lambda: fake_hr)

    resp = client.post("/agent", json={"query": "enable_dolphin_proto 是干什么的？", "max_steps": 3})

    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "它是 MySQL 协议开关 [1]。"
    assert body["tools_used"] == ["get_config_detail"]
    assert body["tool_calls"][0]["origin"] == "agent"
    assert body["citations"] == [1]
    assert body["citation_valid"] is True
    assert body["refused"] is False
    assert body["steps"] == 2
    assert body["latency_ms"] == 1234.5
    assert body["error"] is None

    # 复用 /ask 的检索器单例
    assert captured["hr"] is fake_hr
    assert captured["query"] == "enable_dolphin_proto 是干什么的？"


def test_agent_endpoint_rejects_empty_query(client: TestClient) -> None:
    resp = client.post("/agent", json={"query": ""})

    assert resp.status_code == 422


def test_agent_endpoint_rejects_too_many_steps(client: TestClient) -> None:
    resp = client.post("/agent", json={"query": "x", "max_steps": 7})

    assert resp.status_code == 422


def test_agent_service_turns_exception_into_error_field(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(query: str, hr: Any = None, max_steps: int = 3) -> Dict[str, Any]:
        raise RuntimeError("graph exploded")

    monkeypatch.setattr(agent_service, "_run_agent", boom)

    resp = agent_service.run_agent_service("x")

    assert resp.error is not None
    assert "graph exploded" in resp.error
    assert resp.refused is False


def test_ask_endpoint_still_works(
    client: TestClient, fake_hr: FakeRetriever, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归：新增 /agent 不能改变 /ask 的行为。"""
    monkeypatch.setattr(api7, "get_retriever", lambda: fake_hr)

    def fake_answer(query: str, k: int = 3, hr: Any = None) -> Dict[str, Any]:
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

    monkeypatch.setattr(api7, "answer", fake_answer)

    resp = client.post("/ask", json={"query": "今天西安天气怎么样？", "k": 3})

    assert resp.status_code == 200
    body = resp.json()
    assert body["refused"] is True
    assert body["refuse_reason"] == "high_vector_distance"
    assert body["answer"] == "根据现有文档无法确认。"


def test_ask_refusal_message_matches_rag_layer(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """拒答文案以 answer6 的常量为准，接口不重复定义。"""
    assert answer6.REFUSE_MESSAGE

    monkeypatch.setattr(api7, "get_retriever", lambda: FakeRetriever())
    monkeypatch.setattr(
        api7,
        "answer",
        lambda query, k=3, hr=None: {
            "query": query,
            "answer": answer6.REFUSE_MESSAGE,
            "refused": True,
            "refuse_reason": "no_evidence",
            "cited": [],
            "invalid_citations": [],
            "citation_valid": True,
            "contexts": [],
        },
    )

    resp = client.post("/ask", json={"query": "x"})
    assert resp.json()["answer"] == answer6.REFUSE_MESSAGE


def test_agent_response_model_defaults() -> None:
    """响应模型字段齐全，便于 Agent Eval 直接消费。"""
    resp = agent_service.AgentResponse(query="q", answer="a")

    assert resp.refused is False
    assert resp.tools_used == []
    assert resp.tool_calls == []
    assert resp.citations == []
    assert resp.citation_valid is True
    assert resp.steps == 0
    assert resp.latency_ms == 0.0
    assert resp.error is None


def test_agent_tool_call_origin_defaults_to_agent() -> None:
    call = agent_service.AgentToolCall(name="search_docs")

    assert call.origin == "agent"
    assert call.ok is True
