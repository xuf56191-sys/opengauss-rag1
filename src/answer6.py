"""RAG generate: hybrid retrieve -> prompt -> DeepSeek chat."""
# 模块说明：RAG 生成主流程
# 核心逻辑：混合检索（Hybrid Retrieve） -> 构造提示词（Prompt） -> 调用 DeepSeek 大模型生成回答

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv

# 计算项目根目录，并把 src 加入模块搜索路径
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 导入你自己写的混合检索器（注意：这里导入的是 hybrid_retrieve5）
from hybrid_retrieve5 import HybridRetriever

import re

# 校准数据见 peek_signals / data/eval_cases.json / docs/eval_report.md
# 中文单字分词会让库外题 BM25 虚高，故不能只依赖 bm25；语义距离过远也要拒。
# 语料扩到 ~120 chunks 后：库内 top min_dist≈0.27~0.36，库外 GIL≈0.533 → DIST_MIN=0.52
REFUSE_BM25_MAX = 2.0
REFUSE_DIST_MIN = 0.52
REFUSE_MESSAGE = (
    "根据现有文档无法确认。"
    "当前检索结果与问题相关度过低，已触发代码层拒答，未调用模型生成配置步骤。"
    "若是 openGauss 相关问题，请补充配置项名称、兼容模式或连接场景等关键词。"
)

def should_refuse(hits: List[Dict]) -> tuple:
    """检索层拒答：返回 (是否拒答, 原因)

    规则（评测后收紧）：
    - 无检索结果 → no_hits
    - max_bm25 < 2.0  → 词法证据不足
    - min_vec_dist > 0.52 → 语义不像（任一条成立即拒）
    OR 而非 AND：库外题常见「BM25 虚高但向量很远」。
    """
    if not hits:
        return True, "no_hits"
    max_bm25 = max((h.get("bm25_raw_score") or 0.0) for h in hits)
    dists = [
        h["vector_distance"]
        for h in hits
        if h.get("vector_distance") is not None
    ]
    min_dist = min(dists) if dists else 999.0
    if max_bm25 < REFUSE_BM25_MAX:
        return True, "low_bm25"
    if min_dist > REFUSE_DIST_MIN:
        return True, "high_vector_distance"
    return False, ""

def parse_citations(answer_text: str, n_ctx: int) -> Dict:
    """解析 [n] 引用，校验是否越界"""
    nums = [int(x) for x in re.findall(r"\[(\d+)\]", answer_text)]
    valid = sorted({n for n in nums if 1 <= n <= n_ctx})
    invalid = sorted({n for n in nums if n < 1 or n > n_ctx})
    return {
        "cited": valid,
        "invalid_citations": invalid,
        "citation_valid": len(invalid) == 0,
    }

def _contexts_payload(hits: List[Dict]) -> List[Dict]:
    out = []
    for h in hits:
        out.append({
            "id": h["id"],
            "heading_path": h["meta"].get("heading_path"),
            "score": h["score"],
            "bm25_raw_score": h.get("bm25_raw_score"),
            "vector_distance": h.get("vector_distance"),
            "preview": h["text"][:160],
        })
    return out

# 加载 .env 环境变量
load_dotenv(ROOT / ".env", override=True)

# 大模型 API 配置
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
API_KEY = os.getenv("DEEPSEEK_API_KEY")

# 系统提示词（System Prompt）：定义大模型的角色和回答规则
# 这是 RAG 防幻觉的关键所在，规则必须严谨明确
SYSTEM_PROMPT = """你是 openGauss 配置问答助手。规则：
1. 只依据提供的「资料」回答，禁止使用资料外知识编造步骤或参数。
2. 每条关键结论后标注引用，如 [1] [2]，对应资料编号。
3. 若资料不足以回答，明确说「根据现有文档无法确认」，并说明还缺什么。
4. 用简洁中文，可保留必要的配置项英文名。
"""


def build_user_prompt(query: str, contexts: List[Dict]) -> str:
    """把检索到的多个文本片段，拼接成一段完整的用户提示词"""
    blocks = []
    # 遍历检索回来的文档，给每条编号（[1]、[2]...），并带上来源和小节信息
    for i, c in enumerate(contexts, 1):
        path = c["meta"].get("heading_path") or ""
        src = c["meta"].get("source") or ""
        # 格式：[编号] 来源: 文件名 | 小节: 标题路径 \n 正文
        blocks.append(f"[{i}] 来源: {src} | 小节: {path}\n{c['text']}")

    context_str = "\n\n".join(blocks)

    # 把用户问题、准备好的资料、以及指令拼接在一起，返回给大模型
    return (
        f"问题：{query}\n\n"
        f"资料：\n{context_str}\n\n"
        f"请根据资料回答问题。"
    )


def call_deepseek(messages: List[Dict], temperature: float = 0.2) -> str:
    """调用 DeepSeek 的 Chat API，发送对话消息，并返回生成的回答文本"""
    # 没配置 API Key 直接报错，避免无意义的网络请求
    if not API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY missing in .env")

    # 拼接完整的 API 地址
    url = f"{LLM_BASE_URL}/chat/completions"

    # 构建请求头，携带 Bearer Token 进行身份认证
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    # 构建请求体
    payload = {
        "model": LLM_MODEL,
        "messages": messages,  # 完整的对话历史（System + User）
        "temperature": temperature,  # 0.2 低温度，让回答更严谨、少发散（RAG 场景推荐 0~0.3）
        "stream": False,  # 关闭流式输出，等待完整回答
    }

    # 发送 POST 请求，大模型生成较慢，超时设为 120 秒
    resp = requests.post(url, headers=headers, json=payload, timeout=120)

    # 如果 HTTP 状态码不是 200，抛出异常并打印前 300 字符的错误信息
    if resp.status_code != 200:
        raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")

    # 解析响应 JSON，取出大模型生成的文本内容
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def answer(query: str, k: int = 3, hr: Optional[HybridRetriever] = None) -> Dict:
    """检索 -> 拒答判定 -> Prompt -> LLM -> 引用校验

    hr: 可选外部注入的 HybridRetriever，避免服务层每次请求重建 BM25。
    CLI 直接调用时保持 hr=None，内部懒加载即可。
    """
    if hr is None:
        hr = HybridRetriever()
    hits = hr.search(query, k=k)

    refuse, reason = should_refuse(hits)
    if refuse:
        # 不调 LLM，避免无关上下文诱发编造
        return {
            "query": query,
            "answer": REFUSE_MESSAGE,
            "refused": True,
            "refuse_reason": reason,
            "cited": [],
            "invalid_citations": [],
            "citation_valid": True,
            "contexts": _contexts_payload(hits),
        }

    user_prompt = build_user_prompt(query, hits)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    text = call_deepseek(messages)
    cite = parse_citations(text, len(hits))

    return {
        "query": query,
        "answer": text,
        "refused": False,
        "refuse_reason": None,
        "cited": cite["cited"],
        "invalid_citations": cite["invalid_citations"],
        "citation_valid": cite["citation_valid"],
        "contexts": _contexts_payload(hits),
    }


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "enable_dolphin_proto 是干什么的？"
    result = answer(q)

    print("Q:", result["query"])
    print("refused=", result["refused"], "reason=", result["refuse_reason"])
    print("=" * 60)
    print(result["answer"])
    print("=" * 60)
    print("cited=", result["cited"], "citation_valid=", result["citation_valid"])
    print("Sources:")
    for i, c in enumerate(result["contexts"], 1):
        print(
            f"  [{i}] {c['heading_path']}  rrf={c['score']:.5f} "
            f"bm25={c['bm25_raw_score']} dist={c['vector_distance']}"
        )