---
name: financial_statement
display_name: 财务报表
readonly: true
description: |
  【功能】三大报表（资产负债/利润/现金流）和关键财务指标
  【数据源】AKShare财务报表数据
  【输出数据】资产负债表、利润表、现金流量表、关键指标(ROE/毛利率等)
  【耗时】~5-20秒
  【适用场景】用户问"资产负债表"、"利润表"、"现金流"、"财务指标"时使用
priority: 86
ui_components:
  - component: data_table
    condition: 展示财务报表数据
    priority: 1
    data_hints:
      - has_ts_code
      - has_array
intents:
  - query
  - analyze
keywords:
  - 资产负债
  - 利润表
  - 现金流
  - 财务指标
  - 报表
  - ROE
  - 毛利率
time_estimates:
  default:
    min: 5
    max: 20
    desc: "财务报表查询"
authority: unknown
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/financial_statement/scripts/financial_statement.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/financial_statement/scripts/financial_statement.py <<'EOF'
{...json参数...}
EOF
```

# 财务报表技能

## 执行方式

- 使用 `python3` 直接执行 `scripts/*.py`
- 支持命令行参数或 stdin JSON 输入

## 概述
获取三大报表和关键财务指标数据。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 是 | 股票代码，如 600519.SH |
| statement_type | string | 否 | 报表类型: 资产负债表/利润表/现金流量表/综合，默认 综合 |
| periods | number | 否 | 获取的报告期数，默认 4 |

## 调用示例

```json
{
  "ts_code": "600519.SH",
  "statement_type": "利润表",
  "periods": 4
}
```
