"""
CapabilityGapCounter -- 能力盲区计数器

复用 SessionContextDB 的 capability_gaps 表，跨 session 累积工具失败计数。
达到阈值后触发 EvolutionTaskManager 创建进化任务。

参考: AgentEvolver 自问自导 + DGM 自我改进
"""
from __future__ import annotations

import time
from typing import Optional

from loguru import logger

# 能力盲区关键词（error content 包含这些词才计数）
GAP_KEYWORDS = [
    # 英文：能力缺失类
    "not found", "missing", "unsupported", "no data",
    "not implemented", "404", "unavailable",
    # 中文原有
    "不支持", "无法处理", "接口不存在", "功能未实现", "无数据",
    # 中文业务错误（结合 threshold≥2 + cooldown 24h 过滤临时性错误）
    "数据获取失败", "接口超时", "获取失败", "查询失败", "请求失败",
    "服务不可用", "暂不支持", "暂未支持", "没有数据", "无法获取",
    "数据为空", "返回为空", "接口异常", "调用失败", "执行失败",
    "数据错误", "解析失败", "网络错误", "连接超时", "获取数据失败",
]


def _matches_gap_keyword(error_text: str) -> bool:
    """检查错误信息是否匹配能力盲区关键词"""
    lower = error_text.lower()
    return any(kw in lower for kw in GAP_KEYWORDS)


class CapabilityGapCounter:
    """能力盲区计数器 -- 复用 SessionContextDB"""

    def __init__(self, context_db, config: dict = None):
        """
        Args:
            context_db: SessionContextDB 实例
            config: 含 capability_gap_* 配置的 dict
        """
        self._db = context_db
        cfg = config or {}
        self._enabled = cfg.get("capability_gap_detection_enabled", True)
        self._threshold = cfg.get("capability_gap_trigger_threshold", 3)
        self._cooldown_hours = cfg.get("capability_gap_cooldown_hours", 24)
        # session 内去重
        self._session_triggered: set = set()

    async def increment(
        self, tool_name: str, error_summary: str, session_id: str = "",
    ) -> int:
        """
        记录一次能力盲区失败，返回当前累积次数。
        仅当 error_summary 匹配 GAP_KEYWORDS 时才计数。
        """
        if not self._enabled or not self._db:
            return 0

        if not _matches_gap_keyword(error_summary):
            return 0

        now = time.time()
        try:
            await self._db._ensure_init()
            async with self._db._connect() as db:
                await self._db._setup_conn(db)
                # upsert: 存在则 count+1，不存在则 insert
                await db.execute(
                    "INSERT INTO capability_gaps "
                    "(tool_name, error_summary, session_id, count, last_triggered, created_at, updated_at) "
                    "VALUES (?, ?, ?, 1, 0, ?, ?) "
                    "ON CONFLICT(tool_name) DO UPDATE SET "
                    "count = capability_gaps.count + 1, "
                    "error_summary = excluded.error_summary, "
                    "session_id = excluded.session_id, "
                    "updated_at = excluded.updated_at",
                    (tool_name, error_summary[:500], session_id, now, now),
                )
                await db.commit()

                cursor = await db.execute(
                    "SELECT count FROM capability_gaps WHERE tool_name = ?",
                    (tool_name,),
                )
                row = await cursor.fetchone()
                count = row[0] if row else 0

            logger.debug(
                f"[CapabilityGap] {tool_name}: count={count} "
                f"(threshold={self._threshold})"
            )
            return count
        except Exception as e:
            logger.debug(f"[CapabilityGap] increment error: {e}")
            return 0

    async def should_trigger(self, tool_name: str, session_id: str = "") -> bool:
        """
        是否达到触发阈值。条件:
        1. count >= threshold
        2. 24h 内同一 tool_name 未触发过
        3. 同一 session 内未触发过
        """
        if not self._enabled or not self._db:
            return False

        # session 内去重
        session_key = f"{session_id}:{tool_name}"
        if session_key in self._session_triggered:
            return False

        now = time.time()
        cooldown_seconds = self._cooldown_hours * 3600

        try:
            await self._db._ensure_init()
            async with self._db._connect() as db:
                await self._db._setup_conn(db)
                cursor = await db.execute(
                    "SELECT count, last_triggered FROM capability_gaps "
                    "WHERE tool_name = ?",
                    (tool_name,),
                )
                row = await cursor.fetchone()
                if not row:
                    return False

                count, last_triggered = row[0], row[1]
                if count < self._threshold:
                    return False
                if now - last_triggered < cooldown_seconds:
                    return False

            return True
        except Exception as e:
            logger.debug(f"[CapabilityGap] should_trigger error: {e}")
            return False

    async def mark_triggered(self, tool_name: str, session_id: str = "") -> None:
        """标记已触发，更新 last_triggered 并重置 count"""
        if not self._db:
            return

        session_key = f"{session_id}:{tool_name}"
        self._session_triggered.add(session_key)

        now = time.time()
        try:
            await self._db._ensure_init()
            async with self._db._connect() as db:
                await self._db._setup_conn(db)
                await db.execute(
                    "UPDATE capability_gaps SET last_triggered = ?, count = 0, "
                    "updated_at = ? WHERE tool_name = ?",
                    (now, now, tool_name),
                )
                await db.commit()
        except Exception as e:
            logger.debug(f"[CapabilityGap] mark_triggered error: {e}")

    async def reset(self, tool_name: str) -> None:
        """Skill 进化完成后重置计数"""
        if not self._db:
            return
        try:
            await self._db._ensure_init()
            async with self._db._connect() as db:
                await self._db._setup_conn(db)
                await db.execute(
                    "DELETE FROM capability_gaps WHERE tool_name = ?",
                    (tool_name,),
                )
                await db.commit()
        except Exception as e:
            logger.debug(f"[CapabilityGap] reset error: {e}")

    def clear_session_state(self) -> None:
        """清除 session 内去重状态（session 结束时调用）"""
        self._session_triggered.clear()
