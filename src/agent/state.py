"""Agent 状态定义（LangGraph State）。

说明：
- 用 TypedDict 描述跨节点传的结构化状态；
- 每个节点只返回「要更新的字段」，未返回的字段由 LangGraph 保留原值；
- messages 采用整体替换（不做 add_messages 累加），节点自己维护完整列表，行为更直观。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from typing import TypedDict


class AgentState(TypedDict, total=False):
    """Agent 图的共享状态。total=False 表示字段可缺省。"""

    query: str                      # 用户原始问题
    messages: List[Dict[str, Any]]  # OpenAI 风格消息序列（system/user/assistant/tool）
    step_count: int                 # 已经历的 agent（LLM）轮数
    max_steps: int                  # 最多允许的 LLM 轮数
    pending_tool_calls: List[Dict[str, Any]]  # 最近一次 assistant 要求调用的工具
    tool_calls: List[Dict[str, Any]]          # 全部工具调用日志（名称/参数/成败/耗时）
    observed_context_count: int     # 观测到的最大资料片段数，用于 citation 范围校验
    answer: str                     # 最终回答文本
    refused: bool                   # 是否因资料不足应拒答（阶段 4 会强制使用）
    refusal_reason: Optional[str]   # 拒答原因
    citations: List[int]            # 解析出的有效引用编号
    citation_valid: bool            # 引用是否全部落在资料范围内
    error: Optional[str]            # 流程级错误
    latency_ms: float               # 端到端耗时
