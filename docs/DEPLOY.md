# Docker 部署指南（Windows）

> 目标：用 Docker 在**本机**跑起来，并让同一局域网/公网的人访问。
> 前置：Windows 10/11 + **Docker Desktop**（需要 WSL2 或 Hyper-V）。
> 本文所有命令都在项目根目录 `D:\PPT文档\RAG新项目\opengauss-rag` 执行。

---

## 0. 先确认 Docker 可用

```powershell
docker --version
docker compose version
docker run --rm hello-world
```

三条都正常再继续。若 `docker` 命令不存在 → 安装 Docker Desktop：
<https://www.docker.com/products/docker-desktop/> ，安装后重启，托盘图标显示 Engine running。

**注意**：安装 Docker Desktop 需要开启虚拟化（BIOS 里 Intel VT-x / AMD-V），公司电脑若被策略限制可能装不了。

---

## 1. 准备配置

```powershell
cd D:\PPT文档\RAG新项目\opengauss-rag

# 没有 .env 就先复制
if (-not (Test-Path .env)) { copy .env.example .env }
```

`.env` 里至少要有：

```text
SILICONFLOW_API_KEY=...
DEEPSEEK_API_KEY=...
API_ACCESS_KEY=...        # 可选：设了就要带 X-API-Key 才能调 /ask、/agent
```

`.env` 已被 `.gitignore` 与 `.dockerignore` 排除，**不会进镜像、不会进仓库**。

### 向量库两种方案

| 方案 | 做法 | 特点 |
|------|------|------|
| A. 宿主机先入库（默认） | `python src/ingest3.py` 生成 `./data/chroma`，compose 只读挂载 | 镜像启动快；语料更新要重新入库 |
| B. 容器内自动入库 | 删掉 `docker-compose.yml` 里 `volumes` 那两行 | 首次启动多 20~40s，多花十几次嵌入调用 |

---

## 2. 启动

> **两个必踩的坑（Windows + 国内网络）**
>
> 1. **拉基础镜像需要代理**：Docker Hub 直连不通，报 `Docker Desktop has no HTTPS proxy`。
>    设置：Docker Desktop → **Settings → Resources → Proxies → Manual**，HTTP/HTTPS 都填 `http://127.0.0.1:<你的代理端口>`（如 7897），Apply & restart。
>    **不需要**开 Windows 系统代理。
> 2. **构建阶段的 pip 不走 Docker 的代理**，会卡在 `pip install`。
>    已内置解决：`Dockerfile` 支持 `ARG PIP_INDEX_URL`，`docker-compose.yml` 默认用清华源。
>    海外环境用官方源：`$env:PIP_INDEX_URL="https://pypi.org/simple"; docker-compose build`。

```powershell
# 构建 + 启动（首次约 2~5 分钟，慢网可能 15 分钟以上）
docker compose up --build

# 想后台跑
docker compose up --build -d
```

看日志：

```powershell
docker compose logs -f api
```

正常应依次出现：

```text
[entrypoint] CHROMA_DIR=/app/data/chroma
[entrypoint] 已存在索引，跳过入库        （方案 A）
# 或 [entrypoint] 索引为空，开始入库...    （方案 B）
[entrypoint] exec: ... uvicorn api7:app ... --port 8000
INFO:     Application startup complete.
```

> **卷必须可写**：`docker-compose.yml` 里挂载不能加 `:ro`。
> Chroma 打开 sqlite 需要写权限；只读挂载会被入口脚本判定「不可用」并回退到 `/tmp/chroma`，导致每次启动都重新入库。

---

## 3. 验证

```powershell
# ① 探活（不需要 key）
Invoke-RestMethod http://127.0.0.1:8000/health

# ② 一次问答（若 .env 设了 API_ACCESS_KEY 就必须带头）
$headers = @{ "X-API-Key" = "<你的 API_ACCESS_KEY>" }
$body = @{ query = "enable_dolphin_proto 是干什么的？"; k = 3 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/ask `
  -ContentType "application/json" -Headers $headers -Body $body
```

浏览器也可以：<http://127.0.0.1:8000/docs> → `POST /ask` → Try it out。

**预期**：`/health` 返回 `chunks: 120`；`/ask` 返回 `answer` 带 `[1][2]`、`citation_valid: true`。

---

## 4. 让别人访问

### 4.1 同一局域网（最简单，稳定）

服务已监听 `0.0.0.0`。查你电脑的内网 IP：

```powershell
ipconfig | Select-String "IPv4"
```

别人访问 `http://<你的内网IP>:8000/docs`（例如 `http://192.168.1.23:8000/docs`）。
若连不上：检查 Windows 防火墙是否放行 8000，或临时放行：

```powershell
New-NetFirewallRule -DisplayName "opengauss-rag 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

### 4.2 公网（临时）

在容器跑着的前提下，用 SSH 反向隧道（免注册）：

```powershell
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -R 80:localhost:8000 nokey@localhost.run
```

窗口里会打印 `https://xxxx.lhr.life`，配合 `X-API-Key` 发给别人。**窗口不能关**，地址每次重连会变。
（仓库里的 `share.ps1` 是非容器版的一键脚本，逻辑相同。）

### 4.3 公网（长期）

需要云主机或支持 Docker 的平台：

- **有 VPS**：把仓库拉上去，`docker compose up -d`，用 Nginx/Caddy 反代 8000 并配 HTTPS
- **云平台**：Render 新账号要求绑卡做身份验证；Hugging Face Spaces 的 Docker SDK 目前对部分账号是付费档。
  以后若绑卡或升级，用同一份 `Dockerfile` 即可部署，无需改代码。

---

## 5. 常见问题

| 现象 | 原因 | 处理 |
|------|------|------|
| `port is already allocated` | 本机 8000 被占用 | 改 compose 的 `ports: "8001:8000"`，访问 8001 |
| 容器反复重启 | 启动命令报错 | `docker compose logs api` 看真实异常 |
| `/health` 返回 500 | 检索器没初始化（Chroma 缺失/不可读） | 方案 A 时确认 `./data/chroma` 有 5 个文件；或改方案 B |
| `入库失败` | Key 没传进容器 | `docker compose config` 确认 `env_file` 生效；检查 `.env` 拼写 |
| 401 | 设了 `API_ACCESS_KEY` 但没带 `X-API-Key` | 加请求头，或临时注释掉 `.env` 里那行 |
| 镜像太大/构建慢 | 依赖多（chromadb、langgraph） | 正常，首次拉 base 镜像较慢 |
| 内存不够（WSL 限制） | Docker Desktop 默认 2GB 左右 | Docker Desktop → Settings → Resources 调到 4GB+ |

---

## 6. 停止与清理

```powershell
docker compose down          # 停止并删除容器
docker compose down -v       # 连匿名卷一起删（本项目数据都在宿主 ./data，不受影响）
docker images | Select-String opengauss-rag
docker rmi opengauss-rag:0.1 # 删镜像
```

---

## 7. 一条命令的等价写法（不用 compose）

```powershell
docker build -t opengauss-rag:0.1 .

docker run -d --name rag -p 8000:8000 `
  --env-file .env `
  -e CHROMA_DIR=/app/data/chroma `
  -e PORT=8000 `
  -v "${PWD}\data\chroma:/app/data/chroma:ro" `
  opengauss-rag:0.1

docker logs -f rag
```

---

## 8. 交付状态说明（别对外夸大）

- 已在 **Windows 11 + WSL 2.7.14 + Docker Desktop 4.97** 上完成实测：
  - `docker-compose build` 成功（`Successfully tagged opengauss-rag:0.1`）
  - 容器 `Up (healthy)`，`GET /health` 返回 `chunks=120`
  - `POST /ask`：配置题返回带 `[1][2]` 的回答；天气题 `refused=true / high_vector_distance`
  - `POST /agent`：配置题走 `get_config_detail`（`steps=2`）；天气题 `refused=true / no_evidence`
- 生产化缺口：无账号体系（只有共享 `X-API-Key`）、限流是内存版单进程、无持久化配额、无压测与监控
- 部署过程中的三个真实坑（已沉淀进本文档）：Docker Hub 需代理、构建阶段 pip 需镜像源、Chroma 卷不能只读
