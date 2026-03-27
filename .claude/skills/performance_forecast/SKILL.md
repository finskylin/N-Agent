---
name: performance_forecast
display_name: 业绩预告
readonly: true
description: |
  【功能】业绩预告、快报、报表和披露时间查询
  【数据源】AKShare业绩数据
  【输出数据】股票代码、业绩变动幅度、预告类型、预告内容
  【耗时】~5-15秒
  【适用场景】用户问"业绩预告"、"业绩快报"、"业绩报表"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/performance_forecast/scripts/performance_forecast.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/performance_forecast/scripts/performance_forecast.py <<'EOF'
{...json参数...}
EOF
```

# 业绩预告技能

获取业绩预告/快报/报表数据，分析公司业绩变动情况。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 是 | 股票代码，如 "600519.SH" |

## 调用示例

```json
{
  "ts_code": "600519.SH"
}
```
