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

### Toolbox 部署（标准开发执行环境）

所有开发、编译、脚本执行任务在 toolbox（`agent-toolbox` 容器）中进行。

**⚠️ 关键限制：toolbox 端口映射只有两个，不可随意在 toolbox 里起 HTTP 服务对外暴露：**
- `22 → 12222`（SSH，不可用于 Web）
- `8001 → 18001`（crontab-ui，已占用）

**因此，在 toolbox 中启动的 HTTP 服务无法被外部用户直接访问。**

---

### 对外提供 Web 访问的正确方式

需要给用户提供可访问的 URL 时，有以下两种方式：

#### 方式一：通过 agent-service 静态文件服务（推荐，适合单页 HTML/小文件）

agent-service 挂载了 `/app/app/data/` 目录，且主服务端口 8000 对宿主机可见。

```bash
# 1. 在 toolbox 中生成文件，写入 /opt/agent-workspace/{项目名}/
docker_operator: {"action": "toolbox_action", "toolbox_action": "command",
  "command": "mkdir -p /opt/agent-workspace/{项目名} && cat > /opt/agent-workspace/{项目名}/index.html << 'EOF'\n...内容...\nEOF"}

# 2. 通过 agent-service 的 /api/v1/files/serve 或 object_storage 路径提供下载
#    或将文件写入 MinIO，返回 MinIO 公网 URL
```

实际上最简单的方式：把 HTML 内容通过 MinIO 上传，返回 MinIO 公网 URL，用户直接浏览器打开。

```bash
# 写入 MinIO（通过 toolbox）
docker_operator: {"action": "toolbox_action", "toolbox_action": "command",
  "command": "python3 -c \"\nimport boto3, os\ns3 = boto3.client('s3', endpoint_url=os.environ['MINIO_ENDPOINT'], ...)\ns3.put_object(Bucket='public', Key='{项目名}/index.html', Body=open('index.html','rb').read(), ContentType='text/html')\n\""}
```

#### 方式二：通过 v4-frontend（nginx）反向代理（适合需要持久运行的服务）

v4-frontend 容器是 nginx，端口 18080 对外暴露。可以在 toolbox 里把 HTML 文件写到 nginx 的静态目录。
但 v4-frontend 挂载的是前端 dist 目录，不建议污染。

#### 方式三：使用 agent-service 的 report/file 下载链接（最常用）

生成 HTML 文件后，调用 report_generator 或直接写入 `/app/app/data/object_storage/`，通过 `AGENT_EXTERNAL_HOST` 构造下载 URL。

```bash
# agent-service 内的 object_storage 路径对应公网 URL
# URL 格式：http://{AGENT_EXTERNAL_HOST}:{AGENT_SERVICE_PORT}/files/{文件名}
# 获取宿主机 IP：
curl -s http://agent-service:8000/api/v1/chat/v4/status | python3 -c "import sys,json; d=json.load(sys.stdin); print(d)"
# 或在 toolbox 中：
env | grep AGENT_SERVICE_URL
```

---

**标准部署流程（toolbox 内执行任务）：**

```bash
# 1. 在 toolbox 中完成开发构建
docker_operator: {"action": "toolbox_action", "toolbox_action": "command",
  "command": "cd /opt/agent-workspace/{项目名} && ...构建命令..."}

# 2. 确认 toolbox 内服务正常（内网验证）
docker_operator: {"action": "toolbox_action", "toolbox_action": "command",
  "command": "curl -s http://localhost:{内网端口}/health"}

# 3. 获取对外访问 URL（选方式一：生成文件 → MinIO / agent-service files）
#    不要尝试让 toolbox 的 HTTP 服务直接对外暴露
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
