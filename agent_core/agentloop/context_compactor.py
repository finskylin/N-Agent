"""
ContextCompactor — Mid-Session 上下文压缩

当 token 使用量超过预算阈值时，调用 LLM 将历史消息压缩为摘要，
保留最近 N 条完整消息，避免 context window 溢出。

特性:
- opt-in 设计：enabled=False 时不执行任何操作
- LLM 摘要失败时降级为简单截断
- 压缩后触发 ON_CONTEXT_COMPACT hook
"""
from __future__ import annotations

from typing import Optional
from loguru import logger


class ContextCompactor:
    """
    Mid-Session 上下文压缩器

    使用方式:
        compactor = ContextCompactor(
            llm_provider=llm_provider,
            compaction_threshold=0.70,
            keep_recent=6,
            enabled=True,
        )
        compacted = await compactor.maybe_compact(context_builder, token_budget)
    """

    def __init__(
        self,
        llm_provider,           # LiteLLMProvider
        compaction_threshold: float = 0.70,
        keep_recent: int = 6,
        enabled: bool = True,
    ):
        self._llm = llm_provider
        self._threshold = compaction_threshold
        self._keep_recent = keep_recent
        self._enabled = enabled

    async def maybe_compact(
        self,
        context_builder,    # ContextBuilder
        token_budget: int,  # 以 token 数计（chars // 4 估算）
    ) -> bool:
        """
        检查 token 阈值，超过则执行 LLM 摘要压缩

        Returns:
            True 表示执行了压缩；False 表示未触发
        """
        if not self._enabled:
            return False

        messages = getattr(context_builder, "_messages", None) or getattr(context_builder, "messages", [])
        if not messages:
            return False

        # 估算当前 token 数（字符数 / 4 粗估）
        current_chars = sum(
            len(str(m.get("content", "")))
            for m in messages
            if isinstance(m, dict)
        )
        current_tokens = current_chars // 4

        if current_tokens <= self._threshold * token_budget:
            return False

        logger.info(
            f"[ContextCompactor] Triggering compaction: "
            f"current={current_tokens} tokens, budget={token_budget}, "
            f"threshold={self._threshold}"
        )

        # 分割消息：保留最近 keep_recent 条，其余压缩
        if len(messages) <= self._keep_recent:
            # 消息太少，不压缩
            return False

        to_compact = messages[:-self._keep_recent]
        to_keep = messages[-self._keep_recent:]

        # 提取文本用于摘要
        summary_input = self._extract_text(to_compact)
        if not summary_input.strip():
            return False

        # 调用 LLM 生成摘要
        try:
            summary = await self._call_summarize(summary_input)
        except Exception as e:
            logger.warning(f"[ContextCompactor] LLM summarize failed, truncating: {e}")
            summary = summary_input[:1500]

        # 替换消息数组：[摘要消息] + 最近消息
        summary_msg = {
            "role": "user",
            "content": f"[对话历史摘要]\n{summary}\n[以上为历史摘要，以下为最近对话]",
        }
        new_messages = [summary_msg] + to_keep

        # 写回 ContextBuilder
        if hasattr(context_builder, "_messages"):
            context_builder._messages = new_messages
        elif hasattr(context_builder, "messages"):
            # fallback: 尝试直接赋值
            try:
                context_builder.messages = new_messages
            except AttributeError:
                logger.warning("[ContextCompactor] Cannot write back messages to context_builder")
                return False

        logger.info(
            f"[ContextCompactor] Compacted {len(to_compact)} → 1 summary message, "
            f"kept {len(to_keep)} recent"
        )
        return True

    def _extract_text(self, messages: list) -> str:
        """从消息列表提取文本内容"""
        parts = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = m.get("role", "")
            content = m.get("content", "")
            if isinstance(content, list):
                # 多模态消息
                text_parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                content = " ".join(text_parts)
            if content and isinstance(content, str):
                parts.append(f"{role}: {content[:500]}")
        return "\n".join(parts)

    async def _call_summarize(self, text: str) -> str:
        """
        调用 LLM 生成对话摘要（中文，1500字以内）

        使用非流式调用，fallback 为截断。
        """
        system = "你是一个对话摘要助手。请用简洁的中文总结以下对话历史，保留关键信息、用户意图和已获取的数据要点，不超过1500字。"
        messages = [
            {"role": "user", "content": f"请总结以下对话历史：\n\n{text[:8000]}"}
        ]

        try:
            # 尝试非流式调用
            if hasattr(self._llm, "chat"):
                response = await self._llm.chat(
                    messages=messages,
                    system=system,
                    max_tokens=1500,
                )
                if isinstance(response, dict):
                    return response.get("content", text[:1500])
                return str(response)[:1500]

            # 降级：收集流式输出
            summary_parts = []
            async for event in self._llm.chat_stream(
                messages=messages,
                system=system,
                max_tokens=1500,
            ):
                if event.get("type") == "text_delta":
                    summary_parts.append(event.get("delta", ""))
                elif event.get("type") == "llm_response":
                    break
            return "".join(summary_parts)[:1500]

        except Exception as e:
            logger.warning(f"[ContextCompactor] _call_summarize failed: {e}")
            return text[:1500]
