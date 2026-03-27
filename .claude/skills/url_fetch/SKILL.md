---
name: url_fetch
display_name: 网页内容抓取
readonly: true
description: |
  抓取指定 URL 的网页全文内容，支持单个或多个 URL，能处理 JS 动态渲染页面。
  【适用场景】用户提供了具体链接需要获取内容时、搜索结果需要深入阅读详情页时。
  【不适用】搜索引擎查询（用 quick_search）、文档文件解析（用 document_reader）。
priority: 85
intents:
  - fetch
  - url
  - 抓取
  - 网页内容
keywords:
  - 抓取
  - 链接
  - 网页
  - URL
ui_components:
  - component: dynamic_card
    priority: 3
    data_hints:
      - has_array
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/url_fetch/scripts/url_fetch.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/url_fetch/scripts/url_fetch.py <<'EOF'
{...json参数...}
EOF
```


## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| urls | string | 是 | 要抓取的 URL，支持多个 URL（逗号、空格或换行分隔） |
| render_wait | integer | 否 | 页面渲染等待秒数，默认 3，**最大 8**。内容不足时重试可设为 8，禁止超过 8（单 URL 总超时 = render_wait + 6，上限 15s） |

## 返回格式
```json
{
  "for_llm": {
    "fetched_pages": [
      {"url": "...", "title": "...", "content": "...(最多8000字)", "word_count": 1234, "fetch_success": true}
    ],
    "total_fetched": 3,
    "failed_urls": ["..."]
  }
}
```

## 调用示例

```json
{
  "urls": "https://www.eastmoney.com/a/202503151234.html,https://finance.sina.com.cn/stock/relnews/cn/2026-03-15/doc-abc.shtml",
  "render_wait": 3
}
```
