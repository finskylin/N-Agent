---
name: industry_comparison
display_name: 行业对标分析
readonly: true
description: |
  【功能】获取同行业公司数据对比，计算行业排名
  【数据源】AKShare行业分类、Tushare财务数据
  【输出数据】行业名称、同行公司数、PE/PB/ROE/营收增长排名(如3/20)、行业均值、相对估值比率
  【耗时】~10-30秒
  【适用场景】用户问"某股票在行业排第几"、"同行对比"、"行业龙头"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/industry_comparison/scripts/industry_comparison.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/industry_comparison/scripts/industry_comparison.py <<'EOF'
{...json参数...}
EOF
```

# 行业对标分析技能

获取股票所在行业的同行公司数据，对比PE/PB/ROE/营收增长等核心指标，计算目标股票在行业中的排名位置。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 否 | 股票代码，如 600519.SH |
| industry_name | string | 否 | 行业名称，如"白酒"，指定后获取该行业个股明细 |
| market | string | 否 | 板块类型：sw(申万行业)/concept(概念板块)，默认 sw |

## 调用示例

```json
{
  "ts_code": "600519.SH"
}
```
