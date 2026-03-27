---
name: stock_resolver
display_name: 股票代码解析
readonly: true
description: |
  【功能】将股票名称或模糊查询解析为标准股票代码（ts_code）
  【数据源】新浪财经 Suggest 接口 + AkShare 股票列表
  【输出数据】ts_code（如 688027.SH）、股票名称、市场类型、匹配列表
  【耗时】~2-5秒
  【适用场景】用户提供股票名称而非代码时使用，如"茅台"→"600519.SH"
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/stock_resolver/scripts/stock_resolver.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/stock_resolver/scripts/stock_resolver.py <<'EOF'
{...json参数...}
EOF
```

# 股票代码解析技能

将用户提供的股票名称或关键词解析为标准 ts_code 格式。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| query | string | 是 | 股票名称或关键词，如 "贵州茅台" |

## 调用示例

```json
{
  "query": "贵州茅台"
}
```

## 输出示例

```json
{
  "best_match": {
    "ts_code": "600519.SH",
    "name": "贵州茅台",
    "market": "SH"
  },
  "matches": [
    {"ts_code": "600519.SH", "name": "贵州茅台", "market": "SH"}
  ],
  "for_llm": {
    "ts_code": "600519.SH",
    "name": "贵州茅台",
    "query": "茅台"
  }
}
```
