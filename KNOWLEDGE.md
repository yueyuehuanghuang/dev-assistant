# 知识库使用与实现

当前支持：创建知识库 → 上传与去重 → 段落切片 → 本地/远程 Embedding → 向量索引 → BM25/向量/混合检索 → Agent 问答与来源。
资料与向量保存在 `.data/knowledge.db`，会话的知识库绑定保存在 `.data/conversations.db`。部署仍限单用户、单 worker。

## 当前机器：复用 DevFlow-AI 本地 Qwen

已确认 DevFlow-AI 的真实配置使用本地 `Qwen/Qwen3-Embedding-0.6B`、1024 维、CPU；模型位于相邻项目的 `.data/models/Qwen3-Embedding-0.6B`，推理依赖在其 backend 虚拟环境。
本项目 `.env` 已配置为使用该模型，无需重新下载。现有聊天密钥与 GitHub 配置保持原值。

启动本地向量服务：

```powershell
.\start_local_embedding.ps1
```

脚本优先使用当前虚拟环境；若未安装 SentenceTransformers，会查找相邻 DevFlow-AI 已安装依赖的虚拟环境。它只读取该环境，不安装包、不修改 DevFlow-AI 配置或模型。
服务以隐藏后台进程运行，仅监听 `127.0.0.1:8011`；PID 与日志写入 `.data/local-embedding.*`。访问 http://127.0.0.1:8011/health 检查模型是否加载完成。
也可在终端前台运行，便于 Ctrl+C 停止：

```powershell
..\DevFlow-AI\backend\.venv\Scripts\python.exe -m uvicorn local_embedding_server:app --host 127.0.0.1 --port 8011 --workers 1
```

然后在另一个终端启动或重启主应用：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
```

本地适配器只加载已有模型目录，`local_files_only=True`、不信任远程模型代码，不自动下载或回退到伪向量。
权重在向量服务启动时加载一次，后续请求复用；它与每轮启动的 GitHub MCP 工具进程是不同的服务。
Qwen 查询端使用配置的英文检索指令前缀，文档端不加查询前缀。

## 页面操作

1. 打开 http://127.0.0.1:8000/knowledge，创建一个知识库。
2. 上传 UTF-8 编码的 `.txt`、`.md`、`.markdown`、`.log`、`.json` 或 `.csv`。当前不解析 PDF、DOCX、图片或压缩包；JSON/CSV 按文本切片。
3. 点击“构建 / 更新向量索引”，等待状态变为就绪；页面自动轮询，离开页面不会主动取消后台任务。服务重启会标记中断，不伪装完成。
4. 在检索测试区选择“混合检索”“仅向量”或“仅关键词”，核对召回片段及来源。
5. 点击“用于新对话”，回到聊天提问；也可在聊天输入框上方选择知识库。
6. 会话首次成功保存后固定知识库。切换知识库需新建会话，避免旧文档历史混入另一知识库；刷新和重启后绑定仍保留。

归档文档是可恢复操作：归档后不进入新的检索，片段直链返回 404，恢复后可再次检索；已有会话中的历史工具结果不会被追溯删除。
新增、归档、恢复会使向量索引失效；必须更新索引后才能继续混合/向量检索。纯关键词检索可立即使用。

## 配置

本地默认示例（`LOCAL_EMBEDDING_MODEL` 必须填写本机真实路径）：

```dotenv
EMBEDDING_BASE_URL=http://127.0.0.1:8011/v1
EMBEDDING_API_KEY=
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
EMBEDDING_DIMENSIONS=1024
EMBEDDING_SEND_DIMENSIONS=false
EMBEDDING_QUERY_INSTRUCTION=Given a web search query, retrieve relevant passages that answer the query
EMBEDDING_MIN_SIMILARITY=0.35
LOCAL_EMBEDDING_MODEL=D:/path/to/Qwen3-Embedding-0.6B
LOCAL_EMBEDDING_DEVICE=cpu
```

独立安装本地推理环境时，可使用 `requirements-local-embedding.txt`；其中 PyTorch 等依赖体积较大。主后端只安装 `requirements.lock`，不需要安装 PyTorch。
使用远程兼容服务时替换 BASE_URL、MODEL、DIMENSIONS 和独立 API_KEY，并依据模型文档选择是否发送 dimensions 与查询指令。BASE_URL 不含 `/embeddings` 后缀，远程地址必须为 HTTPS，不允许 URL 内携带用户名、密码或查询参数密钥。
Embedding 配置不会自动复用聊天模型、聊天 API URL 或密钥。

改动模型、维度、服务端点或查询指令后必须重建索引。相同名称模型在服务商端发生静默权重变更无法自动识别，需运维方主动重建。
配置完整不代表服务已经连通；状态就绪也不等于检索质量达标。阈值 0.35 是可校准起点，不是通用准确率阈值。

## 存储、检索与一致性

- 单文件最大 1,000,000 字节；表单在进入上传解析前有总大小限制。文件名仅为标签，永远不作为磁盘路径。
- 统一 UTF-8、BOM 与换行后计算 SHA-256，在同一知识库内去重。归档的相同文件不会自动恢复，需显式点击恢复。
- 片段目标长度最多 1000 字符，优先换行边界，重叠约 160 字符；保存行号和字符偏移。长单行可能拆成多个片段，因此行号相同但偏移不同。
- 每个知识库最多 100 份文档、2000 个片段，包含归档；知识库列表展示最近 200 个。当前未提供永久清理或大规模索引功能。
- BM25 使用英文单词/标识符和中文双字词；向量以单位化后的余弦相似度检索，混合模式用 RRF 合并关键词前 8 个与向量前 30 个候选，返回最多 8 条。
- 小规模向量存入 SQLite JSON，在后端进行精确余弦计算；无需 Milvus。这不是近似向量索引，规模扩大后需迁移专用检索后端。
- 向量接口每批最多 16 个片段，校验返回数量、index 顺序、维度、零向量及 NaN/Inf。不截断或补齐错误向量。
- 索引任务先生成完整向量，再在事务内替换；失败保留旧向量，但状态标记错误，混合检索拒绝把它当作就绪索引。
- 文档修订号、索引修订号和 Embedding 合同必须一致。构建或检索期间文档变化返回冲突，不混用新旧片段。
- 远程 API 配置缺失、服务断开或索引未就绪会明确报错。只有用户显式选择“仅关键词”才运行不依赖 Embedding 的 BM25，不静默降级。

## Agent 与来源

GitHub 的六个工具仍通过 MCP；`search_knowledge` 在主后端执行，只在会话绑定知识库时提供给模型。
模型仅能提交 `query`、`top_k`，不能选择其他知识库、数据库路径、服务地址或模型。当前是单用户数据隔离边界，并不替代登录和权限校验。
检索内容是不可信资料，提示词要求忽略其中的行为指令、无命中时说明证据不足，并引用返回来源。
来源链接为 `/knowledge/{kb_id}/chunks/{chunk_id}`，可阅读实际片段及行号；页面以安全文本显示，HTML 不执行。
这些约束与来源展示不等于对每个回答结论自动核验，也不提供完整的防注入保证。

## API

| 方法与路径 | 用途 |
| --- | --- |
| `POST /api/knowledge` | 创建，JSON `{"name":"项目资料"}` |
| `GET /api/knowledge` | 列表、文档/片段统计、Embedding 配置状态 |
| `GET /api/knowledge/{id}` | 索引状态、修订号及错误 |
| `POST /api/knowledge/{id}/documents` | multipart `file` 上传 |
| `GET /api/knowledge/{id}/documents` | 文档列表，包含归档状态 |
| `POST /api/knowledge/{id}/documents/{doc}/archive` | 归档 |
| `POST /api/knowledge/{id}/documents/{doc}/restore` | 恢复 |
| `POST /api/knowledge/{id}/reindex` | 提交索引任务，返回 202；轮询知识库状态 |
| `POST /api/knowledge/{id}/search` | JSON `{"query":"有效期","mode":"hybrid","top_k":5}` |
| `GET /api/knowledge/{id}/chunks/{chunk}` | 片段 JSON |
| `GET /knowledge/{id}/chunks/{chunk}` | 安全的来源阅读页 |

`POST /chat`、`POST /chat/stream` 增加可选 UUID `knowledge_base_id`；后续会话可省略，服务端恢复原绑定。

## 验证与部署限制

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test tests/test_sse.mjs tests/test_markdown.mjs
node --check static/knowledge.mjs
# 本地真实向量验收，临时合成文档，不改变用户资料
.\.venv\Scripts\python.exe scripts/check_knowledge.py
# 加一次真实聊天模型问答，会产生聊天 API 费用
.\.venv\Scripts\python.exe scripts/check_knowledge.py --ask
```

验收报告写入 `output/knowledge-smoke.json`，分别报告召回、首位排序、回答与来源。两份合成文档不代表全面检索准确率。
本机已验证真实 Qwen 向量和聊天模型闭环；含糊问题仍可能把无关片段排在前面，需要结合检索测试校准，后续可增加重排模型和更大评测集。

备份需停止主应用及索引任务后复制整个 `.data`，同时包含两份数据库。模型目录单独备份；不要只复制 conversations.db。
现有 Docker 镜像只包含主后端，不包含本地模型与 PyTorch；容器内 `127.0.0.1:8011` 不指向宿主机。使用 Docker 时需另行配置可达的 Embedding 服务，不能直接把本机这份 localhost 配置当作容器方案已验证。

参考：[Qwen 模型及查询指令](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)、[兼容 Embedding API 示例](https://www.alibabacloud.com/help/en/model-studio/embedding)。
