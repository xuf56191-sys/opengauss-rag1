"""最小访问控制单测（全离线，不建检索器、不调模型）。

规则：
- 未配置 API_ACCESS_KEY → 放行（本地开发）
- 配置了 → 必须带正确的 X-API-Key，否则 401
- /health 不鉴权（平台探活用）
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import api7


class _FakeRetriever:
    ids = ["a", "b"]


@pytest.fixture()
def client() -> TestClient:
    return TestClient(api7.app)


@pytest.fixture()
def no_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(api7, "API_ACCESS_KEY", "")
    monkeypatch.setattr(api7, "get_retriever", lambda: _FakeRetriever())
    monkeypatch.setattr(
        api7,
        "answer",
        lambda query, k=3, hr=None: {
            "query": query,
            "answer": "ok",
            "refused": False,
            "refuse_reason": None,
            "cited": [],
            "invalid_citations": [],
            "citation_valid": True,
            "contexts": [],
        },
    )


@pytest.fixture()
def with_key(monkeypatch: pytest.MonkeyPatch, no_key) -> str:
    key = "test-key-123"
    monkeypatch.setattr(api7, "API_ACCESS_KEY", key)
    return key


# ------------------------------------------------------------ 依赖函数直测

def test_dependency_allows_when_key_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api7, "API_ACCESS_KEY", "")
    assert api7.require_api_key(None) is None


def test_dependency_rejects_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api7, "API_ACCESS_KEY", "k")
    with pytest.raises(Exception) as exc:
        api7.require_api_key(None)
    assert "401" in str(getattr(exc.value, "status_code", "")) or getattr(exc.value, "status_code", 0) == 401


def test_dependency_rejects_wrong_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api7, "API_ACCESS_KEY", "k")
    with pytest.raises(Exception) as exc:
        api7.require_api_key("wrong")
    assert getattr(exc.value, "status_code", 0) == 401


def test_dependency_accepts_correct_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api7, "API_ACCESS_KEY", "k")
    assert api7.require_api_key("k") is None


# ------------------------------------------------------------ HTTP 层行为

def test_ask_works_without_key_when_auth_disabled(client: TestClient, no_key) -> None:
    resp = client.post("/ask", json={"query": "hello"})
    assert resp.status_code == 200


def test_ask_requires_key_when_configured(client: TestClient, with_key: str) -> None:
    assert client.post("/ask", json={"query": "hello"}).status_code == 401
    assert client.post("/ask", json={"query": "hello"}, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/ask", json={"query": "hello"}, headers={"X-API-Key": with_key}).status_code == 200


def test_agent_requires_key_when_configured(
    client: TestClient, with_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent import service as agent_service

    monkeypatch.setattr(
        api7,
        "run_agent_service",
        lambda query, max_steps=3, hr=None: agent_service.AgentResponse(query=query, answer="ok"),
    )
    assert client.post("/agent", json={"query": "hello"}).status_code == 401
    assert client.post("/agent", json={"query": "hello"}, headers={"X-API-Key": with_key}).status_code == 200


def test_health_is_public(client: TestClient, with_key: str) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_constant_time_compare_used() -> None:
    """防止有人把 compare_digest 改回 == 比较（安全回归）。"""
    import inspect

    src = inspect.getsource(api7.require_api_key)
    assert "compare_digest" in src
