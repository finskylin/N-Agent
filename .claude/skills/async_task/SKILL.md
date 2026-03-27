---
name: async_task
display_name: 后台任务管理
description: |
  【功能】查询和管理后台异步任务的状态、进度和日志。
  【适用场景】用户问"任务跑完了吗"、"进度怎么样"、docker_operator启动后台任务后查询状态、列出所有运行中任务。
  【不适用】创建新任务（用 docker_operator）、定时任务（用 cron）。
priority: 60
readonly: true
keywords:
  - 任务进度
  - 后台任务
  - 任务状态
  - 跑完了吗
  - 进度
  - task_id
  - 异步任务
  - 后台运行
intents:
  - task_query
  - task_status
  - async_task_management
time_estimates:
  default:
    min: 1
    max: 5
    desc: "查询任务状态"
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/async_task/scripts/async_task.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/async_task/scripts/async_task.py <<'EOF'
{...json参数...}
EOF
```

# async_task — 后台任务管理

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| action | string | 是 | `query`/`list`/`cancel` |
| task_id | string | action=query/cancel 时必填 | docker_operator 返回的 task_id |
| status | string | 否 | 过滤状态：`running`/`completed`/`failed`/`cancelled` |
| limit | int | 否 | list 时最多返回条数，默认 20 |

## action 说明

| action | 说明 |
|--------|------|
| `query` | 查询单个任务详情（状态、日志尾部、退出码） |
| `list` | 列出所有任务，可按 status 过滤 |
| `cancel` | 取消/删除任务记录 |

## 调用示例

### 查询任务进度
```json
{"action": "query", "task_id": "task_abc123"}
```

### 列出所有运行中任务
```json
{"action": "list", "status": "running"}
```

### 列出最近所有任务
```json
{"action": "list"}
```

### 取消任务
```json
{"action": "cancel", "task_id": "task_abc123"}
```

## 返回格式

```json
{
  "for_llm": {
    "task_id": "task_abc123",
    "name": "数据清洗任务",
    "status": "completed",
    "exit_code": 0,
    "log_tail": "处理完成，共处理 1000 行",
    "created_at": "2026-03-24T10:00:00",
    "elapsed_seconds": 45
  }
}
```

状态说明：
- `running` — 正在执行
- `completed` — 已完成（exit_code=0 为成功）
- `failed` — 失败（exit_code 非 0）
- `cancelled` — 已取消

## 与 docker_operator 的配合

1. 用 `docker_operator` 启动后台任务，返回 `task_id`
2. 用 `async_task` + `task_id` 查询进度
3. 任务完成后 TaskMonitor 会自动回调 Agent，无需手动轮询

```
docker_operator(background=true) → 返回 task_id
    ↓
async_task(query, task_id) → 查询状态/日志
    ↓
TaskMonitor 检测 .done 文件 → 自动触发 Agent 回调
```
