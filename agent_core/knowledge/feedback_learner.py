"""
Feedback Learner — 用户反馈学习器

从 like/dislike/comment 中学习:
- like → 知识强化 (utility + reinforce_delta)
- dislike → 知识衰减 (utility - decay_delta)
- comment → LLM 偏好蒸馏 → PreferenceUnit

参考: PRELUDE/CIPHER 偏好学习 + Live-Evo 强化/衰减。
"""
import json
import time
from pathlib import Path
from typing import List, Optional, Dict
from uuid import uuid4

from loguru import logger

from .models import KnowledgeUnit, PreferenceUnit, CognitionChange, Episode


def _load_prompt(name: str, **kwargs) -> str:
    """加载提示词模板"""
    try:
        from agent_core.prompts.loader import load_prompt
        return load_prompt(name, **kwargs)
    except Exception:
        try:
            p = Path(__file__).parent.parent.parent / "app" / "prompts" / f"{name}.md"
            if p.exists():
                text = p.read_text(encoding="utf-8")
                if kwargs:
                    try:
                        return text.format(**kwargs)
                    except KeyError:
                        return text
                return text
        except Exception:
            pass
    return ""


class FeedbackLearner:
    """用户反馈学习器"""

    def __init__(self, store, config: dict, llm_call=None):
        self._store = store
        self._config = config.get("feedback_learner", {})
        self._llm_call = llm_call

    async def learn_from_feedback(
        self, feedback_type: str, feedback_data: dict,
        episode: Optional[Episode], user_id: int, instance_id: str,
    ):
        """
        从反馈中学习。
        feedback_type: like / dislike / comment
        """
        if not self._config.get("enabled", True):
            return

        if feedback_type == "like":
            await self._handle_like(episode, user_id, instance_id)
        elif feedback_type == "dislike":
            await self._handle_dislike(episode, user_id, instance_id)
        elif feedback_type == "comment":
            comment = feedback_data.get("comment", "")
            if comment:
                await self._handle_comment(
                    comment, episode, user_id, instance_id,
                )

    async def _handle_like(
        self, episode: Optional[Episode], user_id: int, instance_id: str,
    ):
        """
        like → 知识强化 + SkillProfile 满意度提升。
        Live-Evo 启发: utility + reinforce_delta。
        """
        reinforce_delta = self._config.get("reinforce_delta", 0.1)

        # 强化相关知识
        if episode:
            tags = self._extract_tags_from_episode(episode)
            related = await self._store.retrieve(user_id, instance_id, tags, top_k=5)
            for unit in related:
                new_utility = min(1.0, unit.utility + reinforce_delta)
                await self._store.update_knowledge_field(unit.unit_id, {
                    "utility": new_utility,
                    "feedback_reinforcements": unit.feedback_reinforcements + 1,
                    "last_accessed": time.time(),
                })

                # 记录认知变迁
                change = CognitionChange(
                    old_unit_id=unit.unit_id,
                    new_unit_id=unit.unit_id,
                    reason="用户 like 反馈强化",
                    change_type="reinforcement",
                    user_id=user_id,
                    instance_id=instance_id,
                    confidence_delta=reinforce_delta,
                )
                await self._store.save_cognition_change(change)

            # 更新 Skill 满意度
            for se in episode.skill_executions:
                await self._store.update_skill_satisfaction(
                    se.skill_name, user_id, instance_id, like=True,
                )

        logger.info(f"[FeedbackLearner] Like processed, user={user_id}")

    async def _handle_dislike(
        self, episode: Optional[Episode], user_id: int, instance_id: str,
    ):
        """
        dislike → 知识衰减 + SkillProfile 满意度降低。
        """
        decay_delta = self._config.get("decay_delta", 0.15)

        if episode:
            tags = self._extract_tags_from_episode(episode)
            related = await self._store.retrieve(user_id, instance_id, tags, top_k=5)
            for unit in related:
                new_utility = max(0.0, unit.utility - decay_delta)
                await self._store.update_knowledge_field(unit.unit_id, {
                    "utility": new_utility,
                    "feedback_decays": unit.feedback_decays + 1,
                    "last_accessed": time.time(),
                })

                change = CognitionChange(
                    old_unit_id=unit.unit_id,
                    new_unit_id=unit.unit_id,
                    reason="用户 dislike 反馈衰减",
                    change_type="decay",
                    user_id=user_id,
                    instance_id=instance_id,
                    confidence_delta=-decay_delta,
                )
                await self._store.save_cognition_change(change)

            for se in episode.skill_executions:
                await self._store.update_skill_satisfaction(
                    se.skill_name, user_id, instance_id, like=False,
                )

        logger.info(f"[FeedbackLearner] Dislike processed, user={user_id}")

    async def _handle_comment(
        self, comment: str, episode: Optional[Episode],
        user_id: int, instance_id: str,
    ):
        """
        comment → LLM 偏好蒸馏 → PreferenceUnit。
        PRELUDE/CIPHER 启发。
        """
        if not self._llm_call:
            logger.debug("[FeedbackLearner] No LLM call, skipping comment learning")
            return

        dimensions = self._config.get(
            "preference_dimensions",
            ["style", "depth", "format", "topic_interest", "risk_tolerance"],
        )

        session_context = ""
        if episode:
            session_context = f"Query: {episode.query}"

        prompt = _load_prompt(
            "feedback_preference_distill",
            user_comment=comment,
            session_context=session_context,
            preference_dimensions=", ".join(dimensions),
        )

        if not prompt:
            return

        try:
            per_ep_timeout = self._config.get("llm_timeout_seconds", 30)
            try:
                response = await self._llm_call(prompt, timeout=per_ep_timeout)
            except TypeError:
                response = await self._llm_call(prompt)

            prefs = self._parse_preferences(response, user_id, instance_id, episode)
            for pref in prefs:
                await self._store.save_preference(pref)

            logger.info(f"[FeedbackLearner] Comment processed, extracted {len(prefs)} preferences")
        except Exception as e:
            logger.warning(f"[FeedbackLearner] Comment learning failed: {e}")

    def _parse_preferences(
        self, response: str, user_id: int, instance_id: str,
        episode: Optional[Episode],
    ) -> List[PreferenceUnit]:
        """解析 LLM 偏好输出"""
        try:
            text = response.strip()
            if "```" in text:
                start = text.find("[")
                end = text.rfind("]") + 1
                if start >= 0 and end > start:
                    text = text[start:end]

            items = json.loads(text)
            if not isinstance(items, list):
                return []

            dimensions = self._config.get(
                "preference_dimensions",
                ["style", "depth", "format", "topic_interest", "risk_tolerance"],
            )

            prefs = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                dim = item.get("dimension", "")
                if dim not in dimensions:
                    continue
                pref = PreferenceUnit(
                    user_id=user_id,
                    instance_id=instance_id,
                    dimension=dim,
                    value=str(item.get("value", "")),
                    confidence=min(1.0, max(0.0, float(item.get("confidence", 0.5)))),
                    source_episode_id=episode.episode_id if episode else "",
                )
                prefs.append(pref)
            return prefs
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning(f"[FeedbackLearner] Parse preferences failed: {e}")
            return []

    @staticmethod
    def _extract_tags_from_episode(episode: Episode) -> List[str]:
        """从 Episode 提取检索标签"""
        tags = []
        for se in episode.skill_executions:
            tags.append(se.skill_name)
        # 从 query 提取关键词
        if episode.query:
            words = episode.query.split()[:5]
            tags.extend(words)
        return tags
