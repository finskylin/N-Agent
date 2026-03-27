---
name: terrain_elevation
description: "【功能】查询指定位置的海拔高度，支持单点和剖面查询 【数据源】Open-Elevation API(免费) 【输出数据】海拔高度(米)、海拔剖面图表数据、地图标记 【耗时】~5-15秒 【适用场景】用户问'某地海拔'、'珠穆朗玛峰多高'、'两地海拔剖面'时使用"
readonly: true
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/terrain_elevation/scripts/terrain_elevation.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/terrain_elevation/scripts/terrain_elevation.py <<'EOF'
{...json参数...}
EOF
```


## Imported Metadata

- Original display name: `地形海拔查询`
- Original intents: `terrain_elevation`, `elevation`, `altitude`
- Original keywords: `海拔`, `高度`, `地形`, `高程`, `elevation`, `altitude`, `terrain`

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| query | string | 是 | 地点名称或描述，如 "珠穆朗玛峰" |
| end_point | string | 否 | 终点地名（剖面查询时使用），如 "拉萨" |
| profile_points | integer | 否 | 剖面采样点数，默认 50，范围 5-100 |

## 调用示例

```json
{
  "query": "珠穆朗玛峰海拔高度",
  "end_point": "拉萨",
  "profile_points": 10
}
```
