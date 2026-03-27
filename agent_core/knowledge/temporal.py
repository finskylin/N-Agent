"""
Temporal Knowledge Manager — 时序认知管理器

参考 Graphiti 的双时间戳模型 + 非丢弃式更新。
核心原则: 知识不删除，只失效 (invalidate, never discard)。
"""
import json
import time
from typing import List, Dict, Optional, Any
from uuid import uuid4

from loguru import logger

from .models import KnowledgeUnit, CognitionChange, CognitionSnapshot


class TemporalKnowledgeManager:
    """时序认知管理器 — 知识版本化 + 追溯查询 + 变迁分析"""

    def __init__(self, store, config: dict):
        self._store = store
        self._config = config.get("temporal", {})

    async def update_knowledge(
        self, old_unit_id: str, new_unit: KnowledgeUnit,
        reason: str, user_id: int, instance_id: str,
    ):
        """
        更新知识 — 不覆盖旧知识，创建新版本。
        旧知识标记 valid_until + superseded_by，形成版本链。
        """
        now = time.time()

        # 旧知识失效（不删除）
        await self._store.update_knowledge_field(old_unit_id, {
            "valid_until": now,
            "superseded_by": new_unit.unit_id,
        })

        # 新知识入库
        new_unit.valid_from = now
        new_unit.supersedes = old_unit_id
        new_unit.update_reason = reason
        await self._store.save_knowledge(new_unit, user_id, instance_id)

        # 记录认知变迁日志
        change = CognitionChange(
            change_id=str(uuid4()),
            old_unit_id=old_unit_id,
            new_unit_id=new_unit.unit_id,
            reason=reason,
            change_type="update",
            timestamp=now,
            user_id=user_id,
            instance_id=instance_id,
        )
        await self._store.save_cognition_change(change)
        logger.debug(
            f"[TemporalKM] Knowledge updated: {old_unit_id} → {new_unit.unit_id}, "
            f"reason={reason[:50]}"
        )

    async def point_in_time_query(
        self, user_id: int, instance_id: str,
        entity: str, timestamp: float,
    ) -> List[KnowledgeUnit]:
        """
        追溯查询: 给定时间点，返回当时的有效知识。
        Graphiti 模式: 双时间戳区间查询。
        """
        return await self._store.retrieve(
            user_id, instance_id,
            query_tags=[entity],
            as_of_time=timestamp,
        )

    async def cognition_timeline(
        self, user_id: int, instance_id: str, entity: str,
    ) -> List[Dict]:
        """
        认知变迁: 返回某实体的完整认知演变链。
        沿 superseded_by 链正向遍历，构建时间线。
        """
        await self._store._db._ensure_init()
        async with self._store._db._connect() as db:
            await self._store._db._setup_conn(db)
            cursor = await db.execute(
                """SELECT unit_id, text, tags, utility, confidence,
                          valid_from, valid_until, superseded_by, supersedes,
                          update_reason, ingestion_time
                   FROM knowledge_units
                   WHERE user_id = ? AND instance_id = ?
                     AND tags LIKE ?
                   ORDER BY valid_from ASC""",
                (user_id, instance_id, f"%{entity}%"),
            )
            rows = await cursor.fetchall()

        timeline = []
        for row in rows:
            d = dict(row)
            tags = d.get("tags", "[]")
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except (json.JSONDecodeError, TypeError):
                    tags = []
            timeline.append({
                "unit_id": d["unit_id"],
                "text": d["text"],
                "tags": tags,
                "utility": d["utility"],
                "confidence": d["confidence"],
                "valid_from": d["valid_from"],
                "valid_until": d["valid_until"],
                "superseded_by": d["superseded_by"],
                "supersedes": d["supersedes"],
                "update_reason": d["update_reason"],
                "ingestion_time": d["ingestion_time"],
            })
        return timeline

    async def cognition_backtest(
        self, user_id: int, instance_id: str,
        entity: str, past_time: float, current_time: float,
    ) -> Dict:
        """
        认知回测: 对比两个时间点的认知差异。
        """
        past_knowledge = await self.point_in_time_query(
            user_id, instance_id, entity, past_time,
        )
        current_knowledge = await self.point_in_time_query(
            user_id, instance_id, entity, current_time,
        )
        return {
            "past_time": past_time,
            "current_time": current_time,
            "entity": entity,
            "past_knowledge": [u.text for u in past_knowledge],
            "current_knowledge": [u.text for u in current_knowledge],
            "past_count": len(past_knowledge),
            "current_count": len(current_knowledge),
            "changes": self._diff_knowledge(past_knowledge, current_knowledge),
        }

    async def cognition_trend(
        self, user_id: int, instance_id: str,
        category: str, window_days: Optional[int] = None,
    ) -> Dict:
        """
        认知趋势: 分析某类知识的演变趋势。
        """
        window_days = window_days or self._config.get("trend_window_days", 30)
        threshold = self._config.get("trend_threshold", 0.05)
        cutoff = time.time() - window_days * 86400

        await self._store._db._ensure_init()
        async with self._store._db._connect() as db:
            await self._store._db._setup_conn(db)
            cursor = await db.execute(
                """SELECT utility, confidence, ingestion_time, hit_count, access_count
                   FROM knowledge_units
                   WHERE user_id = ? AND instance_id = ?
                     AND category = ? AND ingestion_time >= ?
                   ORDER BY ingestion_time ASC""",
                (user_id, instance_id, category, cutoff),
            )
            rows = await cursor.fetchall()

        if not rows:
            return {"trend": "no_data", "data_points": 0}

        units = [dict(r) for r in rows]
        utilities = [u["utility"] for u in units]
        half = len(utilities) // 2
        avg_first = sum(utilities[:half]) / max(half, 1)
        avg_second = sum(utilities[half:]) / max(len(utilities) - half, 1)

        if avg_second > avg_first + threshold:
            trend = "improving"
        elif avg_second < avg_first - threshold:
            trend = "declining"
        else:
            trend = "stable"

        return {
            "trend": trend,
            "data_points": len(units),
            "avg_utility_early": round(avg_first, 3),
            "avg_utility_recent": round(avg_second, 3),
        }

    async def create_snapshot(
        self, user_id: int, instance_id: str,
        snapshot_type: str = "daily",
    ) -> CognitionSnapshot:
        """创建认知快照"""
        all_active = await self._store.get_all_knowledge(user_id, instance_id)

        category_stats: Dict[str, int] = {}
        total_utility = 0.0
        total_confidence = 0.0
        for unit in all_active:
            category_stats[unit.category] = category_stats.get(unit.category, 0) + 1
            total_utility += unit.utility
            total_confidence += unit.confidence

        count = len(all_active)
        snapshot = CognitionSnapshot(
            user_id=user_id,
            instance_id=instance_id,
            snapshot_type=snapshot_type,
            active_knowledge_count=count,
            category_stats=category_stats,
            avg_utility=total_utility / max(count, 1),
            avg_confidence=total_confidence / max(count, 1),
        )
        await self._store.save_cognition_snapshot(snapshot)
        logger.debug(
            f"[TemporalKM] Snapshot created: type={snapshot_type}, "
            f"knowledge={count}"
        )
        return snapshot

    @staticmethod
    def _diff_knowledge(
        past: List[KnowledgeUnit], current: List[KnowledgeUnit],
    ) -> Dict:
        """对比两组知识的差异"""
        past_ids = {u.unit_id for u in past}
        current_ids = {u.unit_id for u in current}
        return {
            "added": len(current_ids - past_ids),
            "removed": len(past_ids - current_ids),
            "unchanged": len(past_ids & current_ids),
        }
