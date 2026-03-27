---
name: rating
display_name: 综合评级
readonly: true
description: |
  【功能】多维度综合评分，生成投资建议
  【数据源】基本面(ROE/营收)、技术面(趋势/指标)、估值(PE分位)、资金面(主力流入)
  【输出数据】总评分(0-100)、评级等级(A+/A/B+/B/C)、各维度得分、买卖建议
  【耗时】~20-60秒
  【适用场景】用户问"某股票评级"、"该买还是卖"、"综合分析"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/rating/scripts/rating.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/rating/scripts/rating.py <<'EOF'
{...json参数...}
EOF
```

# 综合评级技能

多维度综合评分，从基本面、技术面、估值面、资金面计算综合评分并生成投资建议。

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
