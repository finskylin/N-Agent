---
name: dividend_analysis
display_name: 分红派息分析
readonly: true
description: |
  【功能】分析历史分红派息情况，评估分红可持续性
  【数据源】上市公司分红公告、财务数据
  【输出数据】当前股息率(%)、派息比率(%)、连续分红年数、每股分红(元)、分红可持续性评分、历史分红记录
  【耗时】~5-15秒
  【适用场景】用户问"股息率多少"、"分红派息历史"、"分红稳定吗"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/dividend_analysis/scripts/dividend_analysis.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/dividend_analysis/scripts/dividend_analysis.py <<'EOF'
{...json参数...}
EOF
```

# 分红派息分析技能

分析股票历史分红派息情况，计算股息率和派息比率，评估分红可持续性。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 是 | 股票代码，如 600519.SH |

## 调用示例

```json
{
  "ts_code": "600519.SH"
}
```
