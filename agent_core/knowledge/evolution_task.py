"""
Evolution Task Manager — 长期自进化任务

独立存储 + 异步后台 + 跨重启续跑。
四阶段: Gap → Seek → Synthesize → Integrate。

参考: AgentEvolver + Agent0 + DeepResearchAgent Autogenesis。
"""
import json
import time
from pathlib import Path
from typing import List, Dict, Optional
from uuid import uuid4

from loguru import logger

from .models import EvolutionTask, KnowledgeUnit


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


class EvolutionTaskManager:
    """进化任务管理器 — 四阶段自主进化"""

    def __init__(self, store, config: dict, llm_call=None, temporal_manager=None,
                 skill_executor=None):
        self._store = store
        self._config = config.get("evolution", {})
        self._full_config = config
        self._llm_call = llm_call
        self._temporal_manager = temporal_manager
        self._skill_executor = skill_executor  # V4SkillExecutor，用于创建新 Skill

    async def create_task(
        self, gap: str, user_id: int, instance_id: str,
    ) -> Optional[EvolutionTask]:
        """创建进化任务"""
        if not self._config.get("enabled", True):
            return None

        max_tasks = self._config.get("max_tasks_per_user", 40)
        existing = await self._store.get_evolution_tasks(user_id, instance_id)
        pending = [t for t in existing if t.get("status") in ("pending", "in_progress")]
        if len(pending) >= self._config.get("max_concurrent_tasks", 3):
            logger.debug("[Evolution] Max concurrent tasks reached")
            return None
        if len(existing) >= max_tasks:
            logger.debug("[Evolution] Max tasks per user reached")
            return None

        task = EvolutionTask(
            user_id=user_id,
            instance_id=instance_id,
            gap_description=gap,
            status="pending",
            phase="gap",
        )
        await self._store.save_evolution_task(task)
        logger.info(f"[Evolution] Task created: {task.task_id}, gap={gap[:50]}")
        return task

    async def execute_pending_tasks(
        self, user_id: int, instance_id: str,
    ) -> List[str]:
        """执行待处理的进化任务"""
        if not self._config.get("enabled", True) or not self._llm_call:
            return []

        tasks = await self._store.get_evolution_tasks(
            user_id, instance_id, status="pending",
        )
        completed_ids = []

        for task_data in tasks[:self._config.get("max_concurrent_tasks", 3)]:
            task = EvolutionTask(
                task_id=task_data["task_id"],
                user_id=task_data["user_id"],
                instance_id=task_data["instance_id"],
                gap_description=task_data["gap_description"],
                status="in_progress",
                phase="gap",
                exploration_log=task_data.get("exploration_log", []),
                result_knowledge_ids=task_data.get("result_knowledge_ids", []),
            )

            try:
                await self._execute_task(task, user_id, instance_id)
                completed_ids.append(task.task_id)
            except Exception as e:
                task.status = "failed"
                task.updated_at = time.time()
                await self._store.save_evolution_task(task)
                logger.warning(f"[Evolution] Task {task.task_id} failed: {e}")

        return completed_ids

    async def _execute_task(
        self, task: EvolutionTask, user_id: int, instance_id: str,
    ):
        """执行单个进化任务的四个阶段"""
        timeout = self._config.get("seek_timeout_seconds", 60)
        max_text_chars = self._full_config.get("distiller", {}).get(
            "max_knowledge_text_chars", 100,
        )

        # Phase 1: Gap Analysis
        task.phase = "gap"
        task.status = "in_progress"
        await self._store.save_evolution_task(task)

        # 获取当前知识概况
        all_knowledge = await self._store.get_all_knowledge(user_id, instance_id)
        category_stats = {}
        total_utility = 0.0
        for k in all_knowledge:
            category_stats[k.category] = category_stats.get(k.category, 0) + 1
            total_utility += k.utility

        gap_prompt = _load_prompt(
            "evolution_gap_analysis",
            gap_description=task.gap_description,
            knowledge_count=len(all_knowledge),
            category_stats=json.dumps(category_stats, ensure_ascii=False),
            avg_utility=f"{total_utility / max(len(all_knowledge), 1):.2f}",
        )

        if gap_prompt:
            import asyncio
            gap_result = await asyncio.wait_for(
                self._llm_call(gap_prompt), timeout=timeout,
            )
            task.exploration_log.append({
                "phase": "gap",
                "result": gap_result[:500],
                "timestamp": time.time(),
            })

        # Phase 2: Seek
        task.phase = "seek"
        await self._store.save_evolution_task(task)

        existing_texts = "\n".join(k.text[:50] for k in all_knowledge[:10])
        seek_prompt = _load_prompt(
            "evolution_knowledge_seek",
            learning_direction=task.gap_description,
            query=task.gap_description,
            existing_knowledge=existing_texts[:500],
            max_knowledge_text_chars=max_text_chars,
        )

        seek_result = ""
        if seek_prompt:
            import asyncio
            seek_result = await asyncio.wait_for(
                self._llm_call(seek_prompt), timeout=timeout,
            )
            task.exploration_log.append({
                "phase": "seek",
                "result": seek_result[:500],
                "timestamp": time.time(),
            })

        # Phase 3: Synthesize
        task.phase = "synthesize"
        await self._store.save_evolution_task(task)

        synth_prompt = _load_prompt(
            "evolution_synthesis",
            exploration_results=seek_result[:1000],
            original_gap=task.gap_description,
            max_knowledge_text_chars=max_text_chars,
        )

        new_units = []
        if synth_prompt:
            import asyncio
            synth_result = await asyncio.wait_for(
                self._llm_call(synth_prompt), timeout=timeout,
            )
            new_units = self._parse_synthesis(synth_result)

        # Phase 3.5: 判断是否需要创建新 Skill
        if self._skill_executor and self._config.get("auto_create_skill", True):
            needs_new_skill = await self._llm_judge_needs_skill(
                task.gap_description, seek_result,
            )
            if needs_new_skill:
                try:
                    skill_result = await self._skill_executor.execute(
                        "skill-creator",
                        {
                            "action": "create",
                            "description": task.gap_description,
                            "reference": seek_result[:500],
                        },
                    )
                    task.exploration_log.append({
                        "phase": "skill_creation",
                        "result": str(skill_result)[:200],
                        "timestamp": time.time(),
                    })
                    logger.info(
                        f"[Evolution] Skill creation triggered for task {task.task_id}"
                    )
                except Exception as e:
                    logger.warning(f"[Evolution] Skill creation failed: {e}")
                    task.exploration_log.append({
                        "phase": "skill_creation",
                        "error": str(e)[:200],
                        "timestamp": time.time(),
                    })

        # Phase 4: Integrate
        task.phase = "integrate"
        for unit in new_units:
            unit.source_episode_id = f"evolution:{task.task_id}"
            await self._store.save_knowledge(unit, user_id, instance_id)
            task.result_knowledge_ids.append(unit.unit_id)

        # 完成
        task.status = "completed"
        task.completed_at = time.time()
        task.updated_at = time.time()

        # 创建认知快照
        if self._config.get("create_snapshot_on_complete", True) and self._temporal_manager:
            snapshot = await self._temporal_manager.create_snapshot(
                user_id, instance_id, snapshot_type="evolution",
            )
            task.knowledge_snapshot_id = snapshot.snapshot_id

        await self._store.save_evolution_task(task)
        logger.info(
            f"[Evolution] Task completed: {task.task_id}, "
            f"new_knowledge={len(new_units)}"
        )

    async def _llm_judge_needs_skill(
        self, gap_description: str, seek_result: str,
    ) -> bool:
        """LLM 判断进化任务是否需要创建新 Skill（而非仅写 KnowledgeUnit）"""
        if not self._llm_call:
            return False
        prompt = (
            f"分析以下能力缺口，判断是否需要创建一个新的工具（Skill）来解决。\n\n"
            f"缺口描述: {gap_description}\n"
            f"调研结果: {seek_result[:300]}\n\n"
            f"如果缺口可以通过补充知识规则解决，回答 NO。\n"
            f"如果需要一个新的可执行工具（如调用新API、新数据处理流程），回答 YES。\n"
            f"仅回答 YES 或 NO。"
        )
        try:
            import asyncio
            result = await asyncio.wait_for(self._llm_call(prompt), timeout=15)
            return "YES" in result.upper()
        except Exception:
            return False

    def _parse_synthesis(self, response: str) -> List[KnowledgeUnit]:
        """解析综合结果"""
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

            now = time.time()
            units = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                unit = KnowledgeUnit(
                    category=item.get("category", "domain_fact"),
                    text=str(item.get("text", "")),
                    tags=item.get("tags", []),
                    utility=min(1.0, max(0.0, float(item.get("utility", 0.5)))),
                    confidence=min(1.0, max(0.0, float(item.get("confidence", 0.5)))),
                    ingestion_time=now,
                    valid_from=now,
                    created_at=now,
                    last_accessed=now,
                )
                units.append(unit)
            return units
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
