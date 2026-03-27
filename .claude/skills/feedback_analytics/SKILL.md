---
name: feedback_analytics
display_name: 反馈统计分析
description: |
  【功能】查询用户对 Agent 回答的满意度统计，包含好评率、差评数、渠道分布、日期趋势和近期评论
  【数据源】MySQL v4_report_feedback 表
  【输出数据】总反馈数、好评数、差评数、好评率、按渠道统计、按日期统计、最近评论列表
  【耗时】~2-5秒
  【适用场景】用户问"反馈统计"、"用户满意度"、"好评率多少"、"最近差评"时使用
priority: 60
keywords:
  - 反馈统计
  - 用户满意度
  - 好评率
  - 差评
  - 好评
  - 反馈分析
  - 用户反馈
  - 满意度统计
  - 反馈趋势
intents:
  - feedback_analytics
  - user_satisfaction
  - rating_statistics
triggers:
  - pattern: "反馈统计|满意度|好评率|差评|反馈分析"
    description: 反馈统计触发词
time_estimates:
  default:
    min: 2
    max: 5
    desc: "反馈统计查询"
authority: internal
---
## 调用方式

通过 `bash` 工具执行：

```bash
echo '{"action":"stats"}' | python3 /app/.claude/skills/feedback_analytics/scripts/feedback_analytics.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/feedback_analytics/scripts/feedback_analytics.py <<'EOF'
{"action":"stats","days":30,"channel":"all"}
EOF
```

# 反馈统计分析技能

查询 Agent 回答的用户满意度统计数据。

## 脚本路径

`.claude/skills/feedback_analytics/scripts/feedback_analytics.py`

## 调用方式

```bash
# 查询近30天统计（默认）
python3 .claude/skills/feedback_analytics/scripts/feedback_analytics.py --action stats

# 查询指定日期范围
python3 .claude/skills/feedback_analytics/scripts/feedback_analytics.py --action stats --start-date 2026-01-01 --end-date 2026-03-11

# 按渠道筛选
python3 .claude/skills/feedback_analytics/scripts/feedback_analytics.py --action stats --channel dingtalk

# 新增反馈
python3 .claude/skills/feedback_analytics/scripts/feedback_analytics.py --action add_feedback --report-id REPORT_ID --rating like --comment "很有帮助"
```

也支持 stdin JSON：
```bash
echo '{"action":"stats","start_date":"2026-01-01","channel":"web"}' | python3 .claude/skills/feedback_analytics/scripts/feedback_analytics.py
```

## 输出格式

### stats 动作
```json
{
  "status": "success",
  "data": {
    "total": 120,
    "like_count": 95,
    "dislike_count": 25,
    "like_rate": 0.7917,
    "by_channel": {
      "dingtalk": {"total": 80, "like": 65, "dislike": 15, "like_rate": 0.8125},
      "web": {"total": 40, "like": 30, "dislike": 10, "like_rate": 0.75}
    },
    "by_date": [
      {"date": "2026-03-01", "like": 12, "dislike": 3}
    ],
    "recent_comments": [
      {"rating": "dislike", "comment": "回答不够准确", "channel": "dingtalk", "created_at": "2026-03-10T14:23:00"}
    ]
  }
}
```

### add_feedback 动作
```json
{
  "status": "success",
  "message": "反馈已保存"
}
```

## 参数说明

| 参数 | 类型 | 必填 | 描述 |
|------|------|------|------|
| `action` | string | 是 | `stats`（统计）或 `add_feedback`（新增反馈） |
| `start_date` | string | 否 | 起始日期 YYYY-MM-DD，默认近30天 |
| `end_date` | string | 否 | 截止日期 YYYY-MM-DD，默认今天 |
| `channel` | string | 否 | 渠道筛选 web/dingtalk，默认全部 |
| `report_id` | string | add_feedback时必填 | 报告ID |
| `rating` | string | add_feedback时必填 | like 或 dislike |
| `comment` | string | 否 | 用户评论文字 |

## 注意事项

- 需要正确配置数据库连接（DATABASE_URL 或 MYSQL_* 环境变量）
- 需要在 Agent 服务目录下执行（路径 `output_project/sthg_agent_service/`）

## 调用示例

```json
{
  "query": "最近7天用户满意度趋势",
  "days": 7,
  "channel": "all"
}
```
