---
name: usgs_explorer
display_name: USGS 卫星数据搜索
readonly: true
description: |
  【功能】搜索USGS Landsat卫星场景数据
  【数据源】USGS M2M API(需Token)、EarthExplorer网站(Playwright降级方案)
  【输出数据】Landsat场景列表、场景ID、日期、云量(%)、缩略图、下载链接
  【耗时】~10-45秒
  【适用场景】用户问"Landsat数据"、"USGS卫星影像"、"遥感数据搜索"时使用
priority: 71
ui_components:
  - component: data_table
    condition: 场景列表
    priority: 1
    data_hints:
      - has_array
intents:
  - usgs
  - landsat
  - satellite_data
keywords:
  - USGS
  - Landsat
  - 卫星数据
  - Earth Explorer
  - 遥感数据
  - landsat8
  - landsat9
time_estimates:
  default:
    min: 10
    max: 45
    desc: "USGS数据探索"
authority: unknown
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/usgs_explorer/scripts/usgs_explorer.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/usgs_explorer/scripts/usgs_explorer.py <<'EOF'
{...json参数...}
EOF
```

# USGS Explorer Skill

## 执行方式

- 使用 `python3` 直接执行 `scripts/*.py`
- 支持命令行参数或 stdin JSON 输入
- 不使用 `mcp__...` 工具名

## 描述

通过 USGS M2M API 搜索 Landsat 卫星场景数据。

## 数据来源

- USGS M2M API (需 USGS_M2M_TOKEN)
- Playwright 爬取 earthexplorer.usgs.gov (降级方案)

## 输入

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| bbox | array[4] | 否 | 边界框 [west, south, east, north] |
| date_from | string | 否 | 开始日期 |
| date_to | string | 否 | 结束日期 |
| dataset | string | 否 | 数据集名 (默认 landsat_ot_c2_l2) |
| max_cloud_cover | float | 否 | 最大云量百分比 |
| max_results | integer | 否 | 最大结果数 (默认 10) |

## 调用示例

```json
{
  "query": "Beijing China",
  "dataset": "LANDSAT_OT_C2_L2",
  "start_date": "2026-01-01",
  "end_date": "2026-03-16",
  "max_results": 10
}
```
