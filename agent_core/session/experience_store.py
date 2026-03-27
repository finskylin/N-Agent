"""
ExperienceStoreCore — 纯 SQLite 经验知识库

Core 版本：仅使用 SessionContextDB（SQLite），无 Redis/MySQL 依赖。
"""
import json
import time
from typing import Dict, List, Optional, Callable, Awaitable, TYPE_CHECKING
from loguru import logger

if TYPE_CHECKING:
    from .context_db import SessionContextDB


class ExperienceStoreCore:
    """用户经验库 — 纯 SQLite 版本"""

    DIMENSIONS = [
        "user_preferences", "stock_insights", "learned_patterns", "corrections",
        "user_knowledge", "system_knowledge",
    ]

    EMPTY_EXPERIENCE: Dict[str, list] = {
        "user_preferences": [],
        "stock_insights": [],
        "learned_patterns": [],
        "corrections": [],
        "user_knowledge": [],
        "system_knowledge": [],
    }

    DEFAULT_MAX_ITEMS = {
        "user_preferences": 15,
        "stock_insights": 30,
        "learned_patterns": 15,
        "corrections": 15,
        "user_knowledge": 50,
        "system_knowledge": 50,
    }

    GLOBAL_MAX_ITEMS = {
        "user_preferences": 30,
        "stock_insights": 50,
        "learned_patterns": 30,
        "corrections": 30,
        "user_knowledge": 100,
        "system_knowledge": 100,
    }

    def __init__(
        self,
        max_items: Optional[Dict[str, int]] = None,
        user_id: int = 1,
        instance_id: str = "default",
        sqlite_db: Optional["SessionContextDB"] = None,
        # 兼容 v4 ExperienceStore 参数（TTL 在 SQLite 模式下无效）
        ttl: int = 0,
        embedding_client=None,
        **kwargs,
    ):
        self._max_items = dict(self.DEFAULT_MAX_ITEMS)
        if max_items:
            self._max_items.update(max_items)
        self._user_id = user_id
        self._instance_id = instance_id
        self._sqlite = sqlite_db
        self._embedding_client = embedding_client

    @staticmethod
    def _normalize_item(item) -> Optional[dict]:
        """标准化经验条目为 v2 格式"""
        if isinstance(item, str):
            if not item.strip():
                return None
            return {"text": item, "score": 0.5, "created_ts": 0}
        elif isinstance(item, dict):
            text = item.get("text", "")
            if not text or not str(text).strip():
                return None
            return {
                "text": str(text),
                "score": float(item.get("score", 0.5)),
                "created_ts": int(item.get("created_ts", 0)),
            }
        return None

    async def get(self, session_id: str) -> Dict[str, list]:
        """获取用户经验"""
        if not self._sqlite:
            return {k: [] for k in self.DIMENSIONS}
        try:
            experience = await self._sqlite.get_experience(
                session_id, self._user_id, self._instance_id,
            )
            if experience:
                for dim in self.DIMENSIONS:
                    if dim not in experience:
                        experience[dim] = []
                return experience
        except Exception as e:
            logger.warning(f"[ExperienceStoreCore] get failed: {e}")
        return {k: [] for k in self.DIMENSIONS}

    async def save(self, session_id: str, experience: Dict[str, list]):
        """保存用户经验"""
        if not self._sqlite:
            return
        try:
            await self._sqlite.save_experience(
                session_id, self._user_id, self._instance_id, experience,
            )
        except Exception as e:
            logger.error(f"[ExperienceStoreCore] save failed: {e}")

    async def extract_and_save(
        self,
        session_id: str,
        user_msg: str,
        assistant_msg: str,
        extract_fn: Callable[[str, str], Awaitable[Dict[str, list]]],
    ):
        """每轮对话后提取经验"""
        try:
            new_experience = await extract_fn(user_msg, assistant_msg)
            if new_experience:
                await self._merge_experience(session_id, new_experience)
        except Exception as e:
            logger.error(f"[ExperienceStoreCore] extract failed: {e}")

    async def _merge_experience(self, session_id: str, new_exp: Dict[str, list]):
        """合并新经验到已有经验库"""
        existing = await self.get(session_id)
        now_ts = int(time.time())

        for key in self.DIMENSIONS:
            new_items = new_exp.get(key, [])
            if not new_items:
                continue

            existing_items = existing.get(key, [])
            existing_texts = {item["text"] for item in existing_items}

            for raw_item in new_items:
                norm = self._normalize_item(raw_item)
                if not norm:
                    continue
                if norm["created_ts"] == 0:
                    norm["created_ts"] = now_ts
                if norm["text"] not in existing_texts:
                    existing_items.append(norm)
                    existing_texts.add(norm["text"])

            max_items = self._max_items.get(key, 15)
            if len(existing_items) > max_items:
                existing_items.sort(
                    key=lambda x: (x.get("score", 0.5), x.get("created_ts", 0)),
                    reverse=True,
                )
                existing_items = existing_items[:max_items]

            existing[key] = existing_items

        await self.save(session_id, existing)
        logger.info(f"[ExperienceStoreCore] Merged experience for session {session_id}")

    async def promote_to_global(self, session_id: str):
        """将当前 session 的经验晋升到 user 级全局库（跨 session 共享）"""
        if not self._sqlite:
            return
        try:
            experience = await self.get(session_id)
            batch_items = []
            for dim in self.DIMENSIONS:
                items = experience.get(dim, [])
                for item in items:
                    if isinstance(item, dict):
                        text = item.get("text", "")
                        score = float(item.get("score", 0.5))
                    else:
                        text = str(item)
                        score = 0.5
                    if not text.strip():
                        continue
                    batch_items.append({
                        "dimension": dim,
                        "text": text,
                        "score": score,
                        "source_session": session_id,
                    })
            if batch_items:
                await self._sqlite.batch_upsert_user_experiences(
                    user_id=self._user_id,
                    instance_id=self._instance_id,
                    items=batch_items,
                    max_items_per_dim=self.GLOBAL_MAX_ITEMS,
                )
                # 后台为每条经验生成 embedding（不阻塞主流程）
                if self._embedding_client:
                    try:
                        from agent_core.background_task_manager import get_global_task_manager
                        for item in batch_items:
                            get_global_task_manager().create_task(
                                self._embed_and_update_experience(
                                    item["dimension"], item["text"]
                                ),
                                task_name=f"embed_exp_{hash(item['text']) & 0xFFFF:04x}",
                            )
                    except Exception as e:
                        logger.warning(f"[ExperienceStoreCore] Failed to schedule embed tasks: {e}")
            logger.debug(f"[ExperienceStoreCore] Promoted session {session_id} to global")
        except Exception as e:
            logger.warning(f"[ExperienceStoreCore] promote_to_global failed: {e}")

    async def get_global(
        self, dimensions: Optional[List[str]] = None
    ) -> Dict[str, list]:
        """获取 user 级全局经验（跨 session）"""
        if not self._sqlite:
            return {k: [] for k in self.DIMENSIONS}
        dims = dimensions or self.DIMENSIONS
        result: Dict[str, list] = {}
        try:
            for dim in dims:
                rows = await self._sqlite.get_user_experiences(
                    user_id=self._user_id,
                    instance_id=self._instance_id,
                    dimension=dim,
                    limit=self.GLOBAL_MAX_ITEMS.get(dim, 50),
                )
                result[dim] = [{"text": r["text"], "score": r["score"]} for r in rows]
        except Exception as e:
            logger.warning(f"[ExperienceStoreCore] get_global failed: {e}")
            return {k: [] for k in dims}
        return result

    async def _embed_and_update_experience(self, dimension: str, text: str):
        """后台任务：为经验条目生成 embedding 并写入 BLOB"""
        try:
            vec = await self._embedding_client.embed(text)
            if vec is not None:
                blob = self._embedding_client.to_blob(vec)
                await self._sqlite.set_user_experience_embedding(
                    self._user_id, self._instance_id, dimension, text, blob,
                )
        except Exception as e:
            logger.warning(f"[ExperienceStoreCore] embed_and_update_experience failed: {e}")

    async def get_global_semantic(
        self,
        query_vec,
        dimensions: Optional[List[str]] = None,
        top_k: int = 20,
        heat_weight: float = 0.1,
    ) -> Dict[str, list]:
        """
        语义向量召回全局经验（跨 session）。

        按余弦相似度排序，返回相关度最高的条目。
        无 embedding 的条目用 score 作 fallback。
        """
        if not self._sqlite:
            return {k: [] for k in (dimensions or self.DIMENSIONS)}

        try:
            import numpy as np
        except ImportError:
            return await self.get_global(dimensions=dimensions)

        dims = dimensions or self.DIMENSIONS
        try:
            rows = await self._sqlite.get_user_experiences_with_embedding(
                user_id=self._user_id,
                instance_id=self._instance_id,
                dimensions=dims,
            )
        except Exception as e:
            logger.warning(f"[ExperienceStoreCore] get_global_semantic fetch failed: {e}")
            return await self.get_global(dimensions=dimensions)

        if not rows:
            return {k: [] for k in dims}

        with_emb = [(r, r["text_embedding"]) for r in rows if r.get("text_embedding")]
        without_emb = [r for r in rows if not r.get("text_embedding")]

        scored = []

        if with_emb and self._embedding_client:
            try:
                blobs = [blob for _, blob in with_emb]
                vecs = np.stack([self._embedding_client.from_blob(b) for b in blobs])
                scores = self._embedding_client.cosine_batch(query_vec, vecs)
                max_score = max((r["score"] for r, _ in with_emb), default=1.0) or 1.0
                for i, (row, _) in enumerate(with_emb):
                    combined = float(scores[i]) + heat_weight * row["score"] / max_score
                    scored.append((combined, row))
            except Exception as e:
                logger.warning(f"[ExperienceStoreCore] cosine_batch failed: {e}")
                without_emb.extend([r for r, _ in with_emb])

        if without_emb:
            max_score = max((r["score"] for r in without_emb), default=1.0) or 1.0
            for row in without_emb:
                fallback = heat_weight * row["score"] / max_score
                scored.append((fallback, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        top_rows = [row for _, row in scored[:top_k]]

        # 按维度组织结果
        result: Dict[str, list] = {k: [] for k in dims}
        for row in top_rows:
            dim = row.get("dimension", "")
            if dim in result:
                result[dim].append({"text": row["text"], "score": row["score"]})
        return result

    async def clear(self, session_id: str):
        """清空经验"""
        if not self._sqlite:
            return
        await self.save(session_id, {k: [] for k in self.DIMENSIONS})
