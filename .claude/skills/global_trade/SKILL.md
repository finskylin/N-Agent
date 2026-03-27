---
name: global_trade
readonly: true
description: "【功能】分析国家间贸易流向、GDP结构、FDI趋势和贸易依赖度 【数据源】世界银行API(GDP/贸易占比/FDI/高技术出口)、UN Comtrade双边贸易数据库 【输出数据】GDP(美元)、贸易/GDP占比(%)、FDI净流入、高技术出口占比、双边贸易额、贸易依赖度矩阵 【耗时】~15-60秒 【适用场景】用户问'中美贸易数据'、'某国GDP/进出口'、'贸易依赖度分析'时使用"
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/global_trade/scripts/global_trade.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/global_trade/scripts/global_trade.py <<'EOF'
{...json参数...}
EOF
```


# 全球贸易格局分析

## 概述
接入世界银行和联合国贸易数据库，分析国家间贸易流向、GDP 结构和贸易依赖度。

## 数据源
- World Bank API: GDP, 贸易/GDP%, FDI, 高技术出口, 出口, 进口
- UN Comtrade Preview API — 双边贸易额

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| countries | string | 否 | 国家列表，逗号分隔，支持中文名或ISO3代码，默认 "CHN,USA" |
| years | number | 否 | 回溯年数，默认 5 |

## 调用示例

```json
{
  "countries": ["中国", "美国", "欧盟"],
  "years": 10
}
```
