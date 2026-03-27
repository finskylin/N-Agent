---
name: technical_indicators
display_name: 技术指标分析
readonly: true
description: |
  【功能】计算技术分析指标，生成交易信号
  【数据源】历史K线数据计算（AkShare 前复权日线）
  【输出数据】MA均线(5/10/20/60日)、MACD(DIF/DEA/柱)、KDJ(K/D/J值)、RSI(6/12/24)、BOLL(上中下轨)、金叉死叉信号
  【耗时】~5-15秒
  【适用场景】用户问"MACD/KDJ/RSI指标"、"技术分析"、"金叉死叉"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/technical_indicators/scripts/technical_indicators.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/technical_indicators/scripts/technical_indicators.py <<'EOF'
{...json参数...}
EOF
```

# 技术指标计算技能

## 概述

基于历史 K 线数据计算技术分析指标，生成交易信号。

## 核心功能

### 1. 计算全部指标

```json
{
  "tool": "execute_skill_with_ui",
  "usage": {
    "skill_name": "technical_indicators",
    "params": {
      "ts_code": "600519.SH"
    }
  }
}
```

### 2. 计算指定指标

```json
{
  "tool": "execute_skill_with_ui",
  "usage": {
    "skill_name": "technical_indicators",
    "params": {
      "ts_code": "600519.SH",
      "indicators": ["MACD", "KDJ", "RSI"]
    }
  }
}
```

## 支持的指标

| 指标 | 描述                 | 参数            |
| ---- | -------------------- | --------------- |
| MA   | 移动平均线           | 5/10/20/60 日   |
| MACD | 指数平滑异同移动平均 | DIF/DEA/MACD 柱 |
| KDJ  | 随机指标             | K/D/J 值        |
| RSI  | 相对强弱指标         | 6/12/24 周期    |
| BOLL | 布林带               | 上轨/中轨/下轨  |

## 输出示例

```json
{
  "ts_code": "600519.SH",
  "title": "技术指标分析 (近60日)",
  "series": [
    {
      "date": "2024-01-15",
      "open": 1850.0,
      "high": 1865.0,
      "low": 1845.0,
      "close": 1860.0,
      "ma5": 1855.2,
      "ma10": 1848.5,
      "ma20": 1840.0,
      "ma60": 1820.0,
      "dif": 12.5,
      "dea": 10.2,
      "macd": 4.6,
      "k": 65.5,
      "d": 58.2,
      "j": 80.1,
      "rsi6": 55.3,
      "rsi12": 52.8,
      "rsi24": 50.5,
      "boll_upper": 1900.0,
      "boll_mid": 1840.0,
      "boll_lower": 1780.0
    }
  ],
  "latest": {
    "ma": { "ma5": 1855.2, "ma10": 1848.5, "ma20": 1840.0, "ma60": 1820.0 },
    "macd": { "dif": 12.5, "dea": 10.2, "macd": 4.6 },
    "kdj": { "k": 65.5, "d": 58.2, "j": 80.1 },
    "rsi": { "rsi6": 55.3, "rsi12": 52.8, "rsi24": 50.5 },
    "boll": { "upper": 1900.0, "mid": 1840.0, "lower": 1780.0 }
  },
  "signals": [
    { "type": "MACD", "signal": "GOLDEN_CROSS", "message": "MACD金叉" }
  ],
  "stats": { "days": 60, "start_date": "2023-11-01", "end_date": "2024-01-15" }
}
```

## 信号类型

- GOLDEN_CROSS: 金叉买入信号
- DEATH_CROSS: 死叉卖出信号
- OVERBOUGHT: 超买警告
- OVERSOLD: 超卖机会

## 调用示例

```json
{
  "ts_code": "600519.SH",
  "indicators": [
    "MA5",
    "MA20",
    "RSI",
    "MACD"
  ],
  "days": 30
}
```
