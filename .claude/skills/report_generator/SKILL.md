---
name: report_generator
display_name: 报告生成
description: |
  【功能】把已整理好的分析内容生成为正式报告文件（md/html/png），返回文件路径。
  【必须调用时机】
  ①用户说”发报告给我/生成报告文件”时；
  ②【强制】钉钉渠道下完成了涉及3个以上工具调用的复杂分析任务，必须主动生成报告并通过send_message发给用户；
  ③用户要图片报告时，本工具自带PNG生成，不需要再调component_screenshot。
  【适用场景】”生成报告””整理成报告/简报””发报告给我””把内容写成文件”。
  【不适用】还在搜索阶段未拿到结论时、简单文字回复不需要报告格式时。
priority: 90
ui_components:
  - component: markdown_card
    condition: 展示报告正文预览
    priority: 1
    data_hints:
      - has_markdown_content
      - has_summary
keywords:
  - 报告
  - 履历
  - 简报
  - 研究报告
  - html
  - markdown
time_estimates:
  default:
    min: 3
    max: 15
    desc: "报告内容生成"
authority: medium
---
## 调用方式

通过 `bash` 工具执行：

```bash
echo '{"title":"报告标题","query":"...","report_type":"analysis","markdown":"..."}' | python3 /app/.claude/skills/report_generator/scripts/report_generator.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/report_generator/scripts/report_generator.py <<'EOF'
{"title":"报告标题","query":"...","report_type":"analysis","markdown":"# 报告内容\n\n...","skip_png":true}
EOF
```

# 报告生成技能

## 执行方式

- 使用 `python3` 直接执行 `scripts/*.py`
- 支持命令行参数或 stdin JSON 输入
- 不使用 `mcp__...` 工具名

把你已经整理好的结论变成正式报告内容。
这个 skill 的核心能力只有一件事：**把已有结论整理成正式报告**。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| title | string | 是 | 报告标题 |
| query | string | 否 | 原始查询/任务描述 |
| report_type | string | 否 | 报告类型: analysis/profile/resume/research/briefing/timeline，默认 "analysis" |
| report_name | string | 否 | 报告文件名（小写英文+下划线），默认由 title 生成 |
| markdown | string | 否 | 完整 Markdown 报告正文（推荐，可控性最高） |
| summary | string | 否 | 导语/摘要（无 markdown 时使用） |
| sections | array | 否 | 章节列表，每项 `{"title": "...", "content": "..."}`（无 markdown 时使用） |
| sources | array | 否 | 来源列表，每项 `{"name": "...", "url": "...", "author": "...", "publication_date": "...", "confidence": 0.9}` |
| confidence_level | string | 否 | 可信度等级: 高/中/低 |
| confidence_reasoning | string | 否 | 可信度说明 |
| uncertainties | array | 否 | 不确定因素列表 |
| report_id | string | 否 | 报告ID，默认自动生成 |
| skip_png | boolean | 否 | 是否跳过 PNG 生成，默认 false |

## 能力

`report_generator` 可以：

- 生成 Markdown 正文
- 生成 HTML 报告
- 统一报告结构、标题、章节、来源、可信度段落
- 把 `summary + sections + sources` 组装成完整报告
- 返回报告正文和元数据，供后续继续截图、发送或展示

`report_generator` 不可以：

- 不能自己搜索资料
- 不能自己判断事实是否真实，前提是你已经完成搜索/分析
- 不发送消息给用户（发送需调 send_message）

## 什么时候用

- 用户明确要“生成报告”“整理成报告”“发我报告”
- 你已经完成搜索/分析，下一步需要正式的结构化报告内容
- 你要把人物履历、事件梳理、行业研究、风险评估整理成正式文件

## 不该用的时候

- 还没拿到核心事实，仍在搜索阶段
- 用户只要一段普通文本回复，不需要正式报告结构

## 你需要自己决定报告类型

`report_type` 由你根据任务决定，常见值：

- `analysis`：综合分析报告
- `profile`：人物档案
- `resume`：履历整理
- `research`：研究报告
- `briefing`：简报
- `timeline`：时间线报告

## 推荐调用方式

优先直接传完整 `markdown`，这样可控性最高。

```json
{
  "title": "比尔盖茨详细履历与事迹报告",
  "query": "获取比尔盖茨的详细信息，包括履历和事迹",
  "report_type": "profile",
  "report_name": "bill_gates_profile_report",
  "markdown": "# 比尔盖茨详细履历与事迹报告\n\n...",
  "sources": [
    {
      "name": "Bill & Melinda Gates Foundation",
      "url": "https://www.gatesfoundation.org/...",
      "author": "Gates Foundation",
      "publication_date": "2025-12-01",
      "confidence": 0.9
    }
  ],
  "confidence_level": "中",
  "confidence_reasoning": "已抓取基金会官网与主流媒体，多源交叉验证，但部分早年细节仍依赖二手资料",
  "uncertainties": [
    "部分早年时间点在不同资料中表述略有差异"
  ],
  "skip_png": true
}
```

## 备选调用方式

如果你还没拼成完整 Markdown，可以传 `summary + sections`，skill 会帮你拼装：

```json
{
  "title": "某人物履历报告",
  "query": "整理某人物履历和主要事迹",
  "report_type": "resume",
  "summary": "这里放导语或摘要。",
  "sections": [
    {"title": "早年经历", "content": "..."},
    {"title": "职业生涯", "content": "..."},
    {"title": "关键事迹", "content": "..."}
  ]
}
```

## 输入要求

最低建议输入：

- `title`
- `query`
- `report_type`
- `markdown`

如果没有完整 `markdown`，至少传：

- `title`
- `query`
- `report_type`
- `summary`
- `sections`

如果有来源和可信度信息，应该一并传入：

- `sources`
- `confidence_level`
- `confidence_reasoning`
- `uncertainties`

## 输出

成功后返回：

- `report_id`
- `report_type`
- `report_name`
- `markdown`（报告正文）
- `html`（HTML正文）
- `md_file_path`（.md 文件绝对路径，直接给 send_message content 用）
- `html_file_path`（.html 文件绝对路径）
- `png_file_path`（PNG 图片绝对路径，直接给 send_message content 用，不需要再调 component_screenshot）
- `generated_at`

## 使用要求

- 报告正文由你负责组织，skill 只做结构化生成
- 需要文件名时传 `report_name`，只用小写英文和下划线
- 用户要图片报告：调本 skill（不传 skip_png）→ 拿 `png_file_path` → 调 `send_message(msg_type=”image”, content=png_file_path)`
- 用户要文件报告：调本 skill（传 skip_png=true）→ 拿 `md_file_path` → 调 `send_message(msg_type=”file”, content=md_file_path)`
- **不需要再调 component_screenshot**，PNG 由本 skill 直接生成
- **content 传文件路径，不传 HTTP URL**

## 调用示例

```json
{
  "title": "贵州茅台2026年投资价值分析报告",
  "query": "贵州茅台值得投资吗",
  "report_type": "analysis",
  "markdown": "# 贵州茅台2026年投资价值分析报告\n\n## 摘要\n贵州茅台（600519.SH）是中国白酒行业龙头...\n\n## 财务分析\n2025年营收1200亿元，同比增长15.2%...\n\n## 估值分析\n当前PE 28.5倍，处于历史30%分位...\n\n## 投资建议\n中长期仍具配置价值...",
  "sources": [{"name": "东方财富", "url": "https://www.eastmoney.com/", "confidence": 0.9}],
  "confidence": 0.85,
  "skip_png": false
}
```
