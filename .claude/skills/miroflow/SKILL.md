---
name: miroflow
description: "【功能】在 Miro 白板上创建流程图、思维导图、架构图等可视化内容（写操作） 【数据源】Miro REST API 【输出数据】Miro 白板 URL、创建的图形元素列表、嵌入式查看链接 【耗时】~5-20秒 【适用场景】用户问'创建流程图'、'画思维导图'、'生成架构图'、'在Miro上画图'时使用"
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/miroflow/scripts/miroflow.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/miroflow/scripts/miroflow.py <<'EOF'
{...json参数...}
EOF
```


## Imported Metadata

- Original display name: `Miro 流程图创建`
- Original intents: `miro`, `flowchart`, `mindmap`, `diagram`, `visualization`
- Original keywords: `miro`, `流程图`, `思维导图`, `架构图`, `白板`, `画图`, `可视化`

## 环境变量

- `MIRO_ACCESS_TOKEN`: Miro API 访问令牌（必须）

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| query | string | 是 | 描述需要绘制的图表内容，包含节点和关系 |
| board_id | string | 否 | Miro 白板 ID，不传则使用默认白板 |
| diagram_type | string | 否 | 图表类型：flowchart/mindmap/architecture/auto，默认 auto |

## 调用示例

```json
{
  "query": "将Agent核心流程绘制为流程图：请求入口→Session准备→Prompt构建→AgentLoop→结束",
  "board_id": "uXjVI_abc123=",
  "diagram_type": "flowchart"
}
```
