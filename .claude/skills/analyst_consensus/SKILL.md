---
name: analyst_consensus
display_name: 券商一致预期
readonly: true
description: |
  【功能】聚合券商分析师评级和盈利预测
  【数据源】券商研报数据、分析师预测数据
  【输出数据】覆盖分析师数、评级分布(买入/增持/中性/减持)、一致目标价、上涨空间(%)、EPS/营收预测、置信度
  【耗时】~5-15秒
  【适用场景】用户问"分析师怎么看"、"目标价多少"、"券商评级"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/analyst_consensus/scripts/analyst_consensus.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/analyst_consensus/scripts/analyst_consensus.py <<'EOF'
{...json参数...}
EOF
```

# 券商一致预期分析技能

获取券商分析师对股票的盈利预测数据，聚合评级分布，分析目标价共识，生成投资置信度信号。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 是 | 股票代码，如 600519.SH |

## 调用示例

```json
{
  "ts_code": "600519.SH"
}
```
