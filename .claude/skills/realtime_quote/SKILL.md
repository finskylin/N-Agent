---
name: realtime_quote
display_name: 实时行情
readonly: true
description: |
  【功能】获取股票实时行情快照，含价格、涨跌幅、买卖盘、量价数据
  【数据源】新浪财经实时行情接口 + AkShare
  【输出数据】当前价、涨跌幅、开高低收、成交量、成交额、买一卖一价、五档盘口
  【耗时】~2-5秒
  【适用场景】用户问"现在股价多少"、"今天涨跌情况"、"实时行情"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/realtime_quote/scripts/realtime_quote.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/realtime_quote/scripts/realtime_quote.py <<'EOF'
{...json参数...}
EOF
```

# 实时行情技能

获取股票当前实时行情数据，包含价格、量价及盘口信息。

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

## 输出示例

```json
{
  "ts_code": "600519.SH",
  "name": "贵州茅台",
  "price": 1680.0,
  "pct_chg": 1.23,
  "open": 1660.0,
  "high": 1695.0,
  "low": 1655.0,
  "pre_close": 1659.6,
  "vol": 12345678,
  "amount": 2067000000.0,
  "for_llm": {
    "ts_code": "600519.SH",
    "name": "贵州茅台",
    "price": 1680.0,
    "pct_chg": 1.23,
    "vol_wan": 1234.57
  }
}
```
