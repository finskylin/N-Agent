---
name: tech_rivalry
readonly: true
description: "【功能】分析特定技术领域的论文产出、引用影响力和科技竞争态势 【数据源】Semantic Scholar学术API、世界银行(研发支出/GDP%)、OECD SDMX(GERD/GDP研发强度) 【输出数据】论文列表(标题/作者/引用数)、机构排名、研发支出占GDP比(%)、创新指数 【耗时】~20-90秒 【适用场景】用户问'AI/量子/芯片领域论文'、'某国科技竞争力'、'研发投入对比'时使用"
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/tech_rivalry/scripts/tech_rivalry.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/tech_rivalry/scripts/tech_rivalry.py <<'EOF'
{...json参数...}
EOF
```


# 科技竞争力评估

## 概述
基于 Semantic Scholar 学术数据库，检索和分析特定技术领域的论文产出和引用影响力。

## 数据源
- Semantic Scholar API — 论文搜索
- World Bank API: GB.XPD.RSDV.GD.ZS (研发支出/GDP%)
- OECD SDMX API: GERD/GDP 研发强度（双源互补）

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| topic | string | 是 | 技术领域关键词，如 "量子计算" |
| countries | string/array | 否 | 国家列表，如 ["中国", "美国"] 或逗号分隔字符串，默认 "CHN,USA,JPN,KOR,DEU" |
| max_papers | integer | 否 | 最大论文数量，默认 20，最大 100 |

## 调用示例

```json
{
  "topic": "量子计算",
  "countries": ["中国", "美国"],
  "max_papers": 10
}
```
