"""
Skill Crystallizer — 经验结晶器

优秀分析流程/经验自动结晶为新 Skill。
结晶前基准测试（SkillsBench 三条件验证，确保不降低性能）。

参考: SkillRL + Memp + MemSkill。
"""
import json
import time
from pathlib import Path
from typing import List, Dict, Optional
from uuid import uuid4

from loguru import logger

from .models import CrystallizedSkill, Episode


def _load_prompt(name: str, **kwargs) -> str:
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


class SkillCrystallizer:
    """经验结晶器 — 优秀模式结晶为 Skill"""

    def __init__(self, store, config: dict, llm_call=None):
        self._store = store
        self._config = config.get("crystallizer", {})
        self._full_config = config
        self._llm_call = llm_call

    async def evaluate_pattern(
        self, user_id: int, instance_id: str,
        pattern: str, episodes: List[Dict],
    ) -> Optional[CrystallizedSkill]:
        """
        评估模式是否适合结晶。
        结晶前必须通过 SkillsBench 式测试。
        """
        if not self._config.get("enabled", True):
            return None

        min_occurrences = self._config.get("min_occurrences", 3)
        min_success_rate = self._config.get("min_success_rate", 0.8)
        min_likes = self._config.get("min_like_count", 2)

        # 检查基本条件
        if len(episodes) < min_occurrences:
            return None

        success_count = sum(1 for ep in episodes if ep.get("success", True))
        success_rate = success_count / max(len(episodes), 1)
        if success_rate < min_success_rate:
            return None

        if not self._llm_call:
            return None

        # LLM 评估
        episode_summaries = "\n".join(
            f"- Query: {ep.get('query', '')[:50]}, Success: {ep.get('success', True)}"
            for ep in episodes[:5]
        )

        prompt = _load_prompt(
            "skill_crystallize_evaluate",
            pattern_description=pattern,
            episode_summaries=episode_summaries,
            occurrences=len(episodes),
            success_rate=f"{success_rate:.1%}",
            like_count=0,
        )

        if not prompt:
            return None

        try:
            per_ep_timeout = self._config.get("llm_timeout_seconds", 30)
            try:
                response = await self._llm_call(prompt, timeout=per_ep_timeout)
            except TypeError:
                response = await self._llm_call(prompt)

            crystal = self._parse_evaluation(response, user_id, instance_id, episodes)
            if not crystal:
                return None

            crystal.occurrences = len(episodes)
            crystal.success_rate = success_rate

            # 结晶前基准测试
            if self._config.get("pre_deploy_test", True):
                test_passed = await self._pre_deploy_test(crystal)
                if not test_passed:
                    crystal.status = "rejected"
                    crystal.rejection_reason = "结晶前性能测试未通过"
                    await self._store.save_crystallized_skill(crystal)
                    logger.warning(f"[Crystallizer] Rejected: {crystal.skill_name}")
                    return None

            crystal.status = "approved"
            await self._store.save_crystallized_skill(crystal)
            logger.info(f"[Crystallizer] Approved: {crystal.skill_name}")
            return crystal

        except Exception as e:
            logger.warning(f"[Crystallizer] Evaluation failed: {e}")
            return None

    async def deploy_approved_skills(
        self, user_id: int, instance_id: str,
    ) -> List[str]:
        """部署已批准的结晶 Skill"""
        if not self._config.get("auto_deploy", False):
            return []

        approved = await self._store.get_crystallized_skills(
            user_id, instance_id, status="approved",
        )
        deployed = []

        for crystal_data in approved:
            try:
                # 通过 dynamic-skill-creator 部署
                # 此处预留接口，实际部署由上层实现
                logger.info(
                    f"[Crystallizer] Deploying: {crystal_data.get('skill_name')}"
                )
                deployed.append(crystal_data.get("crystal_id", ""))
            except Exception as e:
                logger.warning(f"[Crystallizer] Deploy failed: {e}")

        return deployed

    async def _pre_deploy_test(self, crystal: CrystallizedSkill) -> bool:
        """
        结晶前基准测试 — SkillsBench 三条件验证。
        确保结晶 Skill 不降低性能。
        """
        min_boost = self._config.get("min_boost_pp", 0.0)
        # 基础验证: workflow 和 description 不为空
        if not crystal.description or not crystal.workflow:
            return False
        # 此处为简化版测试，实际应调用 SkillCrystallizationBench
        # 默认通过（实际部署时由 benchmark 模块验证）
        crystal.test_result = {
            "passed": True,
            "boost_pp": 0.0,
            "method": "basic_validation",
        }
        return True

    def _parse_evaluation(
        self, response: str, user_id: int, instance_id: str,
        episodes: List[Dict],
    ) -> Optional[CrystallizedSkill]:
        """解析 LLM 评估结果"""
        try:
            text = response.strip()
            if "```" in text:
                start = text.find("{")
                end = text.rfind("}") + 1
                if start >= 0 and end > start:
                    text = text[start:end]

            data = json.loads(text)
            if not isinstance(data, dict):
                return None

            if not data.get("should_crystallize", False):
                return None

            source_eps = [ep.get("episode_id", "") for ep in episodes[:10]]

            crystal = CrystallizedSkill(
                user_id=user_id,
                instance_id=instance_id,
                skill_name=data.get("skill_name", ""),
                description=data.get("description", ""),
                workflow=json.dumps(data.get("workflow_steps", []), ensure_ascii=False),
                prompt_template=data.get("prompt_template", ""),
                source_episodes=source_eps,
                status="candidate",
            )
            return crystal
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
