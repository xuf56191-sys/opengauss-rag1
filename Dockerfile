# openGauss 配置问答助手（RAG + 单 Agent）
#
# 说明：
# - 镜像只装依赖与代码；语料 Markdown 随镜像，向量库在首次启动时构建（见 docker-entrypoint.sh）
# - 不在镜像内固化任何密钥；.env 已在 .dockerignore 中排除，密钥用平台环境变量注入
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
COPY docker-entrypoint.py ./

EXPOSE 8000

# 健康检查用标准库，不额外安装 curl
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

# 入口脚本：缺索引则先入库，再启动（支持平台注入 $PORT）
ENTRYPOINT ["python", "/app/docker-entrypoint.py"]
