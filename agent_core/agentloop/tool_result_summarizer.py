"""
ToolResultSummarizer — 大型工具结果 LLM 语义总结

工具返回超大结果时（超过 threshold 字符），调用 LLM 进行语义压缩，
保留关键数字、URL、股票代码等标识符，丢弃冗余内容。

接入点：loop.py 中 POST_TOOL_USE hook 完成后、add_tool_results() 之前。
不在 skill_invoker 接入，原因：hook 需要完整原始数据。
"""
from __future__ import annotations

import asyncio
from typing import List, Optional
from loguru import logger

from .message_types import ToolResult


class ToolResultSummarizer:
    """
    大型工具结果 LLM 语义总结器

    当工具结果字符数超过 threshold 时，调用 LLM 进行语义压缩。
    失败时降级为硬截断（保留前 hard_limit 字符）。
    """

    def __init__(
        self,
        llm_provider,
        threshold: int = 20000,
        hard_limit: int = 50000,
        enabled: bool = True,
        timeout: float = 60.0,
    ):
        self._llm = llm_provider
        self._threshold = threshold
        self._hard_limit = hard_limit
        self._enabled = enabled
        self._timeout = timeout

    async def maybe_summarize_batch(
        self, results: List[ToolResult], user_task: str = ""
    ) -> List[ToolResult]:
        """批量处理，超阈值的并发总结（asyncio.gather）"""
        if not self._enabled or not results:
            return results

        tasks = []
        indices_to_summarize = []
        for i, result in enumerate(results):
            content = result.content or ""
            if len(content) > self._threshold:
                indices_to_summarize.append(i)
                tasks.append(
                    self.maybe_summarize(result, result.name, user_task)
                )

        if not tasks:
            return results

        summarized = await asyncio.gather(*tasks, return_exceptions=True)

        new_results = list(results)
        for idx, (result_idx, summarized_result) in enumerate(
            zip(indices_to_summarize, summarized)
        ):
            if isinstance(summarized_result, Exception):
                logger.debug(
                    f"[ToolResultSummarizer] summarize failed for {results[result_idx].name}: {summarized_result}"
                )
                # 降级：硬截断
                original = results[result_idx]
                content = original.content or ""
                if len(content) > self._hard_limit:
                    truncated = (
                        content[: self._hard_limit]
                        + f"[TRUNCATED: original {len(content)} chars]"
                    )
                    new_results[result_idx] = ToolResult(
                        tool_call_id=original.tool_call_id,
                        name=original.name,
                        content=truncated,
                        raw_data=original.raw_data,
                        is_error=original.is_error,
                        duration_ms=original.duration_ms,
                    )
            else:
                new_results[result_idx] = summarized_result

        return new_results

    async def maybe_summarize(
        self, result: ToolResult, tool_name: str, user_task: str = ""
    ) -> ToolResult:
        """单条，超阈值时调用 LLM 总结，返回新 ToolResult"""
        content = result.content or ""
        if not self._enabled or len(content) <= self._threshold:
            return result

        original_len = len(content)
        try:
            summary = await asyncio.wait_for(
                self._call_llm_summarize(content, tool_name, user_task),
                timeout=self._timeout,
            )
            new_content = f"[SUMMARY of {original_len} chars]\n{summary}"
            logger.info(
                f"[ToolResultSummarizer] Summarized '{tool_name}': "
                f"{original_len} → {len(new_content)} chars"
            )
            return ToolResult(
                tool_call_id=result.tool_call_id,
                name=result.name,
                content=new_content,
                raw_data=result.raw_data,
                is_error=result.is_error,
                duration_ms=result.duration_ms,
            )
        except Exception as e:
            logger.warning(
                f"[ToolResultSummarizer] LLM summarize failed for '{tool_name}': {e}, "
                f"falling back to hard truncation"
            )
            # 降级：硬截断
            if len(content) > self._hard_limit:
                truncated = (
                    content[: self._hard_limit]
                    + f"[TRUNCATED: original {original_len} chars]"
                )
                return ToolResult(
                    tool_call_id=result.tool_call_id,
                    name=result.name,
                    content=truncated,
                    raw_data=result.raw_data,
                    is_error=result.is_error,
                    duration_ms=result.duration_ms,
                )
            return result

    async def _call_llm_summarize(
        self, content: str, tool_name: str, user_task: str
    ) -> str:
        """调用 LLM 进行语义总结"""
        prompt = (
            f"工具 '{tool_name}' 返回了大型结果（{len(content)} 字符）。请结合用户任务进行简洁总结。\n\n"
            f"用户任务：{user_task}\n\n"
            "总结要求：\n"
            "- 结构化数据（表格、列表）：保留关键汇总数字，省略重复行\n"
            "- 财务数据：必须保留所有数字、百分比、日期\n"
            "- 搜索/新闻结果：保留标题、关键结论、来源 URL\n"
            "- 文档内容：保留结构大纲和核心段落\n"
            "- 始终保留：数字、URL、文件路径、股票代码、ID 等关键标识符\n\n"
            f"内容：\n{content}\n\n"
            "简洁总结："
        )

        # 使用 LLM provider 的非流式调用接口
        try:
            # 尝试使用 call_non_stream 方法（如果有）
            if hasattr(self._llm, "call_non_stream"):
                result = await self._llm.call_non_stream(
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=2048,
                    timeout=self._timeout,
                )
                return result.content or ""
            else:
                # 回退到 call_llm 全局函数
                from agent_core.agentloop.llm_provider import call_llm
                return await call_llm(
                    prompt=prompt,
                    use_small_fast=True,
                    max_tokens=2048,
                    timeout=self._timeout,
                )
        except Exception as e:
            raise RuntimeError(f"LLM summarize call failed: {e}") from e
