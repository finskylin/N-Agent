"""
知识抽取器

使用 LLM 从对话中抽取可复用的知识条目
"""
import json
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from loguru import logger

from .models import KnowledgeEntry


class KnowledgeExtractor:
    """
    从对话中抽取知识

    使用 LLM 分析问答内容，抽取:
    - 事实性知识 (fact): 客观数据、统计数字
    - 洞察结论 (insight): 分析得出的结论
    - 关键数据 (data): 可量化的指标
    - 决策建议 (conclusion): 投资/行动建议
    """

    def __init__(self, llm_client=None):
        """
        初始化抽取器

        Args:
            llm_client: LLM 客户端（需要有 complete 方法）
                        如果为 None，则使用默认的 OpenAI 客户端
        """
        self.llm = llm_client

    async def extract(
        self,
        query: str,
        answer: str,
        skill_results: List[Dict[str, Any]] = None,
        data_sources: List[Dict[str, Any]] = None
    ) -> List[KnowledgeEntry]:
        """
        抽取知识条目

        Args:
            query: 用户问题
            answer: LLM 回答
            skill_results: 技能执行结果列表
            data_sources: 数据来源列表

        Returns:
            知识条目列表
        """
        skill_results = skill_results or []
        data_sources = data_sources or []

        # 构建抽取 prompt
        prompt = self._build_prompt(query, answer, skill_results)

        try:
            # 调用 LLM
            response = await self._call_llm(prompt)

            # 解析结果
            entries = self._parse_response(response, data_sources)

            logger.info(f"[KnowledgeExtractor] Extracted {len(entries)} entries")
            return entries

        except Exception as e:
            logger.error(f"[KnowledgeExtractor] Extraction failed: {e}")
            return []

    def _build_prompt(
        self,
        query: str,
        answer: str,
        skill_results: List[Dict[str, Any]]
    ) -> str:
        """构建抽取 prompt"""
        skill_data_str = ""
        if skill_results:
            skill_data_str = json.dumps(skill_results[:5], ensure_ascii=False, indent=2)  # 限制大小

        return f'''从以下问答中抽取可复用的知识条目。

## 用户问题
{query}

## 回答内容
{answer[:2000]}

## 技能数据
{skill_data_str}

请抽取:
1. **事实性知识 (fact)**: 客观数据、统计数字
2. **洞察结论 (insight)**: 分析得出的结论
3. **关键数据 (data)**: 可量化的指标
4. **决策建议 (conclusion)**: 投资/行动建议

输出 JSON 数组格式（最多5条）:
```json
[
  {{
    "category": "fact|insight|data|conclusion",
    "question": "提炼后的问题（简洁）",
    "answer": "简洁的答案（100字内）",
    "key_points": ["要点1", "要点2"],
    "entities": ["600519.SH", "贵州茅台"],
    "topics": ["白酒", "估值分析"],
    "data_snapshot": {{"pe": 25.3, "roe": 30.5}},
    "confidence": 0.85,
    "valid_days": 7
  }}
]
```

只输出 JSON 数组，不要其他内容。
'''

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM（带 failover）"""
        if self.llm:
            return await self.llm.complete(prompt)

        try:
            from agent_core.agentloop.llm_provider import call_llm
            return await call_llm(prompt, use_small_fast=True, max_tokens=1500, timeout=30.0)
        except Exception as e:
            logger.error(f"[KnowledgeExtractor] LLM call failed: {e}")
            raise

    def _parse_response(
        self,
        response: str,
        data_sources: List[Dict[str, Any]]
    ) -> List[KnowledgeEntry]:
        """解析 LLM 响应"""
        entries = []

        # 提取 JSON
        try:
            # 尝试找到 JSON 数组
            start = response.find('[')
            end = response.rfind(']') + 1

            if start >= 0 and end > start:
                json_str = response[start:end]
                parsed = json.loads(json_str)
            else:
                # 尝试直接解析
                parsed = json.loads(response)

            if not isinstance(parsed, list):
                parsed = [parsed]

            # 转换为 KnowledgeEntry
            source_urls = [s.get('url', '') for s in data_sources if s.get('url')]

            for item in parsed[:5]:  # 最多5条
                entry_id = str(uuid.uuid4())[:12]

                # 处理有效期
                valid_until = None
                valid_days = item.get('valid_days')
                if valid_days and isinstance(valid_days, int):
                    valid_until = datetime.now() + timedelta(days=valid_days)

                entry = KnowledgeEntry(
                    entry_id=entry_id,
                    user_id="",  # 由调用方设置
                    category=item.get('category', 'insight'),
                    question=item.get('question', ''),
                    answer=item.get('answer', ''),
                    key_points=item.get('key_points', []),
                    entities=item.get('entities', []),
                    topics=item.get('topics', []),
                    data_snapshot=item.get('data_snapshot', {}),
                    confidence=float(item.get('confidence', 0.85)),
                    source_urls=source_urls,
                    valid_until=valid_until,
                    created_at=datetime.now()
                )
                entries.append(entry)

        except json.JSONDecodeError as e:
            logger.error(f"[KnowledgeExtractor] JSON parse error: {e}")
        except Exception as e:
            logger.error(f"[KnowledgeExtractor] Parse error: {e}")

        return entries
