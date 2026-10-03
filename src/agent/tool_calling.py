"""阶段 2：最小 Tool Calling（不接 LangGraph）。

目标：把「LLM 决定调用工具 → 我们执行工具 → 结果回灌 → LLM 出最终答案」这一轮跑通并测出来。

复用说明：
- 工具来自 agent/tools.py 的 TOOL_SPECS 与 call_tool
- LLM 配置（Base URL / Model / Key）直接复用 answer6 里已加载的变量，不重复读 .env
- 本阶段只做 1 轮工具调用；步数上限、重试、拒答强制门闩在阶段 4 加
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

# src/agent/tool_calling.py → parents[0]=agent, [1]=src, [2]=repo root
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import answer6  # noqa: E402
from hybrid_retrieve5 import HybridRetriever  # noqa: E402
from answer6 import API_KEY, LLM_BASE_URL, LLM_MODEL  # noqa: E402

try:
    from .schemas import ToolResult
    from .tools import TOOL_SPECS, call_tool
except ImportError:  # 允许 `python src/agent/tool_calling.py` 直接冒烟
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from schemas import ToolResult
    from tools import TOOL_SPECS, call_tool

# OpenAI / DeepSeek 风格的 tools 参数：把内部 TOOL_SPECS 翻译成协议要求的形状
OPENAI_TOOLS: List[Dict[str, Any]] = [
    {"type": "function", "function": spec} for spec in TOOL_SPECS
]

AGENT_SYSTEM_PROMPT = """你是 openGauss 配置问答助手，可以调用工具获取资料。

可用工具：
- search_docs：检索文档片段
- get_config_detail：查询某个配置项（如 enable_dolphin_proto）
- rag_answer：需要完整回答且带引用时使用

规则：
1. 涉及 openGauss 配置事实时，必须先调用工具，不要凭记忆编造。
2. 工具返回的内容里若 refused=true，如实说明「根据现有文档无法确认」。
3. 回答简洁，保留必要的配置项英文名。
"""

LLM_ERROR_MESSAGE = "调用大模型失败，暂时无法回答。"


def resolve_llm_config() -> tuple:
    """Agent 专用 LLM 配置；未显式配置时回退到 answer6 的 DeepSeek 配置。

    支持用 AGENT_LLM_* 切换任意 OpenAI 兼容端点（如 SiliconFlow 的 Qwen），
    避免 Agent 验证被单一厂商 Key 卡住。返回 (base_url, model, api_key)。
    """
    base = os.getenv("AGENT_LLM_BASE_URL") or LLM_BASE_URL
    model = os.getenv("AGENT_LLM_MODEL") or LLM_MODEL
    key = os.getenv("AGENT_LLM_API_KEY")
    if not key:
        # 指向 SiliconFlow 时，复用已有的嵌入 Key（同一账号）
        key = os.getenv("SILICONFLOW_API_KEY") if "siliconflow" in base.lower() else API_KEY
    return base, model, key


def _post_chat(payload: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
    """唯一的大模型出口，便于单测 monkeypatch（不联网）。"""
    base, _model, key = resolve_llm_config()
    if not key:
        raise RuntimeError("缺少 Agent LLM API Key（AGENT_LLM_API_KEY / DEEPSEEK_API_KEY）")

    resp = requests.post(
        f"{base.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _build_payload(
    messages: List[Dict[str, Any]],
    with_tools: bool,
    temperature: float,
) -> Dict[str, Any]:
    _base, model, _key = resolve_llm_config()
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if with_tools:
        payload["tools"] = OPENAI_TOOLS
        payload["tool_choice"] = "auto"
    return payload


def _parse_tool_arguments(raw: str) -> tuple:
    """模型给的 arguments 是字符串，可能不是合法 JSON。"""
    text = (raw or "").strip()
    if not text:
        return {}, None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"arguments 不是合法 JSON: {exc}; raw={text[:200]}"
    if not isinstance(parsed, dict):
        return {}, f"arguments 不是对象: {type(parsed).__name__}"
    return parsed, None


def run_once(
    query: str,
    k: int = 3,
    hr: Optional[HybridRetriever] = None,
    max_tool_rounds: int = 1,
    temperature: float = 0.2,
) -> Dict[str, Any]:
    """最小 Tool Calling：最多 max_tool_rounds 轮工具调用，然后输出最终文本。

    返回字段（阶段 5 的 /agent 会在此基础上扩展）：
    query / answer / tool_calls / rounds / refused_by_tool / error / latency_ms
    """
    started = time.perf_counter()
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": query},
    ]
    tool_calls_log: List[Dict[str, Any]] = []
    refused_by_tool = False
    answer = ""
    rounds = 0

    for round_idx in range(max_tool_rounds + 1):
        # 最后一轮不再提供工具，强制模型产出文本，避免无限调用
        with_tools = round_idx < max_tool_rounds
        rounds = round_idx + 1

        try:
            data = _post_chat(_build_payload(messages, with_tools=with_tools, temperature=temperature))
        except Exception as exc:
            return {
                "query": query,
                "answer": LLM_ERROR_MESSAGE,
                "tool_calls": tool_calls_log,
                "rounds": rounds,
                "refused_by_tool": refused_by_tool,
                "error": f"{type(exc).__name__}: {exc}",
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            }

        message = (data.get("choices") or [{}])[0].get("message") or {}
        pending = message.get("tool_calls") or []

        if not pending:
            answer = message.get("content") or ""
            break

        # 助手先表态要调用哪些工具，随后每条工具结果都要带 tool_call_id 回灌
        messages.append(
            {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": pending,
            }
        )

        for call in pending:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            args, parse_error = _parse_tool_arguments(fn.get("arguments") or "")

            if parse_error:
                result = ToolResult.failure(name or "unknown", "invalid_args", parse_error, time.perf_counter())
            else:
                result = call_tool(name, args, hr=hr)

            if name == "rag_answer" and result.ok and result.data.get("refused"):
                refused_by_tool = True

            tool_calls_log.append(
                {
                    "name": name,
                    "args": args,
                    "ok": result.ok,
                    "error_code": result.error_code,
                    "latency_ms": result.latency_ms,
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": result.model_dump_json(),
                }
            )

    return {
        "query": query,
        "answer": answer,
        "tool_calls": tool_calls_log,
        "rounds": rounds,
        "refused_by_tool": refused_by_tool,
        "error": None,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


if __name__ == "__main__":
    # 真实冒烟（会调用大模型与嵌入 API）
    _base, _model, _key = resolve_llm_config()
    print(f"[llm] base={_base} model={_model} key={'set' if _key else 'MISSING'}")
    for q in [
        "enable_dolphin_proto 是干什么的？",
        "今天西安天气怎么样？",
    ]:
        print("=" * 70)
        print("Q:", q)
        out = run_once(q)
        print("rounds=", out["rounds"], "tool_calls=", out["tool_calls"], "error=", out["error"])
        print("refused_by_tool=", out["refused_by_tool"], "latency_ms=", out["latency_ms"])
        print("-" * 70)
        print(out["answer"])
