"""
Mid-Term Memory (MTM) — 中期记忆管理

职责:
- 接收 ConversationHistory 摘要后的语义页面（STM→MTM 晋升）
- 基于热度公式进行记忆调度: H = α*N_visit + β*L_interaction + γ*exp(-Δh/τ)
- 主题 Jaccard 相似度 ≥ 0.6 时自动合并
- 超容量时 LFU 淘汰低热度页面
- 按主题/实体相关性召回记忆

存储: 通过 SessionContextDB 的 mtm_pages 表操作（不直接写 SQL）
"""
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from agent_core.session.context_db import SessionContextDB
    from agent_core.memory.embedding_client import EmbeddingClient


@dataclass
class MTMPage:
    """中期记忆页面"""
    page_id: str = ""
    session_id: str = ""
    summary: str = ""
    topics: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    msg_range_start: int = 0
    msg_range_end: int = 0
    interaction_length: int = 0
    visit_count: int = 1
    heat_score: float = 0.0
    created_at: int = 0
    last_access_at: int = 0

    def to_dict(self) -> Dict:
        return {
            "page_id": self.page_id,
            "session_id": self.session_id,
            "summary": self.summary,
            "topics": self.topics,
            "entities": self.entities,
            "msg_range_start": self.msg_range_start,
            "msg_range_end": self.msg_range_end,
            "interaction_length": self.interaction_length,
            "visit_count": self.visit_count,
            "heat_score": self.heat_score,
            "created_at": self.created_at,
            "last_access_at": self.last_access_at,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "MTMPage":
        return cls(
            page_id=d.get("page_id", ""),
            session_id=d.get("session_id", ""),
            summary=d.get("summary", ""),
            topics=d.get("topics", []),
            entities=d.get("entities", []),
            msg_range_start=d.get("msg_range_start", 0),
            msg_range_end=d.get("msg_range_end", 0),
            interaction_length=d.get("interaction_length", 0),
            visit_count=d.get("visit_count", 1),
            heat_score=d.get("heat_score", 0.0),
            created_at=d.get("created_at", 0),
            last_access_at=d.get("last_access_at", 0),
        )


class MidTermMemory:
    """
    中期记忆管理器

    通过 SessionContextDB 的 CRUD 方法操作 SQLite，不直接写 SQL。

    Args:
        sqlite_db: SessionContextDB 实例
        user_id: 用户 ID
        instance_id: 实例标识
        max_pages: 最大页面数
        alpha, beta, gamma, tau: 热度计算参数
    """

    def __init__(
        self,
        sqlite_db: "SessionContextDB",
        user_id: int,
        instance_id: str,
        max_pages: int = 200,
        alpha: float = 1.0,
        beta: float = 0.5,
        gamma: float = 2.0,
        tau: float = 168.0,
        embedding_client: Optional["EmbeddingClient"] = None,
    ):
        self._db = sqlite_db
        self._user_id = user_id
        self._instance_id = instance_id
        self._max_pages = max_pages
        self._alpha = alpha
        self._beta = beta
        self._gamma = gamma
        self._tau = tau  # 小时
        self._embedding_client = embedding_client

    def compute_heat(
        self,
        visit_count: int,
        interaction_length: int,
        last_access_ts: int,
    ) -> float:
        """
        计算热度分数: H = α*N_visit + β*L_interaction + γ*exp(-Δh/τ)

        Args:
            visit_count: 被召回次数
            interaction_length: 原始交互轮数
            last_access_ts: 最后访问时间戳（秒）
        """
        now = time.time()
        delta_hours = max((now - last_access_ts) / 3600.0, 0.0)
        recency = math.exp(-delta_hours / self._tau) if self._tau > 0 else 0.0
        return (
            self._alpha * visit_count
            + self._beta * interaction_length
            + self._gamma * recency
        )

    async def promote(
        self,
        session_id: str,
        summary: str,
        topics: List[str],
        entities: List[str],
        msg_range_start: int = 0,
        msg_range_end: int = 0,
        interaction_length: int = 0,
    ) -> str:
        """
        将 STM 摘要晋升为 MTM 页面

        先尝试主题合并（Jaccard ≥ 0.6），无法合并则创建新页面。
        晋升后检查容量，超限则淘汰。

        Returns:
            page_id（新建或合并后的页面 ID）
        """
        now = int(time.time())

        # 尝试合并到已有页面
        merged_id = await self._try_merge(
            summary, topics, entities, session_id,
        )
        if merged_id:
            logger.info(
                f"[MTM] Merged into existing page {merged_id} "
                f"for session {session_id}"
            )
            return merged_id

        # 创建新页面
        page_id = f"mtm_{uuid.uuid4().hex[:12]}"
        heat = self.compute_heat(1, interaction_length, now)

        page = MTMPage(
            page_id=page_id,
            session_id=session_id,
            summary=summary,
            topics=topics,
            entities=entities,
            msg_range_start=msg_range_start,
            msg_range_end=msg_range_end,
            interaction_length=interaction_length,
            visit_count=1,
            heat_score=heat,
            created_at=now,
            last_access_at=now,
        )

        page_dict = page.to_dict()

        # 生成 embedding（失败时 embedding_blob=None，降级到热度排序）
        if self._embedding_client and summary:
            try:
                vec = await self._embedding_client.embed(summary)
                page_dict["summary_embedding"] = (
                    self._embedding_client.to_blob(vec) if vec is not None else None
                )
            except Exception as e:
                logger.warning(f"[MTM] embed failed on promote: {e}")

        await self._db.save_mtm_page(
            self._user_id, self._instance_id, page_dict,
        )

        # 检查容量并淘汰
        await self._evict_if_needed()

        logger.info(
            f"[MTM] Promoted new page {page_id} "
            f"(topics={topics}, heat={heat:.2f})"
        )
        return page_id

    async def recall(
        self,
        query_topics: List[str] = None,
        query_entities: List[str] = None,
        top_k: int = 5,
    ) -> List[MTMPage]:
        """
        按主题/实体相关性召回 MTM 页面

        相关性评分: topic_overlap + entity_overlap + heat_score_normalized
        召回后自动 touch() 更新访问计数。
        """
        all_pages = await self._db.get_mtm_pages(
            self._user_id, self._instance_id, order_by_heat=True,
        )
        if not all_pages:
            return []

        query_topic_set = set(query_topics or [])
        query_entity_set = set(query_entities or [])

        scored = []
        for page_dict in all_pages:
            page = MTMPage.from_dict(page_dict)
            score = 0.0

            # 主题重叠
            if query_topic_set:
                page_topics = set(page.topics)
                overlap = len(query_topic_set & page_topics)
                union = len(query_topic_set | page_topics)
                if union > 0:
                    score += overlap / union * 3.0

            # 实体重叠
            if query_entity_set:
                page_entities = set(page.entities)
                overlap = len(query_entity_set & page_entities)
                if overlap > 0:
                    score += overlap * 2.0

            # 热度归一化（加权较小）
            score += page.heat_score * 0.1

            scored.append((score, page))

        # 排序并取 top_k
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [page for score, page in scored[:top_k] if score > 0]

        # touch 召回的页面
        if results:
            await self.touch([p.page_id for p in results])

        return results

    async def recall_semantic(
        self,
        query_vec: "np.ndarray",
        top_k: int = 5,
        heat_weight: float = 0.1,
    ) -> List[MTMPage]:
        """
        语义向量召回 — 主路径。

        1. 获取所有页面（含 embedding BLOB）
        2. 有 embedding 的页面用余弦相似度打分
        3. 无 embedding 的页面用热度归一化分作为 fallback
        4. 混合排序，返回 top_k，并 touch()
        """
        try:
            import numpy as np
        except ImportError:
            logger.warning("[MTM] numpy not available, falling back to heat-only recall")
            return await self.recall(top_k=top_k)

        all_pages = await self._db.get_mtm_pages_with_embedding(
            self._user_id, self._instance_id,
        )
        if not all_pages:
            return []

        # 分离有/无 embedding 的页面
        pages_with_emb = [(p, p["summary_embedding"]) for p in all_pages if p.get("summary_embedding")]
        pages_without_emb = [p for p in all_pages if not p.get("summary_embedding")]

        scored = []

        # 批量计算余弦相似度
        if pages_with_emb and self._embedding_client:
            blobs = [blob for _, blob in pages_with_emb]
            try:
                vecs = np.stack([
                    self._embedding_client.from_blob(b) for b in blobs
                ])  # shape=(N, dim)
                scores = self._embedding_client.cosine_batch(query_vec, vecs)  # shape=(N,)
                max_heat = max((p["heat_score"] for p, _ in pages_with_emb), default=1.0) or 1.0
                for i, (page_dict, _) in enumerate(pages_with_emb):
                    combined = float(scores[i]) + heat_weight * page_dict["heat_score"] / max_heat
                    scored.append((combined, MTMPage.from_dict(page_dict)))
            except Exception as e:
                logger.warning(f"[MTM] cosine_batch failed: {e}")
                # 降级：将这些页面加入无 embedding 组
                pages_without_emb.extend([p for p, _ in pages_with_emb])

        # 无 embedding 的页面用热度作为 fallback 分数（乘以 heat_weight 确保低于语义分）
        if pages_without_emb:
            max_heat = max((p["heat_score"] for p in pages_without_emb), default=1.0) or 1.0
            for page_dict in pages_without_emb:
                fallback_score = heat_weight * page_dict["heat_score"] / max_heat
                scored.append((fallback_score, MTMPage.from_dict(page_dict)))

        if not scored:
            return []

        scored.sort(key=lambda x: x[0], reverse=True)
        results = [page for score, page in scored[:top_k] if score > 0]

        if results:
            await self.touch([p.page_id for p in results])

        return results

    async def touch(self, page_ids: List[str]):
        """更新访问计数和热度"""
        now = int(time.time())
        for page_id in page_ids:
            page_dict = await self._db.get_mtm_page(
                page_id, self._user_id, self._instance_id,
            )
            if not page_dict:
                continue

            new_visit = page_dict["visit_count"] + 1
            new_heat = self.compute_heat(
                new_visit,
                page_dict["interaction_length"],
                now,
            )
            await self._db.update_mtm_page(
                page_id, self._user_id, self._instance_id,
                {
                    "visit_count": new_visit,
                    "heat_score": new_heat,
                    "last_access_at": now,
                },
            )

    async def evict(self) -> List[str]:
        """LFU 淘汰: 删除热度最低的页面直到不超容"""
        return await self._evict_if_needed()

    async def cleanup_expired(self, max_age_days: int) -> int:
        """清理过期页面"""
        max_age_seconds = max_age_days * 86400
        deleted = await self._db.delete_expired_mtm_pages(
            self._user_id, self._instance_id, max_age_seconds,
        )
        if deleted > 0:
            logger.info(
                f"[MTM] Cleaned up {deleted} expired pages "
                f"(max_age={max_age_days} days)"
            )
        return deleted

    async def _try_merge(
        self,
        summary: str,
        topics: List[str],
        entities: List[str],
        session_id: str,
    ) -> Optional[str]:
        """
        尝试与已有页面合并

        条件: 主题 Jaccard 相似度 ≥ 0.6
        合并方式: 摘要拼接、主题/实体并集、交互长度累加
        """
        if not topics:
            return None

        new_topic_set = set(topics)
        existing_pages = await self._db.get_mtm_pages(
            self._user_id, self._instance_id,
        )

        best_match = None
        best_jaccard = 0.0

        for page_dict in existing_pages:
            existing_topics = set(page_dict.get("topics", []))
            if not existing_topics:
                continue
            intersection = len(new_topic_set & existing_topics)
            union = len(new_topic_set | existing_topics)
            jaccard = intersection / union if union > 0 else 0.0

            if jaccard >= 0.6 and jaccard > best_jaccard:
                best_jaccard = jaccard
                best_match = page_dict

        if not best_match:
            return None

        # 执行合并
        now = int(time.time())
        merged_topics = list(set(best_match.get("topics", [])) | new_topic_set)
        merged_entities = list(
            set(best_match.get("entities", [])) | set(entities)
        )
        merged_summary = (
            f"{best_match.get('summary', '')}\n---\n{summary}"
        )
        new_visit = best_match["visit_count"] + 1
        new_heat = self.compute_heat(
            new_visit,
            best_match["interaction_length"] + 1,
            now,
        )

        updates = {
            "summary": merged_summary,
            "topics": merged_topics,
            "entities": merged_entities,
            "visit_count": new_visit,
            "heat_score": new_heat,
            "last_access_at": now,
            "interaction_length": best_match["interaction_length"] + 1,
        }

        # 合并摘要后重新生成 embedding
        if self._embedding_client and merged_summary:
            try:
                vec = await self._embedding_client.embed(merged_summary)
                if vec is not None:
                    updates["summary_embedding"] = self._embedding_client.to_blob(vec)
            except Exception as e:
                logger.warning(f"[MTM] embed failed on merge: {e}")

        await self._db.update_mtm_page(
            best_match["page_id"], self._user_id, self._instance_id,
            updates,
        )
        return best_match["page_id"]

    async def _evict_if_needed(self) -> List[str]:
        """超容时淘汰低热度页面"""
        count = await self._db.count_mtm_pages(
            self._user_id, self._instance_id,
        )
        if count <= self._max_pages:
            return []

        # 获取所有页面按热度排序（降序），淘汰尾部
        all_pages = await self._db.get_mtm_pages(
            self._user_id, self._instance_id, order_by_heat=True,
        )
        evict_count = count - self._max_pages
        evict_ids = [
            p["page_id"] for p in all_pages[-evict_count:]
        ]

        if evict_ids:
            await self._db.delete_mtm_pages(
                evict_ids, self._user_id, self._instance_id,
            )
            logger.info(
                f"[MTM] Evicted {len(evict_ids)} low-heat pages "
                f"(capacity={self._max_pages})"
            )
        return evict_ids
