你是 Skill 结晶评估引擎。请评估以下分析模式是否适合结晶为独立 Skill。

## 模式描述
{pattern_description}

## 相关 Episode 摘要
{episode_summaries}

## 统计数据
- 出现次数: {occurrences}
- 成功率: {success_rate}
- 用户好评数: {like_count}

## 评估标准
1. 模式是否足够通用（非一次性场景）
2. 执行步骤是否可标准化
3. 结晶后是否会提升效率

## 输出格式
```json
{{
  "should_crystallize": true/false,
  "skill_name": "建议的 Skill 名称",
  "description": "Skill 描述",
  "workflow_steps": ["步骤1", "步骤2"],
  "prompt_template": "Skill 提示词模板",
  "confidence": 0.0-1.0,
  "reason": "评估理由"
}}
```
