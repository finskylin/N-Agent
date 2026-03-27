## 本地知识库

你有一个本地知识库目录 `knowledge/`，包含专业参考资料。

### 目录结构
{knowledge_tree}

### 各目录内容摘要
{knowledge_summaries}

### 检索与阅读方法

**快速检索**：
- `Glob("knowledge/**/*.md")` — 列出所有 Markdown 文档
- `Grep("关键词", path="knowledge/")` — 按关键词搜索
- `Read("knowledge/system/_summary.md")` — 查看目录摘要

**纯文本文件**（`.md`、`.txt`、`.json`、`.csv`）：
- 直接使用 `Read()` 读取内容

**深度阅读（使用 document_reader skill）**：
当需要读取 PDF、DOCX、XLSX、PPTX、图片等非纯文本文件时，使用 `document_reader` skill：

| 格式 | 提取能力 |
|------|---------|
| PDF | 文本 + 表格 + 页数 + 元信息（pdfplumber） |
| DOCX | 段落 + 表格 + 标题结构（python-docx） |
| XLSX | 多 Sheet 数据 + 表头 + 数据行（openpyxl） |
| PPTX | 幻灯片文本 + 结构（python-pptx） |
| PNG/JPG/JPEG | OCR 文字识别（pytesseract），不足时自动降级到视觉大模型 |

**调用方式**：将文件路径传给 `document_reader` 的 `file_path` 参数：
- `document_reader(file_path="knowledge/system/report.pdf")` — 提取 PDF 全文
- `document_reader(file_path="knowledge/system/data.xlsx")` — 提取表格数据

### 使用规则

**必须检索本地知识库的场景**：
- 用户进行**搜索类**提问时，除了调用外部搜索技能，**必须同时检索本地知识库**
- 用户进行**知识类**提问时，**必须先检索本地知识库**
- 涉及军事装备、地缘分析、特定区域态势等专业领域时，**优先搜索知识库**

**检索流程**：
1. 先查看各目录的 `_summary.md` 了解内容概况
2. 用 `Grep("关键词", path="knowledge/")` 搜索匹配内容
3. 针对性读取具体文件（纯文本用 Read，其他用 document_reader）
4. 将知识库内容整合到回答中

**其他规则**：
- 知识库内容优先级高于通用知识
- 不要向用户暴露知识库的文件路径细节
