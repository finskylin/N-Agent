"""
Knowledge Store — 知识存储层

基于 SessionContextDB 的存储层，复用现有 SQLite 连接。
提供 CRUD 方法，所有容量上限从 config 读取。
"""
import json
import time
from typing import List, Dict, Optional, Any

from loguru import logger

from .models import (
    KnowledgeUnit, Episode, SkillProfile, PreferenceUnit,
    CognitionChange, CognitionSnapshot, EvolutionTask,
    CrystallizedSkill, HeatScoreConfig,
)


class KnowledgeStore:
    """知识存储层 — 复用 SessionContextDB 实例"""

    def __init__(self, sqlite_db, config: dict, embedding_client=None):
        self._db = sqlite_db
        self._config = config
        self._heat_config = HeatScoreConfig.from_config(config)
        self._embedding_client = embedding_client

    # ──── Episode ────

    async def save_episode(self, episode: Episode):
        """保存 Episode"""
        await self._db._ensure_init()
        d = episode.to_dict()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            await db.execute(
                """INSERT OR REPLACE INTO episodes
                   (episode_id, user_id, instance_id, session_id, query,
                    skill_executions, feedback,
                    total_duration_ms, success, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (d["episode_id"], d["user_id"], d["instance_id"],
                 d["session_id"], d["query"], d["skill_executions"],
                 d["feedback"],
                 d["total_duration_ms"], d["success"], d["created_at"]),
            )
            await db.commit()

    async def get_recent_episodes(
        self, user_id: int, instance_id: str, limit: int = 10,
    ) -> List[Dict]:
        """获取最近 N 条 Episode"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            cursor = await db.execute(
                """SELECT * FROM episodes
                   WHERE user_id = ? AND instance_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (user_id, instance_id, limit),
            )
            rows = await cursor.fetchall()
            return [self._episode_row_to_dict(row) for row in rows]

    @staticmethod
    def _episode_row_to_dict(row) -> dict:
        d = dict(row)
        for key in ("skill_executions", "feedback"):
            if d.get(key) and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        d["success"] = bool(d.get("success", 0))
        return d

    # ──── Knowledge Units ────

    async def save_knowledge(self, unit: KnowledgeUnit, user_id: int, instance_id: str):
        """保存知识单元"""
        await self._db._ensure_init()
        tags_json = json.dumps(unit.tags, ensure_ascii=False)
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            await db.execute(
                """INSERT OR REPLACE INTO knowledge_units
                   (unit_id, user_id, instance_id, category, text, tags,
                    utility, confidence, access_count, hit_count,
                    feedback_reinforcements, feedback_decays,
                    event_time, ingestion_time, valid_from, valid_until,
                    superseded_by, supersedes, update_reason,
                    source_episode_id, created_at, last_accessed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (unit.unit_id, user_id, instance_id, unit.category, unit.text, tags_json,
                 unit.utility, unit.confidence, unit.access_count, unit.hit_count,
                 unit.feedback_reinforcements, unit.feedback_decays,
                 unit.event_time, unit.ingestion_time, unit.valid_from, unit.valid_until,
                 unit.superseded_by, unit.supersedes, unit.update_reason,
                 unit.source_episode_id, unit.created_at, unit.last_accessed),
            )
            await db.commit()

        # 后台生成 embedding（不阻塞主流程）
        if self._embedding_client and unit.text:
            try:
                from agent_core.background_task_manager import get_global_task_manager
                get_global_task_manager().create_task(
                    self._embed_and_update(unit.unit_id, unit.text),
                    task_name=f"embed_ku_{unit.unit_id[:8]}",
                )
            except Exception as e:
                logger.warning(f"[KnowledgeStore] Failed to schedule embed task: {e}")

    async def _embed_and_update(self, unit_id: str, text: str):
        """后台任务：生成 embedding 并写入 BLOB"""
        try:
            vec = await self._embedding_client.embed(text)
            if vec is not None:
                blob = self._embedding_client.to_blob(vec)
                await self._db.set_knowledge_embedding(unit_id, blob)
        except Exception as e:
            logger.warning(f"[KnowledgeStore] embed_and_update failed for {unit_id}: {e}")

    async def retrieve(
        self, user_id: int, instance_id: str, query_tags: List[str],
        category: Optional[str] = None, top_k: Optional[int] = None,
        as_of_time: Optional[float] = None,
    ) -> List[KnowledgeUnit]:
        """
        检索知识，含评分排序。
        as_of_time: Graphiti 风格 point-in-time 查询。
        """
        await self._db._ensure_init()
        top_k = top_k or self._config.get("retriever", {}).get("default_top_k", 10)
        weights = self._config.get("retriever", {}).get("score_weights", {})
        w_tag = weights.get("tag_overlap", 0.4)
        w_utility = weights.get("utility", 0.3)
        w_heat = weights.get("heat", 0.3)
        min_score = self._config.get("retriever", {}).get("min_score_threshold", 0.1)

        async with self._db._connect() as db:
            await self._db._setup_conn(db)

            if as_of_time:
                sql = (
                    "SELECT * FROM knowledge_units "
                    "WHERE user_id = ? AND instance_id = ? "
                    "AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?)"
                )
                params: list = [user_id, instance_id, as_of_time, as_of_time]
            else:
                sql = (
                    "SELECT * FROM knowledge_units "
                    "WHERE user_id = ? AND instance_id = ? "
                    "AND valid_until IS NULL"
                )
                params = [user_id, instance_id]

            if category:
                sql += " AND category = ?"
                params.append(category)

            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()

        # Score and rank
        query_tags_set = set(query_tags)
        scored = []
        for row in rows:
            unit = self._row_to_knowledge_unit(row)
            unit_tags_set = set(unit.tags)
            union = query_tags_set | unit_tags_set
            tag_overlap = len(query_tags_set & unit_tags_set) / max(len(union), 1)
            heat = unit.heat_score(self._heat_config)
            score = w_tag * tag_overlap + w_utility * unit.utility + w_heat * heat
            if score >= min_score:
                scored.append((score, unit))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [unit for _, unit in scored[:top_k]]

    async def retrieve_semantic(
        self,
        query_vec,
        user_id: int,
        instance_id: str,
        top_k: Optional[int] = None,
        category: Optional[str] = None,
        heat_weight: float = 0.1,
    ) -> List[KnowledgeUnit]:
        """
        语义向量召回 knowledge_units。

        有 embedding 的用余弦相似度打分；无 embedding 的用 utility/heat 作 fallback。
        """
        try:
            import numpy as np
        except ImportError:
            return await self.retrieve(user_id, instance_id, [], top_k=top_k, category=category)

        top_k = top_k or self._config.get("retriever", {}).get("default_top_k", 10)

        rows = await self._db.get_knowledge_units_with_embedding(user_id, instance_id)
        if category:
            rows = [r for r in rows if r.get("category") == category]
        if not rows:
            return []

        with_emb = [(r, r["text_embedding"]) for r in rows if r.get("text_embedding")]
        without_emb = [r for r in rows if not r.get("text_embedding")]

        scored = []

        if with_emb and self._embedding_client:
            try:
                blobs = [blob for _, blob in with_emb]
                vecs = np.stack([self._embedding_client.from_blob(b) for b in blobs])
                scores = self._embedding_client.cosine_batch(query_vec, vecs)
                max_heat = max((self._row_to_knowledge_unit(r).heat_score(self._heat_config)
                                for r, _ in with_emb), default=1.0) or 1.0
                for i, (row, _) in enumerate(with_emb):
                    unit = self._row_to_knowledge_unit(row)
                    combined = float(scores[i]) + heat_weight * unit.heat_score(self._heat_config) / max_heat
                    scored.append((combined, unit))
            except Exception as e:
                logger.warning(f"[KnowledgeStore] cosine_batch failed: {e}")
                without_emb.extend([r for r, _ in with_emb])

        if without_emb:
            max_heat = max((self._row_to_knowledge_unit(r).heat_score(self._heat_config)
                            for r in without_emb), default=1.0) or 1.0
            for row in without_emb:
                unit = self._row_to_knowledge_unit(row)
                fallback = heat_weight * unit.heat_score(self._heat_config) / max_heat + unit.utility * 0.05
                scored.append((fallback, unit))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [unit for _, unit in scored[:top_k]]

    async def update_knowledge_field(
        self, unit_id: str, field_updates: Dict[str, Any],
    ):
        """部分更新知识单元字段"""
        await self._db._ensure_init()
        allowed = {
            "utility", "confidence", "access_count", "hit_count",
            "feedback_reinforcements", "feedback_decays",
            "valid_until", "superseded_by", "last_accessed",
        }
        set_clauses = []
        values = []
        for key, val in field_updates.items():
            if key not in allowed:
                continue
            set_clauses.append(f"{key} = ?")
            values.append(val)
        if not set_clauses:
            return
        values.append(unit_id)
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            await db.execute(
                f"UPDATE knowledge_units SET {', '.join(set_clauses)} WHERE unit_id = ?",
                values,
            )
            await db.commit()

    async def get_knowledge_by_id(self, unit_id: str) -> Optional[KnowledgeUnit]:
        """按 ID 获取知识单元"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            cursor = await db.execute(
                "SELECT * FROM knowledge_units WHERE unit_id = ?", (unit_id,)
            )
            row = await cursor.fetchone()
            return self._row_to_knowledge_unit(row) if row else None

    async def get_all_knowledge(
        self, user_id: int, instance_id: str,
        include_superseded: bool = False,
    ) -> List[KnowledgeUnit]:
        """获取所有知识单元"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            if include_superseded:
                sql = "SELECT * FROM knowledge_units WHERE user_id = ? AND instance_id = ?"
            else:
                sql = (
                    "SELECT * FROM knowledge_units WHERE user_id = ? AND instance_id = ? "
                    "AND valid_until IS NULL"
                )
            cursor = await db.execute(sql, (user_id, instance_id))
            rows = await cursor.fetchall()
            return [self._row_to_knowledge_unit(row) for row in rows]

    @staticmethod
    def _row_to_knowledge_unit(row) -> KnowledgeUnit:
        d = dict(row)
        tags = d.get("tags", "[]")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                tags = []
        return KnowledgeUnit(
            unit_id=d.get("unit_id", ""),
            category=d.get("category", ""),
            text=d.get("text", ""),
            tags=tags,
            utility=d.get("utility", 0.5),
            confidence=d.get("confidence", 0.5),
            access_count=d.get("access_count", 0),
            hit_count=d.get("hit_count", 0),
            feedback_reinforcements=d.get("feedback_reinforcements", 0),
            feedback_decays=d.get("feedback_decays", 0),
            event_time=d.get("event_time"),
            ingestion_time=d.get("ingestion_time", 0.0),
            valid_from=d.get("valid_from", 0.0),
            valid_until=d.get("valid_until"),
            superseded_by=d.get("superseded_by"),
            supersedes=d.get("supersedes"),
            update_reason=d.get("update_reason"),
            source_episode_id=d.get("source_episode_id", ""),
            created_at=d.get("created_at", 0.0),
            last_accessed=d.get("last_accessed", 0.0),
        )

    # ──── Skill Profiles ────

    async def update_skill_profile(
        self, skill_name: str, user_id: int, instance_id: str,
        duration_ms: float, success: bool, confidence: float = 0.0,
    ):
        """更新 Skill 执行档案（upsert）"""
        await self._db._ensure_init()
        now = time.time()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            cursor = await db.execute(
                "SELECT * FROM skill_profiles WHERE skill_name = ? AND user_id = ? AND instance_id = ?",
                (skill_name, user_id, instance_id),
            )
            row = await cursor.fetchone()
            if row:
                d = dict(row)
                total = d["total_executions"] + 1
                sc = d["success_count"] + (1 if success else 0)
                fc = d["failure_count"] + (0 if success else 1)
                avg_dur = (d["avg_duration_ms"] * d["total_executions"] + duration_ms) / total
                avg_conf = (d["avg_confidence"] * d["total_executions"] + confidence) / total
                await db.execute(
                    """UPDATE skill_profiles SET
                       total_executions = ?, success_count = ?, failure_count = ?,
                       avg_duration_ms = ?, avg_confidence = ?,
                       last_execution_at = ?, updated_at = ?
                       WHERE skill_name = ? AND user_id = ? AND instance_id = ?""",
                    (total, sc, fc, avg_dur, avg_conf, now, now,
                     skill_name, user_id, instance_id),
                )
            else:
                await db.execute(
                    """INSERT INTO skill_profiles
                       (skill_name, user_id, instance_id, total_executions,
                        success_count, failure_count, avg_duration_ms, avg_confidence,
                        like_count, dislike_count, satisfaction_score,
                        cognition_version_count, last_execution_at, updated_at)
                       VALUES (?, ?, ?, 1, ?, ?, ?, ?, 0, 0, 0.5, 0, ?, ?)""",
                    (skill_name, user_id, instance_id,
                     1 if success else 0, 0 if success else 1,
                     duration_ms, confidence, now, now),
                )
            await db.commit()

    async def get_skill_profile(
        self, skill_name: str, user_id: int, instance_id: str,
    ) -> Optional[SkillProfile]:
        """获取 Skill 档案"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            cursor = await db.execute(
                "SELECT * FROM skill_profiles WHERE skill_name = ? AND user_id = ? AND instance_id = ?",
                (skill_name, user_id, instance_id),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            d = dict(row)
            return SkillProfile(**{k: d[k] for k in SkillProfile.__dataclass_fields__ if k in d})

    async def update_skill_satisfaction(
        self, skill_name: str, user_id: int, instance_id: str,
        like: bool,
    ):
        """更新 Skill 满意度"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            if like:
                await db.execute(
                    """UPDATE skill_profiles SET like_count = like_count + 1,
                       satisfaction_score = MIN(1.0, satisfaction_score + 0.05),
                       updated_at = ?
                       WHERE skill_name = ? AND user_id = ? AND instance_id = ?""",
                    (time.time(), skill_name, user_id, instance_id),
                )
            else:
                await db.execute(
                    """UPDATE skill_profiles SET dislike_count = dislike_count + 1,
                       satisfaction_score = MAX(0.0, satisfaction_score - 0.08),
                       updated_at = ?
                       WHERE skill_name = ? AND user_id = ? AND instance_id = ?""",
                    (time.time(), skill_name, user_id, instance_id),
                )
            await db.commit()

    # ──── Preferences ────

    async def save_preference(self, pref: PreferenceUnit):
        """保存用户偏好"""
        await self._db._ensure_init()
        d = pref.to_dict()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            await db.execute(
                """INSERT OR REPLACE INTO preferences
                   (preference_id, user_id, instance_id, dimension, value,
                    confidence, source_episode_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (d["preference_id"], d["user_id"], d["instance_id"],
                 d["dimension"], d["value"], d["confidence"],
                 d["source_episode_id"], d["created_at"], d["updated_at"]),
            )
            await db.commit()

    async def get_preferences(
        self, user_id: int, instance_id: str,
    ) -> List[PreferenceUnit]:
        """获取所有用户偏好"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            cursor = await db.execute(
                "SELECT * FROM preferences WHERE user_id = ? AND instance_id = ?",
                (user_id, instance_id),
            )
            rows = await cursor.fetchall()
            return [PreferenceUnit(**{k: dict(r)[k] for k in PreferenceUnit.__dataclass_fields__ if k in dict(r)}) for r in rows]

    # ──── Cognition Changes ────

    async def save_cognition_change(self, change: CognitionChange):
        """保存认知变迁记录"""
        await self._db._ensure_init()
        d = change.to_dict()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            await db.execute(
                """INSERT OR REPLACE INTO cognition_changes
                   (change_id, old_unit_id, new_unit_id, reason, change_type,
                    timestamp, user_id, instance_id, affected_skills, confidence_delta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (d["change_id"], d["old_unit_id"], d["new_unit_id"],
                 d["reason"], d["change_type"], d["timestamp"],
                 d["user_id"], d["instance_id"],
                 json.dumps(d["affected_skills"], ensure_ascii=False),
                 d["confidence_delta"]),
            )
            await db.commit()

    async def get_recent_cognition_changes(
        self, user_id: int, instance_id: str, limit: int = 10,
    ) -> List[Dict]:
        """获取最近认知变迁"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            cursor = await db.execute(
                """SELECT cc.*, ku_old.text as old_text, ku_new.text as new_text
                   FROM cognition_changes cc
                   LEFT JOIN knowledge_units ku_old ON cc.old_unit_id = ku_old.unit_id
                   LEFT JOIN knowledge_units ku_new ON cc.new_unit_id = ku_new.unit_id
                   WHERE cc.user_id = ? AND cc.instance_id = ?
                   ORDER BY cc.timestamp DESC LIMIT ?""",
                (user_id, instance_id, limit),
            )
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                d = dict(row)
                if d.get("affected_skills") and isinstance(d["affected_skills"], str):
                    try:
                        d["affected_skills"] = json.loads(d["affected_skills"])
                    except (json.JSONDecodeError, TypeError):
                        d["affected_skills"] = []
                results.append(d)
            return results

    # ──── Cognition Snapshots ────

    async def save_cognition_snapshot(self, snapshot: CognitionSnapshot):
        """保存认知快照"""
        await self._db._ensure_init()
        d = snapshot.to_dict()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            await db.execute(
                """INSERT OR REPLACE INTO cognition_snapshots
                   (snapshot_id, user_id, instance_id, snapshot_time,
                    snapshot_type, active_knowledge_count, category_stats,
                    avg_utility, avg_confidence, skill_profile_summary, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (d["snapshot_id"], d["user_id"], d["instance_id"],
                 d["snapshot_time"], d["snapshot_type"],
                 d["active_knowledge_count"], d["category_stats"],
                 d["avg_utility"], d["avg_confidence"],
                 d["skill_profile_summary"], d["created_at"]),
            )
            await db.commit()

    # ──── Evolution Tasks ────

    async def save_evolution_task(self, task: EvolutionTask):
        """保存进化任务"""
        await self._db._ensure_init()
        d = task.to_dict()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            await db.execute(
                """INSERT OR REPLACE INTO evolution_tasks
                   (task_id, user_id, instance_id, gap_description,
                    status, phase, exploration_log, result_knowledge_ids,
                    knowledge_snapshot_id, created_at, updated_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (d["task_id"], d["user_id"], d["instance_id"],
                 d["gap_description"], d["status"], d["phase"],
                 d["exploration_log"], d["result_knowledge_ids"],
                 d["knowledge_snapshot_id"],
                 d["created_at"], d["updated_at"], d["completed_at"]),
            )
            await db.commit()

    async def get_evolution_tasks(
        self, user_id: int, instance_id: str, status: Optional[str] = None,
    ) -> List[Dict]:
        """获取进化任务"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            sql = "SELECT * FROM evolution_tasks WHERE user_id = ? AND instance_id = ?"
            params: list = [user_id, instance_id]
            if status:
                sql += " AND status = ?"
                params.append(status)
            sql += " ORDER BY created_at DESC"
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                d = dict(row)
                for key in ("exploration_log", "result_knowledge_ids"):
                    if d.get(key) and isinstance(d[key], str):
                        try:
                            d[key] = json.loads(d[key])
                        except (json.JSONDecodeError, TypeError):
                            d[key] = []
                results.append(d)
            return results

    # ──── Crystallized Skills ────

    async def save_crystallized_skill(self, crystal: CrystallizedSkill):
        """保存结晶 Skill"""
        await self._db._ensure_init()
        d = crystal.to_dict()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            await db.execute(
                """INSERT OR REPLACE INTO crystallized_skills
                   (crystal_id, user_id, instance_id, skill_name,
                    description, workflow, prompt_template, source_episodes,
                    status, rejection_reason, test_result,
                    occurrences, success_rate, like_count,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (d["crystal_id"], d["user_id"], d["instance_id"],
                 d["skill_name"], d["description"], d["workflow"],
                 d["prompt_template"], d["source_episodes"],
                 d["status"], d["rejection_reason"], d["test_result"],
                 d["occurrences"], d["success_rate"], d["like_count"],
                 d["created_at"], d["updated_at"]),
            )
            await db.commit()

    async def get_crystallized_skills(
        self, user_id: int, instance_id: str, status: Optional[str] = None,
    ) -> List[Dict]:
        """获取结晶 Skill"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            sql = "SELECT * FROM crystallized_skills WHERE user_id = ? AND instance_id = ?"
            params: list = [user_id, instance_id]
            if status:
                sql += " AND status = ?"
                params.append(status)
            sql += " ORDER BY created_at DESC"
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                d = dict(row)
                for key in ("source_episodes", "test_result"):
                    if d.get(key) and isinstance(d[key], str):
                        try:
                            d[key] = json.loads(d[key])
                        except (json.JSONDecodeError, TypeError):
                            pass
                results.append(d)
            return results

    # ──── Decay & Cleanup ────

    async def decay_and_cleanup(self, user_id: int, instance_id: str):
        """
        衰减策略: 不物理删除知识，标记为 archived（valid_until）。
        清理旧 Episode（已蒸馏的可物理删除）。
        """
        await self._db._ensure_init()
        max_age_days = self._config.get("store", {}).get("cleanup_max_age_days", 180)
        max_episodes = self._config.get("episode_tracker", {}).get("max_episodes_per_user", 1000)
        cutoff = time.time() - max_age_days * 86400
        now = time.time()

        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            # 过期知识: 标记 valid_until（不删除）
            await db.execute(
                """UPDATE knowledge_units SET valid_until = ?
                   WHERE valid_until IS NULL AND last_accessed < ?
                   AND user_id = ? AND instance_id = ?""",
                (now, cutoff, user_id, instance_id),
            )
            # 清理旧 Episode（保留最近 N 条）
            await db.execute(
                """DELETE FROM episodes WHERE episode_id NOT IN (
                     SELECT episode_id FROM episodes
                     WHERE user_id = ? AND instance_id = ?
                     ORDER BY created_at DESC LIMIT ?
                   ) AND user_id = ? AND instance_id = ?""",
                (user_id, instance_id, max_episodes, user_id, instance_id),
            )
            await db.commit()
            logger.debug(
                f"[KnowledgeStore] Decay/cleanup done for user={user_id}, "
                f"cutoff_days={max_age_days}"
            )
