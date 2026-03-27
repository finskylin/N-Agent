---
name: cron
display_name: 定时任务
description: |
  【功能】创建、查看、删除应用内定时任务，支持周期执行和一次性定时。
  【适用场景】用户要求”每天/每周/定时/自动发送/周期执行/定时提醒”时使用。
  【不适用】立即执行的任务（用 docker_operator）、后台任务查询（用 async_task）。
priority: 90
keywords:
  - 定时任务
  - cron
  - 每天
  - 每周
  - 自动发送
  - 周期执行
time_estimates:
  default:
    min: 1
    max: 5
    desc: "定时任务配置"
authority: medium
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/cron/scripts/cron.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/cron/scripts/cron.py <<'EOF'
{...json参数...}
EOF
```

# 定时任务技能

## 执行方式

- 使用 `python3` 直接执行 `scripts/cron.py`
- 支持命令行参数或 stdin JSON 输入
- 不依赖系统 `crontab`

## 能力

`cron` 可以：

- 创建一次性任务
- 创建固定间隔任务
- 创建基于 5 段 cron 表达式的周期任务
- 列出当前任务
- 删除任务
- 保存钉钉回调上下文，触发时重新执行 agent

`cron` 不可以：

- 不负责搜索或分析内容
- 不负责自己推断消息发送渠道
- 不需要写 shell 脚本

## 什么时候用

- 用户说“每天 9 点发我一份”
- 用户说“每周一自动执行”
- 用户说“30 分钟跑一次”
- 用户说“明天上午 10 点提醒我”

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| action | string | 是 | 操作类型: add/list/remove |
| message | string | add 时必填 | 到点后真正要执行的任务描述 |
| job_name | string | 否 | 任务名称，add 时建议提供 |
| job_id | string | remove 时可选 | 要删除的任务 ID |
| cron_expr | string | 否 | 5 段 cron 表达式（周期任务，三种调度方式之一） |
| every_seconds | number | 否 | 固定间隔秒数（三种调度方式之一） |
| at | string | 否 | 一次性执行时间，格式 ISO 8601（三种调度方式之一） |
| timezone | string | 否 | 时区，如 Asia/Shanghai |
| callback | object | 否 | 回调上下文（钉钉/飞书会话信息） |
| session_id | string | 否 | 会话 ID，用于会话复用 |
| delete_after_run | boolean | 否 | 执行后是否自动删除，默认 false |
| auto_approve_plan | boolean | 否 | 是否自动批准执行计划，默认 true |
| alert_threshold | string | 否 | 告警阈值 |
| silent_if_no_signal | boolean | 否 | 无信号时是否静默，默认 false |

## 添加任务

`action=add` 时，必须且只能提供一种调度方式：

- `every_seconds`
- `cron_expr`
- `at`

同时建议提供：

- `message`: 到点后真正要执行的任务描述
- `job_name`: 任务名
- `callback`: 回调上下文；如果是钉钉，直接传完整会话信息
- `session_id`: 可选，会话复用

### 推荐：周期任务

```json
{
  "action": "add",
  "job_name": "daily_market_brief",
  "message": "每天早上整理一份市场简报并发送给我",
  "cron_expr": "0 9 * * 1-5",
  "timezone": "Asia/Shanghai",
  "callback": {
    "channel": "dingtalk",
    "sender_id": "user_xxx",
    "staff_id": "manager01",
    "sender_nick": "张三",
    "conversation_id": "cid_xxx",
    "conversation_type": "2",
    "robot_code": "dingxxxx"
  }
}
```

### 推荐：固定间隔

```json
{
  "action": "add",
  "job_name": "half_hour_watch",
  "message": "每 30 分钟检查一次目标站点状态并发我结果",
  "every_seconds": 1800
}
```

### 推荐：一次性任务

```json
{
  "action": "add",
  "job_name": "tomorrow_reminder",
  "message": "明天 10 点提醒我查看报告",
  "at": "2026-03-13T10:00:00"
}
```

## 查看和删除

列出任务：

```json
{"action": "list"}
```

删除任务：

```json
{"action": "remove", "job_id": "abc123def456"}
```

也可以按名称删除：

```json
{"action": "remove", "job_name": "daily_market_brief"}
```

## 使用要求

- 创建任务时，`message` 必须写成触发后要执行的完整指令
- 如果用户要求定时把结果发回钉钉，必须把 `callback` 一起传入
- 周期任务优先使用 `cron_expr`
- 单次任务优先使用 `at`
- 不要再调用旧的定时实现方式

## 调用示例

```json
{
  "action": "add",
  "job_name": "daily_market_brief",
  "message": "查询A股市场今日概况、涨跌停家数，并通过钉钉发送给我",
  "cron_expr": "0 9 * * 1-5",
  "timezone": "Asia/Shanghai",
  "callback": {
    "channel": "dingtalk",
    "conversation_id": "cid_xxx",
    "robot_code": "dingxxxx"
  }
}
```
