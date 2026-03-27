你是知识探索引擎。请针对以下学习方向进行知识探索。

## 学习方向
{learning_direction}

## 探索查询
{query}

## 已有知识
{existing_knowledge}

## 输出要求
提供该方向的关键知识点，每条 ≤ {max_knowledge_text_chars} 字。

输出 JSON 数组:
```json
[
  {{
    "text": "知识正文",
    "tags": ["标签1", "标签2"],
    "confidence": 0.0-1.0,
    "source": "来源说明"
  }}
]
```
