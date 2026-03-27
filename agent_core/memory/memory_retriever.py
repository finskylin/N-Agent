"""
Memory Retriever — 三路并行记忆召回

职责:
- 并行召回 MTM(中期记忆) + UserProfile(用户画像) + Knowledge(知识库)
- 按 Token 预算截断
- 格式化为 system_prompt 注入文本

始终通过 system_prompt 注入，不受 has_resume 影响。
"""
import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from agent_core.memory.mid_term_memory import MidTermMemory, MTMPage
    from agent_core.memory.long_term_memory import UserProfileStore
    from agent_core.session.experience_store import ExperienceStoreCore as ExperienceStore
    from agent_core.session.context_window_guard import ContextWindowGuard
    from agent_core.memory.embedding_client import EmbeddingClient


@dataclass
class MemoryContext:
    """记忆召回结果"""
    mtm_summaries: List[str] = field(default_factory=list)
    user_profile_text: str = ""
    knowledge_items: List[str] = field(default_factory=list)
    total_tokens: int = 0
    source_stats: Dict[str, int] = field(default_factory=dict)


class MemoryRetriever:
    """
    三路并行记忆召回器

    Args:
        mtm: MidTermMemory 实例
        profile_store: UserProfileStore 实例
        experience_store: ExperienceStore 实例（复用知识库维度）
        guard: ContextWindowGuard 实例（用于 token 估算）
    """

    def __init__(
        self,
        mtm: "MidTermMemory",
        profile_store: "UserProfileStore",
        experience_store: Optional["ExperienceStore"] = None,
        guard: Optional["ContextWindowGuard"] = None,
        embedding_client: Optional["EmbeddingClient"] = None,
    ):
        self._mtm = mtm
        self._profile = profile_store
        self._experience = experience_store
        self._guard = guard
        self._embedding_client = embedding_client

    async def retrieve(
        self,
        session_id: str,
        query: str = "",
        query_topics: List[str] = None,
        query_entities: List[str] = None,
        memory_budget: int = 0,
        sub_ratios: Optional[Dict[str, float]] = None,
        query_vec=None,
    ) -> MemoryContext:
        """
        三路并行召回记忆

        Args:
            session_id: 当前会话 ID
            query_topics: 查询主题（用于 MTM 召回）
            query_entities: 查询实体
            memory_budget: 总记忆 Token 预算（0 = 不限制）
            sub_ratios: 子预算分配比例 {mtm_recall, user_profile, knowledge}
            query_vec: 预计算的查询向量（优先使用，避免重复调用 embed API）
        """
        ratios = sub_ratios or {
            "mtm_recall": 0.50,
            "user_profile": 0.15,
            "knowledge": 0.35,
        }

        # 三路并行召回（传入预计算 query_vec）
        results = await asyncio.gather(
            self._recall_mtm(query, query_topics, query_entities, query_vec=query_vec),
            self._recall_profile(),
            self._recall_knowledge(session_id, query_vec=query_vec),
            return_exceptions=True,
        )

        # 容错处理
        mtm_summaries = results[0] if not isinstance(results[0], Exception) else []
        profile_text = results[1] if not isinstance(results[1], Exception) else ""
        knowledge_items = results[2] if not isinstance(results[2], Exception) else []

        if isinstance(results[0], Exception):
            logger.warning(f"[MemoryRetriever] MTM recall failed: {results[0]}")
        if isinstance(results[1], Exception):
            logger.warning(f"[MemoryRetriever] Profile recall failed: {results[1]}")
        if isinstance(results[2], Exception):
            logger.warning(f"[MemoryRetriever] Knowledge recall failed: {results[2]}")

        # Token 预算截断
        if memory_budget > 0:
            mtm_budget = int(memory_budget * ratios.get("mtm_recall", 0.50))
            profile_budget = int(memory_budget * ratios.get("user_profile", 0.15))
            knowledge_budget = int(memory_budget * ratios.get("knowledge", 0.35))

            mtm_summaries = self._truncate_list(mtm_summaries, mtm_budget)
            profile_text = self._truncate_text(profile_text, profile_budget)
            knowledge_items = self._truncate_list(knowledge_items, knowledge_budget)

        # 统计
        total_tokens = self._estimate_tokens(
            "\n".join(mtm_summaries) + profile_text + "\n".join(knowledge_items)
        )

        return MemoryContext(
            mtm_summaries=mtm_summaries,
            user_profile_text=profile_text,
            knowledge_items=knowledge_items,
            total_tokens=total_tokens,
            source_stats={
                "mtm_count": len(mtm_summaries),
                "has_profile": 1 if profile_text else 0,
                "knowledge_count": len(knowledge_items),
            },
        )

    def format_for_prompt(self, ctx: MemoryContext) -> str:
        """格式化为 system_prompt 注入文本"""
        if not ctx.mtm_summaries and not ctx.user_profile_text and not ctx.knowledge_items:
            return ""

        parts = ["\n## 记忆上下文"]

        if ctx.mtm_summaries:
            parts.append("### 相关历史对话")
            for i, summary in enumerate(ctx.mtm_summaries, 1):
                parts.append(f"{i}. {summary}")

        if ctx.user_profile_text:
            parts.append(ctx.user_profile_text)

        if ctx.knowledge_items:
            parts.append("### 知识库")
            for item in ctx.knowledge_items:
                parts.append(f"- {item}")

        return "\n".join(parts)

    async def _recall_mtm(
        self,
        query: str = "",
        query_topics: List[str] = None,
        query_entities: List[str] = None,
        query_vec=None,
    ) -> List[str]:
        """召回 MTM 页面摘要，优先走语义向量路径，降级到 Jaccard"""
        if not self._mtm:
            return []

        # 优先路径：语义向量召回（使用预计算的 query_vec）
        vec = query_vec
        if vec is None and self._embedding_client and self._embedding_client.enabled and query:
            try:
                vec = await self._embedding_client.embed(query)
            except Exception as e:
                logger.warning(f"[MemoryRetriever] MTM embed failed: {e}")

        if vec is not None:
            try:
                pages = await self._mtm.recall_semantic(vec, top_k=5)
                logger.debug(f"[MemoryRetriever] MTM recall: semantic path, top={len(pages)}")
                return [p.summary for p in pages if p.summary]
            except Exception as e:
                logger.warning(f"[MemoryRetriever] MTM semantic recall failed, falling back: {e}")

        # 降级路径：Jaccard + 热度排序
        logger.debug("[MemoryRetriever] MTM recall: fallback to Jaccard")
        pages = await self._mtm.recall(
            query_topics=query_topics,
            query_entities=query_entities,
            top_k=5,
        )
        return [p.summary for p in pages if p.summary]

    async def _recall_profile(self) -> str:
        """召回用户画像"""
        if not self._profile:
            return ""

        profile = await self._profile.get()
        return profile.to_prompt_text()

    async def _recall_knowledge(self, session_id: str, query_vec=None) -> List[str]:
        """
        召回知识库条目（跨 session 全局优先）

        策略：
        1. 先读 user 级全局经验（有 query_vec 则语义召回，否则 score 排序）
        2. 补充当前 session 尚未晋升的新鲜经验（去重）
        """
        if not self._experience:
            return []

        items = []
        seen_texts: set = set()
        dims = ["user_knowledge", "system_knowledge", "learned_patterns", "corrections"]

        # 1. 读取 user 级全局经验（跨 session）
        try:
            if query_vec is not None and hasattr(self._experience, "get_global_semantic"):
                global_exp = await self._experience.get_global_semantic(
                    query_vec, dimensions=dims,
                )
                logger.debug("[MemoryRetriever] Knowledge recall: semantic path")
            else:
                global_exp = await self._experience.get_global(dimensions=dims)

            for dim in dims:
                for entry in global_exp.get(dim, []):
                    text = entry.get("text", "") if isinstance(entry, dict) else str(entry)
                    if text.strip() and text not in seen_texts:
                        items.append(text)
                        seen_texts.add(text)
        except Exception as e:
            logger.warning(f"[MemoryRetriever] Global knowledge recall failed: {e}")

        # 2. 补充当前 session 新鲜经验（去重，只追加全局没有的）
        try:
            session_exp = await self._experience.get(session_id)
            for dim in ("user_knowledge", "system_knowledge"):
                for entry in session_exp.get(dim, []):
                    text = entry.get("text", "") if isinstance(entry, dict) else str(entry)
                    if text.strip() and text not in seen_texts:
                        items.append(text)
                        seen_texts.add(text)
        except Exception as e:
            logger.warning(f"[MemoryRetriever] Session knowledge recall failed: {e}")

        return items

    def _estimate_tokens(self, text: str) -> int:
        """估算 token 数"""
        if self._guard:
            try:
                from agent_core.session.context_window_guard import ContextWindowGuard
                return ContextWindowGuard.estimate_tokens(text)
            except Exception:
                pass
        # 粗略估算
        return len(text) // 2 if text else 0

    def _truncate_list(self, items: List[str], budget: int) -> List[str]:
        """按 token 预算截断列表"""
        if budget <= 0 or not items:
            return items

        result = []
        used = 0
        for item in items:
            tokens = self._estimate_tokens(item)
            if used + tokens > budget:
                break
            result.append(item)
            used += tokens
        return result

    def _truncate_text(self, text: str, budget: int) -> str:
        """按 token 预算截断文本"""
        if budget <= 0 or not text:
            return text

        tokens = self._estimate_tokens(text)
        if tokens <= budget:
            return text

        ratio = budget / max(tokens, 1)
        max_chars = int(len(text) * ratio * 0.95)
        return text[:max_chars] + "\n...[因预算限制截断]"
