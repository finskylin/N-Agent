"""
Knowledge Engine Guard — 性能安全保障

提供带超时的安全包装，确保知识引擎操作不会阻塞主流程。
超时降级: retrieve 返回空字符串，finalize 投入后台。
"""
import asyncio

from loguru import logger


class KnowledgeEngineGuard:
    """知识引擎安全守卫 — 超时降级"""

    def __init__(
        self, retrieve_timeout_ms: int = 50, finalize_timeout_ms: int = 100,
    ):
        self._retrieve_timeout = retrieve_timeout_ms / 1000.0
        self._finalize_timeout = finalize_timeout_ms / 1000.0

    async def safe_retrieve(
        self, retriever, user_id: int, instance_id: str,
        query_tags: list, token_budget: int = 0,
    ) -> str:
        """
        带超时的安全检索。
        超时/异常 → 返回空字符串（不影响主流程）。
        """
        try:
            result = await asyncio.wait_for(
                retriever.retrieve_for_prompt(
                    user_id=user_id,
                    instance_id=instance_id,
                    query_tags=query_tags,
                    token_budget=token_budget,
                ),
                timeout=self._retrieve_timeout,
            )
            return result or ""
        except asyncio.TimeoutError:
            logger.warning(
                f"[KnowledgeGuard] retrieve timeout "
                f"({self._retrieve_timeout * 1000:.0f}ms), returning empty"
            )
            return ""
        except Exception as e:
            logger.warning(f"[KnowledgeGuard] retrieve failed: {e}")
            return ""

    async def safe_finalize(
        self, tracker, store, user_id: int = 0,
        instance_id: str = "", session_id: str = "",
        loop_normal_exit: bool = True,
    ):
        """
        带超时的安全 finalize。
        超时 → 投入后台（不阻塞主流程）。
        """
        try:
            await asyncio.wait_for(
                tracker.finalize(store, user_id, instance_id, session_id,
                                 loop_normal_exit=loop_normal_exit),
                timeout=self._finalize_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[KnowledgeGuard] finalize timeout "
                f"({self._finalize_timeout * 1000:.0f}ms), scheduling background"
            )
            asyncio.create_task(
                self._bg_finalize(tracker, store, user_id, instance_id, session_id,
                                  loop_normal_exit=loop_normal_exit)
            )
        except Exception as e:
            logger.warning(f"[KnowledgeGuard] finalize failed: {e}")

    @staticmethod
    async def _bg_finalize(tracker, store, user_id, instance_id, session_id,
                           loop_normal_exit: bool = True):
        """后台 finalize（超时降级后续）"""
        try:
            await tracker.finalize(store, user_id, instance_id, session_id,
                                   loop_normal_exit=loop_normal_exit)
            logger.info("[KnowledgeGuard] Background finalize completed")
        except Exception as e:
            logger.warning(f"[KnowledgeGuard] Background finalize failed: {e}")
