---
name: equity_pledge
display_name: 股权质押
readonly: true
description: |
  【功能】股权质押比例和明细数据
  【数据源】AKShare股权质押数据
  【输出数据】质押比例、质押笔数、质押股数、无限售质押、有限售质押
  【耗时】~5-15秒
  【适用场景】用户问"股权质押"、"质押比例"、"质押风险"时使用
priority: 74
ui_components:
  - component: data_table
    condition: 展示股权质押数据
    priority: 1
    data_hints:
      - has_array
intents:
  - query
keywords:
  - 质押
  - 股权
  - 质押比例
time_estimates:
  default:
    min: 5
    max: 15
    desc: "股权质押查询"
authority: unknown
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/equity_pledge/scripts/equity_pledge.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/equity_pledge/scripts/equity_pledge.py <<'EOF'
{...json参数...}
EOF
```

# 股权质押技能

## 执行方式

- 使用 `python3` 直接执行 `scripts/*.py`
- 支持命令行参数或 stdin JSON 输入
- 不使用 `mcp__...` 工具名

## 概述
获取股权质押比例和明细数据。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 是 | 股票代码，如 000725.SZ |

## 调用示例

```json
{
  "symbol": "000725.SZ",
  "query": "京东方股权质押情况"
}
```
