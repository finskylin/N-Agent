"""
DreamConsolidator — 周期性深度记忆整合

仿照 Claude Code AutoDream，在后台异步执行四阶段知识整合：
  Phase 1: KnowledgeConsolidator  — 清理过期/低质知识，合并同主题碎片
  Phase 2: GraphConsolidator      — 删除孤立节点（合并留给 upsert 的幂等机制）
  Phase 3: MTMConsolidator        — 淘汰低热度/过期 MTM 页面
  Phase 4: DeepReflection         — 聚合近期 Episode，LLM 提炼跨会话知识

触发机制（任一满足即执行）：
  A. 定时触发：`prepare_session()` 时检查距上次 Dream 是否超过 dream_interval_hours
  B. 会话数阈值：完成 dream_session_threshold 次会话后触发

设计原则：
  - 全部 opt-in，默认通过 V4Config.dream_enabled=True 控制
  - 只调用已有模块的现有方法，不新增存储表（状态存在已有 SQLite）
  - 执行全程后台异步，不阻塞任何请求
  - 任一 Phase 失败不影响其他 Phase
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Dict, List, Optional

from loguru import logger

if TYPE_CHECKING:
    from agent_core.knowledge.store import KnowledgeStore
    from agent_core.knowledge.graph_store import GraphStore
    from agent_core.memory.mid_term_memory import MidTermMemory
    from agent_core.session.context_db import SessionContextDB


class DreamConsolidator:
    """
    周期性深度记忆整合器

    由 native_agent 初始化后注入 session_engine，
    在 prepare_session() 中检查触发条件，异步后台执行。
    """

    def __init__(
        self,
        config,
        knowledge_store: Optional["KnowledgeStore"] = None,
        graph_store: Optional["GraphStore"] = None,
        mtm: Optional["MidTermMemory"] = None,
        sqlite_db: Optional["SessionContextDB"] = None,
        llm_call=None,
    ):
        self._config = config
        self._knowledge_store = knowledge_store
        self._graph_store = graph_store
        self._mtm = mtm
        self._sqlite_db = sqlite_db
        self._llm_call = llm_call  # call_llm(prompt, ...) → str

        # 内存级状态（per-process），DB 持久化通过 _load/_save_state 操作
        self._last_dream_at: Dict[int, float] = {}        # user_id → timestamp
        self._session_counter: Dict[int, int] = {}        # user_id → session count
        self._running: Dict[int, bool] = {}               # user_id → is_running

    # ── 触发判断 ────────────────────────────────────────────────────────────────

    def should_run(self, user_id: int) -> bool:
        """检查是否应触发 Dream（不阻塞，O(1)）"""
        if not getattr(self._config, "dream_enabled", True):
            return False

        # 防止并发重入
        if self._running.get(user_id):
            return False

        interval_hours = getattr(self._config, "dream_interval_hours", 24)
        threshold = getattr(self._config, "dream_session_threshold", 50)

        now = time.time()
        last = self._last_dream_at.get(user_id, 0.0)
        elapsed_hours = (now - last) / 3600

        counter = self._session_counter.get(user_id, 0)

        trigger_time = elapsed_hours >= interval_hours
        trigger_count = counter >= threshold

        return trigger_time or trigger_count

    def on_session_complete(self, user_id: int):
        """每次 session 完成时调用，累加计数"""
        self._session_counter[user_id] = self._session_counter.get(user_id, 0) + 1

    def _mark_started(self, user_id: int):
        self._running[user_id] = True

    def _mark_finished(self, user_id: int, stats: dict):
        now = time.time()
        self._running[user_id] = False
        self._last_dream_at[user_id] = now
        self._session_counter[user_id] = 0
        logger.info(f"[Dream] user={user_id} dream finished: {stats}")

    # ── 主入口 ─────────────────────────────────────────────────────────────────

    async def run(self, user_id: int, instance_id: str = "default") -> dict:
        """
        执行四阶段 Dream（全程后台，异常不向上抛）

        Returns: 统计字典 {merged, deleted_knowledge, deleted_mtm, reflected}
        """
        self._mark_started(user_id)
        stats = {
            "merged": 0,
            "deleted_knowledge": 0,
            "deleted_mtm": 0,
            "reflected": 0,
            "errors": [],
        }

        logger.info(f"[Dream] user={user_id} starting consolidation...")
        t0 = time.time()

        # Phase 1: 知识库整合
        try:
            r = await self._phase1_knowledge(user_id, instance_id)
            stats["merged"] += r.get("merged", 0)
            stats["deleted_knowledge"] += r.get("deleted", 0)
        except Exception as e:
            logger.error(f"[Dream] Phase1 error: {e}")
            stats["errors"].append(f"phase1: {e}")

        # Phase 2: 图谱整合
        try:
            r = await self._phase2_graph(user_id, instance_id)
            stats["deleted_knowledge"] += r.get("deleted_nodes", 0)
        except Exception as e:
            logger.error(f"[Dream] Phase2 error: {e}")
            stats["errors"].append(f"phase2: {e}")

        # Phase 3: MTM 整合
        try:
            r = await self._phase3_mtm(user_id, instance_id)
            stats["deleted_mtm"] += r.get("evicted", 0)
        except Exception as e:
            logger.error(f"[Dream] Phase3 error: {e}")
            stats["errors"].append(f"phase3: {e}")

        # Phase 4: 深度反思
        try:
            r = await self._phase4_reflect(user_id, instance_id)
            stats["reflected"] += r.get("reflected", 0)
        except Exception as e:
            logger.error(f"[Dream] Phase4 error: {e}")
            stats["errors"].append(f"phase4: {e}")

        elapsed = round(time.time() - t0, 1)
        stats["duration_seconds"] = elapsed
        self._mark_finished(user_id, stats)

        return stats

    # ── Phase 1: 知识库整合 ────────────────────────────────────────────────────

    async def _phase1_knowledge(self, user_id: int, instance_id: str) -> dict:
        """清理过期/低质知识，合并同主题碎片"""
        if not self._knowledge_store:
            return {}

        merged = 0
        deleted = 0

        # 1a. 清理 valid_until 已过期的知识（superseded）
        deleted += await self._delete_superseded_knowledge(user_id, instance_id)

        # 1b. 清理低质量 + 长期未访问的知识
        deleted += await self._delete_stale_knowledge(user_id, instance_id)

        # 1c. 合并同 tags 的碎片知识（需要 LLM）
        if self._llm_call:
            merged += await self._merge_fragmented_knowledge(user_id, instance_id)

        logger.info(f"[Dream/Phase1] user={user_id} deleted={deleted} merged={merged}")
        return {"deleted": deleted, "merged": merged}

    async def _delete_superseded_knowledge(self, user_id: int, instance_id: str) -> int:
        """删除 superseded_by 不为空且超过保留期的知识"""
        keep_days = getattr(self._config, "dream_superseded_keep_days", 7)
        cutoff = time.time() - keep_days * 86400

        try:
            await self._knowledge_store._db._ensure_init()
            async with self._knowledge_store._db._connect() as db:
                await self._knowledge_store._db._setup_conn(db)
                cursor = await db.execute(
                    """SELECT unit_id FROM knowledge_units
                       WHERE user_id = ? AND instance_id = ?
                         AND superseded_by IS NOT NULL
                         AND created_at < ?""",
                    (user_id, instance_id, cutoff),
                )
                rows = await cursor.fetchall()
                unit_ids = [r[0] for r in rows]

                if unit_ids:
                    placeholders = ",".join("?" * len(unit_ids))
                    await db.execute(
                        f"DELETE FROM knowledge_units WHERE unit_id IN ({placeholders})",
                        unit_ids,
                    )
                    await db.commit()
                return len(unit_ids)
        except Exception as e:
            logger.warning(f"[Dream/Phase1] delete_superseded error: {e}")
            return 0

    async def _delete_stale_knowledge(self, user_id: int, instance_id: str) -> int:
        """删除低效用 + 长期未访问的知识"""
        min_utility = getattr(self._config, "dream_stale_min_utility", 0.2)
        max_age_days = getattr(self._config, "dream_stale_max_age_days", 90)
        cutoff = time.time() - max_age_days * 86400

        try:
            await self._knowledge_store._db._ensure_init()
            async with self._knowledge_store._db._connect() as db:
                await self._knowledge_store._db._setup_conn(db)
                cursor = await db.execute(
                    """SELECT unit_id FROM knowledge_units
                       WHERE user_id = ? AND instance_id = ?
                         AND utility < ?
                         AND last_accessed < ?
                         AND superseded_by IS NULL""",
                    (user_id, instance_id, min_utility, cutoff),
                )
                rows = await cursor.fetchall()
                unit_ids = [r[0] for r in rows]

                if unit_ids:
                    placeholders = ",".join("?" * len(unit_ids))
                    await db.execute(
                        f"DELETE FROM knowledge_units WHERE unit_id IN ({placeholders})",
                        unit_ids,
                    )
                    await db.commit()
                return len(unit_ids)
        except Exception as e:
            logger.warning(f"[Dream/Phase1] delete_stale error: {e}")
            return 0

    async def _merge_fragmented_knowledge(self, user_id: int, instance_id: str) -> int:
        """对同 tags 组内碎片知识 LLM 合并（Jaccard > threshold 的组）"""
        merge_threshold = getattr(self._config, "dream_merge_similarity_threshold", 0.7)
        min_group_size = getattr(self._config, "dream_merge_min_group_size", 3)

        try:
            all_units = await self._knowledge_store.get_all_knowledge(
                user_id, instance_id, include_superseded=False
            )
        except Exception as e:
            logger.warning(f"[Dream/Phase1] get_all_knowledge error: {e}")
            return 0

        if len(all_units) < min_group_size:
            return 0

        # 按主 tag 分组
        tag_groups: Dict[str, list] = {}
        for unit in all_units:
            if unit.tags:
                primary_tag = unit.tags[0]
                tag_groups.setdefault(primary_tag, []).append(unit)

        merged_count = 0
        for tag, group in tag_groups.items():
            if len(group) < min_group_size:
                continue

            # 计算组内两两 Jaccard，找相似度 > threshold 的子集
            similar_groups = self._find_similar_subgroups(group, merge_threshold)
            for sub_group in similar_groups:
                if len(sub_group) < min_group_size:
                    continue
                try:
                    ok = await self._llm_merge_group(user_id, instance_id, sub_group)
                    if ok:
                        merged_count += len(sub_group) - 1  # 合并后减少的条目数
                except Exception as e:
                    logger.debug(f"[Dream/Phase1] merge group error: {e}")

        return merged_count

    def _find_similar_subgroups(self, units: list, threshold: float) -> List[list]:
        """基于 tags Jaccard 相似度找出可合并的子组（贪心）"""
        used = set()
        groups = []

        for i, u in enumerate(units):
            if i in used:
                continue
            group = [u]
            tags_i = set(u.tags or [])
            for j, v in enumerate(units):
                if j <= i or j in used:
                    continue
                tags_j = set(v.tags or [])
                if not tags_i or not tags_j:
                    continue
                union = len(tags_i | tags_j)
                intersection = len(tags_i & tags_j)
                jaccard = intersection / union if union > 0 else 0.0
                if jaccard >= threshold:
                    group.append(v)
                    used.add(j)
            if len(group) >= 2:
                used.add(i)
                groups.append(group)

        return groups

    async def _llm_merge_group(self, user_id: int, instance_id: str, units: list) -> bool:
        """调用 LLM 合并知识组，保存合并结果，删除原有条目"""
        import uuid

        snippets = "\n".join(
            f"[{i+1}] {u.text}" for i, u in enumerate(units)
        )
        prompt = (
            "你是知识整合专家。以下是关于同一主题的多条知识片段，请合并为一条。\n"
            "要求：保留所有有效信息，去掉重复，不超过 200 字，中文输出。\n\n"
            f"{snippets}\n\n"
            "合并结果（直接输出合并后的文本）："
        )

        try:
            merged_text = await self._call_llm_simple(prompt, max_tokens=300)
        except Exception as e:
            logger.debug(f"[Dream/Phase1] LLM merge call failed: {e}")
            return False

        if not merged_text or len(merged_text) < 10:
            return False

        # 合并后的新知识：继承最高 utility
        best = max(units, key=lambda u: u.utility)
        merged_tags = list(set(t for u in units for t in (u.tags or [])))

        from agent_core.knowledge.models import KnowledgeUnit
        new_unit = KnowledgeUnit(
            unit_id=str(uuid.uuid4()),
            category=best.category,
            text=merged_text.strip(),
            tags=merged_tags[:10],
            utility=best.utility,
            confidence=best.confidence,
            ingestion_time=time.time(),
            valid_from=time.time(),
            created_at=time.time(),
            last_accessed=time.time(),
        )

        try:
            await self._knowledge_store.save_knowledge(
                new_unit, user_id, instance_id, source_type="dream"
            )
        except Exception as e:
            logger.warning(f"[Dream/Phase1] save merged knowledge error: {e}")
            return False

        # 删除原有条目
        unit_ids = [u.unit_id for u in units]
        try:
            async with self._knowledge_store._db._connect() as db:
                await self._knowledge_store._db._setup_conn(db)
                placeholders = ",".join("?" * len(unit_ids))
                await db.execute(
                    f"DELETE FROM knowledge_units WHERE unit_id IN ({placeholders})",
                    unit_ids,
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"[Dream/Phase1] delete merged originals error: {e}")

        return True

    # ── Phase 2: 图谱整合 ─────────────────────────────────────────────────────

    async def _phase2_graph(self, user_id: int, instance_id: str) -> dict:
        """删除孤立节点（无边连接 + 超过 max_age）"""
        if not self._graph_store:
            return {}

        max_age_days = getattr(self._config, "dream_isolated_node_max_age_days", 30)
        cutoff = time.time() - max_age_days * 86400
        deleted_nodes = 0

        try:
            await self._graph_store._db._ensure_init()
            async with self._graph_store._db._connect() as db:
                await self._graph_store._db._setup_conn(db)

                # 找出无边连接的节点
                cursor = await db.execute(
                    """SELECT node_id FROM knowledge_nodes
                       WHERE user_id = ? AND instance_id = ?
                         AND updated_at < ?
                         AND node_id NOT IN (
                             SELECT DISTINCT source_node_id FROM knowledge_edges
                             WHERE user_id = ? AND instance_id = ?
                             UNION
                             SELECT DISTINCT target_node_id FROM knowledge_edges
                             WHERE user_id = ? AND instance_id = ?
                         )""",
                    (user_id, instance_id, cutoff,
                     user_id, instance_id,
                     user_id, instance_id),
                )
                rows = await cursor.fetchall()
                node_ids = [r[0] for r in rows]

                if node_ids:
                    placeholders = ",".join("?" * len(node_ids))
                    await db.execute(
                        f"DELETE FROM knowledge_nodes WHERE node_id IN ({placeholders})",
                        node_ids,
                    )
                    await db.commit()
                    deleted_nodes = len(node_ids)
        except Exception as e:
            logger.warning(f"[Dream/Phase2] graph cleanup error: {e}")

        logger.info(f"[Dream/Phase2] user={user_id} deleted_nodes={deleted_nodes}")
        return {"deleted_nodes": deleted_nodes}

    # ── Phase 3: MTM 整合 ─────────────────────────────────────────────────────

    async def _phase3_mtm(self, user_id: int, instance_id: str) -> dict:
        """淘汰低热度/过期 MTM 页面"""
        if not self._mtm:
            return {}

        evicted = 0

        # 3a. 清理超龄页面
        cold_max_age = getattr(self._config, "dream_cold_max_age_days", 30)
        try:
            evicted += await self._mtm.cleanup_expired(max_age_days=cold_max_age)
        except Exception as e:
            logger.warning(f"[Dream/Phase3] cleanup_expired error: {e}")

        # 3b. LFU 淘汰（热度低 + 超容）
        try:
            evicted_ids = await self._mtm.evict()
            evicted += len(evicted_ids)
        except Exception as e:
            logger.warning(f"[Dream/Phase3] evict error: {e}")

        logger.info(f"[Dream/Phase3] user={user_id} evicted={evicted}")
        return {"evicted": evicted}

    # ── Phase 4: 深度反思 ─────────────────────────────────────────────────────

    async def _phase4_reflect(self, user_id: int, instance_id: str) -> dict:
        """聚合近期 Episode，LLM 提炼跨会话规律性知识"""
        if not self._knowledge_store or not self._llm_call:
            return {}

        min_episodes = getattr(self._config, "dream_min_episodes", 5)

        try:
            # 获取近期 Episode（按 dream_interval_hours 窗口）
            interval_hours = getattr(self._config, "dream_interval_hours", 24)
            # 复用 get_recent_episodes，取最多 50 条
            episodes = await self._knowledge_store.get_recent_episodes(
                user_id=user_id,
                instance_id=instance_id,
                limit=50,
            )
        except Exception as e:
            logger.warning(f"[Dream/Phase4] get_recent_episodes error: {e}")
            return {}

        if len(episodes) < min_episodes:
            logger.debug(
                f"[Dream/Phase4] user={user_id} episodes={len(episodes)} < min={min_episodes}, skip"
            )
            return {"reflected": 0}

        # 构建 Episode 摘要文本
        summaries = []
        for ep in episodes[-20:]:  # 最多取最近 20 条
            query = ep.get("query", "")
            skills = ep.get("skill_executions", [])
            skill_names = [s.get("skill_name", "") for s in skills] if isinstance(skills, list) else []
            summaries.append(f"Q: {query[:100]} | 使用工具: {', '.join(skill_names[:5])}")

        episodes_text = "\n".join(summaries)
        prompt = (
            "你是一个智能知识提炼专家。以下是近期用户与 Agent 的多条对话摘要。\n"
            "请从中发现跨会话的规律性知识或用户偏好，提炼为 3-5 条简洁的知识点。\n"
            "每条知识点一行，直接输出，不超过 80 字，中文。\n\n"
            f"对话摘要：\n{episodes_text}\n\n"
            "提炼的知识点："
        )

        try:
            result_text = await self._call_llm_simple(prompt, max_tokens=600)
        except Exception as e:
            logger.warning(f"[Dream/Phase4] LLM reflect failed: {e}")
            return {"reflected": 0}

        if not result_text:
            return {"reflected": 0}

        # 解析并保存知识点
        import uuid
        lines = [l.strip() for l in result_text.strip().split("\n") if l.strip()]
        saved = 0
        for line in lines[:5]:
            if len(line) < 10:
                continue
            from agent_core.knowledge.models import KnowledgeUnit
            unit = KnowledgeUnit(
                unit_id=str(uuid.uuid4()),
                category="deep_reflection",
                text=line,
                tags=["dream_reflection"],
                utility=0.6,
                confidence=0.7,
                ingestion_time=time.time(),
                valid_from=time.time(),
                created_at=time.time(),
                last_accessed=time.time(),
            )
            try:
                await self._knowledge_store.save_knowledge(
                    unit, user_id, instance_id, source_type="dream"
                )
                saved += 1
            except Exception:
                pass

        logger.info(f"[Dream/Phase4] user={user_id} reflected={saved}")
        return {"reflected": saved}

    # ── LLM 工具方法 ──────────────────────────────────────────────────────────

    async def _call_llm_simple(self, prompt: str, max_tokens: int = 400) -> str:
        """非流式调用 LLM，返回文本（使用 small_fast 模型降低成本）"""
        if not self._llm_call:
            return ""

        timeout = getattr(self._config, "dream_llm_timeout_seconds", 60)

        try:
            result = await self._llm_call(
                prompt,
                use_small_fast=True,
                max_tokens=max_tokens,
                timeout=float(timeout),
            )
            return result or ""
        except asyncio.TimeoutError:
            logger.warning("[Dream] LLM call timed out")
            return ""
        except Exception as e:
            logger.warning(f"[Dream] LLM call failed: {e}")
            return ""
