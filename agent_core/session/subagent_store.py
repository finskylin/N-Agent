"""
SubAgentStore — 子代理执行记录的 SQLite 存储层

记录每次 spawn_agent 的元数据：task、result、status、tools_used、关联 session。
供 query_subagent 内置工具查询。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import aiosqlite
from loguru import logger


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS subagent_records (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_agent_id      TEXT NOT NULL UNIQUE,
    parent_agent_id   TEXT NOT NULL,
    parent_session_id TEXT NOT NULL,
    sub_session_id    TEXT NOT NULL,
    user_id           INTEGER NOT NULL DEFAULT 0,
    task              TEXT NOT NULL,
    result            TEXT,
    status            TEXT NOT NULL DEFAULT 'running',
    tools_used        TEXT,
    depth             INTEGER DEFAULT 1,
    created_at        REAL NOT NULL,
    finished_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_subagent_parent_session
    ON subagent_records(parent_session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_subagent_id
    ON subagent_records(sub_agent_id);
"""


class SubAgentStore:
    """
    子代理执行记录存储

    复用现有 SessionContextDB 的同一个 SQLite 文件（WAL 模式，并发安全）。
    不新建单独的 DB 文件，避免文件数量膨胀。
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._initialized = False

    async def _ensure_init(self) -> None:
        if self._initialized:
            return
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.executescript(_SCHEMA_SQL)
            await db.commit()
        self._initialized = True

    async def insert(
        self,
        sub_agent_id: str,
        parent_agent_id: str,
        parent_session_id: str,
        sub_session_id: str,
        user_id: int,
        task: str,
        depth: int = 1,
    ) -> None:
        """创建一条 running 状态的记录"""
        try:
            await self._ensure_init()
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA busy_timeout=5000")
                await db.execute(
                    """
                    INSERT OR IGNORE INTO subagent_records
                        (sub_agent_id, parent_agent_id, parent_session_id,
                         sub_session_id, user_id, task, status, depth, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)
                    """,
                    (sub_agent_id, parent_agent_id, parent_session_id,
                     sub_session_id, user_id, task, depth, time.time()),
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"[SubAgentStore] insert failed: {e}")

    async def complete(
        self,
        sub_agent_id: str,
        result: str,
        tools_used: List[str],
        status: str = "completed",
    ) -> None:
        """更新记录为 completed/failed"""
        try:
            await self._ensure_init()
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA busy_timeout=5000")
                await db.execute(
                    """
                    UPDATE subagent_records
                    SET result=?, tools_used=?, status=?, finished_at=?
                    WHERE sub_agent_id=?
                    """,
                    (result, json.dumps(tools_used, ensure_ascii=False),
                     status, time.time(), sub_agent_id),
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"[SubAgentStore] complete failed: {e}")

    async def get_by_id(self, sub_agent_id: str) -> Optional[Dict[str, Any]]:
        """按 sub_agent_id 查询单条记录"""
        try:
            await self._ensure_init()
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM subagent_records WHERE sub_agent_id=?",
                    (sub_agent_id,),
                ) as cur:
                    row = await cur.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            logger.warning(f"[SubAgentStore] get_by_id failed: {e}")
            return None

    async def list_by_parent_session(
        self,
        parent_session_id: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """查询某父 session 下的所有子代理记录"""
        try:
            await self._ensure_init()
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """
                    SELECT * FROM subagent_records
                    WHERE parent_session_id=?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (parent_session_id, limit),
                ) as cur:
                    rows = await cur.fetchall()
                    return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"[SubAgentStore] list_by_parent_session failed: {e}")
            return []

    async def list_interrupted_by_session(
        self,
        parent_session_id: str,
    ) -> List[Dict[str, Any]]:
        """查询某父 session 下 status='interrupted' 的子代理记录"""
        try:
            await self._ensure_init()
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """
                    SELECT * FROM subagent_records
                    WHERE parent_session_id=? AND status='interrupted'
                    ORDER BY created_at ASC
                    """,
                    (parent_session_id,),
                ) as cur:
                    rows = await cur.fetchall()
                    return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"[SubAgentStore] list_interrupted_by_session failed: {e}")
            return []

    async def list_interrupted(self, min_age_seconds: int = 30) -> List[Dict[str, Any]]:
        """查询 status='running' 且创建超过 min_age_seconds 秒的记录（视为被中断）"""
        try:
            await self._ensure_init()
            cutoff = time.time() - min_age_seconds
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """
                    SELECT * FROM subagent_records
                    WHERE status='running' AND created_at < ?
                    ORDER BY created_at ASC
                    """,
                    (cutoff,),
                ) as cur:
                    rows = await cur.fetchall()
                    return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"[SubAgentStore] list_interrupted failed: {e}")
            return []

    async def claim_interrupted(self, min_age_seconds: int = 0) -> List[Dict[str, Any]]:
        """
        原子性 claim：在同一事务内将 status='running' 的记录改为 'recovering'，再返回这些记录。
        多进程安全：只有第一个执行的 worker 能 claim 到记录，其他 worker 查到空列表。
        """
        try:
            await self._ensure_init()
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA busy_timeout=5000")
                # 服务重启时，所有 status='running' 的记录都视为被中断（无需时间过滤）
                async with db.execute(
                    """
                    SELECT * FROM subagent_records
                    WHERE status='running'
                    ORDER BY created_at ASC
                    """,
                ) as cur:
                    rows = await cur.fetchall()
                    columns = [d[0] for d in cur.description]

                if not rows:
                    return []

                # 原子性标记为 recovering，防止其他 worker 重复处理
                ids = [r[columns.index("sub_agent_id")] for r in rows]
                placeholders = ",".join("?" * len(ids))
                await db.execute(
                    f"UPDATE subagent_records SET status='recovering' WHERE sub_agent_id IN ({placeholders}) AND status='running'",
                    ids,
                )
                await db.commit()

                # 再查一次，只返回真正被本 worker claim 到（status 已改为 recovering）的记录
                async with db.execute(
                    f"SELECT * FROM subagent_records WHERE sub_agent_id IN ({placeholders}) AND status='recovering'",
                    ids,
                ) as cur2:
                    claimed = await cur2.fetchall()
                    col2 = [d[0] for d in cur2.description]
                    return [dict(zip(col2, r)) for r in claimed]

        except Exception as e:
            logger.warning(f"[SubAgentStore] claim_interrupted failed: {e}")
            return []

    async def count_active_background(self, parent_session_id: str) -> int:
        """查询某父 session 下当前同时运行中（running/recovering）的后台任务数量"""
        try:
            await self._ensure_init()
            async with aiosqlite.connect(self._db_path) as db:
                async with db.execute(
                    """
                    SELECT COUNT(*) FROM subagent_records
                    WHERE parent_session_id=? AND status IN ('running','recovering')
                    """,
                    (parent_session_id,),
                ) as cur:
                    row = await cur.fetchone()
                    return row[0] if row else 0
        except Exception as e:
            logger.warning(f"[SubAgentStore] count_active_background failed: {e}")
            return 0

    async def is_session_running(self, sub_session_id: str) -> bool:
        """查询指定 sub_session_id 是否已有 running/recovering 状态的记录（同任务去重）"""
        try:
            await self._ensure_init()
            async with aiosqlite.connect(self._db_path) as db:
                async with db.execute(
                    """
                    SELECT COUNT(*) FROM subagent_records
                    WHERE sub_session_id=? AND status IN ('running','recovering')
                    """,
                    (sub_session_id,),
                ) as cur:
                    row = await cur.fetchone()
                    return (row[0] if row else 0) > 0
        except Exception as e:
            logger.warning(f"[SubAgentStore] is_session_running failed: {e}")
            return False

    async def cancel_by_ids(self, sub_agent_ids: List[str]) -> None:
        """批量将指定记录标为 cancelled（去重淘汰时使用）"""
        if not sub_agent_ids:
            return
        try:
            await self._ensure_init()
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA busy_timeout=5000")
                placeholders = ",".join("?" * len(sub_agent_ids))
                await db.execute(
                    f"""
                    UPDATE subagent_records
                    SET status='cancelled', finished_at=?
                    WHERE sub_agent_id IN ({placeholders})
                    """,
                    [time.time()] + list(sub_agent_ids),
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"[SubAgentStore] cancel_by_ids failed: {e}")

    async def list_active_background(self, parent_session_id: str) -> List[Dict[str, Any]]:
        """查询某父 session 下 status IN ('running','recovering') 的后台任务列表"""
        try:
            await self._ensure_init()
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """
                    SELECT sub_agent_id, task, status, created_at
                    FROM subagent_records
                    WHERE parent_session_id=? AND status IN ('running','recovering')
                    ORDER BY created_at ASC
                    """,
                    (parent_session_id,),
                ) as cur:
                    rows = await cur.fetchall()
                    return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"[SubAgentStore] list_active_background failed: {e}")
            return []

    async def mark_interrupted(self, sub_agent_ids: List[str]) -> None:
        """批量将指定记录标为 interrupted"""
        if not sub_agent_ids:
            return
        try:
            await self._ensure_init()
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA busy_timeout=5000")
                placeholders = ",".join("?" * len(sub_agent_ids))
                await db.execute(
                    f"""
                    UPDATE subagent_records
                    SET status='interrupted', finished_at=?
                    WHERE sub_agent_id IN ({placeholders})
                    """,
                    [time.time()] + list(sub_agent_ids),
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"[SubAgentStore] mark_interrupted failed: {e}")
