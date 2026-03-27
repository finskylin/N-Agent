从这轮对话中提取可复用的经验知识，JSON格式输出：

用户: {user_message}
助手: {assistant_message}

提取维度：
- user_preferences: 用户偏好（关注的行业、指标、风格）
- stock_insights: 股票洞察（对特定股票的分析结论）
- learned_patterns: 学到的规律（用户认可的分析模式）
- corrections: 纠正记录（用户指出的错误和正确答案）

每条经验需附带 score 价值评分（0.0-1.0）：
- 0.9-1.0: 明确的纠正、重要结论、强偏好
- 0.6-0.8: 一般性洞察、偏好
- 0.3-0.5: 弱信号、不确定的模式
- < 0.3: 低价值，可丢弃

输出格式（只输出纯JSON，不要其他文字）：
{{"user_preferences": [{{"text": "...", "score": 0.8}}], "stock_insights": [{{"text": "...", "score": 0.9}}], "learned_patterns": [], "corrections": []}}
