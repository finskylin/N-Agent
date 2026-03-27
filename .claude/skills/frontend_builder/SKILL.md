---
name: frontend_builder
display_name: LLM 动态UI生成器
description: |
  【功能】根据数据自动生成可视化 HTML 页面，支持图表、表格、卡片等多种展示形式。
  【适用场景】需要将工具返回的数据可视化展示时、现有 Vue 组件无法满足展示需求时。
  【不适用】代码开发、项目构建、系统搭建等任务（用 docker_operator）。
priority: 85
ui_components:
  - component: html_render
    condition: LLM 动态生成的 HTML 可视化页面
    priority: 1
    data_hints:
      - has_html
      - has_title
intents:
  - frontend
  - component
  - ui
  - page
  - render
  - dynamic_display
  - card_display
  - dashboard
keywords:
  - 可视化展示
  - 数据展示
  - 动态展示
  - UI展示
  - 页面展示
  - 卡片
  - 看板
  - 仪表盘
  - 渲染
  - 图表
  - 表格样式
  - 组件
  - HTML
time_estimates:
  default:
    min: 5
    max: 30
    desc: "LLM 动态HTML生成"
authority: unknown
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/frontend_builder/scripts/frontend_builder.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/frontend_builder/scripts/frontend_builder.py <<'EOF'
{...json参数...}
EOF
```

# LLM 动态UI生成器 (frontend_builder)

## 执行方式

- 使用 `python3` 直接执行 `scripts/*.py`
- 支持命令行参数或 stdin JSON 输入
- 不使用 `mcp__...` 工具名

## 功能概述

LLM 驱动的前端可视化页面生成器。当系统中没有匹配的预定义 Vue 组件时，
调用 LLM 根据数据结构和描述实时生成完整的 HTML 页面，输出为 `html_render` 组件，
前端通过 `DynamicRenderer.vue` 以 iframe srcdoc 方式安全渲染。

### 生成流程

1. 构建数据结构摘要（字段名、类型、数组长度等）
2. 加载系统提示词（`config/prompts/frontend_builder_system.md`）
3. 加载可用 JS 库列表（`config/ui/js_preloads.json`）
4. 调用 LLM（使用 `anthropic_small_fast_model` 或 `anthropic_model`）生成 HTML
5. 从 LLM 响应中提取 HTML（去除 markdown 代码块包裹）
6. 若 LLM 失败，降级为纯 Python 生成简单表格/卡片 HTML

## 输入参数

| 参数 | 类型 | 必填 | 描述 |
|------|------|------|------|
| data | object/array | 是 | 需要可视化的数据 |
| description | string | 否 | 数据描述或可视化需求 |

## 输出格式

```json
{
  "status": "success",
  "data": {
    "for_ui": {
      "components": [
        {
          "component": "html_render",
          "data": {
            "html": "<!DOCTYPE html>...",
            "title": "数据可视化"
          }
        }
      ]
    }
  }
}
```

## 与其他 Skill 集成

frontend_builder 作为 UI 渲染的最终降级方案：

```
Layer 1: for_ui.components 中匹配预定义 Vue 组件
    |
    v (未匹配)
Layer 2: SKILL.md data_hints 推断 Vue 组件
    |
    v (未匹配)
Layer 3: frontend_builder — LLM 生成 HTML → html_render
```

## 调用示例

```json
{
  "query": "将以下股票行业对比数据可视化为柱状图",
  "data": {"industry": "白酒", "stocks": [{"name": "贵州茅台", "pe": 28.5, "roe": 31.5}, {"name": "五粮液", "pe": 22.3, "roe": 28.7}]},
  "template": "chart"
}
```
