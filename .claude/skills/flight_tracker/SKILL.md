---
name: flight_tracker
description: "【功能】航班/船舶实时轨迹追踪，支持标识符查询和区域范围搜索 【数据源】ADS-B Exchange、FlightRadar24、FlightAware(航班)、ChinaPorts/MarineTraffic(船舶) 【输出数据】航班:ICAO码/呼号/机型/高度/速度/航向/历史轨迹；船舶:MMSI/船名/船型/目的港/ETA/位置 【耗时】~15-90秒 【适用场景】用户问'某航班位置'、'台北周边飞机'、'某船舶追踪'、'台海空域监控'时使用"
readonly: true
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/flight_tracker/scripts/flight_tracker.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/flight_tracker/scripts/flight_tracker.py <<'EOF'
{...json参数...}
EOF
```


## Imported Metadata

- Original display name: `航班轨迹追踪`
- Original intents: `flight`, `ship`, `adsb`, `icao`, `tracker`, `tracking`, `area_search`, `nearby`
- Original keywords: `航班`, `飞机`, `轨迹`, `航线`, `icao`, `adsb`, `ads-b`, `追踪`, `船舶`, `船只`, `轮船`, `mmsi` ...

# 航班轨迹追踪技能

## 概述

提供航班和船舶的实时轨迹追踪能力，支持两种搜索模式：

### 搜索模式

| 模式 | 说明 | 示例 |
|------|------|------|
| **标识符查询** | 通过航班号、ICAO码、MMSI等查询特定目标 | `CA1831`, `a43c7a`, `412345678` |
| **范围搜索** | 通过地名或经纬度搜索区域内所有飞机/船舶 | `台北周边`, `上海附近飞机`, `25.03,121.56` |

### 航班追踪数据源

| 数据源 | 特点 | 适用场景 |
|--------|------|----------|
| ADS-B Exchange | 无过滤的实时数据，支持 ICAO 码和轨迹 | ICAO 码查询、轨迹追踪 |
| FlightRadar24 | 全球最大航班追踪平台 | 航班号查询 |
| FlightAware | 美国专业航班追踪 | 航班号查询、航线信息 |

### 船舶追踪数据源

| 数据源 | 特点 | 适用场景 |
|--------|------|----------|
| ChinaPorts | 中国港口网 | MMSI/IMO 查询 |
| MarineTraffic | 全球船舶追踪 | 船名搜索 |
| ShipXY | 船讯网 | 中国周边海域 |

## 调用示例

```json
{
  "query": "CA1831",
  "tracking_type": "flight",
  "search_mode": "identifier",
  "radius_km": 50,
  "include_trace": true,
  "max_results": 10
}
```
