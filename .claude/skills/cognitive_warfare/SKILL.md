---
name: cognitive_warfare
readonly: true
description: |
  【功能】追踪国际媒体报道情感变化，分析舆论博弈态势和认知域对抗。
  【输出】情感时间线、代表性文章列表、舆论转折点、认知域博弈指数、多国对比。
  【适用场景】用户问"某国舆论态势"、"媒体偏见分析"、"国际舆论战"时使用。
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/cognitive_warfare/scripts/cognitive_warfare.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/cognitive_warfare/scripts/cognitive_warfare.py <<'EOF'
{...json参数...}
EOF
```


# 认知域舆论博弈分析

## 概述
基于 GDELT 情感量化引擎，追踪国际媒体对特定国家或话题的报道情感值变化趋势。

## 数据源
- GDELT DOC API (tonechart) — 情感值时间序列
- GDELT DOC API (artlist) — 代表性文章列表
- YouTube Data API v3 — 视频叙事搜索（需 YOUTUBE_API_KEY）

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| country | string | 是 | 目标国家名称（中文），也可用 query 代替 |
| topic | string | 否 | 关注话题，如"台湾问题"、"南海" |
| days | number | 否 | 分析时间跨度（天），默认 14 |
| compare_with | string | 否 | 对比国家名称（中文），用于多国舆论对比 |

## 调用示例

```json
{
  "country": "中国",
  "topic": "台湾问题",
  "days": 30,
  "compare_with": "美国"
}
```
