---
name: institutional_research
display_name: 机构调研
readonly: true
description: |
  【功能】机构调研统计和分析师评级数据
  【数据源】AKShare机构调研和分析师数据
  【输出数据】调研机构列表、调研次数、分析师评级、目标价
  【耗时】~5-15秒
  【适用场景】用户问"机构调研"、"分析师评级"、"调研报告"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/institutional_research/scripts/institutional_research.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/institutional_research/scripts/institutional_research.py <<'EOF'
{...json参数...}
EOF
```

# 机构调研技能

获取机构调研统计和分析师评级数据。

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
