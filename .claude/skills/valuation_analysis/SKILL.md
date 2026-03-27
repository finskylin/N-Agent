---
name: valuation_analysis
display_name: 估值分析
readonly: true
description: |
  【功能】多维度估值分析，判断估值高低
  【数据源】历史PE/PB数据、行业对比数据、财务数据
  【输出数据】PE/PB/PS当前值、5年历史分位(%)、行业对比排名、DCF合理价值、估值结论(高估/合理/低估)
  【耗时】~10-30秒
  【适用场景】用户问"某股票贵不贵"、"PE/PB分位"、"估值分析"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/valuation_analysis/scripts/valuation_analysis.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/valuation_analysis/scripts/valuation_analysis.py <<'EOF'
{...json参数...}
EOF
```

# 估值分析技能

多维度估值分析，包括绝对估值和相对估值，判断当前股价是否合理。

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
