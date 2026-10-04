"""CLI 单测：不联网、不起服务，只验证入口与输出。"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from agent import service as agent_service
from agent import tools as tools_mod
from agent_cli import main


class FakeRetriever:
    ids = ["a", "b", "c"]


@pytest.fixture()
def patched(monkeypatch: pytest.MonkeyPatch):
    calls: List[Dict[str, Any]] = []

    def fake_run(query: str, max_steps: int = 3, hr: Any = None) -> agent_service.AgentResponse:
        calls.append({"query": query, "max_steps": max_steps, "hr": hr})
        if "天气" in query:
            return agent_service.AgentResponse(
                query=query,
                answer="根据现有文档无法确认。",
                refused=True,
                refusal_reason="no_evidence",
                steps=1,
                latency_ms=12.0,
            )
        return agent_service.AgentResponse(
            query=query,
            answer="它是 MySQL 协议开关 [1]。",
            tools_used=["get_config_detail"],
            tool_calls=[
                agent_service.AgentToolCall(
                    name="get_config_detail",
                    args={"name": "enable_dolphin_proto"},
                    ok=True,
                    origin="agent",
                )
            ],
            citations=[1],
            steps=2,
            latency_ms=34.0,
        )

    monkeypatch.setattr("agent_cli.run_agent_service", fake_run)
    monkeypatch.setattr("agent_cli.HybridRetriever", lambda: FakeRetriever())
    return calls


def test_once_mode_runs_single_question(patched, capsys: pytest.CaptureFixture) -> None:
    code = main(["--once", "enable_dolphin_proto 是干什么的？"])

    out = capsys.readouterr().out
    assert code == 0
    assert "它是 MySQL 协议开关 [1]。" in out
    assert "工具: get_config_detail" in out
    assert "步数: 2" in out
    assert len(patched) == 1


def test_refusal_is_shown_with_reason(patched, capsys: pytest.CaptureFixture) -> None:
    main(["--once", "今天西安天气怎么样？"])

    out = capsys.readouterr().out
    assert "[拒答] reason=no_evidence" in out
    assert "工具: -" in out


def test_max_steps_is_clamped(patched) -> None:
    main(["--once", "q", "--max-steps", "99"])

    assert patched[0]["max_steps"] == 6


def test_interactive_tools_and_quit(patched, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    inputs = iter([":tools", ":quit"])

    def fake_input(_prompt: str = "") -> str:
        return next(inputs)

    monkeypatch.setattr("builtins.input", fake_input)

    code = main([])

    out = capsys.readouterr().out
    assert code == 0
    for spec in tools_mod.TOOL_SPECS:
        assert spec["name"] in out
    assert patched == []  # 没有真正提问
