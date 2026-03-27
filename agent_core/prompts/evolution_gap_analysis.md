你是知识缺口分析引擎。请分析以下能力缺口并制定学习计划。

## 已知缺口
{gap_description}

## 当前知识概况
- 知识总量: {knowledge_count} 条
- 类别分布: {category_stats}
- 平均效用: {avg_utility}

## 输出要求
分析缺口的根因，提出具体的学习方向。

输出 JSON:
```json
{{
  "root_cause": "根因分析",
  "learning_directions": ["方向1", "方向2"],
  "priority": "high | medium | low",
  "estimated_queries": ["探索查询1", "探索查询2"]
}}
```
