---
name: prediction
display_name: AI预测
readonly: false
description: |
  【功能】多模型集成预测股票涨跌方向和概率，支持真实数据驱动的高精度预测。
  【重要】调用前必须先收集真实数据（财务、K线、舆情），否则预测置信度极低。
  【输出】涨跌方向、概率、置信度、预期幅度、关键影响因素。
  【适用场景】用户问"某股票会涨还是跌"、"预测走势"、"AI分析"时使用。
  【不适用】查询基本面/技术面数据（用对应专项工具）。
category: analysis_prediction
priority: 92
ui_components:
  - component: prediction_card
    condition: 展示预测结果卡片
    priority: 1
    data_hints:
      - has_prediction
  - component: adaptive_score_card
    condition: 以评分卡展示预测置信度
    priority: 2
    data_hints:
      - has_score
intents:
  - predict
keywords:
  - 预测
  - 走势
  - 涨跌
  - 趋势
  - 方向
  - ai
  - 机器学习
time_estimates:
  default:
    min: 15
    max: 45
    desc: "AI预测分析"
authority: analysis_prediction
key_params:
  - ts_code
  - model
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/prediction/scripts/prediction.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/prediction/scripts/prediction.py <<'EOF'
{...json参数...}
EOF
```

# AI 预测 Skill

## 功能

- 多模型集成预测 (XGBoost + LightGBM + LSTM + 情绪)
- 支持真实数据驱动，接入 technical_indicators / money_flow / financial_report / sentiment_analysis 结果
- LLM 可在调用时指定市场偏向、模型权重、涨跌阈值等参数动态干预预测
- 输出涨跌方向、概率、置信度、关键影响因素、数据质量标注

## 最佳实践：高精度调用流程

**推荐：先收集真实数据，再预测（精度提升 30%+）**

```
Step 1: technical_indicators(ts_code)   → 获取真实技术指标
Step 2: money_flow(ts_code)             → 获取真实主力/北向/融资数据
Step 3: financial_report(ts_code)       → 获取真实财务数据（PE/ROE/营收等）
Step 4: historical_data(ts_code, days=60) → 获取K线（供LSTM）
Step 5: sentiment_analysis(ts_code)     → 获取真实舆情分数（可选）
Step 6: prediction(ts_code, + 以上所有数据 + market_bias)
```

**LLM 需要根据已收集数据判断 market_bias：**
- `bullish`：主力5日净流入为正 + 技术面均线多头 + 市场量能放大
- `bearish`：主力持续净流出 + 技术面破位 + 市场量能萎缩
- `neutral`：无明显信号（默认）

## 调用示例

### 基础调用（仅 ts_code）
```json
{
  "ts_code": "600519.SH",
  "horizon": "1w"
}
```

### 完整真实数据调用（推荐）
```json
{
  "ts_code": "688027.SH",
  "horizon": "1w",
  "market_bias": "bullish",
  "sentiment_score": 0.72,
  "technical_indicators": { "...来自 technical_indicators skill 的完整结果..." },
  "money_flow": { "...来自 money_flow skill 的完整结果..." },
  "financial_data": { "roe": 18.5, "revenue_yoy": 32.1, "profit_yoy": 28.6, "gross_margin": 45.2, "debt_ratio": 38.0, "total_mv": 500000000 },
  "kline_data": [ "...来自 historical_data skill 的K线数组..." ]
}
```

### 指定模型权重（高级）
```json
{
  "ts_code": "000001.SZ",
  "horizon": "3d",
  "market_bias": "bearish",
  "model_weights": { "xgboost": 0.4, "lightgbm": 0.4, "lstm": 0.1, "sentiment": 0.1 },
  "label_threshold": 0.02
}
```

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 是 | 股票代码，格式如 600519.SH 或 688027.SH |
| horizon | string | 否 | 预测周期：1d=明天，3d=3天内，1w=一周内，2w=两周内，1m=一个月；默认 1w；枚举值: 1d/3d/1w/2w/1m |
| market_bias | string | 否 | 市场偏向。LLM根据已获取的资金面/市场环境/用户描述综合判断：bullish=市场强势/主力持续净流入/政策利好/量能放大，bearish=主力持续出逃/市场低迷/利空，neutral=不偏（默认）；枚举值: neutral/bullish/bearish |
| sentiment_score | number | 否 | 来自 sentiment_analysis skill 的情感分数，0=极度悲观，0.5=中性，1=极度乐观。有真实情绪数据时必须传入；范围: 0-1 |
| financial_data | object | 否 | 来自 financial_report skill 的财务数据，包含 pe_ttm、pb、roe、revenue_yoy、profit_yoy、gross_margin、debt_ratio、total_mv 等字段 |
| money_flow | object | 否 | 来自 money_flow skill 的完整资金数据对象（含 summary.main_net_5d、north_bound_change 等） |
| technical_indicators | object | 否 | 来自 technical_indicators skill 的技术指标数据（含 indicators.ma/macd/kdj/rsi 和 summary 等） |
| kline_data | array | 否 | 来自 historical_data skill 的日K线数组（至少20条），供LSTM时序模型使用 |
| model_weights | object | 否 | 可选：覆盖默认模型权重，四者之和需接近1.0。仅当有充分理由时使用。示例: {"xgboost": 0.4, "lightgbm": 0.3, "lstm": 0.2, "sentiment": 0.1} |
| label_threshold | number | 否 | 涨跌判断阈值（相对收益率）。高波动股票建议0.04-0.05，低波动蓝筹建议0.02，默认0.03；范围: 0.01-0.05 |
| bid_ask | object | 否 | 来自 bid_ask_depth skill 的完整盘口数据对象（含 summary.bid_ratio、summary.spread_pct）。有盘口数据时必须传入，显著提升微观结构特征准确性 |
| tick_data | object | 否 | 来自 intraday_tick skill 的逐笔数据对象（含 items 数组，每条含成交额和方向）。有逐笔数据时传入，用于计算大单净买入和主动买入比例 |

## 输出格式

```json
{
  "direction": "UP",
  "probability": 0.68,
  "confidence": "中",
  "magnitude": "+4.2%",
  "data_quality": "real_data",
  "data_sources_used": ["technical_indicators", "money_flow", "financial_report", "kline_data"],
  "market_bias_applied": "bullish",
  "key_factors": [
    {"name": "5日主力净流入", "contribution": 0.28},
    {"name": "均线趋势(5)", "contribution": 0.22}
  ],
  "model_predictions": {
    "xgboost": {"direction": "UP", "probability": 0.71},
    "lightgbm": {"direction": "UP", "probability": 0.65},
    "lstm": {"direction": "NEUTRAL", "probability": 0.52},
    "sentiment": {"direction": "UP", "probability": 0.68}
  },
  "model_weights": {"xgboost": 0.30, "lightgbm": 0.25, "lstm": 0.25, "sentiment": 0.20},
  "risks": ["关注市场整体风险，设置止损位"],
  "method": "regime_weighted_ensemble"
}
```

### data_quality 说明

| 值 | 含义 |
|------|------|
| `real_data` | ≥3 个真实数据源，预测最可靠 |
| `partial_real` | 1-2 个真实数据源，中等可靠 |
| `estimated` | 全部使用估算数据，置信度较低，建议先收集真实数据 |

## 模型说明

- **XGBoost**: 梯度提升树，51维特征，TimeSeriesSplit 5折验证
- **LightGBM**: 轻量级梯度提升，与 XGBoost 互补（更宽树结构）
- **LSTM**: 双向LSTM + Multi-Head Attention，基于K线时序
- **情绪分析**: 新闻舆情信号，权重由市场状态动态调整

权重由市场状态（牛市/熊市/震荡）自动调整，也可通过 model_weights 覆盖。

## 注意事项

- 预测结果仅供参考，不构成投资建议
- 市场有风险，投资需谨慎
