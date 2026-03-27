"""
PredictionStore — 预测记录存储层

操作 prediction_records 表，复用 SessionContextDB 连接。
所有方法强制要求 (user_id, instance_id) 参数，保证用户隔离。
"""
from __future__ import annotations

import time
import uuid
from typing import List, Dict, Optional

from loguru import logger


class PredictionStore:
    """prediction_records 表的 CRUD + prompt 格式化"""

    def __init__(self, sqlite_db):
        self._db = sqlite_db

    async def save(
        self,
        user_id: int,
        instance_id: str,
        session_id: str,
        subject: str,
        prediction_text: str,
        direction: str = "",
        timeframe: str = "",
        verify_before: float = 0.0,
        source_edge_id: str = "",
    ) -> str:
        """写入新预测，返回 pred_id"""
        await self._db._ensure_init()
        pred_id = f"pred_{uuid.uuid4().hex[:12]}"
        now = time.time()
        if not verify_before:
            verify_before = now + 7 * 86400  # 默认 7 天后验证

        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            await db.execute(
                """INSERT INTO prediction_records
                   (pred_id, user_id, instance_id, session_id, subject,
                    prediction_text, direction, timeframe, verify_before,
                    status, created_at, source_edge_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pred_id, user_id, instance_id, session_id, subject,
                    prediction_text, direction or None, timeframe or None,
                    verify_before, "pending", now, source_edge_id or None,
                ),
            )
            await db.commit()

        logger.debug(f"[PredictionStore] Saved pred_id={pred_id} subject={subject!r}")
        return pred_id

    async def get_pending(
        self,
        user_id: int,
        instance_id: str,
        before_ts: float = None,
    ) -> List[Dict]:
        """查询到期的 pending 预测（verify_before <= before_ts）"""
        await self._db._ensure_init()
        ts = before_ts if before_ts is not None else time.time()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                "SELECT * FROM prediction_records "
                "WHERE instance_id=? AND user_id=? AND status='pending' AND verify_before<=? "
                "ORDER BY verify_before ASC",
                (instance_id, user_id, ts),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_distinct_user_ids(self, instance_id: str) -> List[int]:
        """查询该 instance 下所有有 pending 或 verified 记录的真实 user_id，供 cron 遍历用"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                "SELECT DISTINCT user_id FROM prediction_records WHERE instance_id=?",
                (instance_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [r[0] for r in rows if r[0]]

    async def update_verification(
        self,
        user_id: int,
        instance_id: str,
        pred_id: str,
        status: str,
        actual_outcome: str = "",
        accuracy: float = None,
        verification_note: str = "",
    ) -> None:
        """写入验证结果"""
        await self._db._ensure_init()
        now = time.time()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            await db.execute(
                "UPDATE prediction_records SET status=?, actual_outcome=?, accuracy=?, "
                "verification_note=?, verified_at=? "
                "WHERE instance_id=? AND user_id=? AND pred_id=?",
                (status, actual_outcome or None, accuracy, verification_note or None,
                 now, instance_id, user_id, pred_id),
            )
            await db.commit()

    async def get_accuracy_summary(
        self,
        user_id: int,
        instance_id: str,
        subject: str = None,
        recent_n: int = 10,
    ) -> Dict:
        """
        返回历史胜率摘要：
        {"total", "correct", "wrong", "pending", "accuracy_rate", "recent": [...]}
        """
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)

            base_where = "WHERE instance_id=? AND user_id=?"
            params_base = [instance_id, user_id]
            if subject:
                base_where += " AND subject LIKE ?"
                params_base.append(f"%{subject}%")

            # 统计各状态数量
            async with db.execute(
                f"SELECT status, COUNT(*) as cnt FROM prediction_records {base_where} GROUP BY status",
                params_base,
            ) as cur:
                stat_rows = await cur.fetchall()

            counts = {"pending": 0, "verified_correct": 0, "verified_wrong": 0,
                      "expired": 0, "unverifiable": 0}
            for r in stat_rows:
                counts[r["status"]] = r["cnt"]

            total_verified = counts["verified_correct"] + counts["verified_wrong"]
            accuracy_rate = (
                counts["verified_correct"] / total_verified if total_verified > 0 else 0.0
            )

            # 最近 N 条记录
            async with db.execute(
                f"SELECT subject, prediction_text, status, actual_outcome, verify_before, created_at "
                f"FROM prediction_records {base_where} "
                f"ORDER BY created_at DESC LIMIT ?",
                [*params_base, recent_n],
            ) as cur:
                recent_rows = await cur.fetchall()

        return {
            "total": sum(counts.values()),
            "correct": counts["verified_correct"],
            "wrong": counts["verified_wrong"],
            "pending": counts["pending"],
            "expired": counts["expired"],
            "accuracy_rate": round(accuracy_rate, 2),
            "recent": [dict(r) for r in recent_rows],
        }

    async def get_verified_for_subject(
        self,
        user_id: int,
        instance_id: str,
        subject: str,
        limit: int = 5,
    ) -> List[Dict]:
        """查询与 subject 相关的已验证预测记录（有结论的）"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                "SELECT * FROM prediction_records "
                "WHERE instance_id=? AND user_id=? "
                "AND status IN ('verified_correct','verified_wrong') "
                "AND subject LIKE ? "
                "ORDER BY verified_at DESC LIMIT ?",
                (instance_id, user_id, f"%{subject}%", limit),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_context_for_prompt(
        self,
        user_id: int,
        instance_id: str,
        subject: str = None,
    ) -> str:
        """
        格式化已有结论的历史预测供 KnowledgeRetriever 注入 prompt。
        只注入有验证结论的记录（verified_correct/verified_wrong），
        与 subject 相关；无结论的 pending 不注入。
        返回空字符串表示无相关已验证预测。
        """
        if not subject:
            return ""
        try:
            records = await self.get_verified_for_subject(
                user_id, instance_id, subject=subject, limit=5
            )
            if not records:
                return ""

            from datetime import datetime

            correct = sum(1 for r in records if r["status"] == "verified_correct")
            wrong = sum(1 for r in records if r["status"] == "verified_wrong")
            total = correct + wrong
            rate_str = f"（胜率 {int(correct / total * 100)}%）" if total > 0 else ""

            lines = [
                f"\n[{subject}历史预测验证]",
                f"共{total}条已验证：正确{correct}次，错误{wrong}次{rate_str}",
            ]
            status_label = {"verified_correct": "✓正确", "verified_wrong": "✗错误"}
            for r in records:
                ts = r.get("created_at", 0)
                date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?"
                st = status_label.get(r["status"], r["status"])
                outcome = (r.get("actual_outcome") or "").strip()
                pred_text = r.get("prediction_text", "")[:50]
                outcome_part = f"→ {outcome[:40]}" if outcome else ""
                lines.append(f"- {date_str} 「{pred_text}」{outcome_part}（{st}）")

            return "\n".join(lines)
        except Exception as e:
            logger.debug(f"[PredictionStore] get_context_for_prompt failed: {e}")
            return ""

    async def get_accuracy_by_time_window(
        self,
        user_id: int,
        instance_id: str,
        window_days: int = 7,
    ) -> Dict:
        """
        获取指定时间窗口内的分 subject 准确率。
        返回: {
            "overall": {"verified": N, "correct": M, "accuracy": M/N},
            "by_subject": {
                "国盾量子": {"verified": 8, "correct": 5, "accuracy": 0.625},
            }
        }
        """
        await self._db._ensure_init()
        since = time.time() - window_days * 86400
        async with self._db._connect() as db:
            await self._db._setup_conn(db)

            # 整体统计
            async with db.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN status='verified_correct' THEN 1 ELSE 0 END) as correct "
                "FROM prediction_records "
                "WHERE instance_id=? AND user_id=? "
                "AND status IN ('verified_correct','verified_wrong') "
                "AND verified_at >= ?",
                (instance_id, user_id, since),
            ) as cur:
                row = await cur.fetchone()

            total = (row["total"] or 0) if row else 0
            correct = (row["correct"] or 0) if row else 0

            # 按 subject 分组
            async with db.execute(
                "SELECT subject, COUNT(*) as verified, "
                "SUM(CASE WHEN status='verified_correct' THEN 1 ELSE 0 END) as correct "
                "FROM prediction_records "
                "WHERE instance_id=? AND user_id=? "
                "AND status IN ('verified_correct','verified_wrong') "
                "AND verified_at >= ? "
                "GROUP BY subject",
                (instance_id, user_id, since),
            ) as cur:
                subject_rows = await cur.fetchall()

        by_subject = {}
        for sr in subject_rows:
            v = sr["verified"] or 0
            c = sr["correct"] or 0
            by_subject[sr["subject"]] = {
                "verified": v,
                "correct": c,
                "wrong": v - c,
                "accuracy": round(c / v, 4) if v > 0 else 0.0,
            }

        return {
            "overall": {
                "verified": total,
                "correct": correct,
                "wrong": total - correct,
                "accuracy": round(correct / total, 4) if total > 0 else 0.0,
            },
            "by_subject": by_subject,
        }

    async def get_high_confidence_verified(
        self,
        user_id: int,
        instance_id: str,
        min_accuracy: float = 0.9,
        limit: int = 50,
    ) -> List[Dict]:
        """获取高置信已验证记录，用于自动提炼基础评测用例"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                "SELECT * FROM prediction_records "
                "WHERE instance_id=? AND user_id=? "
                "AND status='verified_correct' "
                "AND accuracy >= ? "
                "AND subject IS NOT NULL AND subject != '' "
                "ORDER BY verified_at DESC LIMIT ?",
                (instance_id, user_id, min_accuracy, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_verified_for_learning(
        self,
        user_id: int,
        instance_id: str,
        limit: int = 50,
        since_ts: float = None,
        subject: str = None,
    ) -> List[Dict]:
        """获取已验证的预测记录（用于 StrategyLearner 归因分析）"""
        await self._db._ensure_init()
        since = since_ts or (time.time() - 30 * 86400)  # 默认最近 30 天
        sql = (
            "SELECT * FROM prediction_records "
            "WHERE instance_id=? AND user_id=? "
            "AND status IN ('verified_correct', 'verified_wrong') "
            "AND verified_at >= ? "
        )
        params: list = [instance_id, user_id, since]
        if subject:
            sql += "AND subject LIKE ? "
            params.append(f"%{subject}%")
        sql += "ORDER BY verified_at DESC LIMIT ?"
        params.append(limit)
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_subjects_with_enough_data(
        self,
        user_id: int,
        instance_id: str,
        min_verified: int = 5,
        since_ts: float = None,
    ) -> List[Dict]:
        """
        返回已验证数量达到 min_verified 的 subject 列表，按数量降序。
        用于 StrategyLearner 按主体分组学习。
        """
        await self._db._ensure_init()
        since = since_ts or (time.time() - 90 * 86400)  # 默认 90 天内
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                "SELECT subject, COUNT(*) as cnt, "
                "SUM(CASE WHEN status='verified_correct' THEN 1 ELSE 0 END) as correct_cnt "
                "FROM prediction_records "
                "WHERE instance_id=? AND user_id=? "
                "AND status IN ('verified_correct','verified_wrong') "
                "AND verified_at >= ? "
                "GROUP BY subject HAVING cnt >= ? "
                "ORDER BY cnt DESC",
                (instance_id, user_id, since, min_verified),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]
