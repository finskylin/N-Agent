---
name: docker_operator
display_name: 容器执行与工具箱
description: |
  【功能】独立的完整计算机执行环境（agent-toolbox），能做任何软件类的工作。内置 Ubuntu Linux + 完整开发工具链 + 持久化存储。
  【核心能力】编程开发、视频/音频生成与处理、数据清洗ETL、网页爬取截图、前端原型开发、Web服务部署、软件安装、文件操作、通知推送、后台长任务。
  【优先使用本工具的场景】偏重任务、消耗资源较大的任务应优先使用 docker_operator 而非 bash，例如：
  生成视频/音频/多媒体文件、安装软件包、编写并运行脚本、数据清洗处理、网页爬取截图、前端开发部署、项目开发、软件部署、训练模型、后台长任务、文件持久化存储等。
priority: 90
keywords:
  # toolbox 执行
  - 脚本
  - script
  - python
  - node
  - bash
  - 命令
  - command
  - 执行
  - 运行
  # 代码开发
  - 写代码
  - 写脚本
  - 编程
  - 开发
  - 实现
  - 代码
  - coding
  - 帮我写
  - 生成代码
  - 编写
  - 写个
  - 写一个
  # 通知发送
  - 通知
  - 发送
  - 推送
  - 钉钉
  - 邮件
  - webhook
  - 提醒
  - 告警
  # 文件操作
  - 保存文件
  - 写入文件
  - 读取文件
  - 删除文件
  - 持久化
  - 文件处理
  - 文件转换
  # 浏览器自动化
  - 截图
  - screenshot
  - 抓取网页
  - 爬虫
  - playwright
  - 浏览器
  - 爬取
  - 抓取
  - 采集
  - 网页采集
  - 数据采集
  # 软件安装
  - 安装
  - install
  - apt
  - pip
  - npm
  # 系统操作
  - 服务器
  - linux
  - 系统
  - 终端
  # 前端原型
  - 前端
  - 原型
  - 页面
  - HTML
  - CSS
  - JavaScript
  - Vue
  - React
  - 组件
  - UI
  - 界面
  - 网页
  - demo
  - 演示
  - 预览
  # Web服务
  - 服务
  - 启动服务
  - Web
  - HTTP
  - API服务
  - Flask
  - FastAPI
  - Express
  - 访问地址
  - URL
  # 数据治理
  - 数据清洗
  - 数据转换
  - 数据分析
  - 数据聚合
  - 数据处理
  - ETL
  - pandas
  - polars
  - numpy
  - dataframe
  - 去重
  - 缺失值
  - 异常值
  - 标准化
  - csv
  - excel
  - json
  - 表格
  # 测试验证
  - 测试
  - 验证
  - 检查
  - 检测
  - 校验
  - 调试
  - debug
  # 容器管理
  - 执行
  - 容器
  - docker
  - 训练
  - 后台
  - 进程
  - 日志
  # 视频音频
  - 视频
  - 音频
  - 转录
  - 字幕
  - yt-dlp
  - ffmpeg
  - whisper
intents:
  - notification
  - script_execution
  - file_operation
  - browser_automation
  - system_operation
  - persistent_task
  - data_governance
  - data_cleaning
  - data_transformation
  - data_analysis
  - frontend_prototype
  - web_service
  - ui_development
  - demo_creation
  - code_development
  - code_execution
  - web_scraping
  - data_crawling
  - api_testing
  - automation
  - batch_processing
  - data_pipeline
  - etl_pipeline
  - container_management
  - background_task
triggers:
  - pattern: "发送|通知|提醒|推送|告警"
    description: 通知触发词
  - pattern: "脚本|执行|运行|命令"
    description: 执行触发词
  - pattern: "清洗|转换|聚合|去重|缺失值|异常值|ETL|数据处理|整理|筛选|过滤"
    description: 数据治理触发词
  - pattern: "前端|原型|页面|HTML|CSS|Vue|React|组件|UI|界面|demo|演示"
    description: 前端原型触发词
  - pattern: "启动服务|Web服务|HTTP服务|API服务|访问地址|URL"
    description: Web服务触发词
  - pattern: "写代码|写脚本|编程|开发|实现|帮我写|写个|写一个"
    description: 代码开发触发词
  - pattern: "爬虫|爬取|抓取|采集|网页抓取|数据采集"
    description: 数据抓取触发词
  - pattern: "测试|验证|检查|检测|校验|调试|debug|排查"
    description: 测试验证触发词
  - pattern: "调用API|API请求|HTTP请求|接口调用|请求接口|curl"
    description: API调用触发词
  - pattern: "自动化|自动执行|批量处理|批量执行"
    description: 自动化触发词
  - pattern: "视频|音频|转录|字幕|yt-dlp|ffmpeg|whisper"
    description: 视频音频触发词
  - pattern: "容器|docker|训练|后台运行|进程管理|日志"
    description: 容器管理触发词
time_estimates:
  default:
    min: 5
    max: 60
    desc: "toolbox 命令执行"
  script_run:
    min: 10
    max: 120
    desc: "toolbox 脚本执行"
  container_op:
    min: 1
    max: 30
    desc: "容器操作"
authority: high
key_params:
  - action
  - toolbox_action
  - command
  - script
---
# Docker Operator / Toolbox 执行技能

## 调用方式（必须用 bash 工具）

通过 `bash` 工具执行，将 JSON 参数通过 stdin pipe 给脚本：

```bash
echo '{"action":"toolbox","toolbox_action":"command","command":"ls -la /opt/agent-workspace"}' | python3 /app/.claude/skills/docker_operator/scripts/docker_operator.py
```

**关键规则**：
- 必须用 `bash` 工具，不能直接调用 `docker_operator`
- JSON 中若有特殊字符，用单引号包裹整个 echo 参数，或用 heredoc：
```bash
python3 /app/.claude/skills/docker_operator/scripts/docker_operator.py <<'EOF'
{"action":"toolbox","toolbox_action":"script","script_type":"python","script":"print('hello')"}
EOF
```

## 执行策略：最小代价原则

按优先级选择执行方式：

| 优先级 | 执行环境 | 用法 | 开销 | 适用场景 |
|--------|----------|------|------|----------|
| **0** | **toolbox（推荐）** | `action=toolbox, toolbox_action=command/script/...` | 5-60s | 任何 Linux 操作，完整环境 |
| 1 | 已有容器 exec | `action=exec, container=容器名` | <1s | 需要进入特定已有容器 |
| 2 | 新建沙箱容器 | `action=run, image=镜像名` | 3-10s | 需要完全隔离的新环境 |

## Toolbox 执行环境（action=toolbox）

agent-toolbox 是一个持续运行的 Ubuntu 容器，内置完整 Linux 工具链，是处理复杂任务的首选环境。

### 支持的 toolbox_action

| toolbox_action | 描述 |
|----------------|------|
| `command` | 执行任意 bash 命令 |
| `script` | 运行脚本（bash/python/node） |
| `file_write` | 写入文件 |
| `file_read` | 读取文件 |
| `file_delete` | 删除文件 |
| `install` | 安装软件包（apt/pip/npm） |
| `status` | 工具箱状态 |
| `playwright` | 浏览器自动化（screenshot/scrape/pdf） |

### Toolbox 工作目录结构

```
/opt/agent-workspace/
├── jobs/           # 定时任务脚本
├── scripts/        # 临时脚本
├── data/           # 数据存储
│   ├── raw/        # 原始数据
│   ├── processed/  # 处理后数据
│   └── output/     # 输出结果
├── logs/           # 日志文件
└── templates/      # 模板文件
```

### 已安装的数据处理库

| 库 | 用途 |
|----|------|
| **Pandas** | 结构化数据处理、DataFrame |
| **Polars** | 高性能大数据处理 |
| **NumPy** | 数值计算、矩阵运算 |
| **yt-dlp** | 视频/音频下载 |
| **ffmpeg** | 音视频处理 |
| **whisper** | 语音转文字 |
| **Playwright** | 浏览器自动化 |
| **requests/httpx** | HTTP 客户端 |

### Toolbox 调用示例

#### 执行命令
```json
{
  "action": "toolbox",
  "toolbox_action": "command",
  "command": "ls -la /opt/agent-workspace/data"
}
```

#### 运行 Python 脚本
```json
{
  "action": "toolbox",
  "toolbox_action": "script",
  "script_type": "python",
  "script": "import pandas as pd\ndf = pd.DataFrame({'a':[1,2,3]})\nprint(df)"
}
```

#### 安装软件包
```json
{
  "action": "toolbox",
  "toolbox_action": "install",
  "package": "jq",
  "package_manager": "apt"
}
```

#### 网页截图
```json
{
  "action": "toolbox",
  "toolbox_action": "playwright",
  "playwright_action": "screenshot",
  "url": "https://example.com",
  "output_path": "/opt/agent-workspace/data/shot.png"
}
```

#### 写入文件
```json
{
  "action": "toolbox",
  "toolbox_action": "file_write",
  "file_path": "/opt/agent-workspace/scripts/myjob.py",
  "file_content": "print('hello')"
}
```

#### 工具箱状态
```json
{
  "action": "toolbox",
  "toolbox_action": "status"
}
```

## 容器管理（action=exec/run/logs/ps/stop/inspect/pull/cleanup）

### 在已有容器内执行命令

```json
{
  "action": "exec",
  "container": "agent-toolbox",
  "command": "python3 /opt/agent-workspace/scripts/train.py",
  "background": true
}
```

### 查看日志

```json
{
  "action": "logs",
  "log_path": "/tmp/train.log",
  "tail": 30,
  "grep": "Epoch|fold|Error"
}
```

### 查看进程

```json
{
  "action": "ps",
  "grep": "train"
}
```

### 启动新沙箱容器（需要隔离时才用）

```json
{
  "action": "run",
  "image": "python:3.11-slim",
  "command": "python -c 'print(1+1)'",
  "mem_limit": "1g",
  "cpus": 1,
  "timeout": 300
}
```

## 什么时候用哪个

- 数据清洗/ETL → `action=toolbox, toolbox_action=script, script_type=python`
- 网页截图/爬虫 → `action=toolbox, toolbox_action=playwright`
- 发钉钉通知 → `action=toolbox, toolbox_action=command, command="python3 /opt/agent-workspace/scripts/notify.py"`
- 视频转录 → `action=toolbox, toolbox_action=script, script_type=bash, script="yt-dlp ... && whisper ..."`
- 进入已有容器执行 → `action=exec, container=容器名, command="..."`
- 隔离沙箱运行 → `action=run, image=...`（需要完全独立环境）
- 查看容器日志 → `action=logs, container=容器名`
- 管理容器生命周期 → `action=stop/inspect/cleanup`

## 后台任务（background=true）与 async_task 配合

启动后台长任务时加 `"background": true`，脚本末尾必须写入 `.done` 文件：

```bash
# 脚本末尾固定写法
echo $? > /opt/agent-workspace/logs/bg_<task>.log.done
```

后台任务返回 `task_id`，之后用 `async_task` skill 查询进度：

```
docker_operator(toolbox, background=true) → 返回 task_id
    ↓ 用户问"跑完了吗"
async_task(query, task_id) → 查看状态/日志尾部
    ↓ 任务完成后
TaskMonitor 自动检测 .done 文件 → 回调 Agent 通知用户
```

**视觉分析脚本注意**：在 toolbox 内调用视觉 API 时直接读取环境变量：
```python
import os
api_base = os.environ["VISION_API_BASE_URL"]   # http://192.168.1.245:32520/litellm
api_key  = os.environ["VISION_API_KEY"]
model    = os.environ["VISION_MODEL"]
```
