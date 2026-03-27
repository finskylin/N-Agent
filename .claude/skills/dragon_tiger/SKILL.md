---
name: dragon_tiger
display_name: 龙虎榜
readonly: true
description: |
  【功能】龙虎榜详情和营业部排名
  【数据源】AKShare龙虎榜数据
  【输出数据】上榜股票、买入/卖出金额、营业部名称、机构净买入
  【耗时】~5-15秒
  【适用场景】用户问"龙虎榜"、"机构买入"、"游资动向"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/dragon_tiger/scripts/dragon_tiger.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/dragon_tiger/scripts/dragon_tiger.py <<'EOF'
{...json参数...}
EOF
```

# 龙虎榜技能

获取龙虎榜详情和营业部排名，分析机构和游资动向。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 是 | 股票代码，如 600519.SH |
| date | string | 否 | 查询日期，格式 YYYYMMDD，默认最近交易日 |

## 调用示例

```json
{
  "ts_code": "600519.SH",
  "date": "20260316"
}
```
