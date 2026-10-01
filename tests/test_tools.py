"""阶段 1 单测：Tool 层不联网（注入假检索器 / monkeypatch rag 函数）。

真实 hit 形状参照 hybrid_retrieve5.HybridRetriever.search 的返回字段。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from agent import tools as tools_mod
from agent.schemas import ToolResult

# ---------------------------------------------------------------- 假检索器

FAKE_HITS: List[Dict[str, Any]] = [
    {
        "id": "personal/peewee-notes.md::2",
        "score": 0.036,
        "text": "enable_dolphin_proto 是是否打开 MySQL 协议能力的开关。",
        "meta": {"heading_path": "2. 连接前提", "source": "personal/peewee-notes.md"},
        "in_vector": True,
        "in_bm25": True,
        "vector_rank": 1,
        "bm25_rank": 1,
        "bm25_raw_score": 13.79,
        "vector_distance": 0.48,
    },
    {
        "id": "personal/peewee-notes.md::3",
        "score": 0.015,
        "text": "dolphin_server_port 决定 MySQL 协议监听端口。",
        "meta": {"heading_path": "2. 连接前提", "source": "personal/peewee-notes.md"},
        "in_vector": True,
        "in_bm25": False,
        "vector_rank": 2,
        "bm25_rank": None,
        "bm25_raw_score": 2.73,
        "vector_distance": 0.47,
    },
]


class FakeRetriever:
    """鸭子类型替身：只实现 search，避免测试依赖 Chroma/嵌入 API。"""

    def __init__(self, hits: Optional[List[Dict[str, Any]]] = None, boom: bool = False) -> None:
        self._hits = hits if hits is not None else FAKE_HITS
        self._boom = boom
        self.calls: List[Dict[str, Any]] = []

    def search(self, query: str, k: int = 3, pool: int = 10) -> List[Dict[str, Any]]:
        self.calls.append({"query": query, "k": k, "pool": pool})
        if self._boom:
            raise RuntimeError("vector store down")
        return self._hits[:k]


# ---------------------------------------------------------------- search_docs


def test_search_docs_returns_normalized_hits() -> None:
    fake = FakeRetriever()

    res = tools_mod.search_docs("enable_dolphin_proto 是干什么的？", k=2, hr=fake)  # type: ignore[arg-type]

    assert isinstance(res, ToolResult)
    assert res.ok is True
    assert res.tool == "search_docs"
    assert res.error_code is None
    assert res.latency_ms >= 0
    assert res.data["count"] == 2

    hit = res.data["hits"][0]
    # 字段必须来自真实 hit，而不是编造
    assert hit["heading_path"] == "2. 连接前提"
    assert hit["bm25_raw_score"] == 13.79
    assert hit["vector_distance"] == 0.48
    assert "enable_dolphin_proto" in hit["text"]

    # 透传 k，不额外乱改参数
    assert fake.calls == [{"query": "enable_dolphin_proto 是干什么的？", "k": 2, "pool": 10}]


@pytest.mark.parametrize("bad_query", ["", "   "])
def test_search_docs_rejects_empty_query(bad_query: str) -> None:
    res = tools_mod.search_docs(bad_query, hr=FakeRetriever())  # type: ignore[arg-type]

    assert res.ok is False
    assert res.error_code == "invalid_query"


def test_search_docs_rejects_bad_k() -> None:
    res = tools_mod.search_docs("x", k=0, hr=FakeRetriever())  # type: ignore[arg-type]

    assert res.ok is False
    assert res.error_code == "invalid_k"


def test_search_docs_wraps_retriever_exception() -> None:
    res = tools_mod.search_docs("x", hr=FakeRetriever(boom=True))  # type: ignore[arg-type]

    assert res.ok is False
    assert res.error_code == "tool_error"
    assert "RuntimeError" in (res.error_message or "")


# ---------------------------------------------------------- get_config_detail


def test_get_config_detail_filters_by_name_in_text() -> None:
    fake = FakeRetriever()

    res = tools_mod.get_config_detail("enable_dolphin_proto", k=3, hr=fake)  # type: ignore[arg-type]

    assert res.ok is True
    assert res.data["name"] == "enable_dolphin_proto"
    # 第二条虽然被检索回来，但正文没有该配置名 → 被过滤
    assert res.data["count"] == 1
    assert res.data["hits"][0]["heading_path"] == "2. 连接前提"


def test_get_config_detail_invalid_name() -> None:
    res = tools_mod.get_config_detail("enable dolphin proto", hr=FakeRetriever())  # type: ignore[arg-type]

    assert res.ok is False
    assert res.error_code == "invalid_config_name"


def test_get_config_detail_not_found() -> None:
    res = tools_mod.get_config_detail("not_exist_cfg", hr=FakeRetriever())  # type: ignore[arg-type]

    assert res.ok is False
    assert res.error_code == "config_not_found"


# ---------------------------------------------------------------- rag_answer


def test_rag_answer_delegates_to_answer6(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}

    def fake_answer(query: str, k: int = 3, hr: Any = None) -> Dict[str, Any]:
        captured.update({"query": query, "k": k, "hr": hr})
        return {
            "query": query,
            "answer": "控制是否打开 MySQL 协议能力 [1]。",
            "refused": False,
            "refuse_reason": None,
            "cited": [1],
            "invalid_citations": [],
            "citation_valid": True,
            "contexts": [{"id": "x", "heading_path": "2. 连接前提", "score": 0.03, "preview": "..."}],
        }

    monkeypatch.setattr(tools_mod, "_rag_answer", fake_answer)
    fake_hr = FakeRetriever()

    res = tools_mod.rag_answer("enable_dolphin_proto 是干什么的？", k=2, hr=fake_hr)  # type: ignore[arg-type]

    assert res.ok is True
    assert captured == {"query": "enable_dolphin_proto 是干什么的？", "k": 2, "hr": fake_hr}
    assert res.data["citation_valid"] is True
    assert res.data["refused"] is False


def test_rag_answer_wraps_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        raise RuntimeError("LLM HTTP 500")

    monkeypatch.setattr(tools_mod, "_rag_answer", boom)

    res = tools_mod.rag_answer("q", hr=FakeRetriever())  # type: ignore[arg-type]

    assert res.ok is False
    assert res.error_code == "tool_error"
    assert "LLM HTTP 500" in (res.error_message or "")


# ---------------------------------------------------------------- call_tool


def test_call_tool_unknown_tool() -> None:
    res = tools_mod.call_tool("no_such_tool", {})

    assert res.ok is False
    assert res.error_code == "unknown_tool"
    assert "search_docs" in (res.error_message or "")


def test_call_tool_invalid_args() -> None:
    res = tools_mod.call_tool("search_docs", {"not_a_param": 1}, hr=FakeRetriever())  # type: ignore[arg-type]

    assert res.ok is False
    assert res.error_code == "invalid_args"


def test_tool_registry_matches_specs() -> None:
    spec_names = {s["name"] for s in tools_mod.TOOL_SPECS}
    assert spec_names == set(tools_mod.TOOLS)
