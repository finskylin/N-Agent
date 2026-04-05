"""
Reflection Engine — 反思引擎

周期性聚合 Episode → 生成 Reflection → 沉淀到知识库。
含认知变迁对比: "之前认为 XX，现在发现 YY，原因是 ZZ"。

参考: Reflexion + EvolveR。
"""
import json
import time
from pathlib import Path
from typing import List, Dict, Optional
from uuid import uuid4

from loguru import logger

from .models import KnowledgeUnit


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


class ReflectionEngine:
    """反思引擎 — 周期性 Episode 聚合反思"""

    def __init__(self, store, config: dict, llm_call=None, temporal_manager=None):
        self._store = store
        self._config = config.get("reflection", {})
        self._full_config = config
        self._llm_call = llm_call
        self._temporal_manager = temporal_manager
        self._episode_count_since_reflect = 0
        self._last_reflect_time = 0.0
        self._consecutive_failures = 0

    async def maybe_reflect(
        self, user_id: int, instance_id: str,
    ) -> Optional[List[KnowledgeUnit]]:
        """
        条件触发反思:
        1. batch_size 达到
        2. interval 到期
        3. 连续失败触发
        """
        if not self._config.get("enabled", True):
            return None

        batch_size = self._config.get("batch_size", 10)
        interval_hours = self._config.get("interval_hours", 24)
        failure_trigger = self._config.get("consecutive_failure_trigger", 3)

        should_reflect = False
        reason = ""

        # 条件 1: Episode 数量
        self._episode_count_since_reflect += 1
        if self._episode_count_since_reflect >= batch_size:
            should_reflect = True
            reason = f"batch_size reached ({self._episode_count_since_reflect})"

        # 条件 2: 时间间隔
        elapsed_hours = (time.time() - self._last_reflect_time) / 3600
        if elapsed_hours >= interval_hours and self._episode_count_since_reflect > 0:
            should_reflect = True
            reason = f"interval reached ({elapsed_hours:.1f}h)"

        # 条件 3: 连续失败
        if self._consecutive_failures >= failure_trigger:
            should_reflect = True
            reason = f"consecutive failures ({self._consecutive_failures})"

        if not should_reflect:
            return None

        logger.info(f"[Reflection] Triggering reflection: {reason}")
        result = await self._reflect(user_id, instance_id)
        self._episode_count_since_reflect = 0
        self._last_reflect_time = time.time()
        self._consecutive_failures = 0
        return result

    def record_failure(self):
        """记录失败（用于连续失败触发）"""
        self._consecutive_failures += 1

    def record_success(self):
        """记录成功（重置连续失败计数）"""
        self._consecutive_failures = 0

    async def _reflect(
        self, user_id: int, instance_id: str,
    ) -> List[KnowledgeUnit]:
        """执行反思"""
        if not self._llm_call:
            return []

        batch_size = self._config.get("batch_size", 10)
        episodes = await self._store.get_recent_episodes(
            user_id, instance_id, limit=batch_size,
        )
        if not episodes:
            return []

        # 聚合统计
        skill_stats = self._aggregate_skill_stats(episodes)
        failure_patterns = self._find_failure_patterns(episodes)
        success_patterns = self._find_success_patterns(episodes)

        # 获取认知变迁（V3）
        cognition_changes_text = "[]"
        if self._config.get("include_cognition_changes", True):
            max_changes = self._full_config.get("retriever", {}).get(
                "max_cognition_changes_in_prompt", 3,
            )
            changes = await self._store.get_recent_cognition_changes(
                user_id, instance_id, limit=max_changes,
            )
            if changes:
                cognition_changes_text = json.dumps([
                    {"old": c.get("old_text", ""), "new": c.get("new_text", ""),
                     "reason": c.get("reason", "")}
                    for c in changes
                ], ensure_ascii=False)

        max_text_chars = self._full_config.get("distiller", {}).get(
            "max_knowledge_text_chars", 100,
        )

        prompt = _load_prompt(
            "knowledge_reflect_v3",
            skill_stats=json.dumps(skill_stats, ensure_ascii=False),
            failure_patterns=json.dumps(failure_patterns, ensure_ascii=False),
            success_patterns=json.dumps(success_patterns, ensure_ascii=False),
            cognition_changes=cognition_changes_text,
            max_knowledge_text_chars=max_text_chars,
        )

        if not prompt:
            return []

        try:
            per_ep_timeout = self._config.get("llm_timeout_seconds", 30)
            try:
                response = await self._llm_call(prompt, timeout=per_ep_timeout)
            except TypeError:
                response = await self._llm_call(prompt)

            units, gaps = self._parse_reflection_output(response, user_id, instance_id)

            # 保存反思知识
            for unit in units:
                await self._store.save_knowledge(
                    unit, user_id, instance_id, source_type="reflect"
                )

            # 自动创建认知快照
            if self._config.get("auto_snapshot", True) and self._temporal_manager:
                await self._temporal_manager.create_snapshot(
                    user_id, instance_id, snapshot_type="reflection",
                )

            # 衰减处理
            await self._store.decay_and_cleanup(user_id, instance_id)

            logger.info(
                f"[Reflection] Completed: {len(units)} knowledge, "
                f"{len(gaps)} gaps identified"
            )
            return units

        except Exception as e:
            logger.warning(f"[Reflection] Failed: {e}")
            return []

    def _parse_reflection_output(
        self, response: str, user_id: int, instance_id: str,
    ) -> tuple:
        """解析反思 LLM 输出"""
        try:
            text = response.strip()
            if "```" in text:
                start = text.find("{") if "{" in text else text.find("[")
                end = max(text.rfind("}"), text.rfind("]")) + 1
                if start >= 0 and end > start:
                    text = text[start:end]

            data = json.loads(text)

            # 支持两种格式
            if isinstance(data, list):
                items = data
                gaps = []
            elif isinstance(data, dict):
                items = data.get("knowledge", [])
                gaps = data.get("gaps", [])
            else:
                return [], []

            units = []
            now = time.time()
            for item in items:
                if not isinstance(item, dict):
                    continue
                unit = KnowledgeUnit(
                    unit_id=str(uuid4()),
                    category=item.get("category", "strategy_rule"),
                    text=str(item.get("text", "")),
                    tags=item.get("tags", []),
                    utility=min(1.0, max(0.0, float(item.get("utility", 0.6)))),
                    confidence=0.7,
                    ingestion_time=now,
                    valid_from=now,
                    source_episode_id="reflection",
                    created_at=now,
                    last_accessed=now,
                )
                units.append(unit)
            return units, gaps
        except (json.JSONDecodeError, TypeError, ValueError):
            return [], []

    @staticmethod
    def _aggregate_skill_stats(episodes: List[Dict]) -> Dict:
        """聚合 Skill 执行统计"""
        stats: Dict[str, Dict] = {}
        for ep in episodes:
            executions = ep.get("skill_executions", [])
            if isinstance(executions, str):
                try:
                    executions = json.loads(executions)
                except (json.JSONDecodeError, TypeError):
                    continue
            for se in executions:
                name = se.get("skill_name", "unknown")
                if name not in stats:
                    stats[name] = {"total": 0, "success": 0, "failure": 0, "avg_duration": 0}
                stats[name]["total"] += 1
                if se.get("success", True):
                    stats[name]["success"] += 1
                else:
                    stats[name]["failure"] += 1
                dur = se.get("duration_ms", 0)
                old_avg = stats[name]["avg_duration"]
                stats[name]["avg_duration"] = (old_avg * (stats[name]["total"] - 1) + dur) / stats[name]["total"]
        return stats

    @staticmethod
    def _find_failure_patterns(episodes: List[Dict]) -> List[str]:
        """识别失败模式"""
        patterns = []
        for ep in episodes:
            if not ep.get("success", True):
                patterns.append(f"Query: {ep.get('query', '')[:50]}")
        return patterns[:5]

    @staticmethod
    def _find_success_patterns(episodes: List[Dict]) -> List[str]:
        """识别成功模式"""
        patterns = []
        for ep in episodes:
            if ep.get("success", True):
                executions = ep.get("skill_executions", [])
                if isinstance(executions, str):
                    try:
                        executions = json.loads(executions)
                    except (json.JSONDecodeError, TypeError):
                        continue
                skills = [se.get("skill_name", "") for se in executions]
                if skills:
                    patterns.append(f"Skills: {', '.join(skills)}")
        return patterns[:5]
