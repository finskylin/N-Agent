---
name: xlsx_preview
display_name: Excel预览
readonly: true
description: |
  【功能】生成Excel表格或预览现有XLSX文件
  【数据源】用户提供的数据(生成模式)、本地/远程XLSX文件(预览模式)
  【输出数据】文件下载链接(download_url)、工作表数据预览、列/行统计
  【耗时】~3-10秒
  【适用场景】用户说"生成Excel表格"、"预览表格数据"、"下载Excel"时使用
license: Proprietary
time_estimates:
  default:
    min: 3
    max: 10
    desc: "Excel生成"
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
echo '{...json参数...}' | python3 /app/.claude/skills/xlsx_preview/scripts/xlsx_preview.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/xlsx_preview/scripts/xlsx_preview.py <<'EOF'
{...json参数...}
EOF
```

# XLSX Generation & Preview Skill

## 执行方式

- 使用 `python3` 直接执行 `scripts/*.py`
- 支持命令行参数或 stdin JSON 输入
- 不使用 `mcp__...` 工具名

## 概述

生成 Excel 表格 (.xlsx) 或渲染现有表格到数据看板。支持从结构化数据生成表格，或预览现有表格内容。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| data | array/object | 否 | 用于生成表格的数据（生成模式），支持二维数组或 `{"sheets": {"Sheet1": [...]}}` |
| title | string | 否 | 表格标题（生成模式） |
| file_path | string | 否 | xlsx 文件路径（预览模式） |
| file_url | string | 否 | xlsx 文件 URL（预览模式） |
| sheet_name | string | 否 | 指定工作表名称 |
| max_rows | integer | 否 | 最大预览行数，默认 100 |

## 输出数据

返回 component 事件，包含:

- `component`: "xlsx_preview"
- `data`: 包含文件内容、下载链接 (`download_url`) 和工作表数据

## 调用示例

```json
{
  "data": [["股票代码", "股票名称", "PE", "PB", "ROE(%)", "股息率(%)"], ["600519.SH", "贵州茅台", 28.5, 8.2, 31.5, 1.8], ["000858.SZ", "五粮液", 22.3, 6.1, 28.7, 2.1], ["000568.SZ", "泸州老窖", 19.8, 5.4, 26.3, 2.5]],
  "title": "白酒行业估值对比"
}
```
