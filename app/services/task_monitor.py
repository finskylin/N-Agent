"""
TaskMonitor — 后台任务生命周期监控

职责：
1. 每 10 秒扫描所有 status=running 的 Task
2. 检查 <log_path>.done 文件是否存在（toolbox 任务完成标记）
3. 若完成 → 更新 Task 状态（completed/failed）
4. 检查所属 TaskGroup，若组内所有任务均已结束 → 触发 Agent 回调

完成检测协议（toolbox 侧约定）：
  任务脚本末尾由 docker_operator 包装器写入：
    echo $? > <log_path>.done
  TaskMonitor 读取该文件内容作为退出码（0=completed, 非0=failed）

Agent 回调：
  复用原 session_id，POST /api/v1/internal/task_complete
  让 Agent 生成报告并通知用户
"""
import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from app.db.database import AsyncSessionLocal
from app.db.models_db import Task, TaskGroup
from app.services.task_group_service import (
    list_running_groups,
    get_group_tasks,
    finalize_group,
)

# 轮询间隔（秒）
_POLL_INTERVAL = int(os.getenv("TASK_MONITOR_INTERVAL", "10"))
# 任务最长等待时间（秒），超时强制标记 failed
_TASK_MAX_AGE = int(os.getenv("TASK_MAX_AGE_SECONDS", str(24 * 3600)))
# Agent 内部回调地址
_AGENT_BASE_URL = os.getenv("AGENT_SERVICE_URL", "http://localhost:8000")

_DEFAULT_CALLBACK_PROMPT = (
    "后台任务已全部完成，请根据各任务的执行结果生成完整的分析报告，"
    "并通过 send_message 将报告摘要发送给用户。"
    "如有失败的任务，请在报告中说明原因并给出建议。"
)


def _check_done_file(log_path: Optional[str]) -> Optional[int]:
    """
    检查 <log_path>.done 文件。
    返回退出码（int）表示完成，None 表示尚未完成。
    """
    if not log_path:
        return None
    done_path = Path(log_path + ".done")
    if not done_path.exists():
        return None
    try:
        content = done_path.read_text().strip()
        return int(content) if content.isdigit() or (content.startswith("-") and content[1:].isdigit()) else 0
    except Exception:
        return 0  # 文件存在但读取异常，视为完成（退出码 0）


def _is_task_timed_out(task: Task) -> bool:
    """检查任务是否超过最大等待时间"""
    if not task.created_at:
        return False
    age = (datetime.now() - task.created_at).total_seconds()
    return age > _TASK_MAX_AGE


async def _scan_running_tasks(db) -> None:
    """扫描所有 running 任务，更新已完成的任务状态"""
    from sqlalchemy import select
    res = await db.execute(
        select(Task).where(Task.status == "running")
    )
    tasks = list(res.scalars().all())

    for task in tasks:
        exit_code = _check_done_file(task.log_path)

        if exit_code is not None:
            task.status = "completed" if exit_code == 0 else "failed"
            task.exit_code = exit_code
            task.finished_at = datetime.now()
            if exit_code != 0:
                task.error = f"任务以退出码 {exit_code} 结束"
            logger.info(
                f"[TaskMonitor] Task {task.task_id} finished: "
                f"status={task.status} exit_code={exit_code}"
            )
        elif _is_task_timed_out(task):
            task.status = "failed"
            task.error = f"任务超时（>{_TASK_MAX_AGE}s），强制标记失败"
            task.finished_at = datetime.now()
            logger.warning(f"[TaskMonitor] Task {task.task_id} timed out, marked failed")

    await db.commit()


async def _check_groups(db) -> None:
    """检查所有 running 任务组，找出已全部完成的组并触发回调"""
    groups = await list_running_groups(db)

    for group in groups:
        tasks = await get_group_tasks(db, group.group_id)
        if not tasks:
            continue

        total = len(tasks)
        completed = sum(1 for t in tasks if t.status == "completed")
        failed = sum(1 for t in tasks if t.status in ("failed", "cancelled"))
        done = completed + failed

        if done < total:
            continue  # 还有任务未结束

        # 所有任务已结束
        await finalize_group(db, group, completed=completed, failed=failed)

        # 构建任务结果摘要
        task_summaries = []
        for t in tasks:
            log_tail = ""
            if t.log_path:
                try:
                    p = Path(t.log_path)
                    if p.exists():
                        lines = p.read_text(errors="replace").splitlines()
                        log_tail = "\n".join(lines[-20:])
                except Exception:
                    pass
            task_summaries.append(
                f"- 任务: {t.name}\n"
                f"  状态: {t.status}  退出码: {t.exit_code}\n"
                f"  日志末尾:\n{log_tail or '（无日志）'}"
            )

        callback_prompt = (group.callback_prompt or _DEFAULT_CALLBACK_PROMPT)
        message = (
            f"{callback_prompt}\n\n"
            f"== 任务组完成报告 ==\n"
            f"任务组 ID: {group.group_id}\n"
            f"完成: {completed}/{total}  失败: {failed}/{total}\n\n"
            + "\n\n".join(task_summaries)
        )

        # 异步触发 Agent 回调（不等待结果）
        asyncio.create_task(
            _fire_agent_callback(
                session_id=group.session_id,
                user_id=group.user_id or "1",
                channel=group.channel or "web",
                message=message,
                group_id=group.group_id,
            )
        )


async def _fire_agent_callback(
    session_id: str,
    user_id: str,
    channel: str,
    message: str,
    group_id: str,
) -> None:
    """向 Agent 内部聊天接口发起回调，触发报告生成和用户通知"""
    url = f"{_AGENT_BASE_URL}/api/v1/chat/v4/stream"
    payload = {
        "session_id": int(session_id) if session_id.isdigit() else session_id,
        "user_id": int(user_id) if str(user_id).isdigit() else user_id,
        "channel": channel,
        "message": message,
        "_source": "task_monitor",   # 标记来源，便于日志追踪
        "_group_id": group_id,
    }
    try:
        # 使用流式接口，读完整个响应（不中断）
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    logger.warning(
                        f"[TaskMonitor] Agent callback failed: "
                        f"status={resp.status_code} group={group_id} body={body[:200]}"
                    )
                    return
                # 消费完 SSE 流（Agent 执行完毕）
                async for _ in resp.aiter_lines():
                    pass
        logger.info(f"[TaskMonitor] Agent callback completed for group={group_id}")
    except Exception as e:
        logger.warning(f"[TaskMonitor] Agent callback error group={group_id}: {e}")


async def run_task_monitor() -> None:
    """
    TaskMonitor 主循环（在 main.py 的 lifespan 中作为后台任务启动）。
    每 _POLL_INTERVAL 秒扫描一次。
    """
    logger.info(f"[TaskMonitor] Started (interval={_POLL_INTERVAL}s, max_age={_TASK_MAX_AGE}s)")
    while True:
        try:
            await asyncio.sleep(_POLL_INTERVAL)
            async with AsyncSessionLocal() as db:
                await _scan_running_tasks(db)
                await _check_groups(db)
        except asyncio.CancelledError:
            logger.info("[TaskMonitor] Stopped")
            break
        except Exception as e:
            logger.warning(f"[TaskMonitor] Scan error: {e}")
            await asyncio.sleep(30)  # 出错后等 30s 再重试
