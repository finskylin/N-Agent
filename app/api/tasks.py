"""
后台任务管理 REST API

Task endpoints:
  POST   /api/v1/tasks/              注册任务
  GET    /api/v1/tasks/              列出任务
  GET    /api/v1/tasks/{task_id}     查询单任务（含日志尾部）
  PATCH  /api/v1/tasks/{task_id}     更新状态
  DELETE /api/v1/tasks/{task_id}     取消/删除任务

TaskGroup endpoints:
  POST   /api/v1/task_groups/                创建任务组
  GET    /api/v1/task_groups/{group_id}      查询任务组
  POST   /api/v1/task_groups/{group_id}/add  添加任务到组
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.services import task_service
from app.services import task_group_service

router = APIRouter(tags=["tasks"])


# ── Task 请求模型 ──────────────────────────────────────────────────────────────

class TaskCreateRequest(BaseModel):
    name: str
    description: str = ""
    exec_mode: str = "local"
    command: str = ""
    pid: Optional[int] = None
    container_id: str = ""
    container_name: str = ""
    log_path: str = ""
    created_by: str = ""
    session_id: str = ""
    group_id: str = ""


class TaskUpdateRequest(BaseModel):
    status: str
    result: Optional[str] = None
    error: Optional[str] = None


# ── Task 端点 ──────────────────────────────────────────────────────────────────

@router.post("/api/v1/tasks/")
async def create_task(req: TaskCreateRequest, db: AsyncSession = Depends(get_db)):
    return await task_service.create_task(
        db,
        name=req.name,
        exec_mode=req.exec_mode,
        command=req.command,
        pid=req.pid,
        container_id=req.container_id,
        container_name=req.container_name,
        log_path=req.log_path,
        description=req.description,
        created_by=req.created_by,
        session_id=req.session_id,
        group_id=req.group_id,
    )


@router.get("/api/v1/tasks/")
async def list_tasks(
    status: Optional[str] = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    return await task_service.list_tasks(db, status=status, limit=limit)


@router.get("/api/v1/tasks/{task_id}")
async def get_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await task_service.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.patch("/api/v1/tasks/{task_id}")
async def update_task(
    task_id: str,
    req: TaskUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    task = await task_service.update_task_status(
        db, task_id, status=req.status, result=req.result, error=req.error
    )
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.delete("/api/v1/tasks/{task_id}")
async def cancel_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await task_service.cancel_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


# ── TaskGroup 请求模型 ─────────────────────────────────────────────────────────

class TaskGroupCreateRequest(BaseModel):
    session_id: str
    user_id: str = "1"
    channel: str = "web"
    callback_prompt: Optional[str] = None


class TaskGroupAddRequest(BaseModel):
    task_id: str


# ── TaskGroup 端点 ─────────────────────────────────────────────────────────────

@router.post("/api/v1/task_groups/")
async def create_task_group(
    req: TaskGroupCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    return await task_group_service.create_group(
        db,
        session_id=req.session_id,
        user_id=req.user_id,
        channel=req.channel,
        callback_prompt=req.callback_prompt,
    )


@router.get("/api/v1/task_groups/{group_id}")
async def get_task_group(group_id: str, db: AsyncSession = Depends(get_db)):
    group = await task_group_service.get_group(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="TaskGroup not found")
    return group


@router.post("/api/v1/task_groups/{group_id}/add")
async def add_task_to_group(
    group_id: str,
    req: TaskGroupAddRequest,
    db: AsyncSession = Depends(get_db),
):
    ok = await task_group_service.add_task_to_group(db, group_id, req.task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="TaskGroup not found")
    return {"ok": True, "group_id": group_id, "task_id": req.task_id}
