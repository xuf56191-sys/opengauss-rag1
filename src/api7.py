"""FastAPI wrapper: POST /ask (RAG 基线) + POST /agent (单 Agent)。

把能力封装成 HTTP 接口：
  - GET  /health  健康检查，返回向量库状态
  - POST /ask     原 RAG 链路：答案 + 引用校验 + 检索上下文（保持不变）
  - POST /agent   单 Agent + 工具调用 + 证据/拒答门闩（阶段 5 新增）
"""

import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 路径与环境初始化
# ---------------------------------------------------------------------------
# 本文件通常位于 <项目根>/api/ 或 <项目根>/app/ 下，
# parents[1] 即项目根目录，用于定位 src/ 和 .env。
ROOT = Path(__file__).resolve().parents[1]

# 把 src/ 加入 sys.path，这样可以直接 import answer6 / hybrid_retrieve5，
# 而不用把它们做成包。必须在 import 业务模块之前执行。
sys.path.insert(0, str(ROOT / "src"))

# 加载 .env（如 API Key、CHROMA_DIR 等），override=True 表示覆盖已有环境变量。
load_dotenv(ROOT / ".env", override=True)

# noqa: E402 —— 因为上面的 sys.path 操作必须在 import 之前，
# 所以这里的 import 位置不符合 PEP8（非文件顶部），显式忽略该告警。
from answer6 import answer  # noqa: E402
from hybrid_retrieve5 import HybridRetriever, CHROMA_DIR, COLLECTION  # noqa: E402
from agent.service import (  # noqa: E402
    AgentRequest,
    AgentResponse,
    run_agent_service,
)


# ---------------------------------------------------------------------------
# 请求 / 响应模型（Pydantic）
# ---------------------------------------------------------------------------
class AskRequest(BaseModel):
    """POST /ask 的请求体。"""
    query: str = Field(..., min_length=1, description="用户问题，不能为空")
    k: int = Field(3, ge=1, le=10, description="返回片段数")


class ContextOut(BaseModel):
    """单条检索上下文的输出结构，供前端展示“答案依据”。"""
    id: str
    heading_path: Optional[str] = None          # 文档标题层级，如 "3.2 安装 > 配置"
    score: float                                # 融合后的最终得分
    bm25_raw_score: Optional[float] = None      # BM25 原始分（调试用）
    vector_distance: Optional[float] = None     # 向量距离（越小越相似）
    preview: str                                # 片段预览文本


class AskResponse(BaseModel):
    """POST /ask 的响应体。"""
    query: str
    answer: str
    refused: bool                               # 是否因证据不足拒答
    refuse_reason: Optional[str] = None         # 拒答原因
    cited: List[int] = []                       # 答案中引用的片段编号
    invalid_citations: List[int] = []           # 引用了不存在片段的编号
    citation_valid: bool = True                 # 引用是否全部有效
    contexts: List[ContextOut] = []             # 实际检索到的上下文


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------
app = FastAPI(title="opengauss-rag", version="0.1.0")

# 检索器全局单例：首次访问时构建一次 BM25 索引，
# 避免每个请求都全量重建，显著降低 /ask 延迟。
_retriever: Optional[HybridRetriever] = None


def get_retriever() -> HybridRetriever:
    """懒加载并复用全局 HybridRetriever 实例。"""
    global _retriever
    if _retriever is None:
        _retriever = HybridRetriever()
    return _retriever


# ---------------------------------------------------------------------------
# 最小访问控制
# ---------------------------------------------------------------------------
# 设计取舍：
# - 只在配置了 API_ACCESS_KEY 时启用；未配置 = 本地开发不鉴权（不破坏 CLI/测试/本地体验）
# - /health 不鉴权，供平台健康检查与探活
# - 用 compare_digest 常量时间比较，避免计时侧信道
# - 这是「防误用/防被刷额度」的最小方案，不是完整用户体系（无账号、无配额、无审计）
API_ACCESS_KEY = os.getenv("API_ACCESS_KEY", "").strip()


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    """FastAPI 依赖：校验 X-API-Key。未配置 API_ACCESS_KEY 时直接放行。"""
    if not API_ACCESS_KEY:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, API_ACCESS_KEY):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


# ---------------------------------------------------------------------------
# 限流与配额（防公开地址被刷额度）
# ---------------------------------------------------------------------------
# 取舍（必须知道边界）：
# - 内存实现，单进程有效；重启即清零，多实例/多 worker 各自计数
# - 三层：每 IP 总次数配额（白名单豁免） + 每 IP 每分钟 + 全局每日总量
# - 只用于「防误用/防刷」，不是商用级网关；要持久化配额应换 Redis
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "10"))   # 0 = 关闭
RATE_LIMIT_PER_DAY = int(os.getenv("RATE_LIMIT_PER_DAY", "200"))  # 0 = 关闭
# 每个 IP 的**总**调用次数上限（0 = 关闭）。给「只让朋友试几次」的场景用
RATE_LIMIT_TOTAL_PER_IP = int(os.getenv("RATE_LIMIT_TOTAL_PER_IP", "0"))
# 白名单 IP：不受任何限流（容器内看到的客户端 IP 往往是 docker 网关，因此更推荐用 OWNER_KEY）
RATE_LIMIT_EXEMPT_IPS = {
    ip.strip()
    for ip in os.getenv("RATE_LIMIT_EXEMPT_IPS", "").split(",")
    if ip.strip()
}
# 主人密钥：请求头 X-Owner-Key 命中则不限额（自己演示用；公网部署时不要外传）
OWNER_KEY = os.getenv("OWNER_KEY", "").strip()

_rate_lock = threading.Lock()
_ip_hits: Dict[str, List[float]] = {}
_ip_total: Dict[str, int] = {}
_day_key: str = ""
_day_count: int = 0


def _client_ip(request: Request) -> str:
    """取真实客户端 IP。

    Render / 反向代理后 request.client.host 是代理地址，必须读 X-Forwarded-For 第一跳；
    未经过可信代理时该头可伪造，所以这层限流只用于「防误用」，不作为安全边界。
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def check_rate_limit(
    client_id: str,
    now: Optional[float] = None,
    owner: bool = False,
) -> Optional[int]:
    """返回 None 表示放行；否则返回建议的 Retry-After 秒数。

    顺序：主人密钥 → 白名单 IP → 每 IP 总配额 → 全局每日 → 每 IP 每分钟。
    """
    global _day_key, _day_count
    ts = time.time() if now is None else now

    with _rate_lock:
        # 0) 主人：完全放行
        if owner:
            return None

        # 1) 白名单 IP 完全放行
        if client_id in RATE_LIMIT_EXEMPT_IPS:
            return None

        # 2) 每 IP 总配额：用完后不再恢复（重启进程才清零）
        if RATE_LIMIT_TOTAL_PER_IP > 0:
            used = _ip_total.get(client_id, 0)
            if used >= RATE_LIMIT_TOTAL_PER_IP:
                return 86400
            _ip_total[client_id] = used + 1

        # 3) 全局每日预算
        if RATE_LIMIT_PER_DAY > 0:
            today = _today()
            if _day_key != today:
                _day_key, _day_count = today, 0
            if _day_count >= RATE_LIMIT_PER_DAY:
                return 3600
            _day_count += 1

        # 4) 每 IP 每分钟
        if RATE_LIMIT_PER_MIN > 0:
            hits = [t for t in _ip_hits.get(client_id, []) if ts - t < 60]
            if len(hits) >= RATE_LIMIT_PER_MIN:
                _ip_hits[client_id] = hits
                return max(1, int(60 - (ts - hits[0])) + 1)
            hits.append(ts)
            _ip_hits[client_id] = hits

    return None


def reset_rate_state() -> None:
    """仅测试使用：清空计数。"""
    global _day_key, _day_count
    with _rate_lock:
        _ip_hits.clear()
        _ip_total.clear()
        _day_key, _day_count = "", 0


def enforce_rate_limit(
    request: Request,
    x_owner_key: Optional[str] = Header(default=None, alias="X-Owner-Key"),
) -> None:
    """限流依赖：带对 X-Owner-Key 的主人不受限；其他人走每 IP 配额。"""
    owner = bool(OWNER_KEY) and bool(x_owner_key) and secrets.compare_digest(x_owner_key, OWNER_KEY)
    retry = check_rate_limit(_client_ip(request), owner=owner)
    if retry is not None:
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded, please retry later",
            headers={"Retry-After": str(retry)},
        )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> Dict:
    """健康检查：确认检索器已就绪，并返回向量库基本信息。"""
    col_count = None
    try:
        hr = get_retriever()
        col_count = len(hr.ids)     # 已加载的片段总数
    except Exception as e:
        # 检索器初始化失败（如 Chroma 目录缺失），直接报 500，便于探活识别。
        raise HTTPException(status_code=500, detail=f"retriever not ready: {e}")
    return {
        "status": "ok",
        "collection": COLLECTION,
        "chroma_dir": CHROMA_DIR,
        "chunks": col_count,
    }


@app.post("/ask", response_model=AskResponse)
def ask(
    req: AskRequest,
    _auth: None = Depends(require_api_key),
    _rate: None = Depends(enforce_rate_limit),
) -> AskResponse:
    """接收用户问题，调用 RAG 问答主流程并返回结构化结果。"""
    # 复用进程内单例检索器，避免每次 /ask 重建 BM25
    hr = get_retriever()

    try:
        result = answer(req.query, k=req.k, hr=hr)
    except RuntimeError as e:
        # 典型情况：API Key 缺失、LLM 服务返回错误等。
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        # 其他未预期异常，统一兜底并带上原始错误信息。
        raise HTTPException(status_code=500, detail=f"ask failed: {e}")

    # 将 answer() 的 dict 结果映射为强类型响应模型，
    # 缺省字段使用 .get(..., default) 兜底，避免 KeyError。
    return AskResponse(
        query=result["query"],
        answer=result["answer"],
        refused=result.get("refused", False),
        refuse_reason=result.get("refuse_reason"),
        cited=result.get("cited", []),
        invalid_citations=result.get("invalid_citations", []),
        citation_valid=result.get("citation_valid", True),
        contexts=result.get("contexts", []),
    )


@app.post("/agent", response_model=AgentResponse)
def agent_endpoint(
    req: AgentRequest,
    _auth: None = Depends(require_api_key),
    _rate: None = Depends(enforce_rate_limit),
) -> AgentResponse:
    """单 Agent 入口：模型可选工具 → 执行 → 证据/拒答门闩 → 返回可观测结果。

    与 /ask 的区别：/ask 是固定 RAG 链路；/agent 由模型决定用哪些工具。
    两者共用同一个进程内检索器单例。
    """
    # 复用 /ask 的检索器单例：Agent 不额外建索引
    hr = get_retriever()
    # service 内部已兜底异常（落到 error 字段），所以这里不再抛 500
    return run_agent_service(req.query, max_steps=req.max_steps, hr=hr)