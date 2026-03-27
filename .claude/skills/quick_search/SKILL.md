---
name: quick_search
display_name: 快速搜索
readonly: true
description: |
  轻量搜索引擎查询，返回搜索结果列表（标题、链接、摘要、来源、日期）。支持多引擎并行搜索，不抓取详情页。
  【适用场景】迭代搜索：搜完看结果，不满意换关键词再搜。
  【不适用】已有具体URL需要抓取内容（用 url_fetch）。
priority: 85
intents:
  - search
  - 搜索
  - 查询
keywords:
  - 搜索
  - search
  - 查询
  - 查找
ui_components:
  - component: dynamic_card
    priority: 3
    data_hints:
      - has_results
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/quick_search/scripts/quick_search.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/quick_search/scripts/quick_search.py <<'EOF'
{...json参数...}
EOF
```


## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| query | string | 是 | 搜索关键词 |
| max_results | integer | 否 | 返回结果数量，默认 10 |
| search_timeout | integer | 否 | 搜索引擎等待超时秒数，默认 5，**最大 15**。结果不足时重试可设为 15，禁止超过 15 |

## 返回格式
```json
{
  "for_llm": {
    "query": "搜索词",
    "total_results": 10,
    "results": [
      {"title": "...", "link": "...", "snippet": "...", "source": "...", "date": "..."}
    ]
  }
}
```

## 调用示例

```json
{
  "query": "贵州茅台2026年一季度业绩",
  "max_results": 10,
  "search_timeout": 10
}
```
