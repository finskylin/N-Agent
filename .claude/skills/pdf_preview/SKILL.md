---
name: pdf_preview
display_name: PDF预览
readonly: true
description: |
  【功能】生成PDF文档或预览现有PDF文件
  【数据源】用户提供的数据(生成模式)、本地/远程PDF文件(预览模式)
  【输出数据】文件下载链接(download_url)、文本预览内容、页数信息
  【耗时】~5-20秒
  【适用场景】用户说"生成PDF报告"、"预览PDF"、"下载PDF"时使用
license: Proprietary
time_estimates:
  default:
    min: 5
    max: 20
    desc: "PDF生成"
ui_components:
  - component: office_preview
    condition: default
    data_hints:
      - has_download_url
authority: unknown
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/pdf_preview/scripts/pdf_preview.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/pdf_preview/scripts/pdf_preview.py <<'EOF'
{...json参数...}
EOF
```

# PDF Generation & Preview Skill

## 执行方式

- 使用 `python3` 直接执行 `scripts/*.py`
- 支持命令行参数或 stdin JSON 输入
- 不使用 `mcp__...` 工具名

## 概述

生成 PDF 文档或渲染现有文档到数据看板。支持从结构化数据生成文档，或预览现有页面内容。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| file_path | string | 否 | PDF 文件路径（预览模式） |
| file_url | string | 否 | PDF 文件 URL（URL 预览模式） |
| data | object | 否 | 用于生成文档的数据（生成模式），格式: {"title": "...", "content": "..."} |
| extract_text_only | boolean | 否 | 是否仅提取文本，默认 false |

## 输出数据

返回 component 事件，包含:

- `component`: "pdf_preview"
- `data`: 包含文件内容、下载链接 (`download_url`) 和文本预览

## 调用示例

```json
{
  "content": "# 贵州茅台2025年度投资分析报告\n\n## 摘要\n贵州茅台2025年营收同比增长15.2%，净利润达650亿元...\n\n## 估值分析\nPE当前28.5倍，处于历史30%分位...",
  "title": "贵州茅台2025年度投资分析报告"
}
```
