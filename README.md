# Stock Agent Service

Claude Agent SDK + Skills 框架的 Python 后端服务。

## 快速开始

### 安装依赖

```bash
cd output_project/sthg_agent_service
pip install -r requirements.txt
```

### 配置环境

```bash
cp .env.example .env
# 编辑 .env 填入 Tushare Token 等配置
```

### 启动服务

```bash
# 开发模式
python -m app.main

# 或使用 uvicorn
uvicorn app.main:app --reload --port 8000
```

### Docker 启动

```bash
docker build -t sthg-agent-service .
docker run -p 8000:8000 sthg-agent-service
```

## API 文档

启动后访问: http://localhost:8000/docs

### 核心接口

- `POST /api/v1/chat/send` - 发送消息给 Agent
- `GET /api/v1/chat/skills` - 获取可用 Skills 列表
- `GET /health` - 健康检查

## 项目结构

```
app/
├── main.py              # FastAPI 入口
├── config.py            # 配置
├── agent/               # Agent 核心
│   └── agent_service.py
├── skills/              # Skills 实现
│   ├── base.py          # Skill 基类
│   ├── registry.py      # 注册表
│   ├── executor.py      # 执行器
│   ├── collection/      # 数据采集
│   ├── feature/         # 特征工程
│   ├── analysis/        # 基本面分析
│   ├── prediction/      # ML预测
│   ├── validation/      # 验证回测
│   └── output/          # 报告输出
└── api/                 # REST API
    └── chat.py
```
