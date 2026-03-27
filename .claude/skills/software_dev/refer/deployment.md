# 上线部署规范

## 部署前检查清单

```
☐ 所有测试通过（启动测试、核心功能 E2E、边界测试）
☐ 无硬编码（URL/端口/密钥全部配置化）
☐ .env.example 已创建（列出所有需要的环境变量）
☐ 敏感信息不在代码中（.env 已加入 .gitignore）
☐ docker-compose.yml 已就绪（或其他部署配置）
☐ README 包含启动命令
☐ 健康检查接口可用（/health 或类似）
```

## 部署方式

### Toolbox 部署（必须，所有开发任务的标准部署方式）

所有服务必须在 toolbox（`agent-toolbox` 容器）中运行，不得在 agent-service 容器中启动服务。

**标准流程：**

```bash
# 1. 在 toolbox 中启动服务（通过 docker_operator）
docker_operator: {
  "action": "exec",
  "command": "cd /opt/agent-workspace/{项目名} && docker-compose up -d"
}

# 2. 确认服务启动成功
docker_operator: {
  "action": "exec",
  "command": "curl -s http://localhost:{端口}/health"
}

# 3. 获取宿主机 IP（用于告知用户访问地址）
docker_operator: {
  "action": "exec",
  "command": "curl -s ifconfig.me || hostname -I | awk '{print $1}'"
}
```

**端口映射说明：**
- toolbox 容器的端口已通过 docker-compose 映射到宿主机
- 用户访问地址为：`http://宿主机IP:{宿主机端口}`
- 部署完成后**必须**在验收报告中明确写出完整访问地址

**docker-compose.yml 端口配置要求：**
```yaml
services:
  app:
    ports:
      - "${HOST_PORT:-8080}:${APP_PORT:-8000}"  # 宿主机端口:容器端口，必须配置化
```

### Docker 部署（推荐）

必须提供 `docker-compose.yml` 和 `Dockerfile`：

```yaml
# docker-compose.yml 最小模板
version: '3.8'
services:
  app:
    build: .
    ports:
      - "${APP_PORT:-8000}:8000"
    env_file: .env
    volumes:
      - ./data:/app/data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
    restart: unless-stopped
```

```dockerfile
# Dockerfile 最小模板
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 本地部署

提供明确的启动命令：

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填写必要配置

# 3. 启动服务
python main.py
# 或
uvicorn main:app --host 0.0.0.0 --port 8000
```

## 测试验证

### 必须执行的测试

| 测试类型 | 触发条件 | 执行方式 |
|---------|---------|---------|
| 启动测试 | 任何服务 | `curl /health` 确认返回 200 |
| 核心功能 E2E | 任何功能 | 用真实参数调用核心接口 |
| 边界测试 | 有输入验证 | 测试空值、超长、非法类型 |
| 集成测试 | 涉及 DB/API | 真实连接测试，禁止 mock |

### 测试方法

```bash
# 启动测试
curl http://localhost:8000/health

# 接口测试
curl -X POST http://localhost:8000/api/xxx \
  -H "Content-Type: application/json" \
  -d '{"key": "value"}'

# 日志检查
docker logs <container> 2>&1 | grep ERROR
```

测试失败**必须修复**，禁止"测试失败但功能应该正常"。

## 验收报告

部署完成后，必须输出验收报告：

```markdown
## 验收报告

### 功能验收
| 功能点 | 预期结果 | 实际结果 | 状态 |
|-------|---------|---------|------|
| 健康检查 | 返回 200 | {"status": "ok"} | ✅ |
| 核心功能1 | ... | ... | ✅/❌ |

### 服务信息
- **访问地址**：http://{宿主机真实IP}:{宿主机端口}（必须是真实可访问的完整 URL，不能写占位符）
- **API 文档**：http://{宿主机真实IP}:{端口}/docs（FastAPI 自动生成）
- **启动命令**：`docker-compose up -d`
- **项目目录**：`/opt/agent-workspace/{项目名}/`（toolbox 持久化目录）
- **配置文件**：`.env`（需填写的配置项列出）
- **日志查看**：`docker logs <container> -f`

> ⚠️ 访问地址必须在部署完成后通过 `hostname -I` 或 `curl ifconfig.me` 获取真实 IP 后填写，不得使用 localhost 或占位符。

### 已知限制
- [当前版本的已知问题或未实现的功能]

### 下一步建议
- [可选的优化方向]
```

## 需要人工介入的场景

以下情况必须暂停并通知用户：

| 场景 | 通知内容 |
|------|---------|
| 需要 API Key | "请在 .env 中填写 XXX_API_KEY" |
| 需要域名/SSL | "请配置 Nginx 反向代理和 HTTPS 证书" |
| 数据库迁移 | "执行前请先备份数据库" |
| 生产部署 | "请确认无流量后执行 ./deploy.sh" |
| 安全风险 | "当前方案存在 XX 风险，请确认是否接受" |

通知格式：
```
⚠️ 需要您手动处理以下事项：

1. [具体操作步骤]
2. ...

完成后告诉我，我继续下一步。
```
