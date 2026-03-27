# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

智能多领域研究分析 Agent 服务，基于 FastAPI + 自研 AgentLoop 构建。提供多领域研究分析、数据采集、报告生成等功能，支持 Web 和钉钉渠道。

## 项目重点约束

### 三层架构，依赖方向严格单向：

```
.claude/skills/（技能层）
    ↑ 只被 agent_core 调用，自身无任何跨层依赖
    禁止：import agent_core.*  import app.*
    禁止：skill 之間被互相引用
    禁止：skill 的內容外溢到自己的文件夾之外

agent_core/（核心引擎层）
    ↑ 只被 app 引用
    禁止：import app.*

app/（应用层）
    引用 agent_core，不直接 import .claude/skills（hot_reload 除外，仅用模块字符串动态加载）
```
### 超级约束
-`agent_core` 与 `.claude/skills` 互相没有任何引用依赖
- `agent_core` 不会引用 `app/`
- 只有 `app/` 会引用 `agent_core/`
- `app/` 不会直接 import `.claude/skills/`
### 应用约束
**agent_core**: 
  - 全部使用个sqllite和文件存储，不要引入任何其他minio,redis,mysql等服务
  - agent_core 是整个核心报告session、memory、核心问答链路的管理实现层，不会外溢代码和逻辑，所有的配置都在agent_core层
**skills**: 
  - 绝对的解耦,skill之间也是绝对解耦不会有任何直接引用,skills与Agent_core,app 之间绝对解耦不会有任何直接引用
**app**:
  - 主要是本地知识库的管理,用户的管理,以及底层执行数据和状态通过hook获取，实现的data2ui或chatweb的实现
### 项目层级
- sthg_agent_service/目录下只允许有三个目录.agent_core/、app/ 、.claude/skills/ 

```
sthg_agent_service/
├── agent_core/              # 核心引擎层（无 app 依赖）
│   ├── agentloop/           # LLM 对话循环（loop.py, llm_provider.py, context_builder.py 等）
│   ├── session/             # 会话管理、历史压缩、上下文数据库
│   ├── knowledge/           # 知识引擎（提炼、反思、进化）
│   ├── memory/              # MemoryOS（跨会话记忆）
│   ├── mcp/                 # MCP stdio 服务器
│   ├── config/              # agent_core 专属配置（session_compact.json, performance.json, mcp/, knowledge/）
│   ├── prompts/             # agent_core 专属提示词（session/memory/knowledge 相关 .md）
│   ├── config.py            # V4Config（Pydantic Settings）
│   ├── skill_discovery.py   # Skill 元数据发现
│   └── skill_executor.py    # Skill 执行器
│
├── app/                     # 应用层（使用 agent_core，不被 agent_core 引用）
│   ├── main.py              # FastAPI 入口
│   ├── config.py            # AppSettings（Pydantic Settings，从 .env 读取）
│   ├── api/                 # REST + SSE 接口（chat_v4.py 等）
│   ├── agent/v4/            # V4NativeAgent 及扩展点（hook_manager, scene_context 等）
│   ├── channels/            # 渠道适配（钉钉、飞书）
│   ├── services/            # 业务服务（file_generator, knowledge_file_service 等）
│   ├── db/                  # 数据库模型和连接
│   ├── prompts/             # 应用层提示词（v4_unified_system, frontend_builder_system 等 .md）
│   ├── config/              # 应用层配置（skill_framework.json, tool_cache.json, ui/, benchmark/）
│   ├── data/                # 运行时数据（uploads, generated, sessions, object_storage）
│   ├── static/              # 静态资源（Vue 组件 bundle）
│   ├── knowledge/           # 知识库文件
│   └── tests/               # 测试用例
│
└── .claude/skills/          # Skill 层（完全独立，无 app/agent_core 依赖）
    └── {skill_name}/
        ├── SKILL.md          # 元数据（name, description, readonly 等）+ body（指导 LLM 执行时的参数用法）
        └── scripts/          # 执行脚本（仅用 stdlib + pip 包 + 环境变量）
```

## 分层依赖规则（铁律）

```
.claude/skills/   ──→  只允许：stdlib、pip 包、环境变量
                        禁止：import agent_core.*  import app.*

agent_core/       ──→  只允许：stdlib、pip 包、agent_core 内部
                        禁止：import app.*

app/              ──→  只允许：stdlib、pip 包、agent_core、app 内部
                        禁止：import .claude.skills.*（hot_reload 除外，仅用模块字符串动态加载）
```

**Skill 访问运行时服务（文件存储、数据库、钉钉 API）的唯一合法方式：读取环境变量，直接调用 HTTP/SQL，不得 import app 层代码。**

## 开发铁律

### 1. 禁止 Mock 与硬编码

- **禁止在任何非测试代码中使用 mock 数据**。所有数据必须来自真实的 API 调用、数据库查询或配置文件。
- **禁止硬编码**，包括但不限于：URL、端口、API Key、模型名称、阈值参数、提示词文本。所有可变值通过 `.env` + `app/config.py`（AppSettings）或 `agent_core/config.py`（V4Config）管理。
- **提示词外置**：
  - `agent_core` 层提示词 → `agent_core/prompts/*.md`，通过 `agent_core.prompts.loader.load_prompt` 加载
  - `app` 层提示词 → `app/prompts/*.md`，通过 `app.prompts.loader.load_prompt` 加载
  - 模板变量用 `{var}`，LLM 需要看到的花括号转义为 `{{` `}}`
- **拒绝纯规则性方案**。意图识别、内容分类、格式推断等场景，优先使用 LLM，if-else 规则仅作兜底。
- **完整的 E2E 测试**。每个功能必须有覆盖真实调用链的 E2E 测试，测试案例示例：

```json
{
  "caseName": "获取国盾量子股票的下周操作指南",
  "entrance": ["钉钉", "web"],
  "UI": [
    "对话框中出现执行状态框，每个问题都有一个执行状态",
    "web 执行报告是美化的 md，具备报告下载地址，具备反馈功能",
    "web 执行可以看到图表类渲染，没有空白渲染的组件",
    "具备报告下载地址，具备整体分析的链接，可以打开，装载了所有数据"
  ],
  "backend": ["后端不报错，存储数据正常，配置化，扩展性强"],
  "result": "问答正常结束"
}
```

### 2. Skill 模块独立性

- **每个 Skill 的代码完全独立在自己的文件内**，不扩散到其他文件，不与其他 Skill 共用代码。
- **Skill 之间不得有直接 import 依赖**，只能通过 AgentLoop 的工具调用机制传递数据。
- **禁止将 Skill 逻辑耦合到 Agent 核心流程**。不得在 `native_agent.py` 中为某个 Skill 写特殊处理逻辑。
- **新增 Skill 不得修改已有 Skill 或 agent_core/app 的任何代码**，遵守 [README.md](.claude/skills/README.md)的規範
- **只读 Skill 标记**：在 `SKILL.md` frontmatter 中声明 `readonly: true`，`ParallelToolExecutor` 会并行执行这类工具。
- **`input_schema` 是废弃字段，禁止写在 frontmatter**，框架传给 LLM 的 parameters 固定为空 `{}`。

### Skill 渐进式披露机制（必须理解）

LLM 与 Skill 的交互分两阶段，编写 SKILL.md 时必须针对不同阶段写不同内容：

| 阶段 | LLM 读取内容 | 作用 |
|------|-------------|------|
| **选工具（阶段 1）** | `name` + `description` | LLM 决定调不调这个工具，**description 决定工具能否被选中** |
| **执行（阶段 2）** | SKILL.md body（pre-inject 注入） | LLM 读完 body 再重新调用，**body 决定参数能否传对** |

- **`description`**：写清楚"能做什么"、"什么场景触发"、"不适用场景"。**不要写参数格式**。
- **SKILL.md body**：写参数表格 + 完整调用示例 JSON。LLM 照着示例构造参数，**示例必须覆盖主要使用场景**。

### 3. 问答核心流程不可变

核心流程由 `app/agent/v4/native_agent.py`（V4NativeAgent）组装，`agent_core/agentloop/loop.py`（AgentLoop）驱动执行：

```
【请求入口】app/api/chat_v4.py → V4NativeAgent.stream()
    ↓
【Session 准备】SessionEngine.prepare_session()
    ├── ConversationHistory.get_with_summary()  → 历史对话 + 摘要
    ├── ExperienceStore.get()                   → 用户经验（6 维度）
    ├── KnowledgeStore 检索                     → 知识引擎相关知识
    └── MemoryRetriever.retrieve()              → MemoryOS 三层记忆召回
    ↓
【Prompt 构建】PromptBuilder → system prompt（历史摘要 + 经验 + 知识 + 记忆 注入）
    ↓
【AgentLoop 执行】loop.py — 循环最多 30 次：
    ├── HookEngine.fire(PRE_LLM_CALL)
    ├── LiteLLMProvider.chat_stream()           → LLM 推理
    ├── HookEngine.fire(POST_LLM_CALL)
    ├── 有工具调用？
    │   ├── [readonly 组] ParallelToolExecutor → asyncio.gather 并行
    │   └── [write 组]   SkillInvoker        → 顺序执行
    │       ↕ HookEngine.fire(PRE/POST_TOOL_USE)
    ├── ContextCompactor.maybe_compact()        → 超 token 预算时 LLM 摘要压缩
    └── 无工具调用 → OutputValidator → break
    ↓
【流程结束 — yield done 立即发出】
    ↓（done 事件，用户立即收到完整回答）
【后处理全异步，不阻塞任何响应】
    ├── ConversationHistory.append_text_exchange()  → create_task 后台
    ├── EpisodeTracker.finish()（知识蒸馏/反思）   → create_task 后台
    ├── MemoryUpdater.on_turn_end()（LTM+经验）    → create_task 后台
    ├── KnowledgeGuard.safe_finalize()（图谱写入）  → create_task 后台
    ├── PredictionExtractor.extract_and_save()      → ensure_future 后台
    └── ConversationHistory.maybe_summarize()       → ensure_future 后台（消息数>8触发）
```

**扩展点**（允许在此添加逻辑，不得改变主流程顺序和数据流）：
- `HookEngine`（PRE_LLM_CALL / POST_LLM_CALL / PRE_TOOL_USE / POST_TOOL_USE / ON_CONTEXT_COMPACT）
- `EventBridge`（事件广播，不影响主流程）
- `DataCollector`（数据采集标注，旁路写入）
- `SceneContext`（场景推断，影响 prompt 内容）

**对核心流程的任何修改必须先产出设计文档，经用户确认后方可动代码。**

### 4. 问答后处理必须全异步，禁止阻塞 done 事件

**`yield done` 必须立即发出，禁止在其之前 `await` 任何耗时操作。**

#### 已实现的异步能力清单（禁止改回同步）

**A. 工具调用并行（AgentLoop 内，`agent_core/agentloop/loop.py`）**

| 能力 | 实现方式 | 涉及文件 |
|------|---------|---------|
| readonly 工具组并行执行 | `ParallelToolExecutor` + `asyncio.gather` | `loop.py`, `skill_invoker.py` |
| spawn_agent 子 Agent 并行 | `asyncio.gather`（spawn_agent skill 内部） | `.claude/skills/spawn_agent/` |

**B. 后处理全异步（`yield done` 发出后才执行）**

| 操作 | 位置 | 实现方式 |
|------|------|---------|
| `history.append_text_exchange`（对话历史落库） | `agent_core/agentloop/session_engine.py` | `asyncio.create_task` |
| `memory_updater.on_turn_end`（LTM + 经验提取，含 LLM） | `agent_core/agentloop/session_engine.py` | `asyncio.create_task`，与 history 并行 |
| `knowledge_guard.safe_finalize`（知识图谱写入） | `app/agent/v4/hook_manager.py` Stop hook | `asyncio.create_task` |
| `episode_tracker.finish`（知识蒸馏/反思，含 LLM） | `app/agent/v4/hook_manager.py` Stop hook | `asyncio.create_task`（经 knowledge_guard） |
| `prediction_extractor.extract_and_save`（预测提取） | `agent_core/agentloop/session_engine.py` | `asyncio.ensure_future` |
| `prediction_scheduler.run_overdue_check`（到期预测验证） | `agent_core/agentloop/session_engine.py` | `asyncio.ensure_future` |
| `maybe_summarize`（历史压缩，消息数 > 8 触发） | `app/agent/v4/native_agent.py` | `asyncio.ensure_future`，`report_ready` 之后调用 |

**C. Session 准备期共享优化（`agent_core/agentloop/session_engine.py`）**

| 操作 | 实现方式 |
|------|---------|
| query embedding 一次计算，知识引擎 + MemoryOS 共享复用 | 请求开始时计算一次 `query_vec`，传给两个检索器，避免重复 API 调用 |

**必须串行（流程控制，不可改为异步）：**
- `Reflection 评估`：决定是否继续 AgentLoop 循环
- `ContextCompactor.maybe_compact()`：运行时 token 压缩，决定下轮上下文
- `drain_event_bridge`：排空 data2ui 事件队列，必须在 `yield done` 之前完成

**违规标准：** 在 `yield done` 之前出现 `await <耗时LLM调用>` 即为违规。

### 5. 大功能必须先设计后实施

- **涉及 3 个以上文件修改、新增模块、架构调整、核心流程扩展点变更**，均属于大功能，必须先产出设计文档。
- 设计文档写入 `output_project/doc_design/agent2/` 目录。
- 命名格式：`YYYY-MM-DD_功能名称.md`（如 `2026-03-14_agentloop_sdk_capability_enhancement.md`）。
- 设计文档必须包含：功能背景与目标、方案设计（含流程图）、影响范围分析、变更文件清单、测试验证方案。
- **设计文档经用户确认后，方可开始写代码。**

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 开发模式启动
python -m app.main

# Docker 启动
docker-compose -f docker-compose.v4-test.yml up -d

# 重启 agent-service
docker restart agent-service

# 查看日志
docker logs agent-service --tail 50 -f

# 运行测试
pytest app/tests/
```

## 配置速查

| 类型 | 位置 | 加载方式 |
|------|------|---------|
| 环境变量 | `.env` | 不提交 git |
| 应用配置 | `app/config.py` | `AppSettings`（Pydantic） |
| AgentCore 配置 | `agent_core/config.py` | `V4Config`（Pydantic） |
| AgentCore 子配置 | `agent_core/config/*.json` | 各模块直接读取 |
| 应用层子配置 | `app/config/*.json` | 各模块直接读取 |
| app 层提示词 | `app/prompts/*.md` | `app.prompts.loader.load_prompt` |
| agent_core 提示词 | `agent_core/prompts/*.md` | `agent_core.prompts.loader.load_prompt` |
| Skill 元数据 | `.claude/skills/*/SKILL.md` | `skill_discovery.py` |
