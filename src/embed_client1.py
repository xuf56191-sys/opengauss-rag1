"""SiliconFlow embedding client. Keep this thin and hand-written."""
# 声明这是一个轻量级、手写的 SiliconFlow 嵌入客户端模块
# 模块顶部的 docstring，用于说明这个文件的作用

import os
# os 模块用于读取环境变量
from typing import List
# 引入类型提示，用于标明参数和返回值的类型

import requests
# requests 用于发送 HTTP 请求，调用 SiliconFlow 的 API
from dotenv import load_dotenv
# python-dotenv 用于从 .env 文件中加载环境变量到 os.environ

# 加载 .env 文件里的所有变量
# 这一步会读取项目根目录下的 .env 文件，把其中的键值对写入环境变量
load_dotenv()

# 从环境变量中读取 API 基础 URL，如果没设置则使用默认的 SiliconFlow 官方地址
EMBED_BASE_URL = os.getenv("EMBED_BASE_URL", "https://api.siliconflow.cn/v1")
# 从环境变量中读取要使用的嵌入模型，如果没设置则默认使用 BAAI/bge-m3 模型
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
# 读取 API Key，用于身份认证
API_KEY = os.getenv("SILICONFLOW_API_KEY")


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Batch embed. Returns one vector per input string."""
    # 函数 docstring：批量将文本转换为向量，每个输入字符串返回一个对应的向量列表

    # 如果传入的是空列表，直接返回空列表，避免无效的 API 调用
    if not texts:
        return []

    # 如果环境变量里没有 API Key，抛出运行时错误，提示用户去 .env 里配置
    if not API_KEY:
        raise RuntimeError("SILICONFLOW_API_KEY missing in .env")

    # 拼接完整的 API 请求地址，例如 https://api.siliconflow.cn/v1/embeddings
    url = f"{EMBED_BASE_URL}/embeddings"

    # 设置 HTTP 请求头
    headers = {
        "Authorization": f"Bearer {API_KEY}",  # 使用 Bearer Token 方式传入 API Key
        "Content-Type": "application/json",    # 声明请求体是 JSON 格式
    }

    # 构建请求体（payload）
    payload = {
        "model": EMBED_MODEL,           # 指定使用的嵌入模型
        "input": texts,                 # 要处理的文本列表
        "encoding_format": "float",     # 指定返回的向量格式为浮点数
    }

    # 发送 POST 请求到 API，timeout=60 表示 60 秒后超时
    resp = requests.post(url, headers=headers, json=payload, timeout=60)

    # 如果 HTTP 状态码不是 2xx（比如 401 未授权、429 限流），这里会抛出异常
    resp.raise_for_status()

    # 解析响应 JSON，取出 "data" 字段，里面是包含 embedding 的列表
    data = resp.json()["data"]

    # API 可能不保证返回顺序与输入顺序一致，这里按 index 字段排序，确保顺序稳定
    data = sorted(data, key=lambda x: x["index"])

    # 从每个结果中提取 "embedding" 字段，返回一个嵌套列表（每个文本对应一个向量）
    return [item["embedding"] for item in data]


# 当这个文件被直接运行时（而不是作为模块导入时），执行下面的测试代码
if __name__ == "__main__":
    # 调用 embed_texts，传入一个测试文本，获取向量
    vecs = embed_texts(["openGauss B兼容模式如何用 Peewee 连接？"])

    # 打印向量数量和维度
    print("n_texts=1, dim=", len(vecs[0]))

    # 打印向量前 8 个值，保留 4 位小数，方便直观查看
    print("head=", [round(x, 4) for x in vecs[0][:8]])