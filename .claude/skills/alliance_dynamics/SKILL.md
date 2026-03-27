---
name: alliance_dynamics
readonly: true
description: |
  【功能】分析国际联盟结构演化、多边机制参与度和双边关系动态。
  【输出】外交新闻、双边情感评分、联盟事件、治理指标、关系评分。
  【适用场景】用户问"北约/金砖/上合动态"、"中美/中俄关系"、"联合国投票一致率"时使用。
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/alliance_dynamics/scripts/alliance_dynamics.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/alliance_dynamics/scripts/alliance_dynamics.py <<'EOF'
{...json参数...}
EOF
```


# 国际联盟关系动态分析

## 概述
基于 GDELT 外交事件数据和世界银行治理指标，分析国际联盟结构和双边关系。

## 数据源
- GDELT DOC API — 外交/联盟/合作主题新闻
- World Bank: CC.EST, GE.EST, RL.EST — 治理指标
- UN 大会投票记录 — 两国投票一致率分析
- NATO 官方新闻 RSS — 北约动态追踪

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| country | string | 是 | 目标国家名称（中文），也可用 query 代替 |
| partner | string | 否 | 对比国家名称（中文），用于双边关系分析 |
| topic | string | 否 | 关注话题，如"军事合作"、"经济合作" |
| days | number | 否 | 分析时间跨度（天），默认 14 |

## 调用示例

```json
{
  "country": "中国",
  "partner": "俄罗斯",
  "topic": "军事合作",
  "days": 30
}
```
