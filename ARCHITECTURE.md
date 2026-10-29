# PostgreSQL、Milvus 与多 Agent

完整部署默认通过 `compose.yaml` 启用这三项能力。本机 `.env` 也已切换到完整模式。SQLite 与 Python 精确向量检索仍作为显式开发选项保留，服务失败不会自动回退。

## 启动与迁移

```powershell
docker compose up -d postgres milvus
.\.venv\Scripts\python.exe scripts/migrate.py upgrade
# 仅首次将原有 .data 两个 SQLite 数据库导入空 PostgreSQL：
.\.venv\Scripts\python.exe scripts/migrate.py import-sqlite --directory .data
.\start_local_embedding.ps1
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
```

PostgreSQL 在本机 54329 端口，Milvus 在 19539，均只映射到回环地址，避免与 DevFlow 默认端口冲突。Compose 使用本机已有的 PostgreSQL 16（pgvector 镜像）、Milvus 2.5.4、etcd 和 MinIO 镜像；向量检索实际使用 Milvus，未使用 pgvector。

完整容器应用使用 `docker compose up -d --build`。容器内地址由 Compose 覆盖。Embedding 若运行在宿主机，须让该服务监听容器可达地址，并设置 `DOCKER_EMBEDDING_BASE_URL`；当前默认的本机 Embedding 脚本只监听 127.0.0.1，推荐本地开发采用上面的宿主机 Python 启动方式。

`scripts/migrate.py` 按 `migrations/*.sql` 的版本顺序执行，记录 `schema_migrations`，使用事务及 advisory lock 防止并发升级。导入要求目标业务表为空；逐表计数并逐字段核对，失败整体回滚。原 SQLite 文件以只读模式访问，保留原 ID、历史、记忆和资料。迁移不删除原文件，也不会自动覆盖已有 PostgreSQL 内容。已有向量导入后标记为 stale，需要在知识库页面重新构建 Milvus 索引。

回退：停止应用，移除 `.env` 中 `DATABASE_URL`，设置 `VECTOR_BACKEND=local` 和 `AGENT_MODE=single`，即可读取原 SQLite 快照。切换后写入 PostgreSQL 的新数据不会自动反向同步，正式回退前应先导出/备份 PostgreSQL。不要用 `docker compose down -v` 清理持久卷。

## 数据与索引

`postgres.py` 复用现有参数绑定仓储接口。会话、原始轮次、摘要、记忆候选、审批事件、知识库、文档与片段统一进入 PostgreSQL。短事务通过数据库 advisory lock 串行化，以保留原 SQLite 写事务和记忆版本比较语义；当前应用仍使用单进程启动，不保证多个应用进程并发续写同一会话时的缓存一致性。

`vector_store.py` 使用官方 pymilvus 客户端建立 HNSW/COSINE 索引。Embedding 服务地址、模型、维度、查询指令与预处理版本共同确定 collection；每次构建使用新的 generation。向量全部写入并 flush 后，才在 PostgreSQL 内检查 revision 并切换 generation。查询同时过滤知识库 ID 和 generation，命中片段还须通过 PostgreSQL 的当前知识库与归档状态核验。维度、模型不符或索引过期时明确报错。混合检索沿用 BM25 与向量结果的等权 RRF（k=60）。

失败构建不会激活半成品向量，但可能留下未引用的 generation；当前保留它们以便诊断，尚无自动垃圾回收任务。应用在索引任务进行中意外退出后，需要管理员确认任务已停止，再将该库 `index_state` 从 `indexing` 改为 `error`，重新构建。后台索引仍是进程内任务，尚未引入外部任务队列。

## Agent 执行链

`AGENT_MODE=multi` 通过 `orchestration.py` 的 LangGraph `StateGraph` 执行。Planner、Orchestrator、四类专用 Agent 与 Synthesis 均是独立图节点，条件边根据计划依次调度。详细实现和边界见 [LANGGRAPH.md](LANGGRAPH.md)：

1. Planner 独立调用模型，输出经 Pydantic 校验的 JSON 任务计划，最多 3 个子任务；无需外部资料可输出空任务。
2. Code Agent 只能读取仓库、目录和源码；Issue Agent 只能读取仓库及 Issue；PR Agent 可读取 PR、diff 和源码；Knowledge Agent 可检索已绑定知识库、已批准记忆及历史。
3. 每个专用 Agent 独立执行工具循环，最多 8 次调用；顺序执行，整轮最多 24 次工具调用、最多 29 次模型请求。超出上下文预算即停止，不静默丢弃关键证据。
4. Synthesis Agent 接收专用报告、真实来源列表及有界工具证据，不再调用工具，汇总最终结论并保留不确定性。

所有角色共用当前会话的知识库/记忆范围及一轮 MCP 子进程。direct 模式共享 commit pins；MCP 模式复用子进程内 pins。角色工具权限由服务端过滤并执行校验。Planner 和综合 Agent 没有工具执行权限。异常和取消不提交半轮会话；实际工具链随最终回答一起归档，工具 ID 重新分配避免跨 Agent 冲突。前端流式状态展示当前角色，来源卡片仍仅提取成功工具结果。

## 验证

```powershell
$env:AGENT_MODE='single'
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
node --test tests/test_sse.mjs tests/test_markdown.mjs
.\.venv\Scripts\python.exe scripts/check_infrastructure.py
# 下项会调用已配置的真实聊天模型和 Embedding 服务：
.\.venv\Scripts\python.exe scripts/check_full_stack.py
```

基础设施验证使用真实 PostgreSQL/Milvus、确定性测试向量，创建独立 schema/collection 并清理。完整链路验证使用真实本地 Embedding、真实模型和合成文档，在临时 schema 内验证检索、分工、汇总、来源和归档，结束只删除该测试知识库的向量。

接口实现参考 [Psycopg 事务文档](https://www.psycopg.org/psycopg3/docs/basic/transactions.html) 和 [Milvus collection/index API](https://milvus.io/api-reference/pymilvus/v2.5.x/MilvusClient/Collections/create_collection.md)。
