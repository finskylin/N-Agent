---
name: money_flow
display_name: 资金流向分析
readonly: true
description: |
  【功能】获取个股实时资金流向数据，支持主力资金、北向资金、融资融券多维度分析
  【数据源】东方财富网API(实时) + AkShare(备用) + 北向持股 + 融资融券
  【输出数据】每日主力/大单/小单净流入(万元)、3/5/10/20日汇总、北向持股数据、融资余额、资金行为分析
  【耗时】~5-15秒
  【适用场景】用户问"主力资金"、"资金流向"、"北向资金"、"融资融券"、"最新资金"时使用
  【优化】2026-03-19 优化数据源，解决数据滞后问题，新增数据时效性标注
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/money_flow/scripts/money_flow.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/money_flow/scripts/money_flow.py <<'EOF'
{...json参数...}
EOF
```

# 资金流向分析技能

获取个股实时资金流向数据，支持主力资金、北向资金、融资融券多维度分析。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 是 | 股票代码，格式如 688027.SH 或 000001.SZ |
| days | integer | 否 | 分析最近 N 天数据，默认 30 |

## 优化内容

1. **实时数据源**：优先使用东方财富API获取实时数据
2. **多数据源降级**：主API -> 历史API -> AkShare备用
3. **数据时效性标注**：明确标注数据质量（实时/延迟/严重滞后）
4. **单位标准化**：统一输出单位（万元为主，汇总用亿元）

## 调用示例

```json
{
  "ts_code": "000988.SZ",
  "days": 20
}
```

## 输出示例

```json
{
  "ts_code": "000988.SZ",
  "data_source": "eastmoney_history_api",
  "data_quality": "实时",
  "data_update_time": "2026-03-19 09:12:27",
  "summary": {
    "main_net_3d": -39.45,
    "main_net_5d": -86.54,
    "main_net_10d": -87.28,
    "main_trend": "流出放缓",
    "latest_day_net": 0.4397,
    "latest_day_date": "2026-03-18"
  },
  "daily": [...],
  "for_llm": {
    "main_net_5d_yi": -86.54,
    "latest_day_net_yi": 0.4397,
    "main_trend": "流出放缓",
    "data_quality": "实时"
  }
}
```

## 数据质量等级

| 等级 | 来源 | 时效性 | 说明 |
|------|------|--------|------|
| **实时** | 东方财富盘中API | 当日分钟级累计 | 最佳数据源，盘中/收盘后均可用 |
| **当日** | 东方财富日线API | 当日收盘数据 | 盘后数据，准确 |
| **T+1** | 东方财富日线API | 昨日数据 | 次日凌晨前可能滞后一天 |
| **延迟** | AkShare | 1-2日延迟 | 备用数据源 |
| **严重滞后** | AkShare | 历史数据 | 需提示用户 |

## 字段说明

| 字段 | 单位 | 说明 |
|------|------|------|
| main_net | 万元 | 主力净流入 |
| main_net_pct | % | 主力净占比 |
| super_large_net | 万元 | 超大单净流入 |
| large_net | 万元 | 大单净流入 |
| medium_net | 万元 | 中单净流入 |
| small_net | 万元 | 小单净流入 |
| main_net_5d_yi | 亿元 | 5日主力净流入汇总 |
