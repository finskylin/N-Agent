---
name: monetary_system
display_name: 货币体系博弈分析
readonly: true
description: |
  【功能】分析全球货币体系演变、利率政策和外汇储备货币构成
  【数据源】IMF Data Mapper(GDP增长/通胀/经常账户)、美联储FRED(利率/美元指数/黄金)、IMF COFER(外汇储备构成)
  【输出数据】联邦基金利率、10年国债收益率、美元指数(DXY)、黄金价格、美元/人民币汇率、外汇储备货币占比(%)、货币政策鹰鸽判断
  【耗时】~15-60秒
  【适用场景】用户问"美联储加息/降息"、"美元指数走势"、"去美元化趋势"、"外汇储备构成"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/monetary_system/scripts/monetary_system.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/monetary_system/scripts/monetary_system.py <<'EOF'
{...json参数...}
EOF
```


## Imported Metadata

- Original display name: `货币体系博弈分析`
- Original category: `geopolitical_analysis`
- Original intents: `analyze`, `query`
- Original keywords: `货币`, `美元`, `人民币`, `汇率`, `利率`, `美联储`, `央行`, `黄金`, `通胀`, `加息`, `降息`, `SWIFT` ...

# 货币体系博弈分析

## 概述
接入 IMF 和 FRED 数据库，分析全球货币体系演变、利率政策和金融市场联动。

## 数据源
- IMF Data Mapper: GDP增长率、通胀率、经常账户
- FRED: 联邦基金利率、10年国债、美元指数、黄金、美元/人民币
- IMF COFER: 全球外汇储备货币构成（美元/欧元/人民币占比）

## 输出
```json
{
  "imf_macro": {...},
  "fred_series": {...},
  "monetary_stance": "鹰派",
  "currency_dynamics": {...},
  "disclaimer": "学术研究模拟，非实时情报"
}
```

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| countries | string | 否 | 国家列表，逗号分隔或数组，支持中文名或ISO3代码，默认 "CHN,USA" |
| include_commodities | boolean | 否 | 是否包含大宗商品数据（黄金等），默认 true |

## 调用示例

```json
{
  "countries": ["美国", "中国", "欧盟"],
  "include_commodities": true
}
```
