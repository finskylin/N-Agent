"""
Progressive Summarizer — 渐进式摘要引擎

职责:
1. 自适应分块: 根据消息平均大小动态调整分块比例
2. 渐进式摘要: 分块 → 独立摘要 → LLM 合并
3. 身份保留: 通过 IDENTIFIER_PRESERVATION_INSTRUCTIONS 强制保留关键标识
4. 三级容错: 全量摘要 → 排除超大消息 → 描述性兜底
5. 双路径服务:
   - Resume 路径: SessionCompactor.compact() 生成 compaction summary
   - Fallback 路径: ConversationHistory.maybe_summarize() 渐进摘要

所有配置从 agent_core/config/session_compact.json 的 progressive_summarize 节读取。
"""
import json
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple, Callable, Awaitable, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from agent_core.config import V4Config
    from agent_core.session.context_window_guard import ContextWindowGuard


def _load_identifier_instructions() -> str:
    """加载标识符保留指令"""
    try:
        from agent_core.prompts.loader import load_prompt
        return load_prompt("v4_identifier_preservation")
    except Exception:
        try:
            from pathlib import Path
            p = Path(__file__).parent.parent.parent / "prompts" / "v4_identifier_preservation.md"
            if p.exists():
                return p.read_text(encoding="utf-8")
        except Exception:
            pass
    return ""


@dataclass
class SummaryChain:
    """
    摘要链 — 多级渐进摘要的存储结构

    每次渐进摘要产出一个条目，追加到 summaries 列表。
    总 Token 数用于 budget 校验。
    """
    summaries: List[Dict[str, Any]] = field(default_factory=list)
    total_tokens: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {"summaries": self.summaries, "total_tokens": self.total_tokens},
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, text: str) -> "SummaryChain":
        if not text:
            return cls()
        try:
            data = json.loads(text)
            return cls(
                summaries=data.get("summaries", []),
                total_tokens=data.get("total_tokens", 0),
            )
        except (json.JSONDecodeError, TypeError):
            return cls()


class ProgressiveSummarizer:
    """
    渐进式摘要引擎

    配置来源: agent_core/config/session_compact.json → progressive_summarize 节
    """

    def __init__(self, config: "V4Config", guard: "ContextWindowGuard"):
        self._config = config
        self._guard = guard
        self._identity_instructions = ""

        # 从 agent_core/config/session_compact.json 加载参数
        try:
            from .session_compactor import SessionCompactConfig
            ps_cfg = SessionCompactConfig.get_instance()._config.get(
                "progressive_summarize", {}
            )
        except Exception:
            ps_cfg = {}

        self._base_chunk_ratio = float(ps_cfg.get("base_chunk_ratio", 0.40))
        self._min_chunk_ratio = float(ps_cfg.get("min_chunk_ratio", 0.15))
        self._safety_margin = float(ps_cfg.get("safety_margin", 1.2))
        self._summarization_overhead = int(
            ps_cfg.get("summarization_overhead_tokens", 4096)
        )
        self._staged_parts = int(ps_cfg.get("staged_parts", 2))
        self._min_messages_for_split = int(
            ps_cfg.get("min_messages_for_split", 4)
        )
        self._oversized_threshold = float(
            ps_cfg.get("oversized_message_threshold", 0.5)
        )
        self._identity_preservation = bool(
            ps_cfg.get("identity_preservation", True)
        )

        # 加载标识符保留指令
        if self._identity_preservation:
            self._identity_instructions = _load_identifier_instructions()

    # ──── 自适应分块 ────

    def compute_adaptive_chunk_ratio(
        self, messages: List[Dict[str, str]], budget: int
    ) -> float:
        """
        根据消息平均大小自适应调整分块比例

        大消息 → 小比例（避免单 chunk 撑爆）
        小消息 → 大比例（更多消息合并摘要）

        Returns:
            比例值，范围 [min_chunk_ratio, base_chunk_ratio]
        """
        if not messages or budget <= 0:
            return self._base_chunk_ratio

        total_chars = sum(len(m.get("content", "")) for m in messages)
        avg_chars = total_chars / len(messages) if messages else 0
        avg_tokens = self._guard.estimate_tokens("x" * int(avg_chars))

        # 如果平均消息大小超过预算的 10%，缩小分块比例
        if avg_tokens > budget * 0.10:
            ratio = self._min_chunk_ratio
        elif avg_tokens > budget * 0.05:
            # 线性插值
            fraction = (avg_tokens - budget * 0.05) / (budget * 0.05)
            ratio = self._base_chunk_ratio - fraction * (
                self._base_chunk_ratio - self._min_chunk_ratio
            )
        else:
            ratio = self._base_chunk_ratio

        return max(self._min_chunk_ratio, min(ratio, self._base_chunk_ratio))

    # ──── Token 分块 ────

    def chunk_messages_by_tokens(
        self, messages: List[Dict[str, str]], max_tokens: int
    ) -> List[List[Dict[str, str]]]:
        """
        按 Token 预算将消息分块

        每个 chunk 的估算 Token 不超过 max_tokens。
        单条消息超过 max_tokens 时独立成 chunk。
        """
        if not messages or max_tokens <= 0:
            return [messages] if messages else []

        chunks = []
        current_chunk = []
        current_tokens = 0

        for msg in messages:
            msg_tokens = self._guard.estimate_tokens(msg.get("content", ""))

            if current_chunk and current_tokens + msg_tokens > max_tokens:
                chunks.append(current_chunk)
                current_chunk = [msg]
                current_tokens = msg_tokens
            else:
                current_chunk.append(msg)
                current_tokens += msg_tokens

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    # ──── 渐进式摘要（核心） ────

    async def summarize_in_stages(
        self,
        messages: List[Dict[str, str]],
        budget: int,
        summarize_fn: Callable[
            [List[Dict[str, str]], Optional[str]], Awaitable[str]
        ],
    ) -> str:
        """
        分阶段摘要: 分块 → 独立摘要 → LLM 合并

        Args:
            messages: 待摘要的消息列表 [{role, content}, ...]
            budget: Token 预算
            summarize_fn: 异步摘要函数，签名 (messages, existing_summary) -> summary_text
        """
        if not messages:
            return ""

        # 少量消息不分块
        if len(messages) < self._min_messages_for_split:
            return await self._summarize_with_fallback(
                messages, budget, summarize_fn
            )

        # 自适应分块
        chunk_ratio = self.compute_adaptive_chunk_ratio(messages, budget)
        chunk_max_tokens = int(budget * chunk_ratio * self._safety_margin)
        chunk_max_tokens = max(chunk_max_tokens, 1000)  # 最小 1000 tokens

        chunks = self.chunk_messages_by_tokens(messages, chunk_max_tokens)

        if len(chunks) <= 1:
            return await self._summarize_with_fallback(
                messages, budget, summarize_fn
            )

        logger.info(
            f"[ProgressiveSummarizer] Staged summarization: "
            f"{len(messages)} messages → {len(chunks)} chunks "
            f"(ratio={chunk_ratio:.2f}, max_tokens={chunk_max_tokens})"
        )

        # 独立摘要每个 chunk
        chunk_summaries = []
        for i, chunk in enumerate(chunks):
            try:
                # 注入标识符保留指令到 existing_summary
                existing = self._identity_instructions if self._identity_instructions else None
                summary = await summarize_fn(chunk, existing)
                if summary:
                    chunk_summaries.append(summary)
                    logger.debug(
                        f"[ProgressiveSummarizer] Chunk {i+1}/{len(chunks)} "
                        f"summarized: {len(summary)} chars"
                    )
            except Exception as e:
                logger.warning(
                    f"[ProgressiveSummarizer] Chunk {i+1} summarize failed: {e}"
                )
                # 降级：取前 200 字符作为描述
                desc = self._describe_messages(chunk)
                if desc:
                    chunk_summaries.append(desc)

        if not chunk_summaries:
            return self._describe_messages(messages)

        # 合并所有 chunk 摘要
        if len(chunk_summaries) == 1:
            return chunk_summaries[0]

        # LLM 合并
        merge_messages = [
            {
                "role": "user",
                "content": f"以下是第 {i+1} 部分的摘要:\n{s}"
            }
            for i, s in enumerate(chunk_summaries)
        ]
        try:
            merged = await summarize_fn(
                merge_messages,
                (
                    "请将以下多段摘要合并为一个连贯的总结。"
                    f"\n{self._identity_instructions}"
                    if self._identity_instructions
                    else "请将以下多段摘要合并为一个连贯的总结。"
                ),
            )
            if merged:
                return merged
        except Exception as e:
            logger.warning(f"[ProgressiveSummarizer] Merge failed: {e}")

        # 合并失败，拼接返回
        return "\n---\n".join(chunk_summaries)

    # ──── 三级容错 ────

    async def _summarize_with_fallback(
        self,
        messages: List[Dict[str, str]],
        budget: int,
        summarize_fn: Callable[
            [List[Dict[str, str]], Optional[str]], Awaitable[str]
        ],
    ) -> str:
        """
        三级容错摘要:
        Level 1: 全量消息摘要
        Level 2: 排除超大消息后摘要
        Level 3: 描述性兜底文本
        """
        # Level 1: 全量摘要
        try:
            existing = self._identity_instructions if self._identity_instructions else None
            result = await summarize_fn(messages, existing)
            if result:
                return result
        except Exception as e:
            logger.warning(f"[ProgressiveSummarizer] Level 1 (full) failed: {e}")

        # Level 2: 排除超大消息
        try:
            filtered = self._exclude_oversized(messages, budget)
            if filtered and len(filtered) < len(messages):
                existing = self._identity_instructions if self._identity_instructions else None
                result = await summarize_fn(filtered, existing)
                if result:
                    excluded_count = len(messages) - len(filtered)
                    return (
                        f"{result}\n"
                        f"（注：{excluded_count} 条超大消息未包含在此摘要中）"
                    )
        except Exception as e:
            logger.warning(
                f"[ProgressiveSummarizer] Level 2 (filtered) failed: {e}"
            )

        # Level 3: 描述性兜底
        logger.warning("[ProgressiveSummarizer] All LLM summaries failed, using descriptive fallback")
        return self._describe_messages(messages)

    def _exclude_oversized(
        self, messages: List[Dict[str, str]], budget: int
    ) -> List[Dict[str, str]]:
        """排除超过预算阈值的超大消息"""
        threshold = int(budget * self._oversized_threshold * TOKEN_TO_BYTES_RATIO)
        return [
            m for m in messages
            if len(m.get("content", "")) < threshold
        ]

    @staticmethod
    def _describe_messages(messages: List[Dict[str, str]]) -> str:
        """生成描述性文本（Level 3 兜底）"""
        if not messages:
            return "[之前有对话记录，详细内容已压缩]"

        user_msgs = [m for m in messages if m.get("role") == "user"]
        topics = []
        for m in user_msgs[:10]:
            content = m.get("content", "")
            if content:
                topic = content[:80].strip()
                if len(content) > 80:
                    topic += "..."
                topics.append(topic)

        parts = [
            f"[之前有 {len(messages)} 条对话记录，详细内容已压缩]"
        ]
        if topics:
            parts.append("讨论主题包括：")
            for i, t in enumerate(topics, 1):
                parts.append(f"  {i}. {t}")

        return "\n".join(parts)

    # ──── Resume 路径入口 ────

    async def generate_compaction_summary(
        self,
        compressed_history_messages: List[Dict],
        budget: int,
        summarize_fn: Callable[
            [List[Dict[str, str]], Optional[str]], Awaitable[str]
        ],
    ) -> str:
        """
        为 SessionCompactor 生成高质量 compaction summary block

        替代现有的简单描述性摘要:
          改造前: "[Session Compact] 压缩了 N 轮历史..."
          改造后: 调用渐进式摘要引擎，保留关键决策和标识符

        Args:
            compressed_history_messages: .jsonl 格式的历史消息
            budget: Token 预算
            summarize_fn: 异步摘要函数
        """
        # 从 .jsonl 消息格式转为简单 {role, content} 格式
        simple_messages = []
        for msg in compressed_history_messages:
            message_data = msg.get("message", {})
            content = message_data.get("content", "")
            if isinstance(content, list):
                # content 是 block 数组
                texts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            texts.append(block.get("text", ""))
                        elif block.get("type") == "tool_result":
                            result_content = block.get("content", "")
                            if isinstance(result_content, str):
                                texts.append(f"[工具结果] {result_content[:200]}")
                content = " ".join(texts)
            elif isinstance(content, str):
                pass  # 已经是字符串
            else:
                content = str(content) if content else ""

            role = message_data.get("role", "user")
            if content.strip():
                simple_messages.append({"role": role, "content": content})

        if not simple_messages:
            return "[之前有对话记录，详细内容已压缩]"

        return await self.summarize_in_stages(
            simple_messages, budget, summarize_fn,
        )

    # ──── Fallback 路径入口 ────

    async def summarize(
        self,
        messages: List[Dict[str, str]],
        existing_chain: Optional[SummaryChain],
        history_budget: int,
        summarize_fn: Callable[
            [List[Dict[str, str]], Optional[str]], Awaitable[str]
        ],
    ) -> Tuple[str, SummaryChain]:
        """
        ConversationHistory.maybe_summarize() 调用的入口

        Args:
            messages: 待摘要的旧消息
            existing_chain: 现有的摘要链（可选）
            history_budget: Token 预算
            summarize_fn: 异步摘要函数

        Returns:
            (summary_text, updated_chain)
        """
        chain = existing_chain or SummaryChain()

        # 如果有已有摘要，作为上下文传入
        existing_summary = None
        if chain.summaries:
            existing_summary = "\n".join(
                s.get("text", "") for s in chain.summaries if s.get("text")
            )

        # 渐进式摘要
        new_summary = await self.summarize_in_stages(
            messages, history_budget, summarize_fn,
        )

        if new_summary:
            # 如果有旧摘要，将新摘要与旧摘要合并
            if existing_summary:
                merge_msgs = [
                    {"role": "user", "content": f"已有摘要:\n{existing_summary}"},
                    {"role": "user", "content": f"新增内容摘要:\n{new_summary}"},
                ]
                try:
                    merged = await summarize_fn(
                        merge_msgs,
                        (
                            "请将已有摘要与新增内容合并为一个完整、连贯的总结。"
                            f"\n{self._identity_instructions}"
                            if self._identity_instructions
                            else "请将已有摘要与新增内容合并为一个完整、连贯的总结。"
                        ),
                    )
                    if merged:
                        new_summary = merged
                except Exception as e:
                    logger.warning(
                        f"[ProgressiveSummarizer] Chain merge failed: {e}, "
                        f"using concatenation"
                    )
                    new_summary = f"{existing_summary}\n\n{new_summary}"

            # 更新 chain
            import time
            chain.summaries.append({
                "text": new_summary,
                "timestamp": int(time.time()),
                "message_count": len(messages),
            })
            chain.total_tokens = self._guard.estimate_tokens(new_summary)

        return new_summary or "", chain


# 导入用于类型引用
from .context_window_guard import TOKEN_TO_BYTES_RATIO
