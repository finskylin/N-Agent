# 技术设计规范

## 触发条件

满足以下任意一项，必须先输出技术设计文档再写代码：
- 超过 3 个文件的新增/修改
- 涉及数据库 schema 设计
- 有前后端交互
- 涉及异步、队列、定时任务
- 需要部署为独立服务

## 设计铁律

1. **禁止硬编码**：所有 URL、端口、API Key、模型名称、阈值参数、提示词文本必须通过配置管理（.env / config 文件），代码中不允许出现任何字面量配置值
2. **禁止 Mock 实现**：非测试代码禁止 mock 数据，所有数据必须来自真实 API/数据库/配置文件
3. **配置化优先**：功能行为、业务规则、阈值、开关等通过配置文件控制，不写死在代码里
4. **扩展性设计**：核心模块必须预留扩展点（接口/抽象类/策略模式），新增功能不应修改已有代码
5. **鲁棒性设计**：所有外部依赖（API/DB/文件/网络）必须有超时、重试、降级、兜底方案

## 标准项目目录结构

根据项目类型选择对应模板：

### Python 后端项目（FastAPI/Flask）
```
{project_name}/
├── docker-compose.yml          # 容器编排（必须）
├── Dockerfile                  # 镜像构建（必须）
├── .env.example                # 配置模板（必须，列出所有环境变量）
├── .gitignore                  # Git 忽略规则
├── requirements.txt            # Python 依赖
├── README.md                   # 启动说明
│
├── src/                        # 源代码
│   ├── main.py                 # 入口文件
│   ├── config.py               # 配置管理（Pydantic Settings，从 .env 读取）
│   ├── api/                    # API 层（路由定义）
│   │   ├── __init__.py
│   │   ├── router.py           # 路由注册
│   │   └── endpoints/          # 各业务接口
│   ├── services/               # 服务层（业务逻辑）
│   │   ├── __init__.py
│   │   └── {module}_service.py
│   ├── db/                     # 数据层
│   │   ├── __init__.py
│   │   ├── models.py           # 数据模型
│   │   ├── database.py         # 数据库连接
│   │   └── migrations/         # 迁移脚本
│   ├── schemas/                # 请求/响应 Schema
│   │   └── {module}_schema.py
│   └── utils/                  # 工具函数
│       └── common.py
│
├── tests/                      # 测试
│   ├── test_api.py
│   └── test_services.py
│
└── docs/                       # 文档（必须）
    ├── requirements/            # 需求文档
    ├── design/                  # 技术设计文档
    └── deployment/              # 部署文档
```

### 前端项目（Vue 3 / React）
```
{project_name}/
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── package.json
├── vite.config.js / next.config.js
│
├── src/
│   ├── main.js / main.ts       # 入口
│   ├── App.vue / App.tsx       # 根组件
│   ├── config/                 # 配置
│   │   └── index.js            # API 地址等配置
│   ├── api/                    # API 调用封装
│   │   └── request.js          # axios 统一封装
│   ├── views/                  # 页面
│   ├── components/             # 组件
│   ├── stores/                 # 状态管理（Pinia / Zustand）
│   ├── router/                 # 路由
│   └── assets/                 # 静态资源
│
└── docs/
    └── requirements/
```

### 全栈项目
```
{project_name}/
├── docker-compose.yml          # 统一编排前后端+数据库
├── .env.example
│
├── backend/                    # 后端（同 Python 后端结构）
│   ├── Dockerfile
│   ├── src/
│   └── tests/
│
├── frontend/                   # 前端（同前端结构）
│   ├── Dockerfile
│   └── src/
│
└── docs/                       # 统一文档
    ├── requirements/
    ├── design/
    └── deployment/
```

## 设计文档结构

```markdown
# [项目名称] 技术设计

## 1. 架构概览

### 整体架构
[描述系统整体结构，组件之间的关系]

### 技术选型
| 层次 | 技术 | 选型理由 |
|------|------|---------|
| 后端框架 | FastAPI / Flask / Express | ... |
| 数据库 | SQLite / MySQL / PostgreSQL | ... |
| 前端框架 | Vue 3 / React / 原生HTML | ... |
| 部署方式 | Docker / 本地 / K8s | ... |

### 技术选型原则
- 优先 SQLite：单机/本地应用不引入 MySQL/Redis
- 优先 Docker：所有服务容器化，docker-compose 一键启动
- 优先现有工具：能用已有 skill/库解决的不重复造轮子

## 2. 配置化设计

### 配置项清单
| 配置项 | 类型 | 默认值 | 说明 | 来源 |
|--------|------|--------|------|------|
| APP_PORT | int | 8000 | 服务端口 | .env |
| DB_URL | str | sqlite:///data.db | 数据库连接 | .env |
| API_KEY | str | (必填) | 第三方 API Key | .env |
| MAX_RETRY | int | 3 | 最大重试次数 | config.json |

### 配置加载方式
- 环境变量 → .env → Pydantic Settings（Python）/ dotenv（Node）
- 业务规则配置 → config.json / config.yaml → 启动时加载

## 3. 模块划分

| 模块 | 职责 | 文件路径 | 依赖 |
|------|------|---------|------|
| API 层 | 接口定义、路由、参数校验 | src/api/ | 服务层 |
| 服务层 | 业务逻辑、流程编排 | src/services/ | 数据层 |
| 数据层 | 数据存取、缓存 | src/db/ | 数据库 |
| 配置 | 环境变量管理 | src/config.py | .env |

## 4. 数据结构

### 数据库 Schema
```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_users_username ON users(username);
```

### 核心数据模型
[用代码块定义接口/类型]

### API 接口定义
| 接口 | 方法 | 路径 | 请求体 | 响应 | 错误码 |
|------|------|------|--------|------|--------|
| 健康检查 | GET | /health | - | {"status":"ok"} | - |
| 用户登录 | POST | /api/login | {username, password} | {token} | 401/422 |

## 5. 扩展性设计

### 扩展点清单
| 扩展点 | 扩展方式 | 说明 |
|--------|---------|------|
| 数据源 | 策略模式/接口 | 新增数据源只需实现接口，不改已有代码 |
| 通知渠道 | 插件模式 | 支持钉钉/邮件/飞书，通过配置切换 |
| 业务规则 | 配置驱动 | 阈值/开关通过 config.json 调整 |

### 接口/抽象定义
[列出需要抽象的核心接口]

## 6. 鲁棒性设计

### 错误处理策略
| 场景 | 策略 | 实现方式 |
|------|------|---------|
| 第三方 API 超时 | 重试 3 次 + 指数退避 | tenacity / 手写重试 |
| 第三方 API 不可用 | 降级返回缓存/默认值 | try-except + fallback |
| 数据库连接失败 | 重试 + 告警 | 连接池 + 健康检查 |
| 输入数据异常 | 参数校验 + 友好提示 | Pydantic / Joi |
| 并发竞争 | 乐观锁 / 幂等设计 | 版本号 / 唯一约束 |

### 监控与告警
- 健康检查接口：`/health`
- 关键操作日志：INFO 级别
- 异常日志：ERROR 级别 + 上下文信息

## 7. 关键流程

### 核心业务流程
1. 用户发起请求
2. API 层校验参数
3. 服务层处理业务逻辑
4. 数据层读写数据库
5. 返回结果

### 异常处理流程
[描述错误场景的处理方式]

## 8. 变更文件清单

### 新增文件
- `src/main.py` — 入口文件
- `src/config.py` — 配置管理
- `src/api/router.py` — 路由定义
- `src/services/xxx.py` — 业务逻辑
- `src/db/models.py` — 数据模型
- `docker-compose.yml` — 容器编排
- `.env.example` — 配置模板
- `docs/design/xxx.md` — 本设计文档

### 修改文件
- [无 / 列出需要修改的现有文件]

## 9. 风险评估

| 风险 | 影响 | 应对方案 |
|------|------|---------|
| 第三方 API 不稳定 | 功能不可用 | 重试+降级+缓存 |
| 数据量超预期 | 性能下降 | 分页+索引+异步 |
| 配置项遗漏 | 部署失败 | .env.example 必须完整 |
```

## 文档保存规则

设计文档必须保存到项目中，路径规则：
```
{project_root}/docs/design/
├── TDD_{项目名称}_{日期}.md        # 技术设计文档
├── TDD_{项目名称}_{日期}_v2.md     # 设计变更版本
└── diagrams/                       # 架构图/流程图
```

## 设计评审检查清单

设计完成后自检：

```
☐ 是否所有可变值都通过配置管理？（禁止硬编码）
☐ 是否有 mock 数据或假实现？（禁止 mock）
☐ 配置项清单是否完整？（.env.example 覆盖所有配置）
☐ 核心模块是否预留了扩展点？（接口/策略/插件）
☐ 所有外部依赖是否有超时/重试/降级方案？
☐ 是否考虑了并发/竞争条件？
☐ 数据库查询是否有索引？
☐ 部署方式是否明确？（docker-compose / 启动命令）
☐ 是否有安全风险？（SQL注入/XSS/明文密码/硬编码密钥）
☐ 文件清单是否完整？（不遗漏配置文件、迁移脚本、文档）
☐ 项目目录结构是否符合标准模板？
```

## 设计确认

设计输出后，必须以以下文字结尾：

> 以上技术设计方案是否确认？确认后开始实现。如需调整请告知。
