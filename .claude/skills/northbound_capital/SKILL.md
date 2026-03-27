---
name: northbound_capital
display_name: 北向资金
readonly: true
description: |
  【功能】沪深港通资金流向和个股持股数据
  【数据源】AKShare沪深港通数据
  【输出数据】北向资金流入/流出、个股持股量/持股市值、持股比例变化
  【耗时】~5-15秒
  【适用场景】用户问"北向资金"、"沪股通/深股通"、"外资流入"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/northbound_capital/scripts/northbound_capital.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/northbound_capital/scripts/northbound_capital.py <<'EOF'
{...json参数...}
EOF
```

# 北向资金技能

沪深港通资金流向和个股持股数据。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 否 | 股票代码，如 600519.SH，不传则获取市场整体北向资金流向 |

## 调用示例

```json
{
  "ts_code": "600519.SH"
}
```
