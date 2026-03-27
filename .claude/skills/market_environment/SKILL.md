---
name: market_environment
display_name: 市场环境分析
readonly: true
description: |
  【功能】综合分析A股市场环境，包括量能、资金面、情绪面、技术面等多维度
  【数据源】AkShare 融资融券余额、北向资金流向、指数历史数据
  【输出数据】市场总量能、两融余额、北向资金流向、市场情绪指数、市场环境综合评级
  【耗时】~10-20秒
  【适用场景】用户问"市场环境如何"、"现在适合买股票吗"、"市场整体风险"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/market_environment/scripts/market_environment.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/market_environment/scripts/market_environment.py <<'EOF'
{...json参数...}
EOF
```

# 市场环境分析技能

从量能、资金面、情绪面多维度综合评估当前 A 股市场环境。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| days | number | 否 | 分析回溯天数，默认 20 |

## 调用示例

```json
{
  "days": 30
}
```

## 输出示例

```json
{
  "date": "20240115",
  "volume": {
    "today_vol_yi": 8500.0,
    "avg_5d_vol_yi": 7800.0,
    "vol_ratio": 1.09
  },
  "margin": {
    "balance_yi": 15600.0,
    "change_5d_yi": 120.0
  },
  "north_flow": {
    "net_5d_yi": 32.5,
    "direction": "流入"
  },
  "environment_score": 65,
  "environment_level": "中性偏多",
  "for_llm": {
    "environment_level": "中性偏多",
    "score": 65,
    "key_signals": ["量能温和放大", "北向净流入"]
  }
}
```
