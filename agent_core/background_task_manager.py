"""
后台任务管理器

用于追踪和管理所有后台异步任务，确保：
1. 任务异常被正确捕获和记录
2. 应用 shutdown 时所有任务被正确取消
3. 防止 fire-and-forget 任务导致的资源泄漏
"""
import asyncio
from typing import Set, Optional, Callable, Any, Coroutine
from loguru import logger


class BackgroundTaskManager:
    """
    后台任务管理器

    功能：
    - 追踪所有创建的后台任务
    - 自动捕获任务异常
    - 应用关闭时清理所有任务
    """

    def __init__(self, name: str = "BackgroundTaskManager"):
        self.name = name
        self._tasks: Set[asyncio.Task] = set()
        self._shutdown = False

    def create_task(
        self,
        coro: Coroutine[Any, Any, Any],
        task_name: Optional[str] = None,
        on_error: Optional[Callable[[Exception], None]] = None
    ) -> asyncio.Task:
        """
        创建并追踪后台任务

        Args:
            coro: 协程对象
            task_name: 任务名称（用于日志）
            on_error: 自定义错误处理器

        Returns:
            asyncio.Task 任务对象
        """
        if self._shutdown:
            # shutdown 期间不再接受新任务，直接关闭协程
            coro.close()
            raise RuntimeError(f"[{self.name}] Cannot create task during shutdown")

        async def wrapped_coro():
            try:
                return await coro
            except asyncio.CancelledError:
                logger.debug(f"[{self.name}] Task '{task_name}' cancelled")
                raise
            except Exception as e:
                logger.error(
                    f"[{self.name}] Task '{task_name}' failed: {e}",
                    exc_info=True
                )
                if on_error:
                    on_error(e)
                raise

        task = asyncio.create_task(wrapped_coro(), name=task_name)

        # 同步添加到追踪集合（无需 async lock）
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

        return task

    def _on_task_done(self, task: asyncio.Task) -> None:
        """任务完成时的回调（同步移除）"""
        self._tasks.discard(task)

        try:
            exception = task.exception()
            if exception and not isinstance(exception, asyncio.CancelledError):
                logger.warning(
                    f"[{self.name}] Task '{task.get_name()}' failed with exception"
                )
        except asyncio.CancelledError:
            pass
        except asyncio.InvalidStateError:
            pass

    async def shutdown(self, timeout: float = 30.0) -> None:
        """
        关闭管理器，取消所有后台任务

        Args:
            timeout: 等待所有任务完成的超时时间（秒）
        """
        logger.info(f"[{self.name}] Shutting down {len(self._tasks)} background tasks...")

        self._shutdown = True
        tasks = list(self._tasks)

        # 取消所有任务
        for task in tasks:
            if not task.done():
                task.cancel()

        # 等待所有任务完成
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=timeout
                )
                logger.info(f"[{self.name}] All {len(tasks)} tasks stopped")
            except asyncio.TimeoutError:
                logger.warning(
                    f"[{self.name}] Timeout waiting for tasks to stop, "
                    f"{len([t for t in tasks if not t.done()])} tasks still running"
                )
            except Exception as e:
                logger.error(f"[{self.name}] Error during shutdown: {e}")

        self._tasks.clear()

    def get_task_count(self) -> int:
        """获取当前活跃任务数"""
        return len(self._tasks)

    def get_task_names(self) -> list:
        """获取所有活跃任务的名称"""
        return [t.get_name() or "unnamed" for t in self._tasks]


# 全局单例
_global_manager: Optional[BackgroundTaskManager] = None


def get_global_task_manager() -> BackgroundTaskManager:
    """获取全局后台任务管理器"""
    global _global_manager
    if _global_manager is None:
        _global_manager = BackgroundTaskManager("Global")
    return _global_manager


def create_background_task(
    coro: Coroutine[Any, Any, Any],
    task_name: Optional[str] = None,
    on_error: Optional[Callable[[Exception], None]] = None
) -> asyncio.Task:
    """
    创建后台任务（便捷函数）

    Args:
        coro: 协程对象
        task_name: 任务名称
        on_error: 错误处理器

    Returns:
        asyncio.Task 任务对象
    """
    return get_global_task_manager().create_task(coro, task_name, on_error)
