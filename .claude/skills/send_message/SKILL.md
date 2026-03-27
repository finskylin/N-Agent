---
name: send_message
display_name: 发送消息
description: |
  向【当前对话】发送消息、图片或文件。
  【必须调用时机】
  ①用户说"发给我/发报告/发图片/发文件/把XX发给我"时；
  ②过程中生成了图表/截图/文件后立即发，不要攒到最后；
  ③【强制】钉钉渠道下完成了复杂分析任务（涉及3个以上工具调用），必须主动生成报告文件并发给用户；
  ④用户要图片时：先调对应skill生成→再调component_screenshot截图→再发出。
  【不适用】简单文字回复直接输出即可，不需要调用本工具。
priority: 99
intents:
  - send_message
  - intermediate_reply
  - progress_update
  - send_report
  - send_file
  - send_image
keywords:
  - 发送消息
  - 中间结果
  - 进展通报
  - 发报告
  - 发文件
  - 发图片
  - 发给我
  - 把报告发给我
  - PNG报告
  - 截图报告
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/send_message/scripts/send_message.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/send_message/scripts/send_message.py <<'EOF'
{...json参数...}
EOF
```


## send_message

### 消息发送节奏

使用 `send_message` 工具在分析过程中主动向用户推送进展。

**强制时机（不可跳过）：**
1. **任务开始**：调用任何其他工具之前，先用 `send_message` 发一句确认语（15-25字，说明正在做什么）
2. **阶段发现**：获得关键数据后发送阶段性结论预览，不要等所有工具全部跑完才开口
3. **图表/文件生成后**：立即发送，不要攒到最后统一发
4. **报告文件生成后**：分析完成若产出了 .md / .html 报告，主动调用 `send_message(msg_type="file", content=<文件URL>, title="分析报告")`，无需等用户要求

**使用原则：**
- 每个任务 2-4 条为宜，避免信息轰炸
- 确认语必须包含对问题的简短复述，不要发空泛的"正在处理中..."
- 搜索结果不充分时主动换关键词、换引擎、追加抓取，不要轻易放弃
- **结果不充分时不要放弃，要自主迭代解决**：工具返回的结果不够、为空、或格式不对时，必须主动重试（调整参数）、换方案（用其他工具或 bash）、或创建新 skill。你是一个自主 Agent，不是一次性调用者——遇到障碍要像工程师一样 debug 和迭代，直到拿到用户需要的结果

### 执行方式

- 使用 `python3` 直接执行 `scripts/*.py`
- 支持命令行参数或 stdin JSON 输入
- 不使用 `mcp__...` 工具名

向当前对话发送消息。支持文本、图片、文件三种类型。

### 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | 是 | text时填Markdown，image时填图片URL，file时填文件URL |
| `msg_type` | string | 否 | "text"(默认) / "image" / "file" |
| `title` | string | 否 | 消息标题，默认"分析进展" |

### 示例

```json
{"content": "## 初步发现\n\n数据已获取...", "msg_type": "text", "title": "情报速报"}
{"content": "http://127.0.0.1:8000/api/files/download?path=generated/charts/trend.png", "msg_type": "image", "title": "趋势图"}
{"content": "http://127.0.0.1:8000/api/files/download?path=generated/reports/abc.md", "msg_type": "file", "title": "分析报告"}
```

## 调用示例

```json
{
  "content": "## 贵州茅台分析完成\n\n核心结论：当前估值合理，建议持有。PE 28.5倍，ROE 31.5%。",
  "msg_type": "text",
  "title": "分析进展"
}
```
