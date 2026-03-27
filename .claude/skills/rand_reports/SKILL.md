---
name: rand_reports
readonly: true
description: "【功能】搜索RAND智库研究报告，返回报告标题、摘要、作者、发布日期和链接 【数据源】RAND Corporation官网搜索API 【输出数据】报告列表(标题/摘要/作者/日期/PDF链接/主题标签) 【耗时】~10-30秒 【适用场景】用户问'RAND报告'、'美国智库分析'、'地缘政治研究报告'时使用"
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/rand_reports/scripts/rand_reports.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/rand_reports/scripts/rand_reports.py <<'EOF'
{...json参数...}
EOF
```


# RAND 智库报告搜索

## 概述
搜索 RAND Corporation 智库发布的研究报告，涵盖国防、安全、外交政策、科技、经济等领域。

## 数据源
- RAND Corporation 官网搜索 API (https://www.rand.org/search.html)
- RAND 全文搜索 API (https://api.rand.org/search)

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| query | string | 是 | 搜索关键词 |
| limit | integer | 否 | 返回结果数量，默认 10，最大 50 |
| year_from | integer | 否 | 起始年份过滤 |
| year_to | integer | 否 | 结束年份过滤 |

## 调用示例

```json
{
  "query": "China military strategy Taiwan strait",
  "limit": 10,
  "year_from": 2022,
  "year_to": 2026
}
```
