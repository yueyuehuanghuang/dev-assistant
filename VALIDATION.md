# 验证记录

## 2026-10-27：记忆分层完善

- 后端全量测试 **92/92** 通过，前端测试 **8/8** 通过；新增记忆及聊天 JS 语法检查、依赖检查通过。
- 参考 DevFlow-AI 的历史存储、上下文压缩、MemoryCandidate 确认与召回设计，实现 SQLite 原始轮次归档、完整轮次预算裁剪、有上限的摘录摘要、用户确认后的长期记忆，以及三个只读记忆工具。
- 新增测试覆盖迁移、跨十轮归档、重启恢复、项目/知识库隔离、并发 ContextVar 隔离、确认/替换/撤回/重提版本控制、数据库失败原子回滚、上下文预算和来源安全。
- 离线记忆验收通过，报告 `output/memory-smoke-replay.json`；原有固定任务回放仍为 8/8。
- 真实模型验收通过，报告 `output/memory-smoke-live.json`：同项目新会话召回已确认 Python 3.12 约定；原会话累计 15 轮、活动窗口 10 轮，仍通过 search_history/read_history 找回第 1 轮标记并引用原文。
- 验收种子及确认操作仅针对临时合成数据库，不写入用户实际长期记忆；报告中的来源 URL 随临时数据库清理后不再可访问。
- 摘要是规则化原文摘录，不是模型抽象总结；长期记忆/历史为关键词检索，不是向量记忆。旧版本已删除的原文不能恢复，当前未提供自动永久清理。
- 新增 `/memory` 页面尚未完成浏览器视觉验收，当前仍没有可用浏览器；未扩大 Docker 构建验证范围。

使用方法及范围规则见 [MEMORY.md](MEMORY.md)。

## 2026-10-27：知识库与本地向量检索

- 后端全量测试 **79/79** 通过，前端测试 **7/7** 通过；聊天与知识库 JS 语法检查、依赖检查通过。
- 新增知识库测试覆盖上传限制、去重、片段偏移、中文关键词、跨库隔离、会话绑定持久化、混合检索、索引失败及修订冲突、Embedding 协议与本地适配器输入校验。
- 复用本机 DevFlow-AI 已下载的 **Qwen/Qwen3-Embedding-0.6B**，CPU、1024 维；没有重新下载模型或改动 DevFlow-AI 的依赖和配置。
- 实际本地服务 `127.0.0.1:8011` 已通过健康检查和真实向量请求。主后端 `.env` 已填写本地模型连接与路径，原聊天/GitHub 配置保持原值。
- 真实检索使用两份临时合成文档与两个没有关键词命中的问法：关键词命中 0，向量/混合检索正确资料召回 **2/2**；首位排序正确 **1/2**。这是有限样例，不是通用召回率或准确率。
- 真实聊天模型在召回证据后回答“15 分钟”并给出知识片段来源；完整记录见 `output/knowledge-smoke.json`。临时知识库已清理，报告里的片段 URL 仅为当时执行记录，不能作为当前可访问的资料链接。
- 初次检索脚本把“首位必须正确”作为成功条件而失败，该记录保留为 `output/knowledge-smoke-initial.json`。最终报告明确分开召回与首位排序，不隐藏排序不足。
- 浏览器仍不可用，因此新增知识库页面尚未做人工视觉验收；上传、检索、索引和普通/流式聊天均已做接口及自动测试。
- Docker Engine 仍未启动，未执行包含知识库的镜像构建；当前 Docker 主镜像不包含本地模型适配器和 PyTorch。

使用方式及完整能力边界见 [KNOWLEDGE.md](KNOWLEDGE.md)。

## 2026-10-26：基础项目

日期：2026-10-26（Asia/Shanghai）。环境：Windows、Python 3.12，使用项目 `.venv` 和锁定依赖。

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| 后端 unittest | 57/57 通过 | 对话、SQLite、GitHub fixture、源码、Issue/PR、SSE、来源和评测器 |
| 前端 Node 测试 | 6/6 通过 | SSE、基础 Markdown、安全 DOM 渲染、链接协议 |
| JS 语法检查 | 通过 | `static/app.mjs` |
| `pip check` | 通过 | 已安装依赖无冲突 |
| SDK MCP 握手/发现 | 通过 | 真实 stdio 子进程，发现 6 个工具 |
| SDK MCP 调用/取消 | 通过 | 合成业务数据，实际协议与子进程生命周期 |
| 离线评测回放 | 8/8 通过 | 固定响应验证流程，不代表模型质量 |
| 最近一轮真实模型评测 | 修正评分器后 7/8 | 实际模型，工具返回固定样例 |
| Compose 配置 | 通过 | 配置格式校验，不启动容器 |
| Docker 镜像构建 | 未验证 | 本机 Docker Engine 未启动 |
| 浏览器视觉验收 | 未验证 | 当前会话没有可用浏览器，已完成 DOM 单元测试 |

## 模型评测结果

运行了三轮真实模型评测。过程中修正了默认参数归一化、源码行引用范围和 Markdown URL 标签解析，并补齐了仓库 fixture 的字段。
首轮与第二轮报告保存在 `output/eval-live-initial.json`、`output/eval-live-second.json`。

最终一轮原始记录为 `output/eval-live.json`：原始工具调用和模型回答保留不变。
随后只修正 URL 标签解析，运行 `python evaluate.py --rescore output/eval-live.json`，结果写入 `output/eval-rescored.json`，为 **7/8**。重评分未调用模型，未改该轮 fixture。
剩余失败是源码任务：模型把 `demo/project main.py` 误判为缺少仓库名，未调用 `read_file`。这是模型任务理解的已知不稳定性，不按成功计数。实际使用建议明确分开表达“仓库：owner/repo；文件：path”。

这些指标是小型合成回归任务的规则评分，不能作为真实仓库分析准确率、全面防注入能力或生产可用性的保证。
本次未部署公开服务、未执行 GitHub 写操作，也未进行完整“真实模型 + 真实 GitHub”的端到端验收。
# LangGraph 接入验证

LangGraph 1.2.18 已替换多 Agent 的手写调度。103 项 Python 测试、8 项 Node 测试和依赖一致性检查通过。新增图测试覆盖真实节点执行顺序、空计划分支、并发状态与资源隔离、记忆范围传递、MCP 连接复用、SSE、失败和取消时不提交半轮。复现命令及当前未启用的 checkpoint 能力见 [LANGGRAPH.md](LANGGRAPH.md)。

更新后的 `scripts/check_full_stack.py` 也已通过：真实模型执行 LangGraph 的 Planner → Orchestrator → Knowledge → Synthesis 节点，使用本地 Embedding 与 Milvus 命中合成资料中的 17 分钟，返回实际工具来源，并在 PostgreSQL 归档。验收资料使用临时 schema，结束清理。

# PostgreSQL / Milvus / 多 Agent 验证

本次本机执行通过：95 项 Python 测试、8 项 Node 测试、前端语法检查。`scripts/check_infrastructure.py` 在真实 PostgreSQL 16 与 Milvus 2.5.4 上验证迁移幂等、会话与历史归档、记忆审批 CAS、HNSW 混合检索、知识库隔离和过期索引拒绝。其 Embedding 为明确的测试向量。

`scripts/check_full_stack.py` 已使用当前配置的真实聊天模型、MCP 子进程与本地 Qwen Embedding，验证 Planner → Knowledge Agent → Synthesis → PostgreSQL 归档，回答命中合成文档中的 17 分钟并包含真实来源。Code / Issue / PR 的角色分派和权限限制通过离线测试验证，本次没有声称对三者分别完成真实 GitHub 全链路验收。

实际旧数据导入：1 个会话、1 个历史轮次，其他业务表原本为空；计数与逐字段核对通过。更多限制和复现命令见 [ARCHITECTURE.md](ARCHITECTURE.md)。

