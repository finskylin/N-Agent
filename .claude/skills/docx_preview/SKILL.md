---
name: docx_preview
display_name: Word预览
readonly: true
description: |
  【功能】生成Word文档或预览现有DOCX文件
  【数据源】用户提供的数据(生成模式)、本地/远程DOCX文件(预览模式)
  【输出数据】文件下载链接(download_url)、文本预览内容、章节结构
  【耗时】~3-10秒
  【适用场景】用户说"生成Word文档"、"预览DOCX"、"下载Word"时使用
license: Proprietary
time_estimates:
  default:
    min: 3
    max: 10
    desc: "Word生成"
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
echo '{...json参数...}' | python3 /app/.claude/skills/docx_preview/scripts/docx_preview.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/docx_preview/scripts/docx_preview.py <<'EOF'
{...json参数...}
EOF
```

# DOCX Generation & Preview Skill

## 执行方式

- 使用 `python3` 直接执行 `scripts/*.py`
- 支持命令行参数或 stdin JSON 输入
- 不使用 `mcp__...` 工具名

## 概述

生成 Word 文档 (.docx) 或渲染现有文档到数据看板。支持从结构化数据生成报告，或提取现有文档内容预览。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| file_path | string | 否 | docx 文件路径（预览模式） |
| file_url | string | 否 | docx 文件 URL（预览模式） |
| data | object | 否 | 用于生成文档的数据（生成模式），格式: `{"title": "...", "sections": [{"heading": "...", "content": "..."}]}` |
| extract_text_only | boolean | 否 | 是否仅提取文本，默认 false |

## 输出数据

返回 component 事件，包含:

- `component`: "docx_preview"
- `data`: 包含文件内容、下载链接 (`download_url`) 和文本预览

## 调用示例

```json
{
  "content": "# 国盾量子竞争格局分析\n\n## 一、公司简介\n国盾量子（688027.SH）是国内量子通信领域龙头企业...\n\n## 二、核心优势\n1. 量子密钥分发技术...",
  "title": "国盾量子竞争格局分析"
}
```
