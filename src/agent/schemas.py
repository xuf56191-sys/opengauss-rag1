"""Tool 与 Agent 的结构化契约（Pydantic）。

设计原则：
- Tool 一律返回同一形状，Agent/评测才能稳定解析；
- 失败也用返回值表达（ok=False + error_code），不靠抛异常穿透到图里。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    """所有 Tool 的统一返回结构。"""

    tool: str = Field(..., description="工具名，如 search_docs")
    ok: bool = Field(..., description="本次调用是否成功")
    data: Dict[str, Any] = Field(default_factory=dict, description="成功时的业务数据")
    error_code: Optional[str] = Field(
        None,
        description="失败码：invalid_query / invalid_config_name / config_not_found / tool_error",
    )
    error_message: Optional[str] = Field(None, description="失败说明，便于日志与评测归因")
    latency_ms: float = Field(0.0, description="本次调用耗时（毫秒）")

    @classmethod
    def success(cls, tool: str, data: Dict[str, Any], started_at: float) -> "ToolResult":
        return cls(
            tool=tool,
            ok=True,
            data=data,
            latency_ms=round((time.perf_counter() - started_at) * 1000, 2),
        )

    @classmethod
    def failure(
        cls,
        tool: str,
        error_code: str,
        error_message: str,
        started_at: float,
    ) -> "ToolResult":
        return cls(
            tool=tool,
            ok=False,
            error_code=error_code,
            error_message=error_message,
            latency_ms=round((time.perf_counter() - started_at) * 1000, 2),
        )
