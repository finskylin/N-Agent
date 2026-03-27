---
name: component_screenshot
display_name: 组件截图
readonly: true
description: |
  【功能】将组件数据或已有HTML页面渲染为PNG截图，支持地图、图表、卡片、报告页面。
  【适用场景】①用户要图片（预测图/K线图/报告截图等）时，先调对应skill生成数据，再调本工具截图；②钉钉渠道展示视觉组件时。
  【不适用】生成报告文件（用 report_generator，它自带PNG生成）。
priority: 50
internal: true
ui_components: []
intents:
  - screenshot
  - render
  - capture
keywords:
  - 截图
  - 渲染
  - 组件图片
time_estimates:
  default:
    min: 5
    max: 15
    desc: "组件截图渲染"
authority: unknown
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/component_screenshot/scripts/component_screenshot.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/component_screenshot/scripts/component_screenshot.py <<'EOF'
{...json参数...}
EOF
```

# 组件截图技能 (component_screenshot)

## 执行方式

- 使用 `python3` 直接执行 `scripts/*.py`
- 支持命令行参数或 stdin JSON 输入
- 不使用 `mcp__...` 工具名

## 功能概述

将任意 UI 组件数据渲染为 PNG 截图，或直接对已有 HTML 地址生成 PNG 预览。
主要用于钉钉渠道的视觉组件展示，以及“把上次 report.html 发成图片”这类需求。

## 工作流程

模式 A：组件数据 -> 图片

1. 接收组件名称和数据
2. 用 `VueComponentRenderer` 生成独立 HTML 文件
3. 将截图页面切到 `agent-service` 内网地址
4. 通过 agent-service 本地 Playwright 截图为 PNG
5. 可选：上传到钉钉图床获取公网 URL

模式 B：HTML 地址 -> 图片

1. 接收已有 `html_url`
2. 将 HTML 地址规范化为 agent-service 本地可访问地址
3. 直接打开 HTML 页面，不重新渲染组件
4. 通过 agent-service 本地 Playwright 截图为 PNG
5. 可选：上传到钉钉图床获取公网 URL

## 支持的组件

| 组件 | 模板 | 说明 |
|------|------|------|
| map_bindary_view | map_bindary_view.html | Leaflet 地图 |
| kline_chart | kline_chart.html | K线图 |
| technical_chart | technical_chart.html | 技术指标图 |
| radar_chart | radar_chart.html | 雷达图 |
| valuation_gauge | gauge_chart.html | 估值仪表盘 |
| money_flow_chart | flow_chart.html | 资金流向图 |
| rating_card | score_card.html | 评分卡 |
| prediction_card | score_card.html | 预测卡 |
| data_table | data_table.html | 数据表格 |
| * | generic.html | 通用兜底 |

## 输入格式

### 模式 A：组件数据渲染

```json
{
  "component": "map_bindary_view",
  "data": { ... },
  "title": "台海态势图"
}
```

### 模式 B：已有 HTML 地址截图

```json
{
  "html_url": "http://minio-public/reports/2026/03/12/report.html",
  "title": "上一轮报告预览",
  "upload_to_dingtalk": true,
  "wait_seconds": 2
}
```

说明：

- `html_url` 支持 `report.html`、技能页、静态 HTML、`/api/files/download?path=...` 等地址
- skill 会优先将 URL 规范化为 agent-service 本地 Playwright 可访问的地址
- 对已有 HTML 页面不会重新走组件渲染
- 普通 HTML 默认按 `networkidle + 固定等待` 截图，不依赖 `data-rendered`

## 输出格式

```json
{
  "screenshot_path": "data/generated/screenshots/xxx.png",
  "screenshot_url": "http://{IP}:{PORT}/api/files/download?path=...",
  "dingtalk_image_url": "https://xxx.dingtalk.com/...",
  "interactive_url": "http://{IP}:{PORT}/api/files/download?path=...",
  "normalized_html_url": "http://agent-service:8000/api/files/download?path=...",
  "elapsed_seconds": 6.2
}
```

## 能力边界

- 适合：已有 HTML 页面截图、report.html 截图、组件数据转图
- 不负责：从历史会话里检索 HTML 地址，这一步应由调用方先提供 `html_url`
- 如果 HTML 指向本服务下载链接，skill 会自动转成 `127.0.0.1` 本地访问地址

## 代码入口

- 组件数据模式：`ComponentScreenshot.render_and_screenshot(...)`
- HTML 地址模式：`ComponentScreenshot.screenshot_html_url(...)`
- 底层截图执行：`ComponentScreenshot._screenshot_direct(...)`

## 调用示例

```json
{
  "url": "http://127.0.0.1:8000/api/files/download?path=generated/reports/maotai_analysis_20260316.html",
  "width": 1200,
  "height": 800
}
```
