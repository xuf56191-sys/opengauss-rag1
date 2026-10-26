"""限流与每日预算的单测（全离线，时间用参数注入，不 sleep）。"""

from __future__ import annotations

import pytest

import api7


@pytest.fixture(autouse=True)
def clean_state():
    api7.reset_rate_state()
    yield
    api7.reset_rate_state()


def test_allows_within_minute_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api7, "RATE_LIMIT_PER_MIN", 3)
    monkeypatch.setattr(api7, "RATE_LIMIT_PER_DAY", 0)

    now = 1_000.0
    for _ in range(3):
        assert api7.check_rate_limit("1.2.3.4", now) is None
    retry = api7.check_rate_limit("1.2.3.4", now)
    assert retry is not None and retry >= 1


def test_window_slides_after_60s(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api7, "RATE_LIMIT_PER_MIN", 2)
    monkeypatch.setattr(api7, "RATE_LIMIT_PER_DAY", 0)

    t = 1_000.0
    assert api7.check_rate_limit("ip", t) is None
    assert api7.check_rate_limit("ip", t + 1) is None
    assert api7.check_rate_limit("ip", t + 2) is not None
    # 61 秒后旧记录过期
    assert api7.check_rate_limit("ip", t + 61) is None


def test_ip_buckets_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api7, "RATE_LIMIT_PER_MIN", 1)
    monkeypatch.setattr(api7, "RATE_LIMIT_PER_DAY", 0)

    t = 500.0
    assert api7.check_rate_limit("a", t) is None
    assert api7.check_rate_limit("a", t) is not None
    assert api7.check_rate_limit("b", t) is None


def test_daily_budget_blocks_everyone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api7, "RATE_LIMIT_PER_MIN", 0)   # 关闭每分钟限制
    monkeypatch.setattr(api7, "RATE_LIMIT_PER_DAY", 2)

    assert api7.check_rate_limit("a", 100.0) is None
    assert api7.check_rate_limit("b", 101.0) is None
    # 第三人（不同 IP）也挡住
    assert api7.check_rate_limit("c", 102.0) == 3600


def test_daily_counter_resets_on_new_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api7, "RATE_LIMIT_PER_MIN", 0)
    monkeypatch.setattr(api7, "RATE_LIMIT_PER_DAY", 1)

    monkeypatch.setattr(api7, "_today", lambda: "2026-01-01")
    assert api7.check_rate_limit("a", 1.0) is None
    assert api7.check_rate_limit("a", 2.0) == 3600

    monkeypatch.setattr(api7, "_today", lambda: "2026-01-02")
    assert api7.check_rate_limit("a", 3.0) is None


def test_can_disable_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api7, "RATE_LIMIT_PER_MIN", 0)
    monkeypatch.setattr(api7, "RATE_LIMIT_PER_DAY", 0)

    for _ in range(100):
        assert api7.check_rate_limit("any", 1.0) is None


def test_http_layer_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端：超过每分钟限制时 /ask 返回 429，且不再进入业务逻辑。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(api7, "API_ACCESS_KEY", "")
    monkeypatch.setattr(api7, "RATE_LIMIT_PER_MIN", 1)
    monkeypatch.setattr(api7, "RATE_LIMIT_PER_DAY", 0)
    monkeypatch.setattr(api7, "get_retriever", lambda: type("R", (), {"ids": []})())
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

    client = TestClient(api7.app)
    assert client.post("/ask", json={"query": "x"}).status_code == 200
    resp = client.post("/ask", json={"query": "x"})
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


def test_client_ip_prefers_forwarded_header(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Req:
        headers = {"x-forwarded-for": "8.8.8.8, 10.0.0.1"}
        client = type("C", (), {"host": "10.0.0.1"})()

    assert api7._client_ip(_Req()) == "8.8.8.8"  # type: ignore[arg-type]
