# openGauss 配置问答助手（RAG + 单 Agent）
#
# 说明：
# - 镜像只装依赖与代码；语料入库结果（Chroma）与 .env 由 compose 挂载/注入
# - 不在镜像内固化任何密钥；.env 已在 .dockerignore 中排除
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖单独一层，源码改动不会触发重新装包
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 只复制运行所需内容（.dockerignore 排除 .venv/.env/向量库等）
COPY src/ ./src/
COPY personal/ ./personal/
COPY data/ ./data/

EXPOSE 8000

# 健康检查用标准库，不额外安装 curl
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

# 与本地运行完全一致的入口：api7(app) + --app-dir src
CMD ["uvicorn", "api7:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
