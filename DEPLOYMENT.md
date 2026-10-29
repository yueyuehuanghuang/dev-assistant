# 运行与部署

支持 Python 3.12、单 worker；页面与 API 由同一 FastAPI 服务提供。无需 Node 构建前端。

## 本地运行

在项目目录创建虚拟环境，安装 `requirements.lock`，从 `.env.example` 创建 `.env`，填写模型配置。
不要覆盖已有 `.env`。Windows：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
```

macOS/Linux 使用 `.venv/bin/python` 运行相同模块。打开 http://127.0.0.1:8000/；`/health` 检查 Web 进程存活，不代表模型、MCP 或 GitHub 可用。
运行 `python mcp_client.py` 单独检查 MCP 握手。子进程使用启动后端的同一解释器，所以必须在该虚拟环境安装 MCP SDK。

## Docker Compose

本项目无用户认证，默认仅供本机或受保护的个人环境使用；所有访问者可读取同一份会话历史。当前文件不提供公网部署的身份与权限隔离。

```powershell
docker compose build
docker compose up -d
docker compose logs --tail 100
```

打开 http://127.0.0.1:8000/。镜像使用普通用户运行，Compose 将端口绑定宿主机 `127.0.0.1`，并把命名卷挂载到 `/app/.data`。
配置来自 `.env`，修改后执行 `docker compose up -d --force-recreate`。
执行 `docker compose down` 停止服务，保留命名卷。不要在希望保留历史时使用 `down -v`。

镜像安装锁定依赖。锁文件在 Windows/Python 3.12 上生成，`pywin32` 带 Windows 条件；Linux 镜像仍需在有 Docker Engine 的环境执行构建验证。
升级依赖后须重跑后端测试、MCP 检查、前端测试和评测，不直接跨到 MCP SDK 2.x。

## 数据备份与恢复

本地数据库位于 `.data/conversations.db`。先停止服务，再复制整个 `.data` 目录到备份位置；恢复时同样先停止，再把备份放回。
知识库还使用 `.data/knowledge.db`；启用知识库后必须同时备份这份数据库，不能只导出 conversations.db。向量服务和本地模型启动见 [KNOWLEDGE.md](KNOWLEDGE.md)。
最近 10 轮是活动窗口，完整成功轮次另外保存在 conversations.db。旧版本已删除的历史无法凭迁移恢复；完整历史目前不自动清理，需留意磁盘容量。详细机制见 [MEMORY.md](MEMORY.md)。

容器部署可先暂停服务，再导出数据库：

```powershell
docker compose stop
docker compose cp assistant:/app/.data/conversations.db ./conversations-backup.db
docker compose start
```

恢复时停止服务，使用 `docker compose cp ./conversations-backup.db assistant:/app/.data/conversations.db`，确认文件归运行用户所有后再启动。保留原备份以便回滚。

## 常见问题

- MCP 503：确认使用安装了锁定依赖的解释器；运行 `mcp_client.py` 排查。可临时设置 `TOOL_TRANSPORT=direct` 并重启对照。
- GitHub 401/403/429：检查 Token 有效性、目标仓库读取权限及额度；公开仓库可不配置 Token。
- 模型 503：补齐 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 后重启；502/504 检查提供方及网络。
- 数据库无法写入：确认 `.data` 或挂载卷归运行用户所有，且只运行一个 worker。
- 回答缺少 patch：GitHub 可能省略或截断 diff；以工具返回的缺失及截断标志为准，不代表没有代码变更。
# 完整架构部署

当前 Compose 包含 PostgreSQL、Milvus、etcd 和 MinIO，并默认启用多 Agent。请优先按 [ARCHITECTURE.md](ARCHITECTURE.md) 启动与迁移；原 SQLite 部署方式仍可用于兼容开发。

