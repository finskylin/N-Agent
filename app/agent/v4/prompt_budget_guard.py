"""
Prompt Budget Guard -- 提示词预算控制

职责:
1. 根据配置限制注入 System Prompt 的 Skill 数量和总字符数
2. Phase 0 推荐的 Skill 享有保护预算（默认 30%）
3. 每个 category 至少保证最低覆盖
4. 剩余空间按 priority 降序填充
5. 单 Skill 描述超限时自动截断

配置来源: app/config/skill_framework.json → prompt_budget
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from loguru import logger


@dataclass
class BudgetAllocation:
    """预算分配结果"""
    selected_skills: list          # 被选中的 SkillMetadata 列表
    excluded_skills: list          # 被排除的 SkillMetadata 列表
    total_chars_used: int          # 已用字符数
    total_chars_budget: int        # 总字符预算
    truncated_count: int           # 被截断描述的 Skill 数量


class PromptBudgetGuard:
    """
    提示词预算控制器

    根据配置限制注入 System Prompt 的 Skill 数量和总字符数。

    预算分配策略:
    1. Phase 0 推荐 Skill → 保护预算 (default 30%)
    2. 每个 category 至少 category_min_coverage 个 Skill
    3. 剩余空间按 priority 降序填充

    用法:
        guard = PromptBudgetGuard(config)
        allocation = guard.select_skills_for_prompt(
            all_skills, phase0_intents, phase0_topics
        )
    """

    def __init__(self, config: Optional[Dict] = None):
        config = config or {}
        self._max_skills = config.get("max_skills", 80)
        self._max_total_chars = config.get("max_total_chars", 40000)
        self._max_single_skill_chars = config.get("max_single_skill_chars", 2000)
        self._phase0_protected_ratio = config.get("phase0_protected_ratio", 0.3)
        self._category_min_coverage = config.get("category_min_coverage", 1)

    def select_skills_for_prompt(
        self,
        all_skills: list,
        phase0_intents: Optional[List[str]] = None,
        phase0_topics: Optional[List[str]] = None,
    ) -> BudgetAllocation:
        """
        根据预算选择注入 prompt 的 Skill

        Args:
            all_skills: 所有可用 SkillMetadata 列表
            phase0_intents: Phase 0 推荐的工具名列表
            phase0_topics: Phase 0 识别的主题列表

        Returns:
            BudgetAllocation
        """
        if not all_skills:
            return BudgetAllocation(
                selected_skills=[],
                excluded_skills=[],
                total_chars_used=0,
                total_chars_budget=self._max_total_chars,
                truncated_count=0,
            )

        # 如果全部在预算内，直接返回
        total_chars = sum(self._get_skill_chars(s) for s in all_skills)
        if len(all_skills) <= self._max_skills and total_chars <= self._max_total_chars:
            return BudgetAllocation(
                selected_skills=list(all_skills),
                excluded_skills=[],
                total_chars_used=total_chars,
                total_chars_budget=self._max_total_chars,
                truncated_count=0,
            )

        selected: List = []
        selected_names: Set[str] = set()
        chars_used = 0
        truncated_count = 0

        # Step 1: Phase 0 推荐 Skill → 保护预算
        phase0_skills = self._match_phase0_skills(
            all_skills, phase0_intents, phase0_topics
        )
        protected_chars = int(self._max_total_chars * self._phase0_protected_ratio)
        phase0_chars_used = 0

        for skill in phase0_skills:
            if len(selected) >= self._max_skills:
                break
            skill_chars = self._get_skill_chars(skill)
            if phase0_chars_used + skill_chars <= protected_chars:
                selected.append(skill)
                selected_names.add(skill.name)
                phase0_chars_used += skill_chars
                chars_used += skill_chars

        # Step 2: category 最低覆盖
        categories: Dict[str, List] = {}
        for s in all_skills:
            cat = getattr(s, "category", "general")
            categories.setdefault(cat, []).append(s)

        for cat, skills_in_cat in categories.items():
            covered = sum(1 for s in skills_in_cat if s.name in selected_names)
            need = max(0, self._category_min_coverage - covered)
            if need <= 0:
                continue

            # 按 priority 降序选取
            candidates = sorted(
                [s for s in skills_in_cat if s.name not in selected_names],
                key=lambda s: getattr(s, "priority", 50),
                reverse=True,
            )
            for skill in candidates[:need]:
                if len(selected) >= self._max_skills:
                    break
                skill_chars = self._get_skill_chars(skill)
                if chars_used + skill_chars <= self._max_total_chars:
                    selected.append(skill)
                    selected_names.add(skill.name)
                    chars_used += skill_chars

        # Step 3: 剩余空间按 priority 降序填满
        remaining = sorted(
            [s for s in all_skills if s.name not in selected_names],
            key=lambda s: getattr(s, "priority", 50),
            reverse=True,
        )

        for skill in remaining:
            if len(selected) >= self._max_skills:
                break
            skill_chars = self._get_skill_chars(skill)
            if chars_used + skill_chars <= self._max_total_chars:
                selected.append(skill)
                selected_names.add(skill.name)
                chars_used += skill_chars

        excluded = [s for s in all_skills if s.name not in selected_names]

        if excluded:
            logger.info(
                f"[PromptBudgetGuard] Budget: {len(selected)}/{len(all_skills)} skills, "
                f"{chars_used}/{self._max_total_chars} chars, "
                f"excluded {len(excluded)}"
            )

        return BudgetAllocation(
            selected_skills=selected,
            excluded_skills=excluded,
            total_chars_used=chars_used,
            total_chars_budget=self._max_total_chars,
            truncated_count=truncated_count,
        )

    def truncate_skill_description(self, skill) -> str:
        """
        截断 Skill 描述到预算限制内

        Args:
            skill: SkillMetadata 实例

        Returns:
            截断后的描述文本
        """
        desc = getattr(skill, "description", "") or ""
        if len(desc) <= self._max_single_skill_chars:
            return desc
        return desc[:self._max_single_skill_chars] + "..."

    def _match_phase0_skills(
        self,
        all_skills: list,
        phase0_intents: Optional[List[str]],
        phase0_topics: Optional[List[str]],
    ) -> list:
        """匹配 Phase 0 推荐的 Skill"""
        if not phase0_intents and not phase0_topics:
            return []

        intent_set = set(phase0_intents or [])
        topic_set = set(t.lower() for t in (phase0_topics or []))

        matched = []
        for skill in all_skills:
            name = getattr(skill, "name", "")
            # 直接名称匹配
            if name in intent_set:
                matched.append(skill)
                continue
            # 关键词匹配
            keywords = getattr(skill, "keywords", []) or []
            for kw in keywords:
                if kw.lower() in topic_set:
                    matched.append(skill)
                    break

        return matched

    @staticmethod
    def _get_skill_chars(skill) -> int:
        """计算单个 Skill 注入 prompt 的字符数"""
        name = getattr(skill, "name", "")
        display = getattr(skill, "display_name", "")
        desc = getattr(skill, "description", "") or ""
        category = getattr(skill, "category", "")
        return len(name) + len(display) + len(desc) + len(category) + 50  # 固定格式开销
