---
name: pptx_preview
display_name: PPT预览
readonly: true
description: |
  【功能】生成PowerPoint演示文稿或预览现有PPTX文件
  【数据源】用户提供的数据(生成模式)、本地/远程PPTX文件(预览模式)
  【输出数据】文件下载链接(download_url)、幻灯片文本预览、页数信息
  【耗时】~5-20秒
  【适用场景】用户说"生成PPT"、"预览幻灯片"、"下载演示文稿"时使用
license: Proprietary
time_estimates:
  default:
    min: 5
    max: 20
    desc: "PPT生成"
authority: unknown
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/pptx_preview/scripts/pptx_preview.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/pptx_preview/scripts/pptx_preview.py <<'EOF'
{...json参数...}
EOF
```

# PPTX Generation & Preview Skill

## 执行方式

- 使用 `python3` 直接执行 `scripts/*.py`
- 支持命令行参数或 stdin JSON 输入
- 不使用 `mcp__...` 工具名

## 概述

生成 PowerPoint 演示文稿 (.pptx) 或渲染现有文稿到数据看板。支持从结构化数据生成幻灯片，或预览现有文稿内容。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| content | string | 否 | Markdown 格式的演示文稿内容（生成模式），用 `## 幻灯片N` 分隔每页 |
| title | string | 否 | 演示文稿标题（生成模式） |
| file_path | string | 否 | pptx 文件路径（预览模式） |
| file_url | string | 否 | pptx 文件 URL（预览模式） |
| data | object | 否 | 用于生成演示文稿的结构化数据，格式: `{"title": "...", "slides": [{"title": "...", "content": "..."}]}` |
| extract_text_only | boolean | 否 | 是否仅提取文本，默认 false |

## 输出数据

返回 component 事件，包含:

- `component`: "pptx_preview"
- `data`: 包含文件内容、下载链接 (`download_url`) 和幻灯片文本预览

## 调用示例

```json
{
  "content": "# 2026年Q1 A股市场展望\n\n## 幻灯片1：市场回顾\n上证指数季度涨幅3.2%，科技板块领涨...\n\n## 幻灯片2：重点行业\n半导体、AI、新能源...\n\n## 幻灯片3：投资建议\n关注高股息蓝筹...",
  "title": "2026年Q1 A股市场展望"
}
```
