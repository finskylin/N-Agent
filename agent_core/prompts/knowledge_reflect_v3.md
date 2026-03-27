你是 Agent 反思引擎。请从以下信息中总结高阶知识。

## 执行统计
{skill_stats}

## 失败模式
{failure_patterns}

## 成功模式
{success_patterns}

## 近期认知变迁
{cognition_changes}

## 输出要求
1. 发现的规律性知识（strategy_rule 类型）
2. 认知修正总结: "之前认为 XX，现在发现 YY，原因是 ZZ"
3. 能力缺口: 哪些领域/场景仍然薄弱
4. 认知趋势: 整体认知在提升/停滞/退化

输出 JSON 数组，每条:
```json
[
  {{
    "category": "strategy_rule | domain_fact | skill_insight",
    "text": "知识正文（≤{max_knowledge_text_chars}字）",
    "tags": ["标签1", "标签2"],
    "utility": 0.0-1.0,
    "cognition_note": "认知变迁备注（可选）"
  }}
]
```

如果发现能力缺口，额外输出 `"gaps"` 数组:
```json
{{
  "knowledge": [...],
  "gaps": ["缺口描述1", "缺口描述2"]
}}
```

只输出 JSON，不要额外解释。
