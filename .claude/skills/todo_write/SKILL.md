---
name: todo_write
display_name: 任务清单
description: |
  创建和管理本次任务的执行清单（TodoList），支持实时更新每个步骤的状态。

  【必须调用时机】
  ① 复杂任务开始前：涉及 ≥3 个独立步骤时，先调用 action=write 列出所有步骤，再开始执行
  ② 每个步骤开始时：调用 action=update 将该步骤状态改为 in_progress
  ③ 每个步骤完成时：调用 action=update 将该步骤状态改为 completed
  ④ 用户问"进度怎么样"、"做到哪了"时：调用 action=read 展示当前清单

  【不适用】简单单步任务、闲聊、不需要追踪进度的场景。
readonly: false
---

## todo_write — 任务清单管理

### 调用方式

```bash
python3 /app/.claude/skills/todo_write/scripts/todo_write.py <<'EOF'
{...json参数...}
EOF
```

---

## 参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `action` | string | 是 | `write` / `update` / `read` / `clear` |
| `session_id` | string | 是 | 当前会话 ID（用于隔离不同任务的清单） |
| `todos` | array | action=write 时必填 | todo 列表，见下方结构 |
| `todo_id` | string | action=update 时必填 | 要更新的 todo ID |
| `status` | string | action=update 时必填 | `pending` / `in_progress` / `completed` / `failed` |
| `note` | string | 否 | 更新时附加备注（如失败原因） |

### Todo 对象结构（action=write 时）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 | 唯一标识，如 `step_1`、`fetch_data` |
| `content` | string | 是 | 步骤描述 |
| `status` | string | 否 | 初始状态，默认 `pending` |

---

## 使用规范

### 正确的执行模式

```
1. 任务开始 → action=write 写入全部步骤（状态均为 pending）
2. 执行步骤1前 → action=update, status=in_progress
3. 步骤1完成后 → action=update, status=completed
4. 执行步骤2前 → action=update, status=in_progress
5. 步骤2完成后 → action=update, status=completed
... 依此类推
```

**写入 todo 后立即继续执行，不要等待用户确认。**

---

## 调用示例

### 开始任务，写入清单
```json
{
  "action": "write",
  "session_id": "dingtalk_xxx_123",
  "todos": [
    {"id": "step_1", "content": "获取国盾量子实时行情"},
    {"id": "step_2", "content": "分析技术指标（MACD/KDJ/RSI）"},
    {"id": "step_3", "content": "获取主力资金流向"},
    {"id": "step_4", "content": "运行 AI 预测模型"},
    {"id": "step_5", "content": "生成综合分析报告"}
  ]
}
```

### 步骤开始执行
```json
{
  "action": "update",
  "session_id": "dingtalk_xxx_123",
  "todo_id": "step_1",
  "status": "in_progress"
}
```

### 步骤完成
```json
{
  "action": "update",
  "session_id": "dingtalk_xxx_123",
  "todo_id": "step_1",
  "status": "completed"
}
```

### 步骤失败（附原因）
```json
{
  "action": "update",
  "session_id": "dingtalk_xxx_123",
  "todo_id": "step_3",
  "status": "failed",
  "note": "API 超时，已跳过"
}
```

### 读取当前清单
```json
{
  "action": "read",
  "session_id": "dingtalk_xxx_123"
}
```

### 清除清单（任务结束后）
```json
{
  "action": "clear",
  "session_id": "dingtalk_xxx_123"
}
```

---

## 返回格式

```json
{
  "action": "write",
  "total": 5,
  "todos": [
    {"id": "step_1", "content": "获取国盾量子实时行情", "status": "pending", "note": ""},
    {"id": "step_2", "content": "分析技术指标", "status": "pending", "note": ""},
    ...
  ],
  "summary": "已创建 5 个任务步骤，开始执行..."
}
```

状态说明：
- `pending` — 待执行 ⏳
- `in_progress` — 执行中 🔧
- `completed` — 已完成 ✅
- `failed` — 失败 ❌
