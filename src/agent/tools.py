"""Agent 的 Tool 层：只做「薄封装」，复用已有 RAG 能力，不重写检索/生成。

工具清单（阶段 1 只做 3 个）：
- search_docs(query, k)        只检索，返回混合检索的真实 hit 字段
- get_config_detail(name, k)   配置项（snake_case）精确向检索
- rag_answer(query, k)         复用 answer6.answer：检索→拒答→LLM→引用校验

复用约定：
- 检索统一走 hybrid_retrieve5.HybridRetriever.search
- 生成/拒答统一走 answer6.answer（含 should_refuse 与 citation 校验）
- hr 可注入，便于 pytest 用假检索器做离线单测
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# src/agent/tools.py → parents[0]=agent, [1]=src, [2]=repo root
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import answer6  # noqa: E402
from hybrid_retrieve5 import HybridRetriever  # noqa: E402

try:
    from .schemas import ToolResult
except ImportError:  # 允许 `python src/agent/tools.py` 直接冒烟
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from schemas import ToolResult

# 便于 pytest monkeypatch：模块属性可替换
_rag_answer: Callable[..., Dict[str, Any]] = answer6.answer

# snake_case 配置项：enable_dolphin_proto / dolphin_server_port 等
_CONFIG_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 给 Agent 看的工具说明（阶段 2 的 Tool Calling 会用到）
TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "search_docs",
        "description": "在 openGauss/Peewee 语料中做混合检索（BM25+向量），只返回相关文档片段，不生成回答。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索问题或关键词"},
                "k": {"type": "integer", "description": "返回片段数，默认 3", "default": 3},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_config_detail",
        "description": "查询某个配置项的说明（如 enable_dolphin_proto），适合精确的 snake_case 配置名。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "配置项名，例如 enable_dolphin_proto"},
                "k": {"type": "integer", "description": "返回片段数，默认 3", "default": 3},
            },
            "required": ["name"],
        },
    },
    {
        "name": "rag_answer",
        "description": "完整 RAG 问答：检索→低相关拒答→基于资料生成并标注 [n] 引用。适合直接回答 openGauss 配置问题。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用户问题"},
                "k": {"type": "integer", "description": "检索片段数，默认 3", "default": 3},
            },
            "required": ["query"],
        },
    },
]


def _hit_payload(hit: Dict[str, Any]) -> Dict[str, Any]:
    """把 HybridRetriever.search 的 hit 规整成 Tool 输出（字段来自真实实现）。"""
    meta = hit.get("meta") or {}
    text = hit.get("text") or ""
    return {
        "id": hit.get("id"),
        "heading_path": meta.get("heading_path"),
        "source": meta.get("source"),
        "score": hit.get("score"),
        "bm25_raw_score": hit.get("bm25_raw_score"),
        "vector_distance": hit.get("vector_distance"),
        "in_vector": hit.get("in_vector"),
        "in_bm25": hit.get("in_bm25"),
        "text": text,
    }


def search_docs(
    query: str,
    k: int = 3,
    hr: Optional[HybridRetriever] = None,
) -> ToolResult:
    """只做检索，不生成。"""
    tool = "search_docs"
    t0 = time.perf_counter()

    if not query or not query.strip():
        return ToolResult.failure(tool, "invalid_query", "query 不能为空", t0)
    if k < 1 or k > 10:
        return ToolResult.failure(tool, "invalid_k", "k 必须在 1~10 之间", t0)

    try:
        retriever = hr or HybridRetriever()
        hits = retriever.search(query.strip(), k=k)
    except Exception as exc:  # 网络/向量库等异常统一转成 Tool 错误
        return ToolResult.failure(tool, "tool_error", f"{type(exc).__name__}: {exc}", t0)

    return ToolResult.success(
        tool,
        {
            "query": query.strip(),
            "k": k,
            "count": len(hits),
            "hits": [_hit_payload(h) for h in hits],
        },
        t0,
    )


def get_config_detail(
    name: str,
    k: int = 3,
    hr: Optional[HybridRetriever] = None,
) -> ToolResult:
    """配置项精确向检索：用配置名检索，并只保留正文真正出现该名字的片段。"""
    tool = "get_config_detail"
    t0 = time.perf_counter()

    if not name or not _CONFIG_NAME_RE.match(name.strip()):
        return ToolResult.failure(
            tool,
            "invalid_config_name",
            "配置名需形如 enable_dolphin_proto（字母/数字/下划线）",
            t0,
        )
    if k < 1 or k > 10:
        return ToolResult.failure(tool, "invalid_k", "k 必须在 1~10 之间", t0)

    needle = name.strip().lower()
    try:
        retriever = hr or HybridRetriever()
        hits = retriever.search(name.strip(), k=k)
    except Exception as exc:
        return ToolResult.failure(tool, "tool_error", f"{type(exc).__name__}: {exc}", t0)

    matched = [h for h in hits if needle in (h.get("text") or "").lower()]
    if not matched:
        return ToolResult.failure(
            tool,
            "config_not_found",
            f"检索到 {len(hits)} 个片段，但正文均未出现该配置名",
            t0,
        )

    return ToolResult.success(
        tool,
        {
            "name": name.strip(),
            "count": len(matched),
            "hits": [_hit_payload(h) for h in matched],
        },
        t0,
    )


def rag_answer(
    query: str,
    k: int = 3,
    hr: Optional[HybridRetriever] = None,
) -> ToolResult:
    """完整 RAG（含拒答与引用校验），直接复用 answer6.answer。"""
    tool = "rag_answer"
    t0 = time.perf_counter()

    if not query or not query.strip():
        return ToolResult.failure(tool, "invalid_query", "query 不能为空", t0)
    if k < 1 or k > 10:
        return ToolResult.failure(tool, "invalid_k", "k 必须在 1~10 之间", t0)

    try:
        result = _rag_answer(query.strip(), k=k, hr=hr)
    except Exception as exc:
        return ToolResult.failure(tool, "tool_error", f"{type(exc).__name__}: {exc}", t0)

    return ToolResult.success(tool, result, t0)


# 工具注册表：阶段 2 的 Tool Calling / 阶段 3 的图直接按名字取用
TOOLS: Dict[str, Callable[..., ToolResult]] = {
    "search_docs": search_docs,
    "get_config_detail": get_config_detail,
    "rag_answer": rag_answer,
}


def call_tool(name: str, args: Dict[str, Any], hr: Optional[HybridRetriever] = None) -> ToolResult:
    """按名字调用工具；未知工具名返回结构化错误，方便 Agent 自我纠正。"""
    t0 = time.perf_counter()
    fn = TOOLS.get(name)
    if fn is None:
        return ToolResult.failure(
            name or "unknown",
            "unknown_tool",
            f"未注册的工具：{name}；可用：{sorted(TOOLS)}",
            t0,
        )
    try:
        return fn(**(args or {}), hr=hr)
    except TypeError as exc:
        return ToolResult.failure(name, "invalid_args", f"参数不匹配：{exc}", t0)


if __name__ == "__main__":
    # 手动冒烟（会调用真实嵌入 API）
    for q in ["enable_dolphin_proto 是干什么的？", "今天西安天气怎么样？"]:
        print("=" * 60)
        print("search_docs:", q)
        res = search_docs(q, k=2)
        print("ok=", res.ok, "count=", res.data.get("count"), "latency_ms=", res.latency_ms)
        for h in res.data.get("hits", []):
            print(f"  - {h['heading_path']} bm25={h['bm25_raw_score']:.3f} dist={h['vector_distance']:.3f}")

    print("=" * 60)
    print("get_config_detail:", "enable_dolphin_proto")
    res = get_config_detail("enable_dolphin_proto")
    print("ok=", res.ok, res.error_code, res.error_message)

    print("=" * 60)
    print("get_config_detail 非法名:", "enable dolphin proto")
    res = get_config_detail("enable dolphin proto")
    print("ok=", res.ok, res.error_code)
