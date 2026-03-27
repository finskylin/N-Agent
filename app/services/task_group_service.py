"""
TaskGroupService — 任务组管理

一次问答可以启动多个后台任务，用 TaskGroup 聚合，
所有任务完成后触发 Agent 回调。
"""
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_db import TaskGroup, Task


async def create_group(
    db: AsyncSession,
    session_id: str,
    user_id: str = "1",
    channel: str = "web",
    callback_prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """创建任务组，返回 group dict（含 group_id）"""
    group_id = f"grp-{uuid.uuid4().hex[:12]}"
    group = TaskGroup(
        group_id=group_id,
        session_id=session_id,
        user_id=user_id,
        channel=channel,
        callback_prompt=callback_prompt,
        status="running",
        total_tasks=0,
        completed_tasks=0,
        failed_tasks=0,
        created_at=datetime.now(),
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)
    logger.info(f"[TaskGroup] Created group {group_id} for session={session_id}")
    return group.to_dict()


async def add_task_to_group(db: AsyncSession, group_id: str, task_id: str) -> bool:
    """将任务关联到任务组，增加 total_tasks 计数"""
    res = await db.execute(select(TaskGroup).where(TaskGroup.group_id == group_id))
    group = res.scalar_one_or_none()
    if not group:
        return False
    group.total_tasks = (group.total_tasks or 0) + 1

    # 同步更新 Task 的 group_id
    task_res = await db.execute(select(Task).where(Task.task_id == task_id))
    task = task_res.scalar_one_or_none()
    if task:
        task.group_id = group_id

    await db.commit()
    return True


async def get_group(db: AsyncSession, group_id: str) -> Optional[Dict[str, Any]]:
    res = await db.execute(select(TaskGroup).where(TaskGroup.group_id == group_id))
    group = res.scalar_one_or_none()
    return group.to_dict() if group else None


async def list_running_groups(db: AsyncSession) -> List[TaskGroup]:
    """返回所有 status=running 的任务组 ORM 对象（供 TaskMonitor 使用）"""
    res = await db.execute(
        select(TaskGroup).where(TaskGroup.status == "running")
    )
    return list(res.scalars().all())


async def get_group_tasks(db: AsyncSession, group_id: str) -> List[Task]:
    """返回任务组内所有 Task ORM 对象"""
    res = await db.execute(
        select(Task).where(Task.group_id == group_id)
    )
    return list(res.scalars().all())


async def finalize_group(
    db: AsyncSession,
    group: TaskGroup,
    completed: int,
    failed: int,
) -> None:
    """更新任务组最终状态"""
    group.completed_tasks = completed
    group.failed_tasks = failed
    group.finished_at = datetime.now()
    if failed == 0:
        group.status = "completed"
    elif completed == 0:
        group.status = "failed"
    else:
        group.status = "partial_failed"
    await db.commit()
    logger.info(
        f"[TaskGroup] Finalized {group.group_id}: "
        f"status={group.status} completed={completed} failed={failed}"
    )
