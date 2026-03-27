---
name: shareholder_analysis
display_name: 股东分析
readonly: true
description: |
  【功能】十大股东、股东户数变化、持股变动分析
  【数据源】AKShare股东数据
  【输出数据】十大股东名单、持股比例、股东户数变化趋势、增减持情况
  【耗时】~5-15秒
  【适用场景】用户问"十大股东"、"股东户数"、"持股变动"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/shareholder_analysis/scripts/shareholder_analysis.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/shareholder_analysis/scripts/shareholder_analysis.py <<'EOF'
{...json参数...}
EOF
```

# 股东分析技能

分析个股十大股东和股东户数变化。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 是 | 股票代码，如 "600519.SH" |

## 调用示例

```json
{
  "ts_code": "600519.SH"
}
```
