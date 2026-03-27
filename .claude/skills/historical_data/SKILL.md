---
name: historical_data
display_name: 历史行情数据
readonly: true
description: |
  【功能】获取股票历史行情数据（日线/周线/月线），含 OHLCV 和涨跌幅
  【数据源】AkShare 历史行情接口（前复权）
  【输出数据】日期、开高低收、成交量、成交额、涨跌幅的时间序列数据
  【耗时】~5-15秒
  【适用场景】用户问"历史走势"、"近期涨跌"、"K线数据"时使用，也为技术指标计算提供基础数据
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/historical_data/scripts/historical_data.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/historical_data/scripts/historical_data.py <<'EOF'
{...json参数...}
EOF
```

# 历史行情数据技能

获取股票日线/周线/月线历史行情数据，包含开高低收及成交量。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 是 | 股票代码，如 600519.SH |
| freq | string | 否 | 频率：D(日线)/W(周线)/M(月线)，默认 D |
| limit | number | 否 | 获取条数，默认 120 |
| start_date | string | 否 | 开始日期，格式 YYYYMMDD |
| end_date | string | 否 | 结束日期，格式 YYYYMMDD |

## 调用示例

```json
{
  "ts_code": "600519.SH",
  "freq": "D",
  "limit": 60,
  "start_date": "20260101",
  "end_date": "20260316"
}
```

## 输出示例

```json
{
  "ts_code": "600519.SH",
  "freq": "D",
  "count": 60,
  "data": [
    {
      "trade_date": "20240115",
      "date": "20240115",
      "open": 1850.0,
      "high": 1865.0,
      "low": 1845.0,
      "close": 1860.0,
      "vol": 8765432,
      "amount": 16234560000.0,
      "pct_chg": 1.23
    }
  ],
  "for_llm": {
    "ts_code": "600519.SH",
    "freq": "D",
    "count": 60,
    "latest_close": 1860.0,
    "latest_pct_chg": 1.23,
    "period": "20231116~20240115"
  }
}
```
