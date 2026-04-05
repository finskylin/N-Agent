"""
Agent 能力评测框架 — LLM-as-Judge 评分器（方案2：主观评分）

评分维度（各 0-10 分）：
- data_source      (权重 0.4): 数据来源清晰度
- logic_coherence  (权重 0.3): 分析逻辑连贯性
- conclusion_support (权重 0.3): 结论是否有数据支撑
"""
from __future__ import annotations

import json
import os
from typing import List

from loguru import logger

from .models import EvalQuestion, ToolCallRecord, JudgeScore

_JUDGE_PROMPT_TEMPLATE = """\
你是一个严格的AI回答质量评审员。请对以下Agent回答进行评分。

【用户问题】
{question}

【Agent回答】
{answer}

【工具调用记录摘要】
{tool_summary}

【评判重点】
{judge_focus}

请对以下三个维度分别打分（0-10分），并给出简短理由：

1. 数据来源（0-10）：回答中的数据/数字是否有明确的工具调用来源？
   - 10分：所有数据都有来源，无任何裸数字
   - 5分：部分数据有来源，部分数字来源不明
   - 0分：大量无来源数字，疑似幻觉

2. 逻辑自洽（0-10）：分析逻辑是否连贯，结论是否从数据推导？
   - 10分：推理链条清晰，每步有依据
   - 5分：基本逻辑通顺，但有跳跃
   - 0分：逻辑混乱或自相矛盾

3. 结论支撑（0-10）：最终结论/建议是否有数据支撑？
   - 10分：结论完全基于前面的数据分析
   - 5分：结论与分析部分对应
   - 0分：结论凭空而来，无数据支撑

请严格按以下JSON格式返回：
{{"data_source": {{"score": 8, "reason": "..."}}, "logic_coherence": {{"score": 7, "reason": "..."}}, "conclusion_support": {{"score": 9, "reason": "..."}}, "overall_comment": "..."}}
"""

_DEFAULT_SCORE = 5.0  # LLM 调用失败时的默认分


class JudgeEvaluator:
    """LLM 裁判评分器"""

    def __init__(self, llm_provider=None):
        # 复用 agent_core 的 call_llm（已有全局配置，支持 failover）
        pass

    async def evaluate(
        self,
        question: EvalQuestion,
        trace: List[ToolCallRecord],
        answer: str,
    ) -> JudgeScore:
        """调用 LLM 对回答质量打分"""

        tool_summary = self._build_tool_summary(trace)
        judge_focus_text = "\n".join(f"- {f}" for f in question.judge_focus)

        prompt = _JUDGE_PROMPT_TEMPLATE.format(
            question=question.question,
            answer=answer[:3000],  # 限制长度，避免超 token
            tool_summary=tool_summary,
            judge_focus=judge_focus_text,
        )

        try:
            from agent_core.agentloop.llm_provider import call_anthropic_api
            raw_text = await call_anthropic_api(
                prompt=prompt,
                use_small_fast=False,
                max_tokens=1024,
                timeout=90.0,
            )
            if not raw_text:
                return self._default_score()
            return self._parse_response(raw_text)
        except Exception as e:
            logger.warning(f"[JudgeEvaluator] LLM call failed: {e}, using default score")
            return self._default_score()

    def _build_tool_summary(self, trace: List[ToolCallRecord]) -> str:
        """生成工具调用摘要（工具名 + 输出前100字）"""
        if not trace:
            return "（无工具调用记录）"

        lines = []
        for i, record in enumerate(trace, 1):
            output_preview = (record.skill_output or "")[:100].replace("\n", " ")
            lines.append(f"{i}. [{record.skill_name}] 输出摘要: {output_preview}...")
        return "\n".join(lines)

    def _parse_response(self, raw_text: str) -> JudgeScore:
        """解析 LLM JSON 输出"""
        # 提取 JSON 部分（LLM 有时会在 JSON 前后加说明）
        text = raw_text.strip()
        # 找到第一个 { 和最后一个 }
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end == 0:
            logger.warning("[JudgeEvaluator] No JSON found in response, using default score")
            return self._default_score()

        json_str = text[start:end]
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(f"[JudgeEvaluator] JSON parse error: {e}, using default score")
            return self._default_score()

        def _get_score(key: str) -> float:
            entry = data.get(key, {})
            if isinstance(entry, dict):
                return float(entry.get("score", _DEFAULT_SCORE))
            return _DEFAULT_SCORE

        def _get_reason(key: str) -> str:
            entry = data.get(key, {})
            if isinstance(entry, dict):
                return str(entry.get("reason", ""))
            return ""

        data_source = _get_score("data_source")
        logic_coherence = _get_score("logic_coherence")
        conclusion_support = _get_score("conclusion_support")

        # 归一化加权总分 (0-10)
        total = round(
            data_source * 0.4
            + logic_coherence * 0.3
            + conclusion_support * 0.3,
            2,
        )

        return JudgeScore(
            data_source=data_source,
            logic_coherence=logic_coherence,
            conclusion_support=conclusion_support,
            total=total,
            reasons={
                "data_source": _get_reason("data_source"),
                "logic_coherence": _get_reason("logic_coherence"),
                "conclusion_support": _get_reason("conclusion_support"),
            },
            overall_comment=str(data.get("overall_comment", "")),
        )

    def _default_score(self) -> JudgeScore:
        """LLM 调用失败时的默认评分"""
        return JudgeScore(
            data_source=_DEFAULT_SCORE,
            logic_coherence=_DEFAULT_SCORE,
            conclusion_support=_DEFAULT_SCORE,
            total=_DEFAULT_SCORE,
            reasons={},
            overall_comment="评分失败，使用默认分",
        )
