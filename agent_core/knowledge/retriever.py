"""
Knowledge Retriever — 知识检索器

检索知识并格式化为 prompt 注入文本。
评分: score = w_tag * tag_overlap + w_utility * utility + w_heat * heat_score。
支持时序查询（as_of_time）和认知变迁注入。
"""
import json
import time
from typing import List, Optional, Dict

from loguru import logger

from .models import KnowledgeUnit, HeatScoreConfig


class KnowledgeRetriever:
    """知识检索器 — 检索 + 格式化为 prompt"""

    def __init__(self, store, config: dict, graph_retriever=None, prediction_store=None):
        self._store = store
        self._config = config.get("retriever", {})
        self._heat_config = HeatScoreConfig.from_config(config)
        self._graph_retriever = graph_retriever  # GraphRetriever（可选）
        self._prediction_store = prediction_store  # PredictionStore（可选）

    async def retrieve_for_prompt(
        self, user_id: int, instance_id: str,
        query_tags: List[str],
        token_budget: int = 0,
        as_of_time: Optional[float] = None,
        query_vec=None,
    ) -> str:
        """
        检索知识并格式化为可注入 prompt 的文本。

        Args:
            query_tags: 检索标签（无 query_vec 时使用）
            token_budget: token 预算（0 = 使用配置默认值）
            as_of_time: 历史时间点（Graphiti 风格追溯查询）
            query_vec: 查询向量（优先走语义路径）

        Returns:
            格式化的知识上下文文本
        """
        budget = token_budget or self._config.get("default_token_budget", 2000)
        top_k = self._config.get("default_top_k", 10)

        # 优先走语义向量路径
        if query_vec is not None and hasattr(self._store, "retrieve_semantic"):
            try:
                units = await self._store.retrieve_semantic(
                    query_vec, user_id, instance_id, top_k=top_k,
                )
                logger.debug(f"[KnowledgeRetriever] semantic path, top={len(units)}")
            except Exception as e:
                logger.warning(f"[KnowledgeRetriever] semantic retrieval failed, fallback: {e}")
                units = await self._store.retrieve(
                    user_id, instance_id, query_tags,
                    top_k=top_k, as_of_time=as_of_time,
                )
        else:
            # 降级路径：tag 匹配
            units = await self._store.retrieve(
                user_id, instance_id, query_tags,
                top_k=top_k, as_of_time=as_of_time,
            )

        if not units:
            return ""

        # 格式化
        parts = ["\n## 知识引擎上下文"]

        # 分类整理
        by_category: Dict[str, List[KnowledgeUnit]] = {}
        for u in units:
            by_category.setdefault(u.category, []).append(u)

        category_labels = {
            "skill_insight": "Skill 使用经验",
            "domain_fact": "领域知识",
            "strategy_rule": "策略规则",
            "user_cognition": "用户认知",
        }

        char_count = 0
        for cat, cat_units in by_category.items():
            label = category_labels.get(cat, cat)
            section = f"\n### {label}"
            parts.append(section)
            char_count += len(section)

            for u in cat_units:
                line = f"- {u.text}"
                if u.tags:
                    line += f" [标签: {', '.join(u.tags[:3])}]"
                if char_count + len(line) > budget:
                    break
                parts.append(line)
                char_count += len(line)

                # 更新访问计数
                await self._store.update_knowledge_field(u.unit_id, {
                    "access_count": u.access_count + 1,
                    "last_accessed": time.time(),
                })

            if char_count > budget:
                break

        # 可选: 注入最近认知变迁
        if self._config.get("inject_cognition_changes", True):
            max_changes = self._config.get("max_cognition_changes_in_prompt", 3)
            changes_text = await self._format_cognition_changes(
                user_id, instance_id, max_changes, budget - char_count,
            )
            if changes_text:
                parts.append(changes_text)

        # 检索用户偏好
        prefs_text = await self._format_preferences(user_id, instance_id, budget - char_count)
        if prefs_text:
            parts.append(prefs_text)

        # 图谱增强（opt-in，graph_retriever=None 时跳过）
        if self._graph_retriever and (units or query_tags):
            try:
                unit_ids = [u.unit_id for u in units] if units else []
                subgraph_text = await self._graph_retriever.subgraph_for_prompt(
                    unit_ids=unit_ids,
                    user_id=user_id,
                    instance_id=instance_id,
                    query_tags=query_tags,
                )
                if subgraph_text:
                    parts.append(f"\n{subgraph_text}")
            except Exception as e:
                logger.warning(f"[KnowledgeRetriever] Graph retrieval failed: {e}")

        # 历史预测记录注入（opt-in，prediction_store=None 时跳过）
        if self._prediction_store and char_count < budget:
            try:
                # 尝试从 query_tags 提取 subject 关键词（取第一个非通用 tag）
                subject_hint = None
                if query_tags:
                    subject_hint = query_tags[0] if len(query_tags[0]) > 1 else None
                pred_text = await self._prediction_store.get_context_for_prompt(
                    user_id=int(user_id) if str(user_id).isdigit() else user_id,
                    instance_id=instance_id,
                    subject=subject_hint,
                )
                if pred_text:
                    parts.append(pred_text)
            except Exception as e:
                logger.debug(f"[KnowledgeRetriever] Prediction context failed (non-fatal): {e}")

        return "\n".join(parts)

    async def _format_cognition_changes(
        self, user_id: int, instance_id: str,
        limit: int, remaining_budget: int,
    ) -> str:
        """格式化最近认知变迁"""
        if remaining_budget < 100:
            return ""

        changes = await self._store.get_recent_cognition_changes(
            user_id, instance_id, limit=limit,
        )
        if not changes:
            return ""

        lines = ["\n### 近期认知变化"]
        for c in changes:
            old_text = c.get("old_text", "")
            new_text = c.get("new_text", "")
            reason = c.get("reason", "")
            if old_text and new_text:
                line = f"- 认知更新: 「{old_text[:30]}」→「{new_text[:30]}」({reason[:20]})"
                lines.append(line)

        return "\n".join(lines) if len(lines) > 1 else ""

    async def _format_preferences(
        self, user_id: int, instance_id: str, remaining_budget: int,
    ) -> str:
        """格式化用户偏好"""
        if remaining_budget < 50:
            return ""

        prefs = await self._store.get_preferences(user_id, instance_id)
        # 只注入高置信度偏好
        stable_prefs = [p for p in prefs if p.confidence >= 0.7]
        if not stable_prefs:
            return ""

        lines = ["\n### 用户偏好"]
        for p in stable_prefs[:5]:
            lines.append(f"- {p.dimension}: {p.value}")

        return "\n".join(lines)
