"""Tool 与 Agent 的结构化契约（Pydantic）。

设计原则：
- Tool 一律返回同一形状，Agent/评测才能稳定解析；
- 失败也用返回值表达（ok=False + error_code），不靠抛异常穿透到图里。
"""

from __future__ import annotations  # 启用延迟注解，让类型注解更灵活，便于前向引用

import time  # 用于统计工具调用耗时
from typing import Any, Dict, Optional  # 类型工具，用于描述字段和返回值结构

from pydantic import BaseModel, Field  # Pydantic 基类与字段定义工具


class ToolResult(BaseModel):
    """所有 Tool 的统一返回结构。"""

    # 工具名，用于标识本次调用来自哪个工具，例如 "search_docs"
    tool: str = Field(..., description="工具名，如 search_docs")

    # 本次调用是否成功；Agent/评测应优先判断该字段
    ok: bool = Field(..., description="本次调用是否成功")

    # 成功时的业务数据；失败时通常为空字典，保持返回结构稳定
    data: Dict[str, Any] = Field(default_factory=dict, description="成功时的业务数据")

    # 失败码，用于程序化归因；成功时为 None
    error_code: Optional[str] = Field(
        None,
        description="失败码：invalid_query / invalid_config_name / config_not_found / tool_error",
    )

    # 失败说明，用于日志排查和评测归因；成功时为 None
    error_message: Optional[str] = Field(None, description="失败说明，便于日志与评测归因")

    # 本次调用耗时，单位毫秒；默认 0.0，由 success/failure 自动填充
    latency_ms: float = Field(0.0, description="本次调用耗时（毫秒）")

    @classmethod
    def success(cls, tool: str, data: Dict[str, Any], started_at: float) -> "ToolResult":
        """构造一个成功结果。

        参数:
            tool: 工具名。
            data: 成功返回的业务数据。
            started_at: 调用开始时间，通常来自 time.perf_counter()。
        """
        return cls(
            tool=tool,  # 记录工具名
            ok=True,  # 标记成功
            data=data,  # 写入业务数据
            # 用当前时间减去开始时间得到耗时；乘以 1000 转毫秒，并保留两位小数
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
        """构造一个失败结果。

        参数:
            tool: 工具名。
            error_code: 失败码，便于程序化判断。
            error_message: 失败说明，便于日志与评测归因。
            started_at: 调用开始时间，通常来自 time.perf_counter()。
        """
        return cls(
            tool=tool,  # 记录工具名
            ok=False,  # 标记失败
            error_code=error_code,  # 写入失败码
            error_message=error_message,  # 写入失败说明
            # 同样计算本次调用耗时，并保留两位小数
            latency_ms=round((time.perf_counter() - started_at) * 1000, 2),
        )