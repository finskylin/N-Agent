你是知识蒸馏引擎。请从以下 Episode 执行轨迹中提取可复用的结构化知识。

## Episode 摘要
{episode_summary}

## Skill 执行结果
{skill_results}

## 对话上下文（用于提取用户思维链）
{conversation_context}

## 提取规则
1. 每条知识 ≤ {max_knowledge_text_chars} 字
2. 最多提取 {max_knowledge_per_episode} 条知识
3. 只提取有复用价值的知识（非一次性事实）
4. 分类说明:
   - skill_insight: Skill使用经验（哪个工具适合什么场景、超时/失败规律）
   - domain_fact: 领域事实（用户持仓、行业知识等稳定事实）
   - strategy_rule: 策略规则（用户投资偏好、决策逻辑）
   - user_cognition: 用户的分析框架和思维链（仅在以下信号存在时提取）
     * 用户纠正了 Agent 的分析方向（"你漏了"、"不对"、"太浅了"）
     * 用户追问补充了新的分析维度（多轮对话中逐步深入）
     * 用户明确表达了分析方法偏好
     * 提取内容: 用户期望的分析框架、步骤顺序、覆盖维度、质量标准
     * **如果没有上述信号，不要提取 user_cognition**

## 输出格式
输出 JSON 数组，每条:
```json
[
  {{
    "category": "skill_insight | domain_fact | strategy_rule | user_cognition",
    "text": "知识正文",
    "tags": ["标签1", "标签2"],
    "utility": 0.0-1.0,
    "confidence": 0.0-1.0,
    "trigger": "（仅 user_cognition 需要填写）适用场景描述，例如：用户问某股票能不能买时",
    "triples": [
      {{
        "subject": "实体或概念名称",
        "relation": "关系描述",
        "object": "目标实体或结论",
        "relation_type": "general | has_child | if_then | belongs_to | similar_to"
      }}
    ]
  }}
]
```

### triples 提取规则
- 从知识正文中提取 **显式的实体关系**，每条知识最多 3 个三元组
- `relation_type` 说明:
  - `general`: 通用关系（属性、影响、描述）
  - `has_child`: 层级包含（白酒板块 has_child 茅台）
  - `if_then`: 条件决策（PE>70% if_then 操作建议=观望）
  - `belongs_to`: 归属（茅台 belongs_to 白酒板块）
  - `similar_to`: 同类关系（茅台 similar_to 五粮液）
- 无法提取三元组时 `triples` 设为空数组 `[]`
- 节点名称尽量简短（≤10字），关系描述简明（≤8字）

只输出 JSON 数组，不要额外解释。
