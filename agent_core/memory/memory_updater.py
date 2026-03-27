"""
Memory Updater — STM→MTM→LTM 晋升管理

职责:
- on_summarize: ConversationHistory 摘要后将语义页面晋升到 MTM
- on_turn_end: 每轮对话结束后并行更新 UserProfile 和知识库
- check_mtm_promotion: 高热度 MTM 页面提取知识到 LTM

晋升链路:
  STM (ConversationHistory) → MTM (MidTermMemory) → LTM (UserProfile + Knowledge)
"""
import asyncio
from typing import List, Dict, Optional, Callable, Awaitable, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from agent_core.memory.mid_term_memory import MidTermMemory
    from agent_core.memory.long_term_memory import UserProfileStore
    from agent_core.session.experience_store import ExperienceStoreCore as ExperienceStore


class MemoryUpdater:
    """
    记忆晋升管理器

    Args:
        mtm: MidTermMemory 实例
        profile_store: UserProfileStore 实例
        experience_store: ExperienceStore 实例（知识库维度）
        profile_update_fn: LLM 画像更新函数
            签名: (user_msg, assistant_msg, current_dims) → Dict[str, str]
        knowledge_extract_fn: LLM 知识提取函数
            签名: (user_msg, assistant_msg) → Dict[str, list]
        ltm_promotion_threshold: MTM→LTM 晋升热度阈值
    """

    def __init__(
        self,
        mtm: "MidTermMemory",
        profile_store: "UserProfileStore",
        experience_store: Optional["ExperienceStore"] = None,
        profile_update_fn: Optional[
            Callable[[str, str, Dict], Awaitable[Dict]]
        ] = None,
        knowledge_extract_fn: Optional[
            Callable[[str, str], Awaitable[Dict[str, list]]]
        ] = None,
        ltm_promotion_threshold: float = 5.0,
    ):
        self._mtm = mtm
        self._profile = profile_store
        self._experience = experience_store
        self._profile_update_fn = profile_update_fn
        self._knowledge_extract_fn = knowledge_extract_fn
        self._ltm_threshold = ltm_promotion_threshold

    async def on_summarize(
        self,
        session_id: str,
        summary: str,
        topics: List[str],
        entities: List[str],
        msg_range_start: int = 0,
        msg_range_end: int = 0,
        interaction_length: int = 0,
    ):
        """
        STM → MTM 晋升

        在 ConversationHistory.maybe_summarize() 完成后调用。
        将摘要页面存入 MTM，含主题和实体标签。
        """
        try:
            page_id = await self._mtm.promote(
                session_id=session_id,
                summary=summary,
                topics=topics,
                entities=entities,
                msg_range_start=msg_range_start,
                msg_range_end=msg_range_end,
                interaction_length=interaction_length,
            )
            logger.info(
                f"[MemoryUpdater] STM→MTM promotion: "
                f"session={session_id}, page={page_id}"
            )
        except Exception as e:
            logger.warning(f"[MemoryUpdater] STM→MTM promotion failed: {e}")

    async def on_turn_end(
        self,
        session_id: str,
        user_msg: str,
        assistant_msg: str,
    ):
        """
        每轮对话结束后的 LTM 更新

        顺序执行（避免 SQLite 写锁竞争）:
        1. 更新用户画像（UserProfile）
        2. 提取知识到 ExperienceStore + 晋升到全局库
        """
        if self._profile_update_fn:
            try:
                await self._update_profile(user_msg, assistant_msg)
            except Exception as e:
                logger.warning(f"[MemoryUpdater] on_turn_end profile update failed: {e}")

        if self._knowledge_extract_fn and self._experience:
            try:
                await self._extract_knowledge(session_id, user_msg, assistant_msg)
            except Exception as e:
                logger.warning(f"[MemoryUpdater] on_turn_end knowledge extract failed: {e}")

    async def check_mtm_promotion(self, session_id: str):
        """
        MTM → LTM 晋升检查

        检查高热度 (≥ threshold) 的 MTM 页面，
        将其知识提取到 ExperienceStore 的 user_knowledge/system_knowledge 维度。
        """
        if not self._experience or not self._knowledge_extract_fn:
            return

        try:
            pages = await self._mtm.recall(top_k=10)
            promoted = 0
            for page in pages:
                if page.heat_score >= self._ltm_threshold and page.summary:
                    try:
                        knowledge = await self._knowledge_extract_fn(
                            page.summary, "",
                        )
                        if knowledge:
                            await self._experience.extract_and_save(
                                session_id,
                                page.summary,
                                "",
                                extract_fn=lambda u, a: asyncio.coroutine(
                                    lambda: knowledge
                                )(),
                            )
                            promoted += 1
                    except Exception as e:
                        logger.debug(
                            f"[MemoryUpdater] MTM→LTM for page "
                            f"{page.page_id} failed: {e}"
                        )

            if promoted > 0:
                logger.info(
                    f"[MemoryUpdater] MTM→LTM promotion: "
                    f"{promoted} pages promoted"
                )
        except Exception as e:
            logger.warning(f"[MemoryUpdater] MTM→LTM check failed: {e}")

    async def _update_profile(self, user_msg: str, assistant_msg: str):
        """更新用户画像"""
        await self._profile.update_from_conversation(
            user_msg, assistant_msg,
            update_fn=self._profile_update_fn,
        )

    async def _extract_knowledge(
        self, session_id: str, user_msg: str, assistant_msg: str,
    ):
        """提取知识到 ExperienceStore，并自动晋升到 user 级全局库"""
        try:
            knowledge = await self._knowledge_extract_fn(
                user_msg, assistant_msg,
            )
            if knowledge and self._experience:
                await self._experience._merge_experience(
                    session_id, knowledge,
                )
                # 自动晋升到 user 级全局库（跨 session 共享）
                await self._experience.promote_to_global(session_id)
                logger.debug(
                    f"[MemoryUpdater] Knowledge extracted and promoted for {session_id}"
                )
        except Exception as e:
            logger.warning(f"[MemoryUpdater] Knowledge extraction failed: {e}")
