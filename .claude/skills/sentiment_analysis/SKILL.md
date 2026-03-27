---
name: sentiment_analysis
display_name: 舆情情感分析
readonly: true
description: |
  【功能】分析股票相关新闻和舆情的情感倾向，生成情感评分
  【数据源】AkShare 股票新闻接口 + TF-IDF ML 情感分类模型（无模型时降级为词典匹配）
  【输出数据】整体情感评分(0-1)、情感标签(正面/中性/负面)、新闻条数、各条新闻情感明细
  【耗时】~10-30秒
  【适用场景】用户问"舆情如何"、"市场情绪"、"新闻面"、"消息面"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/sentiment_analysis/scripts/sentiment_analysis.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/sentiment_analysis/scripts/sentiment_analysis.py <<'EOF'
{...json参数...}
EOF
```

# 舆情情感分析技能

分析个股近期新闻舆情的情感倾向，输出情感评分和明细。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 是 | 股票代码，如 "600519.SH" |
| limit | integer | 否 | 新闻条数上限，默认 20 |

## 调用示例

```json
{
  "ts_code": "600519.SH",
  "limit": 20
}
```

## 输出示例

```json
{
  "ts_code": "600519.SH",
  "news_count": 18,
  "overall_score": 0.62,
  "overall_label": "正面",
  "confidence": 0.71,
  "news_items": [
    {
      "title": "贵州茅台三季报超预期",
      "score": 0.85,
      "label": "正面",
      "confidence": 0.88,
      "method": "ml"
    }
  ],
  "for_llm": {
    "ts_code": "600519.SH",
    "overall_label": "正面",
    "overall_score": 0.62,
    "news_count": 18,
    "positive_count": 12,
    "negative_count": 3,
    "neutral_count": 3
  }
}
```
