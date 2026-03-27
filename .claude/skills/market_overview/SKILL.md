---
name: market_overview
display_name: 市场概览
readonly: true
description: |
  【功能】获取A股市场整体行情概览，包括主要指数涨跌、市场宽度统计
  【数据源】AkShare 沪深指数实时行情接口
  【输出数据】上证指数/深证成指/创业板指实时数据、涨跌停家数、上涨/下跌/平盘家数统计
  【耗时】~5-10秒
  【适用场景】用户问"今天大盘怎么样"、"市场整体表现"、"指数行情"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/market_overview/scripts/market_overview.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/market_overview/scripts/market_overview.py <<'EOF'
{...json参数...}
EOF
```

# 市场概览技能

获取 A 股市场整体行情，包含主要指数及市场宽度统计。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| date | string | 否 | 日期，格式 YYYYMMDD，默认当日 |

## 调用示例

```json
{
  "date": "20260316"
}
```

## 输出示例

```json
{
  "date": "20240115",
  "indices": [
    {"name": "上证指数", "code": "000001", "price": 3050.5, "pct_chg": 0.85},
    {"name": "深证成指", "code": "399001", "price": 9820.3, "pct_chg": 1.2},
    {"name": "创业板指", "code": "399006", "price": 1980.5, "pct_chg": 1.5}
  ],
  "market_stats": {
    "up": 2500,
    "down": 1200,
    "flat": 300,
    "limit_up": 45,
    "limit_down": 8
  },
  "for_llm": {
    "date": "20240115",
    "sh_index": 3050.5,
    "sh_pct_chg": 0.85,
    "up_count": 2500,
    "down_count": 1200,
    "sentiment": "偏多"
  }
}
```
