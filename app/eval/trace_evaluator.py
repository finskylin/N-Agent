"""
Agent 能力评测框架 — 工具调用轨迹验证（方案3：客观评分）

评分维度：
- required_coverage: 必选工具覆盖率
- expected_coverage: 期望工具覆盖率
- data_citation: 工具输出数字是否被回答引用
- hallucination_free: 回答中数字是否有工具输出来源
"""
from __future__ import annotations

import re
from typing import List

from .models import EvalQuestion, ToolCallRecord, TraceScore


class TraceEvaluator:
    """工具调用轨迹验证器（纯客观，无 LLM 调用）"""

    # 最多取工具输出中前 N 个数字，避免过多数字稀释评分
    _MAX_TOOL_NUMBERS = 20

    # skill 名 → 其输出 JSON 中的特征字段（用于 bash 工具的内容反推）
    _SKILL_OUTPUT_SIGNATURES: dict = {
        "financial_report":    ["revenue", "net_profit", "roe", "reports"],
        "financial_statement": ["balance_sheet", "income_statement", "cash_flow"],
        "technical_indicators":["macd", "kdj", "rsi", "boll", "ma5", "ma10"],
        "valuation_analysis":  ["pe_ratio", "pb_ratio", "dcf", "percentile"],
        "money_flow":          ["main_net", "big_net", "north_flow"],
        "stock_query":         ["ts_code", "industry", "market_cap"],
        "realtime_quote":      ["bid1", "ask1", "pre_close", "vol"],
        "historical_data":     ["open", "close", "high", "low", "volume", "trade_date"],
        "sentiment_analysis":  ["sentiment_score", "sentiment_label", "news"],
        "rating":              ["total_score", "grade", "suggestion"],
        "market_overview":     ["sh_index", "sz_index", "up_count", "down_count"],
        "northbound_capital":  ["north_net", "sh_connect", "sz_connect"],
        "stock_resolver":      ["ts_code", "match_list"],
        "quick_search":        ["results", "title", "url", "snippet"],
    }

    def _infer_skills_from_trace(self, trace: List[ToolCallRecord]) -> set:
        """
        从 bash 工具的输出内容反推实际调用的 skill。
        Agent 通过 bash 执行 skill 脚本，DB 里 skill_name='bash'，
        但 raw_data_json 包含 skill 的输出结构，可通过特征字段识别。
        """
        inferred = set()
        for record in trace:
            # 直接记录的 skill 名（非 bash 时直接用）
            if record.skill_name and record.skill_name != "bash":
                inferred.add(record.skill_name)
                continue
            # bash：通过输出内容特征匹配
            output = (record.skill_output or "").lower()
            if not output:
                continue
            for skill_name, signatures in self._SKILL_OUTPUT_SIGNATURES.items():
                if any(sig in output for sig in signatures):
                    inferred.add(skill_name)
        return inferred

    def evaluate(
        self,
        question: EvalQuestion,
        trace: List[ToolCallRecord],
        answer: str,
    ) -> TraceScore:
        # 直接记录的工具名 + 从输出内容反推的 skill 名
        called_tools = self._infer_skills_from_trace(trace)
        raw_called = {r.skill_name for r in trace}

        # 1. 必选工具覆盖率
        if question.required_tools:
            required_called = [t for t in question.required_tools if t in called_tools]
            required_coverage = len(required_called) / len(question.required_tools)
        else:
            required_coverage = 1.0

        # 2. 期望工具覆盖率
        if question.expected_tools:
            expected_called = [t for t in question.expected_tools if t in called_tools]
            expected_coverage = len(expected_called) / len(question.expected_tools)
        else:
            expected_coverage = 1.0

        # 3. 数据引用率：工具输出中的数字，是否出现在回答里
        data_citation = self._check_data_citation(trace, answer)

        # 4. 幻觉洁净度：回答中的数字，是否能在工具输出中找到来源
        hallucination_free = self._check_hallucination(trace, answer)

        # 总分：各维度加权平均，归一化到 0-10
        total_raw = (
            required_coverage * 0.4
            + data_citation * 0.3
            + hallucination_free * 0.3
        )
        total = round(total_raw * 10, 2)

        detail = {
            "called_tools": sorted(called_tools),
            "raw_tools": sorted(raw_called),
            "required_tools": question.required_tools,
            "expected_tools": question.expected_tools,
            "required_called": [t for t in question.required_tools if t in called_tools],
            "expected_called": [t for t in question.expected_tools if t in called_tools],
        }

        return TraceScore(
            required_coverage=round(required_coverage, 3),
            expected_coverage=round(expected_coverage, 3),
            data_citation=round(data_citation, 3),
            hallucination_free=round(hallucination_free, 3),
            total=total,
            detail=detail,
        )

    def _extract_numbers(self, text: str) -> List[str]:
        """从文本中提取所有数字字符串（含小数）"""
        return re.findall(r'\d+\.?\d*', text)

    def _check_data_citation(
        self,
        trace: List[ToolCallRecord],
        answer: str,
    ) -> float:
        """
        检查工具输出中的数字是否被回答引用。

        做法：
        - 从所有工具输出中提取数字（最多取前 MAX_TOOL_NUMBERS 个）
        - 计算其中有多少出现在 answer 中
        """
        if not trace:
            return 0.0

        all_tool_numbers: List[str] = []
        for record in trace:
            nums = self._extract_numbers(record.skill_output or "")
            all_tool_numbers.extend(nums)
            if len(all_tool_numbers) >= self._MAX_TOOL_NUMBERS:
                break

        tool_numbers = all_tool_numbers[:self._MAX_TOOL_NUMBERS]

        if not tool_numbers:
            # 工具没有输出数字（文字类输出）—— 无法验证，给满分
            return 1.0

        cited = sum(1 for n in tool_numbers if n in answer)
        return cited / len(tool_numbers)

    def _check_hallucination(
        self,
        trace: List[ToolCallRecord],
        answer: str,
    ) -> float:
        """
        检查回答中的数字是否有工具输出来源。

        做法：
        - 从 answer 中提取所有数字
        - 对每个数字，在所有工具输出中搜索是否存在
        - 有来源的数字 / 总数字数
        - 若 answer 无数字，返回 1.0（无幻觉）
        """
        answer_numbers = self._extract_numbers(answer)
        if not answer_numbers:
            return 1.0

        # 合并所有工具输出为一个大字符串，供快速搜索
        all_outputs = " ".join(r.skill_output or "" for r in trace)

        sourced = sum(1 for n in answer_numbers if n in all_outputs)
        return sourced / len(answer_numbers)
