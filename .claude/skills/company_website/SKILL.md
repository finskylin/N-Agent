---
name: company_website
description: "【功能】抓取上市公司官网信息和重大事件 【数据源】公司官网、巨潮资讯(备用) 【输出数据】公司简介、主营业务、产品服务、公司新闻列表、投资者关系文档(年报/季报PDF) 【耗时】~10-45秒 【适用场景】用户问'公司简介'、'主营业务'、'最新公告'、'年报下载'时使用"
readonly: true
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/company_website/scripts/company_website.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/company_website/scripts/company_website.py <<'EOF'
{...json参数...}
EOF
```


## Imported Metadata

- Original display name: `公司关键新闻信息`
- Original intents: `news`, `company_info`
- Original keywords: `新闻`, `公告`, `事件`, `官网`, `公司简介`, `产品服务`, `投资者关系`, `年报`, `季报`

# 公司关键新闻信息技能

## 概述

抓取上市公司的重大新闻，重大科研进展，突破，投资等重大事件，支持：

1. 公司重大新闻和公告
2. 科研进展和技术突破
3. 投资并购等重大事件
4. 投资者关系文档（年报、季报 PDF）

## 支持的内容类型

| 类型     | 说明          |
| -------- | ------------- |
| about    | 公司简介      |
| products | 产品服务      |
| news     | 公司新闻      |
| ir       | 投资者关系    |
| ir_docs  | 年报/季报 PDF |
| contact  | 联系方式      |

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| query | string | 否 | 用户查询内容，也可作为股票名称的备选来源 |
| ts_code | string | 否 | 股票代码，如 600519.SH |
| name | string | 否 | 公司名称，也可通过 query 传入 |
| user_intent | string | 否 | 用户意图描述，如"查看公司最新公告" |
| max_results | number | 否 | 最大返回结果数，默认 15 |

## 调用示例

```json
{
  "query": "贵州茅台最新公告和主营业务介绍",
  "ts_code": "600519.SH",
  "name": "贵州茅台",
  "content_type": "news",
  "user_intent": "查看公司最新公告和新闻"
}
```
