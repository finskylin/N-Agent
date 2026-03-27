"""
TaskService — 后台任务管理服务

职责：
- 注册 docker_operator 启动的后台进程/容器任务
- 查询任务状态、读取日志尾部
- 更新任务状态（进行中/完成/失败/取消）

存储：app 主库 agent.db 的 tasks 表
"""
import uuid
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_db import Task


async def create_task(
    db: AsyncSession,
    name: str,
    exec_mode: str = "local",
    command: str = "",
    pid: Optional[int] = None,
    container_id: str = "",
    container_name: str = "",
    log_path: str = "",
    description: str = "",
    created_by: str = "",
    session_id: str = "",
    group_id: str = "",
) -> Dict[str, Any]:
    """注册后台任务，返回 task dict"""
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    now = datetime.now()
    task = Task(
        task_id=task_id,
        name=name,
        description=description,
        exec_mode=exec_mode,
        command=command,
        pid=pid,
        container_id=container_id or None,
        container_name=container_name or None,
        log_path=log_path or None,
        status="running",
        created_by=created_by or None,
        session_id=session_id or None,
        group_id=group_id or None,
        created_at=now,
        started_at=now,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    logger.info(f"[TaskService] Created task {task_id}: {name} ({exec_mode})")
    return task.to_dict()


async def get_task(db: AsyncSession, task_id: str) -> Optional[Dict[str, Any]]:
    """查询单个任务"""
    result = await db.execute(select(Task).where(Task.task_id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        return None
    data = task.to_dict()
    # 附加日志尾部（最近 50 行）
    data["log_tail"] = _read_log_tail(task.log_path, lines=50)
    # 如果状态仍 running，尝试检测进程是否还存活
    if task.status == "running":
        data["process_alive"] = _check_process_alive(task.pid, task.container_id)
    return data


async def list_tasks(
    db: AsyncSession,
    status: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """列出任务"""
    stmt = select(Task).order_by(Task.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Task.status == status)
    result = await db.execute(stmt)
    return [t.to_dict() for t in result.scalars().all()]


async def update_task_status(
    db: AsyncSession,
    task_id: str,
    status: str,
    result: Optional[str] = None,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """更新任务状态"""
    res = await db.execute(select(Task).where(Task.task_id == task_id))
    task = res.scalar_one_or_none()
    if not task:
        return None
    task.status = status
    if result is not None:
        task.result = result
    if error is not None:
        task.error = error
    if status in ("completed", "failed", "cancelled"):
        task.finished_at = datetime.now()
    await db.commit()
    await db.refresh(task)
    logger.info(f"[TaskService] Updated task {task_id} -> {status}")
    return task.to_dict()


async def cancel_task(db: AsyncSession, task_id: str) -> Optional[Dict[str, Any]]:
    """取消任务（发送 SIGTERM）"""
    res = await db.execute(select(Task).where(Task.task_id == task_id))
    task = res.scalar_one_or_none()
    if not task:
        return None

    # 尝试终止进程
    if task.pid:
        try:
            import os
            os.kill(task.pid, signal.SIGTERM)
            logger.info(f"[TaskService] Sent SIGTERM to pid={task.pid}")
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.warning(f"[TaskService] Failed to kill pid={task.pid}: {e}")

    # 尝试停止 Docker 容器
    if task.container_id or task.container_name:
        target = task.container_id or task.container_name
        try:
            subprocess.run(["docker", "stop", target], timeout=10, capture_output=True)
            logger.info(f"[TaskService] Stopped container {target}")
        except Exception as e:
            logger.warning(f"[TaskService] Failed to stop container {target}: {e}")

    return await update_task_status(db, task_id, "cancelled")


def _read_log_tail(log_path: Optional[str], lines: int = 50) -> str:
    """读取日志文件末尾 N 行"""
    if not log_path:
        return ""
    try:
        p = Path(log_path)
        if not p.exists():
            return ""
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return "".join(all_lines[-lines:])
    except Exception:
        return ""


def _check_process_alive(pid: Optional[int], container_id: Optional[str]) -> bool:
    """检查进程或容器是否仍在运行"""
    if pid:
        try:
            import os
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
    if container_id:
        try:
            r = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", container_id],
                capture_output=True, text=True, timeout=5,
            )
            return r.stdout.strip() == "true"
        except Exception:
            return False
    return False
