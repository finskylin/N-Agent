# N-Agent

智能多领域研究分析 Agent 服务，基于 **FastAPI + 自研 AgentLoop** 构建。支持多领域研究分析、数据采集、报告生成，并提供 Web 和钉钉/飞书渠道接入能力。

## 特性

- **AgentLoop 引擎**：自研 LLM 驱动的对话循环，支持最多 30 轮工具调用，readonly 工具并行执行
- **87+ Skills**：覆盖股票分析、财务研报、地缘政治、卫星遥感、文档处理等领域
- **三层解耦架构**：`app` → `agent_core` → `.claude/skills`，依赖方向严格单向
- **多渠道支持**：Web SSE 流式输出 + 钉钉 Stream + 飞书机器人
- **MemoryOS**：跨会话三层记忆（长期记忆、经验、知识图谱）
- **异步后处理**：`yield done` 立即响应，历史落库 / 记忆更新 / 知识蒸馏全部后台异步执行
- **热加载**：Skill 代码修改无需重启服务

## 快速开始

### 环境要求

- Python >= 3.11
- Docker & Docker Compose（推荐）

### 安装依赖

```bash
pip install -r requirements.txt
```

### 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入 LLM API Key、Tushare Token 等配置
```

关键配置项：

| 变量 | 说明 |
|------|------|
| `ANTHROPIC_BASE_URL` | LLM API 基础 URL（支持第三方兼容接口） |
| `ANTHROPIC_AUTH_TOKEN` | LLM 鉴权 Token |
| `ANTHROPIC_MODEL` | 主流程模型名（如 `glm-4.7`） |
| `ANTHROPIC_SMALL_FAST_MODEL` | 快速模型名（用于意图识别等轻量任务） |
| `EXA_API_KEY` | Exa 搜索 API Key |
| `HTTP_PROXY` / `HTTPS_PROXY` | 代理配置（访问 Google 等） |
| `V4_DEFAULT_USER_ID` | 默认用户 ID |

### 启动服务

```bash
# 开发模式
python -m app.main

# 或使用 uvicorn
uvicorn app.main:app --reload --port 8000
```

### Docker 启动（推荐）

```bash
# 构建并启动全套服务
docker-compose -f docker-compose.v4-test.yml up -d

# 查看日志
docker logs agent-service --tail 50 -f

# 重启服务
docker restart agent-service
```

Docker Compose 包含以下服务：

| 服务 | 端口 | 说明 |
|------|------|------|
| `agent-service` | 8000 | FastAPI 主服务 |
| `v4-frontend` | 18080 | Vue 前端（Nginx） |
| `agent-toolbox` | 12222 (SSH) | Linux 执行环境（用于代码沙箱） |
| `clash` | 7890 | 代理服务 |

## 项目结构

```
N-Agent/
├── app/                     # 应用层（使用 agent_core，不被其引用）
│   ├── main.py              # FastAPI 入口
│   ├── config.py            # AppSettings（Pydantic Settings）
│   ├── api/                 # REST + SSE 接口
│   ├── agent/v4/            # V4NativeAgent 及扩展点
│   ├── channels/            # 渠道适配（钉钉、飞书）
│   ├── services/            # 业务服务（文件生成、知识库等）
│   ├── db/                  # 数据库模型（SQLite）
│   ├── prompts/             # 应用层提示词（*.md）
│   ├── config/              # 应用层配置（*.json）
│   ├── data/                # 运行时数据（上传、生成文件、会话）
│   ├── knowledge/           # 本地知识库文件
│   ├── static/              # 静态资源（Vue 组件 bundle）
│   └── tests/               # 测试用例（含 e2e/）
│
├── agent_core/              # 核心引擎层（无 app 依赖）
│   ├── config.py            # V4Config（Pydantic Settings）
│   ├── agentloop/           # LLM 对话循环（loop.py 等）
│   ├── session/             # 会话管理、历史压缩
│   ├── memory/              # MemoryOS（跨会话记忆）
│   ├── knowledge/           # 知识引擎（提炼、反思、进化）
│   ├── mcp/                 # MCP stdio 服务器
│   ├── prompts/             # 核心层提示词（*.md）
│   ├── config/              # 核心层配置（*.json）
│   ├── skill_discovery.py   # Skill 元数据发现
│   └── skill_executor.py    # Skill 执行器
│
└── .claude/skills/          # Skill 层（完全独立，无跨层依赖）
    └── {skill_name}/
        ├── SKILL.md         # 元数据 + LLM 执行指导
        └── scripts/         # 执行脚本（仅用 stdlib + pip）
```

## API 文档

启动后访问：http://localhost:8000/docs

### 核心接口

| 接口 | 说明 |
|------|------|
| `POST /api/v4/chat/send` | 发送消息，SSE 流式返回 |
| `GET /api/v4/chat/skills` | 获取可用 Skills 列表 |
| `GET /health` | 健康检查 |

## Skills 一览

系统内置 87+ 个独立 Skill，按领域分类：

| 类别 | 代表 Skill |
|------|-----------|
| 股票行情 | `stock_query`, `realtime_quote`, `historical_data`, `technical_indicators` |
| 财务分析 | `financial_report`, `financial_statement`, `valuation_analysis`, `dividend_analysis` |
| 资金流向 | `money_flow`, `northbound_capital`, `block_trade`, `margin_trading` |
| 股东与机构 | `shareholder_analysis`, `institutional_research`, `insider_trading` |
| AI 预测 | `prediction`, `backtest`, `kline_pattern`, `catalyst_radar` |
| 市场概览 | `market_overview`, `market_environment`, `limit_board`, `dragon_tiger` |
| 搜索与抓取 | `quick_search`, `url_fetch`, `document_reader`, `research_paper` |
| 地缘政治 | `geopolitical_security`, `conflict_analysis`, `military_balance`, `cognitive_warfare` |
| 地理空间 | `geo_search`, `satellite_imagery`, `city_3d_model`, `map_bindary` |
| UI 与文档 | `frontend_builder`, `pdf_preview`, `xlsx_preview`, `pptx_preview`, `docx_preview` |
| 系统工具 | `docker_operator`, `spawn_agent`, `sandbox_execute`, `cron`, `memory_manager` |

每个 Skill 完全独立，位于 `.claude/skills/{skill_name}/`，添加新 Skill 无需修改任何已有代码。

## 架构约束

三层架构，依赖方向严格单向：

```
.claude/skills/  →  只允许使用 stdlib、pip 包、环境变量
                    禁止 import agent_core.* 或 app.*

agent_core/      →  只允许使用 stdlib、pip 包、agent_core 内部
                    禁止 import app.*
                    存储只用 SQLite + 文件，不引入 Redis/MySQL/MinIO

app/             →  可使用 agent_core、stdlib、pip 包
                    禁止直接 import .claude.skills.*（热加载除外）
```

## 开发规范

- **禁止 Mock**：所有数据来自真实 API / 数据库，非测试代码中不得使用 Mock
- **禁止硬编码**：URL、端口、API Key、模型名、阈值等全部通过 `.env` + Pydantic Settings 管理
- **提示词外置**：提示词统一存放在 `*/prompts/*.md`，通过 `load_prompt` 加载
- **Skill 独立**：每个 Skill 代码完全自包含，Skill 间不得直接 import
- **大功能先设计**：涉及 3 个以上文件修改须先出设计文档，经确认后再写代码
- **异步后处理**：`yield done` 必须立即发出，耗时操作不得阻塞响应

## 技术栈

| 类别 | 技术 |
|------|------|
| Web 框架 | FastAPI 0.109, uvicorn 0.27 |
| 数据库 | SQLite（aiosqlite）, SQLAlchemy 2.0 |
| LLM | anthropic 0.77, openai 1.58, MCP 1.8+ |
| 数据分析 | pandas, numpy, polars |
| 机器学习 | XGBoost, LightGBM, scikit-learn, TensorFlow |
| 浏览器自动化 | Playwright, Selenium |
| 文档处理 | python-docx, python-pptx, PyPDF, reportlab |
| 消息渠道 | dingtalk-stream 0.18, lark-oapi 1.3 |
| 代码沙箱 | e2b-code-interpreter 1.0.3 |
| 配置管理 | Pydantic Settings, python-dotenv |
| 日志 | loguru |

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 开发模式启动
python -m app.main

# Docker 启动
docker-compose -f docker-compose.v4-test.yml up -d

# 重启服务
docker restart agent-service

# 查看日志
docker logs agent-service --tail 50 -f

# 运行测试
pytest app/tests/
```
