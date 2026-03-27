"""
SkillEvolver -- Skill Prompt 进化器 (DGM Patch 机制)

统计各 skill 错误率 -> LLM 生成 SKILL.md description/examples 改进 ->
benchmark before/after 验证 -> 通过才写入。

安全约束:
- 只改 SKILL.md 的 description 和 examples，不改 Python 脚本
- after_score >= before_score - 0.02 才合并
- 每个 skill 每 24h 最多 patch 一次
- 默认 skill_evolution_enabled=False

参考: Darwin Godel Machine (DGM) -- Sakana AI
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from loguru import logger


class SkillEvolver:
    """Skill Prompt 进化器"""

    _PATCH_PROMPT = """\
你是一个 Skill 描述优化专家。以下 Skill 近期出现了较高的调用失败率。

Skill 名称: {skill_name}
当前 description:
{current_description}

当前 examples (如果有):
{current_examples}

近期失败案例 ({error_count} 条):
{failure_cases_text}

请分析失败原因，并优化 description 和 examples，使 LLM 更准确地理解何时/如何调用此工具。

要求:
1. 保持原始功能描述的核心含义
2. 补充容易遗漏的参数说明、使用条件、输出格式说明
3. 增加典型的调用示例（如果缺失）
4. 不要改变工具的 input_schema

输出 JSON（不要有其他文字）:
{{
  "new_description": "优化后的 description（保持中文）",
  "new_examples": "补充的示例说明（可选，为空则保持原样）",
  "summary": "本次优化的摘要（一句话）"
}}
"""

    _BENCHMARK_PROMPT = """\
你是一个工具调用评估器。给定用户问题和可用工具描述，判断应该调用哪个工具。

工具描述:
{tool_description}

用户问题: {user_query}
预期调用工具: {expected_tool}

这个工具描述是否能帮助 LLM 正确决定调用此工具？
仅回答 YES 或 NO。
"""

    def __init__(
        self,
        knowledge_store,
        prediction_store,
        skills_dir: str,
        llm_call: Callable,
        config: dict = None,
        get_skill_error_stats: Callable = None,
    ):
        """
        Args:
            knowledge_store: KnowledgeStore 实例
            prediction_store: PredictionStore 实例
            skills_dir: .claude/skills/ 目录路径
            llm_call: LLM 调用函数
            config: V4Config 或 dict
            get_skill_error_stats: app 层注入的回调
                签名: async (window_days: int) -> List[Dict]
                返回: [{"skill_name", "total_calls", "error_count", "error_rate", "recent_errors"}]
        """
        self._knowledge_store = knowledge_store
        self._pred_store = prediction_store
        self._skills_dir = skills_dir
        self._llm_call = llm_call
        self._get_skill_error_stats = get_skill_error_stats

        cfg = config or {}
        self._enabled = cfg.get("skill_evolution_enabled", False)
        self._error_threshold = cfg.get("skill_evolution_error_threshold", 0.3)
        self._min_calls = cfg.get("skill_evolution_min_calls", 5)
        self._window_days = cfg.get("skill_evolution_window_days", 7)
        self._patch_per_day = cfg.get("skill_evolution_patch_per_day", 1)

        # 记录每个 skill 最后一次 patch 时间，防止频繁触发
        self._last_patch_ts: Dict[str, float] = {}

    async def collect_failing_skills(self) -> List[Dict]:
        """通过注入回调获取高错误率 skill 列表"""
        if not self._get_skill_error_stats:
            logger.debug("[SkillEvolver] No get_skill_error_stats callback, skip")
            return []

        try:
            all_stats = await self._get_skill_error_stats(self._window_days)
        except Exception as e:
            logger.warning(f"[SkillEvolver] get_skill_error_stats failed: {e}")
            return []

        failing = []
        now = time.time()
        cooldown = 86400 / max(self._patch_per_day, 1)

        for stat in all_stats:
            name = stat.get("skill_name", "")
            total = stat.get("total_calls", 0)
            error_rate = stat.get("error_rate", 0.0)

            if total < self._min_calls or error_rate < self._error_threshold:
                continue

            # 冷却检查
            last_ts = self._last_patch_ts.get(name, 0)
            if now - last_ts < cooldown:
                continue

            failing.append(stat)

        logger.info(f"[SkillEvolver] Found {len(failing)} failing skills")
        return failing

    async def generate_patch(
        self, skill_name: str, skill_md_path: str, failure_cases: List[str],
    ) -> Optional[Dict]:
        """
        LLM 生成 description/examples 改进建议。
        返回: {"new_description": str, "new_examples": str, "summary": str}
        """
        current = self._read_skill_md(skill_md_path)
        if not current:
            return None

        cases_text = "\n".join(f"- {c[:150]}" for c in failure_cases[:10])

        prompt = self._PATCH_PROMPT.format(
            skill_name=skill_name,
            current_description=current.get("description", "")[:500],
            current_examples=current.get("examples", "(无)")[:300],
            error_count=len(failure_cases),
            failure_cases_text=cases_text,
        )

        try:
            import asyncio
            text = await asyncio.wait_for(
                self._llm_call(prompt, use_small_fast=False, max_tokens=1024, timeout=30.0),
                timeout=35.0,
            )
        except Exception as e:
            logger.warning(f"[SkillEvolver] LLM patch generation failed: {e}")
            return None

        return self._parse_patch_response(text)

    async def run_benchmark(
        self, skill_name: str, description: str, test_records: List[Dict],
    ) -> float:
        """
        用 prediction_records 评估 skill description 质量。
        返回: 0.0 ~ 1.0 得分（LLM 判定 description 能否正确引导调用）
        """
        if not test_records:
            return 0.5

        correct = 0
        total = min(len(test_records), 10)

        for record in test_records[:total]:
            query = record.get("prediction_text", record.get("query", ""))[:100]
            expected = skill_name

            prompt = self._BENCHMARK_PROMPT.format(
                tool_description=description[:500],
                user_query=query,
                expected_tool=expected,
            )
            try:
                import asyncio
                result = await asyncio.wait_for(
                    self._llm_call(prompt, use_small_fast=True, max_tokens=10, timeout=10.0),
                    timeout=12.0,
                )
                if "YES" in result.upper():
                    correct += 1
            except Exception:
                pass

        score = correct / total if total > 0 else 0.5
        return round(score, 3)

    async def apply_patch_if_validated(
        self, skill_name: str, skill_md_path: str,
        patch: Dict, before_score: float,
        user_id: int = 0, instance_id: str = "",
    ) -> bool:
        """benchmark after >= before - 0.02 则合并 patch"""
        # 获取测试数据
        test_records = await self._get_test_records(skill_name, user_id, instance_id)

        # after benchmark
        new_desc = patch.get("new_description", "")
        if not new_desc:
            return False

        after_score = await self.run_benchmark(skill_name, new_desc, test_records)

        # 验证通过条件
        if after_score >= before_score - 0.02:
            success = self._write_patch_to_skill_md(skill_md_path, patch)
            if success:
                self._last_patch_ts[skill_name] = time.time()
                logger.info(
                    f"[SkillEvolver] Patch applied: {skill_name}, "
                    f"before={before_score:.3f} after={after_score:.3f}"
                )
                return True

        logger.info(
            f"[SkillEvolver] Patch rejected: {skill_name}, "
            f"before={before_score:.3f} after={after_score:.3f}"
        )
        return False

    async def run_evolution_cycle(
        self, user_id: int, instance_id: str,
    ) -> List[str]:
        """完整进化周期，返回已进化的 skill 名称列表"""
        if not self._enabled:
            return []

        failing = await self.collect_failing_skills()
        if not failing:
            return []

        evolved = []
        for stat in failing:
            skill_name = stat["skill_name"]
            skill_md_path = self._find_skill_md(skill_name)
            if not skill_md_path:
                continue

            # 生成 patch
            errors = stat.get("recent_errors", [])
            patch = await self.generate_patch(skill_name, skill_md_path, errors)
            if not patch:
                continue

            # before benchmark
            test_records = await self._get_test_records(skill_name, user_id, instance_id)
            current = self._read_skill_md(skill_md_path)
            before_desc = current.get("description", "") if current else ""
            before_score = await self.run_benchmark(skill_name, before_desc, test_records)

            # 验证并应用
            applied = await self.apply_patch_if_validated(
                skill_name, skill_md_path, patch, before_score, user_id, instance_id,
            )
            if applied:
                evolved.append(skill_name)

        logger.info(f"[SkillEvolver] Evolution cycle: {len(evolved)} skills evolved")
        return evolved

    # ──── 私有方法 ────

    def _find_skill_md(self, skill_name: str) -> Optional[str]:
        """查找 SKILL.md 路径"""
        path = Path(self._skills_dir) / skill_name / "SKILL.md"
        if path.exists():
            return str(path)
        return None

    def _read_skill_md(self, path: str) -> Optional[Dict]:
        """解析 SKILL.md 的 YAML frontmatter"""
        try:
            text = Path(path).read_text(encoding="utf-8")
            # 提取 frontmatter
            match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
            if not match:
                return None

            import yaml
            data = yaml.safe_load(match.group(1))
            if not isinstance(data, dict):
                return None
            return data
        except Exception as e:
            logger.debug(f"[SkillEvolver] Failed to read {path}: {e}")
            return None

    def _write_patch_to_skill_md(self, path: str, patch: Dict) -> bool:
        """将 patch 写入 SKILL.md（只改 description/examples）"""
        try:
            text = Path(path).read_text(encoding="utf-8")
            match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
            if not match:
                return False

            import yaml
            data = yaml.safe_load(match.group(1))
            if not isinstance(data, dict):
                return False

            new_desc = patch.get("new_description", "")
            new_examples = patch.get("new_examples", "")

            if new_desc:
                data["description"] = new_desc
            if new_examples:
                data["examples"] = new_examples

            # 重新生成 frontmatter
            new_frontmatter = yaml.dump(
                data, allow_unicode=True, default_flow_style=False, sort_keys=False,
            )
            rest = text[match.end():]
            new_text = f"---\n{new_frontmatter}---{rest}"

            Path(path).write_text(new_text, encoding="utf-8")
            return True
        except Exception as e:
            logger.warning(f"[SkillEvolver] Failed to write patch to {path}: {e}")
            return False

    async def _get_test_records(
        self, skill_name: str, user_id: int = 0, instance_id: str = ""
    ) -> List[Dict]:
        """从 prediction_records 获取与该 skill 相关的测试数据"""
        try:
            # 优先使用传入的 user_id；若为 0 则查该 instance 下所有用户
            if user_id and user_id != 0:
                uids = [user_id]
            else:
                uids = await self._pred_store.get_distinct_user_ids(instance_id) or []

            all_records: List[Dict] = []
            for uid in uids:
                records = await self._pred_store.get_verified_for_learning(
                    user_id=uid, instance_id=instance_id,
                    limit=20, since_ts=time.time() - 30 * 86400,
                )
                all_records.extend(records or [])
            return all_records
        except Exception:
            return []

    def _parse_patch_response(self, text: str) -> Optional[Dict]:
        """解析 LLM patch 生成响应"""
        if not text:
            return None
        t = text.strip()
        if "```" in t:
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
            if m:
                t = m.group(1).strip()
        try:
            data = json.loads(t)
            if isinstance(data, dict) and data.get("new_description"):
                return data
        except json.JSONDecodeError:
            pass
        # fallback: 尝试找 { }
        try:
            start, end = t.find("{"), t.rfind("}")
            if start >= 0 and end > start:
                data = json.loads(t[start:end + 1])
                if isinstance(data, dict) and data.get("new_description"):
                    return data
        except json.JSONDecodeError:
            pass
        return None
