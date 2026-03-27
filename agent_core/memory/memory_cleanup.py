"""
Memory Cleanup Scheduler — 定期清理过期记忆

职责:
- 定时清理过期 MTM 页面（替代 Redis TTL）
- 后台 asyncio task 运行

生命周期:
- start() 创建后台清理任务
- stop() 取消任务
"""
import asyncio
from typing import Optional, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from agent_core.memory.mid_term_memory import MidTermMemory


class MemoryCleanupScheduler:
    """
    记忆清理调度器

    Args:
        mtm: MidTermMemory 实例
        interval_hours: 清理间隔（小时）
        max_age_days: MTM 页面最大保留天数
    """

    def __init__(
        self,
        mtm: "MidTermMemory",
        interval_hours: int = 24,
        max_age_days: int = 90,
    ):
        self._mtm = mtm
        self._interval_seconds = interval_hours * 3600
        self._max_age_days = max_age_days
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self):
        """启动清理调度

        可在同步或异步上下文中调用：
        - 有 running event loop 时立即创建 task
        - 无 running loop（如同步初始化阶段）时标记 pending，
          等到首次 ensure_started() 调用（在 async 上下文中）再真正启动
        """
        if self._task and not self._task.done():
            logger.debug("[MemoryCleanup] Already running")
            return

        self._running = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            self._task = loop.create_task(self._cleanup_loop())
            logger.info(
                f"[MemoryCleanup] Started "
                f"(interval={self._interval_seconds}s, "
                f"max_age={self._max_age_days}d)"
            )
        else:
            # 无 running loop，标记 pending，等 ensure_started() 在 async 上下文里调用
            logger.info(
                f"[MemoryCleanup] Deferred start "
                f"(interval={self._interval_seconds}s, "
                f"max_age={self._max_age_days}d) — will activate on first async call"
            )

    def ensure_started(self):
        """在 async 上下文中确保后台清理 task 已启动（幂等）"""
        if self._task and not self._task.done():
            return
        if not self._running:
            return
        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._cleanup_loop())
            logger.info("[MemoryCleanup] Deferred task now started")
        except RuntimeError:
            pass

    def stop(self):
        """停止清理调度"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("[MemoryCleanup] Stopped")

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    async def _cleanup_loop(self):
        """清理循环"""
        try:
            while self._running:
                await asyncio.sleep(self._interval_seconds)
                if not self._running:
                    break
                try:
                    deleted = await self._mtm.cleanup_expired(
                        self._max_age_days,
                    )
                    if deleted > 0:
                        logger.info(
                            f"[MemoryCleanup] Cleaned {deleted} expired pages"
                        )
                except Exception as e:
                    logger.warning(
                        f"[MemoryCleanup] Cleanup failed: {e}"
                    )
        except asyncio.CancelledError:
            logger.debug("[MemoryCleanup] Cleanup loop cancelled")
