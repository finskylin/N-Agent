---
name: financial_report
display_name: 财务报表
readonly: true
description: |
  【功能】获取股票财务报表摘要和关键指标
  【数据源】Tushare Pro财务数据API
  【输出数据】营业收入(亿元)、净利润(亿元)、ROE(%)、毛利率(%)、净利率(%)、EPS、同比增长率
  【耗时】~5-15秒
  【适用场景】用户问"某股票财报"、"营收利润"、"ROE多少"时使用
priority: 85
ui_components:
  - component: financial_panel
    condition: 展示财务报表面板
    priority: 1
    data_hints:
      - has_reports
  - component: data_table
    condition: 以表格形式展示财务数据
    priority: 2
    data_hints:
      - has_array
intents:
  - analyze
keywords:
  - 财务
  - 报表
  - 营收
  - 净利润
  - roe
  - 毛利率
  - 财报
time_estimates:
  default:
    min: 5
    max: 15
    desc: "财务报告获取"
authority: official_primary
key_params:
  - ts_code
  - report_type
cache_ttl: 86400
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/financial_report/scripts/financial_report.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/financial_report/scripts/financial_report.py <<'EOF'
{...json参数...}
EOF
```

# 财务报表技能

## 执行方式

- 使用 `python3` 直接执行 `scripts/*.py`
- 支持命令行参数或 stdin JSON 输入
- 不使用 `mcp__...` 工具名

## 概述

获取股票财务摘要和关键指标。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 是 | 股票代码，如 600519.SH |
| periods | number | 否 | 获取的报告期数，默认 8 |

## 用法

```bash
python scripts/financial_report.py --ts_code 600519.SH --periods 8
```

## 输出字段

| 字段         | 说明         |
| ------------ | ------------ |
| revenue      | 营业收入     |
| net_profit   | 净利润       |
| roe          | 净资产收益率 |
| gross_margin | 毛利率       |
| net_margin   | 净利率       |

## 输出示例

```json
{
  "ts_code": "600519.SH",
  "reports": [
    {
      "end_date": "20241231",
      "revenue": 1200.5,
      "revenue_yoy": 15.2,
      "net_profit": 600.2,
      "profit_yoy": 18.5,
      "roe": 32.5,
      "gross_margin": 92.3,
      "eps": 48.5
    }
  ]
}
```

## 调用示例

```json
{
  "symbol": "600519.SH",
  "query": "贵州茅台最近几期财务指标"
}
```
