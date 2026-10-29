# Dev Assistant

从零实现一个面向 GitHub 仓库的 AI 开发助手，参考 DevFlow-AI 的业务方向，逐步学习实现。

## 当前版本与快速开始

当前完整模式已接入 PostgreSQL、Milvus 和 LangGraph 多 Agent 状态图。启动数据库、迁移与本地 Embedding 见 [ARCHITECTURE.md](ARCHITECTURE.md)；图节点、条件路由及测试见 [LANGGRAPH.md](LANGGRAPH.md)。`AGENT_MODE=multi` 启用图编排，`single` 使用原单 Agent 循环。

已完成第 1–13 步：多轮持久化、流式聊天、6 个 GitHub 只读工具、SDK stdio MCP 接入、固定任务评测、会话列表、基础 Markdown 和独立工具来源展示。
这是单用户、单进程的本地项目；尚未实现用户认证、多租户权限或公开服务所需的访问控制。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# 在 .env 填写模型配置；公开仓库 GITHUB_TOKEN 可留空
docker compose up -d postgres milvus
.\.venv\Scripts\python.exe scripts/migrate.py upgrade
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
```

打开 http://127.0.0.1:8000/。完整模式配置见 `.env.example`，工具传输默认 `mcp`；使用知识库向量检索前需要启动并配置 Embedding 服务。
依赖已锁定在 `requirements.lock`（Python 3.12 环境验证）；`requirements.txt` 表达允许的依赖范围。
部署及备份见 [DEPLOYMENT.md](DEPLOYMENT.md)，评测说明见 [evals/README.md](evals/README.md)。以下保留逐步学习记录。
本次检查的通过项及未验证范围见 [VALIDATION.md](VALIDATION.md)。

新增知识库：访问 `/knowledge` 创建、上传和测试 BM25/向量/混合检索，再在聊天中选择知识库问答。默认可复用本机 DevFlow-AI 的 Qwen 0.6B 模型；配置、启动及能力边界见 [KNOWLEDGE.md](KNOWLEDGE.md)。

新增记忆：访问 `/memory` 管理待确认/已确认记忆及完整历史。最近 10 轮之外的原文现在归档保留，提供上下文预算、摘录摘要、按项目/知识库隔离的长期记忆及历史找回工具，见 [MEMORY.md](MEMORY.md)。下方早期步骤中“仅保存最近 10 轮”的描述代表当时版本。

## 第一步：启动后端服务

当前已实现流式聊天、多轮 SQLite 会话、GitHub 仓库资料、源码、Issue 和 PR diff 工具。以下按实现步骤介绍；第一步先验证 FastAPI 服务和 `GET /health` 健康检查。

在本目录打开 PowerShell，依次运行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

访问 http://127.0.0.1:8000/health 应返回 `{"status":"ok"}`。

访问 http://127.0.0.1:8000/docs 可查看并调用接口。

## 第二步：接收聊天消息

在 `/docs` 中展开 `POST /chat`，点击 **Try it out**，提交：

```json
{"message": "介绍一下这个项目"}
```

返回：

```json
{"answer": "已收到你的消息：介绍一下这个项目"}
```

`ChatRequest` 定义请求体，`ChatResponse` 定义响应体。消息必须是字符串，
长度为 1～12000 个字符，不能只包含空白字符；首尾空白会被去除。
缺少消息或消息不合法时，FastAPI 返回 HTTP 422。

第二步的本地回复已在第三步替换为模型生成的回答。

## 第三步：接入大模型

安装新增依赖，并在尚未创建 `.env` 时复制配置模板：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

编辑 `.env`，填写 `LLM_API_KEY`、`LLM_BASE_URL` 和 `LLM_MODEL`。
这三项沿用 DevFlow-AI 的配置，初始本地配置已从 DevFlow-AI 复制；之后两份配置独立维护。
不要提交或分享密钥。

重启服务，然后在 `/docs` 中发送同样的 `POST /chat` 请求。
内容由模型生成，会产生 API 使用费用；第四步起响应还包含 `conversation_id`。

调用链路：`main.py` 校验输入 → `llm.generate_answer()` → 配置的模型服务 `/chat/completions` → 返回文本。
与 DevFlow-AI 一样使用 `httpx.AsyncClient` 发送 HTTP 请求。
`messages` 中的 `system` 是助手行为说明，`user` 是用户消息；
`choices[0].message.content` 是模型文本输出。
`async/await` 让服务在等待网络响应时可以处理其他请求。

缺少配置返回 503；限流或额度不足返回 429；超时返回 504；
连接失败、上游错误或空回复返回 502。不会向客户端直接暴露上游错误详情。

第六步开始加入仓库读取能力。

当前保留 DevFlow-AI 的基础文本调用方式，尚未迁移流式输出、JSON 模式和提示词缓存。

## 第四步：多轮对话

在 `/docs` 发送第一次请求，不填写 `conversation_id`（或填写 `null`）：

```json
{"message": "我正在学习 FastAPI，请简短回答。"}
```

响应包含 `answer` 和新生成的 `conversation_id`。复制这个 ID，发送下一次请求：

```json
{"message": "我刚才说在学什么？", "conversation_id": "替换为上次返回的ID"}
```

调用链路：`main.py` → `memory.py` 查找会话、组织历史 →
`llm.py` 添加系统提示词并请求模型 → `memory.py` 保存本轮 → 返回回答和 ID。

- 不传 ID 会创建独立会话，不同会话不共享历史。
- 保存最近 10 个完整轮次；请求时发送这些历史和当前用户消息。
- 模型失败不会写入本轮消息；同一会话并发请求按顺序处理。
- 格式不合法的 ID 返回 422；合法但不存在的 ID 返回 404。
- 第四步最初使用内存存储；第五步起默认使用 SQLite，重启后仍可恢复最近 10 轮。
- 尚未接入用户认证，会话 ID 用于查找历史，不能替代用户权限校验。

离线测试（不调用付费模型）：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 第五步：SQLite 持久化

不需要安装数据库服务或新增依赖。服务自动创建 `.data/conversations.db`，
该目录已加入 `.gitignore`，不会提交聊天内容。

职责划分：`main.py` 处理接口 → `memory.py` 管理对话 →
`storage.py` 读写 SQLite；`llm.py` 的模型调用不变。

`conversations` 表包括：`id`（会话 ID）、`messages`（JSON 消息数组）、
`updated_at`（最近写入时间）。每次模型成功回答后，原子写入最近 10 个完整轮次。
尚未保存完整无限历史，也没有历史摘要。

验证方法：

1. 在 `/chat` 告诉助手一个信息，保存返回的 `conversation_id`。
2. 停止服务，再启动。
3. 携带相同 ID 询问刚才的信息，应能继续对话。

旧内存版服务中尚未落盘的会话不会自动迁移；更新后请创建一个新会话再测试。
数据库提交失败返回 503，本轮不会更新到内存。应用仍按单进程设计，
请不要启用多个 worker；当前小规模本地版本使用同步 SQLite 短事务。

## 第六步：GitHub 只读接入

沿用 DevFlow-AI 的 `httpx` + GitHub REST API 调用方式，新增 `github.py`。
公开仓库无需 Token；可在 `.env` 配置可选的 `GITHUB_TOKEN`，修改后重启。
仅需读取目标仓库的权限，不需要写权限。不会自动复制其他项目的 GitHub Token。

先在 `/docs` 调用 `GET /github/repos/{owner}/{repo}`，例如
`/github/repos/fastapi/fastapi`。接口返回基本信息、默认分支根目录以及 README。
根目录最多保留 100 项，README 最多保留 12000 字符，截断情况由布尔字段标记。
README 缺失时返回 `null`，不会凭空补全。

然后调用 `POST /chat`：

```json
{
  "message": "请根据仓库资料介绍项目用途和根目录结构，标明哪些结论只是推测。",
  "repository": "fastapi/fastapi"
}
```

后端先读取 GitHub，再把资料与问题一起交给模型，成功后写入会话数据库。
携带返回的 `conversation_id` 可以继续提问；不填写 `repository` 时不会重新请求
GitHub，而是使用会话中尚保留的资料快照。需要刷新时再次填写 `repository`。
快照会随最近 10 轮的历史截断而移除。

当前只支持 `owner/repo` 格式，不接受完整 URL；只读取 GitHub.com。
没有递归读取源码、克隆仓库、执行仓库代码或修改 GitHub。
本阶段由请求显式指定仓库；第七步起也支持模型自主选择工具。

GitHub 错误与模型错误分开提示；仓库不存在/无权限返回 404，
Token 无效返回 401，访问拒绝返回 403，限流返回 429，超时返回 504。

接口参考：https://docs.github.com/en/rest/repos/contents

## 第七步：模型自主调用工具

现在可以不填写 `repository`，直接发送：

```json
{"message": "请读取 fastapi/fastapi，介绍用途和根目录结构，附上来源链接。"}
```

`main.py` 调用 `agent.run_agent()`，其执行流程为：

1. `llm.request_completion()` 将历史消息和工具定义一起发送给模型。
2. 模型可直接回答，也可返回 `tool_calls`。
3. `agent.py` 校验工具名和 JSON 参数，第七步仅允许 `fetch_repository`，第九步扩展为三个只读工具。
4. 后端执行 GitHub 读取，将结果通过 `role=tool`、匹配的 `tool_call_id` 返回模型。
5. 再次请求模型，直到获得最终文本回答。
6. `memory.py` 将本轮用户消息、工具请求、工具结果和回答整体写入 SQLite。

第九步起每轮最多处理 8 次工具调用，最多请求模型 9 次；达到次数限制后使用
`tool_choice=none` 请求最终回答。如果模型仍要求工具，返回 502，本轮不落盘。
未知工具、非法参数和 GitHub 访问错误会作为结构化工具错误交给模型，
不会执行未知函数或把错误伪装成有效资料。

工具协议错误、空回复和模型请求失败也不会保存不完整轮次。
历史仍保留最近 10 轮，但现在按用户轮次截断，避免遗留孤立的工具结果。
为兼容思考模型的工具续接，保留模型返回的 `reasoning_content`，不在聊天响应中显示。

普通问题如“解释 Python 列表”无需 GitHub；是否调用由模型选择。
原有 `repository` 字段和独立 GET 仓库接口仍可使用。
第七步最初只有仓库资料工具；第九步新增目录和源码工具，仍没有执行代码或写入 GitHub 的能力。

工具调用参考：https://api-docs.deepseek.com/guides/tool_calls/

## 第八步：流式聊天页面

启动方式不变，打开 http://127.0.0.1:8000/ 即可使用聊天页面。
本阶段使用原生 HTML/CSS/JavaScript，无需安装 Node 或运行独立前端服务。

页面支持：逐片段显示模型回答、工具执行状态、停止接收、新建会话和刷新恢复。
浏览器仅在本地保存最近一次成功会话的 ID；历史消息由后端 SQLite 提供。
第八步最初以纯文本显示；第十三步加入安全的基础 Markdown 渲染。模型内部推理内容不在页面显示。

新增接口：

| 接口 | 用途 |
| --- | --- |
| `POST /chat/stream` | 与 `/chat` 相同的 JSON 输入，返回 SSE 事件流 |
| `GET /conversations/{conversation_id}` | 返回最近保存的用户消息和最终回答 |

SSE 事件包括 `status`、`tool_start`、`tool_end`、`delta`、`done`、`error`。
`delta` 来自模型服务的真实流式片段，不是把完整答案拆字后模拟。
每次模型调用开始时，`status.reset_answer=true` 表示清除上一轮调用的临时文本；
`done` 携带完整答案和会话 ID，仅在数据库提交成功后发送。
长时间等待时每 10 秒发送一次心跳注释。

流开始前的参数/会话错误使用 HTTP 422/404；流开始后的错误通过 `error` 事件报告，
此时 HTTP 响应头已经发出，不能再更改为错误状态码。前端需检查终止事件，
不能仅凭 HTTP 200 判断成功。断流、上游长度截断或模型失败不会保存半轮对话。

点击停止会取消连接及仍在进行的模型/工具任务。如果刚好在保存完成后停止，
该轮可能已经落盘，可刷新确认。新建会话不会删除旧会话。

新增文件：`streaming.py`（SSE 接口）、`schemas.py`（共享输入输出模型）、
`static/`（聊天页面和浏览器事件流解析）。原有 `/chat` 接口继续可用。

后端离线测试照旧；若已安装 Node，还可验证浏览器 SSE 解析器：

```powershell
node --test tests/test_sse.mjs
node --check static/app.mjs
```

上游流式协议参考：https://api-docs.deepseek.com/api/create-chat-completion/

## 第九步：目录与源码读取

新增 `source_tools.py`，Agent 可自主调用两个只读工具；非流式和流式聊天均可使用，无需更改启动方式。

| 工具 | 参数 | 用途 |
| --- | --- | --- |
| `list_directory` | `repository`、可选 `path`（根目录为空字符串）、可选 `ref` | 查看指定目录的名称、路径、类型 |
| `read_file` | `repository`、`path`、可选 `ref`、`start_line`、`line_count` | 按行读取 UTF-8 源码，返回行号与来源链接 |

首次源码工具调用将 `ref`（省略时为默认分支 HEAD）解析为不可变 commit。
同一轮、同一仓库的后续源码读取复用该 commit，后续应省略 `ref`；中途切换版本返回工具错误。
下一轮重新解析，因此可读取最新版本。原有 `fetch_repository` 和请求中的 `repository` 资料快照仍使用原来的读取方式，不保证与本轮源码同版本；分析实现时以源码工具返回的 commit 为准。

限制与返回信息：

- 路径必须为仓库相对路径，拒绝绝对路径、反斜杠、空路径段及 `.`、`..`；文件路径不能为空。
- 目录最多展示 100 项，达到上限保守标记 `truncated`；GitHub Contents API 单目录最多返回 1000 项，不提供完整大目录分页。请继续读取具体子目录。
- 文件上限 200000 字节，仅支持 UTF-8 普通文本，不跟随下载链接，不执行源码。
- 默认从第 1 行读取 120 行，每次最多 200 行、16000 字符。仅返回完整行；单行超长、二进制、超大文件或越界起始行会明确报错。
- 返回 `commit`、`total_lines`、`start_line`、`end_line`、`truncated`、`next_start_line` 和固定到 commit 的文件行号链接。部分读取会标记截断；按 `next_start_line` 继续读取。
- 提示词要求根据实际源码回答并引用行号链接；这是模型行为要求，尚无答案引用的自动核验。

在聊天页面输入，例如：

```text
请读取 fastapi/fastapi 的 fastapi/applications.py，解释 FastAPI 类的用途，引用源码链接和行号。看不到的部分请说明。
```

也可以逐步定位：先询问某个仓库的目录，再要求读取指定文件；工具状态显示在现有聊天页面中。源码内容和工具记录沿用整轮 SQLite 持久化。

验证继续使用 `python -m unittest discover -s tests -v`；新增离线测试覆盖源码分页和链接、大小与类型限制、非法路径、目录截断、每轮版本固定与重置，不调用付费模型或真实 GitHub。

## 第十步：Issue、PR 与 diff 分析

GitHub 请求、Token 注入、超时、限流和错误映射统一由 `github.py` 的 `github_json()` 处理。
`fetch_repository()` 和 `source_tools.py` 已复用该入口；新增的 `collaboration_tools.py` 也使用它，不再各写一套 HTTP 调用。
只有仓库概览的空根目录和缺失 README 允许将 404 解释为无内容；其他工具的 404 会如实返回错误。

新增三个工具，继续通过 `/chat` 或 `/chat/stream` 调用，不需要新的启动参数或依赖：

| 工具 | 参数 | 返回内容 |
| --- | --- | --- |
| `fetch_issue` | `repository`、正整数 `number` | 标题、正文、状态、作者、更新时间及来源链接 |
| `fetch_pull_request` | `repository`、正整数 `number` | PR 说明、合并/草稿状态、变更统计、base/head SHA 及仓库 |
| `read_pr_diff` | `repository`、正整数 `number`、可选 `page` | 每页 20 个文件、变更类型、重命名前路径、增删统计和 patch |

使用方式（替换为目标仓库中真实存在的编号）：

```text
请读取 owner/repo 的 Issue #123，概括问题、复现条件和缺失的信息，附上来源。
请分析 owner/repo 的 PR #456，读取说明与 diff，解释改动、潜在风险和建议补充的测试。
```

边界与版本处理：

- Issue/PR 正文最多 12000 字符，返回 `body_truncated`；不包含评论、Review、CI 日志，也不自动列举或搜索编号。
- GitHub 的 Issue 接口也能返回 PR，工具会通过 `kind` 区分；需要 diff 时继续使用 PR 工具。
- diff 每文件最多 6000 字符、每页 patch 合计最多 24000 字符；返回 `patch_available` 和 `patch_truncated`。二进制或上游省略 patch 时不伪造内容，也不能将缺失 patch 视为没有改动。
- 使用 `next_page` 继续读取，最多 150 页/3000 文件；`truncated` 标记当前结果是否只覆盖部分文件或 patch。GitHub 本身也可能截断 patch，因此即使本地未截断，也不承诺完整。
- 每次 diff 读取前后核对 base/head SHA；同轮 PR 详情及分页读取也检查 SHA 一致性，变化返回 409 工具错误。下一轮可以重新读取；这不是事务快照，PR 描述及其他元数据仍可能更新。
- PR 来自 fork 时返回 `head_repository`。进一步读取新增源码应使用 head 仓库和 SHA，不能直接分析默认分支；删除源码使用 base 仓库和 SHA。源码工具仍受同轮单仓库单版本约束。
- 仅执行 GitHub GET 请求，所有分析建议由模型根据返回资料生成，不提交评论、修改 Issue 或合并 PR。工具错误沿用现有结构化反馈及整轮持久化规则。
- 私有仓库需为 Token 配置对应仓库的 Contents、Issues、Pull requests 读取权限；不需要写权限。

离线验证：`python -m unittest discover -s tests -v`。新增测试覆盖共享错误处理、正文截断、Issue/PR 区分、fork 版本、diff 分页与预算、缺失 patch、版本变化、参数校验和流式工具事件。

接口参考：[GitHub Issues API](https://docs.github.com/en/rest/issues/issues#get-an-issue)、[GitHub Pull Requests API](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests-files)。

## 第十一步：MCP 工具接入

```text
聊天接口 → Agent 模型工具调用 → MCP Client（官方 Python SDK）
    → stdio 子进程 mcp_server.py → tool_runtime.py → GitHub API
```

- `tool_runtime.py` 统一工具定义、参数校验、业务分发及版本固定；Agent 和 MCP Server 共用，不重复实现业务。
- `mcp_server.py` 使用官方 SDK 注册 `tools/list` 和 `tools/call`，提供全部 6 个只读工具；stdout 仅用于 MCP 协议。
- `mcp_client.py` 执行初始化握手、工具发现，将服务器的 `inputSchema` 转换为模型工具定义，再发送工具调用。
- 每个聊天轮次建立一次 MCP 连接和子进程，轮内复用，完成、失败或取消时关闭；版本固定范围即该连接。外部 MCP 客户端若长时间复用连接，版本固定也会持续，切换版本需重连。
- 子进程通过显式环境变量接收 GitHub Token，不转发模型密钥；SDK 保留其运行所需的默认系统环境变量。Server 不读取项目 `.env`，由启动它的客户端负责注入 GitHub Token。
- SDK 负责协议和进程通信，业务错误返回结构化工具结果；连接失败返回 503，不自动降级成直接调用。
- 配置 `TOOL_TRANSPORT=direct` 并重启，可切回同进程执行便于排错；默认 `mcp`。不支持从聊天内容指定任意服务器或执行命令。

只验证 MCP 握手和工具发现（不调用模型或 GitHub）：

```powershell
.\.venv\Scripts\python.exe mcp_client.py
```

其他客户端可以将本项目解释器作为 command、`mcp_server.py` 的绝对路径作为 args，通过 stdio 复用此服务，并按需配置 `GITHUB_TOKEN`。当前采用 SDK 1.x 维护分支并显式限制 `<2`，避免跨主版本 API 变更。

协议实现参考：[官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)、[MCP stdio 规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#stdio)。

## 第十二步：固定任务评测

`evals/cases.json` 包含 8 个合成任务：普通问答、缺少仓库的澄清、仓库概览、源码、Issue、PR diff、读取失败及仓库内容中的恶意指令。

```powershell
# 免费、离线：固定模型响应回放，验证评测和 Agent 流程
.\.venv\Scripts\python.exe evaluate.py
# 调用已配置模型，工具返回仍为固定样例，会产生 API 费用
.\.venv\Scripts\python.exe evaluate.py --live
```

报告写入 `output/eval-replay.json` 或 `output/eval-live.json`，失败时退出码为 1。
记录工具名与完整参数匹配的 precision/recall、答案关键词、必要引用与引用是否来自成功工具结果。
回放报告明确标记 `model_evaluated=false`；只有 `--live` 才评测实际模型。两种模式都不向 GitHub 发请求。
这些规则适用于小型回归集，不等于语义正确性、真实 GitHub 集成或全面防注入能力，详见评测说明。

## 第十三步：会话、阅读体验与部署

- 页面提供历史会话列表，按最近更新时间排序，支持分页及切换；重启后从 SQLite 恢复。记忆模块更新后，标题优先取归档首轮用户消息，最多 60 字。
- 新增 `GET /conversations?limit=20&offset=0`，返回 `conversations` 和 `next_offset`。会话详情及 SSE `done` 增加 `sources`。
- 回答完成后渲染标题、列表、粗体、行内代码、代码块和 Markdown 链接；流式阶段显示文本。采用 DOM 节点和 `textContent`，不执行原始 HTML，不支持表格、图片、嵌套 Markdown 或语法高亮。
- 回答下方展示“本轮工具来源”，独立于模型生成的引用，从本轮成功工具结果的结构化 URL 提取，仅允许 HTTPS GitHub 链接，最多 30 项。它表示读取来源，不代表答案中的每个结论已被自动核验。
- 提供 `Dockerfile`、`compose.yaml`、锁定依赖和备份说明。Compose 将端口仅映射到宿主机 localhost，SQLite 使用命名卷；不在镜像中包含 `.env`、聊天数据库或虚拟环境。

验证：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test tests/test_sse.mjs tests/test_markdown.mjs
node --check static/app.mjs
.\.venv\Scripts\python.exe evaluate.py
```

MCP 测试包括实际 SDK 握手、工具发现、调用、非法参数和取消；GitHub 业务返回使用合成 fixture。
`.github/workflows/ci.yml` 提供同样的离线检查流程，不需要模型或 GitHub 密钥；发布到 GitHub 后才会运行。
如需人工检查页面，可运行 `python tests/ui_preview.py` 并打开 http://127.0.0.1:8767/，使用临时合成历史，不调用模型；关闭服务即清理临时数据库。
# 存储与 Agent 架构更新

已加入 PostgreSQL 版本化迁移、Milvus HNSW 向量索引，以及 Planner → 专用 Agent → 综合 Agent 编排。完整启动、旧数据导入、验证和当前限制见 [ARCHITECTURE.md](ARCHITECTURE.md)。下方涉及 SQLite、单 Agent 的说明适用于兼容开发模式。

