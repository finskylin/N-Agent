---
name: insider_trading
display_name: 股东/高管增减持分析
readonly: true
description: |
  【功能】追踪股东、高管、董监高增减持动态
  【数据源】上市公司公告、交易所披露数据
  【输出数据】交易笔数、净买入金额(万元)、净买入股数、增减持趋势、大额交易明细、内部人情绪判断
  【耗时】~5-15秒
  【适用场景】用户问"高管增持减持"、"大股东动向"、"内部人交易"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/insider_trading/scripts/insider_trading.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/insider_trading/scripts/insider_trading.py <<'EOF'
{...json参数...}
EOF
```

# 股东/高管增减持分析技能

获取股票内部人交易数据，分析增减持趋势，识别异常大额交易，生成投资信号。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 是 | 股票代码，如 600519.SH |
| months | number | 否 | 回溯月数，默认 6 |

## 调用示例

```json
{
  "ts_code": "600519.SH",
  "months": 10
}
```
