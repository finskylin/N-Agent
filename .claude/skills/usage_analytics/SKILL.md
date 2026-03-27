---
name: usage_analytics
display_name: 使用量统计分析
readonly: true
description: |
  【功能】统计 Agent 使用情况，包括：
  - 时间段内总使用量、每日趋势
  - 指定用户的使用量和活跃时间
  - 问答耗时（平均/最长/最短/分布）
  - 使用的具体工具列表及调用频次排名
  - 用户反馈统计（好评/差评/评分标签）
  【数据源】v4_skill_outputs 表（skill 执行记录）+ v4_report_feedback 表（反馈）
  【耗时】~2-5秒
  【适用场景】管理员问"最近使用量"、"哪个工具用得最多"、"用户满意度"、"某用户用了多少次"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/usage_analytics/scripts/usage_analytics.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/usage_analytics/scripts/usage_analytics.py <<'EOF'
{...json参数...}
EOF
```


## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| start_date | string | 否 | 查询开始日期，格式 YYYY-MM-DD（默认最近7天） |
| end_date | string | 否 | 查询结束日期，格式 YYYY-MM-DD（默认今天） |
| user_id | integer | 否 | 指定用户ID，不填则统计所有用户 |
| metrics | array | 否 | 要统计的指标，不填则返回全部：overview=总览 daily_trend=每日趋势 user_ranking=用户排行 tool_ranking=工具排行 latency=耗时分析 feedback=反馈统计；枚举值: overview/daily_trend/user_ranking/tool_ranking/latency/feedback |
| top_n | integer | 否 | 排行榜返回条数，默认10 |

## 返回格式

```json
{
  "period": {"start": "2026-03-10", "end": "2026-03-17"},
  "overview": {
    "total_requests": 120,
    "unique_users": 8,
    "total_tool_calls": 450,
    "success_rate": 0.95
  },
  "daily_trend": [
    {"date": "2026-03-10", "requests": 15, "tool_calls": 52}
  ],
  "user_ranking": [
    {"user_id": 1000000001, "requests": 40, "tool_calls": 150, "avg_duration_ms": 8200}
  ],
  "tool_ranking": [
    {"tool_name": "quick_search", "calls": 80, "success_rate": 0.98, "avg_duration_ms": 3200}
  ],
  "latency": {
    "avg_ms": 8500, "max_ms": 45000, "min_ms": 1200, "p50_ms": 7000, "p90_ms": 20000
  },
  "feedback": {
    "total": 30, "likes": 25, "dislikes": 5, "like_rate": 0.83,
    "top_tags": [["数据准确", 12], ["分析深入", 8]]
  }
}
```
