---
name: document_reader
display_name: 文档读取与识别
readonly: true
description: |
  【功能】下载并解析文档内容，支持 PDF、DOCX、XLSX、图片等格式，支持本地文件、URL、钉钉附件。
  【适用场景】用户上传文档、发送文件链接、钉钉群发送附件时调用。
  【不适用】网页内容抓取（用 url_fetch）。
priority: 90
ui_components:
  - component: markdown_card
    condition: default
    priority: 1
    data_hints:
      - has_text_content
intents:
  - read_document
  - parse_file
keywords:
  - 读取文档
  - 解析文件
  - 文件内容
  - 识别图片
authority: unknown
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/document_reader/scripts/document_reader.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/document_reader/scripts/document_reader.py <<'EOF'
{...json参数...}
EOF
```

# 文档读取与识别

## 执行方式

- 使用 `python3` 直接执行 `scripts/*.py`
- 支持命令行参数或 stdin JSON 输入
- 不使用 `mcp__...` 工具名

解析用户上传的文档或钉钉附件，提取文本、表格和图片内容。

## 支持格式

| 格式 | 提取内容 |
|------|---------|
| PDF | 文本 + 表格 + 页数 + 元信息 |
| DOCX | 段落 + 表格 + 标题结构 |
| XLSX | Sheet列表 + 表头 + 数据行 |
| PNG/JPG | OCR文字识别（pytesseract优先，视觉大模型兜底） |

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| file_path | string | 否 | 本地文件路径（三种输入方式之一） |
| file_url | string | 否 | HTTP/HTTPS 文件下载链接（三种输入方式之一） |
| dingtalk_download_code | string | 否 | 钉钉附件 downloadCode（三种输入方式之一） |
| dingtalk_robot_code | string | 否 | 钉钉 robotCode，配合 dingtalk_download_code 使用 |
| file_type | string | 否 | 文件类型: auto/pdf/docx/xlsx/image，默认 auto |
| extract_mode | string | 否 | 提取模式: text/table/full/summary，默认 full |
| max_chars | number | 否 | 最大返回字符数，默认 10000 |

## 调用示例

场景1: HTTP URL 文件
```json
{
  "file_url": "https://example.com/report.pdf",
  "file_type": "pdf",
  "extract_mode": "text",
  "max_chars": 10000
}
```

场景2: 钉钉附件（图片/文件）
```json
{
  "dingtalk_download_code": "download_code_from_context",
  "dingtalk_robot_code": "robot_code_from_context",
  "file_type": "image"
}
```

场景3: 本地文件路径
```json
{
  "file_path": "/tmp/uploaded_file.docx"
}
```
