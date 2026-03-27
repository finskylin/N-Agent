---
name: stock_query
display_name: 股票基础信息查询
readonly: true
description: |
  【功能】根据股票代码查询基础信息和实时行情
  【数据源】AkShare 个股信息接口 + 新浪财经实时行情
  【输出数据】股票名称、行业、地域、实时价格、涨跌幅、PE/PB、市值
  【耗时】~3-8秒
  【适用场景】用户问"XX股票的基本信息"、"XX是什么行业"、"当前股价"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/stock_query/scripts/stock_query.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/stock_query/scripts/stock_query.py <<'EOF'
{...json参数...}
EOF
```

# 股票基础信息查询技能

根据股票代码或名称查询基础信息和实时行情。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 是 | 股票代码，如 "600519.SH" |
| query | string | 否 | 查询描述，如 "贵州茅台基本信息" |

## 调用示例

```json
{
  "ts_code": "600519.SH",
  "query": "贵州茅台基本信息和当前股价"
}
```

## 输出示例

```json
{
  "ts_code": "600519.SH",
  "name": "贵州茅台",
  "industry": "白酒",
  "area": "贵州",
  "price": 1680.0,
  "pct_chg": 1.23,
  "pe_ttm": 28.5,
  "pb": 8.2,
  "total_mv": 21000000000,
  "for_llm": {
    "ts_code": "600519.SH",
    "name": "贵州茅台",
    "industry": "白酒",
    "price": 1680.0,
    "pct_chg": 1.23
  }
}
```
