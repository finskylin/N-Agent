---
name: intraday_tick
display_name: 日内逐笔
readonly: true
description: |
  【功能】日内逐笔成交数据，追踪大单动向
  【数据源】AKShare逐笔成交数据
  【输出数据】成交时间、价格、手数、方向(买/卖)、成交额
  【耗时】~5-15秒
  【适用场景】用户问"逐笔成交"、"大单追踪"、"成交明细"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/intraday_tick/scripts/intraday_tick.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/intraday_tick/scripts/intraday_tick.py <<'EOF'
{...json参数...}
EOF
```

# 日内逐笔技能

获取日内逐笔成交数据，追踪大单动向。

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
