"""SiliconFlow embedding client. Keep this thin and hand-written."""
# 声明这是一个轻量级、手写的 SiliconFlow 嵌入客户端模块
# 模块顶部的 docstring，用于说明这个文件的作用

import os
import time
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


def embed_texts(texts: List[str], max_retries: int = 4) -> List[List[float]]:
    """Batch embed. Returns one vector per input string.

    网络偶发 SSLError/超时会自动退避重试，避免评测/入库中途断掉。
    """
    if not texts:
        return []

    if not API_KEY:
        raise RuntimeError("SILICONFLOW_API_KEY missing in .env")

    url = f"{EMBED_BASE_URL}/embeddings"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": EMBED_MODEL,
        "input": texts,
        "encoding_format": "float",
    }

    # trust_env=False：避免本机系统代理对 HTTPS API 的偶发干扰
    session = requests.Session()
    session.trust_env = False

    last_err = None
    for attempt in range(max_retries):
        try:
            resp = session.post(url, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda x: x["index"])
            return [item["embedding"] for item in data]
        except (requests.exceptions.SSLError, requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = e
            sleep_s = 1.5 * (attempt + 1)
            print(f"  embed retry {attempt + 1}/{max_retries} after {type(e).__name__}, sleep {sleep_s}s")
            time.sleep(sleep_s)
        except requests.exceptions.HTTPError:
            raise

    raise RuntimeError(f"embed failed after {max_retries} retries: {last_err}")


# 当这个文件被直接运行时（而不是作为模块导入时），执行下面的测试代码
if __name__ == "__main__":
    # 调用 embed_texts，传入一个测试文本，获取向量
    vecs = embed_texts(["openGauss B兼容模式如何用 Peewee 连接？"])

    # 打印向量数量和维度
    print("n_texts=1, dim=", len(vecs[0]))

    # 打印向量前 8 个值，保留 4 位小数，方便直观查看
    print("head=", [round(x, 4) for x in vecs[0][:8]])