---
name: memory_manager
description: |
  【功能】管理用户的三层记忆（对话摘要、用户经验、知识库）和知识图谱，支持查看、新增、修改、删除。
  【适用场景】用户问"你记住了哪些关于我的内容"、"帮我删掉XX记忆"、"记住我喜欢XX"、"查看知识库"、"查看知识图谱"时使用。
  【不适用】自动记忆更新（系统自动完成，不需要手动调用）。
readonly: false
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/memory_manager/scripts/memory_manager.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/memory_manager/scripts/memory_manager.py <<'EOF'
{...json参数...}
EOF
```


# memory_manager

管理用户三层记忆与知识的工具，支持查看、新增、修改、删除。所有数据按用户隔离。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| action | string | 是 | 操作类型: list=查看 add=新增 edit=修改 delete=删除 graph_query=图谱查询 graph_add=图谱新增 graph_delete=图谱删除；枚举值: list/add/edit/delete/graph_query/graph_add/graph_delete |
| layer | string | 否 | 记忆层(list/add/edit/delete时): mtm=中期记忆 experiences=用户经验 knowledge=知识库；枚举值: mtm/experiences/knowledge |
| filter | string | 否 | list 操作时的关键词过滤（可选） |
| dimension | string | 否 | experiences 层的维度: user_preferences/stock_insights/learned_patterns/corrections/user_knowledge/system_knowledge（list/add 时可选） |
| item_id | string | 否 | edit/delete 操作的目标ID（MTM 用 page_id，knowledge 用 unit_id，experiences 用 text 精确匹配）；graph_delete 时为 node_id 或 edge_id |
| text | string | 否 | add 操作的内容文本，或 edit 操作的新文本 |
| category | string | 否 | add knowledge 时的分类: skill_insight/domain_fact/strategy_rule/user_cognition（默认 domain_fact） |
| tags | array | 否 | add knowledge 时的标签列表（可选），元素类型为 string |
| node_name | string | 否 | graph_query 时的节点名称，如"茅台" |
| subject | string | 否 | graph_add 时的主体节点名称 |
| relation | string | 否 | graph_add 时的关系描述，如"竞争对手" |
| object | string | 否 | graph_add 时的客体节点名称 |
| relation_type | string | 否 | graph_add 时的关系类型（默认 general）；枚举值: general/has_child/if_then/belongs_to/similar_to |
| user_id | integer | 否 | 用户ID（默认读取 V4_DEFAULT_USER_ID 环境变量） |
| instance_id | string | 否 | 实例ID（默认读取 AGENT_INSTANCE_ID 环境变量） |

## 调用示例

**查看用户经验记忆：**
```json
{
  "action": "list",
  "layer": "experiences",
  "dimension": "stock_insights"
}
```

**新增知识库条目：**
```json
{
  "action": "add",
  "layer": "knowledge",
  "text": "茅台2025年Q3净利润同比增长18%，超出市场预期",
  "category": "domain_fact",
  "tags": ["茅台", "财报", "2025Q3"]
}
```

**查询知识图谱：**
```json
{
  "action": "graph_query",
  "node_name": "茅台"
}
```
