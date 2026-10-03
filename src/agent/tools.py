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

from __future__ import annotations  # 启用延迟注解，方便类型前向引用

import re          # 用于校验配置项名称的正则表达式
import sys         # 用于动态调整 sys.path，确保能导入项目模块
import time        # 用于统计工具调用耗时
from pathlib import Path  # 用于处理文件路径
from typing import Any, Callable, Dict, List, Optional  # 类型提示工具

# src/agent/tools.py → parents[0]=agent, [1]=src, [2]=repo root
# 计算项目根目录，并把 src 加入 sys.path，使得后续可以直接 import answer6 / hybrid_retrieve5
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import answer6  # noqa: E402  # 复用已有的完整 RAG 问答能力（含拒答与引用校验）
from hybrid_retrieve5 import HybridRetriever  # noqa: E402  # 复用已有的混合检索器

try:
    # 正常包内导入：从同级 schemas 模块导入统一返回结构 ToolResult
    from .schemas import ToolResult
except ImportError:  # 允许 `python src/agent/tools.py` 直接冒烟
    # 如果直接以脚本方式运行，则把当前文件所在目录加入 sys.path，再导入 schemas
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from schemas import ToolResult

# 便于 pytest monkeypatch：模块属性可替换
# 把 answer6.answer 赋值给模块级变量 _rag_answer，单测时可以替换为假函数，避免真实调用 LLM
_rag_answer: Callable[..., Dict[str, Any]] = answer6.answer

# snake_case 配置项：enable_dolphin_proto / dolphin_server_port 等
# 用于校验配置项名称是否合法：以字母或下划线开头，后面跟字母、数字或下划线
_CONFIG_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 给 Agent 看的工具说明（阶段 2 的 Tool Calling 会用到）
# 这是一个符合 OpenAI function calling 风格的工具描述列表，包含名称、描述和参数 JSON Schema
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
    """把 HybridRetriever.search 的 hit 规整成 Tool 输出（字段来自真实实现）。

    该函数是内部辅助函数，用于从原始检索结果中提取对 Agent 有用的字段，
    避免把整个 hit 字典直接暴露出去，同时统一字段命名。

    Args:
        hit: HybridRetriever.search 返回的单个命中项，通常包含 id、meta、text、score 等。

    Returns:
        一个精简后的字典，包含 id、heading_path、source、score、bm25_raw_score、
        vector_distance、in_vector、in_bm25、text 等字段。
    """
    meta = hit.get("meta") or {}          # 元数据，可能为 None，转为空字典
    text = hit.get("text") or ""          # 正文，可能为 None，转为空字符串
    return {
        "id": hit.get("id"),              # 文档片段唯一标识
        "heading_path": meta.get("heading_path"),  # 标题路径，便于定位
        "source": meta.get("source"),     # 来源文件
        "score": hit.get("score"),        # 混合检索最终得分
        "bm25_raw_score": hit.get("bm25_raw_score"),  # BM25 原始分
        "vector_distance": hit.get("vector_distance"),  # 向量距离
        "in_vector": hit.get("in_vector"),  # 是否命中向量检索
        "in_bm25": hit.get("in_bm25"),      # 是否命中 BM25
        "text": text,                       # 片段正文
    }


def search_docs(
    query: str,
    k: int = 3,
    hr: Optional[HybridRetriever] = None,
) -> ToolResult:
    """只做检索，不生成。

    对应工具 search_docs：在语料库中执行混合检索（BM25 + 向量），返回相关文档片段。

    Args:
        query: 检索问题或关键词，不能为空。
        k: 返回的片段数量，必须在 1~10 之间，默认 3。
        hr: 可选的 HybridRetriever 实例，用于测试时注入假检索器；为 None 时自动创建。

    Returns:
        ToolResult: 统一返回结构。成功时 data 包含 query、k、count、hits 列表；
                    失败时 ok=False，并带有 error_code 和 error_message。
    """
    tool = "search_docs"                       # 工具名，用于日志和返回结构
    t0 = time.perf_counter()                   # 记录开始时间，用于计算耗时

    # 参数校验：query 不能为空
    if not query or not query.strip():
        return ToolResult.failure(tool, "invalid_query", "query 不能为空", t0)
    # 参数校验：k 的范围
    if k < 1 or k > 10:
        return ToolResult.failure(tool, "invalid_k", "k 必须在 1~10 之间", t0)

    try:
        # 如果传入了 hr 则使用，否则创建默认的 HybridRetriever
        retriever = hr or HybridRetriever()
        # 调用检索器的 search 方法，执行混合检索
        hits = retriever.search(query.strip(), k=k)
    except Exception as exc:  # 网络/向量库等异常统一转成 Tool 错误
        # 任何异常都转换为结构化失败，避免异常穿透到 Agent 图
        return ToolResult.failure(tool, "tool_error", f"{type(exc).__name__}: {exc}", t0)

    # 成功时，把原始 hit 规整后返回
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
    """配置项精确向检索：用配置名检索，并只保留正文真正出现该名字的片段。

    对应工具 get_config_detail：适合查询 snake_case 形式的配置项说明。

    Args:
        name: 配置项名称，必须符合 snake_case 格式（字母/数字/下划线，且不以数字开头）。
        k: 检索返回的片段数，必须在 1~10 之间，默认 3。
        hr: 可选的 HybridRetriever 实例，便于测试注入。

    Returns:
        ToolResult: 成功时 data 包含 name、count、hits（已过滤为正文包含该配置名的片段）；
                    失败时可能返回 invalid_config_name、invalid_k、tool_error、config_not_found。
    """
    tool = "get_config_detail"                 # 工具名
    t0 = time.perf_counter()                   # 开始计时

    # 校验配置名格式：不能为空且必须匹配 snake_case 正则
    if not name or not _CONFIG_NAME_RE.match(name.strip()):
        return ToolResult.failure(
            tool,
            "invalid_config_name",
            "配置名需形如 enable_dolphin_proto（字母/数字/下划线）",
            t0,
        )
    # 校验 k 的范围
    if k < 1 or k > 10:
        return ToolResult.failure(tool, "invalid_k", "k 必须在 1~10 之间", t0)

    needle = name.strip().lower()              # 用于后续在正文中做大小写不敏感匹配
    try:
        retriever = hr or HybridRetriever()    # 获取检索器
        hits = retriever.search(name.strip(), k=k)  # 用配置名作为查询进行检索
    except Exception as exc:
        # 异常统一转为 tool_error
        return ToolResult.failure(tool, "tool_error", f"{type(exc).__name__}: {exc}", t0)

    # 只保留正文中确实出现了该配置名的片段（大小写不敏感）
    matched = [h for h in hits if needle in (h.get("text") or "").lower()]
    if not matched:
        # 如果检索到了片段但没有一个正文包含该配置名，则视为未找到
        return ToolResult.failure(
            tool,
            "config_not_found",
            f"检索到 {len(hits)} 个片段，但正文均未出现该配置名",
            t0,
        )

    # 成功返回过滤后的片段
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
    """完整 RAG（含拒答与引用校验），直接复用 answer6.answer。

    对应工具 rag_answer：执行检索、低相关拒答、基于资料生成回答并标注引用。

    Args:
        query: 用户问题，不能为空。
        k: 检索片段数，必须在 1~10 之间，默认 3。
        hr: 可选的 HybridRetriever 实例，透传给 answer6.answer。

    Returns:
        ToolResult: 成功时 data 为 answer6.answer 的原始返回字典；
                    失败时可能返回 invalid_query、invalid_k、tool_error。
    """
    tool = "rag_answer"                        # 工具名
    t0 = time.perf_counter()                   # 开始计时

    # 校验 query
    if not query or not query.strip():
        return ToolResult.failure(tool, "invalid_query", "query 不能为空", t0)
    # 校验 k
    if k < 1 or k > 10:
        return ToolResult.failure(tool, "invalid_k", "k 必须在 1~10 之间", t0)

    try:
        # 调用模块级变量 _rag_answer，便于测试时替换
        result = _rag_answer(query.strip(), k=k, hr=hr)
    except Exception as exc:
        # 异常统一转为结构化错误
        return ToolResult.failure(tool, "tool_error", f"{type(exc).__name__}: {exc}", t0)

    # 成功返回，data 为 answer6.answer 的返回值
    return ToolResult.success(tool, result, t0)


# 工具注册表：阶段 2 的 Tool Calling / 阶段 3 的图直接按名字取用
# 键为工具名，值为对应的可调用函数（均为 ToolResult 返回）
TOOLS: Dict[str, Callable[..., ToolResult]] = {
    "search_docs": search_docs,
    "get_config_detail": get_config_detail,
    "rag_answer": rag_answer,
}


def call_tool(name: str, args: Dict[str, Any], hr: Optional[HybridRetriever] = None) -> ToolResult:
    """按名字调用工具；未知工具名返回结构化错误，方便 Agent 自我纠正。

    Args:
        name: 要调用的工具名，必须是 TOOLS 中注册的键。
        args: 传递给工具的参数字典，将作为关键字参数展开。
        hr: 可选的 HybridRetriever 实例，会以 hr=hr 的形式传给工具函数。

    Returns:
        ToolResult: 工具执行结果。如果工具名未知，返回 unknown_tool 错误；
                    如果参数不匹配（TypeError），返回 invalid_args 错误。
    """
    t0 = time.perf_counter()                   # 开始计时
    fn = TOOLS.get(name)                       # 从注册表中查找函数
    if fn is None:
        # 未知工具名：返回结构化错误，列出可用工具，帮助 Agent 纠正
        return ToolResult.failure(
            name or "unknown",
            "unknown_tool",
            f"未注册的工具：{name}；可用：{sorted(TOOLS)}",
            t0,
        )
    try:
        # 展开 args 作为关键字参数，并传入 hr
        return fn(**(args or {}), hr=hr)
    except TypeError as exc:
        # 参数不匹配（例如缺少必需参数、多余参数）转为 invalid_args
        return ToolResult.failure(name, "invalid_args", f"参数不匹配：{exc}", t0)


if __name__ == "__main__":
    # 手动冒烟（会调用真实嵌入 API）
    # 遍历两个查询，分别测试 search_docs、get_config_detail 的行为
    for q in ["enable_dolphin_proto 是干什么的？", "今天西安天气怎么样？"]:
        print("=" * 60)
        print("search_docs:", q)
        res = search_docs(q, k=2)              # 调用 search_docs，取 2 个片段
        print("ok=", res.ok, "count=", res.data.get("count"), "latency_ms=", res.latency_ms)
        for h in res.data.get("hits", []):
            # 打印每个片段的标题路径、BM25 原始分和向量距离
            print(f"  - {h['heading_path']} bm25={h['bm25_raw_score']:.3f} dist={h['vector_distance']:.3f}")

    print("=" * 60)
    print("get_config_detail:", "enable_dolphin_proto")
    res = get_config_detail("enable_dolphin_proto")  # 查询一个合法配置项
    print("ok=", res.ok, res.error_code, res.error_message)

    print("=" * 60)
    print("get_config_detail 非法名:", "enable dolphin proto")
    res = get_config_detail("enable dolphin proto")  # 查询一个非法配置名（含空格）
    print("ok=", res.ok, res.error_code)