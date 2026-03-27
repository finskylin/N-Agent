---
name: minute_kline
display_name: 分钟K线数据
readonly: true
description: |
  【功能】获取个股分时/分钟级别K线数据，支持1/5/15/30/60分钟周期
  【数据源】AKShare（stock_zh_a_hist_min_em）
  【输出数据】时间、开盘价、收盘价、最高价、最低价、成交量、成交额、涨跌幅
  【耗时】~3s
  【适用场景】用户问"分时走势"、"分钟K线"、"日内行情"、"盘中走势"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/minute_kline/scripts/minute_kline.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/minute_kline/scripts/minute_kline.py <<'EOF'
{...json参数...}
EOF
```

# 分钟K线数据技能

获取个股分时/分钟级别K线数据，支持多种周期，分析日内趋势和成交量变化模式。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 是 | 股票代码，如 600519.SH |
| period | string | 否 | K线周期：1/5/15/30/60（分钟），默认 5 |
| start_date | string | 否 | 开始日期，格式 YYYYMMDD |
| end_date | string | 否 | 结束日期，格式 YYYYMMDD |

## 调用示例

```json
{
  "ts_code": "600519.SH",
  "period": "5"
}
```
